"""Budget adapters for child creation and process-local Activation slots."""

from __future__ import annotations

import asyncio
from typing import Protocol

from traceh.agents.directory import AgentDirectoryReader
from traceh.agents.identity import freeze_agent_spec
from traceh.api.agents import (
    AgentHandle,
    AgentMessage,
    AgentRecord,
    AgentRunReport,
    AgentSpec,
    AgentSupervisor,
    MessageReceipt,
    MessageTarget,
)
from traceh.api.budgets import (
    BudgetLimits,
    BudgetReservation,
    BudgetReservationStatus,
)
from traceh.budgets.enforcement import budget_operation_id
from traceh.budgets.errors import (
    BudgetDirectoryMismatchError,
    BudgetExhaustedError,
    BudgetReservationStateError,
    BudgetWriteError,
)
from traceh.budgets.events import freeze_limits
from traceh.budgets.service import BudgetLedgerService
from traceh.concurrency import await_worker_convergence
from traceh.session.event_store import EventStore
from traceh.supervision.delivery_identity import require_delivery_identifier
from traceh.supervision.execution import (
    AgentActivationFactory,
    AgentExecution,
    durable_log_identity,
)
from traceh.supervision.lifecycle import AgentOwnershipGraph


class ChildBudgetPolicy(Protocol):
    """Host-only authority for resolving one child's complete grant."""

    def limits_for_child(
        self, *, parent: AgentRecord, child: AgentSpec
    ) -> BudgetLimits:
        ...


async def _converge(coro, *, name: str) -> object:
    task = asyncio.create_task(coro, name=name)
    cancellation: asyncio.CancelledError | None = None
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as error:
        cancellation = error
        await await_worker_convergence(task)
    if task.cancelled():
        assert cancellation is not None
        raise cancellation
    error = task.exception()
    if error is not None:
        if cancellation is not None:
            raise cancellation from error
        raise error
    if cancellation is not None:
        raise cancellation
    return task.result()


class BudgetedAgentSupervisor:
    """Reserve a host grant before the existing Supervisor creates a child."""

    __slots__ = (
        "_closed",
        "_inner",
        "_lock",
        "_policy",
        "_reader",
        "_service",
    )

    def __init__(
        self,
        inner: AgentSupervisor,
        service: BudgetLedgerService,
        *,
        child_budget_policy: ChildBudgetPolicy,
    ) -> None:
        if durable_log_identity(inner.store) is not durable_log_identity(service.store):
            raise BudgetDirectoryMismatchError
        self._inner = inner
        self._service = service
        self._policy = child_budget_policy
        self._reader = AgentDirectoryReader(service.store)
        self._closed = False
        # This is the managed cross-stream saga boundary required by ADR-0026.
        # It does not replace either stream's CAS; it prevents this host from
        # releasing a hold while another managed call is creating that identity.
        self._lock = asyncio.Lock()

    @property
    def store(self) -> EventStore:
        return self._inner.store

    async def create(
        self,
        spec: AgentSpec,
        *,
        request_id: str,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> AgentHandle:
        frozen = freeze_agent_spec(spec)
        request_id = require_delivery_identifier(request_id, field="request_id")
        agent_id = (
            require_delivery_identifier(agent_id, field="agent_id")
            if agent_id is not None
            else None
        )
        session_id = (
            require_delivery_identifier(session_id, field="session_id")
            if session_id is not None
            else None
        )
        async with self._lock:
            if self._closed:
                raise RuntimeError("the managed Agent Supervisor is closed")
            if frozen.owner_agent_id is None:
                return await self._inner.create(
                    frozen,
                    request_id=request_id,
                    agent_id=agent_id,
                    session_id=session_id,
                )
            assigned_agent_id = agent_id or budget_operation_id(
                "managed-child-agent",
                owner_agent_id=frozen.owner_agent_id,
                request_id=request_id,
                preset=frozen.preset,
                workspace_id=frozen.workspace_id,
                forked_from_session_id=frozen.forked_from_session_id,
                capability_grants=frozen.capability_grants,
            )
            reservation_id = budget_operation_id(
                "child-reservation",
                parent_agent_id=frozen.owner_agent_id,
                child_agent_id=assigned_agent_id,
                creation_request_id=request_id,
            )
            directory = await self._reader.load()
            parent = directory.get(frozen.owner_agent_id)
            if parent is None:
                raise BudgetDirectoryMismatchError
            child_limits = freeze_limits(
                self._policy.limits_for_child(parent=parent, child=frozen),
                field="child_limits",
            )
            await self._reserve_child_for_create(
                reservation_id=reservation_id,
                parent_agent_id=parent.agent_id,
                child_agent_id=assigned_agent_id,
                request_id=request_id,
                child_limits=child_limits,
            )
            try:
                handle = await self._inner.create(
                    frozen,
                    request_id=request_id,
                    agent_id=assigned_agent_id,
                    session_id=session_id,
                )
            except BaseException as error:
                await self._finish_create(
                    reservation_id=reservation_id,
                    parent_agent_id=parent.agent_id,
                    child_agent_id=assigned_agent_id,
                    request_id=request_id,
                    child_limits=child_limits,
                    reservation_required=True,
                    primary=error,
                )
                raise
            await self._finish_create(
                reservation_id=reservation_id,
                parent_agent_id=parent.agent_id,
                child_agent_id=assigned_agent_id,
                request_id=request_id,
                child_limits=child_limits,
                reservation_required=True,
                primary=None,
            )
            return handle

    async def _reserve_child_for_create(
        self,
        *,
        reservation_id: str,
        parent_agent_id: str,
        child_agent_id: str,
        request_id: str,
        child_limits: BudgetLimits,
    ) -> BudgetReservation:
        reserve_task = asyncio.create_task(
            self._service.reserve_child(
                operation_id=budget_operation_id(
                    "child-reserve", reservation_id=reservation_id
                ),
                reservation_id=reservation_id,
                parent_agent_id=parent_agent_id,
                child_agent_id=child_agent_id,
                creation_request_id=request_id,
                child_limits=child_limits,
            ),
            name="traceh-budget-child-reserve",
        )
        cancellation: asyncio.CancelledError | None = None
        try:
            await asyncio.shield(reserve_task)
        except asyncio.CancelledError as error:
            cancellation = error
            await await_worker_convergence(reserve_task)

        if reserve_task.cancelled():
            primary = cancellation or asyncio.CancelledError()
            await self._finish_create(
                reservation_id=reservation_id,
                parent_agent_id=parent_agent_id,
                child_agent_id=child_agent_id,
                request_id=request_id,
                child_limits=child_limits,
                reservation_required=False,
                primary=primary,
            )
            raise primary

        failure = reserve_task.exception()
        if failure is not None:
            if isinstance(failure, BudgetWriteError) and failure.committed is True:
                await self._finish_create(
                    reservation_id=reservation_id,
                    parent_agent_id=parent_agent_id,
                    child_agent_id=child_agent_id,
                    request_id=request_id,
                    child_limits=child_limits,
                    reservation_required=False,
                    primary=cancellation or failure,
                )
            if cancellation is not None:
                raise cancellation from failure
            raise failure

        if cancellation is not None:
            await self._finish_create(
                reservation_id=reservation_id,
                parent_agent_id=parent_agent_id,
                child_agent_id=child_agent_id,
                request_id=request_id,
                child_limits=child_limits,
                reservation_required=True,
                primary=cancellation,
            )
            raise cancellation

        reservation = reserve_task.result()
        if reservation.status is BudgetReservationStatus.RELEASED:
            # Replaying the reserve fact is idempotent, but a terminal release
            # is not a new permission to repeat the external create effect.
            # Reject it before the inner Supervisor can persist child identity.
            raise BudgetReservationStateError
        if reservation.status not in (
            BudgetReservationStatus.PENDING,
            BudgetReservationStatus.COMMITTED,
        ):
            raise BudgetReservationStateError
        return reservation

    async def _finish_create(
        self,
        *,
        reservation_id: str,
        parent_agent_id: str,
        child_agent_id: str,
        request_id: str,
        child_limits: BudgetLimits,
        reservation_required: bool,
        primary: BaseException | None,
    ) -> None:
        async def reconcile() -> None:
            ledger = await self._service.ledger()
            reservation = ledger.reservation(reservation_id)
            if reservation is None:
                if reservation_required:
                    raise BudgetDirectoryMismatchError
                return
            if (
                reservation.parent_agent_id != parent_agent_id
                or reservation.child_agent_id != child_agent_id
                or reservation.creation_request_id != request_id
                or reservation.child_limits != child_limits
            ):
                raise BudgetDirectoryMismatchError
            directory = await self._reader.load()
            by_child = directory.get(child_agent_id)
            by_request = directory.for_request(request_id)
            exact = (
                by_child is not None
                and by_request is not None
                and by_child.agent_id == by_request.agent_id == child_agent_id
                and by_child.request_id == request_id
                and by_child.owner_agent_id == parent_agent_id
            )
            absent = by_child is None and by_request is None
            if exact:
                finalizer = self._service.commit_reservation(
                    operation_id=budget_operation_id(
                        "child-commit", reservation_id=reservation_id
                    ),
                    reservation_id=reservation_id,
                )
            elif absent:
                finalizer = self._service.release_reservation(
                    operation_id=budget_operation_id(
                        "child-release", reservation_id=reservation_id
                    ),
                    reservation_id=reservation_id,
                    creation_converged=True,
                )
            else:
                raise BudgetDirectoryMismatchError
            await finalizer

        try:
            await _converge(
                reconcile(), name="traceh-budget-child-reconcile"
            )
        except BaseException as final_error:
            if primary is None:
                raise
            if isinstance(primary, asyncio.CancelledError):
                raise primary from final_error
            raise BaseExceptionGroup(
                "Agent creation and Budget reconciliation both failed",
                (primary, final_error),
            ) from None

    async def resume(self, session_id: str) -> AgentHandle:
        return await self._inner.resume(session_id)

    async def send(
        self,
        agent_id: str,
        message: AgentMessage,
        *,
        target: MessageTarget,
        wakeup: bool,
    ) -> MessageReceipt:
        return await self._inner.send(
            agent_id, message, target=target, wakeup=wakeup
        )

    async def interrupt(self, agent_id: str, reason: str = "interrupted") -> bool:
        return await self._inner.interrupt(agent_id, reason)

    async def wait_idle(self, agent_id: str) -> None:
        await self._inner.wait_idle(agent_id)

    async def wait_message(
        self, agent_id: str, message_id: str
    ) -> AgentRunReport:
        return await self._inner.wait_message(agent_id, message_id)

    async def report(self, agent_id: str, message_id: str) -> AgentRunReport:
        return await self._inner.report(agent_id, message_id)

    async def dispose(self, agent_id: str) -> None:
        await self._inner.dispose(agent_id)

    async def aclose(self) -> None:
        async with self._lock:
            self._closed = True
            await self._inner.aclose()


class ProcessSlotLease:
    """One idempotently releasable set of ancestor process slots."""

    __slots__ = ("_authority", "_release_task", "_token")

    def __init__(self, authority: ProcessSlotAuthority, token: object) -> None:
        self._authority = authority
        self._token = token
        self._release_task: asyncio.Task[None] | None = None

    async def release(self) -> None:
        if self._release_task is None:
            self._release_task = asyncio.create_task(
                self._authority._release(self._token),
                name="traceh-budget-process-slot-release",
            )
        task = self._release_task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as cancelled:
            await await_worker_convergence(task)
            if not task.cancelled():
                failure = task.exception()
                if failure is not None:
                    raise cancelled from failure
            raise cancelled


class ProcessSlotAuthority:
    """Process-local descendant slots shared by one explicit host root."""

    __slots__ = ("_held", "_lock", "_reader", "_service")

    def __init__(self, service: BudgetLedgerService) -> None:
        self._service = service
        self._reader = AgentDirectoryReader(service.store)
        self._held: dict[object, tuple[str, ...]] = {}
        self._lock = asyncio.Lock()

    @property
    def store(self) -> EventStore:
        return self._service.store

    async def acquire_existing(self, agent_id: str) -> ProcessSlotLease:
        return await self._acquire(
            agent_id=agent_id,
            owner_agent_id=None,
            existing=True,
        )

    async def acquire_new(
        self, *, agent_id: str, owner_agent_id: str | None
    ) -> ProcessSlotLease:
        return await self._acquire(
            agent_id=agent_id,
            owner_agent_id=owner_agent_id,
            existing=False,
        )

    async def _acquire(
        self,
        *,
        agent_id: str,
        owner_agent_id: str | None,
        existing: bool,
    ) -> ProcessSlotLease:
        async with self._lock:
            directory = await self._reader.load()
            graph = AgentOwnershipGraph(directory)
            if existing:
                lineage = graph.lineage(agent_id)
            elif owner_agent_id is None:
                # A root is provisioned before its durable Directory fact is
                # appended. It has no ancestor capacity to consume, so its
                # pre-commit lineage is the candidate root alone.
                lineage = (agent_id,)
            else:
                lineage = graph.lineage_for_new(agent_id, owner_agent_id)
            if not lineage:
                raise BudgetDirectoryMismatchError
            ancestors = lineage[:-1]
            ledger = await self._service.ledger()
            counts: dict[str, int] = {}
            for held in self._held.values():
                for ancestor in held:
                    counts[ancestor] = counts.get(ancestor, 0) + 1
            charged_ancestors: list[str] = []
            for ancestor in ancestors:
                account = ledger.require_open_account(ancestor)
                limit = account.limits.max_processes
                if limit is None:
                    continue
                if counts.get(ancestor, 0) >= limit:
                    raise BudgetExhaustedError("max_processes")
                charged_ancestors.append(ancestor)
            token = object()
            self._held[token] = tuple(charged_ancestors)
            return ProcessSlotLease(self, token)

    async def _release(self, token: object) -> None:
        async with self._lock:
            self._held.pop(token, None)

    async def held(self, ancestor_agent_id: str) -> int:
        async with self._lock:
            return sum(
                ancestor_agent_id in ancestors
                for ancestors in self._held.values()
            )


class _SlotLeasedExecution:
    __slots__ = ("_dispose_task", "_execution", "_lease")

    def __init__(self, execution: AgentExecution, lease: ProcessSlotLease) -> None:
        self._execution = execution
        self._lease = lease
        self._dispose_task: asyncio.Task[None] | None = None

    @property
    def session_id(self) -> str:
        return self._execution.session_id

    @property
    def event_store(self) -> EventStore:
        return self._execution.event_store

    async def run_turn(self, turn_input):
        return await self._execution.run_turn(turn_input)

    async def cancel_turn(self, *, reason: str) -> bool:
        return await self._execution.cancel_turn(reason=reason)

    async def dispose(self) -> None:
        if self._dispose_task is None:
            self._dispose_task = asyncio.create_task(
                self._dispose(), name="traceh-budget-slot-execution-dispose"
            )
        task = self._dispose_task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as cancelled:
            await await_worker_convergence(task)
            if not task.cancelled():
                failure = task.exception()
                if failure is not None:
                    raise cancelled from failure
            raise cancelled

    async def _dispose(self) -> None:
        failures: list[BaseException] = []
        try:
            await self._execution.dispose()
        except BaseException as error:
            failures.append(error)
        try:
            await self._lease.release()
        except BaseException as error:
            failures.append(error)
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup("execution and process-slot release failed", failures)


class BudgetedActivationFactory:
    """Acquire ancestor slots around an existing Activation factory."""

    __slots__ = ("_inner", "_slots")

    def __init__(
        self,
        inner: AgentActivationFactory,
        slots: ProcessSlotAuthority,
    ) -> None:
        self._inner = inner
        self._slots = slots

    async def provision(
        self,
        spec: AgentSpec,
        *,
        agent_id: str,
        session_id: str | None,
    ) -> AgentExecution:
        lease = await self._slots.acquire_new(
            agent_id=agent_id, owner_agent_id=spec.owner_agent_id
        )
        try:
            execution = await self._inner.provision(
                spec, agent_id=agent_id, session_id=session_id
            )
        except BaseException as error:
            await self._release_after_failure(lease, error)
            raise
        return await self._bind(execution, lease)

    async def activate(self, record: AgentRecord) -> AgentExecution:
        lease = await self._slots.acquire_existing(record.agent_id)
        try:
            execution = await self._inner.activate(record)
        except BaseException as error:
            await self._release_after_failure(lease, error)
            raise
        return await self._bind(execution, lease)

    async def _bind(
        self,
        execution: AgentExecution,
        lease: ProcessSlotLease,
    ) -> AgentExecution:
        wrapped = _SlotLeasedExecution(execution, lease)
        if durable_log_identity(execution.event_store) is durable_log_identity(
            self._slots.store
        ):
            return wrapped
        primary = BudgetDirectoryMismatchError()
        try:
            await _converge(
                wrapped.dispose(),
                name="traceh-budget-mismatched-activation-cleanup",
            )
        except BaseException as cleanup_error:
            raise BaseExceptionGroup(
                "Activation identity mismatch and cleanup both failed",
                (primary, cleanup_error),
            ) from None
        raise primary

    @staticmethod
    async def _release_after_failure(
        lease: ProcessSlotLease, primary: BaseException
    ) -> None:
        try:
            await _converge(
                lease.release(), name="traceh-budget-process-slot-rollback"
            )
        except BaseException as cleanup_error:
            if isinstance(primary, asyncio.CancelledError):
                raise primary from cleanup_error
            raise BaseExceptionGroup(
                "activation failed and process-slot rollback failed",
                (primary, cleanup_error),
            ) from None


__all__ = [
    "BudgetedActivationFactory",
    "BudgetedAgentSupervisor",
    "ChildBudgetPolicy",
    "ProcessSlotAuthority",
    "ProcessSlotLease",
]
