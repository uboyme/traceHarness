"""Append-only crash recovery and orphan-effect reconciliation."""

from __future__ import annotations

from dataclasses import dataclass

from traceh.api.events import EventEnvelope, attempt_identity
from traceh.api.json_types import JsonValue
from traceh.session.projections import StateProjector
from traceh.session.service import SessionService


def _is_evidence_for(candidate: EventEnvelope, start: EventEnvelope) -> bool:
    """Whether `candidate` can testify about the attempt opened by `start`.

    The caller has already matched `attempt_id`. What remains is that the event
    belongs to the same turn and step and was written *after* the attempt
    started; an earlier or differently scoped event describes something else.
    Scopes are compared by value, so `1` never passes for `"1"`.
    """

    return (
        candidate.seq > start.seq
        and candidate.data.get("turn_id") == start.data.get("turn_id")
        and candidate.data.get("step_id") == start.data.get("step_id")
    )


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    session_id: str
    changed: bool
    synthesized_tool_results: int
    closed_model_attempts: int
    closed_step: bool
    closed_turn: bool
    notes: tuple[str, ...]


_ATTEMPT_RECOVERED_FROM_MESSAGE = (
    "The process stopped before the model attempt was closed, but a complete assistant message "
    "for this attempt is already durable, so the attempt is closed as succeeded. Token usage and "
    "the original finish reason were not recorded and are not reconstructed."
)

_ATTEMPT_UNKNOWN_AFTER_CRASH = (
    "The process stopped after the model attempt started and no complete assistant message for "
    "this attempt is durable, so it is unknown whether the model ever returned a full response. "
    "Any partial chunks are kept as audit evidence only and were not merged into an assistant "
    "message. TraceHarness did not call the provider again."
)


class RecoveryService:
    def __init__(self, sessions: SessionService) -> None:
        self.sessions = sessions
        self.state = StateProjector()

    async def _close_model_attempts(
        self,
        session_id: str,
        session_events: tuple[EventEnvelope, ...],
        notes: list[str],
    ) -> int:
        """Append a `model/attempt-end` for every attempt that never got one.

        Evidence, not optimism, decides the status: an attempt is only closed as
        succeeded when a complete `assistant/message` for exactly the same
        attempt, turn and step is already durable. Otherwise the outcome is
        genuinely unknown and is recorded as such. The provider is never called
        again and partial chunks are never merged into a message.
        """

        starts: dict[str, EventEnvelope] = {}
        ended: set[str] = set()
        messages: dict[str, list[EventEnvelope]] = {}
        chunks: dict[str, list[EventEnvelope]] = {}
        unidentified_starts = 0

        for event in session_events:
            attempt_id = attempt_identity(event.data)
            if event.type == "model/attempt-start":
                if attempt_id is None:
                    unidentified_starts += 1
                elif attempt_id not in starts:
                    # First start wins, so repeated ids converge deterministically.
                    starts[attempt_id] = event
            elif attempt_id is None:
                continue
            elif event.type == "model/attempt-end":
                ended.add(attempt_id)
            elif event.type == "assistant/message":
                messages.setdefault(attempt_id, []).append(event)
            elif event.type == "assistant/chunk":
                chunks.setdefault(attempt_id, []).append(event)

        if unidentified_starts:
            notes.append(
                f"skipped {unidentified_starts} model attempt start events "
                "without a usable attempt_id"
            )

        closed = 0
        # ``starts`` keeps insertion order, so attempts converge in the order
        # their start events were originally appended.
        for attempt_id, start in starts.items():
            if attempt_id in ended:
                continue
            turn_id = str(start.data.get("turn_id", ""))
            step_id = str(start.data.get("step_id", ""))
            # Evidence has to belong to this attempt and be written after it
            # started; an id alone proves nothing.
            durable_message = any(
                _is_evidence_for(event, start) for event in messages.get(attempt_id, ())
            )
            partial_chunks = sum(
                1 for event in chunks.get(attempt_id, ()) if _is_evidence_for(event, start)
            )

            data: dict[str, JsonValue] = {
                "turn_id": turn_id,
                "step_id": step_id,
                "attempt_id": attempt_id,
                "recovered": True,
            }
            if durable_message:
                data["status"] = "succeeded"
                data["recovered_from"] = "assistant/message"
                data["message"] = _ATTEMPT_RECOVERED_FROM_MESSAGE
                notes.append(
                    f"closed model attempt {attempt_id} as succeeded from a durable "
                    "assistant message"
                )
            else:
                data["status"] = "unknown_after_crash"
                data["error_type"] = "RecoveredAfterCrash"
                data["recovered_from"] = "none"
                data["partial_chunks"] = partial_chunks
                data["message"] = _ATTEMPT_UNKNOWN_AFTER_CRASH
                notes.append(f"marked model attempt {attempt_id} unknown after crash")

            await self.sessions.append_session(
                session_id,
                "model/attempt-end",
                data,
                correlation_id=start.correlation_id,
                causation_id=start.event_id,
                composition_revision=start.composition_revision,
            )
            closed += 1
        return closed

    async def recover(self, session_id: str) -> RecoveryReport:
        await self.sessions.ensure_session(session_id)
        session_events = await self.sessions.read_session(session_id)
        effect_events = await self.sessions.read_effects(session_id)
        projection = self.state.project(session_events)

        notes: list[str] = []
        closed_model_attempts = await self._close_model_attempts(
            session_id, session_events, notes
        )

        calls = {
            str(event.data.get("tool_call_id")): event
            for event in session_events
            if event.type == "tool/call"
        }
        result_ids = {
            str(event.data.get("tool_call_id"))
            for event in session_events
            if event.type == "tool/result"
        }
        outcomes: dict[str, EventEnvelope] = {}
        intents: dict[str, EventEnvelope] = {}
        for event in effect_events:
            call_id = str(event.data.get("tool_call_id", ""))
            if event.type == "effect/intent":
                intents[call_id] = event
            elif event.type in {"effect/outcome", "effect/reconciled"}:
                outcomes[call_id] = event

        synthesized = 0
        for call_id, call_event in calls.items():
            if call_id in result_ids:
                continue
            outcome = outcomes.get(call_id)
            intent = intents.get(call_id)
            effect_id = str((outcome or intent).data.get("effect_id")) if (outcome or intent) else None
            if outcome is not None:
                status = str(outcome.data.get("status", "unknown"))
                content = str(
                    outcome.data.get("content")
                    or outcome.data.get("message")
                    or "Recovered from a durable effect outcome."
                )
                data = outcome.data.get("data", {})
                if not isinstance(data, dict):
                    data = {}
                recovered_status = "succeeded" if status == "succeeded" else status
                notes.append(f"recovered tool result {call_id} from effect outcome")
            else:
                recovered_status = "unknown_after_crash"
                content = (
                    "The process stopped after the tool call was recorded but before a durable outcome "
                    "was available. TraceHarness did not repeat the side effect automatically."
                )
                data = {}
                if intent is not None:
                    await self.sessions.append_effect(
                        session_id,
                        "effect/reconciled",
                        {
                            "effect_id": effect_id,
                            "tool_call_id": call_id,
                            "tool_name": str(call_event.data.get("tool_name", "")),
                            "status": "unknown_after_crash",
                            "message": content,
                        },
                    )
                notes.append(f"marked tool result {call_id} unknown after crash")

            await self.sessions.append_session(
                session_id,
                "tool/result",
                {
                    "turn_id": str(call_event.data.get("turn_id", "")),
                    "step_id": str(call_event.data.get("step_id", "")),
                    "tool_call_id": call_id,
                    "tool_name": str(call_event.data.get("tool_name", "")),
                    "status": recovered_status,
                    "content": content,
                    "data": data,
                    "effect_id": effect_id,
                    "error_type": "RecoveredAfterCrash",
                },
            )
            synthesized += 1

        closed_step = False
        closed_turn = False
        if projection.open_step_id is not None:
            turn_id = projection.open_turn_id or "unknown"
            await self.sessions.append_session(
                session_id,
                "step/end",
                {
                    "turn_id": turn_id,
                    "step_id": projection.open_step_id,
                    "reason": "interrupted",
                },
            )
            closed_step = True
            notes.append(f"closed interrupted step {projection.open_step_id}")
        if projection.open_turn_id is not None:
            await self.sessions.append_session(
                session_id,
                "turn/end",
                {"turn_id": projection.open_turn_id, "reason": "interrupted"},
            )
            closed_turn = True
            notes.append(f"closed interrupted turn {projection.open_turn_id}")

        changed = synthesized > 0 or closed_model_attempts > 0 or closed_step or closed_turn
        if changed:
            await self.sessions.append_session(
                session_id,
                "runtime/recovered",
                {
                    "synthesized_tool_results": synthesized,
                    "closed_model_attempts": closed_model_attempts,
                    "closed_step": closed_step,
                    "closed_turn": closed_turn,
                    "notes": notes,
                },
            )
        return RecoveryReport(
            session_id=session_id,
            changed=changed,
            synthesized_tool_results=synthesized,
            closed_model_attempts=closed_model_attempts,
            closed_step=closed_step,
            closed_turn=closed_turn,
            notes=tuple(notes),
        )
