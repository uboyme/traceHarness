"""One explicit concurrent-handoff probe over the WC-4 fixture and original owners.

Same frozen files, reference and fixed checks as `writable_materials`; only the
requirement states that the two specified modules are independent, which is the
condition the generic handoff guidance asks the main agent to judge. Nothing here
names a handoff mode: choosing it stays the model's decision.
"""

import argparse
import asyncio
import json
import time
from pathlib import Path

from live_dynamic_collaboration import writable_acceptance as driver
from live_dynamic_collaboration.materials import write
from traceh.evaluation.inputs import digest_bytes

CASE = "concurrent-telemetry"
REQUIREMENT = (
    "Implement the telemetry report specified in INDEX.md. This acceptance explicitly requires "
    "one writable assistant to implement telemetry_rules.py while the main agent owns "
    "telemetry_report.py and final verification. INDEX.md specifies both modules completely: "
    "telemetry_report.summarize can be written from that specification alone and does not need "
    "to read the assistant's code, so the main agent's own module does not depend on the "
    "assistant's result. Delegate the rules module with its exact path. Read the returned Patch "
    "completely and explicitly integrate it before final verification. Only modify those two "
    "Python files; preserve all specifications, add no dependencies/files. Explain actual checks "
    "and their results; do not claim checks you did not run."
)
CALLS: list[dict] = []


def _select():
    """Keep the proven fixture bytes; replace only the requirement text."""
    driver.REQUIREMENT = REQUIREMENT


class TimedProvider(driver.BoundedProvider):
    """Record each real call's role and wall-clock interval, to observe overlap."""

    async def complete(self, request):
        role = "child" if "traceh.product.patch-author" in request.system_prompt else "main"
        entry = {"index": self.calls + 1, "role": role, "start": time.monotonic(), "end": None}
        CALLS.append(entry)
        try:
            return await super().complete(request)
        finally:
            entry["end"] = time.monotonic()


def _overlap(calls):
    """Summed seconds during which a main and a child call were both in flight."""
    points = sorted({value for call in calls for value in (call["start"], call["end"]) if value})
    total = 0.0
    for left, right in zip(points, points[1:], strict=False):
        roles = {
            call["role"]
            for call in calls
            if call["end"] and call["start"] <= left and call["end"] >= right
        }
        if {"main", "child"} <= roles:
            total += right - left
    return total


def prepare(repository, sandbox, output):
    _select()
    driver.prepare(repository, sandbox, output)
    dataset_path = output / "material/dataset.json"
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    dataset["cases"][0].update(case_id=CASE, group_id=CASE)
    write(dataset_path, dataset)
    manifest_path = output / "material/benchmark.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    settings = manifest["task_settings"]
    # Explicit fixture budget, as in the recorded command-recovery round; this is
    # not a production default. A concurrent main spends steps on its own work
    # before collecting, so the token ceiling must not decide the outcome.
    settings["task_budget"]["max_tokens"] = 480_000
    settings["roles"]["coder"]["budget"]["max_tokens"] = 360_000
    manifest["dataset"]["sha256"] = digest_bytes(dataset_path.read_bytes())
    write(manifest_path, manifest)
    (output / "concurrent_acceptance.py").write_bytes(Path(__file__).read_bytes())
    contract_path = output / "contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract.update(
        kind="concurrent-handoff-real-probe",
        case_id=CASE,
        requirement_digest=digest_bytes(REQUIREMENT.encode("utf-8")),
        handoff_mode_named_in_requirement=False,
        baseline_trials=0,
        semantic_judge_calls=0,
        interpretation=(
            "One bounded observation of the concurrent handoff. Mechanism, model choice, "
            "functional result and benefit are separate conclusions; no benefit is claimed."
        ),
    )
    contract["files"] = {
        p.relative_to(output).as_posix(): digest_bytes(p.read_bytes())
        for p in output.rglob("*")
        if p.is_file() and p != contract_path
    }
    write(contract_path, contract)


def _require_driver(output):
    if (output / "concurrent_acceptance.py").read_bytes() != Path(__file__).read_bytes():
        raise ValueError("concurrent-driver-drift")


def preflight(output):
    _select()
    _require_driver(output)
    driver.preflight(output)


async def run(profile, output):
    _select()
    _require_driver(output)
    original = driver.BoundedProvider
    driver.BoundedProvider = TimedProvider
    try:
        await driver.run(profile, output)
    finally:
        driver.BoundedProvider = original
        write(
            output / "calls.json",
            {
                "calls": [
                    {
                        "index": call["index"],
                        "role": call["role"],
                        "seconds": None
                        if call["end"] is None
                        else round(call["end"] - call["start"], 3),
                        "unfinished": call["end"] is None,
                    }
                    for call in CALLS
                ],
                "main_calls": sum(call["role"] == "main" for call in CALLS),
                "child_calls": sum(call["role"] == "child" for call in CALLS),
                "observed_main_child_overlap_seconds": round(_overlap(CALLS), 3),
                "measurement": (
                    "In-flight provider call intervals on one host clock; "
                    "overlap is concurrency of model calls, not a speedup claim."
                ),
            },
        )


def audit(output):
    """Read the closed original log; never re-execute or re-score."""
    from live_dynamic_collaboration.diagnosis_audit import events_at

    report = json.loads((output / "run/report.json").read_text(encoding="utf-8"))
    attempt = report["task_report"]["attempts"][0]
    events = events_at(output / "run" / attempt["directory"] / "ev/events.sqlite3")
    calls = [e for e in events if e["type"] == "tool/call"]
    results = {
        (e["data"]["tool_call_id"], e["data"]["tool_name"]): e["data"]
        for e in events
        if e["type"] == "tool/result"
    }
    plans = [c for c in calls if c["data"]["tool_name"] == "submit_collaboration_plan"]
    collects = [c for c in calls if c["data"]["tool_name"] == "collect_child_patch"]
    turns = [
        {
            "stream": e["stream_id"],
            "type": e["type"],
            "turn_id": e["data"]["turn_id"],
            "at": e["occurred_at"],
        }
        for e in events
        if e["type"] in {"turn/start", "turn/end"}
    ]
    summary = {
        "case_id": attempt.get("case_id"),
        "execution": attempt.get("execution"),
        "assessment": attempt.get("assessment"),
        "handoff_arguments": [p["data"]["arguments"].get("handoff") for p in plans],
        "plan_results": [
            results.get((p["data"]["tool_call_id"], "submit_collaboration_plan"), {}).get("status")
            for p in plans
        ],
        "collect_statuses": [
            results.get((c["data"]["tool_call_id"], "collect_child_patch"), {}).get("status")
            for c in collects
        ],
        "tool_call_counts": {
            name: sum(c["data"]["tool_name"] == name for c in calls)
            for name in sorted({c["data"]["tool_name"] for c in calls})
        },
        "verification": [
            {"passed": e["data"]["passed"], "exit_code": e["data"].get("exit_code")}
            for e in events
            if e["type"] == "verification/result"
        ],
        "turn_boundaries": turns,
        "statistics": report.get("statistics"),
    }
    write(output / "audit.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2)[:4000])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "preflight", "run", "audit"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repository", type=Path)
    parser.add_argument("--sandbox", type=Path)
    parser.add_argument("--profile", type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    if args.action == "prepare":
        prepare(args.repository.resolve(), args.sandbox.resolve(), output)
    elif args.action == "preflight":
        preflight(output)
    elif args.action == "run":
        asyncio.run(run(args.profile.resolve(), output))
    else:
        audit(output)
