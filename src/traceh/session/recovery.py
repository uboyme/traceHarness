"""Append-only crash recovery and orphan-effect reconciliation."""

from __future__ import annotations

from dataclasses import dataclass

from traceh.api.events import EventEnvelope
from traceh.session.projections import StateProjector
from traceh.session.service import SessionService


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    session_id: str
    changed: bool
    synthesized_tool_results: int
    closed_step: bool
    closed_turn: bool
    notes: tuple[str, ...]


class RecoveryService:
    def __init__(self, sessions: SessionService) -> None:
        self.sessions = sessions
        self.state = StateProjector()

    async def recover(self, session_id: str) -> RecoveryReport:
        await self.sessions.ensure_session(session_id)
        session_events = await self.sessions.read_session(session_id)
        effect_events = await self.sessions.read_effects(session_id)
        projection = self.state.project(session_events)

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
        notes: list[str] = []
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

        changed = synthesized > 0 or closed_step or closed_turn
        if changed:
            await self.sessions.append_session(
                session_id,
                "runtime/recovered",
                {
                    "synthesized_tool_results": synthesized,
                    "closed_step": closed_step,
                    "closed_turn": closed_turn,
                    "notes": notes,
                },
            )
        return RecoveryReport(
            session_id,
            changed,
            synthesized,
            closed_step,
            closed_turn,
            tuple(notes),
        )
