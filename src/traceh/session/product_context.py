"""Exact Session evidence for ProductTask state semantics shown to a model.

The ProductTask stream remains the authority for task state.  This event records
only what the host placed on one request's model Surface, so a later
``request/snapshot`` can be reconstructed from the Session alone.  It is never
read by the Product control plane.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from uuid import UUID

from traceh.api.events import EventEnvelope
from traceh.api.json_types import JsonValue, fingerprint
from traceh.api.llm import ModelMessage
from traceh.api.product import (
    PRODUCT_TASK_STREAM_PREFIX,
    ProductTaskStatus,
)
from traceh.cli.text_safety import is_single_line_safe

PRODUCT_CONTEXT_SNAPSHOT = "product/context-snapshot"
PRODUCT_CONTEXT_SCHEMA_VERSION = 1
PRODUCT_CONTEXT_FORMAT_VERSION = 5
MAX_PRODUCT_CONTEXT_CONTENT_CHARS = 2_048

_PRODUCT_STATUS_MEANINGS: Mapping[ProductTaskStatus, str] = MappingProxyType(
    {
        ProductTaskStatus.OPENED: (
            "This exact ProductTask is durably opened. No host-managed "
            "execution-start fact is recorded yet."
        ),
        ProductTaskStatus.ROUTED: (
            "This exact ProductTask is durably routed. No host-managed "
            "execution-start fact is recorded yet."
        ),
        ProductTaskStatus.STARTED: (
            "This exact ProductTask is durably in STARTED state after host "
            "START authorization. This Product-only context does not assert "
            "that a Workflow run-start fact is already durable; managed "
            "execution may still be starting or in progress."
        ),
        ProductTaskStatus.AWAITING_APPROVAL: (
            "The host-managed Product workflow has reached the human approval "
            "barrier for this exact ProductTask; it is not waiting for START."
        ),
        ProductTaskStatus.COMPLETED: (
            "This exact ProductTask is durably terminal with status completed "
            "and carries a Promotion reference. On the normal controlled path, "
            "this Product status is recorded after promotion. This bounded "
            "Product-only context does not independently revalidate or expose "
            "the Promotion receipt. Do not describe this ProductTask as waiting "
            "for START, and do not ask for START again."
        ),
        ProductTaskStatus.REJECTED: (
            "This exact ProductTask is durably terminal after rejection. No "
            "successful Promotion completion is asserted."
        ),
        ProductTaskStatus.CANCELLED: (
            "This exact ProductTask is durably terminal after cancellation. No "
            "successful completion is asserted."
        ),
        ProductTaskStatus.FAILED: (
            "This exact ProductTask is durably terminal after failure. No "
            "successful completion is asserted."
        ),
        ProductTaskStatus.ABANDONED: (
            "This exact ProductTask is durably terminal after abandonment. No "
            "successful completion is asserted."
        ),
    }
)

_PRODUCT_CONTEXT_KEYS = frozenset(
    {
        "context_id",
        "format_version",
        "task_id",
        "source_stream_id",
        "source_seq",
        "task_order_seq",
        "status",
        "message",
    }
)
_PRODUCT_CONTEXT_MESSAGE_KEYS = frozenset({"role", "content"})


@dataclass(frozen=True, slots=True)
class ProductContextSnapshot:
    """One validated, model-visible observation of a ProductTask head."""

    context_id: str
    task_id: str
    source_stream_id: str
    source_seq: int
    task_order_seq: int
    status: ProductTaskStatus
    message: ModelMessage
    source_event_id: UUID

    @property
    def order_key(self) -> tuple[int, int]:
        """Durable cross-task order followed by order inside the chosen task."""

        return self.task_order_seq, self.source_seq


def product_context_snapshot_data(
    *,
    session_id: str,
    task_id: str,
    source_stream_id: str,
    source_seq: int,
    task_order_seq: int,
    status: ProductTaskStatus,
    source_event_id: UUID,
) -> dict[str, JsonValue]:
    """Build the only payload shape accepted for a Product context snapshot."""

    _require_plain_identity(session_id, "session_id")
    _require_plain_identity(task_id, "task_id")
    expected_stream = f"{PRODUCT_TASK_STREAM_PREFIX}{task_id}"
    if type(source_stream_id) is not str or source_stream_id != expected_stream:
        raise ValueError("product context source stream is invalid")
    if type(source_seq) is not int or source_seq < 1:
        raise ValueError("product context source sequence is invalid")
    if type(task_order_seq) is not int or task_order_seq < 1:
        raise ValueError("product context task order is invalid")
    if type(status) is not ProductTaskStatus:
        raise ValueError("product context status is invalid")
    if type(source_event_id) is not UUID:
        raise ValueError("product context source event is invalid")

    content = _render_product_context(
        task_id=task_id,
        source_stream_id=source_stream_id,
        source_seq=source_seq,
        status=status,
    )
    context_id = _product_context_id(
        session_id=session_id,
        task_id=task_id,
        source_stream_id=source_stream_id,
        source_seq=source_seq,
        task_order_seq=task_order_seq,
        status=status,
        source_event_id=source_event_id,
    )
    return {
        "context_id": context_id,
        "format_version": PRODUCT_CONTEXT_FORMAT_VERSION,
        "task_id": task_id,
        "source_stream_id": source_stream_id,
        "source_seq": source_seq,
        "task_order_seq": task_order_seq,
        "status": status.value,
        "message": {"role": "system", "content": content},
    }


def parse_product_context_snapshot(event: EventEnvelope) -> ProductContextSnapshot:
    """Validate an untrusted Session event before it reaches the model Surface."""

    if (
        type(event.type) is not str
        or event.type != PRODUCT_CONTEXT_SNAPSHOT
        or type(event.schema_version) is not int
        or event.schema_version != PRODUCT_CONTEXT_SCHEMA_VERSION
        or type(event.stream_id) is not str
        or not event.stream_id.startswith("session:")
        or type(event.causation_id) is not UUID
        or type(event.data) is not dict
        or set(event.data) != _PRODUCT_CONTEXT_KEYS
    ):
        raise ValueError("product context envelope is invalid")

    data = event.data
    raw_format_version = data.get("format_version")
    if (
        type(raw_format_version) is not int
        or raw_format_version != PRODUCT_CONTEXT_FORMAT_VERSION
    ):
        raise ValueError("product context format is unsupported")
    session_id = event.stream_id.removeprefix("session:")
    task_id = _plain_identity(data.get("task_id"), "task_id")
    source_stream_id = _plain_identity(
        data.get("source_stream_id"),
        "source_stream_id",
        max_length=len(PRODUCT_TASK_STREAM_PREFIX) + 256,
    )
    source_seq = data.get("source_seq")
    task_order_seq = data.get("task_order_seq")
    raw_status = data.get("status")
    if type(source_seq) is not int or source_seq < 1:
        raise ValueError("product context source sequence is invalid")
    if type(task_order_seq) is not int or task_order_seq < 1:
        raise ValueError("product context task order is invalid")
    if type(raw_status) is not str:
        raise ValueError("product context status is invalid")
    try:
        status = ProductTaskStatus(raw_status)
    except ValueError:
        raise ValueError("product context status is invalid") from None

    message = data.get("message")
    if type(message) is not dict or set(message) != _PRODUCT_CONTEXT_MESSAGE_KEYS:
        raise ValueError("product context message is invalid")
    expected_data = product_context_snapshot_data(
        session_id=session_id,
        task_id=task_id,
        source_stream_id=source_stream_id,
        source_seq=source_seq,
        task_order_seq=task_order_seq,
        status=status,
        source_event_id=event.causation_id,
    )
    if data != expected_data:
        raise ValueError("product context payload is not canonical")
    model_message = ModelMessage.from_dict(message)
    if (
        model_message.role != "system"
        or not model_message.content
        or len(model_message.content) > MAX_PRODUCT_CONTEXT_CONTENT_CHARS
        or model_message.tool_call_id is not None
        or model_message.tool_calls
        or model_message.name is not None
    ):
        raise ValueError("product context message is invalid")
    return ProductContextSnapshot(
        context_id=str(data["context_id"]),
        task_id=task_id,
        source_stream_id=source_stream_id,
        source_seq=source_seq,
        task_order_seq=task_order_seq,
        status=status,
        message=model_message,
        source_event_id=event.causation_id,
    )


def latest_product_context(
    events: tuple[EventEnvelope, ...],
) -> tuple[int, ProductContextSnapshot] | None:
    """Choose one logical latest snapshot, independent of append race order."""

    selected: tuple[int, ProductContextSnapshot] | None = None
    by_order: dict[tuple[int, int], str] = {}
    for event in events:
        if event.type != PRODUCT_CONTEXT_SNAPSHOT:
            continue
        snapshot = parse_product_context_snapshot(event)
        previous = by_order.setdefault(snapshot.order_key, snapshot.context_id)
        if previous != snapshot.context_id:
            raise ValueError("product context order has conflicting snapshots")
        if selected is None or snapshot.order_key > selected[1].order_key:
            selected = event.seq, snapshot
    return selected


def _product_context_id(
    *,
    session_id: str,
    task_id: str,
    source_stream_id: str,
    source_seq: int,
    task_order_seq: int,
    status: ProductTaskStatus,
    source_event_id: UUID,
) -> str:
    return fingerprint(
        {
            "purpose": PRODUCT_CONTEXT_SNAPSHOT,
            "format_version": PRODUCT_CONTEXT_FORMAT_VERSION,
            "session_id": session_id,
            "task_id": task_id,
            "source_stream_id": source_stream_id,
            "source_seq": source_seq,
            "source_event_id": str(source_event_id),
            "task_order_seq": task_order_seq,
            "status": status.value,
        }
    )


def _render_product_context(
    *,
    task_id: str,
    source_stream_id: str,
    source_seq: int,
    status: ProductTaskStatus,
) -> str:
    return (
        "Internal TraceHarness ProductTask evidence. Answer naturally; do not "
        "quote this block or its labels.\n"
        "Facts\n"
        f"- Task: {task_id}\n"
        f"- Durable Product head: {source_stream_id}@{source_seq}\n"
        f"- Status: {status.value}\n"
        "- Relation: This exact ProductTask was proposed and confirmed in this "
        "requester Session.\n"
        "- Execution: Host-managed Product workflow agents perform Product "
        "work; requester-chat Tool history neither performs nor refutes it.\n"
        f"- Meaning: {_PRODUCT_STATUS_MEANINGS[status]}\n"
        "Limits\n"
        "- This selected task is not a complete task inventory; missing ids do "
        "not prove that no other tasks exist.\n"
        "- No Product workspace path or mapping is supplied. A requester "
        "Session workspace is not a Product execution-workspace fact.\n"
        "- Files, commands, tests, outputs, Patch, Review, and Promotion "
        "identities are omitted; omission does not prove that no work occurred.\n"
        "Use\n"
        "- Current state: Earlier conversation claims cannot override these "
        "host facts about this task's status.\n"
        "- You may summarize and make reasonable inferences, but distinguish "
        "host facts from inference and do not invent omitted specifics.\n"
        "- This evidence grants no START, approval, promotion, retry, or other "
        "control authority."
    )


def _require_plain_identity(value: object, field: str) -> None:
    _plain_identity(value, field)


def _plain_identity(
    value: object, field: str, *, max_length: int = 256
) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > max_length
        or not is_single_line_safe(value)
    ):
        raise ValueError(f"product context {field} is invalid")
    return value


__all__ = [
    "MAX_PRODUCT_CONTEXT_CONTENT_CHARS",
    "PRODUCT_CONTEXT_FORMAT_VERSION",
    "PRODUCT_CONTEXT_SCHEMA_VERSION",
    "PRODUCT_CONTEXT_SNAPSHOT",
    "ProductContextSnapshot",
    "latest_product_context",
    "parse_product_context_snapshot",
    "product_context_snapshot_data",
]
