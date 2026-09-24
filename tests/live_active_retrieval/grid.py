"""Explicit, frozen AR-D real baseline/candidate journeys. Never invoked by pytest."""

import argparse
import asyncio
import json
import math
import re
import time
from dataclasses import asdict, replace
from pathlib import Path

from memory_fixtures import Resolver, memory_policy
from plugin_fixtures import ScriptedPlugin
from plugin_fixtures import manifest as plugin_manifest
from retrieval_fixtures import retrieval_policy
from skill_fixtures import contribution, digest, discovery, with_resource
from skill_fixtures import policy as skill_policy

from live_active_retrieval.contract import verify_manifest
from live_active_retrieval.fixtures import materialize
from live_active_retrieval.history_smoke import file_digest, load_provider
from traceh.api.history import HistoryReadPolicy
from traceh.api.memory import ProjectMemoryConfig, ProjectScopeLimits
from traceh.api.sandbox import SandboxConfiguration
from traceh.api.skills import SkillResourceRoot, SkillSection, SkillSectionContent
from traceh.llm.retry import ModelRetryPolicy
from traceh.llm.token_meter import TokenBudgetPolicy
from traceh.projects.events import reference
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime_async
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.sandbox.config import load_sandbox_file, parse_sandbox_config
from traceh.session.context_input import ContextInputPolicy
from traceh.session.sqlite import SqliteEventStore
from traceh.tools.policy import DecisionKind, ToolDecision


def write(path, data, *, exclusive=False):
    with path.open("x" if exclusive else "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def source_files(root):
    return {
        str(p.relative_to(root)).replace("\\", "/"): file_digest(p)
        for p in sorted(root.rglob("*.py"))
    }


def support_files():
    root = Path(__file__).resolve().parents[1]
    names = (
        "live_active_retrieval/grid.py",
        "live_active_retrieval/fixtures.py",
        "live_active_retrieval/history_smoke.py",
        "live_active_retrieval/contract.py",
        "live_active_retrieval/manifest.json",
        "memory_fixtures.py",
        "retrieval_fixtures.py",
        "skill_fixtures.py",
        "plugin_fixtures.py",
    )
    return {name: file_digest(root / name) for name in names}


def context_policy(limits, family):
    retrieval = retrieval_policy(
        default_tier="directory",
        context_bytes=limits["context_kind_bytes"],
        max_catalog_bytes=limits["max_catalog_bytes"],
        max_corpus_items=limits["max_corpus_items"],
        max_corpus_bytes=limits["max_corpus_bytes"],
        max_requests=limits["max_requests"],
    )
    history = HistoryReadPolicy(
        max_blocks=limits["history_max_blocks"],
        max_depth=limits["history_max_depth"],
        page_bytes=limits["history_page_bytes"],
        page_messages=limits["history_page_messages"],
        max_source_events=limits["history_max_source_events"],
        max_source_bytes=limits["history_max_source_bytes"],
        max_requests=limits["max_requests"],
    )
    return ContextInputPolicy(
        history_tier="directory" if family in {"history", "output"} else None,
        total_bytes=limits["context_total_bytes"],
        history_bytes=limits["context_kind_bytes"],
        item_bytes=limits["context_item_bytes"],
        max_blocks=limits["context_max_blocks"],
        max_exclusions=limits["context_max_exclusions"],
        max_query_bytes=limits["max_query_bytes"],
        history=history if family in {"history", "output"} else None,
        skills=retrieval if family == "skill" else None,
        memory=retrieval if family == "memory" else None,
    )


class SourceOnly:
    """Explicit evaluation scope: original reference owner; one setup command for output."""

    name = "ar-d-frozen-source-scope"

    def __init__(self, family, workspace):
        self.family = family
        self.workspace = workspace
        self.setup = True
        self._setup_claimed = False
        self.allowed = {
            "history": {"search_history", "request_history_page"},
            "skill": {"search_skill", "request_skill_reference"},
            "memory": {"search_memory", "request_workspace_memory"},
            "output": {"list_tool_outputs", "search_tool_output", "read_tool_output"},
        }[family]

    async def check(self, call, tool, context):
        allowed = call.name in self.allowed
        if self.family == "output" and self.setup and call.name == "shell":
            allowed = (
                call.arguments.get("command") == "python check.py"
                and not self._setup_claimed
                and not (self.workspace / "execution-count.txt").exists()
            )
            if allowed:
                self._setup_claimed = True
        return ToolDecision(
            DecisionKind.ALLOW if allowed else DecisionKind.DENY,
            "Use this task's configured reference source. Workspace exploration and rerunning "
            "historical commands are outside this isolated task.",
            self.name,
        )


def skill_value(fixture, resource_root):
    value = contribution(fixture["plugin_id"], fixture["skill_id"], "placeholder")
    sections = tuple(
        SkillSection(
            item["id"],
            "section",
            digest(item["body"]),
            len(item["body"].encode("utf-8")),
            title=item["title"],
            summary=item["summary"],
        )
        for item in fixture["sections"]
    )
    value = replace(
        value,
        descriptor=replace(
            value.descriptor,
            title=fixture["skill_title"],
            summary=fixture["skill_summary"],
            tags=(),
            sections=sections,
        ),
        sections=tuple(
            SkillSectionContent(item["id"], item["body"]) for item in fixture["sections"]
        ),
    )
    for item in fixture["resources"]:
        (resource_root / item["path"]).write_text(item["body"], encoding="utf-8")
        value = with_resource(value, item["path"], item["body"])
        resource = value.descriptor.resources[-1]
        resource = replace(
            resource,
            resource_id=item["id"],
            title=item["title"],
            summary=item["summary"],
            chunks=(
                replace(
                    resource.chunks[0],
                    chunk_id=item["chunk_id"],
                    title=item["title"],
                    summary=item["summary"],
                ),
            ),
        )
        value = replace(value, descriptor=replace(value.descriptor, resources=(resource,)))
    return value


async def prepare_runtime(folder, fixture, frozen, provider, model):
    limits, family = frozen["manifest"]["limits"], fixture["family"]
    workspace, resources = folder / "workspace", folder / "plugin-resources"
    workspace.mkdir()
    resources.mkdir()
    scope = SourceOnly(family, workspace)
    memory = None
    if family == "memory":
        memory = ProjectMemoryConfig(
            ProjectScopeLimits(100, 100),
            memory_policy(),
            Resolver({fixture["source_id"]: workspace}),
        )
    config = RuntimeConfig(
        data_dir=folder / "data",
        provider=provider.name,
        model=model,
        temperature=limits["temperature"],
        max_steps=limits["max_steps"],
        max_output_tokens=limits["max_output_tokens"],
        max_tool_output_chars=limits["max_tool_output_chars"],
        token_budget=TokenBudgetPolicy(
            "cl100k_base",
            limits["request_window_tokens"],
            limits["max_output_tokens"],
            limits["token_safety_margin"],
        ),
        model_retry_policy=ModelRetryPolicy(limits["max_model_attempts"], 180, 1, 5, 5, 0),
        context_input=context_policy(limits, family),
        memory=memory,
        sandbox=(
            SandboxConfiguration(
                parse_sandbox_config(frozen["sandbox_config"]).policy,
                (folder / "sandbox-cas").resolve(),
            )
            if frozen.get("sandbox_config") is not None
            else None
        ),
    )
    options = {}
    value = None
    if family == "skill":
        value = skill_value(fixture, resources)
        config = replace(
            config,
            skill_policy=skill_policy(
                SkillResourceRoot(value.descriptor.plugin, resources),
                max_catalog_bytes=limits["max_catalog_bytes"],
            ),
        )
        options = {
            "enabled_plugins": (fixture["plugin_id"],),
            "plugin_discovery": discovery(
                ScriptedPlugin(plugin_manifest(fixture["plugin_id"]), skills=(value,))
            ),
        }
    store = SqliteEventStore(folder / "events")
    try:
        runtime = await build_default_runtime_async(
            config, provider=provider, event_store=store, policies=(scope,), **options
        )
    except BaseException:
        await store.aclose()
        raise
    try:
        session = await runtime.create_session(workspace)
    except BaseException:
        await runtime.dispose()
        await store.aclose()
        raise
    return runtime, store, session, scope, value


async def prepare_memory(runtime, session, fixture):
    project, source = fixture["project_id"], fixture["source_id"]
    await runtime.project_scope.create(
        project_id=project,
        label=fixture["domain_name"],
        operation_id="create",
        actor_id="evaluation-host",
        expected_head=0,
    )
    await runtime.project_scope.bind_source(
        project_id=project,
        source_id=source,
        operation_id="source",
        actor_id="evaluation-host",
        expected_head=1,
    )
    await runtime.project_scope.bind_session(
        session,
        project_id=project,
        operation_id="bind",
        actor_id="evaluation-host",
        expected_head=2,
    )
    head = 0

    async def approve(memory_id, slot, body, predecessor=None):
        nonlocal head
        operation = "statement-" + str(head)
        proposal = await runtime.memory.declare(
            session,
            proposal_id=operation,
            body=body,
            statement=body,
            declaration_id=operation,
            operation_id=operation,
            actor_id="evaluation-host",
            expected_head=head,
        )
        head += 1
        arguments = dict(
            proposal_ref=reference(proposal),
            proposal_digest=proposal.data["proposal_digest"],
            memory_id=memory_id,
            fact_slot=slot,
            operation_id="approve-" + str(head),
            actor_id="evaluation-host",
            expected_head=head,
        )
        if predecessor:
            old_proposal, old_activation = predecessor
            activation = await runtime.memory.supersede(
                session,
                **arguments,
                predecessor_ref=reference(old_activation),
                predecessor_digest=old_proposal.data["proposal_digest"],
            )
        else:
            activation = await runtime.memory.approve(session, **arguments)
        head += 1
        return proposal, activation

    for item in fixture["memory_noise"]:
        await approve(item["id"], item["slot"], item["body"])
    predecessor = None
    if fixture["mode"] == "superseded":
        predecessor = await approve(
            "predecessor-" + fixture["memory_id"],
            fixture["fact_slot"],
            "先前批准的档案整理人数上限为 " + fixture["predecessor_value"] + " 人。",
        )
    proposal, activation = await approve(
        fixture["memory_id"], fixture["fact_slot"], fixture["source"], predecessor
    )
    if fixture["mode"] == "revoked":
        await runtime.memory.revoke(
            session,
            memory_id=fixture["memory_id"],
            fact_slot=fixture["fact_slot"],
            predecessor_ref=reference(activation),
            predecessor_digest=proposal.data["proposal_digest"],
            operation_id="revoke",
            actor_id="evaluation-host",
            expected_head=head,
        )
    await runtime.memory.rebuild_index(session)


def output_program(fixture):
    return (
        "from pathlib import Path\nimport sys\n"
        "counter = Path('execution-count.txt')\n"
        "count = int(counter.read_text()) if counter.exists() else 0\n"
        "counter.write_text(str(count + 1))\n"
        "sys.stdout.reconfigure(encoding='utf-8')\n"
        f"sys.stdout.write({fixture['output_text']!r})\n"
        f"raise SystemExit({fixture['exit_code']})\n"
    )


async def setup_source(runtime, session, scope, value, fixture, manifest):
    family = fixture["family"]
    if family == "history":
        for index, text in enumerate(fixture["history_turns"]):
            await runtime.run_existing(session, "请记下这条记录，只回复已记下：" + text)
            print(json.dumps({"case": fixture["identity"], "setup_turn": index + 1}), flush=True)
        events = await runtime.sessions.read_session(session)
        await runtime.compaction.replace_through(
            session, through_seq=events[-1].seq, summary=manifest["fixtures"]["compaction_summary"]
        )
    elif family == "memory":
        if fixture["mode"] != "unbound":
            await prepare_memory(runtime, session, fixture)
    elif family == "skill":
        await runtime.skill_context.select(
            session,
            operation_id="select",
            expected_head=0,
            actor_id="evaluation-host",
            skills=({"skill_id": value.descriptor.skill_id, "version": value.descriptor.version},),
        )
        await runtime.skill_context.rebuild_index(session)
    else:
        (scope.workspace / "check.py").write_text(output_program(fixture), encoding="utf-8")
        await runtime.run_existing(
            session, "执行 python check.py 一次。这是本次检查；运行后仅告知检查结束，不解读输出。"
        )
        counter = scope.workspace / "execution-count.txt"
        if not counter.exists() or counter.read_text() != "1":
            raise ValueError("ar-output-setup-not-executed-once")
    scope.setup = False


def evidence(events, fixture):
    value, family = fixture["value"], fixture["family"]
    found = []
    for event in events:
        if family != "output" and event.type == "context/input":
            for block in event.data["blocks"]:
                if block["kind"] != family:
                    continue
                if block["tier"] == "search":
                    for hit in json.loads(block["body"])["hits"]:
                        if (
                            value
                            and value in hit["text"]
                            and (
                                family != "memory" or hit["reference"]["id"] == fixture["memory_id"]
                            )
                        ):
                            found.append({"context_seq": event.seq, "reference": hit["reference"]})
                elif (
                    block["tier"] in {"summary", "section", "chunk"}
                    and value
                    and value in block["body"]
                ):
                    if family == "memory" and block["id"] != fixture["memory_id"]:
                        continue
                    found.append(
                        {
                            "context_seq": event.seq,
                            "id": block["id"],
                            "version": block["version"],
                            "tier": block["tier"],
                        }
                    )
        if (
            family == "output"
            and event.type == "tool/result"
            and event.data["status"] == "succeeded"
        ):
            name = event.data["tool_name"]
            if name not in {"read_tool_output", "search_tool_output"}:
                continue
            page = json.loads(event.data["content"])
            texts = (
                [page["text"]]
                if name == "read_tool_output"
                else [
                    hit[key]
                    for hit in page["matches"]
                    for key in ("before_context", "matched_lines", "after_context")
                ]
            )
            if value and any(value in text for text in texts):
                found.append(
                    {
                        "tool_result_seq": event.seq,
                        "effect_id": page["effect_id"],
                        "digest": page["digest"],
                    }
                )
    return found


def answer_matches(answer, fixture):
    value = fixture["value"]
    if fixture["expected"] == "no-evidence":
        return False  # Negative conclusions require the recorded manual evidence review.
    if fixture["value_type"] == "number":
        return re.search(r"(?<!\d)" + re.escape(value) + r"(?!\d)", answer) is not None
    return value in answer


def usage(attempts):
    known = [
        a["usage"]
        for a in attempts
        if a.get("usage") is not None and a["usage"].get("quality") in {"exact", "estimated"}
    ]
    return {
        "attempts": len(attempts),
        "unknown_usage": len(attempts) - len(known),
        "estimated_usage": sum(a["quality"] == "estimated" for a in known),
        **{
            key: sum(a.get(key, 0) for a in known)
            for key in ("input_tokens", "output_tokens", "total_tokens")
        },
    }


async def run_case(folder, fixture, frozen, provider, model):
    folder.mkdir(parents=True, exist_ok=False)
    write(folder / "fixture.json", fixture)
    report = {
        "identity": fixture["identity"],
        "case_id": fixture["id"],
        "seed": fixture["seed"],
        "family": fixture["family"],
        "question": fixture["question"],
        "closed": False,
        "review": "pending",
        "provisional_joint_pass": False,
    }
    write(folder / "report.json", report)
    runtime = store = None
    before = None
    started = time.monotonic()
    try:
        runtime, store, session, scope, value = await prepare_runtime(
            folder, fixture, frozen, provider, model
        )
        report["session_id"] = session
        await setup_source(runtime, session, scope, value, fixture, frozen["manifest"])
        events = await runtime.sessions.read_session(session)
        before = len(events)
        report["target_start_seq"] = before
        write(folder / "report.json", report)
        target_start = time.monotonic()
        result = await runtime.run_existing(session, fixture["question"])
        report.update(
            answer=result.final_text,
            steps=result.steps,
            reason=result.reason,
            target_seconds=time.monotonic() - target_start,
        )
    except Exception as error:
        report["error"] = {"type": type(error).__name__, "code": getattr(error, "code", None)}
    finally:
        if runtime is not None:
            try:
                events = await store.read("session:" + session)
                effects = await runtime.sessions.read_effects(session)
                write(folder / "source-events.json", [e.to_dict() for e in events])
                write(folder / "effect-events.json", [e.to_dict() for e in effects])
                target = events[before:] if before is not None else ()
                report["target_usage"] = usage(
                    [e.data for e in target if e.type == "model/attempt-end"]
                )
                report["all_usage"] = usage(
                    [e.data for e in events if e.type == "model/attempt-end"]
                )
                report["evidence"] = evidence(target, fixture)
                report["replay_errors"] = list(
                    await verify_request_snapshots(runtime.sessions, runtime.surface, session)
                )
                report["invariant_errors"] = [
                    str(v) for v in await runtime.check_invariants(session)
                ]
                report["tool_calls"] = [
                    {"name": e.data["tool_name"], "arguments": e.data["arguments"]}
                    for e in target
                    if e.type == "tool/call"
                ]
                report["search_read_count"] = sum(
                    call["name"].startswith(("search_", "request_", "read_tool_"))
                    for call in report["tool_calls"]
                )
                counter = folder / "workspace" / "execution-count.txt"
                report["output_executions"] = int(counter.read_text()) if counter.exists() else None
                report["provisional_joint_pass"] = (
                    not report.get("error")
                    and answer_matches(report.get("answer", ""), fixture)
                    and bool(report["evidence"])
                    and not report["replay_errors"]
                    and not report["invariant_errors"]
                    and (fixture["family"] != "output" or report["output_executions"] == 1)
                )
            except Exception as error:
                report["audit_error"] = {
                    "type": type(error).__name__,
                    "code": getattr(error, "code", None),
                }
            finally:
                await runtime.dispose()
        if store is not None:
            await store.aclose()
        report["elapsed_seconds"] = time.monotonic() - started
        report["closed"] = True
        write(folder / "report.json", report)
    return report


def freeze(args):
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    evidence_root = Path(__file__).resolve().parents[2] / "docs/validation-data/active-retrieval"
    manifest = verify_manifest(
        Path(__file__).with_name("manifest.json"),
        json.loads((evidence_root / "manifest-freeze.json").read_text(encoding="utf-8")),
    )
    provider, model = load_provider(args.profile)
    identity = json.loads((evidence_root / "provider-freeze.json").read_text(encoding="utf-8"))
    if provider.name != identity["provider"] or model != identity["model"]:
        raise ValueError("ar-frozen-provider-changed")
    # Compare only public resolved provider/model identities; never serialize connection secrets.
    frozen = {
        "manifest": manifest,
        "provider": provider.name,
        "model": model,
        "provider_freeze_sha256": file_digest(evidence_root / "provider-freeze.json"),
        "support_files": support_files(),
        "source_files": {
            "baseline": source_files(args.baseline_source.resolve()),
            "candidate": source_files(Path(__file__).resolve().parents[2] / "src"),
        },
        "context_policies": {
            family: context_policy(manifest["limits"], family).to_dict()
            for family in ("history", "skill", "memory", "output")
        },
        "execution": (
            "two isolated arms, two disjoint sequential workers per arm; source-specific Tool scope"
        ),
        "provider_identity_reference": identity,
    }
    settings = load_sandbox_file(args.sandbox_config) if args.sandbox_config else None
    if settings is not None and settings.plugin_grants:
        raise ValueError("active-retrieval-grid-plugin-grants-unsupported")
    frozen["sandbox_config"] = (
        {"format": 2, "policy": asdict(settings.policy), "plugin_grants": []}
        if settings is not None
        else None
    )
    write(root / "fixtures.json", materialize(manifest), exclusive=True)
    frozen["fixtures_sha256"] = file_digest(root / "fixtures.json")
    write(root / "frozen.json", frozen, exclusive=True)
    print(
        json.dumps(
            {"frozen": str(root), "cases_per_arm": 72, "provider": provider.name, "model": model}
        )
    )


def installed_source_root():
    import traceh

    return Path(traceh.__file__).resolve().parent.parent


async def run(args):
    root = args.output.resolve()
    frozen = json.loads((root / "frozen.json").read_text(encoding="utf-8"))
    if frozen["support_files"] != support_files() or frozen["fixtures_sha256"] != file_digest(
        root / "fixtures.json"
    ):
        raise ValueError("ar-frozen-support-changed")
    source = installed_source_root()
    if source_files(source) != frozen["source_files"][args.arm]:
        raise ValueError("ar-frozen-source-changed")
    provider, model = load_provider(args.profile)
    if provider.name != frozen["provider"] or model != frozen["model"]:
        raise ValueError("ar-frozen-provider-changed")
    provider.timeout_seconds = frozen["manifest"]["limits"]["provider_timeout_seconds"]
    fixtures = json.loads((root / "fixtures.json").read_text(encoding="utf-8"))
    fixtures = [fixture for index, fixture in enumerate(fixtures) if index % 2 == args.worker]
    worker_id = args.arm + "-" + str(args.worker)
    reports = []
    for fixture in fixtures:
        folder = root / args.arm / fixture["identity"]
        if folder.exists():
            report = json.loads((folder / "report.json").read_text(encoding="utf-8"))
            if not report["closed"]:
                raise ValueError("ar-incomplete-journey-needs-explicit-audit")
        else:
            report = await run_case(folder, fixture, frozen, provider, model)
        reports.append(report)
        write(
            root / (worker_id + "-progress.json"),
            {
                "arm": args.arm,
                "closed": len(reports),
                "scored_review_complete": False,
                "reports": reports,
            },
        )
        print(
            json.dumps(
                {
                    "arm": args.arm,
                    "case": fixture["identity"],
                    "closed": len(reports),
                    "steps": report.get("steps"),
                    "provisional": report["provisional_joint_pass"],
                    "error": report.get("error"),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    totals = usage([])
    for report in reports:
        for key, value in report.get("all_usage", {}).items():
            totals[key] += value
    write(
        root / (worker_id + "-complete.json"),
        {
            "arm": args.arm,
            "closed": len(reports),
            "scored_review_complete": False,
            "all_usage": totals,
            "mean_target_seconds": sum(r.get("target_seconds", 0) for r in reports) / len(reports),
            "p95_target_seconds": sorted(r.get("target_seconds", 0) for r in reports)[
                math.ceil(0.95 * len(reports)) - 1
            ],
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--freeze", action="store_true")
    parser.add_argument("--baseline-source", type=Path)
    parser.add_argument("--sandbox-config", type=Path)
    parser.add_argument("--arm", choices=("baseline", "candidate"))
    parser.add_argument("--worker", type=int, choices=(0, 1), default=0)
    arguments = parser.parse_args()
    if arguments.freeze:
        if arguments.baseline_source is None:
            parser.error("--freeze requires --baseline-source")
        freeze(arguments)
    else:
        if arguments.arm is None:
            parser.error("a scored run requires --arm")
        asyncio.run(run(arguments))
