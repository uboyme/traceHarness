"""Typed, ephemeral activity derived from observed Session events.

This is display liveness, never durable state.  A model Attempt or admitted
Tool Call is tracked only between its existing start/end facts.  Recovery,
replay and request construction do not consult this module.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum

from traceh.api.events import EventEnvelope

DEFAULT_HEARTBEAT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class Clock:
    monotonic: Callable[[], float]
    sleep: Callable[[float], Awaitable[None]]


def default_clock() -> Clock:
    return Clock(monotonic=time.monotonic, sleep=asyncio.sleep)


class ActivityKind(StrEnum):
    MODEL = "model"
    TOOL = "tool"


class ActivityPhase(StrEnum):
    WAITING = "waiting"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class ActivityUpdate:
    """One adapter-neutral liveness update.

    ``elapsed_seconds`` is measured by the host monotonic clock.  For a waiting
    update it is the exact interval threshold crossed; for a completed update
    it is the measured start-to-end duration.  No terminal wording lives here.
    """

    kind: ActivityKind
    phase: ActivityPhase
    activity_id: str
    label: str
    predicate: str
    elapsed_seconds: float


@dataclass(slots=True)
class _InFlight:
    kind: ActivityKind
    activity_id: str
    label: str
    predicate: str
    started: float
    announced: int = 0


@dataclass(slots=True)
class ActivityTracker:
    """The sole in-flight activity projector shared by Line and future TUI."""

    interval_seconds: float
    monotonic: Callable[[], float]
    _active: dict[tuple[ActivityKind, str], _InFlight] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return self.interval_seconds > 0

    def pending_count(self) -> int:
        return len(self._active)

    def observe(self, event: EventEnvelope) -> ActivityUpdate | None:
        data = event.data if isinstance(event.data, dict) else {}
        if event.type == "model/attempt-start":
            self._start(
                ActivityKind.MODEL,
                _text(data, "attempt_id"),
                _model_label(data),
                "is still working",
            )
            return None
        if event.type == "model/attempt-end":
            return self._finish(ActivityKind.MODEL, _text(data, "attempt_id"))
        if event.type == "tool/admitted":
            self._start(
                ActivityKind.TOOL,
                _text(data, "tool_call_id"),
                _tool_label(data),
                "has not reported completion",
            )
            return None
        if event.type == "tool/result":
            return self._finish(ActivityKind.TOOL, _text(data, "tool_call_id"))
        return None

    def seconds_until_next_wait(self) -> float | None:
        if not self.enabled or not self._active:
            return None
        now = self.monotonic()
        return max(
            0.0,
            min(
                activity.started
                + (activity.announced + 1) * self.interval_seconds
                - now
                for activity in self._active.values()
            ),
        )

    def due_waits(self) -> tuple[ActivityUpdate, ...]:
        if not self.enabled or not self._active:
            return ()
        now = self.monotonic()
        updates: list[ActivityUpdate] = []
        for activity in self._active.values():
            due = int((now - activity.started) // self.interval_seconds)
            while activity.announced < due:
                activity.announced += 1
                updates.append(
                    ActivityUpdate(
                        kind=activity.kind,
                        phase=ActivityPhase.WAITING,
                        activity_id=activity.activity_id,
                        label=activity.label,
                        predicate=activity.predicate,
                        elapsed_seconds=(
                            activity.announced * self.interval_seconds
                        ),
                    )
                )
        return tuple(updates)

    def _start(
        self,
        kind: ActivityKind,
        activity_id: str | None,
        label: str,
        predicate: str,
    ) -> None:
        if activity_id is None:
            return
        self._active[(kind, activity_id)] = _InFlight(
            kind=kind,
            activity_id=activity_id,
            label=label,
            predicate=predicate,
            started=self.monotonic(),
        )

    def _finish(
        self, kind: ActivityKind, activity_id: str | None
    ) -> ActivityUpdate | None:
        if activity_id is None:
            return None
        activity = self._active.pop((kind, activity_id), None)
        if activity is None:
            return None
        return ActivityUpdate(
            kind=activity.kind,
            phase=ActivityPhase.COMPLETED,
            activity_id=activity.activity_id,
            label=activity.label,
            predicate=activity.predicate,
            elapsed_seconds=max(0.0, self.monotonic() - activity.started),
        )


def _text(data: dict, key: str) -> str | None:
    value = data.get(key)
    return value if type(value) is str and value else None


def _model_label(data: dict) -> str:
    provider = _text(data, "provider")
    model = _text(data, "model")
    if provider and model:
        return f"Model {provider}/{model}"
    return "Model"


def _tool_label(data: dict) -> str:
    name = _text(data, "tool_name") or "tool"
    call_id = _text(data, "tool_call_id")
    if call_id:
        return f"Tool {name} (call {call_id})"
    return f"Tool {name}"


__all__ = [
    "DEFAULT_HEARTBEAT_SECONDS",
    "ActivityKind",
    "ActivityPhase",
    "ActivityTracker",
    "ActivityUpdate",
    "Clock",
    "default_clock",
]
