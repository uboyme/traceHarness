"""The Workflow coordinator.

This is not a second Agent scheduler. It does exactly four things: derive ready
nodes from durable facts, call the public service each node names, run
independent ready nodes with structured concurrency, and append what happened.
Agent FIFO order, Turn execution and Activation lifetime stay where they already
live.

Its recovery rule is deliberately narrow. v0.7-E can continue exactly one kind
of interrupted run: one that stopped cleanly at a human Approval barrier. Every
other in-between state fails closed, because continuing would require guessing
what a half-finished node did to an Agent, a Workspace, a Budget hold or a Git
repository.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

from traceh.agents.commit_reconciliation import committed_after_failure
from traceh.api.events import EventEnvelope, PendingEvent
from traceh.api.json_types import JsonValue
from traceh.api.workflow import (
    NodeStatus,
    WorkflowBindingResolver,
    WorkflowDefinition,
    WorkflowNode,
    WorkflowRun,
    WorkflowStatus,
)
from traceh.concurrency import (
    await_worker_convergence,
    combine_failures,
    informative_failure,
)
from traceh.session.event_store import ConcurrencyConflict, Durability, EventStore
from traceh.supervision.execution import durable_log_identity
from traceh.workflow.errors import (
    WorkflowInputError,
    WorkflowLedgerConflictError,
    WorkflowRecoveryError,
    WorkflowServiceClosedError,
    WorkflowStateError,
    WorkflowWriteError,
)
from traceh.workflow.events import (
    WORKFLOW_APPROVAL_AWAITED,
    WORKFLOW_MAP_EXPANDED,
    WORKFLOW_NODE_COMPLETED,
    WORKFLOW_NODE_FAILED,
    WORKFLOW_NODE_STARTED,
    WORKFLOW_RUN_FINISHED,
    WORKFLOW_RUN_STARTED,
    WORKFLOW_SCHEMA_VERSION,
    approval_awaited_data,
    is_workflow_fact,
    map_expanded_data,
    node_failed_data,
    node_started_data,
    run_finished_data,
    run_started_data,
    workflow_stream_id,
)
from traceh.workflow.execution import NodeExecutor, WorkflowServices
from traceh.workflow.models import (
    freeze_workflow_definition,
    node_kind,
    require_workflow_identifier,
    workflow_definition_hash,
)
from traceh.workflow.projection import (
    WorkflowProjection,
    WorkflowStreamReader,
    ready_nodes,
)

MAX_APPEND_ATTEMPTS = 8
"""How often one node fact may lose a stream compare-and-swap before failing."""


class WorkflowService:
    """Advance one fixed typed DAG using only public services and durable facts."""

    __slots__ = (
        "_close_task",
        "_closed",
        "_lock",
        "_pending",
        "_reader",
        "_resolver",
        "_services",
        "_store",
    )

    def __init__(
        self,
        store: EventStore,
        services: WorkflowServices,
        resolver: WorkflowBindingResolver,
    ) -> None:
        if type(services) is not WorkflowServices:
            raise WorkflowInputError("workflow-services-invalid", "services")
        _require_one_durable_log(store, services)
        self._store = store
        self._services = services
        self._resolver = resolver
        self._reader = WorkflowStreamReader(store)
        self._lock = asyncio.Lock()
        self._pending: dict[str, tuple[str, asyncio.Task[WorkflowRun]]] = {}
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    @property
    def store(self) -> EventStore:
        return self._store

    async def workflow_status(self, run_id: str) -> WorkflowStatus | None:
        """Fresh run-level status for product reconciliation.

        This is a projection of the existing Workflow stream, not a cached
        lifecycle flag.  A caller needing node evidence must still call
        :meth:`state` with the exact frozen definition.
        """

        run_id = require_workflow_identifier(run_id, field="run_id")
        return (await self._reader.load(run_id)).status

    async def state(self, run_id: str, definition: WorkflowDefinition) -> WorkflowRun:
        """Read one run without advancing it."""

        run_id = require_workflow_identifier(run_id, field="run_id")
        definition = freeze_workflow_definition(definition)
        projection = await self._reader.load(run_id)
        # A read is still an interpretation: pairing a real run with someone
        # else's definition would report node kinds and outcomes that run never
        # agreed to.
        _require_same_definition(projection, workflow_definition_hash(definition))
        return projection.run(definition)

    async def start(
        self, run_id: str, definition: WorkflowDefinition
    ) -> WorkflowRun:
        return await self._advance(run_id, definition, allow_start=True)

    async def resume(
        self, run_id: str, definition: WorkflowDefinition
    ) -> WorkflowRun:
        return await self._advance(run_id, definition, allow_start=False)

    async def cancel(self, run_id: str) -> bool:
        """Cancel and converge this host's in-flight advancement of ``run_id``.

        This does not manufacture a durable Workflow terminal.  A cancellation
        may land while a node owns an external side effect, so the honest
        durable state is the partial stream already written.  The method owns
        only the process-local execution task: it cancels that exact task,
        waits for node cleanup to finish, and reports whether this service was
        driving the run.  ProductTask may then record its own user cancellation
        without pretending the interrupted Workflow became resumable.
        """

        run_id = require_workflow_identifier(run_id, field="run_id")
        async with self._lock:
            pending = self._pending.get(run_id)
            task = None if pending is None else pending[1]
        if task is None:
            return False
        task.cancel()
        await await_worker_convergence(task)
        # Retrieve an ordinary failure so it cannot become an unobserved task
        # exception.  Cancellation is the requested outcome; a distinct
        # cleanup failure still belongs to the caller as its cause.
        if not task.cancelled():
            failure = task.exception()
            if failure is not None:
                raise failure
        return True

    async def _advance(
        self, run_id: str, definition: WorkflowDefinition, *, allow_start: bool
    ) -> WorkflowRun:
        run_id = require_workflow_identifier(run_id, field="run_id")
        definition = freeze_workflow_definition(definition)
        digest = workflow_definition_hash(definition)
        return await self._owned(
            run_id,
            digest,
            lambda: self._run(run_id, definition, digest, allow_start),
            name="traceh-workflow-run",
        )

    # ------------------------------------------------------------- execution

    async def _run(
        self,
        run_id: str,
        definition: WorkflowDefinition,
        digest: str,
        allow_start: bool,
    ) -> WorkflowRun:
        projection = await self._reader.load(run_id)
        if projection.head_seq == 0:
            if not allow_start:
                raise WorkflowStateError("workflow-run-unknown")
            await self._append(
                run_id,
                projection.head_seq,
                WORKFLOW_RUN_STARTED,
                run_started_data(
                    run_id=run_id,
                    definition_id=definition.definition_id,
                    definition_hash=digest,
                ),
            )
            projection = await self._reader.load(run_id)
        else:
            _require_same_definition(projection, digest)
            self._require_continuable(projection)

        executor = NodeExecutor(
            run_id=run_id, services=self._services, resolver=self._resolver
        )
        while True:
            run = projection.run(definition)
            if run.status in (WorkflowStatus.COMPLETED, WorkflowStatus.FAILED):
                return run
            projection = await self._recheck_approvals(
                run_id, definition, projection, executor
            )
            ready = ready_nodes(definition, projection)
            if not ready:
                if projection.awaiting_approval:
                    return run
                projection = await self._finish(run_id, projection)
                continue
            projection = await self._run_ready(
                run_id, projection, executor, ready
            )

    async def _recheck_approvals(
        self,
        run_id: str,
        definition: WorkflowDefinition,
        projection: WorkflowProjection,
        executor: NodeExecutor,
    ) -> WorkflowProjection:
        """Ask again whether a human has decided, without restarting the node.

        A barrier node already has its start fact, so re-entering must not write
        a second one. It either finds the exact durable Approval and finishes,
        or it stays where it is.
        """

        by_id = {node.node_id: node for node in definition.nodes}
        for node_id in projection.awaiting_approval:
            node = by_id.get(node_id)
            if node is None:
                raise WorkflowStateError("workflow-definition-changed", node_id)
            outcome = await executor.execute(node, None, projection)
            if outcome.awaiting_review_id is not None:
                continue
            assert outcome.completed is not None
            projection = await self._append_reload(
                run_id, projection, WORKFLOW_NODE_COMPLETED, outcome.completed
            )
        return projection

    async def _run_ready(
        self,
        run_id: str,
        projection: WorkflowProjection,
        executor: NodeExecutor,
        ready: tuple[tuple[WorkflowNode, str | None], ...],
    ) -> WorkflowProjection:
        """Start every ready node, then converge every one of them.

        Independent nodes run concurrently, but no failure may release the
        caller while another node is still touching an Agent, a Workspace or a
        Git repository. Failures are collected per node so two nodes raising the
        *same* exception object still count as two independent failures.
        """

        started: list[tuple[WorkflowNode, str | None]] = []
        for node, map_key in ready:
            node_id = _effective_node_id(node, map_key)
            projection = await self._append_reload(
                run_id,
                projection,
                WORKFLOW_NODE_STARTED,
                node_started_data(
                    node_id=node_id, kind=node_kind(node), map_key=map_key
                ),
            )
            started.append((node, map_key))

        tasks = [
            asyncio.create_task(
                executor.execute(node, map_key, projection),
                name=f"traceh-workflow-node-{_effective_node_id(node, map_key)}",
            )
            for node, map_key in started
        ]
        results = await _converge_all(tasks)

        failures: list[tuple[str, BaseException]] = []
        for (node, map_key), outcome in zip(started, results, strict=True):
            node_id = _effective_node_id(node, map_key)
            if isinstance(outcome, BaseException):
                if isinstance(outcome, asyncio.CancelledError):
                    failures.append((node_id, outcome))
                    continue
                code = getattr(outcome, "code", None)
                projection = await self._append_reload(
                    run_id,
                    projection,
                    WORKFLOW_NODE_FAILED,
                    node_failed_data(
                        node_id=node_id,
                        kind=node_kind(node),
                        map_key=map_key,
                        failure_code=_safe_code(code),
                    ),
                )
                failures.append((node_id, outcome))
                continue
            if outcome.awaiting_review_id is not None:
                projection = await self._append_reload(
                    run_id,
                    projection,
                    WORKFLOW_APPROVAL_AWAITED,
                    approval_awaited_data(
                        node_id=node_id, review_id=outcome.awaiting_review_id
                    ),
                )
                continue
            if outcome.map_keys:
                # The expansion must be durable before any child can start, so a
                # later reader derives the same children this run will execute.
                projection = await self._append_reload(
                    run_id,
                    projection,
                    WORKFLOW_MAP_EXPANDED,
                    map_expanded_data(node_id=node_id, map_keys=outcome.map_keys),
                )
            assert outcome.completed is not None
            projection = await self._append_reload(
                run_id, projection, WORKFLOW_NODE_COMPLETED, outcome.completed
            )

        cancelled = [
            error for _, error in failures if isinstance(error, asyncio.CancelledError)
        ]
        if cancelled:
            # A cancelled node keeps its start fact and gains no terminal, so the
            # run stays un-continuable. Writing a run terminal here would claim
            # an outcome nobody established.
            raise cancelled[0]
        if failures:
            # The run really is over. Recording that before reporting the node
            # errors is what stops a later resume() from inventing the terminal
            # fact, which would look exactly like a legitimate continuation.
            #
            # If that record cannot be written, the caller still has to learn
            # why the nodes failed: losing the root cause behind a bookkeeping
            # error would leave the real failure invisible. Both are reported.
            node_failure = BaseExceptionGroup(
                "workflow nodes failed", [error for _, error in failures]
            )
            try:
                await self._finish(run_id, projection)
            except BaseException as terminal_error:
                cause = combine_failures(
                    node_failure,
                    informative_failure(terminal_error),
                    "workflow nodes failed and the run terminal was not recorded",
                )
                if isinstance(terminal_error, asyncio.CancelledError):
                    raise terminal_error from cause
                raise cause from None
            raise node_failure
        return projection

    async def _finish(
        self, run_id: str, projection: WorkflowProjection
    ) -> WorkflowProjection:
        failed = projection.failed_nodes()
        status = NodeStatus.FAILED.value if failed else NodeStatus.COMPLETED.value
        failure_code = "workflow-node-failed" if failed else None
        return await self._append_reload(
            run_id,
            projection,
            WORKFLOW_RUN_FINISHED,
            run_finished_data(status=status, failure_code=failure_code),
        )

    def _require_continuable(self, projection: WorkflowProjection) -> None:
        """The one interrupted state this stage may continue from.

        A node with a start fact and no terminal fact could have left an Agent
        claim, a Turn, a Budget hold, a provisional Workspace, a running capture
        or a running Review behind. Nothing here can tell which, so it refuses
        rather than guess.
        """

        running = [
            node_id
            for node_id in projection.running_nodes()
            if node_id not in projection.awaiting_approval
        ]
        if running:
            raise WorkflowRecoveryError("workflow-node-still-running", running[0])

    # -------------------------------------------------------------- appends

    async def _append_reload(
        self,
        run_id: str,
        projection: WorkflowProjection,
        event_type: str,
        data: dict[str, JsonValue],
    ) -> WorkflowProjection:
        await self._append(run_id, projection.head_seq, event_type, data)
        return await self._reader.load(run_id)

    async def _append(
        self,
        run_id: str,
        expected_seq: int,
        event_type: str,
        data: dict[str, JsonValue],
    ) -> None:
        stream_id = workflow_stream_id(run_id)
        attempts = 0
        while True:
            attempts += 1
            try:
                await self._store.append(
                    stream_id,
                    expected_seq=expected_seq,
                    events=(
                        PendingEvent(
                            type=event_type,
                            data=data,
                            schema_version=WORKFLOW_SCHEMA_VERSION,
                        ),
                    ),
                    durability=Durability.SYNC,
                )
                return
            except asyncio.CancelledError as error:
                await self._committed(run_id, event_type, data)
                raise error
            except Exception as error:
                committed = await self._committed(run_id, event_type, data)
                if committed is True:
                    return
                if isinstance(error, ConcurrencyConflict) and committed is False:
                    if attempts >= MAX_APPEND_ATTEMPTS:
                        raise WorkflowLedgerConflictError from None
                    expected_seq = (await self._reader.load(run_id)).head_seq
                    continue
                raise WorkflowWriteError(committed=committed) from None

    async def _committed(
        self, run_id: str, event_type: str, data: dict[str, JsonValue]
    ) -> bool | None:
        stream_id = workflow_stream_id(run_id)

        def matches(event: EventEnvelope) -> bool:
            return is_workflow_fact(event, stream_id, event_type, data)

        async def read() -> tuple[EventEnvelope, ...]:
            return await self._reader.read_events(run_id)

        return await committed_after_failure(read, matches)

    # ------------------------------------------------------------ ownership

    async def _owned(
        self,
        run_id: str,
        operation_digest: str,
        factory: Callable[[], Coroutine[Any, Any, WorkflowRun]],
        *,
        name: str,
    ) -> WorkflowRun:
        async with self._lock:
            if self._closed:
                raise WorkflowServiceClosedError
            entry = self._pending.get(run_id)
            if entry is None:
                task = asyncio.create_task(factory(), name=name)
                self._pending[run_id] = (operation_digest, task)
            else:
                recorded, task = entry
                if recorded != operation_digest:
                    raise WorkflowStateError("workflow-definition-changed")
        try:
            return await converge_workflow_task(task)
        finally:
            if task.done():
                async with self._lock:
                    current = self._pending.get(run_id)
                    if current is not None and current[1] is task:
                        self._pending.pop(run_id, None)

    async def aclose(self) -> None:
        async with self._lock:
            if self._close_task is None:
                self._closed = True
                tasks = tuple(task for _, task in self._pending.values())
                self._close_task = asyncio.create_task(
                    self._close(tasks), name="traceh-workflow-close"
                )
            task = self._close_task
        await _await_close(task)

    async def _close(self, tasks: tuple[asyncio.Task[WorkflowRun], ...]) -> None:
        failures: list[BaseException] = []
        for task in tasks:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                await await_worker_convergence(task)
                if task.cancelled():
                    failures.append(error)
                elif task.exception() is not None:
                    failures.append(task.exception())  # type: ignore[arg-type]
            except BaseException as error:
                failures.append(error)
        if failures:
            raise BaseExceptionGroup("workflow close failed", failures)


async def _converge_all(
    tasks: list[asyncio.Task[Any]],
) -> list[Any | BaseException]:
    """Wait for every node task, whatever happens to any of them.

    One node's failure must not release the caller while a sibling is still
    running: each task is converged individually and its outcome recorded.
    """

    cancellation: asyncio.CancelledError | None = None
    for task in tasks:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                cancellation = error if cancellation is None else cancellation
                await await_worker_convergence(task)
            except BaseException:
                break
    results: list[Any | BaseException] = []
    for task in tasks:
        if task.cancelled():
            results.append(asyncio.CancelledError())
            continue
        failure = task.exception()
        results.append(failure if failure is not None else task.result())
    if cancellation is not None:
        raise cancellation
    return results


async def converge_workflow_task(task: asyncio.Task[WorkflowRun]) -> WorkflowRun:
    """Wait for owned work; a cancelled caller still gets its own cancellation."""

    cancellation: asyncio.CancelledError | None = None
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as error:
        cancellation = error
        await await_worker_convergence(task)
    if task.cancelled():
        assert cancellation is not None
        raise cancellation
    failure = task.exception()
    cause = combine_failures(
        informative_failure(failure), None, "workflow run failed"
    )
    if cause is not None:
        if cancellation is not None:
            raise cancellation from cause
        raise cause
    if cancellation is not None:
        raise cancellation
    return task.result()


async def _await_close(task: asyncio.Task[None]) -> None:
    cancellation: asyncio.CancelledError | None = None
    try:
        await asyncio.shield(task)
        return
    except asyncio.CancelledError as error:
        cancellation = error
        await await_worker_convergence(task)
    if task.cancelled():
        assert cancellation is not None
        raise cancellation
    failure = task.exception()
    if failure is not None:
        raise cancellation from failure
    raise cancellation


def _require_one_durable_log(store: EventStore, services: WorkflowServices) -> None:
    """Every composed service must write to the one durable log this run uses.

    Splitting them produces two histories that cannot be checked against each
    other: the Workflow would record that a node created an Agent while the
    Agent facts live somewhere this run can never replay.
    """

    identity = durable_log_identity(store)
    for name, service in (
        ("supervisor", services.supervisor),
        ("capture", services.capture),
        ("promotion", services.promotion),
    ):
        if service is None:
            continue
        if durable_log_identity(service.store) is not identity:
            raise WorkflowInputError("workflow-event-store-mismatch", name)


def _require_same_definition(projection: WorkflowProjection, digest: str) -> None:
    if projection.head_seq != 0 and projection.definition_hash != digest:
        raise WorkflowStateError("workflow-definition-changed")


def _effective_node_id(node: WorkflowNode, map_key: str | None) -> str:
    if map_key is None:
        return node.node_id
    from traceh.workflow.models import map_child_node_id

    return map_child_node_id(node.node_id, map_key)


def _safe_code(code: object) -> str:
    """A durable failure code is a fixed identifier, never an exception message."""

    from traceh.agents.identity import is_agent_identifier

    if is_agent_identifier(code):
        return str(code)
    return "workflow-node-error"


__all__ = [
    "MAX_APPEND_ATTEMPTS",
    "WorkflowService",
    "WorkflowServices",
    "converge_workflow_task",
]
