"""Make a read-only investigation stop investigating before it runs out.

An assistant that is cancelled mid-search delivers nothing, and everything it
spent is wasted: in one real trial the investigator used its whole 25-minute
wall clock across 47 model calls, was cancelled, and the main Agent then had no
report to collect. Its own messages showed it still gathering material after
saying it already had the core evidence. Nothing in the host ever told it to stop.

So the host reserves part of the authorization it already granted, and when that
reserve is reached it takes the tools away. Withdrawing them matters because a
prompt saying "stop searching now" is only advice, and this assistant
demonstrably kept searching.

Withdrawing them is not, however, enough on its own. A later run proved it: with
an empty tool list the model still emitted six reader calls, spent thirty
seconds doing it, had every one denied, and was then cancelled by the wall clock
before it wrote anything. The pull came from the host's own folded placeholders,
which stay in the conversation advertising a ready-to-run read action - an
instruction that is true for every other Step and false for this one. The
guidance below therefore says so explicitly, and the denial reply for a Step
that publishes no tools says it too. Enforcement stops the calls; only saying
what changed stops the model spending its last Steps on them.

Every trigger is derived from the event prefix, never from a runtime counter, so
a replay of a historical request reaches the same decision the original did.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from traceh.runtime.step_view import StepViewSelection
from traceh.session.request_view import RequestView

WRAP_UP_LABEL = "investigation-wrap-up"

WRAP_UP_GUIDANCE = """
Your investigation budget is nearly spent, so every tool has been withdrawn for
this request. There is nothing left to call and no further evidence to gather.

This includes the folded-output placeholders earlier in this conversation. Each
one names a read action, and that offer held for every earlier Step but does not
hold now: the reader is gone with the rest, and any call you make will be
refused without running. Attempting one spends the little budget you have left
and returns you nothing.

Write your report now from what you already have. State the conclusions you
actually confirmed and where the evidence is, then state plainly what you did
not resolve and what you would look at next. An honest partial finding with its
gaps named is useful to the main Agent; silence and a cancelled run are not.

Do not claim you verified something you did not, and do not describe intended
work as completed.
""".strip()


@dataclass(frozen=True, slots=True)
class WrapUpReserve:
    """How much of the granted authorization is held back for delivery.

    Stated absolutely rather than as a fraction: a percentage of a large grant
    can still be too little time to write a report, and the host is the only
    party that knows how long its model takes to produce one.
    """

    steps: int
    tool_calls: int
    wall_milliseconds: int

    def __post_init__(self) -> None:
        for value, field in (
            (self.steps, "steps"),
            (self.tool_calls, "tool_calls"),
            (self.wall_milliseconds, "wall_milliseconds"),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"wrap-up reserve {field} must be a non-negative integer")

    def to_dict(self) -> dict[str, int]:
        return {
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "wall_milliseconds": self.wall_milliseconds,
        }

    def reached(self, events, turn_id: str, limits) -> bool:
        """Whether any authorized resource is within its held-back margin.

        Whichever runs out first decides. In the trial that motivated this it
        was the wall clock, with steps at 48 of 100 - reserving only on step
        count would have changed nothing.
        """

        steps = sum(
            1
            for event in events
            if event.type == "step/start" and event.data.get("turn_id") == turn_id
        )
        calls = sum(
            1
            for event in events
            if event.type == "tool/call" and event.data.get("turn_id") == turn_id
        )
        checks = (
            (limits.max_steps, steps, self.steps),
            (limits.max_tool_calls, calls, self.tool_calls),
            (
                limits.max_wall_milliseconds,
                _elapsed_milliseconds(events, turn_id),
                self.wall_milliseconds,
            ),
        )
        return any(
            limit is not None and used >= max(0, limit - held) for limit, used, held in checks
        )


def _elapsed_milliseconds(events, turn_id: str) -> int:
    """Elapsed time this Turn, measured between recorded events.

    Reading a clock here would make the decision unreproducible. The newest
    event is at most one step old, so this underestimates by less than a step -
    and underestimating is the safe direction for a reserve.
    """

    stamps = [
        event.occurred_at
        for event in events
        if event.data.get("turn_id") == turn_id and event.occurred_at
    ]
    if len(stamps) < 2:
        return 0
    try:
        first = datetime.fromisoformat(str(stamps[0]))
        last = datetime.fromisoformat(str(stamps[-1]))
    except ValueError:
        return 0
    return max(0, int((last - first).total_seconds() * 1000))


class InvestigationStepView:
    """Full tools while the budget holds; no tools once the reserve is reached."""

    def __init__(self, limits, reserve: WrapUpReserve) -> None:
        self._limits = limits
        self._reserve = reserve

    def _out_of_room(self, events, turn_id: str) -> bool:
        return self._reserve.reached(events, turn_id, self._limits)

    async def select(self, events, composition, *, turn_id, step_id):
        del step_id
        if not self._out_of_room(events, turn_id):
            return StepViewSelection(
                RequestView(
                    "investigation",
                    tuple(tool.name for tool in composition.tools),
                    composition.system_prompt,
                )
            )
        return StepViewSelection(
            RequestView(WRAP_UP_LABEL, (), composition.system_prompt + "\n" + WRAP_UP_GUIDANCE)
        )


@dataclass(frozen=True, slots=True)
class BoundReserve:
    """A reserve already bound to the limits it is measured against.

    Lets a step view ask "am I nearly out?" without knowing anything about
    Budget shapes, and keeps both roles on one rule.
    """

    reserve: WrapUpReserve
    limits: object

    def reached(self, events, turn_id: str) -> bool:
        return self.reserve.reached(events, turn_id, self.limits)
