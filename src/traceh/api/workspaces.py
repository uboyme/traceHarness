"""Workspace isolation and patch-artifact protocols for future multi-agent coding."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    snapshot_id: str
    source_workspace_id: str
    revision: str


@dataclass(frozen=True, slots=True)
class WorkspaceHandle:
    workspace_id: str
    root: Path
    owner_agent_id: str | None = None
    writable: bool = True


@dataclass(frozen=True, slots=True)
class PatchArtifact:
    artifact_id: str
    workspace_id: str
    unified_diff: str
    base_revision: str


@dataclass(frozen=True, slots=True)
class MergeResult:
    merged: bool
    revision: str | None = None
    conflicts: tuple[str, ...] = ()


class WorkspaceProvider(Protocol):
    async def snapshot(self, workspace_id: str) -> WorkspaceSnapshot:
        ...

    async def branch(
        self,
        snapshot: WorkspaceSnapshot,
        *,
        owner_agent_id: str,
    ) -> WorkspaceHandle:
        ...

    async def diff(self, workspace: WorkspaceHandle) -> PatchArtifact:
        ...

    async def merge(
        self,
        target: WorkspaceHandle,
        patch: PatchArtifact,
    ) -> MergeResult:
        ...
