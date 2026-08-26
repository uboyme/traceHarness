"""Public values and provider seam for host-managed Git workspaces.

The values in this module carry identities and immutable observations. They
never turn a model-supplied string into a filesystem path: only a trusted
``WorkspaceProvider`` resolves a host-approved source id and the durable
workspace catalog resolves a ``workspace_id`` into a concrete root.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class WorkspaceAccess(StrEnum):
    """Tool-level access granted by the host.

    ``READ_ONLY`` is not an operating-system sandbox. It means the host must
    expose only read-only managed tools to the Agent using this workspace.
    """

    READ_ONLY = "read_only"
    WRITABLE = "writable"


class WorkspaceStatus(StrEnum):
    PROVISIONAL = "provisional"
    ATTACHED = "attached"
    QUARANTINED = "quarantined"
    RELEASED = "released"


class WorkspaceLocalState(StrEnum):
    """One provider observation of the catalogued worktree."""

    MISSING = "missing"
    CLEAN = "clean"
    DIRTY = "dirty"
    UNSAFE = "unsafe"


@dataclass(frozen=True, slots=True)
class WorkspaceProvisioningRequest:
    """A host-approved source snapshot and capability intent."""

    source_id: str
    revision: str
    access: WorkspaceAccess


@dataclass(frozen=True, slots=True)
class WorkspaceSourceSnapshot:
    """The exact Git commit selected from one trusted source mapping."""

    source_id: str
    requested_revision: str
    repository_fingerprint: str
    base_revision: str


@dataclass(frozen=True, slots=True)
class WorkspaceRecord:
    """One durable workspace lifecycle record rebuilt from the catalog."""

    workspace_id: str
    provision_operation_id: str
    creation_request_id: str
    source_id: str
    source_revision: str
    repository_fingerprint: str
    base_revision: str
    access: WorkspaceAccess
    owner_agent_id: str | None
    status: WorkspaceStatus
    agent_id: str | None
    session_id: str | None
    reason: str | None
    provisioned_seq: int
    updated_seq: int


@dataclass(frozen=True, slots=True)
class WorkspaceHandle:
    """A catalog-backed local path suitable for one Agent activation."""

    workspace_id: str
    root: Path
    source_id: str
    base_revision: str
    access: WorkspaceAccess
    status: WorkspaceStatus
    owner_agent_id: str | None = None
    agent_id: str | None = None
    session_id: str | None = None

    @property
    def writable(self) -> bool:
        return self.access is WorkspaceAccess.WRITABLE


class WorkspaceProvider(Protocol):
    """Host-owned local workspace effect boundary.

    The durable catalog owns lifecycle facts; the provider owns only concrete
    Git inspection and mutation. Implementations must never delete a path they
    cannot prove is the exact registered worktree described by ``record``.
    """

    @property
    def managed_root(self) -> Path:
        ...

    async def resolve_source(
        self, source_id: str, revision: str
    ) -> WorkspaceSourceSnapshot:
        ...

    async def materialize(self, record: WorkspaceRecord) -> WorkspaceHandle:
        ...

    async def inspect(self, record: WorkspaceRecord) -> WorkspaceLocalState:
        ...

    async def remove(self, record: WorkspaceRecord) -> None:
        ...

    async def remove_captured(
        self, record: WorkspaceRecord, *, candidate_tree: str
    ) -> None:
        """Remove a dirty worktree only when it still equals an immutable capture."""

        ...


__all__ = [
    "WorkspaceAccess",
    "WorkspaceHandle",
    "WorkspaceLocalState",
    "WorkspaceProvider",
    "WorkspaceProvisioningRequest",
    "WorkspaceRecord",
    "WorkspaceSourceSnapshot",
    "WorkspaceStatus",
]
