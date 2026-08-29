"""Host-owned L3 task probe executed inside one temporary comparison venv.

The probe is copied by the outer control plane and run with the Python from
the baseline or candidate environment.  It uses the public Runtime mainline,
but it does not decide whether a candidate is better: it only writes bounded
facts derived from the Event Log, verifier and reconstruction checks.  The
outer host process owns the fixed expectations and computes the comparison.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import shutil
import sys
import time
from pathlib import Path

from traceh.api.plugins import PluginIdentity
from traceh.concurrency import combine_failures
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_runtime import AgentRuntime, RuntimeConfig, build_default_runtime_async
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.plugin_identity import (
    PluginIdentityProtocolError,
    comparable_plugin_identities,
    external_plugin_identities,
    parse_plugin_identities,
)
from traceh.session.projections import StateProjector
from traceh.session.sqlite import SqliteEventStore

PROBE_SCHEMA_VERSION = 1
MAX_CASES = 20


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("comparison suite manifest must be an object")
    return value


def _substitute_python(value: object) -> object:
    """Resolve the one host-owned interpreter token in fixed suite data."""

    if isinstance(value, str):
        return value.replace("{python}", shlex.quote(sys.executable))
    if isinstance(value, list):
        return [_substitute_python(item) for item in value]
    if isinstance(value, dict):
        return {key: _substitute_python(item) for key, item in value.items()}
    return value


def _scripted_provider(script: Path, case_root: Path) -> ScriptedLlmProvider:
    raw = json.loads(script.read_text(encoding="utf-8"))
    substituted = _substitute_python(raw)
    resolved = case_root / "host-script.json"
    resolved.write_text(json.dumps(substituted, ensure_ascii=False), encoding="utf-8")
    return ScriptedLlmProvider.from_file(resolved)


def _case_path(suite: Path, case: dict[str, object], field: str) -> Path:
    raw = case.get(field)
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"comparison case {field} must be a non-empty string")
    root = suite.resolve()
    resolved = (root / raw).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValueError(f"comparison case {field} leaves the suite") from error
    return resolved


def _verifier_selection(
    case: dict[str, object],
    arm: str,
) -> tuple[str | None, str | None, float]:
    raw = case.get(f"{arm}_verifier")
    if not isinstance(raw, dict):
        raise ValueError(f"comparison case {arm}_verifier must be an object")
    kind = raw.get("kind")
    timeout = raw.get("timeout_seconds", 60.0)
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
        raise ValueError("comparison verifier timeout must be positive")
    if kind == "command":
        raw_argv = raw.get("argv")
        if (
            not isinstance(raw_argv, list)
            or not raw_argv
            or any(not isinstance(item, str) or not item for item in raw_argv)
        ):
            raise ValueError("comparison command verifier argv must be non-empty strings")
        argv = [sys.executable if item == "{python}" else item for item in raw_argv]
        return shlex.join(argv), None, float(timeout)
    if kind == "plugin":
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("comparison plugin verifier name must be non-empty")
        return None, name, float(timeout)
    raise ValueError("comparison verifier kind must be command or plugin")


async def _run_case(
    *,
    suite: Path,
    run_root: Path,
    case: dict[str, object],
    arm: str,
    plugin_id: str,
    plugin_version: str,
) -> dict[str, object]:
    case_id = case.get("id")
    task = case.get("task")
    if not isinstance(case_id, str) or not isinstance(task, str):
        raise ValueError("comparison case id and task must be strings")
    initial = _case_path(suite, case, "initial_dir")
    script = _case_path(suite, case, "script")
    case_root = run_root / case_id
    workspace = case_root / "workspace"
    shutil.copytree(initial, workspace)
    provider = _scripted_provider(script, case_root)
    command, verifier_name, verifier_timeout = _verifier_selection(case, arm)
    store = SqliteEventStore(case_root / ".traceh" / "events")
    try:
        runtime = await build_default_runtime_async(
            RuntimeConfig(
                data_dir=case_root / ".traceh",
                provider="scripted",
                model="l3-host-script",
                max_steps=int(case.get("max_steps", 20)),
                verification_command=command,
                verifier_name=verifier_name,
                verification_timeout_seconds=verifier_timeout,
                max_verification_retries=int(case.get("max_verification_retries", 0)),
            ),
            provider=provider,
            event_store=store,
            enabled_plugins=(plugin_id,) if arm == "candidate" else (),
        )
    except BaseException as primary:
        cleanup: BaseException | None = None
        try:
            await store.aclose()
        except BaseException as error:
            cleanup = error
        combined = combine_failures(primary, cleanup, "comparison build/store shutdown failed")
        assert combined is not None
        raise combined from None
    started = time.perf_counter()
    session_id: str | None = None
    expected_plugins = (PluginIdentity(plugin_id, plugin_version),) if arm == "candidate" else ()
    interrupted: BaseException | None = None
    try:
        session_id = await runtime.create_session(
            workspace,
            metadata={
                "evaluation": "l3-comparison",
                "suite": str(case.get("suite_id", "host-owned")),
                "case": case_id,
                "arm": arm,
            },
        )
        result = await runtime.run_existing(session_id, task)
        evidence = await _session_facts(
            runtime,
            session_id,
            expected_turn_id=result.turn_id,
            expected_reason=result.reason,
            expected_steps=result.steps,
            expected_plugins=expected_plugins,
        )
        return {
            "case_id": case_id,
            "completed": True,
            "duration_seconds": round(time.perf_counter() - started, 3),
            **evidence,
        }
    except Exception:
        evidence: dict[str, object] = {
            "model_attempts": 0,
            "tool_calls": 0,
            "tool_results": [],
            "verification_passed": None,
            "verification_exit_code": None,
            "invariant_violations": 0,
            "reconstruction_violations": 0,
            "plugin_identity_matches": False,
            "reason": "runtime_error",
            "steps": 0,
            "evidence_complete": False,
        }
        if session_id is not None:
            try:
                evidence = await _session_facts(
                    runtime,
                    session_id,
                    expected_plugins=expected_plugins,
                )
            except Exception:
                pass
        return {
            "case_id": case_id,
            "completed": False,
            "duration_seconds": round(time.perf_counter() - started, 3),
            **evidence,
        }
    except BaseException as error:
        interrupted = error
    finally:
        cleanup: BaseException | None = None
        try:
            await runtime.dispose()
        except BaseException as error:
            cleanup = error
        try:
            await store.aclose()
        except BaseException as error:
            cleanup = combine_failures(cleanup, error, "comparison case shutdown failed")
        combined = combine_failures(
            interrupted, cleanup, "comparison case and shutdown both failed"
        )
        if combined is not None:
            raise combined
    assert interrupted is not None
    raise interrupted


async def _session_facts(
    runtime: AgentRuntime,
    session_id: str,
    *,
    expected_plugins: tuple[PluginIdentity, ...],
    expected_turn_id: str | None = None,
    expected_reason: str | None = None,
    expected_steps: int | None = None,
) -> dict[str, object]:
    sessions = runtime.sessions
    events = await sessions.read_session(session_id)
    effects = await sessions.read_effects(session_id)
    invariant_violations = runtime.invariants.check(events, effects)
    reconstruction_violations = await verify_request_snapshots(
        sessions,
        runtime.surface,
        session_id,
    )
    tool_results = [event for event in events if event.type == "tool/result"]
    verification_results = [event for event in events if event.type == "verification/result"]
    verification = verification_results[-1] if verification_results else None
    projection = StateProjector().project(events)
    turn_starts = [event for event in events if event.type == "turn/start"]
    if expected_turn_id is None and len(turn_starts) == 1:
        raw_turn_id = turn_starts[0].data.get("turn_id")
        expected_turn_id = raw_turn_id if isinstance(raw_turn_id, str) else None
    matching_starts = [
        event for event in turn_starts if event.data.get("turn_id") == expected_turn_id
    ]
    matching_ends = [
        event
        for event in events
        if event.type == "turn/end" and event.data.get("turn_id") == expected_turn_id
    ]
    lifecycle_complete = (
        expected_turn_id is not None
        and len(matching_starts) == 1
        and len(matching_ends) == 1
        and matching_ends[0].seq > matching_starts[0].seq
        and projection.open_turn_id is None
        and projection.open_step_id is None
    )
    turn_start_seq = matching_starts[0].seq if len(matching_starts) == 1 else 0
    turn_end_seq = matching_ends[0].seq if len(matching_ends) == 1 else 0
    turn_end_data = matching_ends[0].data if len(matching_ends) == 1 else {}
    durable_reason = turn_end_data.get("reason")
    if not isinstance(durable_reason, str) or not durable_reason or len(durable_reason) > 128:
        durable_reason = "runtime_error"
        lifecycle_complete = False
    step_ends = [
        event
        for event in events
        if event.type == "step/end"
        and event.data.get("turn_id") == expected_turn_id
        and turn_start_seq < event.seq < turn_end_seq
    ]
    durable_steps = len(step_ends)
    recorded_steps = turn_end_data.get("steps")
    if recorded_steps is not None and (
        isinstance(recorded_steps, bool)
        or not isinstance(recorded_steps, int)
        or recorded_steps != durable_steps
    ):
        lifecycle_complete = False
    if expected_reason is not None and durable_reason != expected_reason:
        lifecycle_complete = False
    if expected_steps is not None and durable_steps != expected_steps:
        lifecycle_complete = False

    snapshots = [
        event
        for event in events
        if event.type == "composition/snapshot" and turn_start_seq < event.seq < turn_end_seq
    ]
    expected_identity = comparable_plugin_identities(expected_plugins)
    plugin_identity_matches = bool(snapshots)
    for snapshot in snapshots:
        try:
            observed = external_plugin_identities(
                parse_plugin_identities(
                    snapshot.data.get("plugins"),
                    allow_core=True,
                    error_code="composition-plugins-valid",
                    seq=snapshot.seq,
                )
            )
        except PluginIdentityProtocolError:
            plugin_identity_matches = False
            break
        if comparable_plugin_identities(observed) != expected_identity:
            plugin_identity_matches = False
            break
    return {
        "evidence_complete": lifecycle_complete,
        "plugin_identity_matches": plugin_identity_matches,
        "reason": durable_reason,
        "steps": durable_steps,
        "model_attempts": sum(event.type == "model/attempt-start" for event in events),
        "tool_calls": sum(event.type == "tool/call" for event in events),
        "tool_results": [
            {
                "tool_name": event.data.get("tool_name"),
                "status": event.data.get("status"),
                "policy": (
                    event.data.get("data", {}).get("policy")
                    if isinstance(event.data.get("data"), dict)
                    else None
                ),
            }
            for event in tool_results
        ],
        "verification_passed": (
            verification.data.get("passed") if verification is not None else None
        ),
        "verification_exit_code": (
            verification.data.get("exit_code") if verification is not None else None
        ),
        "invariant_violations": len(invariant_violations),
        "reconstruction_violations": len(reconstruction_violations),
    }


async def _run(args: argparse.Namespace) -> None:
    suite = args.suite.resolve()
    manifest = _read_json(suite / "suite.json")
    raw_cases = manifest.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases or len(raw_cases) > MAX_CASES:
        raise ValueError("comparison suite must contain 1 to 20 cases")
    run_root = args.run_root.resolve()
    run_root.mkdir(parents=True)
    cases = []
    for raw in raw_cases:
        if not isinstance(raw, dict):
            raise ValueError("comparison case must be an object")
        case = dict(raw)
        case["suite_id"] = manifest.get("suite_id")
        cases.append(
            await _run_case(
                suite=suite,
                run_root=run_root,
                case=case,
                arm=args.arm,
                plugin_id=args.plugin_id,
                plugin_version=args.plugin_version,
            )
        )
    payload = {
        "schema_version": PROBE_SCHEMA_VERSION,
        "arm": args.arm,
        "cases": cases,
    }
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(args.output)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--arm", choices=("baseline", "candidate"), required=True)
    parser.add_argument("--plugin-id", required=True)
    parser.add_argument("--plugin-version", required=True)
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()
