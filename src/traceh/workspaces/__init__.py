"""Host-managed Git workspaces and durable lifecycle reconstruction."""

from traceh.workspaces.catalog import (
    WorkspaceCatalog,
    WorkspaceCatalogIssue,
    WorkspaceCatalogReader,
    validate_workspace_catalog_events,
)
from traceh.workspaces.errors import (
    WorkspaceCatalogConflictError,
    WorkspaceDirectoryMismatchError,
    WorkspaceDirtyError,
    WorkspaceError,
    WorkspaceGitError,
    WorkspaceInputError,
    WorkspaceNotFoundError,
    WorkspaceOperationConflictError,
    WorkspacePathError,
    WorkspaceProtocolError,
    WorkspaceQuarantinedError,
    WorkspaceSessionMismatchError,
    WorkspaceSourceError,
    WorkspaceStateError,
    WorkspaceWriteError,
)
from traceh.workspaces.events import WORKSPACE_CATALOG_STREAM
from traceh.workspaces.local_git import LocalGitWorkspaceProvider
from traceh.workspaces.policy import ManagedWorkspaceAccessPolicy
from traceh.workspaces.service import (
    WorkspaceService,
    workspace_identity,
    workspace_operation_id,
)
from traceh.workspaces.supervision import (
    AgentWorkspacePolicy,
    WorkspaceManagedAgentSupervisor,
)

__all__ = [
    "WORKSPACE_CATALOG_STREAM",
    "AgentWorkspacePolicy",
    "LocalGitWorkspaceProvider",
    "ManagedWorkspaceAccessPolicy",
    "WorkspaceCatalog",
    "WorkspaceCatalogConflictError",
    "WorkspaceCatalogIssue",
    "WorkspaceCatalogReader",
    "WorkspaceDirectoryMismatchError",
    "WorkspaceDirtyError",
    "WorkspaceError",
    "WorkspaceGitError",
    "WorkspaceInputError",
    "WorkspaceManagedAgentSupervisor",
    "WorkspaceNotFoundError",
    "WorkspaceOperationConflictError",
    "WorkspacePathError",
    "WorkspaceProtocolError",
    "WorkspaceQuarantinedError",
    "WorkspaceService",
    "WorkspaceSessionMismatchError",
    "WorkspaceSourceError",
    "WorkspaceStateError",
    "WorkspaceWriteError",
    "validate_workspace_catalog_events",
    "workspace_identity",
    "workspace_operation_id",
]
