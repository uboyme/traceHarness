"""Stable, non-echoing failures for the Budget control plane."""

from __future__ import annotations


class BudgetError(Exception):
    code = "budget-error"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class BudgetInputError(BudgetError, ValueError):
    def __init__(self, code: str, field: str) -> None:
        self.code = code
        self.field = field
        super().__init__(f"budget {field} is not usable")


class BudgetProtocolError(BudgetError, ValueError):
    def __init__(self, code: str, seq: int) -> None:
        self.code = code
        self.seq = seq
        super().__init__(_protocol_message(code))


def _protocol_message(code: str) -> str:
    messages = {
        "budget-event-type-unknown": "the budget ledger contains an unknown event",
        "budget-stream-unexpected": "a budget fact is on the wrong stream",
        "budget-schema-version-unsupported": "a budget fact uses an unsupported schema",
        "budget-sequence-invalid": "the budget ledger sequence is not contiguous",
        "budget-payload-keys-unexpected": "a budget fact has unexpected payload keys",
        "budget-payload-invalid": "a budget fact is malformed",
        "budget-identity-invalid": "a budget fact has an unusable identity",
        "budget-limits-invalid": "a budget fact has malformed limits",
        "budget-amounts-invalid": "a budget fact has malformed amounts",
        "budget-operation-duplicate": "two budget facts share one operation id",
        "budget-root-duplicate": "two root grants claim one Agent",
        "budget-root-agent-invalid": "a root grant does not name a durable root Agent",
        "budget-account-unknown": "a budget fact names an unknown account",
        "budget-account-closed": "a budget fact spends a closed account",
        "budget-child-account-duplicate": "a child Budget account already exists",
        "budget-reservation-duplicate": "two reservations share one identity",
        "budget-reservation-child-conflict": "a child is held by another reservation",
        "budget-reservation-request-conflict": (
            "a creation request is held by another reservation"
        ),
        "budget-reservation-unknown": "a budget fact names an unknown reservation",
        "budget-reservation-terminal": "a reservation has contradictory terminal facts",
        "budget-reservation-directory-conflict": (
            "a reservation conflicts with the durable Agent directory"
        ),
        "budget-commit-without-agent": "a reservation commit lacks durable Agent identity",
        "budget-release-after-agent": "a released reservation has a durable child Agent",
        "budget-child-limits-invalid": "a child grant exceeds its parent constraints",
        "budget-depth-exhausted": "a budget account has no remaining child depth",
        "budget-capacity-exceeded": "a budget fact exceeds available capacity",
        "budget-charge-inactive": "a charge uses a dimension the host did not activate",
        "budget-charge-empty": "a budget charge contains no usage",
        "budget-close-with-pending-reservation": (
            "a budget account was closed with an open reservation"
        ),
        "budget-close-duplicate": "a budget account has contradictory close facts",
    }
    return messages.get(code, "the budget ledger protocol is invalid")


class BudgetOperationConflictError(BudgetError):
    code = "budget-operation-reused"

    def __init__(self) -> None:
        super().__init__("operation_id was already used for another budget fact")


class BudgetLedgerConflictError(BudgetError):
    code = "budget-ledger-changed"

    def __init__(self) -> None:
        super().__init__("the budget ledger changed before this fact could be recorded")


class BudgetWriteError(BudgetError):
    code = "budget-write-failed"

    def __init__(self, *, committed: bool | None) -> None:
        self.committed = committed
        if committed is None:
            message = "budget write failed and whether it was recorded is unknown"
        elif committed:
            message = "budget write was recorded but the call failed"
        else:
            message = "budget write could not be recorded"
        super().__init__(message)


class BudgetAccountNotFoundError(BudgetError, LookupError):
    code = "budget-account-not-found"

    def __init__(self) -> None:
        super().__init__("the Budget account does not exist")


class BudgetAccountClosedError(BudgetError):
    code = "budget-account-closed"

    def __init__(self) -> None:
        super().__init__("the Budget account is closed")


class BudgetExhaustedError(BudgetError):
    code = "budget-exhausted"

    def __init__(self, dimension: str) -> None:
        self.dimension = dimension
        super().__init__("the Budget account has insufficient capacity")


class BudgetReservationNotFoundError(BudgetError, LookupError):
    code = "budget-reservation-not-found"

    def __init__(self) -> None:
        super().__init__("the Budget reservation does not exist")


class BudgetReservationStateError(BudgetError):
    code = "budget-reservation-state-invalid"

    def __init__(self) -> None:
        super().__init__("the Budget reservation is not in the required state")


class BudgetDirectoryMismatchError(BudgetError):
    code = "budget-directory-mismatch"

    def __init__(self) -> None:
        super().__init__("durable Agent identity does not match the Budget operation")


__all__ = [
    "BudgetAccountClosedError",
    "BudgetAccountNotFoundError",
    "BudgetDirectoryMismatchError",
    "BudgetError",
    "BudgetExhaustedError",
    "BudgetInputError",
    "BudgetLedgerConflictError",
    "BudgetOperationConflictError",
    "BudgetProtocolError",
    "BudgetReservationNotFoundError",
    "BudgetReservationStateError",
    "BudgetWriteError",
]
