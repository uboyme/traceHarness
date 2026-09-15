"""DA admission uses the original Budget CAS and active-Turn gate."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from test_budget_enforcement import RecordingTool, root_context
from test_budget_ledger import AppendBarrierStore, granted_root, limits

from traceh.api.budgets import BudgetUsageReservationStatus
from traceh.api.events import PendingEvent
from traceh.api.llm import ModelResponse, ToolCall, Usage, UsageQuality
from traceh.api.tools import ToolOutput
from traceh.api.turns import TurnInput
from traceh.budgets import (
    BudgetEnforcement,
    BudgetExhaustedError,
    BudgetLedgerConflictError,
    BudgetLedgerReader,
    BudgetLedgerService,
    BudgetProtocolError,
)
from traceh.budgets.events import (
    BUDGET_CHILD_RESERVED,
    BUDGET_LEDGER_STREAM,
    BUDGET_SCHEMA_VERSION,
    child_reserved_data,
)
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.runtime.continuation import DefaultContinuationRuntime
from traceh.session.event_store import InMemoryEventStore
from traceh.supervision import AgentRuntimeExecution

pytestmark = pytest.mark.asyncio


def child_limits():
    return limits(
        max_tokens=40,
        max_steps=0,
        max_tool_calls=0,
        max_wall_milliseconds=0,
        max_children=0,
        max_depth=0,
        max_processes=0,
    )


async def test_concurrent_child_grants_preserve_parent_integration_tokens():
    inner = InMemoryEventStore()
    barrier = AppendBarrierStore(inner)
    await granted_root(barrier, root_limits=limits(max_tokens=100))
    services = (BudgetLedgerService(barrier), BudgetLedgerService(barrier))
    barrier.enabled = True

    async def reserve(index):
        return await services[index].reserve_child(
            operation_id=f"reserve-{index}",
            reservation_id=f"reservation-{index}",
            parent_agent_id="root",
            child_agent_id=f"child-{index}",
            creation_request_id=f"create-{index}",
            child_limits=child_limits(),
            retained_tokens=30,
        )

    tasks = [asyncio.create_task(reserve(i)) for i in range(2)]
    await barrier.both_entered.wait()
    barrier.release.set()
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    loser = next(i for i, item in enumerate(outcomes) if isinstance(item, BaseException))
    assert isinstance(outcomes[loser], BudgetLedgerConflictError)
    # A retry with fresh state must not consume the parent's protected reserve.
    with pytest.raises(BudgetExhaustedError) as exhausted:
        await reserve(loser)
    assert exhausted.value.dimension == "retained_tokens"
    ledger = await BudgetLedgerReader(inner).load()
    assert ledger.available("root").max_tokens == 60
    assert len(ledger.reservations) == 1
    assert ledger.reservations[0].retained_tokens == 30


async def test_reader_rejects_retention_violation_even_without_service():
    store = InMemoryEventStore()
    await granted_root(store, root_limits=limits(max_tokens=60))
    await store.append(
        BUDGET_LEDGER_STREAM,
        expected_seq=1,
        events=(
            PendingEvent(
                type=BUDGET_CHILD_RESERVED,
                schema_version=BUDGET_SCHEMA_VERSION,
                data=child_reserved_data(
                    operation_id="reserve",
                    reservation_id="reservation",
                    parent_agent_id="root",
                    child_agent_id="child",
                    creation_request_id="create",
                    child_limits=child_limits(),
                    retained_tokens=30,
                ),
            ),
        ),
    )
    with pytest.raises(BudgetProtocolError) as invalid:
        await BudgetLedgerReader(store).load()
    assert invalid.value.code == "budget-retained-tokens-exhausted"


@pytest.mark.parametrize("cancel", [False, True])
async def test_turn_wall_cap_leaves_child_time_and_settles_on_cancel(tmp_path: Path, cancel):
    store, service = await root_context(root_limits=limits(max_tokens=100))
    entered = asyncio.Event()
    release = asyncio.Event()

    class ReserveTool(RecordingTool):
        async def execute(self, arguments, context):
            await service.reserve_child(
                operation_id="reserve",
                reservation_id="reservation",
                parent_agent_id="agent-root",
                child_agent_id="child",
                creation_request_id="create-child",
                child_limits=limits(
                    max_tokens=10,
                    max_steps=1,
                    max_tool_calls=1,
                    max_wall_milliseconds=1000,
                    max_children=0,
                    max_depth=0,
                    max_processes=0,
                ),
                retained_tokens=20,
            )
            entered.set()
            await release.wait()
            return ToolOutput("child capacity reserved")

    provider = ScriptedLlmProvider(
        (
            ModelResponse(
                content="delegate",
                tool_calls=(ToolCall("call", "reserve", {}),),
                usage=Usage(1, 1, UsageQuality.EXACT),
            ),
            ModelResponse(content="done", usage=Usage(1, 1, UsageQuality.EXACT)),
        )
    )
    enforcement = BudgetEnforcement(
        service,
        agent_id="agent-root",
        session_id="session-root",
        continuation=DefaultContinuationRuntime(),
    )
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path, provider="scripted", model="model"),
        provider=provider,
        event_store=store,
        additional_tools=(ReserveTool("reserve", []),),
        continuation=enforcement.continuation,
        llm_runtime=enforcement.llm_runtime,
        tool_admission_gate=enforcement.tool_admission_gate,
    )
    execution = enforcement.wrap(
        AgentRuntimeExecution(runtime, "session-root"), max_turn_wall_milliseconds=2000
    )
    turn = asyncio.create_task(execution.run_turn(TurnInput("investigate", "work")))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        ledger = await service.ledger()
        assert ledger.available("agent-root").max_wall_milliseconds == 7000
        if cancel:
            turn.cancel()
            turn.cancel()
            with pytest.raises(asyncio.CancelledError):
                await turn
        else:
            release.set()
            await turn
        ledger = await service.ledger()
        assert ledger.account("agent-root").reserved.wall_milliseconds == 0
        assert all(
            r.status is BudgetUsageReservationStatus.SETTLED for r in ledger.usage_reservations
        )
    finally:
        release.set()
        if not turn.done():
            turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
        await execution.dispose()
