"""The append-only orchestration vocabulary of one Workflow run.

The stream records *orchestration* only: which node started, what it expanded
into, and how it ended. The Agent's report, the Patch bytes, the Review evidence
and the human Approval remain owned by their own fact sources; this stream keeps
identities that point at them, never copies of their state.
"""

from __future__ import annotations

from datetime import UTC, datetime

from traceh.api.events import EventEnvelope
from traceh.api.json_types import JsonValue, canonical_json
from traceh.api.workflow import NodeStatus, WorkflowNodeKind
from traceh.workflow.errors import WorkflowInputError, WorkflowProtocolError
from traceh.workflow.models import (
    MAX_FAN_OUT,
    WORKFLOW_PROTOCOL_VERSION,
    map_child_node_id,
    require_workflow_identifier,
)

WORKFLOW_SCHEMA_VERSION = 1
WORKFLOW_RUN_STARTED = "workflow/run-started"
WORKFLOW_NODE_STARTED = "workflow/node-started"
WORKFLOW_MAP_EXPANDED = "workflow/map-expanded"
WORKFLOW_NODE_COMPLETED = "workflow/node-completed"
WORKFLOW_NODE_FAILED = "workflow/node-failed"
WORKFLOW_APPROVAL_AWAITED = "workflow/approval-awaited"
WORKFLOW_RUN_FINISHED = "workflow/run-finished"

WORKFLOW_EVENT_TYPES = (
    WORKFLOW_RUN_STARTED,
    WORKFLOW_NODE_STARTED,
    WORKFLOW_MAP_EXPANDED,
    WORKFLOW_NODE_COMPLETED,
    WORKFLOW_NODE_FAILED,
    WORKFLOW_APPROVAL_AWAITED,
    WORKFLOW_RUN_FINISHED,
)

_RUN_STARTED_KEYS = frozenset(
    {"run_id", "definition_id", "definition_hash", "workflow_protocol_version"}
)
_NODE_STARTED_KEYS = frozenset({"node_id", "kind", "map_key"})
_MAP_EXPANDED_KEYS = frozenset({"node_id", "map_keys", "child_node_ids"})
_NODE_COMPLETED_KEYS = frozenset(
    {
        "node_id",
        "kind",
        "map_key",
        "agent_id",
        "message_id",
        "artifact_id",
        "review_id",
        "approval_digest",
    }
)
_NODE_FAILED_KEYS = frozenset({"node_id", "kind", "map_key", "failure_code"})
_APPROVAL_AWAITED_KEYS = frozenset({"node_id", "review_id"})
_RUN_FINISHED_KEYS = frozenset({"status", "failure_code"})

_TERMINAL_STATUSES = (NodeStatus.COMPLETED.value, NodeStatus.FAILED.value)


def workflow_stream_id(run_id: str) -> str:
    run_id = require_workflow_identifier(run_id, field="run_id")
    return f"workflow:{run_id}"


def run_started_data(
    *, run_id: str, definition_id: str, definition_hash: str
) -> dict[str, JsonValue]:
    return {
        "run_id": require_workflow_identifier(run_id, field="run_id"),
        "definition_id": require_workflow_identifier(
            definition_id, field="definition_id"
        ),
        "definition_hash": _require_digest(definition_hash, "definition_hash"),
        "workflow_protocol_version": WORKFLOW_PROTOCOL_VERSION,
    }


def node_started_data(
    *, node_id: str, kind: WorkflowNodeKind, map_key: str | None
) -> dict[str, JsonValue]:
    return {
        "node_id": require_workflow_identifier(node_id, field="node_id"),
        "kind": _require_kind(kind),
        "map_key": _optional_identifier(map_key),
    }


def map_expanded_data(
    *, node_id: str, map_keys: tuple[str, ...]
) -> dict[str, JsonValue]:
    node_id = require_workflow_identifier(node_id, field="node_id")
    if type(map_keys) is not tuple or not map_keys or len(map_keys) > MAX_FAN_OUT:
        raise WorkflowInputError("workflow-map-keys-invalid", "map_keys")
    keys = [require_workflow_identifier(key, field="map_key") for key in map_keys]
    if len(set(keys)) != len(keys):
        raise WorkflowInputError("workflow-map-keys-duplicate", "map_keys")
    return {
        "node_id": node_id,
        "map_keys": keys,
        "child_node_ids": [map_child_node_id(node_id, key) for key in keys],
    }


def node_completed_data(
    *,
    node_id: str,
    kind: WorkflowNodeKind,
    map_key: str | None,
    agent_id: str | None = None,
    message_id: str | None = None,
    artifact_id: str | None = None,
    review_id: str | None = None,
    approval_digest: str | None = None,
) -> dict[str, JsonValue]:
    return {
        "node_id": require_workflow_identifier(node_id, field="node_id"),
        "kind": _require_kind(kind),
        "map_key": _optional_identifier(map_key),
        "agent_id": _optional_identifier(agent_id),
        "message_id": _optional_identifier(message_id),
        "artifact_id": _optional_identifier(artifact_id),
        "review_id": _optional_identifier(review_id),
        "approval_digest": (
            None
            if approval_digest is None
            else _require_digest(approval_digest, "approval_digest")
        ),
    }


def node_failed_data(
    *,
    node_id: str,
    kind: WorkflowNodeKind,
    map_key: str | None,
    failure_code: str,
) -> dict[str, JsonValue]:
    return {
        "node_id": require_workflow_identifier(node_id, field="node_id"),
        "kind": _require_kind(kind),
        "map_key": _optional_identifier(map_key),
        "failure_code": require_workflow_identifier(
            failure_code, field="failure_code"
        ),
    }


def approval_awaited_data(*, node_id: str, review_id: str) -> dict[str, JsonValue]:
    return {
        "node_id": require_workflow_identifier(node_id, field="node_id"),
        "review_id": require_workflow_identifier(review_id, field="review_id"),
    }


def run_finished_data(
    *, status: str, failure_code: str | None
) -> dict[str, JsonValue]:
    if type(status) is not str or status not in _TERMINAL_STATUSES:
        raise WorkflowInputError("workflow-run-status-invalid", "status")
    return {
        "status": status,
        "failure_code": (
            None
            if failure_code is None
            else require_workflow_identifier(failure_code, field="failure_code")
        ),
    }


def workflow_event_header(
    event: EventEnvelope, stream_id: str
) -> tuple[str, dict[str, JsonValue], datetime, int]:
    """Validate what every Workflow fact shares before any payload is read."""

    try:
        return _workflow_event_header(event, stream_id)
    except WorkflowProtocolError:
        raise
    except Exception:
        raise WorkflowProtocolError("workflow-payload-invalid", _safe_seq(event)) from None


def _workflow_event_header(
    event: EventEnvelope, stream_id: str
) -> tuple[str, dict[str, JsonValue], datetime, int]:
    if type(event.stream_id) is not str or event.stream_id != stream_id:
        raise WorkflowProtocolError("workflow-stream-unexpected", _safe_seq(event))
    seq = event.seq
    if type(seq) is not int or seq < 1:
        raise WorkflowProtocolError("workflow-sequence-invalid", 0)
    if (
        type(event.schema_version) is not int
        or event.schema_version != WORKFLOW_SCHEMA_VERSION
    ):
        raise WorkflowProtocolError("workflow-schema-version-unsupported", seq)
    if type(event.type) is not str or event.type not in WORKFLOW_EVENT_TYPES:
        raise WorkflowProtocolError("workflow-event-type-unknown", seq)
    if type(event.occurred_at) is not datetime or event.occurred_at.tzinfo is None:
        raise WorkflowProtocolError("workflow-recorded-at-invalid", seq)
    data = event.data
    if type(data) is not dict:
        raise WorkflowProtocolError("workflow-payload-invalid", seq)
    return str(event.type), data, event.occurred_at.astimezone(UTC), seq


def expected_keys(event_type: str) -> frozenset[str]:
    return {
        WORKFLOW_RUN_STARTED: _RUN_STARTED_KEYS,
        WORKFLOW_NODE_STARTED: _NODE_STARTED_KEYS,
        WORKFLOW_MAP_EXPANDED: _MAP_EXPANDED_KEYS,
        WORKFLOW_NODE_COMPLETED: _NODE_COMPLETED_KEYS,
        WORKFLOW_NODE_FAILED: _NODE_FAILED_KEYS,
        WORKFLOW_APPROVAL_AWAITED: _APPROVAL_AWAITED_KEYS,
        WORKFLOW_RUN_FINISHED: _RUN_FINISHED_KEYS,
    }[event_type]


def is_workflow_fact(
    event: EventEnvelope, stream_id: str, event_type: str, data: dict[str, JsonValue]
) -> bool:
    """Whether ``event`` is exactly the fact a failed append tried to write."""

    try:
        actual_type, _, _, _ = workflow_event_header(event, stream_id)
    except WorkflowProtocolError:
        return False
    if actual_type != event_type:
        return False
    # An encoding failure is unknowable and intentionally propagates to the
    # shared reconciler, which maps it to ``None`` rather than false absence.
    return canonical_json(event.data) == canonical_json(data)


def _require_kind(kind: object) -> str:
    if type(kind) is not WorkflowNodeKind:
        raise WorkflowInputError("workflow-node-kind-invalid", "kind")
    return kind.value


def _optional_identifier(value: object) -> str | None:
    if value is None:
        return None
    return require_workflow_identifier(value, field="identity")


def _require_digest(value: object, field: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise WorkflowInputError(f"workflow-{field.replace('_', '-')}-invalid", field)
    return value


def _safe_seq(event: object) -> int:
    try:
        seq = event.seq  # type: ignore[attr-defined]
    except Exception:
        return 0
    return seq if type(seq) is int and seq >= 0 else 0


__all__ = [
    "WORKFLOW_APPROVAL_AWAITED",
    "WORKFLOW_EVENT_TYPES",
    "WORKFLOW_MAP_EXPANDED",
    "WORKFLOW_NODE_COMPLETED",
    "WORKFLOW_NODE_FAILED",
    "WORKFLOW_NODE_STARTED",
    "WORKFLOW_RUN_FINISHED",
    "WORKFLOW_RUN_STARTED",
    "WORKFLOW_SCHEMA_VERSION",
    "approval_awaited_data",
    "expected_keys",
    "is_workflow_fact",
    "map_expanded_data",
    "node_completed_data",
    "node_failed_data",
    "node_started_data",
    "run_finished_data",
    "run_started_data",
    "workflow_event_header",
    "workflow_stream_id",
]
