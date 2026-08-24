"""Workspace catalog event vocabulary shared by writers and replay."""

from __future__ import annotations

from dataclasses import dataclass

from traceh.agents.identity import is_agent_identifier
from traceh.api.events import EventEnvelope
from traceh.api.json_types import JsonValue, canonical_json
from traceh.api.workspaces import WorkspaceAccess, WorkspaceProvisioningRequest
from traceh.workspaces.errors import WorkspaceInputError, WorkspaceProtocolError

WORKSPACE_CATALOG_STREAM = "workspaces:catalog"
WORKSPACE_SCHEMA_VERSION = 1

WORKSPACE_PROVISIONED = "workspace/provisioned"
WORKSPACE_ATTACHED = "workspace/attached"
WORKSPACE_QUARANTINED = "workspace/quarantined"
WORKSPACE_RELEASED = "workspace/released"

QUARANTINE_REASONS = frozenset(
    {
        "agent-identity-unknown",
        "agent-identity-conflict",
        "git-state-unknown",
        "path-unsafe",
        "session-workspace-mismatch",
        "workspace-dirty",
        "workspace-write-unknown",
    }
)
RELEASE_REASONS = frozenset(
    {
        "agent-not-created",
        "explicit-release",
        "rejected",
        "merged",
    }
)

_PROVISIONED_KEYS = frozenset(
    {
        "operation_id",
        "workspace_id",
        "creation_request_id",
        "source_id",
        "source_revision",
        "repository_fingerprint",
        "base_revision",
        "access",
        "owner_agent_id",
    }
)
_ATTACHED_KEYS = frozenset(
    {"operation_id", "workspace_id", "agent_id", "session_id"}
)
_TERMINAL_KEYS = frozenset({"operation_id", "workspace_id", "reason"})


@dataclass(frozen=True, slots=True)
class WorkspaceProvisionedFact:
    operation_id: str
    workspace_id: str
    creation_request_id: str
    source_id: str
    source_revision: str
    repository_fingerprint: str
    base_revision: str
    access: WorkspaceAccess
    owner_agent_id: str | None
    seq: int


@dataclass(frozen=True, slots=True)
class WorkspaceAttachedFact:
    operation_id: str
    workspace_id: str
    agent_id: str
    session_id: str
    seq: int


@dataclass(frozen=True, slots=True)
class WorkspaceQuarantinedFact:
    operation_id: str
    workspace_id: str
    reason: str
    seq: int


@dataclass(frozen=True, slots=True)
class WorkspaceReleasedFact:
    operation_id: str
    workspace_id: str
    reason: str
    seq: int


type WorkspaceFact = (
    WorkspaceProvisionedFact
    | WorkspaceAttachedFact
    | WorkspaceQuarantinedFact
    | WorkspaceReleasedFact
)


def require_workspace_identifier(value: object, *, field: str) -> str:
    try:
        valid = is_agent_identifier(value)
        normalized = str(value) if valid else ""
    except Exception:
        valid = False
        normalized = ""
    if not valid or not is_agent_identifier(normalized):
        raise WorkspaceInputError("workspace-identity-invalid", field)
    return normalized


def freeze_provisioning_request(value: object) -> WorkspaceProvisioningRequest:
    if not isinstance(value, WorkspaceProvisioningRequest):
        raise WorkspaceInputError("workspace-request-invalid", "request")
    source_id = require_workspace_identifier(value.source_id, field="source_id")
    revision = require_workspace_identifier(value.revision, field="revision")
    if type(value.access) is not WorkspaceAccess:
        raise WorkspaceInputError("workspace-access-invalid", "access")
    return WorkspaceProvisioningRequest(source_id, revision, value.access)


def _is_hex_digest(value: object, lengths: tuple[int, ...]) -> bool:
    return (
        type(value) is str
        and len(value) in lengths
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


def provisioned_data(
    *,
    operation_id: str,
    workspace_id: str,
    creation_request_id: str,
    source_id: str,
    source_revision: str,
    repository_fingerprint: str,
    base_revision: str,
    access: WorkspaceAccess,
    owner_agent_id: str | None,
) -> dict[str, JsonValue]:
    if not _is_hex_digest(repository_fingerprint, (64,)):
        raise WorkspaceInputError(
            "workspace-repository-fingerprint-invalid",
            "repository_fingerprint",
        )
    if not _is_hex_digest(base_revision, (40, 64)):
        raise WorkspaceInputError(
            "workspace-base-revision-invalid", "base_revision"
        )
    if type(access) is not WorkspaceAccess:
        raise WorkspaceInputError("workspace-access-invalid", "access")
    return {
        "operation_id": require_workspace_identifier(
            operation_id, field="operation_id"
        ),
        "workspace_id": require_workspace_identifier(
            workspace_id, field="workspace_id"
        ),
        "creation_request_id": require_workspace_identifier(
            creation_request_id, field="creation_request_id"
        ),
        "source_id": require_workspace_identifier(source_id, field="source_id"),
        "source_revision": require_workspace_identifier(
            source_revision, field="source_revision"
        ),
        "repository_fingerprint": repository_fingerprint,
        "base_revision": base_revision,
        "access": access.value,
        "owner_agent_id": (
            None
            if owner_agent_id is None
            else require_workspace_identifier(
                owner_agent_id, field="owner_agent_id"
            )
        ),
    }


def attached_data(
    *, operation_id: str, workspace_id: str, agent_id: str, session_id: str
) -> dict[str, JsonValue]:
    return {
        "operation_id": require_workspace_identifier(
            operation_id, field="operation_id"
        ),
        "workspace_id": require_workspace_identifier(
            workspace_id, field="workspace_id"
        ),
        "agent_id": require_workspace_identifier(agent_id, field="agent_id"),
        "session_id": require_workspace_identifier(session_id, field="session_id"),
    }


def lifecycle_data(
    *,
    operation_id: str,
    workspace_id: str,
    reason: str,
    allowed_reasons: frozenset[str],
) -> dict[str, JsonValue]:
    if type(reason) is not str or reason not in allowed_reasons:
        raise WorkspaceInputError("workspace-reason-invalid", "reason")
    return {
        "operation_id": require_workspace_identifier(
            operation_id, field="operation_id"
        ),
        "workspace_id": require_workspace_identifier(
            workspace_id, field="workspace_id"
        ),
        "reason": reason,
    }


def _read_identifier(data: dict[str, JsonValue], key: str, seq: int) -> str:
    value = data.get(key)
    if not is_agent_identifier(value):
        raise WorkspaceProtocolError("workspace-identity-invalid", seq)
    assert isinstance(value, str)
    return str(value)


def _read_optional_identifier(
    data: dict[str, JsonValue], key: str, seq: int
) -> str | None:
    value = data[key]
    if value is None:
        return None
    return _read_identifier(data, key, seq)


def _read_access(value: object, seq: int) -> WorkspaceAccess:
    try:
        return WorkspaceAccess(value)
    except (TypeError, ValueError):
        raise WorkspaceProtocolError("workspace-access-invalid", seq) from None


def _read_digest(
    value: object, *, lengths: tuple[int, ...], code: str, seq: int
) -> str:
    if not _is_hex_digest(value, lengths):
        raise WorkspaceProtocolError(code, seq)
    assert isinstance(value, str)
    return value


def _read_reason(
    data: dict[str, JsonValue], allowed: frozenset[str], seq: int
) -> str:
    value = data.get("reason")
    if type(value) is not str or value not in allowed:
        raise WorkspaceProtocolError("workspace-payload-invalid", seq)
    return value


def parse_workspace_fact(event: EventEnvelope) -> WorkspaceFact:
    try:
        return _parse_workspace_fact(event)
    except WorkspaceProtocolError:
        raise
    except Exception:
        try:
            seq = event.seq
        except Exception:
            seq = 0
        if type(seq) is not int:
            seq = 0
        raise WorkspaceProtocolError("workspace-payload-invalid", seq) from None


def _parse_workspace_fact(event: EventEnvelope) -> WorkspaceFact:
    if event.stream_id != WORKSPACE_CATALOG_STREAM:
        raise WorkspaceProtocolError("workspace-stream-unexpected", event.seq)
    if event.schema_version != WORKSPACE_SCHEMA_VERSION:
        raise WorkspaceProtocolError(
            "workspace-schema-version-unsupported", event.seq
        )
    data = event.data
    if not isinstance(data, dict):
        raise WorkspaceProtocolError("workspace-payload-invalid", event.seq)
    if event.type == WORKSPACE_PROVISIONED:
        if set(data) != _PROVISIONED_KEYS:
            raise WorkspaceProtocolError(
                "workspace-payload-keys-unexpected", event.seq
            )
        return WorkspaceProvisionedFact(
            operation_id=_read_identifier(data, "operation_id", event.seq),
            workspace_id=_read_identifier(data, "workspace_id", event.seq),
            creation_request_id=_read_identifier(
                data, "creation_request_id", event.seq
            ),
            source_id=_read_identifier(data, "source_id", event.seq),
            source_revision=_read_identifier(
                data, "source_revision", event.seq
            ),
            repository_fingerprint=_read_digest(
                data["repository_fingerprint"],
                lengths=(64,),
                code="workspace-repository-fingerprint-invalid",
                seq=event.seq,
            ),
            base_revision=_read_digest(
                data["base_revision"],
                lengths=(40, 64),
                code="workspace-base-revision-invalid",
                seq=event.seq,
            ),
            access=_read_access(data["access"], event.seq),
            owner_agent_id=_read_optional_identifier(
                data, "owner_agent_id", event.seq
            ),
            seq=event.seq,
        )
    if event.type == WORKSPACE_ATTACHED:
        if set(data) != _ATTACHED_KEYS:
            raise WorkspaceProtocolError(
                "workspace-payload-keys-unexpected", event.seq
            )
        return WorkspaceAttachedFact(
            operation_id=_read_identifier(data, "operation_id", event.seq),
            workspace_id=_read_identifier(data, "workspace_id", event.seq),
            agent_id=_read_identifier(data, "agent_id", event.seq),
            session_id=_read_identifier(data, "session_id", event.seq),
            seq=event.seq,
        )
    if event.type in (WORKSPACE_QUARANTINED, WORKSPACE_RELEASED):
        if set(data) != _TERMINAL_KEYS:
            raise WorkspaceProtocolError(
                "workspace-payload-keys-unexpected", event.seq
            )
        fact_type = (
            WorkspaceQuarantinedFact
            if event.type == WORKSPACE_QUARANTINED
            else WorkspaceReleasedFact
        )
        allowed = (
            QUARANTINE_REASONS
            if event.type == WORKSPACE_QUARANTINED
            else RELEASE_REASONS
        )
        return fact_type(
            operation_id=_read_identifier(data, "operation_id", event.seq),
            workspace_id=_read_identifier(data, "workspace_id", event.seq),
            reason=_read_reason(data, allowed, event.seq),
            seq=event.seq,
        )
    raise WorkspaceProtocolError("workspace-event-type-unknown", event.seq)


def is_workspace_fact(
    event: EventEnvelope, event_type: str, data: dict[str, JsonValue]
) -> bool:
    try:
        parse_workspace_fact(event)
    except WorkspaceProtocolError:
        return False
    if event.type != event_type:
        return False
    return canonical_json(event.data) == canonical_json(data)


__all__ = [
    "QUARANTINE_REASONS",
    "RELEASE_REASONS",
    "WORKSPACE_ATTACHED",
    "WORKSPACE_CATALOG_STREAM",
    "WORKSPACE_PROVISIONED",
    "WORKSPACE_QUARANTINED",
    "WORKSPACE_RELEASED",
    "WORKSPACE_SCHEMA_VERSION",
    "WorkspaceAttachedFact",
    "WorkspaceFact",
    "WorkspaceProvisionedFact",
    "WorkspaceQuarantinedFact",
    "WorkspaceReleasedFact",
    "attached_data",
    "freeze_provisioning_request",
    "is_workspace_fact",
    "lifecycle_data",
    "parse_workspace_fact",
    "provisioned_data",
    "require_workspace_identifier",
]
