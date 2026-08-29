"""v0.7-B contracts for Budget enforcement at existing owned boundaries."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import pytest

from traceh.agents import AgentRegistrar
from traceh.api.agents import AgentSpec
from traceh.api.budgets import (
    BudgetLimits,
    BudgetUsageReservationStatus,
)
from traceh.api.llm import (
    ModelAttemptIdentity,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCall,
    Usage,
    UsageQuality,
)
from traceh.api.tools import (
    EffectKind,
    PreparedToolCall,
    ToolAdmissionDecision,
    ToolExecutionContext,
    ToolOutput,
)
from traceh.api.turns import TurnInput
from traceh.budgets import (
    BudgetDirectoryMismatchError,
    BudgetedLlmRuntime,
    BudgetEnforcement,
    BudgetExhaustedError,
    BudgetInputError,
    BudgetLedgerService,
    BudgetToolAdmissionGate,
)
from traceh.budgets.events import BUDGET_USAGE_STARTED
from traceh.llm.runtime import LlmAdmission, LlmRuntime
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.runtime.continuation import DefaultContinuationRuntime
from traceh.session.event_store import Durability, InMemoryEventStore
from traceh.session.service import SessionService
from traceh.supervision import AgentRuntimeExecution
from traceh.tools.policy import AllowByDefaultPolicy
from traceh.tools.registry import ToolRegistry
from traceh.tools.runtime import ToolRuntime

pytestmark = pytest.mark.asyncio


def limits(**overrides: int | None) -> BudgetLimits:
    values: dict[str, int | None] = {
        "max_tokens": 20,
        "max_steps": 4,
        "max_tool_calls": 4,
        "max_wall_milliseconds": 10_000,
        "max_children": 2,
        "max_depth": 2,
        "max_processes": 2,
    }
    values.update(overrides)
    return BudgetLimits(**values)


async def root_context(
    *,
    root_limits: BudgetLimits | None = None,
    store: InMemoryEventStore | None = None,
) -> tuple[InMemoryEventStore, BudgetLedgerService]:
    if store is None:
        store = InMemoryEventStore()
    sessions = SessionService(store)
    await sessions.create_session(Path.cwd(), session_id="session-root")
    await AgentRegistrar(store).create_agent(
        AgentSpec(preset="managed", workspace_id="workspace"),
        request_id="create-root",
        agent_id="agent-root",
        session_id="session-root",
    )
    service = BudgetLedgerService(store)
    await service.grant_root(
        operation_id="grant-root",
        agent_id="agent-root",
        limits=root_limits or limits(),
    )
    return store, service


class CommitThenWaitStore(InMemoryEventStore):
    """Commit one selected fact, then hold its append return behind a Gate."""

    def __init__(self, event_type: str) -> None:
        super().__init__()
        self.event_type = event_type
        self.committed = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()

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
        if any(event.type == self.event_type for event in events):
            self.committed.set()
            try:
                await self.release.wait()
            finally:
                self.finished.set()
        return appended


@dataclass(slots=True)
class RecordingTool:
    name: str
    entered: list[str]
    fail: bool = False
    effect_kind: EffectKind = EffectKind.PURE_READ
    description: str = "record one admitted call"
    input_schema: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
    )

    async def execute(self, arguments, context) -> ToolOutput:
        del arguments
        self.entered.append(context.tool_call_id)
        if self.fail:
            raise RuntimeError("tool failed")
        return ToolOutput(context.tool_call_id)


def request(step_id: str, *, max_output_tokens: int | None = None) -> ModelRequest:
    return ModelRequest(
        provider="scripted",
        model="model",
        messages=(ModelMessage(role="user", content="work"),),
        max_output_tokens=max_output_tokens,
        metadata={
            "session_id": "session-root",
            "turn_id": "turn-root",
            "step_id": step_id,
        },
    )


def attempt_for(model_request: ModelRequest, *, ordinal: int = 1) -> ModelAttemptIdentity:
    metadata = model_request.metadata
    session_id = str(metadata["session_id"])
    turn_id = str(metadata["turn_id"])
    step_id = str(metadata["step_id"])
    return ModelAttemptIdentity(
        session_id=session_id,
        turn_id=turn_id,
        step_id=step_id,
        attempt_id=str(uuid4()),
        ordinal=ordinal,
    )


async def invoke(
    runtime: LlmRuntime,
    provider,
    model_request: ModelRequest,
) -> ModelResponse:
    admission = await runtime.admit(
        provider,
        model_request,
        attempt=attempt_for(model_request),
    )
    return await admission.dispatch(
        provider=admission.provider,
        request=admission.request,
    )


class CountingProvider:
    name = "scripted"

    def __init__(self, response: ModelResponse | None = None) -> None:
        self.response = response or ModelResponse(content="ok")
        self.calls = 0
        self.requests: list[ModelRequest] = []

    async def complete(self, model_request: ModelRequest) -> ModelResponse:
        self.calls += 1
        self.requests.append(model_request)
        return self.response


class FixedTokenCounter:
    def __init__(self, count: int) -> None:
        self.count = count

    def count_request(self, model_request: ModelRequest) -> int:
        del model_request
        return self.count


async def test_token_reservation_settles_exact_usage_and_releases_unused_capacity() -> None:
    _, service = await root_context(root_limits=limits(max_tokens=10))
    provider = CountingProvider(
        ModelResponse(
            content="ok",
            usage=Usage(2, 1, UsageQuality.EXACT),
        )
    )
    runtime = BudgetedLlmRuntime(
        service,
        agent_id="agent-root",
        session_id="session-root",
    )

    response = await invoke(runtime, provider, request("step-one"))

    assert response.content == "ok"
    account = (await service.ledger()).account("agent-root")
    assert account is not None
    assert account.charged.tokens == 3
    assert account.reserved.tokens == 0
    assert (await service.ledger()).available("agent-root").max_tokens == 7
    reservation = (await service.ledger()).usage_reservations[0]
    assert reservation.status is BudgetUsageReservationStatus.SETTLED
    assert reservation.usage_quality is UsageQuality.EXACT


@pytest.mark.parametrize(
    ("quality", "allow_estimated", "expected"),
    [
        (UsageQuality.UNKNOWN, False, 10),
        (UsageQuality.ESTIMATED, False, 10),
        (UsageQuality.ESTIMATED, True, 3),
    ],
)
async def test_unknown_or_untrusted_usage_is_conservative(
    quality: UsageQuality,
    allow_estimated: bool,
    expected: int,
) -> None:
    _, service = await root_context(root_limits=limits(max_tokens=10))
    provider = CountingProvider(
        ModelResponse(content="ok", usage=Usage(2, 1, quality))
    )
    runtime = BudgetedLlmRuntime(
        service,
        agent_id="agent-root",
        session_id="session-root",
        allow_estimated=allow_estimated,
    )

    await invoke(runtime, provider, request("step-quality"))

    account = (await service.ledger()).account("agent-root")
    assert account is not None
    assert account.charged.tokens == expected


async def test_token_counter_caps_output_without_using_character_count() -> None:
    _, service = await root_context(root_limits=limits(max_tokens=10))
    provider = CountingProvider(
        ModelResponse(content="ok", usage=Usage(4, 6, UsageQuality.EXACT))
    )
    runtime = BudgetedLlmRuntime(
        service,
        agent_id="agent-root",
        session_id="session-root",
        token_counter=FixedTokenCounter(4),
    )

    await invoke(runtime, provider, request("step-counter", max_output_tokens=50))

    assert provider.requests[0].max_output_tokens == 6


async def test_without_tokenizer_output_is_capped_and_overage_is_unknown() -> None:
    _, service = await root_context(root_limits=limits(max_tokens=10))
    provider = CountingProvider(
        ModelResponse(content="ok", usage=Usage(8, 5, UsageQuality.EXACT))
    )
    runtime = BudgetedLlmRuntime(
        service,
        agent_id="agent-root",
        session_id="session-root",
    )

    await invoke(runtime, provider, request("step-overage", max_output_tokens=50))

    assert provider.requests[0].max_output_tokens == 10
    ledger = await service.ledger()
    account = ledger.account("agent-root")
    assert account is not None
    assert account.charged.tokens == 10
    assert ledger.usage_reservations[0].usage_quality is UsageQuality.UNKNOWN


async def test_competing_admissions_hold_independent_pending_reservations() -> None:
    _, service = await root_context(root_limits=limits(max_tokens=20))
    provider = CountingProvider(
        ModelResponse(content="ok", usage=Usage(2, 1, UsageQuality.EXACT))
    )
    runtime = BudgetedLlmRuntime(
        service,
        agent_id="agent-root",
        session_id="session-root",
        token_counter=FixedTokenCounter(5),
    )
    model_request = request("step-shared", max_output_tokens=5)
    first = await runtime.admit(
        provider,
        model_request,
        attempt=attempt_for(model_request),
    )
    second = await runtime.admit(
        provider,
        model_request,
        attempt=attempt_for(model_request),
    )

    assert first.reservation_id != second.reservation_id
    assert provider.calls == 0
    assert [
        item.status for item in (await service.ledger()).usage_reservations
    ] == [BudgetUsageReservationStatus.PENDING, BudgetUsageReservationStatus.PENDING]

    await second.abort()
    await first.dispatch(provider=first.provider, request=first.request)
    assert provider.calls == 1
    assert [
        item.status for item in (await service.ledger()).usage_reservations
    ] == [BudgetUsageReservationStatus.SETTLED, BudgetUsageReservationStatus.RELEASED]


async def test_provider_failure_consumes_the_whole_token_reservation() -> None:
    _, service = await root_context(root_limits=limits(max_tokens=9))

    class FailingProvider:
        name = "scripted"

        async def complete(self, model_request):
            del model_request
            raise RuntimeError("provider failed")

    runtime = BudgetedLlmRuntime(
        service,
        agent_id="agent-root",
        session_id="session-root",
    )
    with pytest.raises(RuntimeError, match="provider failed"):
        await invoke(runtime, FailingProvider(), request("step-failed"))

    account = (await service.ledger()).account("agent-root")
    assert account is not None
    assert account.charged.tokens == 9
    assert account.reserved.tokens == 0


async def test_provider_cancellation_settles_once_before_propagation() -> None:
    _, service = await root_context(root_limits=limits(max_tokens=9))
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class BlockingProvider:
        name = "scripted"

        async def complete(self, model_request: ModelRequest) -> ModelResponse:
            del model_request
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    runtime = BudgetedLlmRuntime(
        service,
        agent_id="agent-root",
        session_id="session-root",
    )
    invocation = asyncio.create_task(
        invoke(runtime, BlockingProvider(), request("step-cancelled"))
    )
    await entered.wait()
    invocation.cancel()

    with pytest.raises(asyncio.CancelledError):
        await invocation
    assert cancelled.is_set()
    ledger = await service.ledger()
    account = ledger.account("agent-root")
    assert account is not None
    assert account.charged.tokens == 9
    assert account.reserved.tokens == 0
    assert len(ledger.usage_reservations) == 1
    assert ledger.usage_reservations[0].status is BudgetUsageReservationStatus.SETTLED


async def test_cancel_after_token_start_commit_settles_without_calling_provider() -> None:
    store = CommitThenWaitStore(BUDGET_USAGE_STARTED)
    _, service = await root_context(
        root_limits=limits(max_tokens=10),
        store=store,
    )
    provider = CountingProvider()
    runtime = BudgetedLlmRuntime(
        service,
        agent_id="agent-root",
        session_id="session-root",
    )
    invocation = asyncio.create_task(
        invoke(runtime, provider, request("step-start-cancelled"))
    )
    await store.committed.wait()

    invocation.cancel()
    await asyncio.sleep(0)
    invocation.cancel()
    await asyncio.sleep(0)
    store.release.set()

    with pytest.raises(asyncio.CancelledError):
        await invocation
    assert store.finished.is_set()
    assert provider.calls == 0
    ledger = await service.ledger()
    account = ledger.account("agent-root")
    assert account is not None
    assert account.charged.tokens == 10
    assert account.reserved.tokens == 0
    assert len(ledger.usage_reservations) == 1
    assert ledger.usage_reservations[0].status is BudgetUsageReservationStatus.SETTLED
    assert ledger.usage_reservations[0].usage_quality is UsageQuality.UNKNOWN


async def test_falsey_injected_llm_runtime_remains_the_wrapped_mainline() -> None:
    _, service = await root_context(root_limits=limits(max_tokens=10))

    class FalseyRuntime(LlmRuntime):
        def __init__(self) -> None:
            self.calls = 0

        def __bool__(self) -> bool:
            return False

        async def admit(
            self,
            provider,
            model_request,
            *,
            attempt,
        ) -> LlmAdmission:
            self.calls += 1
            return LlmAdmission(provider, model_request, attempt=attempt)

    inner = FalseyRuntime()
    provider = CountingProvider(
        ModelResponse(
            content="wrapped",
            usage=Usage(1, 1, UsageQuality.EXACT),
        )
    )
    runtime = BudgetedLlmRuntime(
        service,
        agent_id="agent-root",
        session_id="session-root",
        inner=inner,
    )

    response = await invoke(runtime, provider, request("step-falsey-runtime"))

    assert response.content == "wrapped"
    assert inner.calls == 1
    assert provider.calls == 1


@pytest.mark.parametrize("invalid", ["false", 1, None])
async def test_allow_estimated_requires_an_exact_boolean(invalid: object) -> None:
    _, service = await root_context(root_limits=limits(max_tokens=10))

    with pytest.raises(BudgetInputError) as error:
        BudgetedLlmRuntime(
            service,
            agent_id="agent-root",
            session_id="session-root",
            allow_estimated=invalid,  # type: ignore[arg-type]
        )

    assert error.value.code == "budget-estimated-usage-policy-invalid"
    assert error.value.field == "allow_estimated"
    assert (await service.ledger()).usage_reservations == ()


async def test_tool_admission_is_deterministic_and_only_counts_prepared_calls(
    tmp_path: Path,
) -> None:
    store, service = await root_context(root_limits=limits(max_tool_calls=2))
    sessions = SessionService(store)
    registry = ToolRegistry()
    entered: list[str] = []
    registry.register(RecordingTool("read", entered))
    gate = BudgetToolAdmissionGate(
        service,
        agent_id="agent-root",
        session_id="session-root",
    )
    runtime = ToolRuntime(
        registry,
        sessions,
        policies=(AllowByDefaultPolicy(),),
        admission_gate=gate,
    )
    context = ToolExecutionContext(
        "session-root", "turn-tools", "step-tools", "batch", tmp_path, tmp_path
    )
    calls = (
        ToolCall("unknown", "missing", {}),
        ToolCall("invalid", "read", {"unexpected": True}),
        ToolCall("first", "read", {}),
        ToolCall("second", "read", {}),
        ToolCall("third", "read", {}),
    )

    results = await runtime.execute_batch(
        calls, context=context, composition_revision="revision"
    )

    assert [result.status for result in results] == [
        "invalid",
        "invalid",
        "succeeded",
        "succeeded",
        "denied",
    ]
    assert entered == ["first", "second"]
    admitted = [
        event.data["tool_call_id"]
        for event in await sessions.read_session("session-root")
        if event.type == "tool/admitted"
    ]
    assert admitted == ["first", "second"]
    account = (await service.ledger()).account("agent-root")
    assert account is not None
    assert account.charged.tool_calls == 2


async def test_inactive_tool_dimension_allows_calls_without_usage_facts(
    tmp_path: Path,
) -> None:
    store, service = await root_context(
        root_limits=limits(max_tool_calls=None)
    )
    sessions = SessionService(store)
    registry = ToolRegistry()
    entered: list[str] = []
    registry.register(RecordingTool("read", entered))
    runtime = ToolRuntime(
        registry,
        sessions,
        policies=(AllowByDefaultPolicy(),),
        admission_gate=BudgetToolAdmissionGate(
            service,
            agent_id="agent-root",
            session_id="session-root",
        ),
    )

    results = await runtime.execute_batch(
        (
            ToolCall("call-one", "read", {}),
            ToolCall("call-two", "read", {}),
        ),
        context=ToolExecutionContext(
            "session-root", "turn", "step", "batch", tmp_path, tmp_path
        ),
        composition_revision="revision",
    )

    assert [result.status for result in results] == ["succeeded", "succeeded"]
    assert entered == ["call-one", "call-two"]
    ledger = await service.ledger()
    assert ledger.account("agent-root").charged.tool_calls == 0
    assert ledger.charges == ()


async def test_admitted_tool_failure_is_not_refunded(tmp_path: Path) -> None:
    store, service = await root_context(root_limits=limits(max_tool_calls=1))
    sessions = SessionService(store)
    registry = ToolRegistry()
    registry.register(RecordingTool("failing", [], fail=True))
    runtime = ToolRuntime(
        registry,
        sessions,
        policies=(AllowByDefaultPolicy(),),
        admission_gate=BudgetToolAdmissionGate(
            service,
            agent_id="agent-root",
            session_id="session-root",
        ),
    )
    result = await runtime.execute_batch(
        (ToolCall("call-failed", "failing", {}),),
        context=ToolExecutionContext(
            "session-root", "turn", "step", "batch", tmp_path, tmp_path
        ),
        composition_revision="revision",
    )

    assert result[0].status == "failed"
    assert (await service.ledger()).available("agent-root").max_tool_calls == 0


async def test_tool_admission_cancellation_converges_before_dispatch(
    tmp_path: Path,
) -> None:
    store, _ = await root_context()
    sessions = SessionService(store)
    registry = ToolRegistry()
    entered_tools: list[str] = []
    registry.register(RecordingTool("read", entered_tools))
    gate_entered = asyncio.Event()
    gate_release = asyncio.Event()

    class GatedAdmission:
        async def admit(
            self,
            calls: tuple[PreparedToolCall, ...],
            context: ToolExecutionContext,
        ) -> tuple[ToolAdmissionDecision, ...]:
            del context
            gate_entered.set()
            await gate_release.wait()
            return tuple(
                ToolAdmissionDecision(call.tool_call_id, True) for call in calls
            )

    runtime = ToolRuntime(
        registry,
        sessions,
        policies=(AllowByDefaultPolicy(),),
        admission_gate=GatedAdmission(),
    )
    task = asyncio.create_task(
        runtime.execute_batch(
            (ToolCall("call-one", "read", {}),),
            context=ToolExecutionContext(
                "session-root", "turn", "step", "batch", tmp_path, tmp_path
            ),
            composition_revision="revision",
        )
    )
    await gate_entered.wait()
    task.cancel()
    gate_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert entered_tools == []
    events = await sessions.read_session("session-root")
    assert [event.type for event in events if event.type.startswith("tool/")] == [
        "tool/call",
        "tool/admitted",
        "tool/result",
    ]
    result = next(event for event in events if event.type == "tool/result")
    assert result.data["status"] == "aborted_before_dispatch"


async def test_repeated_cancellation_waits_for_tool_outcome_finalizer(
    tmp_path: Path,
) -> None:
    class GatedOutcomeStore(InMemoryEventStore):
        def __init__(self) -> None:
            super().__init__()
            self.outcome_entered = asyncio.Event()
            self.outcome_release = asyncio.Event()
            self.outcome_finished = asyncio.Event()

        async def append(
            self,
            stream_id,
            *,
            expected_seq,
            events,
            durability=Durability.SYNC,
        ):
            if any(
                event.type == "effect/outcome"
                and event.data.get("status") == "cancelled"
                for event in events
            ):
                self.outcome_entered.set()
                await self.outcome_release.wait()
                try:
                    return await super().append(
                        stream_id,
                        expected_seq=expected_seq,
                        events=events,
                        durability=durability,
                    )
                finally:
                    self.outcome_finished.set()
            return await super().append(
                stream_id,
                expected_seq=expected_seq,
                events=events,
                durability=durability,
            )

    class GatedTool:
        name = "gated"
        description = "wait until cancelled"
        input_schema = {"type": "object", "additionalProperties": False}
        effect_kind = EffectKind.WORKSPACE_WRITE

        def __init__(self) -> None:
            self.entered = asyncio.Event()

        async def execute(self, arguments, context) -> ToolOutput:
            del arguments, context
            self.entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    store = GatedOutcomeStore()
    sessions = SessionService(store)
    await sessions.create_session(tmp_path, session_id="session")
    registry = ToolRegistry()
    tool = GatedTool()
    registry.register(tool)
    runtime = ToolRuntime(
        registry,
        sessions,
        policies=(AllowByDefaultPolicy(),),
    )
    execution = asyncio.create_task(
        runtime.execute_batch(
            (ToolCall("call", "gated", {}),),
            context=ToolExecutionContext(
                "session", "turn", "step", "batch", tmp_path, tmp_path
            ),
            composition_revision="revision",
        )
    )
    await tool.entered.wait()

    execution.cancel()
    await store.outcome_entered.wait()
    execution.cancel()
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(execution), timeout=0.1)
    finally:
        store.outcome_release.set()
        await store.outcome_finished.wait()
    with pytest.raises(asyncio.CancelledError):
        await execution
    effects = await sessions.read_effects("session")
    assert [event.type for event in effects][-1] == "effect/outcome"
    events = await sessions.read_session("session")
    assert [event.type for event in events][-1] == "tool/result"


async def test_step_limit_stops_continuation_and_blocks_the_next_turn(
    tmp_path: Path,
) -> None:
    store = InMemoryEventStore()
    service = BudgetLedgerService(store)
    provider = ScriptedLlmProvider(
        (
            ModelResponse(
                content="use tool",
                tool_calls=(ToolCall("call-one", "noop", {}),),
                usage=Usage(1, 1, UsageQuality.EXACT),
            ),
            ModelResponse(
                content="second response",
                usage=Usage(1, 1, UsageQuality.EXACT),
            ),
        )
    )
    enforcement = BudgetEnforcement(
        service,
        agent_id="agent-root",
        session_id="session-root",
        continuation=DefaultContinuationRuntime(),
    )
    tool = RecordingTool("noop", [])
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data", provider="scripted", model="model"),
        provider=provider,
        event_store=store,
        additional_tools=(tool,),
        continuation=enforcement.continuation,
        llm_runtime=enforcement.llm_runtime,
        tool_admission_gate=enforcement.tool_admission_gate,
    )
    await runtime.create_session(tmp_path, session_id="session-root")
    await AgentRegistrar(store).create_agent(
        AgentSpec(preset="managed", workspace_id="workspace"),
        request_id="create-root",
        agent_id="agent-root",
        session_id="session-root",
    )
    await service.grant_root(
        operation_id="grant-root",
        agent_id="agent-root",
        limits=limits(max_steps=1),
    )
    execution = enforcement.wrap(AgentRuntimeExecution(runtime, "session-root"))

    first = await execution.run_turn(TurnInput("work", "message-one"))
    assert first.reason == "budget_steps_exhausted"
    assert provider._index == 1
    events_before = await runtime.sessions.read_session("session-root")

    with pytest.raises(BudgetExhaustedError) as exhausted:
        await execution.run_turn(TurnInput("again", "message-two"))
    assert exhausted.value.dimension == "max_steps"
    events_after = await runtime.sessions.read_session("session-root")
    assert events_after == events_before
    await execution.dispose()


async def test_wall_deadline_cancels_and_converges_the_real_turn(tmp_path: Path) -> None:
    store = InMemoryEventStore()
    service = BudgetLedgerService(store)

    class GatedProvider:
        name = "scripted"

        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def complete(self, model_request):
            del model_request
            self.entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    provider = GatedProvider()
    enforcement = BudgetEnforcement(
        service,
        agent_id="agent-root",
        session_id="session-root",
        continuation=DefaultContinuationRuntime(),
    )
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data", provider="scripted", model="model"),
        provider=provider,
        event_store=store,
        continuation=enforcement.continuation,
        llm_runtime=enforcement.llm_runtime,
        tool_admission_gate=enforcement.tool_admission_gate,
    )
    await runtime.create_session(tmp_path, session_id="session-root")
    await AgentRegistrar(store).create_agent(
        AgentSpec(preset="managed", workspace_id="workspace"),
        request_id="create-root",
        agent_id="agent-root",
        session_id="session-root",
    )
    await service.grant_root(
        operation_id="grant-root",
        agent_id="agent-root",
        limits=limits(max_wall_milliseconds=500),
    )
    execution = enforcement.wrap(AgentRuntimeExecution(runtime, "session-root"))

    turn = asyncio.create_task(
        execution.run_turn(TurnInput("wait", "message-wall"))
    )
    await asyncio.wait_for(provider.entered.wait(), timeout=1)
    with pytest.raises(BudgetExhaustedError) as exhausted:
        await turn
    assert exhausted.value.dimension == "max_wall_milliseconds"
    assert provider.cancelled.is_set()
    assert runtime._active == {}
    account = (await service.ledger()).account("agent-root")
    assert account is not None
    assert account.charged.wall_milliseconds >= 1
    assert account.reserved.wall_milliseconds == 0
    assert (
        (await service.ledger()).usage_reservations[-1].status
        is BudgetUsageReservationStatus.SETTLED
    )
    await execution.dispose()


async def test_cancel_after_wall_start_commit_settles_without_starting_turn(
    tmp_path: Path,
) -> None:
    store = CommitThenWaitStore(BUDGET_USAGE_STARTED)
    service = BudgetLedgerService(store)
    provider = CountingProvider()
    enforcement = BudgetEnforcement(
        service,
        agent_id="agent-root",
        session_id="session-root",
        continuation=DefaultContinuationRuntime(),
    )
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data", provider="scripted", model="model"),
        provider=provider,
        event_store=store,
        continuation=enforcement.continuation,
        llm_runtime=enforcement.llm_runtime,
        tool_admission_gate=enforcement.tool_admission_gate,
    )
    await runtime.create_session(tmp_path, session_id="session-root")
    await AgentRegistrar(store).create_agent(
        AgentSpec(preset="managed", workspace_id="workspace"),
        request_id="create-root",
        agent_id="agent-root",
        session_id="session-root",
    )
    await service.grant_root(
        operation_id="grant-root",
        agent_id="agent-root",
        limits=limits(max_wall_milliseconds=50),
    )
    execution = enforcement.wrap(AgentRuntimeExecution(runtime, "session-root"))
    turn = asyncio.create_task(
        execution.run_turn(TurnInput("wait", "message-start-cancelled"))
    )
    await store.committed.wait()

    turn.cancel()
    await asyncio.sleep(0)
    turn.cancel()
    await asyncio.sleep(0)
    store.release.set()

    with pytest.raises(asyncio.CancelledError):
        await turn
    assert store.finished.is_set()
    assert provider.calls == 0
    events = await runtime.sessions.read_session("session-root")
    assert not any(event.type == "turn/start" for event in events)
    ledger = await service.ledger()
    account = ledger.account("agent-root")
    assert account is not None
    assert account.charged.wall_milliseconds == 50
    assert account.reserved.wall_milliseconds == 0
    assert ledger.usage_reservations[-1].status is BudgetUsageReservationStatus.SETTLED
    await execution.dispose()


async def test_generation_replacement_preserves_the_same_tool_gate(
    tmp_path: Path,
) -> None:
    store, service = await root_context()
    gate = BudgetToolAdmissionGate(
        service,
        agent_id="agent-root",
        session_id="session-root",
    )
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data"),
        event_store=store,
        tool_admission_gate=gate,
    )
    current = runtime.loop.compositions.current_generation
    assert current.tools.admission_gate is gate
    await runtime.dispose()


async def test_runtime_factory_does_not_truth_test_an_injected_llm_runtime(
    tmp_path: Path,
) -> None:
    class HostileTruthRuntime(LlmRuntime):
        def __bool__(self) -> bool:
            raise RuntimeError("truth testing an injected runtime is forbidden")

    injected = HostileTruthRuntime()
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data"),
        llm_runtime=injected,
    )

    assert runtime.loop.llm_runtime is injected
    await runtime.dispose()


async def test_enforcement_rejects_a_runtime_bound_to_another_store(
    tmp_path: Path,
) -> None:
    budget_store = InMemoryEventStore()
    runtime_store = InMemoryEventStore()
    enforcement = BudgetEnforcement(
        BudgetLedgerService(budget_store),
        agent_id="agent-root",
        session_id="session-root",
        continuation=DefaultContinuationRuntime(),
    )
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data"),
        event_store=runtime_store,
        continuation=enforcement.continuation,
        llm_runtime=enforcement.llm_runtime,
        tool_admission_gate=enforcement.tool_admission_gate,
    )
    await runtime.create_session(tmp_path, session_id="session-root")

    with pytest.raises(BudgetDirectoryMismatchError):
        enforcement.wrap(AgentRuntimeExecution(runtime, "session-root"))
    await runtime.dispose()


async def test_enforcement_bundle_identity_is_read_only() -> None:
    _, service = await root_context()
    enforcement = BudgetEnforcement(
        service,
        agent_id="agent-root",
        session_id="session-root",
        continuation=DefaultContinuationRuntime(),
    )

    with pytest.raises(AttributeError):
        enforcement.agent_id = "other"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        enforcement.session_id = "other"  # type: ignore[misc]
