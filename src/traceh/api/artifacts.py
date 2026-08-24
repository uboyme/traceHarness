"""Public immutable values and narrow seams for Patch Artifacts."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from traceh.api.workspaces import WorkspaceHandle
from traceh.session.event_store import EventStore


@dataclass(frozen=True, slots=True)
class PatchCaptureLimits:
    """Host-selected bounds for one immutable Patch capture."""

    max_changed_paths: int
    max_path_bytes: int
    max_file_bytes: int
    max_total_file_bytes: int
    max_patch_bytes: int


@dataclass(frozen=True, slots=True)
class PatchBlob:
    """One content-addressed raw Git patch stored outside the Event Log."""

    sha256: str
    size_bytes: int
    address: str
    protocol_version: int


@dataclass(frozen=True, slots=True)
class PatchManifest:
    """Durable source binding for one immutable Patch blob."""

    artifact_id: str
    capture_key: str
    blob: PatchBlob
    agent_id: str
    session_id: str
    message_id: str
    turn_id: str
    workspace_id: str
    workspace_generation: int
    repository_fingerprint: str
    base_revision: str
    workspace_head_revision: str
    candidate_tree: str
    changed_paths: tuple[str, ...]
    captured_at: datetime
    capture_protocol_version: int
    manifest_digest: str
    recorded_seq: int

    @property
    def reference(self) -> str:
        """Stable report reference; resolving it still verifies CAS bytes."""

        return f"patch:{self.artifact_id}@{self.manifest_digest}"


@dataclass(frozen=True, slots=True)
class PatchArtifact:
    """A verified Manifest together with its exact immutable Patch bytes."""

    manifest: PatchManifest
    content: bytes


class ArtifactCas(Protocol):
    """Content-addressed byte storage used by the Artifact domain."""

    @property
    def local_root(self) -> Path | None:
        """Local storage root, when overlap checks are meaningful."""

        ...

    async def put(self, content: bytes) -> PatchBlob:
        ...

    async def read(self, blob: PatchBlob) -> bytes:
        ...


class WorkspaceCaptureGate(Protocol):
    """Workspace adapter seam that linearizes capture with managed writers."""

    @property
    def store(self) -> EventStore:
        ...

    def capture_workspace(
        self, agent_id: str
    ) -> AbstractAsyncContextManager[WorkspaceHandle]:
        ...


__all__ = [
    "ArtifactCas",
    "PatchArtifact",
    "PatchBlob",
    "PatchCaptureLimits",
    "PatchManifest",
    "WorkspaceCaptureGate",
]
