"""Explicit ordinary/competing-source controls through the original Runtime, not a scorer."""

import argparse
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from live_unified_evaluation.baseline import connection

from traceh.api.json_types import fingerprint, to_json_value
from traceh.api.memory import MemoryPolicy, ProjectMemoryConfig, ProjectScopeLimits
from traceh.api.plugins import PluginIdentity
from traceh.api.sandbox import SandboxConfiguration
from traceh.api.skills import (
    SkillContribution,
    SkillDescriptor,
    SkillLimits,
    SkillPolicy,
    SkillSection,
    SkillSectionContent,
)
from traceh.concurrency import await_worker_convergence, combine_failures
from traceh.evaluation.evaluators.episode_assessment import dispatched_evidence, usage
from traceh.evaluation.evaluators.episode_setup import (
    SOURCE_TOOLS,
    EpisodeResolver,
    MaterialDiscovery,
    PreparedEpisode,
    SourceScope,
    _memory,
)
from traceh.evaluation.inputs import digest_bytes
from traceh.evaluation.variants import source_digest, source_files
from traceh.llm.token_meter import TokenBudgetPolicy
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime_async
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.sandbox.config import load_sandbox_file
from traceh.session.context_input import ContextInputPolicy
from traceh.session.invariants import CoreInvariantChecker
from traceh.session.service import SessionService
from traceh.session.sqlite import SqliteEventStore
from traceh.session.surface import SurfaceProjector


def contribution(raw):
    body = raw["body"].encode()
    section = SkillSection(
        raw["section_id"],
        "section",
        digest_bytes(body),
        len(body),
        title=raw["section_title"],
        summary=raw["section_summary"],
    )
    descriptor = SkillDescriptor(
        raw["skill_id"],
        raw["version"],
        PluginIdentity(raw["plugin_id"], raw["version"]),
        raw["title"],
        raw["summary"],
        (),
        ">=0.4,<1.0",
        (section,),
        (),
    )
    return SkillContribution(descriptor, (SkillSectionContent(raw["section_id"], raw["body"]),))


def context_policy(settings):
    context = dict(settings["contexts"]["history"])
    for family in ("memory", "skills"):
        source = "skill" if family == "skills" else family
        context[family] = settings["contexts"][source][family]
    return ContextInputPolicy.from_dict(context)


@asynccontextmanager
async def prepared_control(folder, spec, settings, provider, model, sandbox):
    folder.mkdir(parents=True, exist_ok=False)
    workspace = folder / "workspace"
    workspace.mkdir()
    skill = contribution(spec["skill"])
    memory = ProjectMemoryConfig(
        ProjectScopeLimits(**settings["project_limits"]),
        MemoryPolicy(
            **{
                **settings["memory_policy"],
                "denied_patterns": tuple(settings["memory_policy"]["denied_patterns"]),
            }
        ),
        EpisodeResolver(spec["memory"]["source_id"], workspace),
    )
    scope = SourceScope("skill", spec["output"]["command"])
    scope.allowed = frozenset().union(*SOURCE_TOOLS.values())
    raw = settings["runtime"]
    config = RuntimeConfig(
        data_dir=folder / "data",
        provider=provider.name,
        model=model,
        **{k: v for k, v in raw.items() if k != "token_budget"},
        token_budget=TokenBudgetPolicy(**raw["token_budget"]),
        context_input=context_policy(settings),
        memory=memory,
        skill_policy=SkillPolicy(SkillLimits(**settings["skill_limits"]), ()),
        sandbox=SandboxConfiguration(sandbox, (folder / "cas").resolve()) if sandbox else None,
    )
    store, runtime, primary = SqliteEventStore(folder / "events"), None, None
    try:
        runtime = await build_default_runtime_async(
            config,
            provider=provider,
            event_store=store,
            policies=(scope,),
            enabled_plugins=(skill.descriptor.plugin.plugin_id,),
            plugin_discovery=MaterialDiscovery(skill),
        )
        session = await runtime.create_session(workspace)
        yield PreparedEpisode(runtime, store, session, session, workspace, scope, skill)
    except BaseException as error:
        primary = error
    finally:

        async def close():
            failure = None
            if runtime is not None:
                try:
                    await runtime.dispose()
                except BaseException as error:
                    failure = error
            try:
                await store.aclose()
            except BaseException as error:
                failure = combine_failures(failure, error, "control store close failed")
            if failure is not None:
                raise failure

        worker = asyncio.create_task(close())
        try:
            await asyncio.shield(worker)
        except BaseException as error:
            await await_worker_convergence(worker)
            cleanup = worker.exception() if not worker.cancelled() else error
            primary = combine_failures(primary, cleanup or error, "control cleanup failed")
        if primary is not None:
            raise primary


async def run_control(folder, spec, case, settings, provider, model, sandbox):
    events, effects, replay, invariants = (), (), (), ()
    before, result, failure, session, count = 0, None, None, None, None
    try:
        async with prepared_control(folder, spec, settings, provider, model, sandbox) as prepared:
            runtime, session = prepared.runtime, prepared.session_id
            await _memory(prepared, spec["memory"])
            descriptor = prepared.contribution.descriptor
            await runtime.skill_context.select(
                session,
                operation_id="select",
                expected_head=0,
                actor_id="evaluation-host",
                skills=({"skill_id": descriptor.skill_id, "version": descriptor.version},),
            )
            await runtime.skill_context.rebuild_index(session)
            for text in spec["history"]["turns"]:
                prepared_turn = await runtime.run_existing(
                    session, spec["history"]["prompt_prefix"] + text
                )
                if prepared_turn.reason != "completed":
                    raise ValueError("control-history-preparation-failed")
            output = spec["output"]
            (prepared.workspace / output["script_path"]).write_text(
                output["script"], encoding="utf-8"
            )
            prepared_turn = await runtime.run_existing(session, output["prompt"])
            counter = prepared.workspace / output["counter_path"]
            if (
                prepared_turn.reason != "completed"
                or not counter.is_file()
                or counter.read_text() != "1"
            ):
                raise ValueError("control-output-not-executed-once")
            prepared.scope.preparing = False
            original = await runtime.sessions.read_session(session)
            await runtime.compaction.replace_through(
                session, through_seq=original[-1].seq, summary=spec["history"]["summary"]
            )
            before = (await runtime.sessions.read_session(session))[-1].seq
            result = await runtime.run_existing(session, case["question"])
            count = counter.read_text()
    except Exception as error:
        failure = type(error).__name__
    # Independent reopening also preserves a failed preparation's original evidence.
    if session is not None:
        store = SqliteEventStore(folder / "events")
        try:
            sessions = SessionService(store)
            events = await sessions.read_session(session)
            effects = await sessions.read_effects(session)
            replay = await verify_request_snapshots(sessions, SurfaceProjector(), session)
            invariants = [str(v) for v in CoreInvariantChecker().check(events, effects)]
        finally:
            await store.aclose()
    target = [e for e in events if before and e.seq > before]
    evidence = []
    if result is not None and result.reason == "completed" and case["family"]:
        family = case["family"]
        source = {
            "history": spec["history"]["turns"][0],
            "skill": spec["skill"]["body"],
            "memory": spec["memory"]["items"][0]["body"],
            "output": spec["output"]["source_text"],
        }[family]
        reference = {
            "skill": spec["skill"]["skill_id"],
            "memory": spec["memory"]["items"][0]["memory_id"],
        }.get(family, "")
        evidence = dispatched_evidence(
            events,
            effects,
            {
                "family": family,
                "expectation": {
                    "kind": "value",
                    "value": case["expected_value"],
                    "source_text": source,
                    "reference_id": reference,
                },
            },
            target_start=before,
            target_turn=result.turn_id,
            max_chars=settings["runtime"]["max_tool_output_chars"],
        )
    return {
        **case,
        "session_id": session,
        "target_start_seq": before,
        "target_turn_id": result.turn_id if result else None,
        "answer": result.final_text if result else None,
        "reason": result.reason if result else None,
        "failure": failure,
        "evidence": evidence,
        "replay_errors": list(replay),
        "invariant_errors": list(invariants),
        "output_execution_count": count,
        "event_digest": fingerprint([e.to_dict() for e in events]),
        "calls": [{"seq": e.seq, **e.data} for e in target if e.type == "tool/call"],
        "usage": {"all": usage(events), "target": usage(target)},
        "assessment": "pending_review",
    }


async def execute(options):
    args, provider, model = connection(options.profile)
    spec = json.loads(options.materials.read_text(encoding="utf-8"))
    settings = json.loads((options.benchmark / "benchmark.json").read_text(encoding="utf-8"))[
        "task_settings"
    ]
    sandbox = load_sandbox_file(options.sandbox).policy
    output = options.output.resolve()
    if output.is_relative_to(options.benchmark.resolve()) or output.is_relative_to(
        options.materials.resolve().parent
    ):
        raise ValueError("control-output-overlap")
    output.mkdir(parents=True, exist_ok=False)
    code_digest = source_digest(source_files()[1])
    drivers = {}
    for name in ("controls.py", "baseline.py"):
        data = await asyncio.to_thread(Path(__file__).with_name(name).read_bytes)
        (output / name).write_bytes(data)
        drivers[name] = digest_bytes(data)
    frozen = {
        "format": 1,
        "source_digest": code_digest,
        "materials": spec,
        "settings": settings,
        "context_policy": context_policy(settings).to_dict(),
        "sandbox": to_json_value(sandbox),
        "provider": provider.name,
        "model": model,
        "connection_digest": digest_bytes(args.base_url.encode()),
        "network": "direct",
        "timeout_seconds": 60,
        "ordinary_and_competing_separate_from_72": True,
        "drivers": drivers,
    }
    (output / "frozen.json").write_text(
        json.dumps(frozen, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    rows = []
    for index, case in enumerate(spec["cases"], 1):
        if source_digest(source_files()[1]) != code_digest:
            raise ValueError("control-source-drift")
        print(json.dumps({"case": case["case_id"], "event": "started"}), flush=True)
        row = await run_control(
            output / f"attempts/{index:03d}", spec, case, settings, provider, model, sandbox
        )
        rows.append(row)
        (output / f"case-{index:03d}.json").write_text(
            json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {"case": case["case_id"], "failure": row["failure"], "calls": len(row["calls"])}
            ),
            flush=True,
        )
        if row["failure"] is not None:
            break
    (output / "report.json").write_text(
        json.dumps(
            {
                "frozen_digest": fingerprint(frozen),
                "rows": rows,
                "not_started": spec["cases"][len(rows) :],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("profile", "sandbox", "benchmark", "materials", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    asyncio.run(execute(parser.parse_args()))
