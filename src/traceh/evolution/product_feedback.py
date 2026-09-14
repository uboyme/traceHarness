"""Read task-owned structural signals; never label user work as a benchmark answer."""

from traceh.agents.inbox import AgentInboxReader
from traceh.api.json_types import fingerprint
from traceh.api.optimization import RuntimeObservation
from traceh.api.product import ResolvedTaskMode
from traceh.product.activity import ProductTaskActivityReader
from traceh.supervision.execution import durable_log_identity
from traceh.supervision.reports import AgentRunReportReader


async def product_observation(sessions, period, reader, task_id):
    if durable_log_identity(reader.store) is not durable_log_identity(sessions.store):
        raise ValueError("background-product-store-mismatch")
    observation = await reader.load(task_id)
    summary = observation.summary
    if summary is None or summary.resolved_mode != ResolvedTaskMode.MULTI:
        return None
    workspace = await sessions.workspace_for(summary.confirmation_session_id)
    if str(workspace.resolve()) != period.workspace:
        raise ValueError("background-observation-outside-scope")
    if not summary.settled and summary.status.value != "awaiting_approval":
        return None
    activity = await ProductTaskActivityReader(sessions.store).load(observation)
    reports = []
    for role in activity.roles:
        if role.turns_started != role.turns_completed:
            return None
        inbox = await AgentInboxReader(sessions.store).load(role.agent_id)
        for accepted in inbox:
            # The original reader refuses an unsettled delivery. No partial task
            # is admitted as a finished observation, including cancel-before-start.
            reports.append(
                await AgentRunReportReader(sessions.store).load(
                    role.agent_id, accepted.message.message_id
                )
            )
    denied = sum(tool.status == "denied" for role in activity.roles for tool in role.tools)
    failed = sum(tool.status == "failed" for role in activity.roles for tool in role.tools)
    unfinished = sum(report.status != "completed" for report in reports)
    if not denied and not failed and not unfinished:
        return None
    root_events = await sessions.read_session(summary.confirmation_session_id)
    ended = next(
        (
            event
            for event in root_events
            if event.type == "turn/end"
            and event.data.get("turn_id") == summary.confirmation_turn_id
        ),
        None,
    )
    if ended is None:
        raise ValueError("background-turn-not-completed")
    root_evidence = tuple(event for event in root_events if event.seq <= ended.seq)
    evidence = [(sessions.session_stream(summary.confirmation_session_id), root_evidence)]
    for head in observation.stream_heads:
        if not head.task_bound or head.stream_id == evidence[0][0]:
            continue
        events = await sessions.store.read(head.stream_id)
        evidence.append((head.stream_id, tuple(event for event in events if event.seq <= head.seq)))
    # AgentRunReport includes exact accepted/delivery references. Its status is
    # authoritative for completion, but its final_text is only an agent statement.
    locations = tuple(f"{stream}@{events[-1].seq if events else 0}" for stream, events in evidence)
    locations += tuple(ref for report in reports for ref in report.evidence_refs)
    return RuntimeObservation(
        summary.confirmation_session_id,
        summary.confirmation_turn_id,
        ended.seq,
        fingerprint((evidence, reports)),
        "product-signal-unverified",
        f"Completed multi task has {denied} denied and {failed} failed tool results; "
        f"{unfinished} agent deliveries ended without completion. Cancellation or denial may "
        "be intentional. These counts do not prove a wrong answer or a useful text change. "
        "Only propose a general improvement supported by the permitted evidence; otherwise stop.",
        locations,
    )
