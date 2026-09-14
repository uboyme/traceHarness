"""Offline diagnostic views of closed original logs; no replacement evaluator."""

import hashlib
import json
import sqlite3
from collections import Counter

TOOLS = {
    "delegate_investigation",
    "followup_investigation",
    "collect_investigation",
    "stop_investigation",
}


def events_at(database):
    database = database.resolve()
    wal = database.with_name(database.name + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise ValueError("closed-checkpointed-evidence-required")
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    connection = sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        events = [
            json.loads(row[0])
            for row in connection.execute("SELECT envelope_json FROM events ORDER BY stream_id,seq")
        ]
    finally:
        connection.close()
    if hashlib.sha256(database.read_bytes()).hexdigest() != before:
        raise ValueError("diagnostic-original-log-changed")
    return events


def main_requests(events, session_ids):
    return [
        e
        for e in events
        if e["type"] == "request/snapshot"
        and e["stream_id"] in {"session:" + sid for sid in session_ids}
    ]


def audit_prior(batch_roots):
    rows = []
    for root in batch_roots:
        for report_file in sorted(root.glob("experiment-*/arms/*/run/report.json")):
            report = json.loads(report_file.read_text(encoding="utf-8"))
            for attempt in report["task_report"]["attempts"]:
                if attempt["requested_mode"] != "adaptive":
                    continue
                directory = report_file.parent / attempt["directory"]
                events = events_at(directory / "ev/events.sqlite3")
                sessions = attempt["evidence"]["execution"]["sessions"]
                requests = main_requests(events, [s["session_id"] for s in sessions])
                counts = Counter(e["data"]["tool_name"] for e in events if e["type"] == "tool/call")
                budgets = [
                    e["data"]["child_limits"]
                    for e in events
                    if e["type"] == "budget/child-reserved"
                ]
                source = directory / "source"
                code = [p for p in source.rglob("*.py") if ".git" not in p.parts]
                descriptions = {
                    t["name"]: t["description"]
                    for t in requests[0]["data"]["dispatch_request"]["tools"]
                    if t["name"] in TOOLS
                }
                rows.append(
                    dict(
                        batch=root.name,
                        case=attempt["benchmark_task_id"],
                        repetition=attempt["repetition"],
                        session_count=len(sessions),
                        requests=len(requests),
                        all_requests_expose_tools=bool(requests)
                        and all(
                            TOOLS <= {t["name"] for t in r["data"]["dispatch_request"]["tools"]}
                            for r in requests
                        ),
                        descriptions=descriptions,
                        budget_limits=budgets,
                        delegate_calls=counts["delegate_investigation"],
                        code_files=len(code),
                        code_lines=sum(len(p.read_bytes().splitlines()) for p in code),
                    )
                )
    return dict(
        format=1,
        trials=len(rows),
        requests=sum(r["requests"] for r in rows),
        all_requests_expose_tools=all(r["all_requests_expose_tools"] for r in rows),
        delegate_calls=sum(r["delegate_calls"] for r in rows),
        rows=rows,
    )


def describe_run(root):
    report = json.loads((root / "report.json").read_text(encoding="utf-8"))
    rows = []
    for attempt in report["task_report"]["attempts"]:
        events = events_at(root / attempt["directory"] / "ev/events.sqlite3")
        handoffs = attempt["evidence"]["investigations"]
        child_ids = {r["session_id"] for r in handoffs}
        main_ids = {
            s["session_id"] for s in attempt["evidence"]["execution"]["sessions"]
        } - child_ids
        requests = main_requests(events, main_ids)
        parent = [e for e in events if e["stream_id"] in {"session:" + sid for sid in main_ids}]
        delegates = [
            e
            for e in parent
            if e["type"] == "tool/call" and e["data"]["tool_name"] == "delegate_investigation"
        ]
        prior_reads = [
            e
            for e in parent
            if e["type"] == "tool/result"
            and e["data"]["tool_name"] in {"read_file", "search_text", "list_files"}
            and e["data"]["status"] == "succeeded"
        ]
        rows.append(
            dict(
                case=attempt["benchmark_task_id"],
                repetition=attempt["repetition"],
                structural_success=attempt["success"],
                error=attempt["error_code"],
                delegate_calls=len(delegates),
                delegation_after_actual_read=any(
                    r["stream_id"] == d["stream_id"] and r["seq"] < d["seq"]
                    for r in prior_reads
                    for d in delegates
                ),
                handoffs=handoffs,
                main_requests=len(requests),
                collaboration=attempt["evidence"]["collaboration"],
                execution=attempt["evidence"]["execution"],
                task_status=attempt["evidence"]["product_status"],
            )
        )
    return dict(
        format=1,
        rows=rows,
        quality_interpretation="Structural gate only; conclusions need source review.",
    )
