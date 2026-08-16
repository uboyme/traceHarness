"""State projections derived exclusively from durable events."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from traceh.api.events import EventEnvelope


class SessionStatus(str, Enum):
    CREATED = "created"
    IDLE = "idle"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SessionProjection:
    status: SessionStatus
    open_turn_id: str | None = None
    open_step_id: str | None = None
    turns_completed: int = 0
    steps_completed: int = 0
    last_seq: int = 0
    last_error: str | None = None


class StateProjector:
    def project(self, events: tuple[EventEnvelope, ...]) -> SessionProjection:
        status = SessionStatus.CREATED
        open_turn: str | None = None
        open_step: str | None = None
        turns_completed = 0
        steps_completed = 0
        last_error: str | None = None

        for event in events:
            if event.type == "turn/start":
                open_turn = str(event.data["turn_id"])
                status = SessionStatus.RUNNING
            elif event.type == "step/start":
                open_step = str(event.data["step_id"])
            elif event.type == "step/end":
                if str(event.data.get("step_id")) == open_step:
                    open_step = None
                steps_completed += 1
            elif event.type == "turn/end":
                if str(event.data.get("turn_id")) == open_turn:
                    open_turn = None
                reason = str(event.data.get("reason", "completed"))
                if reason in {"interrupted", "cancelled"}:
                    status = SessionStatus.INTERRUPTED
                elif reason == "failed":
                    status = SessionStatus.FAILED
                else:
                    status = SessionStatus.IDLE
                turns_completed += 1
            elif event.type == "runtime/error":
                status = SessionStatus.FAILED
                last_error = str(event.data.get("message", "runtime error"))

        if open_turn is not None:
            status = SessionStatus.RUNNING
        return SessionProjection(
            status=status,
            open_turn_id=open_turn,
            open_step_id=open_step,
            turns_completed=turns_completed,
            steps_completed=steps_completed,
            last_seq=events[-1].seq if events else 0,
            last_error=last_error,
        )
