"""Opt-in real Provider navigation evaluation through the production Runtime.

Run explicitly, never collected by pytest. No model response or Tool call is
scripted. The temporary source plugin uses stdlib distribution metadata and the
normal PluginManager; this is not Wheel/L2 validation. Corpus keys and answers
are evaluator-only. Opaque public IDs vary with the repeat, not the case.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import sys
import time
import unicodedata
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from traceh.api.plugins import PluginIdentity
from traceh.api.retrieval import ReferenceRetrievalPolicy
from traceh.api.skills import SkillLimits, SkillPolicy, SkillResourceRoot
from traceh.cli.env_file import load_env_file, validate_env_var_name
from traceh.llm.failures import ProviderFailure
from traceh.llm.openai_compatible import OpenAICompatibleProvider
from traceh.llm.retry import ModelRetryPolicy
from traceh.plugins.discovery import PluginDiscovery
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime_async
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.context_input import ContextInputPolicy
from traceh.session.sqlite import SqliteEventStore
from traceh.tools.policy import DecisionKind, ToolDecision

PLUGIN_ID = "navigation.fixture"
PLUGIN_VERSION = "1.0.0"
PLUGIN_SOURCE = """from pathlib import Path
import json
from traceh.api.plugins import PluginManifest
from traceh.api.skills import SkillContribution, SkillDescriptor, SkillSectionContent

class Plugin:
    manifest = PluginManifest(
        plugin_id="navigation.fixture", version="1.0.0", requires_traceh=">=0.8,<1.0",
        allowed_scopes=("application",), trust_mode="trusted", provides=("navigation",),
    )
    async def setup(self, context, config):
        values = json.loads(Path(__file__).with_suffix(".json").read_text(encoding="utf-8"))
        for value in values:
            context.register_skill(SkillContribution(
                SkillDescriptor.from_dict(value["descriptor"]),
                tuple(SkillSectionContent(**s) for s in value["sections"]),
            ))
"""


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def content_metadata(body):
    data = body.encode("utf-8")
    return {"content_digest": hashlib.sha256(data).hexdigest(), "content_bytes": len(data)}


def fixture(corpus, root, repeat):
    """Materialize explicit corpus inputs outside the empty model workspace."""

    def identity(key):
        return "n" + uuid5(NAMESPACE_URL, f"navigation-v1/{repeat}/{key}").hex[:20]

    values, references, selected = [], {}, []
    for skill in corpus["skills"]:
        key = skill["key"]
        skill_id = identity(key)
        sections, bodies, resources = [], [], []
        for section in skill["sections"]:
            reference = f"{key}/{section['key']}"
            section_id = identity(reference)
            sections.append(
                {
                    "section_id": section_id,
                    "tier": "section",
                    "title": section["title"],
                    "summary": section["summary"],
                    **content_metadata(section["body"]),
                }
            )
            bodies.append({"section_id": section_id, "body": section["body"]})
            references[reference] = [skill_id, "section", section_id, None, None]
        for resource in skill["resources"]:
            resource_id = identity(f"{key}/{resource['key']}")
            chunks, body, offset = [], "", 0
            for chunk in resource["chunks"]:
                reference = f"{key}/{resource['key']}/{chunk['key']}"
                chunk_id = identity(reference)
                meta = content_metadata(chunk["body"])
                chunks.append(
                    {
                        "chunk_id": chunk_id,
                        "byte_start": offset,
                        "byte_end": offset + meta["content_bytes"],
                        **meta,
                        "title": chunk["title"],
                        "summary": chunk["summary"],
                    }
                )
                offset += meta["content_bytes"]
                body += chunk["body"]
                references[reference] = [skill_id, "chunk", None, resource_id, chunk_id]
            relative_path = resource_id + ".txt"
            (root / relative_path).write_bytes(body.encode("utf-8"))
            resources.append(
                {
                    "resource_id": resource_id,
                    "relative_path": relative_path,
                    "title": resource["title"],
                    "summary": resource["summary"],
                    **content_metadata(body),
                    "chunks": chunks,
                }
            )
        values.append(
            {
                "descriptor": {
                    "skill_id": skill_id,
                    "version": PLUGIN_VERSION,
                    "plugin": {"plugin_id": PLUGIN_ID, "version": PLUGIN_VERSION},
                    "title": skill["title"],
                    "summary": skill["summary"],
                    "tags": [],
                    "requires_traceh": ">=0.8,<1.0",
                    "sections": sorted(sections, key=lambda x: x["section_id"]),
                    "resources": sorted(resources, key=lambda x: x["resource_id"]),
                },
                "sections": sorted(bodies, key=lambda x: x["section_id"]),
            }
        )
        if key in corpus["selected"]:
            selected.append({"skill_id": skill_id, "version": PLUGIN_VERSION})
    return values, references, tuple(sorted(selected, key=lambda x: x["skill_id"]))


def source_plugin(root, values):
    module = "navigation_fixture_" + uuid5(NAMESPACE_URL, str(root)).hex
    (root / (module + ".py")).write_text(PLUGIN_SOURCE, encoding="utf-8")
    write_json(root / (module + ".json"), values)
    dist = root / "navigation_fixture-1.0.0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: navigation-fixture\nVersion: 1.0.0\n"
        "Requires-Dist: traceharness-py>=0.8,<1.0\n",
        encoding="utf-8",
    )
    (dist / "entry_points.txt").write_text(
        f"[traceh.plugins]\n{PLUGIN_ID} = {module}:Plugin\n",
        encoding="utf-8",
    )
    points = tuple(importlib.metadata.PathDistribution(dist).entry_points)
    sys.path.insert(0, str(root))
    return PluginDiscovery(entry_points_provider=lambda **_: points)


class ReferenceOnlyPolicy:
    name = "navigation-evaluation-reference-only"

    async def check(self, call, tool, context):
        del tool, context
        allowed = call.name == "request_skill_reference"
        return ToolDecision(
            DecisionKind.ALLOW if allowed else DecisionKind.DENY,
            "This evaluation grants only reference disclosure.",
            self.name,
        )


def policies(tier):
    retrieval = ReferenceRetrievalPolicy(
        unicode_version=unicodedata.unidata_version,
        default_tier=tier,
        match_fields=("id", "tag", "path", "symbol", "error"),
        k1=1.2,
        b=0.75,
        rrf_constant=60,
        exact_weight=2,
        fts_weight=1,
        context_bytes=64_000,
        max_catalog_bytes=64_000,
        max_terms=300,
        max_corpus_items=40,
        max_corpus_bytes=100_000,
        max_candidates=20,
        max_requests=12,
    )
    return ContextInputPolicy(
        history_tier=None,
        total_bytes=80_000,
        history_bytes=0,
        item_bytes=32_000,
        max_blocks=20,
        max_exclusions=60,
        max_query_bytes=8000,
        skills=retrieval,
    )


def reference_identity(data):
    return [
        data.get(k) for k in ("skill_id", "requested_tier", "section_id", "resource_id", "chunk_id")
    ]


def assess(case, references, events, final_text, replay_errors, invariants, selected_ids):
    expected = [references[key] for key in case["references"]]
    calls = [e.data for e in events if e.type == "tool/call"]
    body_calls = [
        reference_identity(c["arguments"])
        for c in calls
        if c["tool_name"] == "request_skill_reference"
        and c["arguments"].get("requested_tier") in {"section", "chunk"}
    ]
    contexts = [e for e in events if e.type == "context/input"]
    scope_violations = [
        {"seq": e.seq, "skill_id": b["id"]}
        for e in contexts
        for b in e.data["blocks"]
        if b["kind"] == "skill" and b["id"] not in selected_ids
    ]
    disclosed = []
    for event in contexts:
        for block in event.data["blocks"]:
            if block["kind"] == "skill" and block["tier"] in {"section", "chunk"}:
                disclosed.append(
                    reference_identity({**block["provenance"], "requested_tier": block["tier"]})
                )
    snapshots = [e for e in events if e.type == "request/snapshot"]
    answer_ok = all(s in final_text for s in case["answer_fragments"]) and not any(
        s in final_text for s in case["forbidden_fragments"]
    )
    unexpected = [r for r in body_calls if r not in expected]
    errors = [
        e.data for e in events if e.type == "tool/result" and e.data.get("status") != "succeeded"
    ]
    evidence_ok = bool(snapshots) and all(r in disclosed for r in expected)
    foreign_calls = [c["tool_name"] for c in calls if c["tool_name"] != "request_skill_reference"]
    # A correct guessed answer, or reading every chapter, does not pass navigation.
    passed = (
        answer_ok
        and evidence_ok
        and not unexpected
        and not errors
        and not foreign_calls
        and not replay_errors
        and not invariants
        and not scope_violations
    )
    return {
        "passed": passed,
        "task_passed": (
            answer_ok
            and evidence_ok
            and not errors
            and not foreign_calls
            and not replay_errors
            and not invariants
            and not scope_violations
        ),
        "answer_ok": answer_ok,
        "evidence_ok": evidence_ok,
        "expected_references": expected,
        "body_calls": body_calls,
        "unexpected_body_calls": unexpected,
        "disclosed": disclosed,
        "tool_errors": errors,
        "other_tool_calls": foreign_calls,
        "requests": len(snapshots),
        "replay_errors": list(map(str, replay_errors)),
        "invariants": list(map(str, invariants)),
        "scope_violations": scope_violations,
    }


async def attempt(args, corpus, case, repeat, tier):
    root = args.output / f"{repeat}-{tier}-{case['id']}"
    root.mkdir()
    inputs, workspace = root / "inputs", root / "workspace"
    inputs.mkdir()
    workspace.mkdir()
    values, references, selected = fixture(corpus, inputs, repeat)
    discovery = source_plugin(inputs, values)
    config = RuntimeConfig(
        data_dir=root / "data",
        provider="openai-compatible",
        model=args.model,
        max_steps=args.max_steps,
        max_output_tokens=args.max_output_tokens,
        max_tool_output_chars=80_000,
        temperature=0,
        model_retry_policy=ModelRetryPolicy(3, 600, 1, 8, 8, 0),
        context_input=policies(tier),
        skill_policy=SkillPolicy(
            SkillLimits(
                max_skills=8,
                max_catalog_bytes=64_000,
                max_summary_bytes=1000,
                max_content_bytes=64_000,
                max_resource_bytes=32_000,
            ),
            (SkillResourceRoot(PluginIdentity(PLUGIN_ID, PLUGIN_VERSION), inputs),),
        ),
    )
    store = SqliteEventStore(root / "events")
    runtime = None
    session = None
    started = time.monotonic()
    result = {"case": case["id"], "repeat": repeat, "tier": tier, "passed": False}
    try:
        provider = OpenAICompatibleProvider(
            os.environ["TRACEH_BASE_URL"],
            api_key_env=args.key_env,
            timeout_seconds=args.timeout_seconds,
        )
        runtime = await build_default_runtime_async(
            config,
            provider=provider,
            event_store=store,
            policies=(ReferenceOnlyPolicy(),),
            enabled_plugins=(PLUGIN_ID,),
            plugin_discovery=discovery,
        )
        session = await runtime.create_session(workspace)
        await runtime.skill_context.select(
            session,
            operation_id="select-fixture",
            expected_head=0,
            actor_id="evaluation-host",
            skills=selected,
        )
        await runtime.skill_context.rebuild_index(session)
        # No Skill IDs, chapter IDs, expected answers or forced Tool calls in this input.
        turn = await runtime.run_existing(session, case["query"])
        events = await runtime.sessions.read_session(session)
        replay = await verify_request_snapshots(runtime.sessions, runtime.surface, session)
        result.update(
            assess(
                case,
                references,
                events,
                turn.final_text,
                replay,
                runtime.invariants.check(events),
                {item["skill_id"] for item in selected},
            )
        )
        result.update(
            {
                "session_id": session,
                "final_text": turn.final_text,
                "reason": turn.reason,
                "usage": turn.usage.to_dict(),
            }
        )
        write_json(root / "events.json", [e.to_dict() for e in events])
        write_json(root / "expected.json", {"case": case, "references": references})
    except Exception as error:
        # Provider errors are typed and sanitized; do not echo arbitrary exception text.
        result["error_type"] = type(error).__name__
        if isinstance(error, ProviderFailure):
            result["provider_failure"] = {"code": error.code, "category": error.category.value}
        if runtime is not None and session is not None:
            result["session_id"] = session
            try:
                events = await runtime.sessions.read_session(session)
                write_json(root / "events.json", [e.to_dict() for e in events])
                write_json(root / "expected.json", {"case": case, "references": references})
                result["requests"] = sum(e.type == "request/snapshot" for e in events)
            except Exception as evidence_error:
                result["evidence_error_type"] = type(evidence_error).__name__
    finally:
        if runtime is not None:
            await runtime.dispose()
        await store.aclose()
        sys.path.remove(str(inputs))
    result["seconds"] = round(time.monotonic() - started, 3)
    write_json(root / "result.json", result)
    print(
        json.dumps(
            {
                k: result.get(k)
                for k in ("case", "repeat", "tier", "passed", "answer_ok", "requests", "error_type")
            }
        ),
        flush=True,
    )
    return result


def prepare(args):
    if args.repeats < 1 or args.max_steps < 1 or args.max_output_tokens < 1:
        raise ValueError("positive evaluation bounds required")
    args.output.mkdir(parents=True, exist_ok=False)
    load_env_file(args.env_file)
    args.model = args.model or os.environ.get("TRACEH_MODEL")
    args.key_env = validate_env_var_name(
        os.environ.get("TRACEH_API_KEY_ENV", "OPENAI_API_KEY"),
        setting="TRACEH_API_KEY_ENV",
    )
    if not args.model or any(not os.environ.get(k) for k in ("TRACEH_BASE_URL", args.key_env)):
        raise ValueError("explicit real Provider configuration required")
    corpus_bytes = args.corpus.read_bytes()
    corpus = json.loads(corpus_bytes.decode("utf-8-sig"))
    if args.cases:
        if set(args.cases) - {case["id"] for case in corpus["cases"]}:
            raise ValueError("unknown evaluation case")
        corpus["cases"] = [case for case in corpus["cases"] if case["id"] in args.cases]
    source_root = Path(__import__("traceh").__file__).resolve().parent
    manifest = {
        "corpus_sha256": hashlib.sha256(corpus_bytes).hexdigest(),
        "source_sha256": {
            str(p.relative_to(source_root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(source_root.rglob("*.py"))
        },
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "model": args.model,
        "repeats": args.repeats,
        "tiers": args.tiers,
        "cases": [case["id"] for case in corpus["cases"]],
        "max_steps": args.max_steps,
        "max_output_tokens": args.max_output_tokens,
        "temperature": 0,
        "provider_timeout_seconds": args.timeout_seconds,
        "scope": "real Provider + production Runtime; source plugin, no Wheel/L2",
    }
    write_json(args.output / "manifest.json", manifest)
    return corpus, manifest


async def main(args):
    corpus, manifest = prepare(args)
    results = []
    for repeat in range(args.repeats):
        for tier in args.tiers:
            for case in corpus["cases"]:
                results.append(await attempt(args, corpus, case, repeat, tier))
                write_json(args.output / "report.json", {"manifest": manifest, "results": results})
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--model", help="Explicit evaluation model; otherwise use TRACEH_MODEL.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--cases", nargs="+")
    parser.add_argument("--repeats", type=int, required=True)
    parser.add_argument("--tiers", nargs="+", choices=("directory", "summary"), required=True)
    parser.add_argument("--max-steps", type=int, required=True)
    parser.add_argument("--max-output-tokens", type=int, required=True)
    parser.add_argument("--timeout-seconds", type=float, required=True)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
