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
from dataclasses import dataclass
from uuid import uuid4

from traceh.agents.errors import AgentRequestConflictError
from traceh.agents.identity import agent_spec_request_fingerprint, freeze_agent_spec
from traceh.agents.inbox import AgentInboxReader
from traceh.agents.inbox_service import AgentInboxService
from traceh.agents.registrar import AgentRegistrar
from traceh.api.agents import (
    AcceptedMessage,
    AgentMessage,
    AgentRecord,
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
    AgentNotActiveError,
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


@dataclass(frozen=True, slots=True)
class _PendingCreate:
    request: _CreateRequest
    task: asyncio.Task[_Activation]


class _Activation:
    """One Agent's live worker and exclusive execution runtime."""

    __slots__ = (
        "_current_claim",
        "_dispose_task",
        "_execution",
        "_fault",
        "_idle",
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
    ) -> None:
        self.agent_id = agent_id
        self.session_id = session_id
        self.activation_id = activation_id
        self._execution = execution
        self._supervisor = supervisor
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
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
                )
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
                )
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
                )
            )
            return self._fault is None
        self._current_claim = None
        await self._record_terminal(
            lambda: self._supervisor.deliveries.complete(
                agent_id=self.agent_id,
                claim=claim,
                turn_id=result.turn_id,
                reason=result.reason,
            )
        )
        return self._fault is None

    async def _record_terminal(self, factory) -> None:
        """Append a terminal fact, converging even if we are cancelled.

        The Turn already ran. Abandoning the append because a disposal arrived
        would leave a claim with no outcome, which is exactly the state nothing
        in this Stage can repair.
        """

        task = asyncio.create_task(factory(), name=f"traceh-agent-terminal-{self.agent_id}")
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await await_worker_convergence(task)
            if not task.cancelled():
                self._absorb_terminal_result(task)
            raise
        except Exception:
            self._set_fault("terminal-not-durable")

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

    async def dispose(self) -> None:
        """Stop this Activation and release its runtime.

        The work belongs to one internal Task rather than to whoever waits for
        it, so a caller's cancellation can interrupt the *waiting* but never
        the shutdown itself. Repeated calls await that same Task, so disposal
        runs exactly once and a second caller sees the same real outcome.
        """

        async with self._state_lock:
            self._stopping = True
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
        "_close_task",
        "_deliveries",
        "_disposed",
        "_agent_disposals",
        "_factory",
        "_inbox_service",
        "_inboxes",
        "_lock",
        "_pending_activations",
        "_pending_creates",
        "_registrar",
        "_sessions",
        "_session_owner",
        "_store",
    )

    def __init__(self, *, store: EventStore, factory: AgentActivationFactory) -> None:
        self._store = store
        self._factory = factory
        self._registrar = AgentRegistrar(store)
        self._inbox_service = AgentInboxService(store)
        self._inboxes = AgentInboxReader(store)
        self._deliveries = AgentDeliveryService(store)
        self._sessions = SessionService(store)
        self._activations: dict[str, _Activation] = {}
        self._session_owner: dict[str, str] = {}
        self._pending_activations: dict[str, asyncio.Task[_Activation]] = {}
        self._pending_creates: dict[str, _PendingCreate] = {}
        self._agent_disposals: dict[str, asyncio.Task[None]] = {}
        self._close_task: asyncio.Task[None] | None = None
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
        async with self._lock:
            self._require_open()
            pending = self._pending_creates.get(request_id)
            if pending is None:
                task = asyncio.create_task(
                    self._create(request, request_id),
                    name=f"traceh-agent-create-{request_id}",
                )
                pending = _PendingCreate(request=request, task=task)
                self._pending_creates[request_id] = pending
            elif not pending.request.joins(request):
                raise AgentRequestConflictError()
        activation = await self._await_shared(pending.task)
        return activation.handle()

    async def _create(
        self,
        request: _CreateRequest,
        request_id: str,
    ) -> _Activation:
        try:
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
                return await self._activate(record)

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
                return await self._activate(record)
            return await self._install(record, execution)
        finally:
            async with self._lock:
                self._pending_creates.pop(request_id, None)

    async def resume(self, session_id: str) -> SupervisedAgentHandle:
        """Rebuild an Activation for an Agent that already exists.

        An explicit, human-initiated wake - not cold recovery. It also drains
        anything that was accepted with ``wakeup=False`` while nothing was
        running.
        """

        session_id = require_delivery_identifier(session_id, field="session_id")
        record = await self._record_for_session(session_id)
        activation = await self._activate(record)
        await activation.request_wake()
        return activation.handle()

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
        receipt = await self._inbox_service.accept(
            agent_id, message, target=target, wakeup=wakeup
        )
        if not wakeup:
            # Durably accepted and deliberately not started. Nothing is
            # created, resumed or woken.
            return receipt
        try:
            record = await self._record_for_agent(agent_id)
            activation = await self._activate(record)
            await activation.request_wake()
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            # The acceptance is durable. Reporting a bare failure would invite
            # a retry that appends the same message under a new id, so the
            # receipt travels with the error.
            raise MessageWakeError(receipt) from error
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

    async def dispose(self, agent_id: str) -> None:
        """Stop this Agent's Activation and release its runtime.

        Idempotent, and it does not delete anything durable: the `AgentRecord`,
        the Inbox and the delivery history all survive, which is what makes a
        later `resume()` meaningful.
        """

        agent_id = require_delivery_identifier(agent_id, field="agent_id")
        async with self._lock:
            if self._close_task is not None:
                close_task = self._close_task
                task = None
            else:
                close_task = None
                task = self._agent_disposals.get(agent_id)
                if task is None:
                    task = asyncio.create_task(
                        self._dispose_agent(agent_id),
                        name=f"traceh-supervisor-dispose-{agent_id}",
                    )
                    self._agent_disposals[agent_id] = task
        if close_task is not None:
            await self._await_task(close_task)
            return
        assert task is not None
        await self._await_task(task)

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

    async def _activate(self, record: AgentRecord) -> _Activation:
        """Return this Agent's Activation, building exactly one if needed.

        The single-flight is the linearization point for "one Activation per
        Agent": concurrent callers either find the installed Activation or join
        the same in-flight build, so the factory runs once even when several
        resumes race.
        """

        while True:
            async with self._lock:
                self._require_open()
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
            if pending is None:
                # An Activation is being disposed. Wait for it to leave the
                # registry rather than installing a second one beside it.
                await existing.dispose()
                continue
            return await self._await_shared(pending)

    async def _build(self, record: AgentRecord) -> _Activation:
        try:
            execution = await self._factory.activate(record)
            try:
                self._require_same_store(execution)
                if execution.session_id != record.session_id:
                    raise ExecutionSessionMismatchError()
            except BaseException as error:
                await self._dispose_after_failure(execution, error)
            return await self._install(record, execution)
        finally:
            async with self._lock:
                self._pending_activations.pop(record.agent_id, None)

    async def _install(self, record: AgentRecord, execution: AgentExecution) -> _Activation:
        installed: _Activation | None = None
        rejection: BaseException | None = None
        activation: _Activation | None = None
        try:
            await self._lock.acquire()
            try:
                if self._disposed or record.agent_id in self._agent_disposals:
                    rejection = SupervisorDisposedError()
                else:
                    owner = self._session_owner.get(record.session_id)
                    if owner is not None and owner != record.agent_id:
                        rejection = ActivationConflictError()
                    else:
                        installed = self._activations.get(record.agent_id)
                        if installed is None or installed.stopping:
                            activation = _Activation(
                                agent_id=record.agent_id,
                                session_id=record.session_id,
                                activation_id=str(uuid4()),
                                execution=execution,
                                supervisor=self,
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
    async def _await_shared(task: asyncio.Task[_Activation]) -> _Activation:
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

    async def _close(self) -> None:
        failures: list[BaseException] = []
        async with self._lock:
            pending = [item.task for item in self._pending_creates.values()]
            pending.extend(self._pending_activations.values())
        for task in dict.fromkeys(pending):
            error = await self._cancel_and_join(task)
            if error is not None and not isinstance(error, SupervisorDisposedError):
                failures.append(error)

        async with self._lock:
            agent_ids = tuple(self._activations)
            disposals = dict(self._agent_disposals)
            for agent_id in agent_ids:
                if agent_id not in disposals:
                    task = asyncio.create_task(
                        self._dispose_agent(agent_id),
                        name=f"traceh-supervisor-close-agent-{agent_id}",
                    )
                    self._agent_disposals[agent_id] = task
                    disposals[agent_id] = task
        for task in dict.fromkeys(disposals.values()):
            try:
                await asyncio.shield(task)
            except BaseException as error:
                failures.append(error)
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
