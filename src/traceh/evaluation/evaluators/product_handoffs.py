"""Diagnostic joins of original Inbox, Delivery and successful model dispatches.

Read outputs are not automatically relevant evidence. These locators let an
independent reviewer inspect relevance without mistaking completion for reading.
"""

import json
from collections import Counter

from traceh.agents import AgentDirectoryReader, AgentInboxReader
from traceh.api.json_types import fingerprint
from traceh.evaluation.evaluators.episode_assessment import successful_dispatches
from traceh.session.service import SessionService
from traceh.supervision.errors import AgentMessageNotSettledError
from traceh.supervision.investigation_work import read_investigation_work
from traceh.supervision.reports import AgentRunReportReader
from traceh.supervision.writable_work import read_writable_work


def _visible(events, turn_id=None):
    turns = {e.data["turn_id"] for e in events if e.type == "turn/end"}
    for turn in sorted(turns if turn_id is None else {turn_id}):
        for _, snapshot in successful_dispatches(events, target_start=0, target_turn=turn):
            for message in snapshot.data["dispatch_request"]["messages"]:
                if message["role"] == "tool" and any(
                    event.type == "tool/result"
                    and event.seq < snapshot.seq
                    and event.data["status"] == "succeeded"
                    and event.data["tool_call_id"] == message.get("tool_call_id")
                    and event.data["tool_name"] == message.get("name")
                    and event.data["content"] == message["content"]
                    for event in events
                ):
                    yield snapshot, message


async def investigations(store, main_id, revision):
    directory = await AgentDirectoryReader(store).load()
    main = directory.get(main_id)
    if main is None:
        raise ValueError("evaluation-investigation-main-missing")
    sessions = SessionService(store)
    parent = await sessions.read_session(main.session_id)
    parent_visible = tuple(_visible(parent))
    rows = []
    for child in directory.records:
        if child.owner_agent_id != main_id:
            continue
        events = await sessions.read_session(child.session_id)
        for accepted in await AgentInboxReader(store).load(child.agent_id):
            writable = "apply_patch" in child.capability_grants
            work = (
                read_writable_work(accepted.message.content)
                if writable
                else read_investigation_work(accepted.message.content)
            )
            if writable:
                from traceh.product.execution import product_task_owner_id
                from traceh.workspaces.catalog import WorkspaceCatalogReader

                workspace = (await WorkspaceCatalogReader(store).load()).for_agent(child.agent_id)
                if (
                    work["child_agent_id"] != child.agent_id
                    or work["session_id"] != child.session_id
                    or work["message_id"] != accepted.message.message_id
                    or product_task_owner_id(work["task_id"]) != main.owner_agent_id
                    or workspace is None
                    or workspace.workspace_id != child.workspace_id
                    or workspace.source_id != work["source_id"]
                ):
                    raise ValueError("evaluation-writable-input-mismatch")
            if (
                accepted.message.source != main_id
                or work["owner_agent_id"] != main_id
                or work["revision"] != revision
                # The accepted role must match what the host actually granted, so
                # a mislabelled assignment cannot be reported as another role.
                or writable is not (work["role"] == "patch_author")
            ):
                raise ValueError("evaluation-investigation-input-mismatch")
            try:
                report = await AgentRunReportReader(store).load(
                    child.agent_id, accepted.message.message_id
                )
            except AgentMessageNotSettledError:
                report = None
            reads, consumed = {}, set()
            if report is not None and report.status == "completed":
                for snapshot, message in _visible(events, report.turn_id):
                    if message.get("name") in {"read_file", "search_text", "list_files"}:
                        receipt = {
                            "request": f"{snapshot.stream_id}@{snapshot.seq}",
                            "tool": message.get("name"),
                            "tool_call_id": message.get("tool_call_id"),
                            "content_digest": fingerprint(message["content"]),
                        }
                        reads[fingerprint(receipt)] = receipt
                for snapshot, message in parent_visible:
                    if message.get("name") not in {
                        "collect_investigation",
                        "collect_child_patch",
                        "submit_collaboration_plan",
                    }:
                        continue
                    try:
                        value = json.loads(message["content"])
                    except (ValueError, TypeError):
                        continue  # A bounded preview is not a complete report receipt.
                    # A plan receipt carries one row per assignment; only the row
                    # bound to this exact child counts as its delivered report.
                    receipts = (
                        value.get("children") or ()
                        if message.get("name") == "submit_collaboration_plan"
                        and type(value) is dict
                        else (value,)
                    )
                    if any(
                        type(receipt) is dict
                        and receipt.get("agent_id") == child.agent_id
                        and receipt.get("message_id") == accepted.message.message_id
                        and receipt.get("input_digest") == work["input_digest"]
                        and receipt.get("revision") == revision
                        and receipt.get("status") == report.status
                        and receipt.get("statement") == report.final_text
                        for receipt in receipts
                    ):
                        consumed.add(f"{snapshot.stream_id}@{snapshot.seq}")
            rows.append(
                {
                    "agent_id": child.agent_id,
                    "assignment_id": work["assignment_id"],
                    "role": work["role"],
                    "session_id": child.session_id,
                    "message_id": accepted.message.message_id,
                    "input_digest": work["input_digest"],
                    "source_id": work["source_id"],
                    "revision": work["revision"],
                    "goal": work["goal"],
                    "work_digest": fingerprint(
                        {
                            key: work[key]
                            for key in (
                                "source_id",
                                "revision",
                                "goal",
                                "scope",
                                "exclusions",
                                "deliverable",
                                "briefing",
                                "main_work",
                            )
                        }
                    ),
                    "report_available": report is not None,
                    "report_status": None if report is None else report.status,
                    "report_refs": [] if report is None else list(report.evidence_refs),
                    "child_visible_source_outputs": list(reads.values()),
                    "parent_report_dispatches": sorted(consumed),
                    "interpretation": (
                        "Visibility and completion only; "
                        "relevance and correctness require assessment."
                    ),
                }
            )
    return tuple(rows)


def collaboration_diagnostics(session_events, handoffs):
    """Observed overlap and identical work are diagnostics, never quality labels."""
    boundaries, valid = [], True
    for events in session_events:
        opened = {}
        for event in events:
            if event.type == "turn/start":
                opened[event.data["turn_id"]] = event.occurred_at
            elif event.type == "turn/end":
                start = opened.pop(event.data["turn_id"], None)
                if start is None or event.occurred_at < start:
                    valid = False
                elif event.occurred_at > start:
                    boundaries.extend(((start, 1), (event.occurred_at, -1)))
        valid = valid and not opened
    active, peak = 0, 0
    # Intervals are half-open: equal timestamps alone do not prove overlap.
    for _, delta in sorted(boundaries):
        active += delta
        peak = max(peak, active)
    repeated = Counter(row["work_digest"] for row in handoffs)
    return {
        "observed_peak_active_turns": peak if valid else None,
        "overlap_measurement": (
            "same-host recorded UTC half-open Turn intervals; not CPU parallelism"
        ),
        "identical_work_groups": sum(count > 1 for count in repeated.values()),
        "identical_work_extra_deliveries": sum(count - 1 for count in repeated.values()),
        "available_reports": sum(row["report_available"] for row in handoffs),
        "reports_dispatched_to_parent": sum(
            bool(row["parent_report_dispatches"]) for row in handoffs
        ),
        "interpretation": (
            "Identical work is not necessarily wasted work; visibility is not correctness."
        ),
    }
