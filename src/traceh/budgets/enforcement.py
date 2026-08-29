"""Thin Budget adapters for the real execution boundaries.

The ledger is the only balance fact source.  This module does not add a
scheduler or a Runtime state bag: it installs small adapters at the model,
Tool, continuation and AgentExecution seams that already own the work.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import replace
from typing import Protocol

from traceh.agents.directory import AgentDirectoryReader
from traceh.agents.identity import is_agent_identifier
from traceh.api.budgets import (
    BudgetAmounts,
    BudgetUsageReservation,
    BudgetUsageReservationStatus,
)
from traceh.api.json_types import fingerprint
from traceh.api.llm import (
    LlmProvider,
    ModelAttemptIdentity,
    ModelRequest,
    ModelResponse,
    Usage,
    UsageQuality,
    model_attempt_reservation_id,
)
from traceh.api.tools import (
    PreparedToolCall,
    ToolAdmissionDecision,
    ToolExecutionContext,
)
from traceh.api.turns import TurnInput
from traceh.budgets.errors import (
    BudgetDirectoryMismatchError,
    BudgetEvidenceError,
    BudgetExhaustedError,
    BudgetInputError,
    BudgetReservationStateError,
    BudgetWriteError,
)
from traceh.budgets.events import MAX_BUDGET_VALUE
from traceh.budgets.service import BudgetLedgerService
from traceh.concurrency import await_worker_convergence
from traceh.llm.failures import ProviderFailure
from traceh.llm.runtime import (
    LlmAdmission,
    LlmAdmissionAccounting,
    LlmRuntime,
    require_llm_admission_binding,
)
from traceh.runtime.agent_loop import TurnResult
from traceh.runtime.continuation import (
    ContinuationRuntime,
    Continue,
    Finish,
    LoopDirective,
    VerificationFeedback,
)
from traceh.session.event_store import EventStore
from traceh.session.service import SessionService
from traceh.supervision.execution import (
    AgentExecution,
    AgentRuntimeExecution,
    durable_log_identity,
)


class TokenCounter(Protocol):
    """A host-trusted tokenizer for one concrete provider/model family."""

    def count_request(self, request: ModelRequest) -> int:
        ...


def budget_operation_id(kind: str, **identity: object) -> str:
    """Create one bounded stable id from host-owned operation identity."""

    return fingerprint({"kind": kind, "identity": identity})


async def _require_agent_session(
    store: EventStore, *, agent_id: str, session_id: str
) -> None:
    directory = await AgentDirectoryReader(store).load()
    record = directory.get(agent_id)
    if record is None or record.session_id != session_id:
        raise BudgetDirectoryMismatchError


async def _validate_model_attempt_budget_evidence(
    service: BudgetLedgerService,
    *,
    agent_id: str,
    session_id: str,
) -> None:
    """Cross-check Session dispatch permits against the one Budget ledger."""

    await _require_agent_session(
        service.store,
        agent_id=agent_id,
        session_id=session_id,
    )
    ledger = await service.ledger()
    account = ledger.account(agent_id)
    if account is None:
        raise BudgetEvidenceError
    token_budgeted = account.limits.max_tokens is not None
    events = await SessionService(service.store).read_session(session_id)
    attempt_ends = [event for event in events if event.type == "model/attempt-end"]
    for event in events:
        if event.type != "model/attempt-start":
            continue
        try:
            turn_id = _read_identity(event.data.get("turn_id"))
            step_id = _read_identity(event.data.get("step_id"))
            attempt_id = _read_identity(event.data.get("attempt_id"))
            ordinal = event.data.get("ordinal")
            if type(ordinal) is not int or ordinal < 1:
                raise BudgetEvidenceError
            identity = ModelAttemptIdentity(
                session_id=session_id,
                turn_id=turn_id,
                step_id=step_id,
                attempt_id=attempt_id,
                ordinal=ordinal,
            )
            reservation_id = event.data.get("reservation_id")
            matching_ends = [
                end
                for end in attempt_ends
                if end.data.get("attempt_id") == attempt_id
            ]
            if len(matching_ends) != 1:
                raise BudgetEvidenceError
            end = matching_ends[0]
            for key, expected in (
                ("turn_id", turn_id),
                ("step_id", step_id),
                ("ordinal", ordinal),
                ("reservation_id", reservation_id),
            ):
                if end.data.get(key) != expected:
                    raise BudgetEvidenceError
            if token_budgeted:
                if reservation_id != model_attempt_reservation_id(identity):
                    raise BudgetEvidenceError
                assert isinstance(reservation_id, str)
                reservation = ledger.usage_reservation(reservation_id)
                if (
                    reservation is None
                    or reservation.agent_id != agent_id
                    or reservation.amounts.tokens < 1
                    or reservation.status
                    not in {
                        BudgetUsageReservationStatus.SETTLED,
                        BudgetUsageReservationStatus.RELEASED,
                    }
                ):
                    raise BudgetEvidenceError
            elif reservation_id is not None:
                raise BudgetEvidenceError
        except BudgetEvidenceError:
            raise
        except Exception:
            raise BudgetEvidenceError from None


def _read_identity(value: object) -> str:
    if not is_agent_identifier(value):
        raise BudgetEvidenceError
    assert isinstance(value, str)
    return value


async def _await_owned(coro, *, name: str):
    """Converge one stateful finalizer before propagating cancellation."""

    task = asyncio.create_task(coro, name=name)
    cancellation: asyncio.CancelledError | None = None
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError as error:
        cancellation = error
        await await_worker_convergence(task)
    if task.cancelled():
        raise cancellation or asyncio.CancelledError()
    failure = task.exception()
    if failure is not None:
        if cancellation is not None:
            raise cancellation from failure
        raise failure
    if cancellation is not None:
        raise cancellation
    return task.result()


async def _finish_owned(
    coro,
    *,
    primary: BaseException | None,
    name: str,
    failure_message: str,
) -> None:
    """Run a finalizer once without masking the operation that required it."""

    try:
        await _await_owned(coro, name=name)
    except asyncio.CancelledError:
        # The finalizer has already converged before _await_owned re-raises
        # cancellation.  Let the lifecycle owner observe that cancellation so
        # it can close Attempt -> Step -> Turn; wrapping it with an earlier
        # provider outcome in BaseExceptionGroup would bypass that owner.
        raise
    except BaseException as finalizer_error:
        if primary is None:
            raise
        if isinstance(primary, asyncio.CancelledError):
            raise primary from finalizer_error
        raise BaseExceptionGroup(
            failure_message,
            (primary, finalizer_error),
        ) from None


async def _release_pending_usage(
    service: BudgetLedgerService,
    *,
    reservation_id: str,
    operation_kind: str,
    primary: BaseException,
) -> None:
    async def release() -> None:
        ledger = await service.ledger()
        reservation = ledger.usage_reservation(reservation_id)
        if (
            reservation is None
            or reservation.status is not BudgetUsageReservationStatus.PENDING
        ):
            return
        await service.release_usage(
            operation_id=budget_operation_id(
                operation_kind, reservation_id=reservation_id
            ),
            reservation_id=reservation_id,
        )

    await _finish_owned(
        release(),
        primary=primary,
        name="traceh-budget-usage-release",
        failure_message="usage reservation failed and release failed",
    )


async def _terminalize_cancelled_usage_start(
    service: BudgetLedgerService,
    *,
    reservation_id: str,
    release_operation_kind: str,
    settle_operation_kind: str,
    usage_quality: UsageQuality | None,
    primary: BaseException,
) -> None:
    """Close the hold when cancellation races the durable START commit."""

    async def terminalize() -> None:
        ledger = await service.ledger()
        reservation = ledger.usage_reservation(reservation_id)
        if reservation is None:
            return
        if reservation.status is BudgetUsageReservationStatus.PENDING:
            await service.release_usage(
                operation_id=budget_operation_id(
                    release_operation_kind,
                    reservation_id=reservation_id,
                ),
                reservation_id=reservation_id,
            )
            return
        if reservation.status is BudgetUsageReservationStatus.STARTED:
            await service.settle_usage(
                operation_id=budget_operation_id(
                    settle_operation_kind,
                    reservation_id=reservation_id,
                ),
                reservation_id=reservation_id,
                amounts=reservation.amounts,
                usage_quality=usage_quality,
            )

    await _finish_owned(
        terminalize(),
        primary=primary,
        name="traceh-budget-cancelled-start-terminalize",
        failure_message="usage start cancellation and terminalization both failed",
    )


async def _start_reserved_usage(
    service: BudgetLedgerService,
    *,
    reservation_id: str,
    start_operation_kind: str,
    release_operation_kind: str,
    settle_operation_kind: str,
    usage_quality: UsageQuality | None,
) -> BudgetUsageReservation:
    """Own START until its commit verdict is known, even under cancellation.

    Shielding only this ledger operation does not let the Provider or Turn run
    after cancellation.  It distinguishes a START this call successfully
    acquired from a concurrent caller's START before deciding whether a full
    conservative settlement is this call's responsibility.
    """

    start_task = asyncio.create_task(
        service.start_usage(
            operation_id=budget_operation_id(
                start_operation_kind,
                reservation_id=reservation_id,
            ),
            reservation_id=reservation_id,
        ),
        name="traceh-budget-usage-start",
    )
    cancellation: asyncio.CancelledError | None = None
    try:
        await asyncio.shield(start_task)
    except asyncio.CancelledError as error:
        cancellation = error
        await await_worker_convergence(start_task)

    if start_task.cancelled():
        primary = cancellation or asyncio.CancelledError()
        await _terminalize_cancelled_usage_start(
            service,
            reservation_id=reservation_id,
            release_operation_kind=release_operation_kind,
            settle_operation_kind=settle_operation_kind,
            usage_quality=usage_quality,
            primary=primary,
        )
        raise primary

    failure = start_task.exception()
    if failure is not None:
        primary = cancellation or failure
        if isinstance(failure, BudgetWriteError) and failure.committed is True:
            await _terminalize_cancelled_usage_start(
                service,
                reservation_id=reservation_id,
                release_operation_kind=release_operation_kind,
                settle_operation_kind=settle_operation_kind,
                usage_quality=usage_quality,
                primary=primary,
            )
        else:
            await _release_pending_usage(
                service,
                reservation_id=reservation_id,
                operation_kind=release_operation_kind,
                primary=primary,
            )
        if cancellation is not None:
            raise cancellation from failure
        raise failure

    reservation = start_task.result()
    if cancellation is not None:
        await _terminalize_cancelled_usage_start(
            service,
            reservation_id=reservation_id,
            release_operation_kind=release_operation_kind,
            settle_operation_kind=settle_operation_kind,
            usage_quality=usage_quality,
            primary=cancellation,
        )
        raise cancellation
    return reservation


class BudgetToolAdmissionGate:
    """Charge Tool slots in model order after normal Policy has allowed them."""

    __slots__ = ("_agent_id", "_service", "_session_id")

    def __init__(
        self,
        service: BudgetLedgerService,
        *,
        agent_id: str,
        session_id: str,
    ) -> None:
        self._service = service
        self._agent_id = agent_id
        self._session_id = session_id

    async def admit(
        self,
        calls: tuple[PreparedToolCall, ...],
        context: ToolExecutionContext,
    ) -> tuple[ToolAdmissionDecision, ...]:
        if not calls:
            return ()
        if context.session_id != self._session_id:
            raise BudgetDirectoryMismatchError
        await _require_agent_session(
            self._service.store,
            agent_id=self._agent_id,
            session_id=self._session_id,
        )
        admitted = await self._service.admit_tool_calls(
            operation_id=budget_operation_id(
                "tool-batch-admission",
                agent_id=self._agent_id,
                session_id=self._session_id,
                turn_id=context.turn_id,
                step_id=context.step_id,
                tool_call_ids=tuple(call.tool_call_id for call in calls),
            ),
            agent_id=self._agent_id,
            requested=len(calls),
        )
        return tuple(
            ToolAdmissionDecision(
                call.tool_call_id,
                index < admitted,
                code=(
                    None if index < admitted else "budget-tool-calls-exhausted"
                ),
            )
            for index, call in enumerate(calls)
        )


class BudgetContinuationRuntime:
    """Reconcile durable Step facts and stop before a further Step starts."""

    __slots__ = ("_agent_id", "_inner", "_service", "_session_id", "_sessions")

    def __init__(
        self,
        service: BudgetLedgerService,
        *,
        agent_id: str,
        session_id: str,
        inner: ContinuationRuntime,
    ) -> None:
        self._service = service
        self._agent_id = agent_id
        self._session_id = session_id
        self._inner = inner
        self._sessions = SessionService(service.store)

    async def preflight(self) -> None:
        await self.reconcile()
        remaining = (await self._service.ledger()).available(
            self._agent_id
        ).max_steps
        if remaining == 0:
            raise BudgetExhaustedError("max_steps")

    async def decide(
        self,
        *,
        response: ModelResponse,
        step_number: int,
        max_steps: int,
        verification: VerificationFeedback | None,
        verification_failures: int,
        max_verification_retries: int,
    ) -> LoopDirective:
        directive = await self._inner.decide(
            response=response,
            step_number=step_number,
            max_steps=max_steps,
            verification=verification,
            verification_failures=verification_failures,
            max_verification_retries=max_verification_retries,
        )
        await self.reconcile()
        if isinstance(directive, Continue):
            remaining = (await self._service.ledger()).available(
                self._agent_id
            ).max_steps
            if remaining == 0:
                return Finish("budget_steps_exhausted")
        return directive

    async def reconcile(self) -> None:
        await _validate_model_attempt_budget_evidence(
            self._service,
            agent_id=self._agent_id,
            session_id=self._session_id,
        )
        ledger = await self._service.ledger()
        if ledger.available(self._agent_id).max_steps is None:
            return
        await _require_agent_session(
            self._service.store,
            agent_id=self._agent_id,
            session_id=self._session_id,
        )
        events = await self._sessions.read_session(self._session_id)
        seen: set[str] = set()
        for event in events:
            try:
                if event.type != "step/start":
                    continue
                data = event.data
                if not isinstance(data, dict):
                    raise BudgetEvidenceError
                step_id = _read_identity(data.get("step_id"))
                _read_identity(data.get("turn_id"))
                number = data.get("number")
                if type(number) is not int or number < 1 or step_id in seen:
                    raise BudgetEvidenceError
                seen.add(step_id)
            except BudgetEvidenceError:
                raise
            except Exception:
                raise BudgetEvidenceError from None
            operation_id = budget_operation_id(
                "step-observation",
                agent_id=self._agent_id,
                session_id=self._session_id,
                step_id=step_id,
            )
            if ledger.operation_exists(operation_id):
                continue
            await self._service.record_usage(
                operation_id=operation_id,
                agent_id=self._agent_id,
                amounts=BudgetAmounts(steps=1),
            )


class _BudgetedLlmAccounting(LlmAdmissionAccounting):
    """One pending Token hold around, but never owning, Provider dispatch."""

    __slots__ = ("_owner", "_reservation_id")

    def __init__(self, owner: BudgetedLlmRuntime, *, reservation_id: str) -> None:
        self._owner = owner
        self._reservation_id = reservation_id

    async def start(self) -> object:
        return await _start_reserved_usage(
            self._owner._service,
            reservation_id=self._reservation_id,
            start_operation_kind="model-token-start",
            release_operation_kind="model-token-release",
            settle_operation_kind="model-token-settle",
            usage_quality=UsageQuality.UNKNOWN,
        )

    async def finish(
        self,
        started: object,
        *,
        response: ModelResponse | None,
        error: BaseException | None,
    ) -> None:
        if type(started) is not BudgetUsageReservation:
            raise BudgetReservationStateError
        reserved = started.amounts.tokens
        if error is not None:
            if type(error) is ProviderFailure and error.usage is not None:
                tokens, quality = self._owner._usage_settlement(
                    error.usage,
                    reserved,
                )
            else:
                tokens, quality = reserved, UsageQuality.UNKNOWN
            await self._owner._settle_after_outcome(
                self._reservation_id,
                tokens,
                quality,
                error,
            )
            return
        assert response is not None
        try:
            tokens, quality = self._owner._usage_settlement(response.usage, reserved)
        except BaseException as settlement_error:
            await self._owner._settle_after_outcome(
                self._reservation_id,
                reserved,
                UsageQuality.UNKNOWN,
                settlement_error,
            )
            raise
        await self._owner._settle_after_outcome(
            self._reservation_id,
            tokens,
            quality,
            None,
        )

    async def abort(self) -> None:
        ledger = await self._owner._service.ledger()
        reservation = ledger.usage_reservation(self._reservation_id)
        if (
            reservation is not None
            and reservation.status is BudgetUsageReservationStatus.PENDING
        ):
            await self._owner._service.release_usage(
                operation_id=budget_operation_id(
                    "model-token-release",
                    reservation_id=self._reservation_id,
                ),
                reservation_id=self._reservation_id,
            )


class BudgetedLlmRuntime(LlmRuntime):
    """Reserve Token authority before a provider call and settle it afterwards."""

    __slots__ = (
        "_agent_id",
        "_allow_estimated",
        "_inner",
        "_service",
        "_session_id",
        "_token_counter",
    )

    def __init__(
        self,
        service: BudgetLedgerService,
        *,
        agent_id: str,
        session_id: str,
        inner: LlmRuntime | None = None,
        token_counter: TokenCounter | None = None,
        allow_estimated: bool = False,
    ) -> None:
        if type(allow_estimated) is not bool:
            raise BudgetInputError(
                "budget-estimated-usage-policy-invalid",
                "allow_estimated",
            )
        self._service = service
        self._agent_id = agent_id
        self._session_id = session_id
        self._inner = LlmRuntime() if inner is None else inner
        self._token_counter = token_counter
        self._allow_estimated = allow_estimated

    async def admit(
        self,
        provider: LlmProvider,
        request: ModelRequest,
        *,
        attempt: ModelAttemptIdentity,
    ) -> LlmAdmission:
        metadata = request.metadata
        try:
            request_session = _read_identity(metadata.get("session_id"))
            turn_id = _read_identity(metadata.get("turn_id"))
            step_id = _read_identity(metadata.get("step_id"))
        except Exception:
            raise BudgetEvidenceError from None
        if (
            request_session != self._session_id
            or attempt.session_id != self._session_id
            or attempt.turn_id != turn_id
            or attempt.step_id != step_id
        ):
            raise BudgetDirectoryMismatchError
        await _require_agent_session(
            self._service.store,
            agent_id=self._agent_id,
            session_id=self._session_id,
        )
        reservation_id = model_attempt_reservation_id(attempt)
        ledger = await self._service.ledger()
        if ledger.usage_reservation(reservation_id) is not None:
            raise BudgetReservationStateError
        available = ledger.available(self._agent_id)
        remaining = available.max_tokens
        if remaining is None:
            inner = await self._inner.admit(
                provider,
                request,
                attempt=attempt,
            )
            return require_llm_admission_binding(
                inner,
                provider=provider,
                attempt=attempt,
            )
        if remaining == 0:
            raise BudgetExhaustedError("max_tokens")

        bounded_request, reservation_amount = self._bounded_request(
            request, remaining
        )
        if attempt.ordinal > 1 and bounded_request != request:
            # Ordinal one freezes the provider-bound request. A later Attempt
            # either reserves that exact request or does not exist; it cannot
            # buy another call by silently lowering the output ceiling.
            raise BudgetExhaustedError("max_tokens")
        reserve_operation = budget_operation_id(
            "model-token-reserve", reservation_id=reservation_id
        )
        try:
            reservation = await self._service.reserve_usage(
                operation_id=reserve_operation,
                reservation_id=reservation_id,
                agent_id=self._agent_id,
                amounts=BudgetAmounts(tokens=reservation_amount),
            )
        except BaseException as error:
            await _release_pending_usage(
                self._service,
                reservation_id=reservation_id,
                operation_kind="model-token-release",
                primary=error,
            )
            raise
        if reservation.status is not BudgetUsageReservationStatus.PENDING:
            raise BudgetReservationStateError
        try:
            inner = await self._inner.admit(
                provider,
                bounded_request,
                attempt=attempt,
            )
            inner = require_llm_admission_binding(
                inner,
                provider=provider,
                attempt=attempt,
            )
            inner._bind_accounting(
                _BudgetedLlmAccounting(self, reservation_id=reservation_id),
                reservation_id=reservation_id,
            )
        except BaseException as error:
            await _release_pending_usage(
                self._service,
                reservation_id=reservation_id,
                operation_kind="model-token-release",
                primary=error,
            )
            raise
        return inner

    def _bounded_request(
        self, request: ModelRequest, remaining: int
    ) -> tuple[ModelRequest, int]:
        requested = request.max_output_tokens
        if requested is not None and (type(requested) is not int or requested < 1):
            raise BudgetInputError("budget-output-limit-invalid", "max_output_tokens")
        if self._token_counter is None:
            output_limit = min(remaining, requested or remaining)
            return replace(request, max_output_tokens=output_limit), remaining
        try:
            input_tokens = self._token_counter.count_request(request)
        except Exception:
            raise BudgetInputError("budget-token-counter-failed", "token_counter") from None
        if type(input_tokens) is not int or input_tokens < 0:
            raise BudgetInputError("budget-token-counter-invalid", "token_counter")
        output_capacity = remaining - input_tokens
        if output_capacity <= 0:
            raise BudgetExhaustedError("max_tokens")
        output_limit = min(output_capacity, requested or output_capacity)
        return replace(request, max_output_tokens=output_limit), input_tokens + output_limit

    def _usage_settlement(
        self, usage: Usage, reserved: int
    ) -> tuple[int, UsageQuality]:
        try:
            input_tokens = usage.input_tokens
            output_tokens = usage.output_tokens
            quality = usage.quality
            valid = (
                type(input_tokens) is int
                and 0 <= input_tokens <= MAX_BUDGET_VALUE
                and type(output_tokens) is int
                and 0 <= output_tokens <= MAX_BUDGET_VALUE
                and input_tokens + output_tokens <= MAX_BUDGET_VALUE
                and type(quality) is UsageQuality
            )
        except Exception:
            return reserved, UsageQuality.UNKNOWN
        if not valid:
            return reserved, UsageQuality.UNKNOWN
        total = input_tokens + output_tokens
        if total > reserved:
            return reserved, UsageQuality.UNKNOWN
        if quality is UsageQuality.EXACT:
            return total, UsageQuality.EXACT
        if quality is UsageQuality.ESTIMATED and self._allow_estimated:
            return total, UsageQuality.ESTIMATED
        return reserved, UsageQuality.UNKNOWN

    async def _settle_after_outcome(
        self,
        reservation_id: str,
        tokens: int,
        quality: UsageQuality,
        primary: BaseException | None,
    ) -> None:
        await _finish_owned(
            self._service.settle_usage(
                operation_id=budget_operation_id(
                    "model-token-settle", reservation_id=reservation_id
                ),
                reservation_id=reservation_id,
                amounts=BudgetAmounts(tokens=tokens),
                usage_quality=quality,
            ),
            primary=primary,
            name="traceh-budget-token-settle",
            failure_message="model outcome and token settlement both failed",
        )


class BudgetedAgentExecution:
    """Turn preflight, wall deadline and settlement around one execution."""

    __slots__ = (
        "_agent_id",
        "_execution",
        "_service",
        "_session_id",
        "_steps",
    )

    def __init__(
        self,
        execution: AgentExecution,
        service: BudgetLedgerService,
        *,
        agent_id: str,
        session_id: str,
        steps: BudgetContinuationRuntime,
    ) -> None:
        if execution.session_id != session_id:
            raise BudgetDirectoryMismatchError
        if durable_log_identity(execution.event_store) is not durable_log_identity(
            service.store
        ):
            raise BudgetDirectoryMismatchError
        self._execution = execution
        self._service = service
        self._agent_id = agent_id
        self._session_id = session_id
        self._steps = steps

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def event_store(self) -> EventStore:
        return self._execution.event_store

    async def run_turn(self, turn_input: TurnInput) -> TurnResult:
        await self._steps.preflight()
        await _require_agent_session(
            self._service.store,
            agent_id=self._agent_id,
            session_id=self._session_id,
        )
        remaining = (await self._service.ledger()).available(
            self._agent_id
        ).max_wall_milliseconds
        if remaining == 0:
            raise BudgetExhaustedError("max_wall_milliseconds")

        wall_reservation_id: str | None = None
        wall_reserved = 0
        if remaining is not None:
            wall_reservation_id = budget_operation_id(
                "turn-wall-reservation",
                agent_id=self._agent_id,
                session_id=self._session_id,
                message_id=turn_input.message_id,
            )
            try:
                reservation = await self._service.reserve_usage(
                    operation_id=budget_operation_id(
                        "turn-wall-reserve",
                        reservation_id=wall_reservation_id,
                    ),
                    reservation_id=wall_reservation_id,
                    agent_id=self._agent_id,
                    amounts=BudgetAmounts(wall_milliseconds=remaining),
                )
            except BaseException as error:
                await _release_pending_usage(
                    self._service,
                    reservation_id=wall_reservation_id,
                    operation_kind="turn-wall-release",
                    primary=error,
                )
                raise
            if reservation.status is not BudgetUsageReservationStatus.PENDING:
                raise BudgetReservationStateError
            reservation = await _start_reserved_usage(
                self._service,
                reservation_id=wall_reservation_id,
                start_operation_kind="turn-wall-start",
                release_operation_kind="turn-wall-release",
                settle_operation_kind="turn-wall-settle",
                usage_quality=None,
            )
            wall_reserved = reservation.amounts.wall_milliseconds

        started_ns = time.monotonic_ns()
        result: TurnResult | None = None
        primary: BaseException | None = None
        try:
            if wall_reservation_id is None:
                result = await self._execution.run_turn(turn_input)
            else:
                timeout = asyncio.timeout(wall_reserved / 1000)
                try:
                    async with timeout:
                        result = await self._execution.run_turn(turn_input)
                except TimeoutError:
                    if timeout.expired():
                        raise BudgetExhaustedError(
                            "max_wall_milliseconds"
                        ) from None
                    raise
        except BaseException as error:
            primary = error

        elapsed = min(
            MAX_BUDGET_VALUE,
            max(1, math.ceil((time.monotonic_ns() - started_ns) / 1_000_000)),
        )
        await _finish_owned(
            self._finalize_turn(
                wall_reservation_id=wall_reservation_id,
                elapsed=(
                    min(elapsed, wall_reserved)
                    if wall_reservation_id is not None
                    else elapsed
                ),
            ),
            primary=primary,
            name="traceh-budget-turn-finalize",
            failure_message="Turn outcome and Budget settlement both failed",
        )
        if primary is not None:
            raise primary
        assert result is not None
        return result

    async def _finalize_turn(
        self,
        *,
        wall_reservation_id: str | None,
        elapsed: int,
    ) -> None:
        failures: list[BaseException] = []
        try:
            await self._steps.reconcile()
        except BaseException as error:
            failures.append(error)
        if wall_reservation_id is not None:
            try:
                await self._service.settle_usage(
                    operation_id=budget_operation_id(
                        "turn-wall-settle",
                        reservation_id=wall_reservation_id,
                    ),
                    reservation_id=wall_reservation_id,
                    amounts=BudgetAmounts(wall_milliseconds=elapsed),
                    usage_quality=None,
                )
            except BaseException as error:
                failures.append(error)
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup("Budget Turn finalization failed", failures)

    async def cancel_turn(self, *, reason: str) -> bool:
        return await self._execution.cancel_turn(reason=reason)

    async def dispose(self) -> None:
        await self._execution.dispose()


class BudgetEnforcement:
    """One explicit per-Agent assembly of all durable execution gates."""

    __slots__ = (
        "_agent_id",
        "_continuation",
        "_llm_runtime",
        "_service",
        "_session_id",
        "_tool_admission_gate",
    )

    def __init__(
        self,
        service: BudgetLedgerService,
        *,
        agent_id: str,
        session_id: str,
        continuation: ContinuationRuntime,
        llm_runtime: LlmRuntime | None = None,
        token_counter: TokenCounter | None = None,
        allow_estimated_usage: bool = False,
    ) -> None:
        self._service = service
        self._agent_id = agent_id
        self._session_id = session_id
        self._continuation = BudgetContinuationRuntime(
            service,
            agent_id=agent_id,
            session_id=session_id,
            inner=continuation,
        )
        self._llm_runtime = BudgetedLlmRuntime(
            service,
            agent_id=agent_id,
            session_id=session_id,
            inner=llm_runtime,
            token_counter=token_counter,
            allow_estimated=allow_estimated_usage,
        )
        self._tool_admission_gate = BudgetToolAdmissionGate(
            service,
            agent_id=agent_id,
            session_id=session_id,
        )

    @property
    def service(self) -> BudgetLedgerService:
        return self._service

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def continuation(self) -> BudgetContinuationRuntime:
        return self._continuation

    @property
    def llm_runtime(self) -> BudgetedLlmRuntime:
        return self._llm_runtime

    @property
    def tool_admission_gate(self) -> BudgetToolAdmissionGate:
        return self._tool_admission_gate

    def wrap(self, execution: AgentRuntimeExecution) -> BudgetedAgentExecution:
        """Verify the runtime uses this exact bundle, then wrap its Turn seam."""

        runtime = execution.runtime
        if runtime.loop.continuation is not self._continuation:
            raise RuntimeError("runtime does not use this Budget continuation")
        if runtime.loop.llm_runtime is not self._llm_runtime:
            raise RuntimeError("runtime does not use this Budget LLM boundary")
        current = runtime.loop.compositions.current_generation
        if current.tools.admission_gate is not self._tool_admission_gate:
            raise RuntimeError("runtime does not use this Budget Tool gate")
        return BudgetedAgentExecution(
            execution,
            self._service,
            agent_id=self._agent_id,
            session_id=self._session_id,
            steps=self._continuation,
        )


__all__ = [
    "BudgetContinuationRuntime",
    "BudgetEnforcement",
    "BudgetToolAdmissionGate",
    "BudgetedAgentExecution",
    "BudgetedLlmRuntime",
    "TokenCounter",
]
