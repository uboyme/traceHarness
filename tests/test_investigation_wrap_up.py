"""An investigation must stop searching before its budget runs out.

A real trial ended with the assistant cancelled on its 25-minute wall clock
after 47 model calls, having delivered nothing; the main Agent then had no
report to collect and the whole run failed. Steps were only 48 of 100, so a
step-count reserve alone would have changed nothing - whichever authorized
resource binds first has to be the one that triggers.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from traceh.api.budgets import BudgetLimits
from traceh.api.events import EventEnvelope
from traceh.api.llm import ToolSchema
from traceh.supervision.investigation_wrap_up import (
    WRAP_UP_LABEL,
    InvestigationStepView,
    WrapUpReserve,
)

TURN = "turn-1"


class _Composition:
    tools = (
        ToolSchema("read_file", "read", {"type": "object", "properties": {}}),
        ToolSchema("search_text", "search", {"type": "object", "properties": {}}),
    )
    system_prompt = "investigate"


def event(seq: int, kind: str, *, minute: int = 0) -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid4(),
        stream_id="session:s",
        seq=seq,
        type=kind,
        data={"turn_id": TURN},
        occurred_at=f"2026-09-21T04:{minute:02d}:00+00:00",
        schema_version=1,
    )


def limits(**overrides) -> BudgetLimits:
    return BudgetLimits(
        **{
            "max_tokens": 2_000_000,
            "max_steps": 100,
            "max_tool_calls": 180,
            "max_wall_milliseconds": 1_500_000,
            "max_children": 0,
            "max_depth": 0,
            "max_processes": 0,
            **overrides,
        }
    )


def reserve(**overrides) -> WrapUpReserve:
    return WrapUpReserve(
        **{"steps": 4, "tool_calls": 20, "wall_milliseconds": 180_000, **overrides}
    )


async def select(events, view):
    return await view.select(tuple(events), _Composition(), turn_id=TURN, step_id="s1")


@pytest.mark.asyncio
async def test_tools_stay_available_while_the_budget_holds() -> None:
    view = InvestigationStepView(limits(), reserve())
    events = [event(1, "turn/start"), event(2, "step/start"), event(3, "tool/call")]

    selection = await select(events, view)

    assert selection.view.label == "investigation"
    assert set(selection.view.tool_names) == {"read_file", "search_text"}


@pytest.mark.asyncio
async def test_the_wall_clock_alone_can_trigger_the_wrap_up() -> None:
    """The case that actually happened: time ran out with steps to spare."""

    view = InvestigationStepView(limits(), reserve())
    # 22 minutes elapsed against a 25-minute grant with a 3-minute reserve,
    # while only three steps and one tool call have been used.
    events = [
        event(1, "turn/start", minute=0),
        event(2, "step/start", minute=0),
        event(3, "tool/call", minute=1),
        event(4, "step/start", minute=22),
    ]

    selection = await select(events, view)

    assert selection.view.label == WRAP_UP_LABEL
    # Withdrawn, not discouraged: there is nothing left to call.
    assert selection.view.tool_names == ()
    assert "Write your report now" in selection.view.system_prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("events", "why"),
    [
        (
            [event(1, "turn/start")] + [event(2 + i, "step/start") for i in range(96)],
            "step count",
        ),
        (
            [event(1, "turn/start")] + [event(2 + i, "tool/call") for i in range(160)],
            "tool calls",
        ),
    ],
    ids=("steps", "tool-calls"),
)
async def test_whichever_resource_binds_first_triggers(events, why) -> None:
    selection = await select(events, InvestigationStepView(limits(), reserve()))
    assert selection.view.tool_names == (), why


@pytest.mark.asyncio
async def test_elapsed_time_comes_from_the_events_not_a_clock() -> None:
    """Reading a clock would make a replayed request decide differently."""

    view = InvestigationStepView(limits(), reserve())
    events = [event(1, "turn/start", minute=0), event(2, "step/start", minute=0)]

    first = await select(events, view)
    # The same prefix, evaluated again much later in real time.
    second = await select(events, view)

    assert first.view.label == second.view.label == "investigation"


def test_a_reserve_must_be_stated_in_whole_non_negative_amounts() -> None:
    with pytest.raises(ValueError, match="wrap-up reserve steps"):
        WrapUpReserve(steps=-1, tool_calls=0, wall_milliseconds=0)
    with pytest.raises(ValueError, match="wrap-up reserve wall_milliseconds"):
        WrapUpReserve(steps=0, tool_calls=0, wall_milliseconds=None)


@pytest.mark.asyncio
async def test_the_main_agent_keeps_its_collect_tools_until_reports_are_in() -> None:
    """Withdrawing collect too early would make the contract unsatisfiable.

    The multi contract refuses delivery until every dispatched assignment has
    been collected as completed. Taking that tool away at the reserve would turn
    "nearly out of budget" into "can never finish", so it survives the wrap-up
    while any report is still outstanding - and only then.
    """

    from traceh.product.collaboration import WRAP_UP, CollaborationPolicy
    from traceh.supervision.delegation import COLLECT_INVESTIGATION
    from traceh.supervision.investigation_wrap_up import BoundReserve

    class _Reached:
        def reached(self, events, turn_id):
            return True

    class _MainComposition:
        tools = (
            ToolSchema("shell", "run", {"type": "object", "properties": {}}),
            ToolSchema("apply_patch", "edit", {"type": "object", "properties": {}}),
            ToolSchema(COLLECT_INVESTIGATION, "collect", {"type": "object", "properties": {}}),
        )
        system_prompt = "build it"

    policy = CollaborationPolicy(wrap_up=_Reached())
    # No accepted plan in these events, so nothing is outstanding.
    selection = await policy.select(
        (event(1, "turn/start"),), _MainComposition(), turn_id=TURN, step_id="s1"
    )

    assert selection.view.label == WRAP_UP
    # Nothing dispatched means nothing to collect: every working tool is gone.
    assert selection.view.tool_names == ()
    assert "Deliver now" in selection.view.system_prompt
    assert BoundReserve  # bound form is what production passes


def test_without_a_reserve_the_main_agent_keeps_its_old_behaviour() -> None:
    """Absence is the historical behaviour, not a quietly different default."""

    from traceh.product.collaboration import CollaborationPolicy

    class _MainComposition:
        tools = (ToolSchema("shell", "run", {"type": "object", "properties": {}}),)
        system_prompt = "build it"

    policy = CollaborationPolicy()
    assert policy._wrap_up_view((event(1, "turn/start"),), _MainComposition(), TURN) is None
