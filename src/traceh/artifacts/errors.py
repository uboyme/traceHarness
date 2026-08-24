"""Stable, non-echoing failures for immutable Patch Artifacts."""

from __future__ import annotations


class ArtifactError(Exception):
    code = "artifact-error"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class ArtifactInputError(ArtifactError, ValueError):
    def __init__(self, code: str, field: str) -> None:
        self.code = code
        self.field = field
        super().__init__(f"artifact {field} is not usable")


class ArtifactProtocolError(ArtifactError, ValueError):
    def __init__(self, code: str, seq: int) -> None:
        self.code = code
        self.seq = seq
        super().__init__("the artifact catalog protocol is invalid")


class ArtifactNotFoundError(ArtifactError, LookupError):
    code = "artifact-not-found"

    def __init__(self) -> None:
        super().__init__("the Patch Artifact does not exist")


class ArtifactCaptureStateError(ArtifactError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("the Agent or workspace is not ready for Patch capture")


class ArtifactOperationConflictError(ArtifactError):
    code = "artifact-operation-reused"

    def __init__(self) -> None:
        super().__init__("the capture identity was already used for another artifact")


class ArtifactCatalogConflictError(ArtifactError):
    code = "artifact-catalog-changed"

    def __init__(self) -> None:
        super().__init__("the artifact catalog changed before this manifest was recorded")


class ArtifactWriteError(ArtifactError):
    code = "artifact-write-failed"

    def __init__(self, *, committed: bool | None) -> None:
        self.committed = committed
        if committed is None:
            message = "artifact write failed and whether it was recorded is unknown"
        elif committed:
            message = "artifact write was recorded but the call failed"
        else:
            message = "artifact write could not be recorded"
        super().__init__(message)


class ArtifactCasError(ArtifactError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("the Patch Artifact content store is not usable")


class ArtifactGitError(ArtifactError):
    def __init__(self, code: str = "artifact-git-failed") -> None:
        self.code = code
        super().__init__("the managed workspace could not be captured safely")


class ArtifactServiceClosedError(ArtifactError):
    code = "artifact-service-closed"

    def __init__(self) -> None:
        super().__init__("the Patch capture service is closed")


__all__ = [
    "ArtifactCaptureStateError",
    "ArtifactCasError",
    "ArtifactCatalogConflictError",
    "ArtifactError",
    "ArtifactGitError",
    "ArtifactInputError",
    "ArtifactNotFoundError",
    "ArtifactOperationConflictError",
    "ArtifactProtocolError",
    "ArtifactServiceClosedError",
    "ArtifactWriteError",
]
