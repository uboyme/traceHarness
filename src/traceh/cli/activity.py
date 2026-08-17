"""Ephemeral liveness state for the interactive CLI.

A timeline built purely from events goes quiet exactly when the user most wants
reassurance: between `model/attempt-start` and `model/attempt-end` there is no
event to print, so a slow provider or a slow tool is indistinguishable from a
hang. This module keeps just enough transient state to say "still working"
without inventing anything.

What it is not, and must never become:

* **Not an event.** Nothing here is appended to any stream. There is no
  heartbeat event type, `AgentLoop` is not asked to emit one, and no line
  produced here carries an ``[event N]`` prefix - that prefix is reserved for a
  real persisted `seq`.
* **Not a fact source.** Recovery, replay, the surface projection and request
  fingerprints never see this. It is display state that dies with the process.
* **Not a second history.** Only in-flight work is tracked; a completed activity
  is forgotten immediately.

Everything it knows comes from events the timeline already receives:
`model/attempt-start` and `tool/admitted` open an activity, `model/attempt-end`
and `tool/result` close it. Activities are keyed by `attempt_id` and
`tool_call_id`, because `ToolRuntime` runs read-only tools concurrently and a
single "current activity" would report one of them and lose the rest.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from traceh.api.events import EventEnvelope
from traceh.cli.errors import CliConfigurationError
from traceh.cli.timeline import payload_text

#: Default seconds between "still working" lines.
DEFAULT_HEARTBEAT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class Clock:
    """The two time operations the heartbeat needs, injectable for tests.

    ``monotonic`` must be monotonic rather than wall clock: a waiting duration
    computed from wall clock would jump - or go backwards - if the system time is
    adjusted mid-turn, reporting a nonsense elapsed time or suppressing the
    heartbeat entirely.

    ``sleep`` is separated from the clock reading so a test can advance time
    deterministically instead of waiting ten real seconds to observe one line.
    """

    monotonic: Callable[[], float]
    sleep: Callable[[float], Awaitable[None]]


def default_clock() -> Clock:
    return Clock(monotonic=time.monotonic, sleep=asyncio.sleep)


def validate_heartbeat_seconds(value: float, *, timeline: bool = True) -> float:
    """Resolve the configured heartbeat interval, or fail loudly.

    ``0`` disables the heartbeat while leaving the timeline intact. Turning the
    timeline off disables the heartbeat too: it is a timeline decoration, and a
    "still working" line with no surrounding activity to relate it to would be
    noise. Anything not a usable duration - negative, NaN, infinity - is a
    configuration error rather than a silently clamped value.
    """

    if not timeline:
        return 0.0
    number = float(value)
    if math.isnan(number):
        raise CliConfigurationError("--heartbeat-seconds must be a number, not NaN")
    if math.isinf(number):
        raise CliConfigurationError("--heartbeat-seconds must be finite")
    if number < 0:
        raise CliConfigurationError(
            f"--heartbeat-seconds cannot be negative (got {number:g}); use 0 to disable it"
        )
    return number


@dataclass(slots=True)
class _InFlight:
    """One activity that has started and not yet reported a result."""

    label: str
    verb: str
    started: float
    announced: int = 0


@dataclass(slots=True)
class ActivityTracker:
    """Tracks in-flight model attempts and tool calls to report waiting time.

    Two outputs, both derived only from observed events plus the clock:

    * `due_waits()` returns the "still working" lines that have come due;
    * `observe()` returns how long an activity ran, when the event it just saw
      is the one that ended it, so the completion line can carry a duration.
    """

    interval_seconds: float
    monotonic: Callable[[], float]
    _active: dict[tuple[str, str], _InFlight] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return self.interval_seconds > 0

    def pending_count(self) -> int:
        """How many activities are currently in flight. Diagnostics and tests."""

        return len(self._active)

    def observe(self, event: EventEnvelope) -> float | None:
        """Update tracking from one event; return elapsed seconds if it ended one.

        Unknown or irrelevant events are ignored, and a payload missing its
        identity is ignored rather than tracked under an invented key - an
        activity that cannot be identified cannot be matched to its end either,
        so tracking it would leak a "still working" line forever.

        The elapsed time returned is measured between the two *events*: for a
        model attempt, `model/attempt-start` to `model/attempt-end`; for a tool,
        `tool/admitted` to the persisted `tool/result`. For a tool inside a
        gathered parallel group that is longer than its own execution, because
        the result is only appended once the whole group is done.
        """

        data = event.data if isinstance(event.data, dict) else {}
        if event.type == "model/attempt-start":
            self._start(
                ("model", payload_text(data, "attempt_id")),
                self._model_label(data),
                # A model attempt really is still in progress: its end event is
                # appended as soon as the provider returns, with nothing batched
                # in between.
                "is still working",
            )
            return None
        if event.type == "model/attempt-end":
            return self._finish(("model", payload_text(data, "attempt_id")))
        if event.type == "tool/admitted":
            self._start(
                ("tool", payload_text(data, "tool_call_id")),
                self._tool_label(data),
                # Not "still running", which would overclaim. `ToolRuntime`
                # gathers a parallel-safe group and appends every `tool/result`
                # only once the whole group finishes, so a tool that has already
                # returned is indistinguishable - from the event stream - from one
                # still executing. What is actually known is that no result has
                # been persisted yet, so that is what the line says.
                "has not reported completion",
            )
            return None
        if event.type == "tool/result":
            return self._finish(("tool", payload_text(data, "tool_call_id")))
        return None

    def seconds_until_next_wait(self) -> float | None:
        """Delay until the earliest activity crosses its next threshold.

        ``None`` when nothing is in flight, and never negative.

        This exists because a heartbeat that simply sleeps one interval at a time
        is phase-locked to whenever it started, not to the work it reports on.
        With a 10s interval and a tool starting at t=10.1, the t=20 wake sees only
        9.9s elapsed and stays silent, so the first "waiting 10s" line lands at
        t=30 - nearly twenty seconds after the user began waiting, which is
        exactly the stretch this feature exists to cover. Scheduling from each
        activity's own deadline makes the first line appear one interval after
        that activity started, whatever phase it started in.
        """

        if not self.enabled or not self._active:
            return None
        now = self.monotonic()
        return max(
            0.0,
            min(
                activity.started + (activity.announced + 1) * self.interval_seconds - now
                for activity in self._active.values()
            ),
        )

    def due_waits(self) -> tuple[str, ...]:
        """Lines for activities that have crossed another interval boundary.

        The reported number is the threshold that was crossed, not the raw
        elapsed time, so a slow event loop produces "20s" rather than "20.3s"
        and successive lines stay readable.
        """

        if not self.enabled or not self._active:
            return ()
        now = self.monotonic()
        lines: list[str] = []
        for activity in self._active.values():
            due = int((now - activity.started) // self.interval_seconds)
            while activity.announced < due:
                activity.announced += 1
                waited = activity.announced * self.interval_seconds
                # The predicate lives entirely in ``verb`` because a model and a
                # tool can honestly claim different things: see `observe()`.
                lines.append(
                    f"[waiting {_format_seconds(waited)}] {activity.label} {activity.verb}"
                )
        return tuple(lines)

    def _start(self, key: tuple[str, str | None], label: str, verb: str) -> None:
        if key[1] is None:
            return
        self._active[(key[0], key[1])] = _InFlight(
            label=label, verb=verb, started=self.monotonic()
        )

    def _finish(self, key: tuple[str, str | None]) -> float | None:
        if key[1] is None:
            return None
        activity = self._active.pop((key[0], key[1]), None)
        if activity is None:
            return None
        return max(0.0, self.monotonic() - activity.started)

    @staticmethod
    def _model_label(data: dict) -> str:
        provider = payload_text(data, "provider")
        model = payload_text(data, "model")
        if provider and model:
            return f"Model {provider}/{model}"
        return "Model"

    @staticmethod
    def _tool_label(data: dict) -> str:
        """Name and call id only.

        Deliberately no arguments: a tool's arguments carry paths, patches and -
        for `shell` - a whole command line, and a liveness line is not worth the
        risk of putting any of that on a terminal every ten seconds.
        """

        name = payload_text(data, "tool_name") or "tool"
        call_id = payload_text(data, "tool_call_id")
        if call_id:
            return f"Tool {name} (call {call_id})"
        return f"Tool {name}"


def _format_seconds(value: float) -> str:
    """Whole seconds when exact, one decimal otherwise."""

    if float(value).is_integer():
        return f"{int(value)}s"
    return f"{value:.1f}s"
