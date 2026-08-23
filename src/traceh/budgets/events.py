"""One event vocabulary for the append-only hierarchical Budget ledger."""

from __future__ import annotations

from dataclasses import dataclass

from traceh.agents.identity import is_agent_identifier
from traceh.api.budgets import BudgetAmounts, BudgetLimits
from traceh.api.events import EventEnvelope
from traceh.api.json_types import JsonValue, canonical_json
from traceh.budgets.errors import BudgetInputError, BudgetProtocolError

BUDGET_LEDGER_STREAM = "budgets:ledger"
BUDGET_SCHEMA_VERSION = 1
MAX_BUDGET_VALUE = 2**53 - 1

BUDGET_ROOT_GRANTED = "budget/root-granted"
BUDGET_CHILD_RESERVED = "budget/child-reserved"
BUDGET_RESERVATION_COMMITTED = "budget/reservation-committed"
BUDGET_RESERVATION_RELEASED = "budget/reservation-released"
BUDGET_USAGE_CHARGED = "budget/usage-charged"
BUDGET_ACCOUNT_CLOSED = "budget/account-closed"

_LIMIT_FIELDS = (
    "max_tokens",
    "max_steps",
    "max_tool_calls",
    "max_wall_milliseconds",
    "max_children",
    "max_depth",
    "max_processes",
)
_AMOUNT_FIELDS = ("tokens", "steps", "tool_calls", "wall_milliseconds")

_ROOT_KEYS = frozenset(("operation_id", "agent_id", "limits"))
_RESERVE_KEYS = frozenset(
    (
        "operation_id",
        "reservation_id",
        "parent_agent_id",
        "child_agent_id",
        "creation_request_id",
        "child_limits",
    )
)
_TERMINAL_KEYS = frozenset(("operation_id", "reservation_id"))
_CHARGE_KEYS = frozenset(("operation_id", "agent_id", "amounts"))
_CLOSE_KEYS = frozenset(("operation_id", "agent_id"))


@dataclass(frozen=True, slots=True)
class RootGrantedFact:
    operation_id: str
    agent_id: str
    limits: BudgetLimits
    seq: int


@dataclass(frozen=True, slots=True)
class ChildReservedFact:
    operation_id: str
    reservation_id: str
    parent_agent_id: str
    child_agent_id: str
    creation_request_id: str
    child_limits: BudgetLimits
    seq: int


@dataclass(frozen=True, slots=True)
class ReservationCommittedFact:
    operation_id: str
    reservation_id: str
    seq: int


@dataclass(frozen=True, slots=True)
class ReservationReleasedFact:
    operation_id: str
    reservation_id: str
    seq: int


@dataclass(frozen=True, slots=True)
class UsageChargedFact:
    operation_id: str
    agent_id: str
    amounts: BudgetAmounts
    seq: int


@dataclass(frozen=True, slots=True)
class AccountClosedFact:
    operation_id: str
    agent_id: str
    seq: int


type BudgetFact = (
    RootGrantedFact
    | ChildReservedFact
    | ReservationCommittedFact
    | ReservationReleasedFact
    | UsageChargedFact
    | AccountClosedFact
)


def require_budget_identifier(value: object, *, field: str) -> str:
    """Validate a caller value without echoing it.

    Budget account ids are Agent ids, and operation/request ids share the same
    bounded, terminal-safe identifier contract as the durable Agent plane.
    """

    if not is_agent_identifier(value):
        raise BudgetInputError("budget-identity-invalid", field)
    assert isinstance(value, str)
    return value


def _valid_limit(value: object) -> bool:
    return value is None or (
        type(value) is int
        and 0 <= value <= MAX_BUDGET_VALUE
    )


def freeze_limits(value: object, *, field: str = "limits") -> BudgetLimits:
    """Validate and copy host limits before the first suspension point."""

    if not isinstance(value, BudgetLimits):
        raise BudgetInputError("budget-limits-invalid", field)
    try:
        values = {name: getattr(value, name) for name in _LIMIT_FIELDS}
    except Exception:
        raise BudgetInputError("budget-limits-invalid", field) from None
    if not all(_valid_limit(item) for item in values.values()):
        raise BudgetInputError("budget-limits-invalid", field)
    return BudgetLimits(**values)


def limits_to_data(value: BudgetLimits) -> dict[str, JsonValue]:
    frozen = freeze_limits(value)
    return {name: getattr(frozen, name) for name in _LIMIT_FIELDS}


def _read_limits(value: object, seq: int) -> BudgetLimits:
    if not isinstance(value, dict) or set(value) != set(_LIMIT_FIELDS):
        raise BudgetProtocolError("budget-limits-invalid", seq)
    values = {name: value[name] for name in _LIMIT_FIELDS}
    if not all(_valid_limit(item) for item in values.values()):
        raise BudgetProtocolError("budget-limits-invalid", seq)
    return BudgetLimits(**values)  # type: ignore[arg-type]


def _valid_amount(value: object) -> bool:
    return (
        type(value) is int
        and 0 <= value <= MAX_BUDGET_VALUE
    )


def freeze_amounts(value: object, *, require_usage: bool = True) -> BudgetAmounts:
    """Validate and copy a charge vector before the first suspension point."""

    if not isinstance(value, BudgetAmounts):
        raise BudgetInputError("budget-amounts-invalid", "amounts")
    try:
        values = {name: getattr(value, name) for name in _AMOUNT_FIELDS}
    except Exception:
        raise BudgetInputError("budget-amounts-invalid", "amounts") from None
    if not all(_valid_amount(item) for item in values.values()):
        raise BudgetInputError("budget-amounts-invalid", "amounts")
    if require_usage and not any(values.values()):
        raise BudgetInputError("budget-charge-empty", "amounts")
    return BudgetAmounts(**values)


def amounts_to_data(value: BudgetAmounts) -> dict[str, JsonValue]:
    frozen = freeze_amounts(value)
    return {name: getattr(frozen, name) for name in _AMOUNT_FIELDS}


def _read_amounts(value: object, seq: int) -> BudgetAmounts:
    if not isinstance(value, dict) or set(value) != set(_AMOUNT_FIELDS):
        raise BudgetProtocolError("budget-amounts-invalid", seq)
    values = {name: value[name] for name in _AMOUNT_FIELDS}
    if not all(_valid_amount(item) for item in values.values()):
        raise BudgetProtocolError("budget-amounts-invalid", seq)
    if not any(values.values()):
        raise BudgetProtocolError("budget-charge-empty", seq)
    return BudgetAmounts(**values)  # type: ignore[arg-type]


def root_granted_data(
    *, operation_id: str, agent_id: str, limits: BudgetLimits
) -> dict[str, JsonValue]:
    return {
        "operation_id": require_budget_identifier(operation_id, field="operation_id"),
        "agent_id": require_budget_identifier(agent_id, field="agent_id"),
        "limits": limits_to_data(limits),
    }


def child_reserved_data(
    *,
    operation_id: str,
    reservation_id: str,
    parent_agent_id: str,
    child_agent_id: str,
    creation_request_id: str,
    child_limits: BudgetLimits,
) -> dict[str, JsonValue]:
    return {
        "operation_id": require_budget_identifier(operation_id, field="operation_id"),
        "reservation_id": require_budget_identifier(reservation_id, field="reservation_id"),
        "parent_agent_id": require_budget_identifier(
            parent_agent_id, field="parent_agent_id"
        ),
        "child_agent_id": require_budget_identifier(child_agent_id, field="child_agent_id"),
        "creation_request_id": require_budget_identifier(
            creation_request_id, field="creation_request_id"
        ),
        "child_limits": limits_to_data(child_limits),
    }


def reservation_terminal_data(
    *, operation_id: str, reservation_id: str
) -> dict[str, JsonValue]:
    return {
        "operation_id": require_budget_identifier(operation_id, field="operation_id"),
        "reservation_id": require_budget_identifier(reservation_id, field="reservation_id"),
    }


def usage_charged_data(
    *, operation_id: str, agent_id: str, amounts: BudgetAmounts
) -> dict[str, JsonValue]:
    return {
        "operation_id": require_budget_identifier(operation_id, field="operation_id"),
        "agent_id": require_budget_identifier(agent_id, field="agent_id"),
        "amounts": amounts_to_data(amounts),
    }


def account_closed_data(*, operation_id: str, agent_id: str) -> dict[str, JsonValue]:
    return {
        "operation_id": require_budget_identifier(operation_id, field="operation_id"),
        "agent_id": require_budget_identifier(agent_id, field="agent_id"),
    }


def _read_identifier(data: dict[str, JsonValue], field: str, seq: int) -> str:
    value = data.get(field)
    if not is_agent_identifier(value):
        raise BudgetProtocolError("budget-identity-invalid", seq)
    assert isinstance(value, str)
    return value


def parse_budget_fact(event: EventEnvelope) -> BudgetFact:
    """Parse one untrusted envelope into the only Budget event vocabulary."""

    try:
        return _read_budget_fact(event)
    except BudgetProtocolError:
        raise
    except Exception:
        # Protocol fields and payload containers are all caller-influenced.
        # Catch `Exception`, never interpreter/cancellation BaseExceptions.
        raise BudgetProtocolError("budget-payload-invalid", event.seq) from None


def _read_budget_fact(event: EventEnvelope) -> BudgetFact:
    if event.stream_id != BUDGET_LEDGER_STREAM:
        raise BudgetProtocolError("budget-stream-unexpected", event.seq)
    if event.schema_version != BUDGET_SCHEMA_VERSION:
        raise BudgetProtocolError("budget-schema-version-unsupported", event.seq)
    data = event.data
    if not isinstance(data, dict):
        raise BudgetProtocolError("budget-payload-invalid", event.seq)

    if event.type == BUDGET_ROOT_GRANTED:
        if set(data) != _ROOT_KEYS:
            raise BudgetProtocolError("budget-payload-keys-unexpected", event.seq)
        return RootGrantedFact(
            operation_id=_read_identifier(data, "operation_id", event.seq),
            agent_id=_read_identifier(data, "agent_id", event.seq),
            limits=_read_limits(data["limits"], event.seq),
            seq=event.seq,
        )
    if event.type == BUDGET_CHILD_RESERVED:
        if set(data) != _RESERVE_KEYS:
            raise BudgetProtocolError("budget-payload-keys-unexpected", event.seq)
        return ChildReservedFact(
            operation_id=_read_identifier(data, "operation_id", event.seq),
            reservation_id=_read_identifier(data, "reservation_id", event.seq),
            parent_agent_id=_read_identifier(data, "parent_agent_id", event.seq),
            child_agent_id=_read_identifier(data, "child_agent_id", event.seq),
            creation_request_id=_read_identifier(data, "creation_request_id", event.seq),
            child_limits=_read_limits(data["child_limits"], event.seq),
            seq=event.seq,
        )
    if event.type in (BUDGET_RESERVATION_COMMITTED, BUDGET_RESERVATION_RELEASED):
        if set(data) != _TERMINAL_KEYS:
            raise BudgetProtocolError("budget-payload-keys-unexpected", event.seq)
        fact_type = (
            ReservationCommittedFact
            if event.type == BUDGET_RESERVATION_COMMITTED
            else ReservationReleasedFact
        )
        return fact_type(
            operation_id=_read_identifier(data, "operation_id", event.seq),
            reservation_id=_read_identifier(data, "reservation_id", event.seq),
            seq=event.seq,
        )
    if event.type == BUDGET_USAGE_CHARGED:
        if set(data) != _CHARGE_KEYS:
            raise BudgetProtocolError("budget-payload-keys-unexpected", event.seq)
        return UsageChargedFact(
            operation_id=_read_identifier(data, "operation_id", event.seq),
            agent_id=_read_identifier(data, "agent_id", event.seq),
            amounts=_read_amounts(data["amounts"], event.seq),
            seq=event.seq,
        )
    if event.type == BUDGET_ACCOUNT_CLOSED:
        if set(data) != _CLOSE_KEYS:
            raise BudgetProtocolError("budget-payload-keys-unexpected", event.seq)
        return AccountClosedFact(
            operation_id=_read_identifier(data, "operation_id", event.seq),
            agent_id=_read_identifier(data, "agent_id", event.seq),
            seq=event.seq,
        )
    raise BudgetProtocolError("budget-event-type-unknown", event.seq)


def is_budget_fact(event: EventEnvelope, event_type: str, data: dict[str, JsonValue]) -> bool:
    """Whether ``event`` is exactly the fact a failed append attempted."""

    try:
        parse_budget_fact(event)
    except BudgetProtocolError:
        return False
    if event.type != event_type:
        return False
    # Encoding failure is unknowable and intentionally propagates to the
    # shared reconciler, which maps it to ``None`` rather than false absence.
    return canonical_json(event.data) == canonical_json(data)


__all__ = [
    "BUDGET_ACCOUNT_CLOSED",
    "BUDGET_CHILD_RESERVED",
    "BUDGET_LEDGER_STREAM",
    "BUDGET_RESERVATION_COMMITTED",
    "BUDGET_RESERVATION_RELEASED",
    "BUDGET_ROOT_GRANTED",
    "BUDGET_SCHEMA_VERSION",
    "BUDGET_USAGE_CHARGED",
    "MAX_BUDGET_VALUE",
    "AccountClosedFact",
    "BudgetFact",
    "ChildReservedFact",
    "ReservationCommittedFact",
    "ReservationReleasedFact",
    "RootGrantedFact",
    "UsageChargedFact",
    "account_closed_data",
    "amounts_to_data",
    "child_reserved_data",
    "freeze_amounts",
    "freeze_limits",
    "is_budget_fact",
    "limits_to_data",
    "parse_budget_fact",
    "require_budget_identifier",
    "reservation_terminal_data",
    "root_granted_data",
    "usage_charged_data",
]
