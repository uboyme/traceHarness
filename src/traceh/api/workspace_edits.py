"""Immutable file images for an explicitly bound managed workspace edit."""

import asyncio
from dataclasses import dataclass
from hashlib import sha256

from traceh.api.json_types import fingerprint


class WorkspaceEditCancelled(asyncio.CancelledError):
    def __init__(self, receipt):
        super().__init__("workspace edit cancelled after convergence")
        self.receipt = receipt


@dataclass(frozen=True, slots=True)
class FileImage:
    content: bytes | None
    mode: str
    object_id: str | None

    def data(self):
        return {
            "exists": self.content is not None,
            "sha256": sha256(self.content).hexdigest() if self.content is not None else None,
            "size_bytes": len(self.content) if self.content is not None else 0,
            "mode": self.mode,
            "object_id": self.object_id,
        }


@dataclass(frozen=True, slots=True)
class FileEdit:
    path: str
    before: FileImage
    after: FileImage

    def data(self):
        return {"path": self.path, "before": self.before.data(), "after": self.after.data()}


@dataclass(frozen=True, slots=True)
class WorkspaceEditPlan:
    artifact_id: str
    manifest_digest: str
    patch_digest: str
    workspace_id: str
    workspace_generation: int
    agent_id: str
    session_id: str
    source_id: str
    base_revision: str
    repository_fingerprint: str
    files: tuple[FileEdit, ...]

    def data(self):
        return {
            "format": 1,
            "artifact_id": self.artifact_id,
            "manifest_digest": self.manifest_digest,
            "patch_digest": self.patch_digest,
            "workspace_id": self.workspace_id,
            "workspace_generation": self.workspace_generation,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "source_id": self.source_id,
            "base_revision": self.base_revision,
            "repository_fingerprint": self.repository_fingerprint,
            "files": [edit.data() for edit in self.files],
        }

    @property
    def digest(self):
        return fingerprint(self.data())
