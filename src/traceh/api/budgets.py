"""Public values for the durable hierarchical Budget protocol.

Budget authority is deliberately separate from Agent identity.  An
``AgentSpec`` says which Agent should exist; a Budget ledger says what a host
has granted, delegated and consumed.  Keeping those facts separate prevents a
caller-controlled creation DTO from minting capacity.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from traceh.api.llm import UsageQuality


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    """Host-issued limits for one Budget account.

    ``None`` means that the host chose not to enforce that dimension.  Every
    field is required at construction so an omitted host decision cannot turn
    into a permissive hidden default.  Durable quantities use integers;
    wall-time is measured in milliseconds so conservation never depends on
    floating-point arithmetic.
    """

    max_tokens: int | None
    max_steps: int | None
    max_tool_calls: int | None
    max_wall_milliseconds: int | None
    max_children: int | None
    max_depth: int | None
    max_processes: int | None


@dataclass(frozen=True, slots=True)
class BudgetAmounts:
    """Durable execution quantities charged or delegated by the ledger.

    Direct-child capacity is intentionally absent: it is owned exclusively by
    child reservations, so a generic usage charge cannot count one child a
    second time.
    """

    tokens: int = 0
    steps: int = 0
    tool_calls: int = 0
    wall_milliseconds: int = 0


class BudgetAccountStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class BudgetReservationStatus(StrEnum):
    PENDING = "pending"
    COMMITTED = "committed"
    RELEASED = "released"


class BudgetChargeMode(StrEnum):
    """Whether capacity was admitted before work or observed afterwards."""

    ADMISSION = "admission"
    OBSERVATION = "observation"


class BudgetUsageReservationStatus(StrEnum):
    PENDING = "pending"
    STARTED = "started"
    SETTLED = "settled"
    RELEASED = "released"


@dataclass(frozen=True, slots=True)
class BudgetAccount:
    """One account reconstructed from root and committed-child facts."""

    agent_id: str
    parent_agent_id: str | None
    limits: BudgetLimits
    status: BudgetAccountStatus
    created_seq: int
    charged: BudgetAmounts = BudgetAmounts()
    delegated: BudgetAmounts = BudgetAmounts()
    reserved: BudgetAmounts = BudgetAmounts()
    reserved_children: int = 0
    closed_seq: int | None = None


@dataclass(frozen=True, slots=True)
class BudgetReservation:
    """A durable hold for one not-yet-or-already-created child Agent."""

    reservation_id: str
    parent_agent_id: str
    child_agent_id: str
    creation_request_id: str
    child_limits: BudgetLimits
    status: BudgetReservationStatus
    reserved_seq: int
    terminal_seq: int | None = None
    identity_seq: int | None = None


@dataclass(frozen=True, slots=True)
class BudgetCharge:
    """One idempotent usage charge."""

    operation_id: str
    agent_id: str
    amounts: BudgetAmounts
    mode: BudgetChargeMode
    usage_quality: UsageQuality | None
    charged_seq: int


@dataclass(frozen=True, slots=True)
class BudgetUsageReservation:
    """One durable Token or active-Turn wall hold and its terminal state."""

    reservation_id: str
    agent_id: str
    amounts: BudgetAmounts
    status: BudgetUsageReservationStatus
    reserved_seq: int
    started_seq: int | None = None
    terminal_seq: int | None = None
    settled_amounts: BudgetAmounts | None = None
    usage_quality: UsageQuality | None = None


__all__ = [
    "BudgetAccount",
    "BudgetAccountStatus",
    "BudgetAmounts",
    "BudgetCharge",
    "BudgetChargeMode",
    "BudgetLimits",
    "BudgetReservation",
    "BudgetReservationStatus",
    "BudgetUsageReservation",
    "BudgetUsageReservationStatus",
]
