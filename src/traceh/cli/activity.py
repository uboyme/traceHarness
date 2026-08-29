"""Line-CLI validation for the shared Chat activity interval.

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

The actual in-flight projection is UI-neutral and lives in
``traceh.chat.activity``.  This adapter owns only translation from an invalid
command-line value into the CLI error vocabulary.
"""

from __future__ import annotations

import math

from traceh.chat.activity import (
    DEFAULT_HEARTBEAT_SECONDS,
    ActivityPhase,
    ActivityUpdate,
)
from traceh.cli.errors import CliConfigurationError
from traceh.cli.timeline import sanitize


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


def render_activity_wait(update: ActivityUpdate) -> str:
    """Render one typed waiting update for the Line adapter."""

    if update.phase is not ActivityPhase.WAITING:
        raise ValueError("activity update is not a waiting update")
    return (
        f"[waiting {_format_seconds(update.elapsed_seconds)}] "
        f"{sanitize(update.label)} {update.predicate}"
    )


def _format_seconds(value: float) -> str:
    if float(value).is_integer():
        return f"{int(value)}s"
    return f"{value:.1f}s"


__all__ = [
    "DEFAULT_HEARTBEAT_SECONDS",
    "render_activity_wait",
    "validate_heartbeat_seconds",
]
