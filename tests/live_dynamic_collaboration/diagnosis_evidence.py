"""Diagnostic visibility even when a child has no completed answer.

These are successful request receipts, not relevance or semantic pass scores.
The Product evaluator's completed-handoff measure remains unchanged.
"""

import json


def visible_tool_results(events, session_id):
    stream = "session:" + session_id
    owned = [event for event in events if event["stream_id"] == stream]
    by_seq = {event["seq"]: event for event in owned}
    results = [
        event
        for event in owned
        if event["type"] == "tool/result" and event["data"]["status"] == "succeeded"
    ]
    receipts = {}
    for event in owned:
        data = event["data"]
        if event["type"] != "model/attempt-end" or data["status"] != "succeeded":
            continue
        snapshot = by_seq.get(data["request_snapshot_seq"])
        if (
            snapshot is None
            or snapshot["type"] != "request/snapshot"
            or snapshot["data"]["dispatch_fingerprint"] != data["dispatch_fingerprint"]
        ):
            raise ValueError("diagnostic-request-receipt-mismatch")
        for message in snapshot["data"]["dispatch_request"]["messages"]:
            if message["role"] != "tool":
                continue
            for result in results:
                value = result["data"]
                if (
                    result["seq"] < snapshot["seq"]
                    and value["tool_call_id"] == message.get("tool_call_id")
                    and value["tool_name"] == message.get("name")
                    and value["content"] == message["content"]
                ):
                    receipts[result["seq"]] = {
                        "result_seq": result["seq"],
                        "request_seq": snapshot["seq"],
                        "tool": value["tool_name"],
                        "data": value["data"],
                        "content": value["content"],
                    }
    return list(receipts.values())


def child_observation(events, handoff, parent_ids):
    reads = [
        r
        for r in visible_tool_results(events, handoff["session_id"])
        if r["tool"] in {"read_file", "search_text"}
    ]
    reports = []
    for parent in parent_ids:
        for receipt in visible_tool_results(events, parent):
            if receipt["tool"] != "collect_investigation":
                continue
            try:
                report = json.loads(receipt["content"])
            except ValueError:
                continue  # A preview does not qualify as the exact structured report.
            if (
                report.get("agent_id") == handoff["agent_id"]
                and report.get("message_id") == handoff["message_id"]
            ):
                reports.append(
                    {
                        "session_id": parent,
                        "request_seq": receipt["request_seq"],
                        "status": report["status"],
                    }
                )
    return {
        "session_id": handoff["session_id"],
        "report_status": handoff["report_status"],
        "source_results_seen_before_termination": [
            {key: value for key, value in r.items() if key != "content"} for r in reads
        ],
        "distinct_source_output_bytes": sum(len(r["content"].encode("utf-8")) for r in reads),
        "collect_states_seen_by_parent": reports,
        "interpretation": "Observed visibility, including failed children; not successful handoff.",
    }
