"""Stable, non-echoing failures for managed workspace control."""

from __future__ import annotations


class WorkspaceError(Exception):
    code = "workspace-error"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class WorkspaceInputError(WorkspaceError, ValueError):
    def __init__(self, code: str, field: str) -> None:
        self.code = code
        self.field = field
        super().__init__(f"workspace {field} is not usable")


class WorkspaceProtocolError(WorkspaceError, ValueError):
    def __init__(self, code: str, seq: int) -> None:
        self.code = code
        self.seq = seq
        super().__init__(_protocol_message(code))


def _protocol_message(code: str) -> str:
    messages = {
        "workspace-event-type-unknown": "the workspace catalog contains an unknown event",
        "workspace-stream-unexpected": "a workspace fact is on the wrong stream",
        "workspace-schema-version-unsupported": (
            "a workspace fact uses an unsupported schema"
        ),
        "workspace-sequence-invalid": "the workspace catalog sequence is not contiguous",
        "workspace-payload-keys-unexpected": (
            "a workspace fact has unexpected payload keys"
        ),
        "workspace-payload-invalid": "a workspace fact is malformed",
        "workspace-identity-invalid": "a workspace fact has an unusable identity",
        "workspace-access-invalid": "a workspace fact has an invalid access mode",
        "workspace-repository-fingerprint-invalid": (
            "a workspace fact has an invalid repository fingerprint"
        ),
        "workspace-base-revision-invalid": (
            "a workspace fact has an invalid base revision"
        ),
        "workspace-operation-duplicate": (
            "two workspace facts share one operation id"
        ),
        "workspace-id-duplicate": "two workspace provisions share one workspace id",
        "workspace-request-duplicate": (
            "two workspace provisions share one Agent creation request"
        ),
        "workspace-unknown": "a workspace fact names an unknown workspace",
        "workspace-state-invalid": "a workspace fact violates the lifecycle state",
        "workspace-agent-duplicate": "an Agent is attached to two workspaces",
        "workspace-session-duplicate": "a Session is attached to two workspaces",
    }
    return messages.get(code, "the workspace catalog protocol is invalid")


class WorkspaceOperationConflictError(WorkspaceError):
    code = "workspace-operation-reused"

    def __init__(self) -> None:
        super().__init__("operation_id was already used for another workspace fact")


class WorkspaceCatalogConflictError(WorkspaceError):
    code = "workspace-catalog-changed"

    def __init__(self) -> None:
        super().__init__("the workspace catalog changed before this fact was recorded")


class WorkspaceWriteError(WorkspaceError):
    code = "workspace-write-failed"

    def __init__(self, *, committed: bool | None) -> None:
        self.committed = committed
        if committed is None:
            message = "workspace write failed and whether it was recorded is unknown"
        elif committed:
            message = "workspace write was recorded but the call failed"
        else:
            message = "workspace write could not be recorded"
        super().__init__(message)


class WorkspaceNotFoundError(WorkspaceError, LookupError):
    code = "workspace-not-found"

    def __init__(self) -> None:
        super().__init__("the managed workspace does not exist")


class WorkspaceStateError(WorkspaceError):
    code = "workspace-state-invalid"

    def __init__(self) -> None:
        super().__init__("the managed workspace is not in the required state")


class WorkspaceDirectoryMismatchError(WorkspaceError):
    code = "workspace-directory-mismatch"

    def __init__(self) -> None:
        super().__init__("durable Agent identity does not match the workspace operation")


class WorkspaceSessionMismatchError(WorkspaceError):
    code = "workspace-session-mismatch"

    def __init__(self) -> None:
        super().__init__("the Agent Session is not bound to the managed workspace path")


class WorkspaceSourceError(WorkspaceError):
    code = "workspace-source-invalid"

    def __init__(self) -> None:
        super().__init__("the configured Git workspace source is not usable")


class WorkspacePathError(WorkspaceError):
    code = "workspace-path-unsafe"

    def __init__(self) -> None:
        super().__init__("the managed workspace path is unsafe or outside its root")


class WorkspaceGitError(WorkspaceError):
    code = "workspace-git-failed"

    def __init__(self) -> None:
        super().__init__("the managed Git workspace operation failed")


class WorkspaceDirtyError(WorkspaceError):
    code = "workspace-dirty"

    def __init__(self) -> None:
        super().__init__("the managed workspace contains uncollected changes")


class WorkspaceQuarantinedError(WorkspaceError):
    code = "workspace-quarantined"

    def __init__(self) -> None:
        super().__init__("the managed workspace requires explicit reconciliation")


__all__ = [
    "WorkspaceCatalogConflictError",
    "WorkspaceDirectoryMismatchError",
    "WorkspaceDirtyError",
    "WorkspaceError",
    "WorkspaceGitError",
    "WorkspaceInputError",
    "WorkspaceNotFoundError",
    "WorkspaceOperationConflictError",
    "WorkspacePathError",
    "WorkspaceProtocolError",
    "WorkspaceQuarantinedError",
    "WorkspaceSessionMismatchError",
    "WorkspaceSourceError",
    "WorkspaceStateError",
    "WorkspaceWriteError",
]
