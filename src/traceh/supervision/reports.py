"""Durable reconstruction of one supervised message's run report.

The report is evidence, not an Activation result cache. It joins the durable
Agent Directory, Inbox, delivery lifecycle and Session stream. A process that
did not execute the Turn therefore reaches the same answer, and a terminal
delivery fact that points at missing or contradictory Session facts fails
closed instead of manufacturing a successful child result.

Workspace patch artifacts are intentionally absent. Stage E can return the
child's final text and stable evidence references; isolated workspace branches
and `PatchArtifact` collection remain v0.7 work.
"""

from __future__ import annotations

from traceh.agents import AgentDirectoryReader, AgentInboxReader
from traceh.api.agents import AgentRunReport
from traceh.session.event_store import EventStore
from traceh.session.service import SessionService
from traceh.supervision.delivery import AgentDeliveryReader
from traceh.supervision.delivery_identity import require_delivery_identifier
from traceh.supervision.errors import (
    AgentMessageNotFoundError,
    AgentMessageNotSettledError,
    AgentRunEvidenceError,
)


class AgentRunReportReader:
    """Build `AgentRunReport` solely from durable facts."""

    __slots__ = ("_deliveries", "_directory", "_inboxes", "_sessions")

    def __init__(self, store: EventStore) -> None:
        self._directory = AgentDirectoryReader(store)
        self._inboxes = AgentInboxReader(store)
        self._deliveries = AgentDeliveryReader(store)
        self._sessions = SessionService(store)

    async def load(self, agent_id: str, message_id: str) -> AgentRunReport:
        agent_id = require_delivery_identifier(agent_id, field="agent_id")
        message_id = require_delivery_identifier(message_id, field="message_id")
        directory = await self._directory.load()
        record = directory.get(agent_id)
        if record is None:
            # Imported lazily to keep this replay reader independent of the
            # live Supervisor module during import.
            from traceh.supervision.supervisor import AgentNotFoundError

            raise AgentNotFoundError()

        inbox = await self._inboxes.load(agent_id)
        accepted = inbox.get(message_id)
        if accepted is None:
            raise AgentMessageNotFoundError()
        delivery = await self._deliveries.load(agent_id, inbox)
        claim = delivery.claim_for(message_id)
        outcome = delivery.outcome_for_message(message_id)
        if claim is None or outcome is None:
            raise AgentMessageNotSettledError()
        if claim.session_id != record.session_id:
            raise AgentRunEvidenceError("run-session-mismatch")

        evidence_refs = (
            f"agent-inbox:{agent_id}#{accepted.accepted_seq}",
            f"agent-delivery:{agent_id}#{outcome.terminal_seq}",
        )
        if outcome.state != "completed":
            return AgentRunReport(
                agent_id=agent_id,
                session_id=record.session_id,
                reason=outcome.code,
                final_text="",
                evidence_refs=evidence_refs,
                status=outcome.state,
                message_id=message_id,
            )
        if outcome.turn_id is None:
            raise AgentRunEvidenceError("run-turn-missing")

        reason, final_text, end_seq = await self._completed_turn(
            session_id=record.session_id,
            message_id=message_id,
            turn_id=outcome.turn_id,
        )
        if reason != outcome.code:
            raise AgentRunEvidenceError("run-reason-mismatch")
        return AgentRunReport(
            agent_id=agent_id,
            session_id=record.session_id,
            reason=reason,
            final_text=final_text,
            evidence_refs=(
                *evidence_refs,
                f"session:{record.session_id}#{end_seq}",
            ),
            status="completed",
            message_id=message_id,
            turn_id=outcome.turn_id,
        )

    async def _completed_turn(
        self,
        *,
        session_id: str,
        message_id: str,
        turn_id: str,
    ) -> tuple[str, str, int]:
        events = await self._sessions.read_session(session_id)
        starts: list[int] = []
        ends: list[tuple[int, str]] = []
        messages: list[tuple[int, str]] = []
        try:
            for event in events:
                event_type = event.type
                data = event.data
                event_turn_id = data.get("turn_id")
                if event_turn_id != turn_id:
                    continue
                if event_type == "turn/start":
                    if data.get("message_id") != message_id:
                        raise AgentRunEvidenceError("run-message-mismatch")
                    starts.append(event.seq)
                elif event_type == "assistant/message":
                    content = data.get("content")
                    if not isinstance(content, str):
                        raise AgentRunEvidenceError("run-final-text-invalid")
                    messages.append((event.seq, content))
                elif event_type == "turn/end":
                    reason = data.get("reason")
                    if not isinstance(reason, str):
                        raise AgentRunEvidenceError("run-reason-invalid")
                    ends.append((event.seq, reason))
            if len(starts) != 1 or len(ends) != 1 or not messages:
                raise AgentRunEvidenceError("run-lifecycle-incomplete")
            start_seq = starts[0]
            end_seq, reason = ends[0]
            if start_seq >= end_seq or any(
                message_seq <= start_seq or message_seq >= end_seq
                for message_seq, _ in messages
            ):
                raise AgentRunEvidenceError("run-lifecycle-order-invalid")
            return reason, messages[-1][1], end_seq
        except AgentRunEvidenceError:
            raise
        except Exception:
            # EventEnvelope is a public DTO. Reading its fields *and comparing
            # their order* are both untrusted work, so a hostile ``seq`` must
            # not leak a Python TypeError outside this stable boundary.
            raise AgentRunEvidenceError("run-event-invalid") from None


__all__ = ["AgentRunReportReader"]
