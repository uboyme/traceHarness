"""Read-only AR-D review packets; never call a Provider or auto-approve negative answers."""

import argparse
import hashlib
import json
from pathlib import Path


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dispatched_evidence(events, report, fixture):
    """Require evidence in an actual successful target Attempt, not merely an appended block."""
    by_seq = {event["seq"]: event for event in events}
    results = []
    for attempt in events:
        data = attempt["data"]
        if (
            attempt["type"] != "model/attempt-end"
            or attempt["seq"] <= report.get("target_start_seq", len(events))
            or data["status"] != "succeeded"
        ):
            continue
        snapshot = by_seq[data["request_snapshot_seq"]]
        messages = snapshot["data"]["dispatch_request"]["messages"]
        for evidence in report.get("evidence", []):
            if "context_seq" in evidence:
                if snapshot["data"]["context_input_seq"] != evidence["context_seq"]:
                    continue
                blocks = []
                for message in messages:
                    content = message.get("content") or ""
                    if not content.startswith("Current references for the active user request."):
                        continue
                    blocks.extend(json.loads(content.split("\n")[1]))
                visible = any(
                    block["kind"] == fixture["family"]
                    and fixture["value"] in block.get("body", "")
                    and (
                        fixture["family"] != "memory"
                        or block.get("id") == fixture["memory_id"]
                        or (
                            block["tier"] == "search"
                            and any(
                                hit["reference"]["id"] == fixture["memory_id"]
                                and fixture["value"] in hit["text"]
                                for hit in json.loads(block["body"])["hits"]
                            )
                        )
                    )
                    for block in blocks
                )
            else:
                result = by_seq[evidence["tool_result_seq"]]["data"]
                visible = any(
                    message["role"] == "tool"
                    and message.get("tool_call_id") == result["tool_call_id"]
                    and fixture["value"] in (message.get("content") or "")
                    for message in messages
                )
            if visible:
                results.append(
                    {
                        "attempt_end_seq": attempt["seq"],
                        "request_snapshot_seq": snapshot["seq"],
                        "dispatch_fingerprint": data["dispatch_fingerprint"],
                        "source_evidence": evidence,
                    }
                )
    return results


def packet(folder):
    report = load(folder / "report.json")
    fixture = load(folder / "fixture.json")
    if not report["closed"]:
        return None
    events_path = folder / "source-events.json"
    events = load(events_path) if events_path.exists() else []
    target = [e for e in events if e["seq"] > report.get("target_start_seq", len(events))]
    searches = []
    for event in target:
        if event["type"] != "context/input":
            continue
        for block in event["data"]["blocks"]:
            if block["tier"] == "search":
                page = json.loads(block["body"])
                searches.append(
                    {
                        "context_seq": event["seq"],
                        "kind": block["kind"],
                        "query": page["query"],
                        "status": page["status"],
                        "scanned": page["scanned"],
                        "total": page["total"],
                        "has_next": page["next_cursor"] is not None,
                        "texts": [h["text"] for h in page["hits"]],
                    }
                )
    success_tools = [
        {"seq": e["seq"], "name": e["data"]["tool_name"]}
        for e in target
        if e["type"] == "tool/result" and e["data"]["status"] == "succeeded"
    ]
    return {
        "identity": report["identity"],
        "family": fixture["family"],
        "question": fixture["question"],
        "expected": fixture["expected"],
        "expected_value": fixture["value"],
        "source": fixture["source"],
        "predecessor_value": fixture.get("predecessor_value"),
        "answer": report.get("answer", ""),
        "error": report.get("error"),
        "audit_error": report.get("audit_error"),
        "reason": report.get("reason"),
        "steps": report.get("steps"),
        "provisional_joint_pass": report["provisional_joint_pass"],
        "dispatched_evidence": dispatched_evidence(events, report, fixture),
        "searches": searches,
        "calls": report.get("tool_calls", []),
        "successful_tools": success_tools,
        "output_executions": report.get("output_executions"),
        "replay_errors": report.get("replay_errors"),
        "invariant_errors": report.get("invariant_errors"),
        "all_usage": report.get("all_usage"),
        "target_usage": report.get("target_usage"),
        "target_seconds": report.get("target_seconds"),
        "report_sha256": sha(folder / "report.json"),
        "source_events_sha256": sha(events_path) if events_path.exists() else None,
        "manual_review": "pending: assess answer attribution, unsupported facts and negatives",
    }


def build(root, output):
    packets = []
    for arm in ("baseline", "candidate"):
        for path in sorted((root / arm).glob("*/report.json")):
            item = packet(path.parent)
            if item is not None:
                packets.append({"arm": arm, **item})
    output.write_text(json.dumps(packets, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"packets": len(packets), "output": str(output)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build(args.root, args.output)
