"""Opt-in DA-10 request probes; no Tool execution or Product success scoring."""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
import zipfile
from dataclasses import replace
from pathlib import Path

from live_unified_evaluation.baseline import connection

from live_dynamic_collaboration.diagnosis_audit import events_at
from traceh.api.json_types import fingerprint
from traceh.api.llm import ModelMessage, ModelRequest, ModelResponse, UsageQuality
from traceh.evaluation.inputs import digest_bytes
from traceh.tools.schema import validate_arguments

# Explicit historical study material identities, never production defaults.
CASES = ("separable-review", "coupled-review", "simple-read")
CONDITIONS = ("original", "drop-stale", "phase-last", "both")
DECISION_TOOL = "decide_task_decomposition"


def write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def prompts(archive: Path) -> tuple[str, str]:
    with zipfile.ZipFile(archive) as source:
        tree = ast.parse(source.read("src/traceh/product/decomposition.py").decode("utf-8"))
    values = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {
                    "_SCOUT_PROMPT",
                    "_DECISION_PROMPT",
                }:
                    values[target.id] = ast.literal_eval(node.value)
    if set(values) != {"_SCOUT_PROMPT", "_DECISION_PROMPT"} or any(
        not isinstance(v, str) or not v for v in values.values()
    ):
        raise ValueError("archived-prompts-required")
    return values["_SCOUT_PROMPT"], values["_DECISION_PROMPT"]


def transform(request: ModelRequest, condition: str, scout: str, decision: str) -> ModelRequest:
    if condition not in CONDITIONS:
        raise ValueError("unknown-condition")
    if len(request.tools) != 1 or request.tools[0].name != DECISION_TOOL:
        raise ValueError("exclusive-decision-request-required")
    if not scout or not decision or not (request.system_prompt or "").endswith(decision):
        raise ValueError("decision-prompt-mismatch")
    stale = [i for i, m in enumerate(request.messages) if m.role == "user" and m.content == scout]
    if len(stale) != 1:
        raise ValueError("exactly-one-stale-reminder-required")
    if request.messages[stale[0]].tool_calls or request.messages[stale[0]].tool_call_id:
        raise ValueError("stale-reminder-must-not-carry-tools")
    messages = request.messages
    if condition in {"drop-stale", "both"}:
        messages = tuple(m for i, m in enumerate(messages) if i != stale[0])
    if condition in {"phase-last", "both"}:
        messages = (*messages, ModelMessage("system", decision))
    return replace(request, messages=messages)


def assess(request: ModelRequest, response: ModelResponse) -> dict:
    calls = response.tool_calls
    admitted = len(calls) == 1 and calls[0].name == request.tools[0].name
    schema_valid = False
    decision_valid = False
    selected = None
    if admitted:
        args = calls[0].arguments
        try:
            validate_arguments(args, request.tools[0].input_schema)
            for key, schema in request.tools[0].input_schema["properties"].items():
                if "maxLength" in schema and len(args[key]) > schema["maxLength"]:
                    raise ValueError("bounded-text-required")
            schema_valid = True
        except (ValueError, TypeError):
            pass
        if schema_valid:
            selected = args.get("decision")
            main = args["main_goal"].strip()
            child = [args[k].strip() for k in ("child_goal", "child_deliverable", "briefing")]
            if selected == "local":
                decision_valid = bool(main) and not any(child) and not args["independent_readonly"]
            elif selected == "separable":
                decision_valid = bool(
                    main and all(child) and main != child[0] and args["independent_readonly"]
                )
    return {
        "tool_names": [c.name for c in calls],
        "only_disclosed_tool": admitted,
        "schema_valid": schema_valid,
        "decision_valid": decision_valid,
        "decision": selected,
        "tool_executed": False,
    }


def prepare(source_root: Path, archive: Path, output: Path) -> None:
    source_root, output = source_root.resolve(), output.resolve()
    if output.is_relative_to(source_root):
        raise ValueError("output-overlaps-original")
    scout, decision = prompts(archive)
    rows = []
    sources = []
    for case in CASES:
        database = source_root / "runs" / case / "attempts/001/ev/events.sqlite3"
        events = events_at(database)
        candidates = [
            e
            for e in events
            if e["type"] == "request/snapshot"
            and [t["name"] for t in e["data"]["dispatch_request"]["tools"]] == [DECISION_TOOL]
        ]
        if not candidates or len({e["stream_id"] for e in candidates}) != 1:
            raise ValueError("unique-decision-session-required")
        event = min(candidates, key=lambda e: e["seq"])
        raw = event["data"]["dispatch_request"]
        if fingerprint(raw) != event["data"]["dispatch_fingerprint"]:
            raise ValueError("original-request-fingerprint-mismatch")
        request = ModelRequest.from_dict(raw)
        if request.to_dict() != raw:
            raise ValueError("request-roundtrip-mismatch")
        source = {
            "case": case,
            "database": str(database),
            "database_sha256": digest_bytes(database.read_bytes()),
            "stream_id": event["stream_id"],
            "seq": event["seq"],
            "dispatch_fingerprint": fingerprint(raw),
        }
        sources.append(source)
        for condition in CONDITIONS:
            candidate = transform(request, condition, scout, decision)
            rows.append(
                {
                    "case": case,
                    "condition": condition,
                    "source": source,
                    "request": candidate.to_dict(),
                    "request_fingerprint": fingerprint(candidate.to_dict()),
                }
            )
    output.mkdir(parents=True, exist_ok=False)
    write(
        output / "plan.json",
        {
            "kind": "counterfactual-request-probe",
            "not_product_evaluation": True,
            "driver_sha256": digest_bytes(Path(__file__).read_bytes()),
            "archive_sha256": digest_bytes(archive.read_bytes()),
            "max_calls": 12,
            "retry": False,
            "timeout_seconds": 60,
            "sources": sources,
            "rows": rows,
        },
    )


async def probe(provider, request: ModelRequest, path: Path) -> dict:
    # The exact counterfactual is durable before dispatch. Never execute a returned Tool.
    write(path, {"status": "started", "request": request.to_dict()})
    try:
        response = await provider.complete(request)
    except BaseException as error:
        write(
            path,
            {
                "status": "cancelled" if isinstance(error, asyncio.CancelledError) else "failed",
                "request": request.to_dict(),
                "error_type": type(error).__name__,
            },
        )
        raise
    result = {
        "status": "completed",
        "request": request.to_dict(),
        "response": response.to_dict(),
        "assessment": assess(request, response),
    }
    write(path, result)
    if response.usage.quality != UsageQuality.EXACT:
        raise ValueError("unknown-usage-stop")
    return result


def verify_sources(plan: dict) -> None:
    for source in plan["sources"]:
        database = Path(source["database"])
        events_at(database)  # Also rejects an active WAL.
        if digest_bytes(database.read_bytes()) != source["database_sha256"]:
            raise ValueError("original-database-changed")


async def run(output: Path, profile: Path) -> None:
    plan = json.loads((output / "plan.json").read_text(encoding="utf-8"))
    driver_bytes = await asyncio.to_thread(Path(__file__).read_bytes)
    if plan["driver_sha256"] != digest_bytes(driver_bytes):
        raise ValueError("driver-changed-after-freeze")
    expected = [(case, condition) for case in CASES for condition in CONDITIONS]
    if [(r["case"], r["condition"]) for r in plan["rows"]] != expected or plan["max_calls"] != 12:
        raise ValueError("frozen-matrix-mismatch")
    for row in plan["rows"]:
        if fingerprint(row["request"]) != row["request_fingerprint"]:
            raise ValueError("probe-request-changed")
    verify_sources(plan)
    results_dir = output / "results"
    results_dir.mkdir(exist_ok=False)  # A partial or completed run is never retried.
    raise RuntimeError(
        "historical-collaboration-contract: use frozen source; current multi requires WC-1F"
    )
    _, provider, model = connection(profile)
    if any(
        r["request"]["provider"] != provider.name or r["request"]["model"] != model
        for r in plan["rows"]
    ):
        raise ValueError("authorized-model-does-not-match-original")
    rows = []
    try:
        for index, row in enumerate(plan["rows"]):
            result = await probe(
                provider, ModelRequest.from_dict(row["request"]), results_dir / f"{index:02}.json"
            )
            rows.append(
                {
                    "case": row["case"],
                    "condition": row["condition"],
                    **result["assessment"],
                    "usage": result["response"]["usage"],
                }
            )
            write(output / "summary.json", {"complete": len(rows) == 12, "rows": rows})
            print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    finally:
        verify_sources(plan)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_subparsers(dest="action", required=True)
    pre = action.add_parser("prepare")
    for name in ("source-root", "archive", "output"):
        pre.add_argument("--" + name, type=Path, required=True)
    live = action.add_parser("run")
    live.add_argument("--output", type=Path, required=True)
    live.add_argument("--profile", type=Path, required=True)
    opts = parser.parse_args()
    if opts.action == "prepare":
        prepare(opts.source_root, opts.archive, opts.output)
    else:
        asyncio.run(run(opts.output, opts.profile))
