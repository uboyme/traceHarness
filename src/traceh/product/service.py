"""The one host-owned writer of ProductTask facts.

This is not a scheduler and not a product. It records what a host decided, in an
order the contract permits, with values earlier facts already fixed - and it
refuses everything else. It starts no Workflow, calls no model, promotes
nothing, and knows about no Chat.

Four rules govern every write, and all four are reused rather than reinvented:

* the projection that validated the stream is the same one whose ``head_seq``
  becomes the compare-and-swap expectation, so there is no window between
  "checked the history" and "wrote against a head read separately";
* an ``operation_id`` is idempotent only for a byte-identical canonical payload;
  the same id with different content is a conflict, never a quiet success;
* a failed or cancelled append is reconciled through the shared three-state
  `committed_after_failure()`, so "unknown" survives as unknown;
* one in-flight write per task is owned by one Task, and a cancelled caller
  waits for it and re-raises its *own* cancellation - repeatedly cancelling
  cannot release it early or cut the reconciliation short.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any, Protocol

from traceh.agents.commit_reconciliation import committed_after_failure
from traceh.api.events import EventEnvelope, PendingEvent
from traceh.api.json_types import JsonValue, canonical_json
from traceh.api.product import (
    PRODUCT_TASK_ABANDONED,
    PRODUCT_TASK_AWAITING,
    PRODUCT_TASK_CANCELLED,
    PRODUCT_TASK_COMPLETED,
    PRODUCT_TASK_FAILED,
    PRODUCT_TASK_OPENED,
    PRODUCT_TASK_REJECTED,
    PRODUCT_TASK_ROUTED,
    PRODUCT_TASK_SCHEMA_VERSION,
    PRODUCT_TASK_STARTED,
    ProductAssemblyReceipt,
    ProductTaskProposal,
    ProductTaskSummary,
    ProductTaskView,
    ProductTaskViewStatus,
    ProposalConfirmation,
    RequestedTaskMode,
    TaskRouting,
    product_event_contract,
    product_required_values,
    product_transition_allowed,
    proposal_confirmable,
)
from traceh.api.workflow import WorkflowStatus
from traceh.concurrency import await_worker_convergence
from traceh.product.errors import (
    ProductInputError,
    ProductOperationConflictError,
    ProductServiceClosedError,
    ProductStateError,
    ProductStreamConflictError,
    ProductWriteError,
)
from traceh.product.events import (
    ParsedProductEvent,
    is_product_fact,
    normalize_task_opening,
    product_task_stream,
    require_product_identifier,
    task_abandoned_data,
    task_awaiting_data,
    task_cancelled_data,
    task_completed_data,
    task_failed_data,
    task_rejected_data,
    task_routed_data,
    task_started_data,
)
from traceh.product.evidence import (
    SessionEvidenceReader,
    require_confirmation_evidence,
)
from traceh.product.projection import (
    ProductTaskStreamReader,
    replay_product_task,
)
from traceh.session.event_store import ConcurrencyConflict, Durability, EventStore

MAX_APPEND_ATTEMPTS = 8
"""How often one fact may lose a stream compare-and-swap before failing."""


class WorkflowStateSource(Protocol):
    """Where a fresh Workflow status comes from.

    A seam rather than a stored value, because a view that cached this would
    report a barrier that had already been passed. Every ``view()`` call asks
    again.
    """

    @property
    def store(self) -> EventStore:
        ...

    async def workflow_status(self, run_id: str) -> WorkflowStatus | None:
        ...


class TaskOwnershipSource(Protocol):
    """Whether this process still owns the run behind a task.

    Also asked fresh every time: ownership is exactly the fact that changes when
    the thing this view is about stops being true.
    """

    def owns_task(self, task_id: str) -> bool:
        ...


class ProductTaskService:
    """Record ProductTask facts. Nothing here executes any of them."""

    __slots__ = (
        "_close_task",
        "_closed",
        "_lock",
        "_ownership",
        "_pending",
        "_reader",
        "_sessions",
        "_store",
        "_workflow",
    )

    def __init__(
        self,
        store: EventStore,
        *,
        sessions: SessionEvidenceReader,
        workflow: WorkflowStateSource,
        ownership: TaskOwnershipSource,
    ) -> None:
        self._store = store
        self._reader = ProductTaskStreamReader(store)
        self._sessions = sessions
        self._workflow = workflow
        self._ownership = ownership
        self._lock = asyncio.Lock()
        self._pending: dict[str, tuple[str, asyncio.Task[ProductTaskSummary]]] = {}
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._require_session_store()
        self._require_workflow_store()

    @property
    def store(self) -> EventStore:
        return self._store

    # ----------------------------------------------------------------- reads

    async def load(self, task_id: str) -> ProductTaskSummary | None:
        """Fresh replay. ``None`` means the task has no stream."""

        return await self._reader.load(
            require_product_identifier(task_id, field="task_id")
        )

    async def view(self, task_id: str) -> ProductTaskView | None:
        """Derive the current answer from all three sources, read fresh.

        None of the three is stored between calls. A cached Workflow status
        would keep reporting a barrier that has been passed; cached ownership
        would keep claiming a run this process no longer drives.
        """

        summary = await self.load(task_id)
        if summary is None:
            return None
        self._require_workflow_store()
        workflow_status = await self._workflow.workflow_status(
            summary.workflow_run_id
        )
        if workflow_status is not None and type(workflow_status) is not WorkflowStatus:
            raise ProductInputError("product-workflow-status-invalid", "workflow_status")
        owned = self._ownership.owns_task(summary.task_id)
        if type(owned) is not bool:
            raise ProductInputError("product-ownership-invalid", "owned_by_this_host")
        return ProductTaskView(
            summary=summary,
            workflow_status=workflow_status,
            owned_by_this_host=owned,
        )

    # ---------------------------------------------------------------- writes

    async def open_task(
        self,
        *,
        task_id: str,
        operation_id: str,
        proposal: ProductTaskProposal,
        confirmation: ProposalConfirmation,
    ) -> ProductTaskSummary:
        """Open a task from an offer a person is proven to have accepted."""

        task_id = require_product_identifier(task_id, field="task_id")
        opening = normalize_task_opening(
            task_id=task_id,
            operation_id=operation_id,
            proposal=proposal,
            confirmation=confirmation,
        )
        if not proposal_confirmable(opening.proposal, opening.confirmation):
            raise ProductStateError("product-confirmation-invalid", task_id)
        return await self._write(
            task_id,
            PRODUCT_TASK_OPENED,
            opening.data,
            before=lambda: self._require_confirmation(
                opening.proposal, opening.confirmation
            ),
        )

    async def _require_confirmation(
        self,
        proposal: ProductTaskProposal,
        confirmation: ProposalConfirmation,
    ) -> None:
        self._require_session_store()
        await require_confirmation_evidence(self._sessions, proposal, confirmation)

    def _require_session_store(self) -> None:
        try:
            same_store = self._sessions.store is self._store
        except Exception:
            same_store = False
        if not same_store:
            raise ProductInputError("product-session-store-mismatch", "sessions")

    def _require_workflow_store(self) -> None:
        try:
            same_store = self._workflow.store is self._store
        except Exception:
            same_store = False
        if not same_store:
            raise ProductInputError("product-workflow-store-mismatch", "workflow")

    async def record_routing(
        self,
        *,
        task_id: str,
        operation_id: str,
        routing: TaskRouting,
        router_agent_id: str,
        routing_session_id: str,
    ) -> ProductTaskSummary:
        task_id = require_product_identifier(task_id, field="task_id")
        data = task_routed_data(
            task_id=task_id,
            operation_id=operation_id,
            routing=routing,
            router_agent_id=router_agent_id,
            routing_session_id=routing_session_id,
        )
        return await self._write(task_id, PRODUCT_TASK_ROUTED, data)

    async def start_task(
        self, *, task_id: str, operation_id: str, receipt: ProductAssemblyReceipt
    ) -> ProductTaskSummary:
        """Start against the exact binding the person confirmed, or not at all.

        ``binds()`` runs before anything is written, and it needs the Receipt -
        which is why it lives here rather than in the projector. The started
        payload then repeats ``preflight_digest`` so a later replay can make the
        weaker comparison it *is* able to make.
        """

        task_id = require_product_identifier(task_id, field="task_id")
        data = task_started_data(
            task_id=task_id, operation_id=operation_id, receipt=receipt
        )

        def check(summary: ProductTaskSummary) -> None:
            if not receipt.binds(summary.preflight_digest):
                raise ProductStateError("product-preflight-drifted", task_id)

        return await self._write(task_id, PRODUCT_TASK_STARTED, data, inspect=check)

    async def record_awaiting(
        self, *, task_id: str, operation_id: str, review_id: str
    ) -> ProductTaskSummary:
        task_id = require_product_identifier(task_id, field="task_id")
        data = task_awaiting_data(
            task_id=task_id, operation_id=operation_id, review_id=review_id
        )
        return await self._write(task_id, PRODUCT_TASK_AWAITING, data)

    async def complete_task(
        self, *, task_id: str, operation_id: str, promotion_id: str
    ) -> ProductTaskSummary:
        task_id = require_product_identifier(task_id, field="task_id")
        data = task_completed_data(
            task_id=task_id, operation_id=operation_id, promotion_id=promotion_id
        )
        return await self._write(task_id, PRODUCT_TASK_COMPLETED, data)

    async def reject_task(
        self, *, task_id: str, operation_id: str, review_id: str
    ) -> ProductTaskSummary:
        task_id = require_product_identifier(task_id, field="task_id")
        data = task_rejected_data(
            task_id=task_id, operation_id=operation_id, review_id=review_id
        )
        return await self._write(task_id, PRODUCT_TASK_REJECTED, data)

    async def cancel_task(
        self, *, task_id: str, operation_id: str, reason_code: str
    ) -> ProductTaskSummary:
        task_id = require_product_identifier(task_id, field="task_id")
        data = task_cancelled_data(
            task_id=task_id, operation_id=operation_id, reason_code=reason_code
        )
        return await self._write(task_id, PRODUCT_TASK_CANCELLED, data)

    async def fail_task(
        self, *, task_id: str, operation_id: str, failure_code: str
    ) -> ProductTaskSummary:
        task_id = require_product_identifier(task_id, field="task_id")
        data = task_failed_data(
            task_id=task_id, operation_id=operation_id, failure_code=failure_code
        )
        return await self._write(task_id, PRODUCT_TASK_FAILED, data)

    async def abandon_task(
        self, *, task_id: str, operation_id: str, reason_code: str
    ) -> ProductTaskSummary:
        """Mark a task abandoned, only where the derived view says ``interrupted``.

        This is the honest terminal, and it is honest only because of the
        condition: the view derives ``interrupted`` from all three fresh reads,
        and nothing else may be written down as one. It explicitly does *not*
        claim the Agent claim, Budget hold or worktree were released - a process
        that died proved nothing, which is precisely why ``cancelled`` is a
        different fact.
        """

        task_id = require_product_identifier(task_id, field="task_id")
        data = task_abandoned_data(
            task_id=task_id, operation_id=operation_id, reason_code=reason_code
        )

        async def interrupted() -> None:
            view = await self.view(task_id)
            if view is None:
                raise ProductStateError("product-task-unknown", task_id)
            if view.status is not ProductTaskViewStatus.INTERRUPTED:
                raise ProductStateError("product-task-not-interrupted", task_id)

        return await self._write(
            task_id, PRODUCT_TASK_ABANDONED, data, before=interrupted
        )

    # ------------------------------------------------------------- machinery

    async def _write(
        self,
        task_id: str,
        event_type: str,
        data: dict[str, JsonValue],
        *,
        before: Callable[[], Coroutine[Any, Any, None]] | None = None,
        inspect: Callable[[ProductTaskSummary], None] | None = None,
    ) -> ProductTaskSummary:
        digest = canonical_json({"type": event_type, "data": data})
        return await self._owned(
            task_id,
            digest,
            lambda: self._record(task_id, event_type, data, before, inspect),
            name=f"traceh-product-{event_type.rpartition('/')[2]}",
        )

    async def _record(
        self,
        task_id: str,
        event_type: str,
        data: dict[str, JsonValue],
        before: Callable[[], Coroutine[Any, Any, None]] | None,
        inspect: Callable[[ProductTaskSummary], None] | None,
    ) -> ProductTaskSummary:
        stream_id = product_task_stream(task_id)
        attempts = 0
        while True:
            attempts += 1
            # One read answers three questions - is this a repeat, is the fact
            # admissible, and what is the CAS expectation - so the history that
            # was approved is exactly the history written against. Reading the
            # head separately would reopen that window.
            events = await self._reader.read_events(task_id)
            summary, parsed_events = replay_product_task(task_id, events)
            repeat = _existing_operation(parsed_events, event_type, data)
            if repeat is _Existing.SAME:
                assert summary is not None
                return summary
            if repeat is _Existing.CONFLICT:
                raise ProductOperationConflictError
            # The Receipt-level check runs first so a drifted binding reports
            # what actually went wrong, rather than surfacing as the generic
            # decided-value mismatch it also happens to cause.
            if summary is not None and inspect is not None:
                inspect(summary)
            _require_admissible(summary, event_type, data, task_id)
            if before is not None:
                await before()
            head_seq = 0 if summary is None else summary.head_seq
            try:
                await self._store.append(
                    stream_id,
                    expected_seq=head_seq,
                    events=(
                        PendingEvent(
                            type=event_type,
                            data=data,
                            schema_version=PRODUCT_TASK_SCHEMA_VERSION,
                        ),
                    ),
                    durability=Durability.SYNC,
                )
            except asyncio.CancelledError as error:
                # A cancellation landing inside the store's critical section
                # leaves the event durable. Reconciling before re-raising is
                # what stops a later attempt from writing a second copy.
                await self._committed(task_id, event_type, data)
                raise error
            except Exception as error:
                committed = await self._committed(task_id, event_type, data)
                if committed is True:
                    break
                if isinstance(error, ConcurrencyConflict) and committed is False:
                    if attempts >= MAX_APPEND_ATTEMPTS:
                        raise ProductStreamConflictError from None
                    continue
                raise ProductWriteError(committed=committed) from None
            break
        try:
            stored = await self._reader.load(task_id)
        except Exception:
            # ``append`` returned normally, so commit is no longer uncertain.
            # A broken result read must not erase that known fact or leak the
            # Store's exception vocabulary through the Product API.
            raise ProductWriteError(committed=True) from None
        if stored is None:
            raise ProductWriteError(committed=True)
        return stored

    async def _committed(
        self, task_id: str, event_type: str, data: dict[str, JsonValue]
    ) -> bool | None:
        stream_id = product_task_stream(task_id)

        def matches(event: EventEnvelope) -> bool:
            return is_product_fact(event, stream_id, event_type, data)

        async def read() -> tuple[EventEnvelope, ...]:
            return await self._reader.read_events(task_id)

        return await committed_after_failure(read, matches)

    async def _owned(
        self,
        task_id: str,
        operation_digest: str,
        factory: Callable[[], Coroutine[Any, Any, ProductTaskSummary]],
        *,
        name: str,
    ) -> ProductTaskSummary:
        async with self._lock:
            if self._closed:
                raise ProductServiceClosedError
            entry = self._pending.get(task_id)
            if entry is None:
                task = asyncio.create_task(factory(), name=name)
                self._pending[task_id] = (operation_digest, task)
            else:
                recorded, task = entry
                # Sharing an in-flight write is only correct when it is the same
                # write. Otherwise the second caller receives a receipt for work
                # it never described.
                if recorded != operation_digest:
                    raise ProductOperationConflictError
        try:
            return await converge_product_task(task)
        finally:
            if task.done():
                async with self._lock:
                    current = self._pending.get(task_id)
                    if current is not None and current[1] is task:
                        self._pending.pop(task_id, None)

    async def aclose(self) -> None:
        async with self._lock:
            if self._close_task is None:
                self._closed = True
                tasks = tuple(task for _, task in self._pending.values())
                self._close_task = asyncio.create_task(
                    self._close(tasks), name="traceh-product-close"
                )
            task = self._close_task
        await _await_close(task)

    async def _close(self, tasks: tuple[asyncio.Task[ProductTaskSummary], ...]) -> None:
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
            raise BaseExceptionGroup("product task close failed", failures)


class _Existing:
    """Whether this operation id is already in the stream, and with what."""

    NONE = "none"
    SAME = "same"
    CONFLICT = "conflict"


def _existing_operation(
    events: tuple[ParsedProductEvent, ...],
    event_type: str,
    data: dict[str, JsonValue],
) -> str:
    """Decide idempotency by content, not by identity alone.

    A repeat carrying the same ``operation_id`` and a byte-identical canonical
    payload is the same write and returns the same answer. The same id carrying
    anything else is a conflict, and it is refused *before* the append: letting
    it through would put a duplicate operation id in the stream, which the
    projector would then reject forever.
    """

    operation_id = data.get("operation_id")
    encoded = canonical_json(data)
    for event in events:
        actual_type, payload = event.event_type, event.data
        if payload.get("operation_id") != operation_id:
            continue
        if actual_type == event_type and canonical_json(payload) == encoded:
            return _Existing.SAME
        return _Existing.CONFLICT
    return _Existing.NONE


def _require_admissible(
    summary: ProductTaskSummary | None,
    event_type: str,
    data: dict[str, JsonValue],
    task_id: str,
) -> None:
    """Refuse anything the projector would refuse, before it is written.

    The rules are not restated here. ``PRODUCT_TASK_TRANSITIONS`` decides order
    and ``product_required_values()`` decides the values earlier facts fixed, so
    a writer cannot be more permissive than a reader - which is what would
    happen if the service kept its own table of allowed predecessors.
    """

    contract = product_event_contract(event_type)
    if contract is None:  # pragma: no cover - callers pass frozen constants
        raise ProductStateError("product-event-type-unknown", task_id)
    if summary is None:
        if event_type != PRODUCT_TASK_OPENED:
            raise ProductStateError("product-task-unknown", task_id)
        requested = _requested_mode_of(data, task_id)
        current = None
    else:
        if event_type == PRODUCT_TASK_OPENED:
            raise ProductStateError("product-task-exists", task_id)
        requested = summary.requested_mode
        current = summary.status
    if not product_transition_allowed(
        current, contract.status, requested_mode=requested
    ):
        raise ProductStateError("product-transition-invalid", task_id)
    if summary is None:
        return
    required = product_required_values(event_type, summary.facts())
    if required is None:
        raise ProductStateError("product-transition-invalid", task_id)
    for key, value in required.items():
        if data.get(key) != value:
            raise ProductStateError("product-decided-value-invalid", task_id)


def _requested_mode_of(data: dict[str, JsonValue], task_id: str) -> RequestedTaskMode:
    value = data.get("requested_mode")
    if type(value) is not str:
        raise ProductStateError("product-requested-mode-invalid", task_id)
    try:
        return RequestedTaskMode(value)
    except ValueError:
        raise ProductStateError("product-requested-mode-invalid", task_id) from None


async def converge_product_task(
    task: asyncio.Task[ProductTaskSummary],
) -> ProductTaskSummary:
    """Wait for owned work; a cancelled caller still gets its own cancellation.

    Neither failure hides the other: when the caller was cancelled *and* the
    work failed, the cancellation is what the caller asked for and the failure
    is chained onto it.
    """

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
    if failure is not None:
        if cancellation is not None:
            raise cancellation from failure
        raise failure
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


__all__ = [
    "MAX_APPEND_ATTEMPTS",
    "ProductTaskService",
    "TaskOwnershipSource",
    "WorkflowStateSource",
    "converge_product_task",
]
