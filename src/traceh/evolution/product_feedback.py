"""Read task-owned mechanisms; never label user work as a benchmark answer."""

from traceh.agents.inbox import AgentInboxReader
from traceh.evolution.detection import detect, runtime_observation, turn_bounds
from traceh.product.activity import ProductTaskActivityReader
from traceh.supervision.execution import durable_log_identity
from traceh.supervision.reports import AgentRunReportReader


async def product_findings(sessions, period, reader, task_id):
    """Detections in a settled Product task, one source per task, any mode.

    The task is read through the original Product, Activity and Inbox readers; an
    unsettled delivery or a Turn still running means no finding yet, not a partial one.
    """

    if durable_log_identity(reader.store) is not durable_log_identity(sessions.store):
        raise ValueError("background-product-store-mismatch")
    observation = await reader.load(task_id)
    summary = observation.summary
    if summary is None or summary.resolved_mode is None:
        return ()
    workspace = await sessions.workspace_for(summary.confirmation_session_id)
    if str(workspace.resolve()) != period.workspace:
        raise ValueError("background-observation-outside-scope")
    if not summary.settled and summary.status.value != "awaiting_approval":
        return ()
    activity = await ProductTaskActivityReader(sessions.store).load(observation)
    for role in activity.roles:
        if role.turns_started != role.turns_completed:
            return ()
        inbox = await AgentInboxReader(sessions.store).load(role.agent_id)
        for accepted in inbox:
            # The original reader refuses an unsettled delivery.
            await AgentRunReportReader(sessions.store).load(
                role.agent_id, accepted.message.message_id
            )
    findings = []
    for head in observation.stream_heads:
        if head.task_bound and head.stream_id.startswith("session:"):
            events = await sessions.store.read(head.stream_id)
            findings.extend(
                stream_findings(
                    f"product:{task_id}",
                    head.stream_id,
                    tuple(e for e in events if e.seq <= head.seq),
                )
            )
    return tuple(findings)


def stream_findings(source, stream_id, events):
    """Detections in one task-bound Session, cut at its last durable Turn end."""

    from traceh.evolution.background import Finding

    ended = [e for e in events if e.type == "turn/end"]
    if not ended or turn_bounds(events, ended[-1].data["turn_id"]) is None:
        return ()
    session_id, turn_id = stream_id.removeprefix("session:"), ended[-1].data["turn_id"]
    return tuple(
        Finding(source, runtime_observation(session_id, turn_id, events, detection))
        for detection in detect(session_id, events, stream_id=stream_id)
    )
