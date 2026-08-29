from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from traceh.agents import AgentRegistrar
from traceh.api.agents import AgentSpec
from traceh.api.budgets import BudgetLimits, BudgetUsageReservationStatus
from traceh.api.events import EventEnvelope
from traceh.api.json_types import fingerprint
from traceh.api.llm import (
    ModelAttemptIdentity,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    Usage,
    UsageQuality,
    model_attempt_reservation_id,
)
from traceh.api.turns import TurnInput
from traceh.budgets import (
    BudgetedLlmRuntime,
    BudgetEnforcement,
    BudgetEvidenceError,
    BudgetExhaustedError,
    BudgetLedgerService,
)
from traceh.llm.failures import ProviderFailure, ProviderFailureCategory
from traceh.llm.runtime import LlmAdmission, LlmAdmissionBindingError, LlmRuntime
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.runtime.continuation import DefaultContinuationRuntime
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.event_store import Durability, InMemoryEventStore
from traceh.session.invariants import CoreInvariantChecker
from traceh.session.service import ModelAttemptConflictError, SessionService
from traceh.supervision import AgentRuntimeExecution

pytestmark = pytest.mark.asyncio


def limits(*, max_tokens: int) -> BudgetLimits:
    return BudgetLimits(
        max_tokens=max_tokens,
        max_steps=4,
        max_tool_calls=4,
        max_wall_milliseconds=None,
        max_children=2,
        max_depth=2,
        max_processes=2,
    )


class FixedTokenCounter:
    def __init__(self, value: int) -> None:
        self.value = value

    def count_request(self, request: ModelRequest) -> int:
        del request
        return self.value


class CountingProvider:
    name = "scripted"

    def __init__(self) -> None:
        self.calls = 0
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        self.requests.append(request)
        return ModelResponse(
            content="done",
            usage=Usage(input_tokens=2, output_tokens=1, quality=UsageQuality.EXACT),
        )


async def budget_context(
    store: InMemoryEventStore,
    tmp_path: Path,
    *,
    max_tokens: int,
) -> tuple[SessionService, BudgetLedgerService]:
    sessions = SessionService(store)
    await sessions.create_session(tmp_path, session_id="session-root")
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
        limits=limits(max_tokens=max_tokens),
    )
    return sessions, budgets


def model_request(*, max_output_tokens: int = 5) -> ModelRequest:
    return ModelRequest(
        provider="scripted",
        model="model",
        messages=(ModelMessage(role="user", content="work"),),
        max_output_tokens=max_output_tokens,
        metadata={
            "session_id": "session-root",
            "turn_id": "turn-root",
            "step_id": "step-root",
            "composition_revision": "revision",
        },
    )


def attempt(*, ordinal: int = 1) -> ModelAttemptIdentity:
    return ModelAttemptIdentity(
        session_id="session-root",
        turn_id="turn-root",
        step_id="step-root",
        attempt_id=str(uuid4()),
        ordinal=ordinal,
    )


async def test_zero_token_budget_never_records_or_dispatches_an_attempt(
    tmp_path: Path,
) -> None:
    store = InMemoryEventStore()
    provider = CountingProvider()
    _, budgets = await budget_context(store, tmp_path, max_tokens=0)
    enforcement = BudgetEnforcement(
        budgets,
        agent_id="agent-root",
        session_id="session-root",
        continuation=DefaultContinuationRuntime(),
    )
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "data",
            provider="scripted",
            model="model",
        ),
        provider=provider,
        event_store=store,
        continuation=enforcement.continuation,
        llm_runtime=enforcement.llm_runtime,
        tool_admission_gate=enforcement.tool_admission_gate,
    )
    execution = enforcement.wrap(AgentRuntimeExecution(runtime, "session-root"))

    with pytest.raises(BudgetExhaustedError) as exhausted:
        await execution.run_turn(TurnInput("work", "message-zero"))

    assert exhausted.value.dimension == "max_tokens"
    assert provider.calls == 0
    event_types = {event.type for event in await runtime.sessions.read_session("session-root")}
    assert "request/snapshot" not in event_types
    assert "model/attempt-start" not in event_types
    await execution.dispose()


async def test_dispatch_snapshot_is_the_exact_budget_bounded_provider_request(
    tmp_path: Path,
) -> None:
    store = InMemoryEventStore()
    provider = CountingProvider()
    _, budgets = await budget_context(store, tmp_path, max_tokens=10)
    enforcement = BudgetEnforcement(
        budgets,
        agent_id="agent-root",
        session_id="session-root",
        continuation=DefaultContinuationRuntime(),
    )
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "data",
            provider="scripted",
            model="model",
            max_output_tokens=50,
        ),
        provider=provider,
        event_store=store,
        continuation=enforcement.continuation,
        llm_runtime=enforcement.llm_runtime,
        tool_admission_gate=enforcement.tool_admission_gate,
    )
    execution = enforcement.wrap(AgentRuntimeExecution(runtime, "session-root"))

    result = await execution.run_turn(TurnInput("work", "message-bounded"))

    assert result.reason == "completed"
    assert provider.calls == 1
    events = await runtime.sessions.read_session("session-root")
    snapshot = next(event for event in events if event.type == "request/snapshot")
    assert snapshot.data["composed_request"]["max_output_tokens"] == 50
    assert snapshot.data["dispatch_request"]["max_output_tokens"] == 10
    assert provider.requests[0].to_dict() == snapshot.data["dispatch_request"]
    assert not await verify_request_snapshots(
        runtime.sessions,
        runtime.surface,
        "session-root",
    )
    assert not await runtime.check_invariants("session-root")
    await execution.dispose()


async def test_admission_cannot_rewrite_model_context_before_dispatch(
    tmp_path: Path,
) -> None:
    class RewritingRuntime(LlmRuntime):
        async def admit(self, provider, request, *, attempt):
            rewritten = replace(
                request,
                messages=(ModelMessage(role="user", content="rewritten"),),
            )
            return LlmAdmission(provider, rewritten, attempt=attempt)

    store = InMemoryEventStore()
    provider = CountingProvider()
    _, budgets = await budget_context(store, tmp_path, max_tokens=10)
    enforcement = BudgetEnforcement(
        budgets,
        agent_id="agent-root",
        session_id="session-root",
        continuation=DefaultContinuationRuntime(),
        llm_runtime=RewritingRuntime(),
        token_counter=FixedTokenCounter(2),
    )
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "data",
            provider="scripted",
            model="model",
            max_output_tokens=5,
        ),
        provider=provider,
        event_store=store,
        continuation=enforcement.continuation,
        llm_runtime=enforcement.llm_runtime,
        tool_admission_gate=enforcement.tool_admission_gate,
    )
    execution = enforcement.wrap(AgentRuntimeExecution(runtime, "session-root"))

    with pytest.raises(ModelAttemptConflictError):
        await execution.run_turn(TurnInput("work", "message-rewrite"))

    assert provider.calls == 0
    events = await runtime.sessions.read_session("session-root")
    assert not any(event.type == "request/snapshot" for event in events)
    assert not any(event.type == "model/attempt-start" for event in events)
    assert (await budgets.ledger()).usage_reservations[
        -1
    ].status is BudgetUsageReservationStatus.RELEASED
    await execution.dispose()


async def test_admission_cannot_swap_the_composition_provider_at_dispatch(
    tmp_path: Path,
) -> None:
    primary = CountingProvider()
    alternate = CountingProvider()

    class ProviderSwappingRuntime(LlmRuntime):
        async def admit(self, provider, request, *, attempt):
            del provider
            return LlmAdmission(alternate, request, attempt=attempt)

    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "data",
            provider="scripted",
            model="model",
        ),
        provider=primary,
        llm_runtime=ProviderSwappingRuntime(),
        event_store=InMemoryEventStore(),
    )
    await runtime.sessions.create_session(tmp_path, session_id="session-root")
    execution = AgentRuntimeExecution(runtime, "session-root")

    try:
        with pytest.raises(
            LlmAdmissionBindingError,
            match="model-admission-binding-mismatch",
        ):
            await execution.run_turn(TurnInput("work", "message-provider-swap"))

        assert primary.calls == 0
        assert alternate.calls == 0
        events = await runtime.sessions.read_session("session-root")
        assert not any(event.type == "request/snapshot" for event in events)
        assert not any(event.type == "model/attempt-start" for event in events)
    finally:
        await runtime.dispose()


async def test_admission_subclass_cannot_rewrite_the_request_during_dispatch(
    tmp_path: Path,
) -> None:
    provider = CountingProvider()

    class DispatchRewritingAdmission(LlmAdmission):
        async def _dispatch(self, *, on_text_delta=None):
            del on_text_delta
            rewritten = replace(self.request, model="other-model")
            return await self.provider.complete(rewritten)

    class DispatchRewritingRuntime(LlmRuntime):
        async def admit(self, provider, request, *, attempt):
            return DispatchRewritingAdmission(provider, request, attempt=attempt)

    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "data",
            provider="scripted",
            model="model",
        ),
        provider=provider,
        llm_runtime=DispatchRewritingRuntime(),
        event_store=InMemoryEventStore(),
    )
    await runtime.sessions.create_session(tmp_path, session_id="session-root")
    execution = AgentRuntimeExecution(runtime, "session-root")

    try:
        with pytest.raises(
            LlmAdmissionBindingError,
            match="model-admission-binding-mismatch",
        ):
            await execution.run_turn(TurnInput("work", "message-dispatch-rewrite"))

        assert provider.calls == 0
        events = await runtime.sessions.read_session("session-root")
        assert not any(event.type == "request/snapshot" for event in events)
        assert not any(event.type == "model/attempt-start" for event in events)
    finally:
        await runtime.dispose()


async def test_budget_releases_when_an_inner_runtime_forges_dispatch(
    tmp_path: Path,
) -> None:
    class ForgedAdmission(LlmAdmission):
        async def _dispatch(self, *, on_text_delta=None):
            del on_text_delta
            return ModelResponse(content="forged")

    class ForgingRuntime(LlmRuntime):
        async def admit(self, provider, request, *, attempt):
            return ForgedAdmission(provider, request, attempt=attempt)

    store = InMemoryEventStore()
    provider = CountingProvider()
    _, budgets = await budget_context(store, tmp_path, max_tokens=10)
    enforcement = BudgetEnforcement(
        budgets,
        agent_id="agent-root",
        session_id="session-root",
        continuation=DefaultContinuationRuntime(),
        llm_runtime=ForgingRuntime(),
        token_counter=FixedTokenCounter(2),
    )
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "data",
            provider="scripted",
            model="model",
            max_output_tokens=5,
        ),
        provider=provider,
        event_store=store,
        continuation=enforcement.continuation,
        llm_runtime=enforcement.llm_runtime,
        tool_admission_gate=enforcement.tool_admission_gate,
    )
    execution = enforcement.wrap(AgentRuntimeExecution(runtime, "session-root"))

    try:
        with pytest.raises(
            LlmAdmissionBindingError,
            match="model-admission-binding-mismatch",
        ):
            await execution.run_turn(TurnInput("work", "message-budget-forgery"))

        assert provider.calls == 0
        events = await runtime.sessions.read_session("session-root")
        assert not any(event.type == "request/snapshot" for event in events)
        assert not any(event.type == "model/attempt-start" for event in events)
        assert (await budgets.ledger()).usage_reservations[
            -1
        ].status is BudgetUsageReservationStatus.RELEASED
    finally:
        await execution.dispose()


class TwoReaderBarrierStore(InMemoryEventStore):
    def __init__(self) -> None:
        super().__init__()
        self._armed_stream: str | None = None
        self._readers = 0
        self._both_read = asyncio.Event()

    def arm(self, stream_id: str) -> None:
        self._armed_stream = stream_id

    async def read(self, stream_id: str, *, from_seq: int = 1):
        snapshot = await super().read(stream_id, from_seq=from_seq)
        if stream_id == self._armed_stream:
            self._readers += 1
            if self._readers == 2:
                self._both_read.set()
            await self._both_read.wait()
        return snapshot


async def test_session_cas_is_the_only_dispatch_permit_for_competing_owners(
    tmp_path: Path,
) -> None:
    store = TwoReaderBarrierStore()
    sessions, budgets = await budget_context(store, tmp_path, max_tokens=20)
    await sessions.append_session("session-root", "turn/start", {"turn_id": "turn-root"})
    await sessions.append_session(
        "session-root",
        "step/start",
        {"turn_id": "turn-root", "step_id": "step-root"},
    )
    provider = CountingProvider()
    llm = BudgetedLlmRuntime(
        budgets,
        agent_id="agent-root",
        session_id="session-root",
        token_counter=FixedTokenCounter(5),
    )
    request = model_request()
    first = await llm.admit(provider, request, attempt=attempt())
    second = await llm.admit(provider, request, attempt=attempt())
    assert first.reservation_id != second.reservation_id
    assert fingerprint(first.request.to_dict()) == fingerprint(second.request.to_dict())
    store.arm("session:session-root")

    async def run_owner(
        owner_sessions: SessionService,
        admission,
    ) -> bool:
        try:
            _, start = await owner_sessions.start_model_attempt(
                "session-root",
                attempt=admission.attempt,
                source_seq=3,
                composition_revision="revision",
                composed_request=request,
                composed_fingerprint=fingerprint(request.to_dict()),
                dispatch_request=admission.request,
                dispatch_fingerprint=fingerprint(admission.request.to_dict()),
                reservation_id=admission.reservation_id,
            )
        except ModelAttemptConflictError:
            await admission.abort()
            return False
        await admission.dispatch(
            provider=admission.provider,
            request=admission.request,
        )
        await owner_sessions.append_session(
            "session-root",
            "model/attempt-end",
            {
                "turn_id": "turn-root",
                "step_id": "step-root",
                "attempt_id": admission.attempt.attempt_id,
                "ordinal": 1,
                "request_snapshot_seq": start.data["request_snapshot_seq"],
                "dispatch_fingerprint": start.data["dispatch_fingerprint"],
                "reservation_id": admission.reservation_id,
                "status": "succeeded",
            },
        )
        return True

    outcomes = await asyncio.gather(
        run_owner(SessionService(store), first),
        run_owner(SessionService(store), second),
    )

    assert provider.calls == 1
    assert sorted(outcomes) == [False, True]
    events = await sessions.read_session("session-root")
    assert sum(event.type == "request/snapshot" for event in events) == 1
    assert sum(event.type == "model/attempt-start" for event in events) == 1
    assert not CoreInvariantChecker().check(events)
    statuses = {item.status for item in (await budgets.ledger()).usage_reservations}
    assert statuses == {
        BudgetUsageReservationStatus.RELEASED,
        BudgetUsageReservationStatus.SETTLED,
    }


async def test_later_ordinal_reuses_the_exact_snapshot_with_a_new_reservation(
    tmp_path: Path,
) -> None:
    store = InMemoryEventStore()
    sessions, budgets = await budget_context(store, tmp_path, max_tokens=20)
    await sessions.append_session("session-root", "turn/start", {"turn_id": "turn-root"})
    await sessions.append_session(
        "session-root",
        "step/start",
        {"turn_id": "turn-root", "step_id": "step-root"},
    )
    class RetryCountingProvider(CountingProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            self.calls += 1
            self.requests.append(request)
            if self.calls == 1:
                raise ProviderFailure(
                    "provider-dns-temporary",
                    ProviderFailureCategory.DNS,
                    usage=Usage(1, 0, UsageQuality.EXACT),
                )
            return ModelResponse(
                content="done",
                usage=Usage(2, 1, UsageQuality.EXACT),
            )

    provider = RetryCountingProvider()
    llm = BudgetedLlmRuntime(
        budgets,
        agent_id="agent-root",
        session_id="session-root",
        token_counter=FixedTokenCounter(2),
    )
    request = model_request()
    starts = []
    admissions = []
    for ordinal in (1, 2):
        admission = await llm.admit(
            provider,
            request,
            attempt=attempt(ordinal=ordinal),
        )
        _, start = await sessions.start_model_attempt(
            "session-root",
            attempt=admission.attempt,
            source_seq=3,
            composition_revision="revision",
            composed_request=request,
            composed_fingerprint=fingerprint(request.to_dict()),
            dispatch_request=admission.request,
            dispatch_fingerprint=fingerprint(admission.request.to_dict()),
            reservation_id=admission.reservation_id,
            retry_wait_milliseconds=0 if ordinal == 1 else 1,
            retry_failure_code=(
                None if ordinal == 1 else "provider-dns-temporary"
            ),
            retry_failure_category=None if ordinal == 1 else "dns",
        )
        status = "succeeded"
        try:
            await admission.dispatch(
                provider=admission.provider,
                request=admission.request,
            )
        except ProviderFailure:
            status = "failed"
        await sessions.append_session(
            "session-root",
            "model/attempt-end",
            {
                "turn_id": "turn-root",
                "step_id": "step-root",
                "attempt_id": admission.attempt.attempt_id,
                "ordinal": ordinal,
                "request_snapshot_seq": start.data["request_snapshot_seq"],
                "dispatch_fingerprint": start.data["dispatch_fingerprint"],
                "reservation_id": admission.reservation_id,
                "status": status,
                **(
                    {
                        "failure_code": "provider-dns-temporary",
                        "failure_category": "dns",
                    }
                    if status == "failed"
                    else {}
                ),
            },
        )
        admissions.append(admission)
        starts.append(start)

    events = await sessions.read_session("session-root")
    assert provider.calls == 2
    assert sum(event.type == "request/snapshot" for event in events) == 1
    assert [start.data["ordinal"] for start in starts] == [1, 2]
    assert starts[0].data["request_snapshot_seq"] == starts[1].data["request_snapshot_seq"]
    assert starts[0].data["dispatch_fingerprint"] == starts[1].data["dispatch_fingerprint"]
    assert admissions[0].reservation_id != admissions[1].reservation_id
    assert not CoreInvariantChecker().check(events)
    assert {item.status for item in (await budgets.ledger()).usage_reservations} == {
        BudgetUsageReservationStatus.SETTLED
    }


class BeforeCommitGateStore(InMemoryEventStore):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.finished = asyncio.Event()

    async def append(
        self,
        stream_id,
        *,
        expected_seq,
        events,
        durability=Durability.SYNC,
    ):
        if any(event.type == "request/snapshot" for event in events):
            self.entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.finished.set()
        return await super().append(
            stream_id,
            expected_seq=expected_seq,
            events=events,
            durability=durability,
        )


async def test_cancel_before_attempt_commit_releases_the_pending_admission(
    tmp_path: Path,
) -> None:
    store = BeforeCommitGateStore()
    sessions, budgets = await budget_context(store, tmp_path, max_tokens=10)
    await sessions.append_session("session-root", "turn/start", {"turn_id": "turn-root"})
    await sessions.append_session(
        "session-root",
        "step/start",
        {"turn_id": "turn-root", "step_id": "step-root"},
    )
    provider = CountingProvider()
    llm = BudgetedLlmRuntime(
        budgets,
        agent_id="agent-root",
        session_id="session-root",
        token_counter=FixedTokenCounter(2),
    )
    request = model_request()
    admission = await llm.admit(provider, request, attempt=attempt())

    async def claim() -> None:
        try:
            await sessions.start_model_attempt(
                "session-root",
                attempt=admission.attempt,
                source_seq=3,
                composition_revision="revision",
                composed_request=request,
                composed_fingerprint=fingerprint(request.to_dict()),
                dispatch_request=admission.request,
                dispatch_fingerprint=fingerprint(admission.request.to_dict()),
                reservation_id=admission.reservation_id,
            )
        except BaseException:
            await admission.abort()
            raise

    worker = asyncio.create_task(claim())
    await store.entered.wait()
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker

    assert store.finished.is_set()
    assert provider.calls == 0
    events = await sessions.read_session("session-root")
    assert not any(event.type == "request/snapshot" for event in events)
    assert not any(event.type == "model/attempt-start" for event in events)
    reservation = (await budgets.ledger()).usage_reservations[-1]
    assert reservation.status is BudgetUsageReservationStatus.RELEASED


class CommitThenRaiseStore(InMemoryEventStore):
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
        if any(event.type == "request/snapshot" for event in events):
            raise OSError("attempt commit outcome unavailable")
        return appended


async def test_unknown_attempt_commit_never_dispatches_and_turn_closes_it(
    tmp_path: Path,
) -> None:
    store = CommitThenRaiseStore()
    _, budgets = await budget_context(store, tmp_path, max_tokens=10)
    provider = CountingProvider()
    enforcement = BudgetEnforcement(
        budgets,
        agent_id="agent-root",
        session_id="session-root",
        continuation=DefaultContinuationRuntime(),
        token_counter=FixedTokenCounter(2),
    )
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "data",
            provider="scripted",
            model="model",
            max_output_tokens=5,
        ),
        provider=provider,
        event_store=store,
        continuation=enforcement.continuation,
        llm_runtime=enforcement.llm_runtime,
        tool_admission_gate=enforcement.tool_admission_gate,
    )
    execution = enforcement.wrap(AgentRuntimeExecution(runtime, "session-root"))

    with pytest.raises(OSError, match="outcome unavailable"):
        await execution.run_turn(TurnInput("work", "message-unknown-commit"))

    assert provider.calls == 0
    assert (await budgets.ledger()).usage_reservations[
        -1
    ].status is BudgetUsageReservationStatus.RELEASED
    events = await runtime.sessions.read_session("session-root")
    assert sum(event.type == "model/attempt-start" for event in events) == 1
    assert sum(event.type == "model/attempt-end" for event in events) == 1
    assert any(event.type == "step/end" for event in events)
    assert any(event.type == "turn/end" for event in events)
    assert not await runtime.check_invariants("session-root")
    await execution.dispose()


async def test_replay_rejects_tampered_attempt_and_dispatch_bindings(
    tmp_path: Path,
) -> None:
    store = InMemoryEventStore()
    sessions = SessionService(store)
    await sessions.create_session(tmp_path, session_id="session-root")
    await sessions.append_session("session-root", "turn/start", {"turn_id": "turn-root"})
    await sessions.append_session(
        "session-root",
        "step/start",
        {"turn_id": "turn-root", "step_id": "step-root"},
    )
    request = model_request()
    identity = attempt()
    reservation_id = model_attempt_reservation_id(identity)
    await sessions.start_model_attempt(
        "session-root",
        attempt=identity,
        source_seq=3,
        composition_revision="revision",
        composed_request=request,
        composed_fingerprint=fingerprint(request.to_dict()),
        dispatch_request=request,
        dispatch_fingerprint=fingerprint(request.to_dict()),
        reservation_id=reservation_id,
    )
    events = await sessions.read_session("session-root")
    snapshot_index = next(
        index for index, event in enumerate(events) if event.type == "request/snapshot"
    )
    start_index = next(
        index for index, event in enumerate(events) if event.type == "model/attempt-start"
    )

    def tamper(index: int, **changes: object) -> tuple[EventEnvelope, ...]:
        changed = list(events)
        data = dict(changed[index].data)
        data.update(changes)
        changed[index] = replace(changed[index], data=data)
        return tuple(changed)

    raw_dispatch = dict(events[snapshot_index].data["dispatch_request"])
    raw_dispatch["provider"] = "other"
    assert "request-dispatch-evidence" in {
        item.name
        for item in CoreInvariantChecker().check(
            tamper(snapshot_index, dispatch_request=raw_dispatch)
        )
    }
    coherent_rewrite = list(events)
    rewritten_hash = fingerprint(raw_dispatch)
    rewritten_snapshot = dict(coherent_rewrite[snapshot_index].data)
    rewritten_snapshot.update(
        dispatch_request=raw_dispatch,
        dispatch_fingerprint=rewritten_hash,
    )
    coherent_rewrite[snapshot_index] = replace(
        coherent_rewrite[snapshot_index], data=rewritten_snapshot
    )
    rewritten_start = dict(coherent_rewrite[start_index].data)
    rewritten_start.update(
        provider="other",
        dispatch_fingerprint=rewritten_hash,
    )
    coherent_rewrite[start_index] = replace(coherent_rewrite[start_index], data=rewritten_start)
    assert "request-dispatch-evidence" in {
        item.name for item in CoreInvariantChecker().check(tuple(coherent_rewrite))
    }
    assert "attempt-ordinal-contiguous" in {
        item.name for item in CoreInvariantChecker().check(tamper(start_index, ordinal=2))
    }
    assert "attempt-provider-model-binding" in {
        item.name for item in CoreInvariantChecker().check(tamper(start_index, provider="other"))
    }
    assert "attempt-reservation-binding" in {
        item.name
        for item in CoreInvariantChecker().check(tamper(start_index, reservation_id="wrong"))
    }


async def test_budget_reconciliation_rejects_a_tampered_attempt_end_binding(
    tmp_path: Path,
) -> None:
    store = InMemoryEventStore()
    sessions, budgets = await budget_context(store, tmp_path, max_tokens=10)
    await sessions.append_session("session-root", "turn/start", {"turn_id": "turn-root"})
    await sessions.append_session(
        "session-root",
        "step/start",
        {"turn_id": "turn-root", "step_id": "step-root"},
    )
    provider = CountingProvider()
    llm = BudgetedLlmRuntime(
        budgets,
        agent_id="agent-root",
        session_id="session-root",
        token_counter=FixedTokenCounter(2),
    )
    request = model_request()
    admission = await llm.admit(provider, request, attempt=attempt())
    _, start = await sessions.start_model_attempt(
        "session-root",
        attempt=admission.attempt,
        source_seq=3,
        composition_revision="revision",
        composed_request=request,
        composed_fingerprint=fingerprint(request.to_dict()),
        dispatch_request=admission.request,
        dispatch_fingerprint=fingerprint(admission.request.to_dict()),
        reservation_id=admission.reservation_id,
    )
    await admission.dispatch(
        provider=admission.provider,
        request=admission.request,
    )
    await sessions.append_session(
        "session-root",
        "model/attempt-end",
        {
            "turn_id": "turn-root",
            "step_id": "step-root",
            "attempt_id": admission.attempt.attempt_id,
            "ordinal": 1,
            "request_snapshot_seq": start.data["request_snapshot_seq"],
            "dispatch_fingerprint": start.data["dispatch_fingerprint"],
            "reservation_id": "wrong",
            "status": "succeeded",
        },
    )
    enforcement = BudgetEnforcement(
        budgets,
        agent_id="agent-root",
        session_id="session-root",
        continuation=DefaultContinuationRuntime(),
    )

    with pytest.raises(BudgetEvidenceError):
        await enforcement.continuation.reconcile()
