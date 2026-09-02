"""Freeze canonical ProductTask state semantics into the requester Surface.

This bridge performs a fresh cross-stream read before an ordinary Product Chat
Turn.  It copies no Product authority into the Session: the copied event is
evidence of what the model was shown, while every control decision continues to
replay the canonical ``product-task:*`` stream.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

from traceh.agents.commit_reconciliation import committed_after_failure
from traceh.api.events import EventEnvelope
from traceh.api.json_types import JsonValue, canonical_json
from traceh.api.product import (
    PRODUCT_TASK_STREAM_PREFIX,
    ProductTaskStatus,
    RequestedTaskMode,
    ResolvedTaskMode,
)
from traceh.concurrency import await_worker_convergence
from traceh.product.errors import (
    ProductContextError,
    ProductError,
    ProductEvidenceError,
    ProductInputError,
    ProductStateError,
)
from traceh.product.events import task_id_from_stream
from traceh.product.memory import (
    ProductTaskMemory,
    ProductTaskMemoryHead,
    ProductTaskMemoryReader,
)
from traceh.session.event_store import ConcurrencyConflict, EventStore
from traceh.session.product_context import (
    MAX_PRODUCT_CONTEXT_TASKS,
    PRODUCT_CONTEXT_SCHEMA_VERSION,
    PRODUCT_CONTEXT_SNAPSHOT,
    ProductContextExecutionSummary,
    ProductContextSnapshot,
    ProductContextTask,
    bounded_product_context_excerpt,
    latest_product_context,
    parse_product_context_snapshot,
    product_context_snapshot_data,
)
from traceh.session.service import SessionService
from traceh.supervision.execution import durable_log_identity

MAX_PRODUCT_CONTEXT_APPEND_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class _Candidate:
    memory_head: ProductTaskMemoryHead
    task_id: str
    status: ProductTaskStatus
    source_event: EventEnvelope
    task_order_seq: int
    requested_mode: RequestedTaskMode
    resolved_mode: ResolvedTaskMode | None
    requirement_digest: str
    origin_message_id: str
    source_excerpt: str
    source_excerpt_truncated: bool
    execution_summary: ProductContextExecutionSummary | None = None

    @property
    def order_key(self) -> tuple[int, int]:
        return self.task_order_seq, self.source_event.seq

    @property
    def context_task(self) -> ProductContextTask:
        return ProductContextTask(
            task_id=self.task_id,
            source_stream_id=self.source_event.stream_id,
            source_seq=self.source_event.seq,
            source_event_id=self.source_event.event_id,
            task_order_seq=self.task_order_seq,
            status=self.status,
            requested_mode=self.requested_mode,
            resolved_mode=self.resolved_mode,
            requirement_digest=self.requirement_digest,
            origin_message_id=self.origin_message_id,
            source_excerpt=self.source_excerpt,
            source_excerpt_truncated=self.source_excerpt_truncated,
            execution_summary=self.execution_summary,
        )


class ProductModelContext:
    """Synchronize safe current and recent Product facts before dispatch."""

    __slots__ = ("_memory", "_sessions", "_store")

    def __init__(
        self,
        sessions: SessionService,
        store: EventStore,
        memory: ProductTaskMemoryReader,
    ) -> None:
        if type(sessions) is not SessionService:
            raise ProductInputError("product-context-sessions-invalid", "sessions")
        if durable_log_identity(sessions.store) is not durable_log_identity(store):
            raise ProductInputError("product-context-store-mismatch", "sessions")
        if (
            not isinstance(memory, ProductTaskMemoryReader)
            or durable_log_identity(memory.store) is not durable_log_identity(store)
        ):
            raise ProductInputError("product-context-store-mismatch", "memory")
        self._sessions = sessions
        self._store = store
        self._memory = memory

    async def synchronize(self, session_id: str) -> ProductContextSnapshot | None:
        """Append a changed snapshot, or reuse the latest exact durable one.

        A CAS conflict restarts the fresh reads.  This matters when an older
        reader loses a race: retrying its fixed payload would append a stale
        state after the newer one.  Logical ordering in the payload additionally
        prevents such a stale append from winning the model projection.
        """

        for attempt in range(1, MAX_PRODUCT_CONTEXT_APPEND_ATTEMPTS + 1):
            try:
                catalog = await self._catalog(session_id)
                if catalog is None:
                    return None
                focus, selected, total_tasks = catalog
                data = product_context_snapshot_data(
                    session_id=session_id,
                    focus=focus.context_task,
                    tasks=tuple(candidate.context_task for candidate in selected),
                    total_tasks=total_tasks,
                )
                history = await self._sessions.read_session(session_id)
            except ProductStateError as error:
                if (
                    error.code == "product-memory-product-head-changed"
                    and attempt < MAX_PRODUCT_CONTEXT_APPEND_ATTEMPTS
                ):
                    continue
                raise
            except ProductError:
                raise
            except Exception:
                raise ProductContextError("product-context-read-failed") from None
            try:
                current = latest_product_context(history)
            except ValueError:
                raise ProductContextError("product-context-history-invalid") from None
            if current is not None:
                snapshot = current[1]
                if snapshot.context_id == data["context_id"]:
                    return snapshot
                if snapshot.order_key > focus.order_key:
                    # Another fresh reader already recorded a later canonical
                    # task/head.  Never append this older observation behind it.
                    return snapshot
                if snapshot.order_key == focus.order_key:
                    raise ProductContextError("product-context-history-conflict")

            expected_seq = history[-1].seq if history else 0
            append = asyncio.create_task(
                self._sessions.append_session(
                    session_id,
                    PRODUCT_CONTEXT_SNAPSHOT,
                    data,
                    expected_seq=expected_seq,
                    causation_id=focus.source_event.event_id,
                ),
                name="traceh-product-context-snapshot",
            )
            try:
                event = await asyncio.shield(append)
            except asyncio.CancelledError as cancellation:
                await await_worker_convergence(append)
                # The exact reread is required even though cancellation still
                # wins: it converges a may-have-committed append before the
                # caller can retry or close the Store.
                await self._committed(
                    session_id,
                    data,
                    focus.source_event.event_id,
                )
                if not append.cancelled() and append.exception() is not None:
                    raise cancellation from append.exception()
                raise cancellation
            except Exception as error:
                committed = await self._committed(
                    session_id,
                    data,
                    focus.source_event.event_id,
                )
                if committed is True:
                    return await self._read_exact(session_id, str(data["context_id"]))
                if (
                    isinstance(error, ConcurrencyConflict)
                    and committed is False
                    and attempt < MAX_PRODUCT_CONTEXT_APPEND_ATTEMPTS
                ):
                    continue
                code = (
                    "product-context-write-unknown"
                    if committed is None
                    else "product-context-write-failed"
                )
                raise ProductContextError(code, committed=committed) from None
            try:
                return parse_product_context_snapshot(event)
            except ValueError:
                raise ProductContextError(
                    "product-context-write-invalid", committed=True
                ) from None
        raise ProductContextError("product-context-session-changed")

    async def _catalog(
        self, session_id: str
    ) -> tuple[_Candidate, tuple[_Candidate, ...], int] | None:
        candidates: list[_Candidate] = []
        live: list[_Candidate] = []
        streams = await self._store.list_streams(prefix=PRODUCT_TASK_STREAM_PREFIX)
        for stream_id in streams:
            task_id = task_id_from_stream(stream_id)
            try:
                memory_head = await self._memory.load_head_if_related(session_id, task_id)
            except ProductEvidenceError as error:
                raise ProductContextError(_context_relation_code(error.code)) from None
            if memory_head is None:
                continue
            summary = memory_head.summary
            head = memory_head.source_event
            origin = memory_head.relation.origin
            source_excerpt, source_excerpt_truncated = (
                bounded_product_context_excerpt(origin.content)
            )
            candidate = _Candidate(
                memory_head=memory_head,
                task_id=task_id,
                status=summary.status,
                source_event=head,
                task_order_seq=memory_head.relation.confirmation.accepted_seq,
                requested_mode=summary.requested_mode,
                resolved_mode=summary.resolved_mode,
                requirement_digest=summary.requirement_digest,
                origin_message_id=summary.origin_message_id,
                source_excerpt=source_excerpt,
                source_excerpt_truncated=source_excerpt_truncated,
            )
            candidates.append(candidate)
            if not summary.settled:
                live.append(candidate)

        if len(live) > 1:
            raise ProductStateError("product-observation-session-ambiguous")
        if not candidates:
            return None
        if len({candidate.task_order_seq for candidate in candidates}) != len(
            candidates
        ):
            raise ProductStateError("product-context-session-ambiguous")
        focus = (
            live[0]
            if live
            else max(candidates, key=lambda candidate: candidate.task_order_seq)
        )
        if focus.status in {
            ProductTaskStatus.AWAITING_APPROVAL,
            ProductTaskStatus.COMPLETED,
            ProductTaskStatus.REJECTED,
            ProductTaskStatus.CANCELLED,
            ProductTaskStatus.FAILED,
        }:
            memory = await self._memory.load_for_head(session_id, focus.memory_head)
            focus = _candidate_with_execution(focus, memory)
        recent = sorted(
            (candidate for candidate in candidates if candidate.task_id != focus.task_id),
            key=lambda candidate: candidate.task_order_seq,
            reverse=True,
        )
        selected = (focus, *recent[: MAX_PRODUCT_CONTEXT_TASKS - 1])
        return focus, selected, len(candidates)

    async def _committed(
        self,
        session_id: str,
        data: dict[str, JsonValue],
        source_event_id: UUID,
    ) -> bool | None:
        expected_payload = canonical_json(data)

        def matches(event: EventEnvelope) -> bool:
            return (
                event.stream_id == self._sessions.session_stream(session_id)
                and event.type == PRODUCT_CONTEXT_SNAPSHOT
                and event.schema_version == PRODUCT_CONTEXT_SCHEMA_VERSION
                and event.causation_id == source_event_id
                # JSON identity is deliberately type-sensitive. Python would
                # otherwise treat hostile values such as ``True`` and ``1``
                # as equal while reconciling a may-have-committed append.
                and canonical_json(event.data) == expected_payload
            )

        return await committed_after_failure(
            lambda: self._sessions.read_session(session_id), matches
        )

    async def _read_exact(
        self, session_id: str, context_id: str
    ) -> ProductContextSnapshot:
        try:
            snapshots = tuple(
                parse_product_context_snapshot(event)
                for event in await self._sessions.read_session(session_id)
                if event.type == PRODUCT_CONTEXT_SNAPSHOT
            )
        except Exception:
            raise ProductContextError(
                "product-context-write-unreadable", committed=True
            ) from None
        for snapshot in reversed(snapshots):
            if snapshot.context_id == context_id:
                return snapshot
        raise ProductContextError("product-context-write-unreadable", committed=True)


def _candidate_with_execution(
    candidate: _Candidate,
    memory: ProductTaskMemory,
) -> _Candidate:
    evidence = memory.observation.evidence
    review = None if evidence is None else evidence.review
    durable_review = memory.observation.review
    if (review is None) != (durable_review is None):
        raise ProductContextError("product-context-review-evidence-incomplete")
    execution = ProductContextExecutionSummary(
        workflow_status=memory.observation.workflow_status,
        managed_tool_call_count=memory.activity.tool_call_count,
        changed_path_count=None if review is None else len(review.changed_paths),
        verification_passed=(
            None if durable_review is None else durable_review.passed
        ),
        verifier_count=None if review is None else len(review.verifiers),
        promotion_recorded=memory.observation.promotion is not None,
    )
    return _Candidate(
        memory_head=candidate.memory_head,
        task_id=candidate.task_id,
        status=candidate.status,
        source_event=candidate.source_event,
        task_order_seq=candidate.task_order_seq,
        requested_mode=candidate.requested_mode,
        resolved_mode=candidate.resolved_mode,
        requirement_digest=candidate.requirement_digest,
        origin_message_id=candidate.origin_message_id,
        source_excerpt=candidate.source_excerpt,
        source_excerpt_truncated=candidate.source_excerpt_truncated,
        execution_summary=execution,
    )


def _context_relation_code(code: str) -> str:
    return {
        "product-task-session-mismatch": "product-context-session-mismatch",
        "product-task-origin-missing": "product-context-origin-missing",
        "product-task-origin-mismatch": "product-context-origin-mismatch",
        "product-task-confirmation-missing": "product-context-confirmation-missing",
        "product-task-confirmation-mismatch": "product-context-confirmation-mismatch",
        "product-task-origin-incomplete": "product-context-origin-incomplete",
        "product-task-confirmation-order-invalid": (
            "product-context-confirmation-order-invalid"
        ),
    }.get(code, "product-context-session-evidence-invalid")


__all__ = [
    "MAX_PRODUCT_CONTEXT_APPEND_ATTEMPTS",
    "ProductModelContext",
]
