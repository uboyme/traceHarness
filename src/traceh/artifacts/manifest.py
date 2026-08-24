"""Canonical identities, paths and digests for Patch Manifests."""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime

from traceh.agents.identity import is_agent_identifier
from traceh.api.artifacts import PatchCaptureLimits
from traceh.api.json_types import fingerprint
from traceh.artifacts.errors import ArtifactInputError, ArtifactProtocolError

PATCH_BLOB_PROTOCOL_VERSION = 1
PATCH_CAPTURE_PROTOCOL_VERSION = 1
MAX_PROTOCOL_CHANGED_PATHS = 100_000
MAX_PROTOCOL_PATH_BYTES = 4096
MAX_PROTOCOL_BLOB_BYTES = 1024 * 1024 * 1024

_CONTROL_COMPONENTS = frozenset({".git", ".traceh"})
_WINDOWS_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)


def require_artifact_identifier(value: object, *, field: str) -> str:
    try:
        valid = is_agent_identifier(value)
        normalized = str(value) if valid else ""
    except Exception:
        valid = False
        normalized = ""
    if not valid or not is_agent_identifier(normalized):
        raise ArtifactInputError("artifact-identity-invalid", field)
    return normalized


def require_hex_digest(
    value: object, *, lengths: tuple[int, ...], field: str
) -> str:
    if not _is_hex_digest(value, lengths):
        raise ArtifactInputError(f"artifact-{field}-invalid", field)
    assert isinstance(value, str)
    return value


def freeze_capture_limits(value: object) -> PatchCaptureLimits:
    if type(value) is not PatchCaptureLimits:
        raise ArtifactInputError("artifact-limits-invalid", "limits")
    fields = (
        value.max_changed_paths,
        value.max_path_bytes,
        value.max_file_bytes,
        value.max_total_file_bytes,
        value.max_patch_bytes,
    )
    if any(type(item) is not int or item <= 0 for item in fields):
        raise ArtifactInputError("artifact-limits-invalid", "limits")
    if value.max_path_bytes > MAX_PROTOCOL_PATH_BYTES:
        raise ArtifactInputError("artifact-limits-invalid", "limits")
    if value.max_changed_paths > MAX_PROTOCOL_CHANGED_PATHS:
        raise ArtifactInputError("artifact-limits-invalid", "limits")
    if value.max_patch_bytes > MAX_PROTOCOL_BLOB_BYTES:
        raise ArtifactInputError("artifact-limits-invalid", "limits")
    return value


def patch_capture_key(
    *, agent_id: str, message_id: str, workspace_id: str, workspace_generation: int
) -> str:
    agent_id = require_artifact_identifier(agent_id, field="agent_id")
    message_id = require_artifact_identifier(message_id, field="message_id")
    workspace_id = require_artifact_identifier(workspace_id, field="workspace_id")
    if type(workspace_generation) is not int or workspace_generation < 1:
        raise ArtifactInputError(
            "artifact-workspace-generation-invalid", "workspace_generation"
        )
    return fingerprint(
        {
            "agent_id": agent_id,
            "message_id": message_id,
            "workspace_id": workspace_id,
            "workspace_generation": workspace_generation,
        }
    )


def patch_artifact_id(capture_key: str) -> str:
    capture_key = require_hex_digest(
        capture_key, lengths=(64,), field="capture-key"
    )
    return f"patch-{capture_key}"


def freeze_changed_paths(
    paths: object,
    *,
    max_paths: int = MAX_PROTOCOL_CHANGED_PATHS,
    max_path_bytes: int = MAX_PROTOCOL_PATH_BYTES,
    protocol: bool = False,
    seq: int = 0,
) -> tuple[str, ...]:
    if type(paths) not in (list, tuple):
        return _path_failure(protocol, seq)
    if type(max_paths) is not int or type(max_path_bytes) is not int:
        return _path_failure(protocol, seq)
    if len(paths) > max_paths:
        return _path_failure(protocol, seq, code="artifact-path-count-exceeded")
    normalized: list[str] = []
    collision_keys: set[str] = set()
    for value in paths:
        if type(value) is not str:
            return _path_failure(protocol, seq)
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeError:
            return _path_failure(protocol, seq)
        if not encoded or len(encoded) > max_path_bytes:
            return _path_failure(protocol, seq, code="artifact-path-size-exceeded")
        if value != unicodedata.normalize("NFC", value):
            return _path_failure(protocol, seq, code="artifact-path-normalization-invalid")
        if (
            value.startswith("/")
            or "\\" in value
            or "\0" in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            return _path_failure(protocol, seq)
        components = value.split("/")
        if any(component in ("", ".", "..") for component in components):
            return _path_failure(protocol, seq)
        for component in components:
            folded = component.casefold()
            if folded in _CONTROL_COMPONENTS:
                return _path_failure(protocol, seq, code="artifact-control-path-rejected")
            if ":" in component or component.endswith((" ", ".")):
                return _path_failure(protocol, seq)
            device_stem = folded.split(".", 1)[0]
            if device_stem in _WINDOWS_DEVICE_NAMES:
                return _path_failure(protocol, seq)
        if value.casefold() == ".gitmodules":
            return _path_failure(protocol, seq, code="artifact-gitmodules-rejected")
        collision_key = value.casefold()
        if collision_key in collision_keys:
            return _path_failure(protocol, seq, code="artifact-path-collision")
        collision_keys.add(collision_key)
        normalized.append(value)
    return tuple(sorted(normalized, key=lambda item: item.encode("utf-8")))


def patch_manifest_digest(data: object, captured_at: datetime) -> str:
    if type(captured_at) is not datetime or captured_at.tzinfo is None:
        raise ArtifactInputError("artifact-captured-at-invalid", "captured_at")
    timestamp = captured_at.astimezone(UTC).isoformat()
    try:
        return fingerprint({"captured_at": timestamp, "manifest": data})
    except Exception:
        raise ArtifactInputError("artifact-manifest-invalid", "manifest") from None


def _path_failure(
    protocol: bool,
    seq: int,
    *,
    code: str = "artifact-path-invalid",
) -> tuple[str, ...]:
    if protocol:
        raise ArtifactProtocolError(code, seq)
    raise ArtifactInputError(code, "changed_paths")


def _is_hex_digest(value: object, lengths: tuple[int, ...]) -> bool:
    return (
        type(value) is str
        and len(value) in lengths
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = [
    "MAX_PROTOCOL_BLOB_BYTES",
    "MAX_PROTOCOL_CHANGED_PATHS",
    "MAX_PROTOCOL_PATH_BYTES",
    "PATCH_BLOB_PROTOCOL_VERSION",
    "PATCH_CAPTURE_PROTOCOL_VERSION",
    "freeze_capture_limits",
    "freeze_changed_paths",
    "patch_artifact_id",
    "patch_capture_key",
    "patch_manifest_digest",
    "require_artifact_identifier",
    "require_hex_digest",
]
