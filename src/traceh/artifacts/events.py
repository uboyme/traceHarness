"""Patch Manifest event vocabulary shared by capture and replay."""

from __future__ import annotations

from datetime import UTC, datetime

from traceh.agents.identity import is_agent_identifier
from traceh.api.artifacts import PatchBlob, PatchManifest
from traceh.api.events import EventEnvelope
from traceh.api.json_types import JsonValue, canonical_json
from traceh.artifacts.errors import ArtifactInputError, ArtifactProtocolError
from traceh.artifacts.manifest import (
    MAX_PROTOCOL_BLOB_BYTES,
    PATCH_BLOB_PROTOCOL_VERSION,
    PATCH_CAPTURE_PROTOCOL_VERSION,
    freeze_changed_paths,
    patch_artifact_id,
    patch_capture_key,
    patch_manifest_digest,
    require_artifact_identifier,
    require_hex_digest,
)

ARTIFACT_CATALOG_STREAM = "artifacts:catalog"
PATCH_MANIFEST_RECORDED = "artifact/patch-captured"
ARTIFACT_SCHEMA_VERSION = 1

_MANIFEST_KEYS = frozenset(
    {
        "artifact_id",
        "capture_key",
        "blob_sha256",
        "blob_size_bytes",
        "blob_address",
        "blob_protocol_version",
        "agent_id",
        "session_id",
        "message_id",
        "turn_id",
        "workspace_id",
        "workspace_generation",
        "repository_fingerprint",
        "base_revision",
        "workspace_head_revision",
        "candidate_tree",
        "changed_paths",
        "capture_protocol_version",
    }
)


def patch_manifest_data(
    *,
    artifact_id: str,
    capture_key: str,
    blob: PatchBlob,
    agent_id: str,
    session_id: str,
    message_id: str,
    turn_id: str,
    workspace_id: str,
    workspace_generation: int,
    repository_fingerprint: str,
    base_revision: str,
    workspace_head_revision: str,
    candidate_tree: str,
    changed_paths: tuple[str, ...],
) -> dict[str, JsonValue]:
    artifact_id = require_artifact_identifier(artifact_id, field="artifact_id")
    capture_key = require_hex_digest(
        capture_key, lengths=(64,), field="capture-key"
    )
    agent_id = require_artifact_identifier(agent_id, field="agent_id")
    session_id = require_artifact_identifier(session_id, field="session_id")
    message_id = require_artifact_identifier(message_id, field="message_id")
    turn_id = require_artifact_identifier(turn_id, field="turn_id")
    workspace_id = require_artifact_identifier(workspace_id, field="workspace_id")
    if type(workspace_generation) is not int or workspace_generation < 1:
        raise ArtifactInputError(
            "artifact-workspace-generation-invalid", "workspace_generation"
        )
    expected_capture_key = patch_capture_key(
        agent_id=agent_id,
        message_id=message_id,
        workspace_id=workspace_id,
        workspace_generation=workspace_generation,
    )
    if capture_key != expected_capture_key:
        raise ArtifactInputError("artifact-capture-key-invalid", "capture_key")
    if artifact_id != patch_artifact_id(expected_capture_key):
        raise ArtifactInputError("artifact-id-invalid", "artifact_id")
    if type(blob) is not PatchBlob:
        raise ArtifactInputError("artifact-blob-invalid", "blob")
    blob_sha256 = require_hex_digest(
        blob.sha256, lengths=(64,), field="blob-digest"
    )
    if (
        type(blob.size_bytes) is not int
        or blob.size_bytes < 0
        or blob.size_bytes > MAX_PROTOCOL_BLOB_BYTES
        or type(blob.protocol_version) is not int
        or blob.protocol_version != PATCH_BLOB_PROTOCOL_VERSION
        or type(blob.address) is not str
        or blob.address != f"sha256/{blob_sha256}"
    ):
        raise ArtifactInputError("artifact-blob-invalid", "blob")
    return {
        "artifact_id": artifact_id,
        "capture_key": capture_key,
        "blob_sha256": blob_sha256,
        "blob_size_bytes": blob.size_bytes,
        "blob_address": blob.address,
        "blob_protocol_version": blob.protocol_version,
        "agent_id": agent_id,
        "session_id": session_id,
        "message_id": message_id,
        "turn_id": turn_id,
        "workspace_id": workspace_id,
        "workspace_generation": workspace_generation,
        "repository_fingerprint": require_hex_digest(
            repository_fingerprint,
            lengths=(64,),
            field="repository-fingerprint",
        ),
        "base_revision": require_hex_digest(
            base_revision, lengths=(40, 64), field="base-revision"
        ),
        "workspace_head_revision": require_hex_digest(
            workspace_head_revision,
            lengths=(40, 64),
            field="workspace-head-revision",
        ),
        "candidate_tree": require_hex_digest(
            candidate_tree, lengths=(40, 64), field="candidate-tree"
        ),
        "changed_paths": list(freeze_changed_paths(changed_paths)),
        "capture_protocol_version": PATCH_CAPTURE_PROTOCOL_VERSION,
    }


def parse_patch_manifest(event: EventEnvelope) -> PatchManifest:
    try:
        return _parse_patch_manifest(event)
    except ArtifactProtocolError:
        raise
    except Exception:
        try:
            seq = event.seq
        except Exception:
            seq = 0
        if type(seq) is not int:
            seq = 0
        raise ArtifactProtocolError("artifact-payload-invalid", seq) from None


def _parse_patch_manifest(event: EventEnvelope) -> PatchManifest:
    if event.stream_id != ARTIFACT_CATALOG_STREAM:
        raise ArtifactProtocolError("artifact-stream-unexpected", event.seq)
    if event.schema_version != ARTIFACT_SCHEMA_VERSION:
        raise ArtifactProtocolError(
            "artifact-schema-version-unsupported", event.seq
        )
    if event.type != PATCH_MANIFEST_RECORDED:
        raise ArtifactProtocolError("artifact-event-type-unknown", event.seq)
    if type(event.seq) is not int or event.seq < 1:
        raise ArtifactProtocolError("artifact-sequence-invalid", 0)
    if type(event.occurred_at) is not datetime or event.occurred_at.tzinfo is None:
        raise ArtifactProtocolError("artifact-captured-at-invalid", event.seq)
    data = event.data
    if not isinstance(data, dict):
        raise ArtifactProtocolError("artifact-payload-invalid", event.seq)
    if set(data) != _MANIFEST_KEYS:
        raise ArtifactProtocolError("artifact-payload-keys-unexpected", event.seq)

    artifact_id = _read_identifier(data, "artifact_id", event.seq)
    capture_key = _read_digest(data, "capture_key", (64,), event.seq)
    blob_sha256 = _read_digest(data, "blob_sha256", (64,), event.seq)
    blob_size = _read_int(
        data, "blob_size_bytes", event.seq, minimum=0, maximum=MAX_PROTOCOL_BLOB_BYTES
    )
    blob_address = data.get("blob_address")
    if type(blob_address) is not str or blob_address != f"sha256/{blob_sha256}":
        raise ArtifactProtocolError("artifact-blob-invalid", event.seq)
    blob_protocol = _read_int(
        data,
        "blob_protocol_version",
        event.seq,
        minimum=PATCH_BLOB_PROTOCOL_VERSION,
        maximum=PATCH_BLOB_PROTOCOL_VERSION,
    )
    capture_protocol = _read_int(
        data,
        "capture_protocol_version",
        event.seq,
        minimum=PATCH_CAPTURE_PROTOCOL_VERSION,
        maximum=PATCH_CAPTURE_PROTOCOL_VERSION,
    )
    agent_id = _read_identifier(data, "agent_id", event.seq)
    session_id = _read_identifier(data, "session_id", event.seq)
    message_id = _read_identifier(data, "message_id", event.seq)
    turn_id = _read_identifier(data, "turn_id", event.seq)
    workspace_id = _read_identifier(data, "workspace_id", event.seq)
    workspace_generation = _read_int(
        data, "workspace_generation", event.seq, minimum=1
    )
    expected_capture_key = patch_capture_key(
        agent_id=agent_id,
        message_id=message_id,
        workspace_id=workspace_id,
        workspace_generation=workspace_generation,
    )
    if capture_key != expected_capture_key:
        raise ArtifactProtocolError("artifact-capture-key-invalid", event.seq)
    if artifact_id != patch_artifact_id(expected_capture_key):
        raise ArtifactProtocolError("artifact-id-invalid", event.seq)

    changed_paths = freeze_changed_paths(
        data.get("changed_paths"), protocol=True, seq=event.seq
    )
    captured_at = event.occurred_at.astimezone(UTC)
    normalized_data = {
        "artifact_id": artifact_id,
        "capture_key": capture_key,
        "blob_sha256": blob_sha256,
        "blob_size_bytes": blob_size,
        "blob_address": blob_address,
        "blob_protocol_version": blob_protocol,
        "agent_id": agent_id,
        "session_id": session_id,
        "message_id": message_id,
        "turn_id": turn_id,
        "workspace_id": workspace_id,
        "workspace_generation": workspace_generation,
        "repository_fingerprint": _read_digest(
            data, "repository_fingerprint", (64,), event.seq
        ),
        "base_revision": _read_digest(
            data, "base_revision", (40, 64), event.seq
        ),
        "workspace_head_revision": _read_digest(
            data, "workspace_head_revision", (40, 64), event.seq
        ),
        "candidate_tree": _read_digest(
            data, "candidate_tree", (40, 64), event.seq
        ),
        "changed_paths": list(changed_paths),
        "capture_protocol_version": capture_protocol,
    }
    return PatchManifest(
        artifact_id=artifact_id,
        capture_key=capture_key,
        blob=PatchBlob(
            sha256=blob_sha256,
            size_bytes=blob_size,
            address=blob_address,
            protocol_version=blob_protocol,
        ),
        agent_id=str(normalized_data["agent_id"]),
        session_id=str(normalized_data["session_id"]),
        message_id=str(normalized_data["message_id"]),
        turn_id=str(normalized_data["turn_id"]),
        workspace_id=str(normalized_data["workspace_id"]),
        workspace_generation=int(normalized_data["workspace_generation"]),
        repository_fingerprint=str(normalized_data["repository_fingerprint"]),
        base_revision=str(normalized_data["base_revision"]),
        workspace_head_revision=str(normalized_data["workspace_head_revision"]),
        candidate_tree=str(normalized_data["candidate_tree"]),
        changed_paths=changed_paths,
        captured_at=captured_at,
        capture_protocol_version=capture_protocol,
        manifest_digest=patch_manifest_digest(normalized_data, captured_at),
        recorded_seq=event.seq,
    )


def is_patch_manifest_event(
    event: EventEnvelope, data: dict[str, JsonValue]
) -> bool:
    try:
        parse_patch_manifest(event)
    except ArtifactProtocolError:
        return False
    # Encoding failure is unknowable and intentionally propagates to the
    # shared reconciler, which maps it to ``None`` rather than false absence.
    return canonical_json(event.data) == canonical_json(data)


def _read_identifier(data: dict[str, JsonValue], key: str, seq: int) -> str:
    value = data.get(key)
    if not is_agent_identifier(value):
        raise ArtifactProtocolError("artifact-identity-invalid", seq)
    assert isinstance(value, str)
    return str(value)


def _read_digest(
    data: dict[str, JsonValue], key: str, lengths: tuple[int, ...], seq: int
) -> str:
    value = data.get(key)
    if (
        type(value) is not str
        or len(value) not in lengths
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ArtifactProtocolError(f"artifact-{key.replace('_', '-')}-invalid", seq)
    return value


def _read_int(
    data: dict[str, JsonValue],
    key: str,
    seq: int,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    value = data.get(key)
    if (
        type(value) is not int
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        raise ArtifactProtocolError(f"artifact-{key.replace('_', '-')}-invalid", seq)
    return value


__all__ = [
    "ARTIFACT_CATALOG_STREAM",
    "ARTIFACT_SCHEMA_VERSION",
    "PATCH_MANIFEST_RECORDED",
    "is_patch_manifest_event",
    "parse_patch_manifest",
    "patch_manifest_data",
]
