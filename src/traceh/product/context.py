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
from traceh.api.product import PRODUCT_TASK_STREAM_PREFIX, ProductTaskStatus
from traceh.concurrency import await_worker_convergence
from traceh.product.errors import (
    ProductContextError,
    ProductError,
    ProductInputError,
    ProductStateError,
)
from traceh.product.events import task_id_from_stream
from traceh.product.evidence import SessionEvidenceReader
from traceh.product.projection import ProductTaskStreamReader, replay_product_task
from traceh.session.event_store import ConcurrencyConflict, EventStore
from traceh.session.product_context import (
    PRODUCT_CONTEXT_SCHEMA_VERSION,
    PRODUCT_CONTEXT_SNAPSHOT,
    ProductContextSnapshot,
    latest_product_context,
    parse_product_context_snapshot,
    product_context_snapshot_data,
)
from traceh.session.service import SessionService
from traceh.supervision.execution import durable_log_identity

MAX_PRODUCT_CONTEXT_APPEND_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class _Candidate:
    task_id: str
    status: ProductTaskStatus
    source_event: EventEnvelope
    task_order_seq: int

    @property
    def order_key(self) -> tuple[int, int]:
        return self.task_order_seq, self.source_event.seq


class ProductModelContext:
    """Synchronize one safe Product state observation before model dispatch."""

    __slots__ = ("_evidence", "_reader", "_sessions", "_store")

    def __init__(self, sessions: SessionService, store: EventStore) -> None:
        if type(sessions) is not SessionService:
            raise ProductInputError("product-context-sessions-invalid", "sessions")
        if durable_log_identity(sessions.store) is not durable_log_identity(store):
            raise ProductInputError("product-context-store-mismatch", "sessions")
        self._sessions = sessions
        self._store = store
        self._reader = ProductTaskStreamReader(store)
        self._evidence = SessionEvidenceReader(store)

    async def synchronize(self, session_id: str) -> ProductContextSnapshot | None:
        """Append a changed snapshot, or reuse the latest exact durable one.

        A CAS conflict restarts the fresh reads.  This matters when an older
        reader loses a race: retrying its fixed payload would append a stale
        state after the newer one.  Logical ordering in the payload additionally
        prevents such a stale append from winning the model projection.
        """

        for attempt in range(1, MAX_PRODUCT_CONTEXT_APPEND_ATTEMPTS + 1):
            try:
                candidate = await self._latest_candidate(session_id)
                if candidate is None:
                    return None
                data = product_context_snapshot_data(
                    session_id=session_id,
                    task_id=candidate.task_id,
                    source_stream_id=candidate.source_event.stream_id,
                    source_seq=candidate.source_event.seq,
                    task_order_seq=candidate.task_order_seq,
                    status=candidate.status,
                    source_event_id=candidate.source_event.event_id,
                )
                history = await self._sessions.read_session(session_id)
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
                if snapshot.order_key > candidate.order_key:
                    # Another fresh reader already recorded a later canonical
                    # task/head.  Never append this older observation behind it.
                    return snapshot
                if snapshot.order_key == candidate.order_key:
                    raise ProductContextError("product-context-history-conflict")

            expected_seq = history[-1].seq if history else 0
            append = asyncio.create_task(
                self._sessions.append_session(
                    session_id,
                    PRODUCT_CONTEXT_SNAPSHOT,
                    data,
                    expected_seq=expected_seq,
                    causation_id=candidate.source_event.event_id,
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
                    candidate.source_event.event_id,
                )
                if not append.cancelled() and append.exception() is not None:
                    raise cancellation from append.exception()
                raise cancellation
            except Exception as error:
                committed = await self._committed(
                    session_id,
                    data,
                    candidate.source_event.event_id,
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

    async def _latest_candidate(self, session_id: str) -> _Candidate | None:
        messages = {
            message.message_id: message
            for message in await self._evidence.messages(session_id)
        }
        candidates: list[_Candidate] = []
        live: list[_Candidate] = []
        streams = await self._store.list_streams(prefix=PRODUCT_TASK_STREAM_PREFIX)
        for stream_id in streams:
            task_id = task_id_from_stream(stream_id)
            events = await self._reader.read_events(task_id)
            summary, _ = replay_product_task(task_id, events)
            if summary is None:
                raise ProductContextError("product-context-product-history-invalid")
            related = session_id in {
                summary.origin_session_id,
                summary.confirmation_session_id,
            }
            if not related:
                continue
            if (
                summary.origin_session_id != session_id
                or summary.confirmation_session_id != session_id
            ):
                raise ProductContextError("product-context-session-mismatch")
            evidence = messages.get(summary.confirmation_message_id)
            if evidence is None:
                raise ProductContextError("product-context-confirmation-missing")
            if evidence.turn_id != summary.confirmation_turn_id:
                raise ProductContextError("product-context-confirmation-mismatch")
            head = events[-1]
            if head.seq != summary.head_seq:
                raise ProductContextError("product-context-product-history-invalid")
            candidate = _Candidate(
                task_id=task_id,
                status=summary.status,
                source_event=head,
                task_order_seq=evidence.accepted_seq,
            )
            candidates.append(candidate)
            if not summary.settled:
                live.append(candidate)

        if len(live) > 1:
            raise ProductStateError("product-observation-session-ambiguous")
        if live:
            return live[0]
        if not candidates:
            return None
        latest_order = max(candidate.task_order_seq for candidate in candidates)
        latest = [
            candidate
            for candidate in candidates
            if candidate.task_order_seq == latest_order
        ]
        if len(latest) != 1:
            raise ProductStateError("product-context-session-ambiguous")
        return latest[0]

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


__all__ = [
    "MAX_PRODUCT_CONTEXT_APPEND_ATTEMPTS",
    "ProductModelContext",
]
