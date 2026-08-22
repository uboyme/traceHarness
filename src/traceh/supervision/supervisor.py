"""A process-local Agent Supervisor.

This is the first thing in the repository that *runs* an Agent. Stage A made
identity durable, Stage B made acceptance durable; this turns a durably
accepted message into a durable claim, a real Turn on that Agent's own Session,
and a durable terminal outcome.

Four things are kept apart and must not be confused:

* **Identity** - `AgentRecord`, from ``agents:directory``. Which Agents exist.
* **Acceptance** - `AcceptedMessage`, from ``agent-inbox:<id>``. What arrived,
  in what order. Accepted is still not processed.
* **Delivery lifecycle** - claims and outcomes, from ``agent-delivery:<id>``.
  What has been taken for execution and how it ended.
* **Activation** - the live, in-process object below. It owns a Task and an
  execution runtime, can be disposed and rebuilt, and is *not* an identity.

Only the first three survive a restart. The Activation is deliberately
reconstructible from them and never the other way round.

It lives outside `traceh.runtime` on purpose: `AgentRuntime` is one Agent's
execution facade, and folding scheduling, claims and an Inbox drain into it
would make it the thing this repository has spent three ADRs avoiding. The
dependency points one way - the Supervisor uses a narrow execution protocol,
and neither `AgentRuntime` nor `AgentLoop` knows it exists.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from uuid import uuid4

from traceh.agents.errors import (
    AgentOwnerNotFoundError,
    AgentRequestConflictError,
    AgentUnknownError,
)
from traceh.agents.identity import agent_spec_request_fingerprint, freeze_agent_spec
from traceh.agents.inbox import AgentInboxReader
from traceh.agents.inbox_service import AgentInboxService
from traceh.agents.registrar import AgentRegistrar
from traceh.api.agents import (
    AcceptedMessage,
    AgentMessage,
    AgentRecord,
    AgentRunReport,
    AgentSpec,
    MessageReceipt,
    MessageTarget,
)
from traceh.api.turns import TurnInput
from traceh.concurrency import await_worker_convergence
from traceh.session.event_store import EventStore
from traceh.session.service import SessionNotFoundError, SessionService
from traceh.supervision.delivery import MessageClaim
from traceh.supervision.delivery_identity import (
    require_delivery_identifier,
    require_delivery_reason,
)
from traceh.supervision.delivery_service import AgentDeliveryService
from traceh.supervision.errors import (
    ActivationConflictError,
    ActivationFaultedError,
    AgentMessageNotSettledError,
    AgentNotActiveError,
    AgentOwnerNotActiveError,
    DeliveryAppendError,
    DeliveryConflictError,
    ExecutionSessionMismatchError,
    ExecutionStoreMismatchError,
    MessageWakeError,
    SupervisionError,
    SupervisorDisposedError,
    UnsupportedMessageTargetError,
)
from traceh.supervision.execution import (
    AgentActivationFactory,
    AgentExecution,
    durable_log_identity,
)
from traceh.supervision.lifecycle import (
    AgentLifecycleCoordinator,
    AgentOwnershipGraph,
    AgentOwnershipGraphError,
    LifecycleAdmissionClosed,
)
from traceh.supervision.reports import AgentRunReportReader


class AgentNotFoundError(SupervisionError):
    """No durable Agent matches the identity that was asked for."""

    code = "agent-not-found"

    def __init__(self) -> None:
        super().__init__("no registered agent matches that identity")


@dataclass(frozen=True, slots=True)
class SupervisedAgentHandle:
    """A live Activation's addresses.

    Satisfies the `AgentHandle` protocol. It is a set of ids, deliberately not
    a control surface: everything that acts on an Agent goes through the
    Supervisor, so a stale handle cannot drive a disposed Activation.
    """

    agent_id: str
    session_id: str
    activation_id: str


@dataclass(frozen=True, slots=True)
class _CreateRequest:
    """The frozen meaning of one in-process creation single-flight."""

    spec: AgentSpec
    fingerprint: str
    requested_agent_id: str | None
    assigned_agent_id: str
    requested_session_id: str | None

    def joins(self, other: _CreateRequest) -> bool:
        return (
            self.fingerprint == other.fingerprint
            and self.requested_agent_id == other.requested_agent_id
            and self.requested_session_id == other.requested_session_id
        )


@dataclass(slots=True)
class _PendingCreate:
    request: _CreateRequest
    task: asyncio.Task[_CreateOutcome]
    waiters: set[object] = field(default_factory=set)
    delivered: bool = False
    compensation_task: asyncio.Task[None] | None = None


@dataclass(frozen=True, slots=True)
class _CreateOutcome:
    """The exact Activation materialized by one shared create operation."""

    activation: _Activation


@dataclass(frozen=True, slots=True)
class _CancelledCreate:
    """A cancelled waiter after the shared create has converged.

    The sentinel crosses the lifecycle admission boundary before cleanup is
    attempted.  Disposing while still holding that Agent's admission lease
    would deadlock against subtree quiescence.
    """

    error: asyncio.CancelledError
    compensation_task: asyncio.Task[None] | None


@dataclass(frozen=True, slots=True)
class _RetryCreate:
    """A caller that arrived while an unreturned create was converging."""

    compensation_task: asyncio.Task[None]


@dataclass(slots=True)
class _CreateCallState:
    """One create invocation, separate from its caller Task container."""

    caller: asyncio.Task[object]
    owned_work: asyncio.Task[object] | None
    exit_started: bool
    returned: asyncio.Future[None]


_MESSAGE_REPORT_POLL_SECONDS = 0.25


def _failure_leaves(error: BaseException) -> tuple[BaseException, ...]:
    if isinstance(error, BaseExceptionGroup):
        return tuple(
            leaf
            for nested in error.exceptions
            for leaf in _failure_leaves(nested)
        )
    return (error,)


def _collect_tree_residual_failures(
    failures: list[BaseException],
    error: BaseException,
    cleanup_tasks: set[asyncio.Task[None]],
) -> None:
    """Keep tree-only failures while removing repeated Task observations.

    One subtree Task aggregates the errors of the per-Agent cleanup Tasks it
    joined. Final close joins those Tasks again to prove convergence. The
    duplicate is the *observation of that Task*, not the exception object: two
    independent cleanup Tasks are allowed to raise the very same object and
    both failures must remain visible.
    """

    repeated_by_identity: dict[int, int] = {}
    repeated_cancellations = 0
    for task in cleanup_tasks:
        if not task.done():
            continue
        if task.cancelled():
            repeated_cancellations += 1
            continue
        task_error = task.exception()
        if task_error is None:
            continue
        for leaf in _failure_leaves(task_error):
            identity = id(leaf)
            repeated_by_identity[identity] = repeated_by_identity.get(identity, 0) + 1

    for leaf in _failure_leaves(error):
        identity = id(leaf)
        remaining = repeated_by_identity.get(identity, 0)
        if remaining:
            repeated_by_identity[identity] = remaining - 1
        elif isinstance(leaf, asyncio.CancelledError) and repeated_cancellations:
            repeated_cancellations -= 1
        else:
            failures.append(leaf)


class _Activation:
    """One Agent's live worker and exclusive execution runtime."""

    __slots__ = (
        "_abandonment_task",
        "_current_claim",
        "_dispose_task",
        "_execution",
        "_fault",
        "_idle",
        "_message_waiters",
        "_retained",
        "_state_lock",
        "_stopping",
        "_supervisor",
        "_wake",
        "_worker",
        "activation_id",
        "agent_id",
        "session_id",
    )

    def __init__(
        self,
        *,
        agent_id: str,
        session_id: str,
        activation_id: str,
        execution: AgentExecution,
        supervisor: ProcessAgentSupervisor,
        abandonable: bool,
    ) -> None:
        self.agent_id = agent_id
        self.session_id = session_id
        self.activation_id = activation_id
        self._execution = execution
        self._supervisor = supervisor
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._message_waiters: dict[str, set[asyncio.Future[None]]] = {}
        # A fresh create is not yet an externally retained resource. The first
        # successful create delivery, resume or wakeup atomically flips this
        # bit under the Supervisor lock; abandonment may win that same race
        # exactly once and publishes its cleanup Task instead.
        self._retained = not abandonable
        self._abandonment_task: asyncio.Task[None] | None = None
        self._state_lock = asyncio.Lock()
        self._worker: asyncio.Task[None] | None = None
        self._dispose_task: asyncio.Task[None] | None = None
        self._fault: str | None = None
        self._stopping = False
        self._current_claim: MessageClaim | None = None

    @property
    def execution(self) -> AgentExecution:
        return self._execution

    @property
    def fault(self) -> str | None:
        return self._fault

    @property
    def stopping(self) -> bool:
        return self._stopping

    def handle(self) -> SupervisedAgentHandle:
        return SupervisedAgentHandle(
            agent_id=self.agent_id,
            session_id=self.session_id,
            activation_id=self.activation_id,
        )

    def start(self) -> None:
        self._worker = asyncio.create_task(
            self._run(), name=f"traceh-agent-worker-{self.agent_id}"
        )

    async def request_wake(self) -> None:
        """Ask the worker to drain the Inbox.

        ``_wake`` is set and ``_idle`` cleared under one lock, and the worker
        only sets ``_idle`` under that same lock after finding ``_wake`` clear.
        That pairing is what makes a wake un-losable: there is no window where
        a request lands between "I finished draining" and "I am now idle".
        """

        async with self._state_lock:
            if self._stopping:
                raise SupervisorDisposedError()
            if self._fault is not None:
                raise ActivationFaultedError(self._fault)
            self._idle.clear()
            self._wake.set()

    async def wait_idle(self) -> None:
        await self._idle.wait()
        if self._fault is not None:
            raise ActivationFaultedError(self._fault)

    async def interrupt(self, reason: str) -> bool:
        """Cancel the Turn currently running, if any.

        Only the Turn. The Activation stays alive and keeps draining, which is
        what distinguishes an interrupt from a disposal.
        """

        if self._stopping:
            raise SupervisorDisposedError()
        return await self._execution.cancel_turn(reason=reason)

    async def _run(self) -> None:
        try:
            while True:
                await self._wake.wait()
                async with self._state_lock:
                    if self._stopping:
                        return
                    # Cleared *before* draining, never after: a wake arriving
                    # while we work must survive into the next iteration.
                    self._wake.clear()
                await self._drain()
                async with self._state_lock:
                    if not self._wake.is_set():
                        self._idle.set()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Inbox/projector/store failures are control-plane faults, not an
            # idle worker.  Raw third-party text is deliberately discarded;
            # callers receive one stable, terminal-safe code through
            # ``wait_idle`` and future wakes.
            self._set_fault("worker-failed")
        finally:
            # Whatever happened - normal stop, cancellation, or a fault - a
            # waiter must not be left hanging. ``wait_idle`` re-checks the
            # fault after waking, so this releases without claiming success.
            self._idle.set()

    async def _drain(self) -> None:
        while True:
            if self._stopping or self._fault is not None:
                return
            # Re-read every iteration. The Supervisor keeps no in-memory queue:
            # the durable Inbox and the durable delivery log are the only
            # things that decide what runs next, so another process claiming a
            # message is visible here immediately.
            inbox = await self._supervisor.inboxes.load(self.agent_id)
            delivery = await self._supervisor.deliveries.delivery_log(self.agent_id, inbox)
            accepted = delivery.next_unclaimed(inbox)
            if accepted is None:
                return
            if not await self._deliver(accepted, inbox, delivery):
                return

    async def _deliver(self, accepted: AcceptedMessage, inbox, delivery) -> bool:
        """Claim one message and run it. Returns whether draining may continue."""

        try:
            claim = await self._supervisor.deliveries.claim(
                agent_id=self.agent_id,
                accepted=accepted,
                claim_id=str(uuid4()),
                activation_id=self.activation_id,
                session_id=self.session_id,
                inbox=inbox,
                delivery=delivery,
            )
        except DeliveryConflictError:
            # Someone else claimed it. Nothing was written by us and nothing
            # ran; re-read and look at what is next.
            return True
        except DeliveryAppendError as error:
            # A claim we cannot prove is durable is a claim we must not act on:
            # if it did land and we also ran, a second worker seeing no claim
            # would run the same message again.
            self._set_fault(
                "claim-not-durable" if error.committed is None else "claim-append-failed"
            )
            return False

        if accepted.target is not MessageTarget.NEW_TURN:
            # Refused at ``send()``, so this can only arrive through a direct
            # `AgentInboxService` write. It is recorded as a terminal failure
            # rather than skipped, because skipping would silently reorder the
            # FIFO, and rather than faulted, because one undeliverable message
            # should not stop the rest.
            await self._record_terminal(
                lambda: self._supervisor.deliveries.fail(
                    agent_id=self.agent_id, claim=claim, error_code="unsupported-target"
                ),
                message_id=claim.message_id,
            )
            return self._fault is None

        self._current_claim = claim
        try:
            result = await self._execution.run_turn(
                TurnInput(
                    content=accepted.message.content,
                    message_id=accepted.message.message_id,
                    source=accepted.message.source,
                )
            )
        except asyncio.CancelledError:
            await self._record_terminal(
                lambda: self._supervisor.deliveries.cancel(
                    agent_id=self.agent_id, claim=claim, reason="turn-cancelled"
                ),
                message_id=claim.message_id,
            )
            self._current_claim = None
            task = asyncio.current_task()
            if task is not None and task.cancelling() > 0:
                # *We* are being cancelled - disposal. Propagate.
                raise
            # Only the Turn was cancelled, by ``interrupt()``. The Activation
            # survives and keeps draining, so the cancellation must not escape
            # and kill this worker.
            return self._fault is None
        except Exception:
            # The exception text is deliberately not persisted: it is arbitrary
            # third-party output that may quote a request, a path or a
            # credential. What happened is in the Session Event Log.
            self._current_claim = None
            await self._record_terminal(
                lambda: self._supervisor.deliveries.fail(
                    agent_id=self.agent_id, claim=claim, error_code="turn-failed"
                ),
                message_id=claim.message_id,
            )
            return self._fault is None
        self._current_claim = None
        await self._record_terminal(
            lambda: self._supervisor.deliveries.complete(
                agent_id=self.agent_id,
                claim=claim,
                turn_id=result.turn_id,
                reason=result.reason,
            ),
            message_id=claim.message_id,
        )
        return self._fault is None

    async def _record_terminal(self, factory, *, message_id: str) -> None:
        """Append a terminal fact, converging even if we are cancelled.

        The Turn already ran. Abandoning the append because a disposal arrived
        would leave a claim with no outcome, which is exactly the state nothing
        in this Stage can repair.
        """

        task = asyncio.create_task(factory(), name=f"traceh-agent-terminal-{self.agent_id}")
        try:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await await_worker_convergence(task)
                if not task.cancelled():
                    self._absorb_terminal_result(task)
                raise
            except Exception:
                self._set_fault("terminal-not-durable")
        finally:
            # This is only an in-process notification. The waiter always
            # re-reads the durable report and never treats the Future as fact.
            self._wake_message_waiters(message_id)

    def _absorb_terminal_result(self, task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:  # pragma: no cover - excluded by caller
            pass
        except Exception:
            self._set_fault("terminal-not-durable")

    def _set_fault(self, code: str) -> None:
        if self._fault is None:
            self._fault = code
        self._wake_message_waiters()

    def register_message_waiter(self, message_id: str) -> asyncio.Future[None]:
        """Register one notification waiter without making it a fact source."""

        if self._fault is not None:
            raise ActivationFaultedError(self._fault)
        if self._stopping:
            raise SupervisorDisposedError()
        waiter = asyncio.get_running_loop().create_future()
        self._message_waiters.setdefault(message_id, set()).add(waiter)
        return waiter

    def remove_message_waiter(
        self, message_id: str, waiter: asyncio.Future[None]
    ) -> None:
        waiters = self._message_waiters.get(message_id)
        if waiters is None:
            return
        waiters.discard(waiter)
        if not waiters:
            self._message_waiters.pop(message_id, None)

    def _wake_message_waiters(self, message_id: str | None = None) -> None:
        if message_id is None:
            groups = tuple(self._message_waiters.values())
            self._message_waiters.clear()
        else:
            groups = (self._message_waiters.pop(message_id, set()),)
        for waiters in groups:
            for waiter in waiters:
                if not waiter.done():
                    waiter.set_result(None)

    async def dispose(self) -> None:
        """Stop this Activation and release its runtime.

        The work belongs to one internal Task rather than to whoever waits for
        it, so a caller's cancellation can interrupt the *waiting* but never
        the shutdown itself. Repeated calls await that same Task, so disposal
        runs exactly once and a second caller sees the same real outcome.
        """

        async with self._state_lock:
            self._stopping = True
            self._wake_message_waiters()
            # Wake the worker so it observes ``_stopping`` and returns instead
            # of parking forever.
            self._wake.set()
            if self._dispose_task is None:
                self._dispose_task = asyncio.create_task(
                    self._shutdown(), name=f"traceh-agent-dispose-{self.agent_id}"
                )
            task = self._dispose_task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await await_worker_convergence(task)
            raise

    async def _shutdown(self) -> None:
        worker = self._worker
        if worker is not None:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
            except Exception:
                # A worker failure is already recorded as a fault; disposal
                # still has to release the runtime.
                self._set_fault("worker-failed")
        await self._execution.dispose()
        self._idle.set()


class ProcessAgentSupervisor:
    """Runs Agents in this process, one Activation each.

    Not a cold-recovery supervisor: it does not scan for Agents at startup and
    does not take over a claim left behind by a crashed process. Both need an
    attempt identity and a recovery policy this Stage deliberately does not
    have, and guessing would run a message twice.
    """

    __slots__ = (
        "_activations",
        "_create_calls",
        "_close_resources_done",
        "_close_task",
        "_deliveries",
        "_disposed",
        "_agent_disposals",
        "_factory",
        "_inbox_service",
        "_inboxes",
        "_lifecycle",
        "_lock",
        "_pending_activations",
        "_pending_creates",
        "_registrar",
        "_reports",
        "_sessions",
        "_session_owner",
        "_store",
        "_tree_cleanup_tasks",
        "_tree_disposals",
    )

    def __init__(self, *, store: EventStore, factory: AgentActivationFactory) -> None:
        self._store = store
        self._factory = factory
        self._registrar = AgentRegistrar(store)
        self._inbox_service = AgentInboxService(store)
        self._inboxes = AgentInboxReader(store)
        self._deliveries = AgentDeliveryService(store)
        self._reports = AgentRunReportReader(store)
        self._sessions = SessionService(store)
        self._activations: dict[str, _Activation] = {}
        self._session_owner: dict[str, str] = {}
        self._pending_activations: dict[str, asyncio.Task[_Activation]] = {}
        self._pending_creates: dict[str, _PendingCreate] = {}
        self._create_calls: dict[object, _CreateCallState] = {}
        self._agent_disposals: dict[str, asyncio.Task[None]] = {}
        self._tree_cleanup_tasks: dict[
            asyncio.Task[None], set[asyncio.Task[None]]
        ] = {}
        self._tree_disposals: dict[str, asyncio.Task[None]] = {}
        self._close_task: asyncio.Task[None] | None = None
        self._close_resources_done = asyncio.Event()
        self._lifecycle = AgentLifecycleCoordinator()
        self._lock = asyncio.Lock()
        self._disposed = False

    # -- read-only collaborators the Activation worker needs -----------------

    @property
    def store(self) -> EventStore:
        return self._store

    @property
    def inboxes(self) -> AgentInboxReader:
        return self._inboxes

    @property
    def deliveries(self) -> AgentDeliveryService:
        return self._deliveries

    @property
    def registrar(self) -> AgentRegistrar:
        return self._registrar

    # -- public control surface ---------------------------------------------

    async def create(
        self,
        spec: AgentSpec,
        *,
        request_id: str,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> SupervisedAgentHandle:
        """Create an Agent while making the full public call close-owned."""

        caller = asyncio.current_task()
        assert caller is not None
        call_token = object()
        state = _CreateCallState(
            caller=caller,
            owned_work=None,
            exit_started=False,
            returned=asyncio.get_running_loop().create_future(),
        )
        async with self._lock:
            self._require_open()
            self._create_calls[call_token] = state
        try:
            # Run inline so lifecycle admission keeps its established
            # linearization. The state object, not the caller Task, is the
            # operation receipt: close may use the Task only to deliver
            # cancellation while this exact call remains registered.
            return await self._create_call(
                spec,
                request_id=request_id,
                agent_id=agent_id,
                session_id=session_id,
                call_state=state,
            )
        finally:
            # From this synchronous, no-await point onward the create call is
            # only transferring control back to its caller.  Cancelling the
            # caller Task could therefore land in unrelated caller work rather
            # than in this operation.  The post-return receipt remains
            # close-owned, but caller cancellation permission ends here.
            state.exit_started = True
            # The completion Task cannot run until this coroutine returns to
            # its caller.  It removes the registration and publishes the
            # method-return receipt under one Supervisor-lock acquisition, so
            # close observes either the live call or its completed receipt --
            # never the old post-unregister/pre-return gap.
            asyncio.create_task(
                self._complete_create_call(call_token, state),
                name="traceh-agent-create-complete",
            )

    async def _create_call(
        self,
        spec: AgentSpec,
        *,
        request_id: str,
        agent_id: str | None = None,
        session_id: str | None = None,
        call_state: _CreateCallState,
    ) -> SupervisedAgentHandle:
        """Create an Agent, its Session and its Activation.

        ``request_id`` is required and stable: it is what makes a retry return
        the same Agent instead of creating a second one, and generating a fresh
        one internally on each attempt would defeat exactly that.

        The order is deliberate - Session first, identity second. There is no
        transaction across two append-only streams, so one of them commits
        first and a failure in between leaves *something* behind. A Session
        with no `AgentRecord` is detectable and inert; an `AgentRecord`
        pointing at a Session that does not exist is a broken identity nothing
        can use. See ADR-0021.
        """

        request_id = require_delivery_identifier(request_id, field="request_id")
        requested_agent_id = (
            require_delivery_identifier(agent_id, field="agent_id")
            if agent_id is not None
            else None
        )
        requested_session_id = (
            require_delivery_identifier(session_id, field="session_id")
            if session_id is not None
            else None
        )
        frozen_spec = freeze_agent_spec(spec)
        request = _CreateRequest(
            spec=frozen_spec,
            fingerprint=agent_spec_request_fingerprint(frozen_spec),
            requested_agent_id=requested_agent_id,
            assigned_agent_id=requested_agent_id or str(uuid4()),
            requested_session_id=requested_session_id,
        )
        while True:
            directory = await self._registrar.directory()
            graph = AgentOwnershipGraph(directory)
            existing = directory.for_request(request_id)
            try:
                lineage = (
                    graph.lineage(existing.agent_id)
                    if existing is not None
                    else graph.lineage_for_new(
                        request.assigned_agent_id, frozen_spec.owner_agent_id
                    )
                )
            except AgentOwnershipGraphError as error:
                if error.code == "agent-owner-unknown":
                    raise AgentOwnerNotFoundError() from error
                raise
            try:
                async with self._lifecycle.admission(lineage):
                    result = await self._create_admitted(
                        request,
                        request_id,
                        call_state,
                    )
            except LifecycleAdmissionClosed as error:
                raise SupervisorDisposedError() from error
            if isinstance(result, _RetryCreate):
                # The compensation Task was registered while the cancelled
                # caller still held admission. Leave our admission before
                # joining it, otherwise subtree quiescence would wait on us.
                await self._await_task(result.compensation_task)
                continue
            if isinstance(result, _CancelledCreate):
                if result.compensation_task is None:
                    raise result.error
                await self._raise_cancel_after_cleanup(
                    result.error,
                    result.compensation_task,
                )
            return result

    async def _create_admitted(
        self,
        request: _CreateRequest,
        request_id: str,
        call_state: _CreateCallState,
    ) -> SupervisedAgentHandle | _CancelledCreate | _RetryCreate:
        """Join one create and atomically account for handle delivery.

        Every caller gets a process-local waiter token under the same lock as
        the shared create.  Cancellation may own cleanup only when this is the
        last waiter, no caller received a handle, and the actual shared task
        installed a fresh local Activation rather than reusing a durable
        identity. A caller-side Directory snapshot cannot express that
        ownership across pending generations.
        """

        waiter = object()
        async with self._lock:
            self._require_open()
            pending = self._pending_creates.get(request_id)
            if pending is not None and pending.compensation_task is not None:
                call_state.owned_work = pending.compensation_task
                return _RetryCreate(pending.compensation_task)
            if pending is None:
                task = asyncio.create_task(
                    self._materialize_create(request, request_id),
                    name=f"traceh-agent-create-{request_id}",
                )
                pending = _PendingCreate(
                    request=request,
                    task=task,
                )
                self._pending_creates[request_id] = pending
            elif not pending.request.joins(request):
                raise AgentRequestConflictError()
            pending.waiters.add(waiter)
            call_state.owned_work = pending.task
        try:
            outcome = await self._await_shared(pending.task)
        except asyncio.CancelledError as error:
            compensation_task = await self._release_cancelled_create_waiter(
                request_id,
                pending,
                waiter,
                call_state,
            )
            return _CancelledCreate(error, compensation_task)
        except BaseException:
            await self._release_create_waiter_converged(
                request_id,
                pending,
                waiter,
                delivered=False,
            )
            raise

        try:
            async with self._lock:
                self._release_create_waiter_locked(
                    request_id,
                    pending,
                    waiter,
                    delivered=True,
                )
        except asyncio.CancelledError as error:
            # Cancellation may land after the shared task completed but before
            # this caller atomically records that it received the handle.
            compensation_task = await self._release_cancelled_create_waiter(
                request_id,
                pending,
                waiter,
                call_state,
            )
            return _CancelledCreate(error, compensation_task)
        return outcome.activation.handle()

    async def _materialize_create(
        self,
        request: _CreateRequest,
        request_id: str,
    ) -> _CreateOutcome:
        directory = await self._registrar.directory()
        existing = directory.for_request(request_id)
        if existing is not None:
            # Re-enter the Registrar even though the id is known.  Its
            # complete request reconciliation is the authority on whether
            # this request means the same Agent; looking up request_id alone
            # would silently accept a different preset or pinned identity.
            record = await self._registrar.create_agent(
                request.spec,
                request_id=request_id,
                agent_id=request.requested_agent_id,
                session_id=request.requested_session_id,
            )
            return _CreateOutcome(await self._activate(record))

        await self._require_live_owner(request.spec.owner_agent_id)

        execution = await self._factory.provision(
            freeze_agent_spec(request.spec),
            agent_id=request.assigned_agent_id,
            session_id=request.requested_session_id,
        )
        try:
            self._require_same_store(execution)
            if (
                request.requested_session_id is not None
                and execution.session_id != request.requested_session_id
            ):
                raise ExecutionSessionMismatchError()
            record = await self._registrar.create_agent(
                request.spec,
                request_id=request_id,
                agent_id=request.assigned_agent_id,
                session_id=execution.session_id,
            )
        except BaseException as error:
            # Includes the case where the identity append is *unknown*: a
            # live Activation must not be left behind for an Agent whose
            # record cannot be proved, so the candidate runtime is
            # released and the caller is told.
            await self._dispose_after_failure(execution, error)
        if (
            record.agent_id != request.assigned_agent_id
            or record.session_id != execution.session_id
        ):
            # Another process committed the same unpinned request while our
            # Session was being provisioned.  Its durable identity wins;
            # this candidate belongs to different ids and must converge
            # before the winner can be activated.
            await self._dispose_candidate(execution)
            return _CreateOutcome(await self._activate(record))
        return _CreateOutcome(
            await self._install(record, execution, abandonable=True)
        )

    async def resume(self, session_id: str) -> SupervisedAgentHandle:
        """Rebuild an Activation for an Agent that already exists.

        An explicit, human-initiated wake - not cold recovery. It also drains
        anything that was accepted with ``wakeup=False`` while nothing was
        running.
        """

        session_id = require_delivery_identifier(session_id, field="session_id")
        record = await self._record_for_session(session_id)
        lineage = await self._record_lineage(record)
        try:
            async with self._lifecycle.admission(lineage):
                activation = await self._activate(record)
                await activation.request_wake()
                return activation.handle()
        except LifecycleAdmissionClosed as error:
            raise SupervisorDisposedError() from error

    async def send(
        self,
        agent_id: str,
        message: AgentMessage,
        *,
        target: MessageTarget,
        wakeup: bool,
    ) -> MessageReceipt:
        """Accept a message and, if asked, wake the Agent to run it.

        ``NEXT_STEP`` is refused *before* acceptance, so nothing is written for
        a message this Stage cannot deliver. A target that is not a
        `MessageTarget` at all falls through to Stage B, whose validation owns
        that question.
        """

        if isinstance(target, MessageTarget) and target is not MessageTarget.NEW_TURN:
            raise UnsupportedMessageTargetError()
        if not wakeup:
            # Durably accepted and deliberately not started. Nothing is
            # created, resumed or woken.
            return await self._inbox_service.accept(
                agent_id, message, target=target, wakeup=False
            )
        try:
            record = await self._record_for_agent(agent_id)
        except AgentNotFoundError:
            # Preserve Stage B's public error contract and its proof that an
            # unknown Agent writes nothing. This Directory read is also the
            # operation's linearization point; a later external creation must
            # be retried explicitly rather than returning a receipt that was
            # never woken by this Supervisor.
            raise AgentUnknownError() from None
        lineage = await self._record_lineage(record)
        try:
            async with self._lifecycle.admission(lineage):
                receipt = await self._inbox_service.accept(
                    agent_id, message, target=target, wakeup=True
                )
                try:
                    activation = await self._activate(record)
                    await activation.request_wake()
                except asyncio.CancelledError:
                    raise
                except BaseException as error:
                    raise MessageWakeError(receipt) from error
        except LifecycleAdmissionClosed as error:
            raise SupervisorDisposedError() from error
        except asyncio.CancelledError:
            raise
        return receipt

    async def interrupt(self, agent_id: str, reason: str = "interrupted") -> bool:
        """Cancel the Turn this Agent is running, if any.

        Idempotent when idle. ``reason`` is bounded and single-line-safe before
        it can reach any log or terminal, because it is caller text.
        """

        reason = require_delivery_reason(reason, field="reason")
        return await self._require_activation(agent_id).interrupt(reason)

    async def wait_idle(self, agent_id: str) -> None:
        """Wait until everything already scheduled for this Agent has settled.

        "Settled" means claimed, run and terminal-recorded - not merely
        accepted. A message accepted with ``wakeup=False`` was never scheduled,
        so this does not wait for it and does not pretend it was handled.

        Raises `ActivationFaultedError` if the Activation stopped because it
        could not prove a durable fact, rather than waiting forever or
        returning as though all was well.
        """

        await self._require_activation(agent_id).wait_idle()

    async def report(self, agent_id: str, message_id: str) -> AgentRunReport:
        """Return one message's report from durable facts, without waiting.

        A terminal delivery outcome is necessary but not sufficient: completed
        work must also point at one coherent Session Turn. The replay reader
        checks that join and never consults an in-memory `TurnResult`.
        """

        return await self._reports.load(agent_id, message_id)

    async def wait_message(self, agent_id: str, message_id: str) -> AgentRunReport:
        """Wait for a scheduled message, then return its durable run report.

        Waiting is intentionally separate from `send()`: acceptance is not
        completion. Cancelling this waiter does not cancel the Agent or its
        Turn; the caller must use `interrupt()` or `dispose()` for that. Local
        message notification is only a fast path: another supported Supervisor
        may write the terminal fact, so bounded polling always re-checks the
        durable report.
        """

        while True:
            try:
                return await self.report(agent_id, message_id)
            except AgentMessageNotSettledError:
                pass

            try:
                activation = self._require_activation(agent_id)
                waiter = activation.register_message_waiter(message_id)
            except (AgentNotActiveError, ActivationFaultedError, SupervisorDisposedError) as error:
                # Terminal persistence may win the race with disposal/fault.
                # Durable evidence gets one final read before the process-local
                # state error is allowed to escape.
                try:
                    return await self.report(agent_id, message_id)
                except AgentMessageNotSettledError:
                    raise error from None

            try:
                # Close the check/register race: a terminal append that landed
                # immediately before registration is already durable, while a
                # later local append resolves this exact message's waiter. A
                # terminal append from another Supervisor is found by the
                # bounded durable re-read below.
                try:
                    return await self.report(agent_id, message_id)
                except AgentMessageNotSettledError:
                    await self._wait_for_message_progress(waiter)
            finally:
                activation.remove_message_waiter(message_id, waiter)

    async def _wait_for_message_progress(self, waiter: asyncio.Future[None]) -> None:
        """Wait for local progress or the next durable report poll."""

        await asyncio.wait((waiter,), timeout=_MESSAGE_REPORT_POLL_SECONDS)

    async def dispose(self, agent_id: str) -> None:
        """Stop this Agent's owned subtree, descendants before their owner.

        Idempotent, and it does not delete anything durable: the `AgentRecord`,
        the Inbox and the delivery history all survive, which is what makes a
        later explicit `resume()` meaningful. Communication and fork lineage
        do not participate in this traversal; only ``owner_agent_id`` does.
        """

        agent_id = require_delivery_identifier(agent_id, field="agent_id")
        async with self._lock:
            if self._close_task is not None:
                close_task = self._close_task
                task = None
            else:
                close_task = None
                task = self._tree_disposal_task_locked(agent_id)
        if close_task is not None:
            await self._await_task(close_task)
            return
        assert task is not None
        await self._join_tree_disposal(agent_id, task)

    def _tree_disposal_task_locked(self, agent_id: str) -> asyncio.Task[None]:
        """Return the shared subtree Task while the Supervisor lock is held."""

        task = self._tree_disposals.get(agent_id)
        if task is None:
            task = asyncio.create_task(
                self._dispose_subtree(agent_id),
                name=f"traceh-supervisor-dispose-tree-{agent_id}",
            )
            self._tree_cleanup_tasks[task] = set()
            self._tree_disposals[agent_id] = task
        return task

    async def _join_tree_disposal(
        self,
        agent_id: str,
        task: asyncio.Task[None],
    ) -> None:
        """Join one tree Task without deleting close-owned failure evidence."""

        try:
            await self._await_task(task)
        finally:
            async with self._lock:
                # Creating the close Task is the linearization point where
                # shutdown takes ownership of every tree disposal still in
                # this registry.  A cancelled public waiter must not remove a
                # Task (and its failure evidence) before `_close()` has joined
                # it.  `_close()` removes the exact entries after observation.
                if (
                    self._close_task is None
                    and self._tree_disposals.get(agent_id) is task
                ):
                    del self._tree_disposals[agent_id]
                    self._tree_cleanup_tasks.pop(task, None)

    async def aclose(self) -> None:
        """Dispose every Activation this Supervisor owns."""

        async with self._lock:
            if self._close_task is None:
                self._disposed = True
                self._close_task = asyncio.create_task(
                    self._close(), name="traceh-supervisor-close"
                )
            task = self._close_task
        await self._await_task(task)

    # -- internals ----------------------------------------------------------

    async def _complete_create_call(
        self,
        call_token: object,
        state: _CreateCallState,
    ) -> None:
        async with self._lock:
            if self._create_calls.get(call_token) is state:
                del self._create_calls[call_token]
            if not state.returned.done():
                state.returned.set_result(None)

    def _require_open(self) -> None:
        if self._disposed:
            raise SupervisorDisposedError()

    def _require_activation(self, agent_id: str) -> _Activation:
        activation = self._activations.get(agent_id)
        if activation is None:
            raise AgentNotActiveError()
        return activation

    def _require_same_store(self, execution: AgentExecution) -> None:
        # Object identity, never configuration: two stores can look identical
        # and be two different logs, and a Turn written to the wrong one leaves
        # the claim pointing at a Session history that does not contain it.
        # ``durable_log_identity`` resolves only the one transparent decorator
        # this repository ships, so a wrapped store still matches the store it
        # wraps without the comparison degrading into "looks similar".
        if durable_log_identity(execution.event_store) is not durable_log_identity(self._store):
            raise ExecutionStoreMismatchError()

    async def _record_for_agent(self, agent_id: str) -> AgentRecord:
        directory = await self._registrar.directory()
        record = directory.get(agent_id)
        if record is None:
            raise AgentNotFoundError()
        return record

    async def _record_for_session(self, session_id: str) -> AgentRecord:
        directory = await self._registrar.directory()
        record = directory.for_session(session_id)
        if record is None:
            raise AgentNotFoundError()
        try:
            await self._sessions.ensure_session(record.session_id)
        except SessionNotFoundError as error:
            raise AgentNotFoundError() from error
        return record

    async def _record_lineage(self, record: AgentRecord) -> tuple[str, ...]:
        directory = await self._registrar.directory()
        lineage = AgentOwnershipGraph(directory).lineage(record.agent_id)
        if not lineage:
            raise AgentNotFoundError()
        return lineage

    async def _require_live_owner(self, owner_agent_id: str | None) -> None:
        if owner_agent_id is None:
            return
        async with self._lock:
            owner = self._activations.get(owner_agent_id)
            if owner is None or owner.stopping or owner.fault is not None:
                raise AgentOwnerNotActiveError()

    async def _activate(self, record: AgentRecord) -> _Activation:
        """Return this Agent's Activation, building exactly one if needed.

        The single-flight is the linearization point for "one Activation per
        Agent": concurrent callers either find the installed Activation or join
        the same in-flight build, so the factory runs once even when several
        resumes race.
        """

        while True:
            abandonment: asyncio.Task[None] | None = None
            async with self._lock:
                self._require_open()
                if record.owner_agent_id is not None:
                    owner_activation = self._activations.get(record.owner_agent_id)
                    if (
                        owner_activation is None
                        or owner_activation.stopping
                        or owner_activation.fault is not None
                    ):
                        raise AgentOwnerNotActiveError()
                disposal = self._agent_disposals.get(record.agent_id)
                if disposal is not None:
                    if not disposal.done():
                        raise SupervisorDisposedError()
                    if not disposal.cancelled():
                        error = disposal.exception()
                        if error is not None:
                            raise error
                    # A successful old disposal no longer owns this id once a
                    # caller explicitly resumes it.
                    del self._agent_disposals[record.agent_id]
                existing = self._activations.get(record.agent_id)
                if existing is not None and not existing.stopping:
                    abandonment = self._retain_activation_locked(existing)
                    if abandonment is None:
                        return existing
                if existing is None:
                    pending = self._pending_activations.get(record.agent_id)
                    if pending is None:
                        pending = asyncio.create_task(
                            self._build(record),
                            name=f"traceh-agent-activate-{record.agent_id}",
                        )
                        self._pending_activations[record.agent_id] = pending
                else:
                    pending = None
            if abandonment is not None:
                # Abandonment won the same Supervisor-lock race. Fail closed
                # instead of handing out a handle already committed to
                # cleanup. A later explicit retry may reactivate the durable
                # identity after disposal converges.
                raise SupervisorDisposedError()
            if pending is None:
                # An Activation is being disposed. Wait for it to leave the
                # registry rather than installing a second one beside it.
                await existing.dispose()
                continue
            activation = await self._await_shared(pending)
            async with self._lock:
                if (
                    self._activations.get(record.agent_id) is not activation
                    or activation.stopping
                ):
                    retry = True
                    abandonment = None
                else:
                    retry = False
                    abandonment = self._retain_activation_locked(activation)
            if abandonment is not None:
                raise SupervisorDisposedError()
            if retry:
                continue
            return activation

    @staticmethod
    def _retain_activation_locked(
        activation: _Activation,
    ) -> asyncio.Task[None] | None:
        """Retain one Activation or return the cleanup that won the race.

        The caller must hold the Supervisor lock. Every public path that
        resumes or wakes an Activation flows through `_activate()`, so the
        abandonment decision observes more than create waiters alone.
        """

        if activation._abandonment_task is not None:
            return activation._abandonment_task
        activation._retained = True
        return None

    async def _build(self, record: AgentRecord) -> _Activation:
        try:
            execution = await self._factory.activate(record)
            try:
                self._require_same_store(execution)
                if execution.session_id != record.session_id:
                    raise ExecutionSessionMismatchError()
            except BaseException as error:
                await self._dispose_after_failure(execution, error)
            return await self._install(record, execution, abandonable=False)
        finally:
            async with self._lock:
                self._pending_activations.pop(record.agent_id, None)

    async def _install(
        self,
        record: AgentRecord,
        execution: AgentExecution,
        *,
        abandonable: bool,
    ) -> _Activation:
        installed: _Activation | None = None
        rejection: BaseException | None = None
        activation: _Activation | None = None
        try:
            await self._lock.acquire()
            try:
                if self._disposed or record.agent_id in self._agent_disposals:
                    rejection = SupervisorDisposedError()
                elif record.owner_agent_id is not None:
                    owner_activation = self._activations.get(record.owner_agent_id)
                    if (
                        owner_activation is None
                        or owner_activation.stopping
                        or owner_activation.fault is not None
                    ):
                        rejection = AgentOwnerNotActiveError()
                if rejection is None:
                    owner = self._session_owner.get(record.session_id)
                    if owner is not None and owner != record.agent_id:
                        rejection = ActivationConflictError()
                if rejection is None:
                    installed = self._activations.get(record.agent_id)
                    if installed is None or installed.stopping:
                        activation = _Activation(
                            agent_id=record.agent_id,
                            session_id=record.session_id,
                            activation_id=str(uuid4()),
                            execution=execution,
                            supervisor=self,
                            abandonable=abandonable,
                        )
                        self._activations[record.agent_id] = activation
                        self._session_owner[record.session_id] = record.agent_id
            finally:
                self._lock.release()
        except BaseException as error:
            await self._dispose_after_failure(execution, error)

        if rejection is not None:
            await self._dispose_after_failure(execution, rejection)
        if activation is not None:
            # No suspension exists between publication and start, so a caller
            # cannot observe an installed Activation whose worker was never
            # created.
            activation.start()
            return activation
        assert installed is not None
        # Someone installed one while this candidate was being built. The
        # installed owner wins, but returning it is only safe after the losing
        # candidate has genuinely converged. Cleanup failure is observable.
        await self._dispose_candidate(execution)
        return installed

    @staticmethod
    async def _await_shared(task: asyncio.Task[_CreateOutcome]) -> _CreateOutcome:
        """Await work shared with other callers without abandoning it."""

        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                raise
            # Another caller may still need this build; converge rather than
            # leaving it running after we return.
            await await_worker_convergence(task)
            raise

    def _release_create_waiter_locked(
        self,
        request_id: str,
        pending: _PendingCreate,
        waiter: object,
        *,
        delivered: bool,
    ) -> asyncio.Task[None] | None:
        """Release one waiter and return its registered compensation Task."""

        pending.waiters.remove(waiter)
        if delivered:
            pending.delivered = True
            if (
                pending.task.done()
                and not pending.task.cancelled()
                and pending.task.exception() is None
            ):
                pending.task.result().activation._retained = True
        compensation_task: asyncio.Task[None] | None = None
        if not pending.waiters:
            activation = (
                pending.task.result().activation
                if pending.task.done()
                and not pending.task.cancelled()
                and pending.task.exception() is None
                else None
            )
            if (
                not pending.delivered
                and activation is not None
                and not activation._retained
                and activation._abandonment_task is None
            ):
                compensation_task = asyncio.create_task(
                    self._compensate_unreturned_create(
                        request_id,
                        pending,
                        activation.agent_id,
                    ),
                    name=f"traceh-agent-create-cancel-{request_id}",
                )
                activation._abandonment_task = compensation_task
                pending.compensation_task = compensation_task
            else:
                registered = self._pending_creates.get(request_id)
                if registered is pending:
                    del self._pending_creates[request_id]
        return compensation_task

    async def _release_create_waiter(
        self,
        request_id: str,
        pending: _PendingCreate,
        waiter: object,
        *,
        delivered: bool,
    ) -> asyncio.Task[None] | None:
        async with self._lock:
            return self._release_create_waiter_locked(
                request_id,
                pending,
                waiter,
                delivered=delivered,
            )

    async def _release_create_waiter_converged(
        self,
        request_id: str,
        pending: _PendingCreate,
        waiter: object,
        *,
        delivered: bool,
    ) -> asyncio.Task[None] | None:
        """Finish waiter accounting despite repeated caller cancellation."""

        task = asyncio.create_task(
            self._release_create_waiter(
                request_id,
                pending,
                waiter,
                delivered=delivered,
            ),
            name=f"traceh-agent-create-release-{request_id}",
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await await_worker_convergence(task)
            return task.result()

    async def _release_cancelled_create_waiter(
        self,
        request_id: str,
        pending: _PendingCreate,
        waiter: object,
        call_state: _CreateCallState,
    ) -> asyncio.Task[None] | None:
        compensation = await self._release_create_waiter_converged(
            request_id,
            pending,
            waiter,
            delivered=False,
        )
        if compensation is not None:
            update = asyncio.create_task(
                self._set_create_owned_work(call_state, compensation),
                name="traceh-agent-create-work-handoff",
            )
            try:
                await asyncio.shield(update)
            except asyncio.CancelledError:
                await await_worker_convergence(update)
        return compensation

    async def _set_create_owned_work(
        self,
        call_state: _CreateCallState,
        owned_work: asyncio.Task[object],
    ) -> None:
        async with self._lock:
            call_state.owned_work = owned_work

    async def _compensate_unreturned_create(
        self,
        request_id: str,
        pending: _PendingCreate,
        agent_id: str,
    ) -> None:
        """Dispose one unreturned create while keeping retries behind it."""

        try:
            async with self._lock:
                close_owned = self._close_task is not None
                task = (
                    None
                    if close_owned
                    else self._tree_disposal_task_locked(agent_id)
                )
            if close_owned:
                # Close owns the full durable forest. Waiting for its resource
                # phase avoids a cycle where compensation awaits close while
                # close awaits the public create call that owns compensation.
                await self._close_resources_done.wait()
            else:
                assert task is not None
                await self._join_tree_disposal(agent_id, task)
        finally:
            async with self._lock:
                if self._pending_creates.get(request_id) is pending:
                    del self._pending_creates[request_id]

    @staticmethod
    async def _raise_cancel_after_cleanup(
        cancelled: asyncio.CancelledError,
        cleanup: asyncio.Task[None],
    ) -> None:
        """Converge unreturned creation cleanup, then preserve cancellation."""

        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # The first cancellation already selected this path. Further
                # cancellations cannot transfer cleanup ownership or release
                # the caller while the same subtree disposal is still live.
                continue
            except BaseException:
                break
        if not cleanup.cancelled():
            cleanup_error = cleanup.exception()
            if cleanup_error is not None:
                raise cancelled from cleanup_error
        raise cancelled

    @staticmethod
    async def _await_task(task: asyncio.Task[None]) -> None:
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await await_worker_convergence(task)
            raise

    @staticmethod
    async def _dispose_candidate(execution: AgentExecution) -> None:
        task = asyncio.create_task(execution.dispose(), name="traceh-agent-candidate-dispose")
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as cancelled:
            await await_worker_convergence(task)
            if task.cancelled():
                raise
            cleanup_error = task.exception()
            if cleanup_error is not None:
                raise BaseExceptionGroup(
                    "candidate disposal was cancelled and cleanup failed",
                    (cancelled, cleanup_error),
                ) from None
            raise

    @classmethod
    async def _dispose_after_failure(
        cls, execution: AgentExecution, primary: BaseException
    ) -> None:
        try:
            await cls._dispose_candidate(execution)
        except BaseException as cleanup_error:
            raise BaseExceptionGroup(
                "activation failed and candidate cleanup failed",
                (primary, cleanup_error),
            ) from None
        raise primary

    async def _dispose_agent(self, agent_id: str) -> None:
        failures: list[BaseException] = []
        async with self._lock:
            pending_tasks = [
                pending.task
                for pending in self._pending_creates.values()
                if pending.request.assigned_agent_id == agent_id
            ]
            pending_activation = self._pending_activations.get(agent_id)
            if pending_activation is not None:
                pending_tasks.append(pending_activation)

        for pending in dict.fromkeys(pending_tasks):
            error = await self._cancel_and_join(pending)
            if error is not None and not isinstance(error, SupervisorDisposedError):
                failures.append(error)

        async with self._lock:
            activation = self._activations.get(agent_id)
        if activation is not None:
            try:
                await activation.dispose()
            except BaseException as error:
                failures.append(error)
            finally:
                async with self._lock:
                    if self._activations.get(agent_id) is activation:
                        del self._activations[agent_id]
                    if self._session_owner.get(activation.session_id) == agent_id:
                        del self._session_owner[activation.session_id]

        if failures:
            raise BaseExceptionGroup("agent disposal failed", failures)

    async def _dispose_subtree(self, agent_id: str) -> None:
        """Quiesce one durable ownership subtree and release it child-first."""

        directory = await self._registrar.directory()
        initial = AgentOwnershipGraph(directory)
        affected = initial.subtree_postorder(agent_id) or (agent_id,)
        affected_set = frozenset(affected)
        affected_requests = frozenset(
            record.request_id
            for record in directory.records
            if record.agent_id in affected_set
        )
        failures: list[BaseException] = []
        async with self._lifecycle.disposal(affected) as barrier:
            # Stage C already guarantees that disposing an Agent converges its
            # in-flight create/resume candidate.  Registering the subtree first
            # prevents a replacement from entering while these candidates are
            # cancelled. A pending child is selected by its declared owner even
            # though its identity is not durable yet.
            async with self._lock:
                pending = [
                    item.task
                    for request_id, item in self._pending_creates.items()
                    if request_id in affected_requests
                    or item.request.assigned_agent_id in affected_set
                    or item.request.spec.owner_agent_id in affected_set
                ]
                pending.extend(
                    task
                    for pending_id, task in self._pending_activations.items()
                    if pending_id in affected_set
                )
                pending_tasks = tuple(dict.fromkeys(pending))
                create_calls = tuple(
                    state
                    for state in self._create_calls.values()
                    if state.owned_work in pending_tasks
                )
            for task in pending_tasks:
                error = await self._cancel_and_join(task)
                if error is not None and not isinstance(error, SupervisorDisposedError):
                    failures.append(error)
            await barrier.wait_quiescent()
            # Candidate cancellation can finish before the public create
            # method completes its local return/unregister boundary. Join that
            # exact receipt, but do not wait a call that transferred ownership
            # to post-admission compensation: the overlapping subtree cleanup
            # below is the resource owner in that case.
            for state in create_calls:
                if state.owned_work in pending_tasks:
                    await asyncio.shield(state.returned)
            # A child creation admitted before the disposal scope may have
            # committed before cancellation reached it. Reloading after
            # quiescence is what prevents that durable child escaping.
            current = AgentOwnershipGraph(await self._registrar.directory())
            order = current.subtree_postorder(agent_id) or (agent_id,)
            for owned_id in order:
                task = await self._agent_disposal_task(owned_id)
                tree_task = asyncio.current_task()
                assert tree_task is not None
                async with self._lock:
                    self._tree_cleanup_tasks.setdefault(tree_task, set()).add(task)
                try:
                    await asyncio.shield(task)
                except BaseException as error:
                    failures.append(error)
        if failures:
            raise BaseExceptionGroup("agent subtree disposal failed", failures)

    async def _agent_disposal_task(self, agent_id: str) -> asyncio.Task[None]:
        """Return the one cleanup Task that owns an Agent's current activation."""

        async with self._lock:
            task = self._agent_disposals.get(agent_id)
            if task is None:
                task = asyncio.create_task(
                    self._dispose_agent(agent_id),
                    name=f"traceh-supervisor-dispose-agent-{agent_id}",
                )
                self._agent_disposals[agent_id] = task
            return task

    async def _close(self) -> None:
        failures: list[BaseException] = []
        await self._lifecycle.begin_close()
        async with self._lock:
            pending = [item.task for item in self._pending_creates.values()]
            pending.extend(self._pending_activations.values())
            create_calls = tuple(self._create_calls.items())
        # Public create owns more than the candidate Task: after cancellation
        # it may leave admission and converge an unreturned Activation. Close
        # requests cancellation now, performs the resource phase below, then
        # joins every complete public call before it may return.
        for _, state in create_calls:
            if state.owned_work is None and not state.exit_started:
                state.caller.cancel()
        for task in dict.fromkeys(pending):
            error = await self._cancel_and_join(task)
            if error is not None and not isinstance(error, SupervisorDisposedError):
                failures.append(error)

        # Public create/resume/send calls keep their admission through the
        # shared candidate work. Cancelling the candidates above lets those
        # calls leave; only then is it safe to take the final Directory view.
        await self._lifecycle.wait_quiescent()

        async with self._lock:
            tree_disposals = tuple(
                (task, self._tree_cleanup_tasks.setdefault(task, set()))
                for task in dict.fromkeys(self._tree_disposals.values())
            )
        for task, cleanup_tasks in tree_disposals:
            try:
                await asyncio.shield(task)
            except BaseException as error:
                _collect_tree_residual_failures(failures, error, cleanup_tasks)
        async with self._lock:
            # Shutdown has now observed every tree Task it claimed.  Remove
            # only those exact registrations: the identity check makes the
            # ownership transfer explicit even though close admission already
            # prevents a replacement Task from being installed.
            for task, _ in tree_disposals:
                for agent_id, registered in tuple(self._tree_disposals.items()):
                    if registered is task:
                        del self._tree_disposals[agent_id]
                self._tree_cleanup_tasks.pop(task, None)

        graph: AgentOwnershipGraph | None
        try:
            graph = AgentOwnershipGraph(await self._registrar.directory())
        except BaseException as error:
            # The durable protocol error remains observable, but it cannot be
            # allowed to strand process-local workers or runtimes. Without a
            # trustworthy graph no ownership order may be inferred, so close
            # falls back to releasing every known resource and reports the
            # projection failure after convergence.
            graph = None
            failures.append(error)
        async with self._lock:
            active_ids = tuple(self._activations)
            disposal_ids = tuple(self._agent_disposals)
        if graph is None:
            # Reverse installation order is deterministic and usually retains
            # descendant-first cleanup, but is deliberately not presented as
            # ownership evidence: the durable graph was unreadable.
            order = list(reversed(active_ids))
        else:
            order = list(graph.forest_postorder())
            for active_id in active_ids:
                if active_id not in graph:
                    order.append(active_id)
        scheduled = set(order)
        for disposal_id in disposal_ids:
            if disposal_id not in scheduled:
                scheduled.add(disposal_id)
                order.append(disposal_id)
        for owned_id in order:
            task = await self._agent_disposal_task(owned_id)
            try:
                await asyncio.shield(task)
            except BaseException as error:
                failures.append(error)
        # Compensation that linearized after close began waits on this event
        # instead of awaiting the close Task itself. This is the hand-off point:
        # the durable forest and all known local resources have been attempted.
        self._close_resources_done.set()
        for _, state in create_calls:
            try:
                await asyncio.shield(state.returned)
            except asyncio.CancelledError:
                # Close requested this outcome; the call's compensation has
                # already converged before the cancellation can become final.
                pass
        if failures:
            raise BaseExceptionGroup("supervisor close failed", failures)

    @staticmethod
    async def _cancel_and_join(task: asyncio.Task[object]) -> BaseException | None:
        if not task.done():
            task.cancel()
        await await_worker_convergence(task)
        if task.cancelled():
            return None
        try:
            task.result()
        except BaseException as error:
            return error
        return None


__all__ = ["AgentNotFoundError", "ProcessAgentSupervisor", "SupervisedAgentHandle"]
