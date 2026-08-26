"""The one Projector that rebuilds a ProductTask from its own stream.

There is no status file, no cache and no second store. Every load replays the
whole stream and rebuilds the summary, so a reader and a writer cannot hold two
opinions about where a task got to.

Three kinds of rule run here, and keeping them apart is what makes the projector
honest about its own reach:

* **shape** - stream, sequence, schema, type and the exact key set F0 froze;
* **order** - `PRODUCT_TASK_TRANSITIONS`, so a fact cannot follow one it may not;
* **value** - `product_required_values()`, so a field an earlier fact already
  decided is recomputed rather than trusted.

What it deliberately does *not* check is the half of ``product/task-started``
that only a Receipt can decide. A replaying reader holds an opaque
``assembly_digest`` and cannot rebuild a Receipt from it; the started fact
repeats ``preflight_digest`` precisely so that this layer still has something to
compare against the opening fact.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace

from traceh.api.events import EventEnvelope
from traceh.api.json_types import JsonValue
from traceh.api.product import (
    PRODUCT_TASK_ABANDONED,
    PRODUCT_TASK_AWAITING,
    PRODUCT_TASK_CANCELLED,
    PRODUCT_TASK_COMPLETED,
    PRODUCT_TASK_FAILED,
    PRODUCT_TASK_OPENED,
    PRODUCT_TASK_PROTOCOL_VERSION,
    PRODUCT_TASK_REJECTED,
    PRODUCT_TASK_ROUTED,
    PRODUCT_TASK_STARTED,
    ProductTaskStatus,
    ProductTaskSummary,
    RequestedTaskMode,
    ResolvedTaskMode,
    TaskModeSource,
    product_event_contract,
    product_required_values,
    product_transition_allowed,
)
from traceh.product.errors import ProductProtocolError
from traceh.product.events import (
    ParsedProductEvent,
    parse_product_event,
    product_task_stream,
    protocol_digest,
    protocol_display_text,
    protocol_identifier,
    require_product_identifier,
)
from traceh.session.event_store import EventStore


@dataclass(frozen=True, slots=True)
class ProductTaskIssue:
    """A stable, non-secret validation result for one ProductTask stream."""

    code: str
    seq: int


def rebuild_product_task(
    task_id: str, events: tuple[EventEnvelope, ...]
) -> ProductTaskSummary | None:
    """Replay one task's stream, or return ``None`` when it has none.

    ``None`` is not an empty summary. Every required field of a summary is
    established by ``product/task-opened``; without that event there is no
    status, no mode and no origin, and returning a summary-shaped value would
    mean inventing all of them.
    """

    summary, _ = replay_product_task(task_id, events)
    return summary


def replay_product_task(
    task_id: str, events: tuple[EventEnvelope, ...]
) -> tuple[ProductTaskSummary | None, tuple[ParsedProductEvent, ...]]:
    """Replay once and retain the same normalized facts for idempotency checks."""

    normalized_task_id = require_product_identifier(task_id, field="task_id")
    stream_id = product_task_stream(normalized_task_id)
    summary: ProductTaskSummary | None = None
    parsed_events: list[ParsedProductEvent] = []
    operations: set[str] = set()
    expected_seq = 1
    for event in events:
        parsed = parse_product_event(event, stream_id)
        event_type, data, seq = parsed.event_type, parsed.data, parsed.seq
        if seq != expected_seq:
            raise ProductProtocolError("product-sequence-invalid", seq)
        expected_seq += 1

        # The payload repeats the identity the stream name already carries, so
        # the two can be checked against each other instead of one being
        # trusted alone.
        if protocol_identifier(data.get("task_id"), seq) != normalized_task_id:
            raise ProductProtocolError("product-task-identity-invalid", seq)

        operation_id = protocol_identifier(data.get("operation_id"), seq)
        if operation_id in operations:
            raise ProductProtocolError("product-operation-duplicate", seq)
        operations.add(operation_id)

        contract = product_event_contract(event_type)
        assert contract is not None  # require_exact_keys already refused unknown
        _require_allowed(summary, event_type, contract.status, data, seq)
        _require_decided_values(summary, event_type, data, seq)
        summary = _apply(summary, event_type, data, normalized_task_id, seq)
        parsed_events.append(parsed)
    return summary, tuple(parsed_events)


def _require_allowed(
    summary: ProductTaskSummary | None,
    event_type: str,
    following: ProductTaskStatus,
    data: Mapping[str, JsonValue],
    seq: int,
) -> None:
    if summary is None:
        if event_type != PRODUCT_TASK_OPENED:
            raise ProductProtocolError("product-transition-invalid", seq)
        requested = _requested_mode(data.get("requested_mode"), seq)
        current = None
    else:
        requested = summary.requested_mode
        current = summary.status
    if not product_transition_allowed(current, following, requested_mode=requested):
        raise ProductProtocolError("product-transition-invalid", seq)


def _require_decided_values(
    summary: ProductTaskSummary | None,
    event_type: str,
    data: Mapping[str, JsonValue],
    seq: int,
) -> None:
    """Recompute, rather than trust, every field an earlier fact already fixed."""

    if summary is None:
        return
    required = product_required_values(event_type, summary.facts())
    if required is None:
        # The transition check should already have refused this, so reaching
        # here means the two contracts disagree - which is a protocol failure,
        # not something to paper over with a permissive branch.
        raise ProductProtocolError("product-decided-value-unavailable", seq)
    for key, value in required.items():
        if data.get(key) != value:
            raise ProductProtocolError("product-decided-value-invalid", seq)


def _apply(
    summary: ProductTaskSummary | None,
    event_type: str,
    data: Mapping[str, JsonValue],
    task_id: str,
    seq: int,
) -> ProductTaskSummary:
    if event_type == PRODUCT_TASK_OPENED:
        assert summary is None
        if data.get("product_protocol_version") != PRODUCT_TASK_PROTOCOL_VERSION:
            raise ProductProtocolError("product-protocol-version-unsupported", seq)
        return ProductTaskSummary(
            task_id=task_id,
            status=ProductTaskStatus.OPENED,
            requested_mode=_requested_mode(data.get("requested_mode"), seq),
            mode_source=_mode_source(data.get("mode_source"), seq),
            requirement_digest=protocol_digest(
                data.get("requirement_digest"), lengths=(64,), seq=seq
            ),
            profile_digest=protocol_digest(
                data.get("profile_digest"), lengths=(64,), seq=seq
            ),
            preflight_digest=protocol_digest(
                data.get("preflight_digest"), lengths=(64,), seq=seq
            ),
            origin_session_id=protocol_identifier(data.get("origin_session_id"), seq),
            origin_turn_id=protocol_identifier(data.get("origin_turn_id"), seq),
            origin_message_id=protocol_identifier(data.get("origin_message_id"), seq),
            confirmation_session_id=protocol_identifier(
                data.get("confirmation_session_id"), seq
            ),
            confirmation_turn_id=protocol_identifier(
                data.get("confirmation_turn_id"), seq
            ),
            confirmation_message_id=protocol_identifier(
                data.get("confirmation_message_id"), seq
            ),
            head_seq=seq,
        )

    assert summary is not None
    if event_type == PRODUCT_TASK_ROUTED:
        return replace(
            summary,
            status=ProductTaskStatus.ROUTED,
            resolved_mode=_resolved_mode(data.get("resolved_mode"), seq),
            reason_display=protocol_display_text(data.get("reason_display"), seq),
            router_agent_id=protocol_identifier(data.get("router_agent_id"), seq),
            routing_session_id=protocol_identifier(
                data.get("routing_session_id"), seq
            ),
            head_seq=seq,
        )
    if event_type == PRODUCT_TASK_STARTED:
        return replace(
            summary,
            status=ProductTaskStatus.STARTED,
            # ``mode`` was already required to equal what an explicit request or
            # a routing decision fixed, so recording it cannot introduce a
            # second opinion.
            resolved_mode=_resolved_mode(data.get("mode"), seq),
            definition_hash=protocol_digest(
                data.get("definition_hash"), lengths=(64,), seq=seq
            ),
            assembly_digest=protocol_digest(
                data.get("assembly_digest"), lengths=(64,), seq=seq
            ),
            source_base_revision=protocol_digest(
                data.get("source_base_revision"), lengths=(40, 64), seq=seq
            ),
            head_seq=seq,
        )
    if event_type == PRODUCT_TASK_AWAITING:
        return replace(
            summary,
            status=ProductTaskStatus.AWAITING_APPROVAL,
            review_id=protocol_identifier(data.get("review_id"), seq),
            head_seq=seq,
        )
    if event_type == PRODUCT_TASK_COMPLETED:
        return replace(
            summary,
            status=ProductTaskStatus.COMPLETED,
            promotion_id=protocol_identifier(data.get("promotion_id"), seq),
            head_seq=seq,
        )
    if event_type == PRODUCT_TASK_REJECTED:
        return replace(
            summary,
            status=ProductTaskStatus.REJECTED,
            review_id=protocol_identifier(data.get("review_id"), seq),
            head_seq=seq,
        )
    if event_type == PRODUCT_TASK_CANCELLED:
        return replace(
            summary,
            status=ProductTaskStatus.CANCELLED,
            reason_code=protocol_identifier(data.get("reason_code"), seq),
            head_seq=seq,
        )
    if event_type == PRODUCT_TASK_FAILED:
        return replace(
            summary,
            status=ProductTaskStatus.FAILED,
            failure_code=protocol_identifier(data.get("failure_code"), seq),
            head_seq=seq,
        )
    if event_type == PRODUCT_TASK_ABANDONED:
        return replace(
            summary,
            status=ProductTaskStatus.ABANDONED,
            reason_code=protocol_identifier(data.get("reason_code"), seq),
            head_seq=seq,
        )
    raise ProductProtocolError("product-event-type-unknown", seq)


def _requested_mode(value: object, seq: int) -> RequestedTaskMode:
    return _member(RequestedTaskMode, value, seq)


def _resolved_mode(value: object, seq: int) -> ResolvedTaskMode:
    return _member(ResolvedTaskMode, value, seq)


def _mode_source(value: object, seq: int) -> TaskModeSource:
    return _member(TaskModeSource, value, seq)


def _member[T](enum: type[T], value: object, seq: int) -> T:
    # ``type(value) is str`` rather than ``isinstance``: a ``str`` subclass that
    # compares equal here could behave differently when the value is stored,
    # compared again or rendered.
    if type(value) is not str:
        raise ProductProtocolError("product-enum-invalid", seq)
    try:
        return enum(value)  # type: ignore[call-arg]
    except ValueError:
        raise ProductProtocolError("product-enum-invalid", seq) from None


class ProductTaskStreamReader:
    """Fresh replay of one ProductTask stream. Implements ``ProductTaskReader``."""

    __slots__ = ("_store",)

    def __init__(self, store: EventStore) -> None:
        self._store = store

    @property
    def store(self) -> EventStore:
        return self._store

    async def read_events(self, task_id: str) -> tuple[EventEnvelope, ...]:
        return await self._store.read(product_task_stream(task_id))

    async def load(self, task_id: str) -> ProductTaskSummary | None:
        return rebuild_product_task(task_id, await self.read_events(task_id))


async def validate_product_task(
    store: EventStore, task_id: str
) -> tuple[ProductTaskIssue, ...]:
    try:
        await ProductTaskStreamReader(store).load(task_id)
    except ProductProtocolError as error:
        return (ProductTaskIssue(error.code, error.seq),)
    return ()


__all__ = [
    "ProductTaskIssue",
    "ProductTaskStreamReader",
    "replay_product_task",
    "rebuild_product_task",
    "validate_product_task",
]
