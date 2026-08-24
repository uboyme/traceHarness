"""Stable, non-echoing failures for Patch review, approval and promotion."""

from __future__ import annotations


class PromotionError(Exception):
    code = "promotion-error"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class PromotionInputError(PromotionError, ValueError):
    def __init__(self, code: str, field: str) -> None:
        self.code = code
        self.field = field
        super().__init__(f"promotion {field} is not usable")


class PromotionProtocolError(PromotionError, ValueError):
    def __init__(self, code: str, seq: int) -> None:
        self.code = code
        self.seq = seq
        super().__init__("the promotion ledger protocol is invalid")


class PromotionNotFoundError(PromotionError, LookupError):
    def __init__(self, code: str = "promotion-not-found") -> None:
        self.code = code
        super().__init__("the promotion fact does not exist")


class PromotionStateError(PromotionError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("the review or promotion state does not allow this operation")


class PromotionApprovalError(PromotionError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("the supplied approval is not valid for this review")


class PromotionTargetDriftError(PromotionError):
    def __init__(self, code: str = "promotion-target-drift") -> None:
        self.code = code
        super().__init__("the promotion target moved away from the approved revision")


class PromotionOperationConflictError(PromotionError):
    code = "promotion-operation-reused"

    def __init__(self) -> None:
        super().__init__("the operation identity was already used for other content")


class PromotionLedgerConflictError(PromotionError):
    code = "promotion-ledger-changed"

    def __init__(self) -> None:
        super().__init__("the promotion ledger changed before this fact was recorded")


class PromotionWriteError(PromotionError):
    code = "promotion-write-failed"

    def __init__(self, *, committed: bool | None) -> None:
        self.committed = committed
        if committed is None:
            message = "promotion write failed and whether it was recorded is unknown"
        elif committed:
            message = "promotion write was recorded but the call failed"
        else:
            message = "promotion write could not be recorded"
        super().__init__(message)


class PromotionGitError(PromotionError):
    def __init__(self, code: str = "promotion-git-failed") -> None:
        self.code = code
        super().__init__("the promotion Git operation could not complete safely")


class PromotionVerificationError(PromotionError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("the fixed host verification could not be executed")


class PromotionServiceClosedError(PromotionError):
    code = "promotion-service-closed"

    def __init__(self) -> None:
        super().__init__("the promotion service is closed")


__all__ = [
    "PromotionApprovalError",
    "PromotionError",
    "PromotionGitError",
    "PromotionInputError",
    "PromotionLedgerConflictError",
    "PromotionNotFoundError",
    "PromotionOperationConflictError",
    "PromotionProtocolError",
    "PromotionServiceClosedError",
    "PromotionStateError",
    "PromotionTargetDriftError",
    "PromotionVerificationError",
    "PromotionWriteError",
]
