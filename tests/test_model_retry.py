from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from traceh.agents import AgentRegistrar
from traceh.api.agents import AgentSpec
from traceh.api.budgets import BudgetLimits, BudgetUsageReservationStatus
from traceh.api.events import PendingEvent
from traceh.api.llm import (
    ModelAttemptIdentity,
    ModelRequest,
    ModelResponse,
    Usage,
    UsageQuality,
)
from traceh.api.turns import TurnInput
from traceh.budgets import (
    BudgetedLlmRuntime,
    BudgetEnforcement,
    BudgetExhaustedError,
    BudgetLedgerService,
)
from traceh.llm.failures import ProviderFailure, ProviderFailureCategory
from traceh.llm.retry import ModelRetryPolicy, RetryScheduler
from traceh.llm.runtime import LlmAdmission, LlmRuntime
from traceh.runtime.agent_loop import ModelRetryRequestDriftError
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.runtime.continuation import DefaultContinuationRuntime
from traceh.session.event_store import Durability, InMemoryEventStore
from traceh.session.service import ModelAttemptConflictError
from traceh.supervision import AgentRuntimeExecution

pytestmark = pytest.mark.asyncio


def retry_policy(*, max_attempts: int = 2) -> ModelRetryPolicy:
    return ModelRetryPolicy(
        max_attempts=max_attempts,
        max_elapsed_seconds=10.0,
        base_delay_seconds=0.25,
        max_delay_seconds=2.0,
        retry_after_cap_seconds=1.0,
        jitter_ratio=0.0,
    )


class FakeScheduler:
    def __init__(self) -> None:
        self.now = 0.0
        self.delays: list[float] = []
        self.sleep_entered = asyncio.Event()
        self.sleep_release: asyncio.Event | None = None

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    async def sleep(self, seconds: float) -> None:
        self.delays.append(seconds)
        self.sleep_entered.set()
        if self.sleep_release is not None:
            await self.sleep_release.wait()
        self.advance(seconds)

    def scheduler(self) -> RetryScheduler:
        return RetryScheduler(self.monotonic, self.sleep, lambda: 0.5)


class OutcomeProvider:
    name = "scripted"

    def __init__(self, scheduler: FakeScheduler, outcomes: tuple[object, ...]) -> None:
        self._scheduler = scheduler
        self._outcomes = outcomes
        self.calls = 0
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        self.requests.append(request)
        self._scheduler.advance(0.02)
        outcome = self._outcomes[self.calls - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        assert type(outcome) is ModelResponse
        return outcome


async def build_plain_runtime(
    tmp_path: Path,
    provider: OutcomeProvider,
    scheduler: FakeScheduler,
    *,
    llm_runtime: LlmRuntime | None = None,
    store: InMemoryEventStore | None = None,
    policy: ModelRetryPolicy | None = None,
):
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "data",
            provider="scripted",
            model="model",
            max_steps=1,
            model_retry_policy=policy or retry_policy(),
        ),
        provider=provider,
        llm_runtime=llm_runtime or LlmRuntime(provider_clock=scheduler.monotonic),
        retry_scheduler=scheduler.scheduler(),
        event_store=store or InMemoryEventStore(),
    )
    await runtime.sessions.create_session(tmp_path, session_id="session-root")
    return runtime, AgentRuntimeExecution(runtime, "session-root")


async def test_transient_failure_then_success_reuses_one_frozen_request(
    tmp_path: Path,
) -> None:
    scheduler = FakeScheduler()
    provider = OutcomeProvider(
        scheduler,
        (
            ProviderFailure(
                "provider-dns-temporary",
                ProviderFailureCategory.DNS,
                usage=Usage(0, 0, UsageQuality.EXACT),
            ),
            ModelResponse(
                content="done",
                usage=Usage(3, 2, UsageQuality.EXACT),
            ),
        ),
    )
    runtime, execution = await build_plain_runtime(tmp_path, provider, scheduler)
    try:
        result = await execution.run_turn(TurnInput("perform the task", "message-root"))
        events = await runtime.sessions.read_session("session-root")
    finally:
        await execution.dispose()

    starts = [event for event in events if event.type == "model/attempt-start"]
    ends = [event for event in events if event.type == "model/attempt-end"]
    snapshots = [event for event in events if event.type == "request/snapshot"]
    assert result.final_text == "done"
    assert provider.calls == 2
    assert provider.requests[0] == provider.requests[1]
    assert len(starts) == len(ends) == 2
    assert len(snapshots) == 1
    assert [event.data["ordinal"] for event in starts] == [1, 2]
    assert starts[1].data["retry_failure_category"] == "dns"
    assert starts[1].data["retry_wait_milliseconds"] == 250
    assert ends[0].data["usage"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "quality": "exact",
    }
    assert [event.data["provider_active_milliseconds"] for event in ends] == [20, 20]
    assert scheduler.delays == [0.25]
    assert not await runtime.check_invariants("session-root")


async def test_invalid_attempt_history_cannot_acquire_a_retry_dispatch_permit(
    tmp_path: Path,
) -> None:
    scheduler = FakeScheduler()
    scheduler.sleep_release = asyncio.Event()
    store = InMemoryEventStore()
    provider = OutcomeProvider(
        scheduler,
        (
            ProviderFailure("provider-timeout", ProviderFailureCategory.TIMEOUT),
            ModelResponse(content="must-not-run"),
        ),
    )
    runtime, execution = await build_plain_runtime(
        tmp_path,
        provider,
        scheduler,
        store=store,
    )
    task = asyncio.create_task(
        execution.run_turn(TurnInput("perform the task", "message-root"))
    )
    try:
        await asyncio.wait_for(scheduler.sleep_entered.wait(), timeout=1)
        events = await store.read("session:session-root")
        first_end = next(event for event in events if event.type == "model/attempt-end")
        await store.append(
            "session:session-root",
            expected_seq=events[-1].seq,
            events=(
                PendingEvent(
                    type="model/attempt-end",
                    data=dict(first_end.data),
                    correlation_id=first_end.correlation_id,
                    composition_revision=first_end.composition_revision,
                ),
            ),
            durability=Durability.SYNC,
        )
        scheduler.sleep_release.set()

        with pytest.raises(ModelAttemptConflictError):
            await asyncio.wait_for(task, timeout=1)
        events = await store.read("session:session-root")
    finally:
        scheduler.sleep_release.set()
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await execution.dispose()

    assert provider.calls == 1
    assert sum(event.type == "model/attempt-start" for event in events) == 1
    assert sum(event.type == "model/attempt-end" for event in events) == 2
    violations = await runtime.check_invariants("session-root")
    assert any(item.name == "single-attempt-end" for item in violations)


@pytest.mark.parametrize(
    "failure",
    (
        ProviderFailure(
            "provider-http-authentication",
            ProviderFailureCategory.AUTHENTICATION,
        ),
        ProviderFailure(
            "provider-response-invalid",
            ProviderFailureCategory.PROTOCOL,
        ),
        ProviderFailure(
            "provider-transport-unknown",
            ProviderFailureCategory.UNKNOWN,
        ),
        RuntimeError("ordinary-host-error"),
    ),
)
async def test_non_retryable_and_unknown_failures_stop_after_one_attempt(
    tmp_path: Path,
    failure: BaseException,
) -> None:
    scheduler = FakeScheduler()
    provider = OutcomeProvider(scheduler, (failure, ModelResponse(content="must-not-run")))
    runtime, execution = await build_plain_runtime(tmp_path, provider, scheduler)
    try:
        expected = type(failure) if type(failure) is ProviderFailure else ProviderFailure
        with pytest.raises(expected):
            await execution.run_turn(TurnInput("perform the task", "message-root"))
        events = await runtime.sessions.read_session("session-root")
    finally:
        await execution.dispose()

    assert provider.calls == 1
    assert scheduler.delays == []
    assert sum(event.type == "model/attempt-start" for event in events) == 1


async def test_cancellation_during_retry_delay_never_admits_a_next_attempt(
    tmp_path: Path,
) -> None:
    scheduler = FakeScheduler()
    scheduler.sleep_release = asyncio.Event()
    provider = OutcomeProvider(
        scheduler,
        (
            ProviderFailure("provider-timeout", ProviderFailureCategory.TIMEOUT),
            ModelResponse(content="must-not-run"),
        ),
    )
    runtime, execution = await build_plain_runtime(tmp_path, provider, scheduler)
    task = asyncio.create_task(
        execution.run_turn(TurnInput("perform the task", "message-root"))
    )
    try:
        await scheduler.sleep_entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        events = await runtime.sessions.read_session("session-root")
    finally:
        scheduler.sleep_release.set()
        await execution.dispose()

    assert provider.calls == 1
    assert sum(event.type == "model/attempt-start" for event in events) == 1
    assert sum(event.type == "model/attempt-end" for event in events) == 1


class EventGateStore(InMemoryEventStore):
    def __init__(self, event_type: str) -> None:
        super().__init__()
        self.event_type = event_type
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.used = False

    async def append(
        self,
        stream_id,
        *,
        expected_seq,
        events,
        durability=Durability.SYNC,
    ):
        if not self.used and any(event.type == self.event_type for event in events):
            self.used = True
            self.entered.set()
            await self.release.wait()
        return await super().append(
            stream_id,
            expected_seq=expected_seq,
            events=events,
            durability=durability,
        )


class CommitThenWaitStore(InMemoryEventStore):
    def __init__(self, event_type: str) -> None:
        super().__init__()
        self.event_type = event_type
        self.committed = asyncio.Event()
        self.release = asyncio.Event()
        self.used = False

    async def append(
        self,
        stream_id,
        *,
        expected_seq,
        events,
        durability=Durability.SYNC,
    ):
        appended = await super().append(
            stream_id,
            expected_seq=expected_seq,
            events=events,
            durability=durability,
        )
        if not self.used and any(event.type == self.event_type for event in events):
            self.used = True
            self.committed.set()
            await self.release.wait()
        return appended


async def test_cancellation_during_attempt_end_append_never_retries(
    tmp_path: Path,
) -> None:
    scheduler = FakeScheduler()
    store = CommitThenWaitStore("model/attempt-end")
    provider = OutcomeProvider(
        scheduler,
        (
            ProviderFailure("provider-timeout", ProviderFailureCategory.TIMEOUT),
            ModelResponse(content="must-not-run"),
        ),
    )
    runtime, execution = await build_plain_runtime(
        tmp_path,
        provider,
        scheduler,
        store=store,
    )
    task = asyncio.create_task(
        execution.run_turn(TurnInput("perform the task", "message-root"))
    )
    try:
        await store.committed.wait()
        task.cancel()
        store.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        events = await runtime.sessions.read_session("session-root")
    finally:
        store.release.set()
        await execution.dispose()

    assert provider.calls == 1
    ends = [event for event in events if event.type == "model/attempt-end"]
    assert len(ends) == 1
    assert ends[0].data["status"] == "failed"


async def test_cancellation_during_provider_call_never_retries(tmp_path: Path) -> None:
    scheduler = FakeScheduler()

    class GatedProvider(OutcomeProvider):
        def __init__(self) -> None:
            super().__init__(scheduler, ())
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def complete(self, request: ModelRequest) -> ModelResponse:
            self.calls += 1
            self.requests.append(request)
            self.entered.set()
            await self.release.wait()
            return ModelResponse(content="must-not-return")

    provider = GatedProvider()
    runtime, execution = await build_plain_runtime(tmp_path, provider, scheduler)
    task = asyncio.create_task(
        execution.run_turn(TurnInput("perform the task", "message-root"))
    )
    try:
        await provider.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        events = await runtime.sessions.read_session("session-root")
    finally:
        provider.release.set()
        await execution.dispose()

    assert provider.calls == 1
    ends = [event for event in events if event.type == "model/attempt-end"]
    assert len(ends) == 1
    assert ends[0].data["status"] == "cancelled"


async def test_attempt_limit_stops_before_a_third_provider_call(tmp_path: Path) -> None:
    scheduler = FakeScheduler()
    provider = OutcomeProvider(
        scheduler,
        (
            ProviderFailure("provider-timeout", ProviderFailureCategory.TIMEOUT),
            ProviderFailure("provider-timeout", ProviderFailureCategory.TIMEOUT),
            ModelResponse(content="must-not-run"),
        ),
    )
    runtime, execution = await build_plain_runtime(tmp_path, provider, scheduler)
    try:
        with pytest.raises(ProviderFailure, match="provider-timeout"):
            await execution.run_turn(TurnInput("perform the task", "message-root"))
        events = await runtime.sessions.read_session("session-root")
    finally:
        await execution.dispose()

    assert provider.calls == 2
    assert sum(event.type == "model/attempt-start" for event in events) == 2


async def test_retry_after_is_capped_by_the_host_policy(tmp_path: Path) -> None:
    scheduler = FakeScheduler()
    provider = OutcomeProvider(
        scheduler,
        (
            ProviderFailure(
                "provider-http-rate-limited",
                ProviderFailureCategory.RATE_LIMITED,
                retry_after_seconds=60.0,
                usage=Usage(0, 0, UsageQuality.EXACT),
            ),
            ModelResponse(content="done"),
        ),
    )
    runtime, execution = await build_plain_runtime(
        tmp_path,
        provider,
        scheduler,
        policy=ModelRetryPolicy(
            max_attempts=2,
            max_elapsed_seconds=10.0,
            base_delay_seconds=0.25,
            max_delay_seconds=2.0,
            retry_after_cap_seconds=1.0,
            jitter_ratio=0.0,
        ),
    )
    try:
        result = await execution.run_turn(TurnInput("perform the task", "message-root"))
    finally:
        await execution.dispose()

    assert result.final_text == "done"
    assert provider.calls == 2
    assert scheduler.delays == [1.0]


async def test_elapsed_bound_refuses_retry_before_sleep(tmp_path: Path) -> None:
    scheduler = FakeScheduler()
    provider = OutcomeProvider(
        scheduler,
        (
            ProviderFailure("provider-timeout", ProviderFailureCategory.TIMEOUT),
            ModelResponse(content="must-not-run"),
        ),
    )
    runtime, execution = await build_plain_runtime(
        tmp_path,
        provider,
        scheduler,
        policy=ModelRetryPolicy(
            max_attempts=2,
            max_elapsed_seconds=0.03,
            base_delay_seconds=0.25,
            max_delay_seconds=2.0,
            retry_after_cap_seconds=1.0,
            jitter_ratio=0.0,
        ),
    )
    try:
        with pytest.raises(ProviderFailure, match="provider-timeout"):
            await execution.run_turn(TurnInput("perform the task", "message-root"))
    finally:
        await execution.dispose()

    assert provider.calls == 1
    assert scheduler.delays == []


async def test_permanent_failures_cannot_be_added_to_retry_policy() -> None:
    with pytest.raises(ValueError, match="cannot include permanent failures"):
        ModelRetryPolicy(
            max_attempts=2,
            max_elapsed_seconds=10.0,
            base_delay_seconds=0.25,
            max_delay_seconds=2.0,
            retry_after_cap_seconds=1.0,
            jitter_ratio=0.0,
            retryable_categories=frozenset(
                {ProviderFailureCategory.AUTHENTICATION}
            ),
        )


async def test_a_large_valid_ordinal_still_produces_a_finite_bounded_delay() -> None:
    policy = ModelRetryPolicy(
        max_attempts=2_000,
        max_elapsed_seconds=10.0,
        base_delay_seconds=0.25,
        max_delay_seconds=2.0,
        retry_after_cap_seconds=1.0,
        jitter_ratio=1.0,
    )
    failure = ProviderFailure("provider-dns-failed", ProviderFailureCategory.DNS)

    upper = policy.decide(
        failure,
        completed_ordinal=1_500,
        elapsed_seconds=0.0,
        entropy=1.0,
    )
    lower = policy.decide(
        failure,
        completed_ordinal=1_500,
        elapsed_seconds=0.0,
        entropy=0.0,
    )

    assert upper is not None and upper.delay_seconds == 2.0
    assert lower is not None and lower.delay_seconds == 0.0


async def test_later_admission_cannot_drift_the_frozen_request(tmp_path: Path) -> None:
    scheduler = FakeScheduler()
    provider = OutcomeProvider(
        scheduler,
        (
            ProviderFailure("provider-timeout", ProviderFailureCategory.TIMEOUT),
            ModelResponse(content="must-not-run"),
        ),
    )

    class DriftingRuntime(LlmRuntime):
        def __init__(self) -> None:
            super().__init__(provider_clock=scheduler.monotonic)
            self.admissions = 0

        async def admit(self, provider, request, *, attempt):
            self.admissions += 1
            if self.admissions == 2:
                request = replace(request, max_output_tokens=1)
            return LlmAdmission(
                provider,
                request,
                attempt=attempt,
                provider_clock=scheduler.monotonic,
            )

    runtime, execution = await build_plain_runtime(
        tmp_path,
        provider,
        scheduler,
        llm_runtime=DriftingRuntime(),
    )
    try:
        with pytest.raises(ModelRetryRequestDriftError, match="model-retry-request-drift"):
            await execution.run_turn(TurnInput("perform the task", "message-root"))
        events = await runtime.sessions.read_session("session-root")
    finally:
        await execution.dispose()

    assert provider.calls == 1
    assert sum(event.type == "model/attempt-start" for event in events) == 1


class FixedTokenCounter:
    def count_request(self, request: ModelRequest) -> int:
        del request
        return 2


async def build_budgeted_execution(
    tmp_path: Path,
    scheduler: FakeScheduler,
    store: InMemoryEventStore,
    provider: OutcomeProvider,
    *,
    max_tokens: int = 20,
):
    await AgentRegistrar(store).create_agent(
        AgentSpec(preset="managed", workspace_id="workspace"),
        request_id="create-root",
        agent_id="agent-root",
        session_id="session-root",
    )
    budgets = BudgetLedgerService(store)
    await budgets.grant_root(
        operation_id="grant-root",
        agent_id="agent-root",
        limits=BudgetLimits(
            max_tokens=max_tokens,
            max_steps=2,
            max_tool_calls=2,
            max_wall_milliseconds=None,
            max_children=1,
            max_depth=1,
            max_processes=1,
        ),
    )
    enforcement = BudgetEnforcement(
        budgets,
        agent_id="agent-root",
        session_id="session-root",
        continuation=DefaultContinuationRuntime(),
        llm_runtime=LlmRuntime(provider_clock=scheduler.monotonic),
        token_counter=FixedTokenCounter(),
    )
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "data",
            provider="scripted",
            model="model",
            max_steps=1,
            max_output_tokens=5,
            model_retry_policy=retry_policy(),
        ),
        provider=provider,
        event_store=store,
        continuation=enforcement.continuation,
        llm_runtime=enforcement.llm_runtime,
        tool_admission_gate=enforcement.tool_admission_gate,
        retry_scheduler=scheduler.scheduler(),
    )
    await runtime.sessions.create_session(tmp_path, session_id="session-root")
    execution = enforcement.wrap(AgentRuntimeExecution(runtime, "session-root"))
    return runtime, execution, budgets


async def test_cancellation_after_reservation_commit_never_dispatches(
    tmp_path: Path,
) -> None:
    scheduler = FakeScheduler()
    store = CommitThenWaitStore("budget/usage-reserved")
    provider = OutcomeProvider(scheduler, (ModelResponse(content="must-not-run"),))
    runtime, execution, budgets = await build_budgeted_execution(
        tmp_path, scheduler, store, provider
    )
    task = asyncio.create_task(
        execution.run_turn(TurnInput("perform the task", "message-root"))
    )
    try:
        await store.committed.wait()
        task.cancel()
        store.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        events = await runtime.sessions.read_session("session-root")
        ledger = await budgets.ledger()
    finally:
        store.release.set()
        await execution.dispose()

    assert provider.calls == 0
    assert not any(event.type == "model/attempt-start" for event in events)
    assert len(ledger.usage_reservations) == 1
    assert (
        ledger.usage_reservations[0].status
        is BudgetUsageReservationStatus.RELEASED
    )


async def test_cancellation_after_attempt_start_commit_never_dispatches(
    tmp_path: Path,
) -> None:
    scheduler = FakeScheduler()
    store = CommitThenWaitStore("request/snapshot")
    provider = OutcomeProvider(scheduler, (ModelResponse(content="must-not-run"),))
    runtime, execution, budgets = await build_budgeted_execution(
        tmp_path, scheduler, store, provider
    )
    task = asyncio.create_task(
        execution.run_turn(TurnInput("perform the task", "message-root"))
    )
    try:
        await store.committed.wait()
        task.cancel()
        store.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        events = await runtime.sessions.read_session("session-root")
        ledger = await budgets.ledger()
    finally:
        store.release.set()
        await execution.dispose()

    assert provider.calls == 0
    assert sum(event.type == "model/attempt-start" for event in events) == 1
    ends = [event for event in events if event.type == "model/attempt-end"]
    assert len(ends) == 1
    assert ends[0].data["status"] == "cancelled"
    assert len(ledger.usage_reservations) == 1
    assert (
        ledger.usage_reservations[0].status
        is BudgetUsageReservationStatus.RELEASED
    )


async def test_each_retry_has_independent_budget_settlement(tmp_path: Path) -> None:
    scheduler = FakeScheduler()
    store = InMemoryEventStore()
    await AgentRegistrar(store).create_agent(
        AgentSpec(preset="managed", workspace_id="workspace"),
        request_id="create-root",
        agent_id="agent-root",
        session_id="session-root",
    )
    budgets = BudgetLedgerService(store)
    await budgets.grant_root(
        operation_id="grant-root",
        agent_id="agent-root",
        limits=BudgetLimits(
            max_tokens=20,
            max_steps=2,
            max_tool_calls=2,
            max_wall_milliseconds=None,
            max_children=1,
            max_depth=1,
            max_processes=1,
        ),
    )
    provider = OutcomeProvider(
        scheduler,
        (
            ProviderFailure(
                "provider-dns-temporary",
                ProviderFailureCategory.DNS,
                usage=Usage(1, 0, UsageQuality.EXACT),
            ),
            ModelResponse(
                content="done",
                usage=Usage(2, 1, UsageQuality.EXACT),
            ),
        ),
    )
    enforcement = BudgetEnforcement(
        budgets,
        agent_id="agent-root",
        session_id="session-root",
        continuation=DefaultContinuationRuntime(),
        llm_runtime=LlmRuntime(provider_clock=scheduler.monotonic),
        token_counter=FixedTokenCounter(),
    )
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "data",
            provider="scripted",
            model="model",
            max_steps=1,
            max_output_tokens=5,
            model_retry_policy=retry_policy(),
        ),
        provider=provider,
        event_store=store,
        continuation=enforcement.continuation,
        llm_runtime=enforcement.llm_runtime,
        tool_admission_gate=enforcement.tool_admission_gate,
        retry_scheduler=scheduler.scheduler(),
    )
    await runtime.sessions.create_session(tmp_path, session_id="session-root")
    execution = enforcement.wrap(AgentRuntimeExecution(runtime, "session-root"))
    try:
        result = await execution.run_turn(TurnInput("perform the task", "message-root"))
        ledger = await budgets.ledger()
    finally:
        await execution.dispose()

    assert result.final_text == "done"
    assert provider.calls == 2
    assert provider.requests[0] == provider.requests[1]
    assert len(ledger.usage_reservations) == 2
    assert all(
        item.status is BudgetUsageReservationStatus.SETTLED
        for item in ledger.usage_reservations
    )
    assert [item.settled_amounts.tokens for item in ledger.usage_reservations] == [1, 3]


async def test_budget_runtime_reserves_every_retry_ordinal_directly(
    tmp_path: Path,
) -> None:
    scheduler = FakeScheduler()
    store = InMemoryEventStore()
    await AgentRegistrar(store).create_agent(
        AgentSpec(preset="managed", workspace_id="workspace"),
        request_id="create-root",
        agent_id="agent-root",
        session_id="session-root",
    )
    budgets = BudgetLedgerService(store)
    await budgets.grant_root(
        operation_id="grant-root",
        agent_id="agent-root",
        limits=BudgetLimits(
            max_tokens=20,
            max_steps=2,
            max_tool_calls=2,
            max_wall_milliseconds=None,
            max_children=1,
            max_depth=1,
            max_processes=1,
        ),
    )
    provider = OutcomeProvider(
        scheduler,
        (
            ProviderFailure(
                "provider-dns-temporary",
                ProviderFailureCategory.DNS,
                usage=Usage(1, 0, UsageQuality.EXACT),
            ),
            ModelResponse(
                content="done",
                usage=Usage(2, 1, UsageQuality.EXACT),
            ),
        ),
    )
    runtime = BudgetedLlmRuntime(
        budgets,
        agent_id="agent-root",
        session_id="session-root",
        inner=LlmRuntime(provider_clock=scheduler.monotonic),
        token_counter=FixedTokenCounter(),
    )
    request = ModelRequest(
        provider="scripted",
        model="model",
        messages=(),
        max_output_tokens=5,
        metadata={
            "session_id": "session-root",
            "turn_id": "turn-root",
            "step_id": "step-root",
        },
    )
    first = await runtime.admit(
        provider,
        request,
        attempt=ModelAttemptIdentity(
            "session-root", "turn-root", "step-root", "attempt-1", 1
        ),
    )
    with pytest.raises(ProviderFailure):
        await first.dispatch(provider=first.provider, request=first.request)
    second = await runtime.admit(
        provider,
        request,
        attempt=ModelAttemptIdentity(
            "session-root", "turn-root", "step-root", "attempt-2", 2
        ),
    )
    response = await second.dispatch(provider=second.provider, request=second.request)
    ledger = await budgets.ledger()

    assert response.content == "done"
    assert provider.calls == 2
    assert len(ledger.usage_reservations) == 2
    assert [item.settled_amounts.tokens for item in ledger.usage_reservations] == [1, 3]


async def test_cancellation_during_failed_attempt_settlement_never_retries(
    tmp_path: Path,
) -> None:
    scheduler = FakeScheduler()
    store = EventGateStore("budget/usage-settled")
    await AgentRegistrar(store).create_agent(
        AgentSpec(preset="managed", workspace_id="workspace"),
        request_id="create-root",
        agent_id="agent-root",
        session_id="session-root",
    )
    budgets = BudgetLedgerService(store)
    await budgets.grant_root(
        operation_id="grant-root",
        agent_id="agent-root",
        limits=BudgetLimits(
            max_tokens=20,
            max_steps=2,
            max_tool_calls=2,
            max_wall_milliseconds=None,
            max_children=1,
            max_depth=1,
            max_processes=1,
        ),
    )
    provider = OutcomeProvider(
        scheduler,
        (
            ProviderFailure("provider-timeout", ProviderFailureCategory.TIMEOUT),
            ModelResponse(content="must-not-run"),
        ),
    )
    enforcement = BudgetEnforcement(
        budgets,
        agent_id="agent-root",
        session_id="session-root",
        continuation=DefaultContinuationRuntime(),
        llm_runtime=LlmRuntime(provider_clock=scheduler.monotonic),
        token_counter=FixedTokenCounter(),
    )
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "data",
            provider="scripted",
            model="model",
            max_steps=1,
            max_output_tokens=5,
            model_retry_policy=retry_policy(),
        ),
        provider=provider,
        event_store=store,
        continuation=enforcement.continuation,
        llm_runtime=enforcement.llm_runtime,
        tool_admission_gate=enforcement.tool_admission_gate,
        retry_scheduler=scheduler.scheduler(),
    )
    await runtime.sessions.create_session(tmp_path, session_id="session-root")
    execution = enforcement.wrap(AgentRuntimeExecution(runtime, "session-root"))
    task = asyncio.create_task(
        execution.run_turn(TurnInput("perform the task", "message-root"))
    )
    try:
        await store.entered.wait()
        task.cancel()
        store.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        events = await runtime.sessions.read_session("session-root")
        ledger = await budgets.ledger()
    finally:
        store.release.set()
        await execution.dispose()

    assert provider.calls == 1
    assert sum(event.type == "model/attempt-start" for event in events) == 1
    ends = [event for event in events if event.type == "model/attempt-end"]
    assert len(ends) == 1
    assert ends[0].data["status"] == "cancelled"
    assert len(ledger.usage_reservations) == 1
    assert (
        ledger.usage_reservations[0].status
        is BudgetUsageReservationStatus.SETTLED
    )


async def test_unknown_failure_usage_exhausts_budget_without_shrinking_retry(
    tmp_path: Path,
) -> None:
    scheduler = FakeScheduler()
    store = InMemoryEventStore()
    await AgentRegistrar(store).create_agent(
        AgentSpec(preset="managed", workspace_id="workspace"),
        request_id="create-root",
        agent_id="agent-root",
        session_id="session-root",
    )
    budgets = BudgetLedgerService(store)
    await budgets.grant_root(
        operation_id="grant-root",
        agent_id="agent-root",
        limits=BudgetLimits(
            max_tokens=8,
            max_steps=2,
            max_tool_calls=2,
            max_wall_milliseconds=None,
            max_children=1,
            max_depth=1,
            max_processes=1,
        ),
    )
    provider = OutcomeProvider(
        scheduler,
        (
            ProviderFailure("provider-timeout", ProviderFailureCategory.TIMEOUT),
            ModelResponse(content="must-not-run"),
        ),
    )
    enforcement = BudgetEnforcement(
        budgets,
        agent_id="agent-root",
        session_id="session-root",
        continuation=DefaultContinuationRuntime(),
        llm_runtime=LlmRuntime(provider_clock=scheduler.monotonic),
        token_counter=FixedTokenCounter(),
    )
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "data",
            provider="scripted",
            model="model",
            max_steps=1,
            max_output_tokens=5,
            model_retry_policy=retry_policy(),
        ),
        provider=provider,
        event_store=store,
        continuation=enforcement.continuation,
        llm_runtime=enforcement.llm_runtime,
        tool_admission_gate=enforcement.tool_admission_gate,
        retry_scheduler=scheduler.scheduler(),
    )
    await runtime.sessions.create_session(tmp_path, session_id="session-root")
    execution = enforcement.wrap(AgentRuntimeExecution(runtime, "session-root"))
    try:
        with pytest.raises(BudgetExhaustedError):
            await execution.run_turn(TurnInput("perform the task", "message-root"))
        ledger = await budgets.ledger()
    finally:
        await execution.dispose()

    assert provider.calls == 1
    assert len(ledger.usage_reservations) == 1
    assert ledger.usage_reservations[0].settled_amounts.tokens == 7
