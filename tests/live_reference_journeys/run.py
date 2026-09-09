"""Opt-in real Memory/History/mixed journeys on the original Runtime.

No model responses or Tool calls are scripted here. Run as a module with tests
on PYTHONPATH. The CLI never runs during pytest collection.
"""

import argparse
import asyncio
import hashlib
import json
import os
import time
import traceback
from dataclasses import asdict
from pathlib import Path

from live_skill_navigation.run import write_json

from traceh.cli.env_file import load_env_file, validate_env_var_name
from traceh.llm.failures import ProviderFailure
from traceh.llm.openai_compatible import OpenAICompatibleProvider
from traceh.runtime.request_builder import verify_request_snapshots

from .assessment import assess, summarize
from .bootstrap import FixtureFailure, bootstrap, expected_sources, question
from .journey_fixtures import build_journey, context_policy


def attempt_metrics(streams, target_turn_id):
    groups = {"bootstrap": [], "target": []}
    for stream, events in streams.items():
        if not stream.startswith("session:"):
            continue
        for event in events:
            if event["type"] == "model/attempt-end":
                data = event["data"]
                group = "target" if data["turn_id"] == target_turn_id else "bootstrap"
                groups[group].append(data)
    return {
        group: {
            "attempts": len(attempts),
            "succeeded": sum(item["status"] == "succeeded" for item in attempts),
            "with_exact_usage": sum(
                item.get("usage", {}).get("quality") == "exact" for item in attempts
            ),
            "exact_tokens": sum(
                item["usage"]["input_tokens"] + item["usage"]["output_tokens"]
                for item in attempts
                if item.get("usage", {}).get("quality") == "exact"
            ),
            "missing_usage": sum("usage" not in item for item in attempts),
        }
        for group, attempts in groups.items()
    }


async def collect_evidence(state, root):
    """Export original streams even if higher-level validation rejects a Session."""
    streams = {
        name: [event.to_dict() for event in await state["store"].read(name)]
        for name in await state["store"].list_streams()
    }
    write_json(root / "streams.json", streams)
    replay, invariants, sessions = [], [], {}
    runtime = state["runtime"]
    for session in state["sessions"]:
        events = await runtime.sessions.read_session(session)
        sessions[session] = events
        replay.extend(
            {"session_id": session, **item}
            for item in await verify_request_snapshots(
                runtime.sessions,
                runtime.surface,
                session,
            )
        )
        invariants.extend(
            {"session_id": session, "error": str(item)} for item in runtime.invariants.check(events)
        )
    return sessions, replay, invariants


def record_error(result, error):
    # Never echo arbitrary transport exception text, bodies, headers or credentials.
    result["error_type"] = type(error).__name__
    result["error_site"] = [
        {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
        for frame in traceback.extract_tb(error.__traceback__)
    ]
    if isinstance(error, FixtureFailure):
        result["fixture_failure"] = str(error)
    if isinstance(error, ProviderFailure):
        result["provider_failure"] = {"code": error.code, "category": error.category.value}


async def attempt(args, corpus, skill_corpus, case, repeat, *, provider=None):
    root = args.output / f"{repeat}-{case['id']}"
    result = {
        "case": case["id"],
        "group": case["group"],
        "repeat": repeat,
        "passed": False,
        "task_passed": False,
        "bootstrap_completed": False,
    }
    started = time.monotonic()
    try:
        if provider is None:
            provider = OpenAICompatibleProvider(
                os.environ["TRACEH_BASE_URL"],
                api_key_env=args.key_env,
                timeout_seconds=corpus["bounds"]["provider_timeout_seconds"],
            )
        async with build_journey(
            root, corpus, skill_corpus, case, repeat, args.model, provider
        ) as state:
            try:
                config = state["config"]
                write_json(
                    root / "fixture-policy.json",
                    {
                        "context": config.context_input.to_dict(),
                        "memory": asdict(config.memory.memory_policy),
                        "project_limits": asdict(config.memory.project_limits),
                        "skill_limits": asdict(config.skill_policy.limits),
                        "tool_output_chars": config.max_tool_output_chars,
                    },
                )
                await bootstrap(state, corpus, case)
                expected = await expected_sources(state, corpus, skill_corpus, case)
                write_json(root / "expected.json", {"case": case, "expected": expected})
                result["bootstrap_completed"] = True
                turn = await state["runtime"].run_existing(state["session"], question(case))
                result.update(
                    session_id=state["session"],
                    target_turn_id=turn.turn_id,
                    final_text=turn.final_text,
                    reason=turn.reason,
                    target_usage=turn.usage.to_dict(),
                )
                sessions, replay, invariants = await collect_evidence(state, root)
                result.update(
                    assess(
                        case,
                        expected,
                        sessions[state["session"]],
                        turn.final_text,
                        turn.reason,
                        replay,
                        invariants,
                        corpus["bounds"]["allowed_tools"],
                    )
                )
            except Exception as error:
                record_error(result, error)
                try:
                    sessions, replay, invariants = await collect_evidence(state, root)
                    result.update(replay_errors=replay, invariants=invariants)
                except Exception as evidence_error:
                    result["evidence_error_type"] = type(evidence_error).__name__
            result["session_id"] = state["session"]
            result["sessions"] = state["sessions"]
            result["bootstrap_turns"] = state.get("bootstrap_turns", [])
            result["target_start_seq"] = state.get("target_start_seq")
    except Exception as error:
        # Includes bootstrap construction and cleanup failures. Preserve a failed denominator.
        result.update(passed=False, task_passed=False)
        record_error(result, error)
    result["seconds"] = round(time.monotonic() - started, 3)
    root.mkdir(parents=True, exist_ok=True)
    if (root / "streams.json").exists():
        streams = json.loads((root / "streams.json").read_text(encoding="utf-8"))
        if result.get("target_start_seq") is not None and "target_turn_id" not in result:
            starts = [
                e
                for e in streams.get("session:" + result["session_id"], [])
                if e["seq"] >= result["target_start_seq"] and e["type"] == "turn/start"
            ]
            if len(starts) == 1:
                result["target_turn_id"] = starts[0]["data"]["turn_id"]
        result["attempt_metrics"] = attempt_metrics(streams, result.get("target_turn_id"))
    write_json(root / "result.json", result)
    print(
        json.dumps(
            {
                k: result.get(k)
                for k in (
                    "case",
                    "repeat",
                    "group",
                    "task_passed",
                    "answer_ok",
                    "evidence_ok",
                    "requests",
                    "error_type",
                    "fixture_failure",
                )
            }
        ),
        flush=True,
    )
    return result


def hashes(root):
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*.py"))
    }


def prepare(args):
    corpus_bytes, skill_bytes = args.corpus.read_bytes(), args.skill_corpus.read_bytes()
    corpus, skill_corpus = (
        json.loads(data.decode("utf-8-sig")) for data in (corpus_bytes, skill_bytes)
    )
    if corpus["format"] != 2:
        raise ValueError("unsupported-evaluation-corpus-format")
    for case in corpus["cases"]:
        question(case)
    if args.model not in corpus["acceptance"]["models"]:
        raise ValueError("model-not-in-frozen-grid")
    if args.cases and set(args.cases) - {case["id"] for case in corpus["cases"]}:
        raise ValueError("unknown-evaluation-case")
    if args.repeats is not None and not 1 <= args.repeats <= corpus["acceptance"]["repeats"]:
        raise ValueError("invalid-diagnostic-repeat-count")
    cases = [case for case in corpus["cases"] if not args.cases or case["id"] in args.cases]
    repeats = args.repeats if args.repeats is not None else corpus["acceptance"]["repeats"]
    args.output.mkdir(parents=True, exist_ok=False)
    source_root = Path(__import__("traceh").__file__).resolve().parent
    helper_root = Path(__file__).resolve().parent
    manifest = {
        "format": 1,
        "model": args.model,
        "repeats": repeats,
        "cases": [c["id"] for c in cases],
        "diagnostic_subset": args.cases is not None or args.repeats is not None,
        "acceptance": corpus["acceptance"],
        "bounds": corpus["bounds"],
        "context_policies": {c["id"]: context_policy(c).to_dict() for c in cases},
        "corpus_sha256": hashlib.sha256(corpus_bytes).hexdigest(),
        "skill_corpus_sha256": hashlib.sha256(skill_bytes).hexdigest(),
        "source_sha256": hashes(source_root),
        "journey_helpers_sha256": hashes(helper_root),
        "navigation_helper_sha256": hashlib.sha256(
            (helper_root.parent / "live_skill_navigation" / "run.py").read_bytes(),
        ).hexdigest(),
        "scope": (
            "Real Provider, production Runtime, SQLite, host authority and isolated real Git; "
            "no Wheel/L2."
        ),
    }
    write_json(args.output / "manifest.json", manifest)
    # Only the original loader reads secrets here. Never export environment values.
    load_env_file(args.env_file)
    args.key_env = validate_env_var_name(
        os.environ.get("TRACEH_API_KEY_ENV", "OPENAI_API_KEY"),
        setting="TRACEH_API_KEY_ENV",
    )
    if any(not os.environ.get(key) for key in ("TRACEH_BASE_URL", args.key_env)):
        raise ValueError("explicit-real-provider-configuration-required")
    return corpus, skill_corpus, cases, repeats, manifest


async def main(args):
    corpus, skill_corpus, cases, repeats, manifest = prepare(args)
    results = []
    for repeat in range(repeats):
        for case in cases:
            results.append(await attempt(args, corpus, skill_corpus, case, repeat))
            complete = len(results) == len(cases) * repeats and not manifest["diagnostic_subset"]
            summary = summarize(corpus, results, complete=complete)
            write_json(
                args.output / "report.json",
                {"manifest": manifest, "summary": summary, "results": results},
            )
    return 0 if summary["acceptance_passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--skill-corpus", type=Path, required=True)
    parser.add_argument(
        "--cases", nargs="+", help="Explicit diagnostic subset; never full acceptance."
    )
    parser.add_argument(
        "--repeats", type=int, help="Explicit diagnostic repeat count; never full acceptance."
    )
    raise SystemExit(asyncio.run(main(parser.parse_args())))
