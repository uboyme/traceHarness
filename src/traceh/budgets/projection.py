"""Replay the single append-only Budget ledger against durable Agent identity.

The ledger never stores a mutable balance.  Accounts, reservations and usage
are projections of immutable facts, while ``agents:directory`` remains the
only proof that a child Agent was actually created.  In particular,
``budget/reservation-committed`` is an audit acknowledgement, not a second
commit point.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from traceh.agents.directory import AgentDirectory, AgentDirectoryReader
from traceh.api.agents import AgentRecord
from traceh.api.budgets import (
    BudgetAccount,
    BudgetAccountStatus,
    BudgetAmounts,
    BudgetCharge,
    BudgetLimits,
    BudgetReservation,
    BudgetReservationStatus,
)
from traceh.api.events import EventEnvelope
from traceh.api.json_types import JsonValue, canonical_json
from traceh.budgets.errors import (
    BudgetAccountClosedError,
    BudgetAccountNotFoundError,
    BudgetExhaustedError,
    BudgetInputError,
    BudgetProtocolError,
)
from traceh.budgets.events import (
    BUDGET_ACCOUNT_CLOSED,
    BUDGET_CHILD_RESERVED,
    BUDGET_LEDGER_STREAM,
    BUDGET_RESERVATION_COMMITTED,
    BUDGET_RESERVATION_RELEASED,
    BUDGET_ROOT_GRANTED,
    BUDGET_USAGE_CHARGED,
    AccountClosedFact,
    BudgetFact,
    ChildReservedFact,
    ReservationCommittedFact,
    ReservationReleasedFact,
    RootGrantedFact,
    UsageChargedFact,
    parse_budget_fact,
)
from traceh.session.event_store import EventStore


@dataclass(frozen=True, slots=True)
class BudgetLedgerIssue:
    """A stable validation result that never includes untrusted payload text."""

    code: str
    seq: int


@dataclass(frozen=True, slots=True)
class _Operation:
    event_type: str
    payload: str
    seq: int


_CONSUMABLE_DIMENSIONS = (
    ("max_tokens", "tokens"),
    ("max_steps", "steps"),
    ("max_tool_calls", "tool_calls"),
    ("max_wall_milliseconds", "wall_milliseconds"),
)


@dataclass(frozen=True, slots=True)
class _ReservationDebit:
    amounts: BudgetAmounts
    children: int = 1


def _add(left: BudgetAmounts, right: BudgetAmounts) -> BudgetAmounts:
    return BudgetAmounts(
        tokens=left.tokens + right.tokens,
        steps=left.steps + right.steps,
        tool_calls=left.tool_calls + right.tool_calls,
        wall_milliseconds=left.wall_milliseconds + right.wall_milliseconds,
    )


def _subtract(left: BudgetAmounts, right: BudgetAmounts) -> BudgetAmounts:
    values = {
        "tokens": left.tokens - right.tokens,
        "steps": left.steps - right.steps,
        "tool_calls": left.tool_calls - right.tool_calls,
        "wall_milliseconds": left.wall_milliseconds - right.wall_milliseconds,
    }
    if any(value < 0 for value in values.values()):
        raise AssertionError("Budget projection attempted to create negative usage")
    return BudgetAmounts(**values)


def _event_type(fact: BudgetFact) -> str:
    if isinstance(fact, RootGrantedFact):
        return BUDGET_ROOT_GRANTED
    if isinstance(fact, ChildReservedFact):
        return BUDGET_CHILD_RESERVED
    if isinstance(fact, ReservationCommittedFact):
        return BUDGET_RESERVATION_COMMITTED
    if isinstance(fact, ReservationReleasedFact):
        return BUDGET_RESERVATION_RELEASED
    if isinstance(fact, UsageChargedFact):
        return BUDGET_USAGE_CHARGED
    if isinstance(fact, AccountClosedFact):
        return BUDGET_ACCOUNT_CLOSED
    raise AssertionError("unreachable Budget fact")


def _child_debit(
    parent: BudgetLimits, child: BudgetLimits, seq: int
) -> _ReservationDebit:
    """Validate monotonic authority and compute the parent's durable debit.

    Token, Step, Tool and wall-time grants are permanently carved out of the
    parent.  ``max_children`` means cumulative *direct* children, so creating
    this child debits one; the child's own direct-child ceiling is a monotonic
    constraint rather than a second debit.  Depth and process limits are also
    constraints: depth decreases, while process slots are leased and returned
    by the process-local authority in Stage B.
    """

    for field in (
        "max_tokens",
        "max_steps",
        "max_tool_calls",
        "max_wall_milliseconds",
        "max_children",
        "max_processes",
    ):
        parent_value = getattr(parent, field)
        child_value = getattr(child, field)
        if parent_value is not None and (
            child_value is None or child_value > parent_value
        ):
            raise BudgetProtocolError("budget-child-limits-invalid", seq)

    if parent.max_depth is not None:
        if parent.max_depth == 0:
            raise BudgetProtocolError("budget-depth-exhausted", seq)
        if child.max_depth is None or child.max_depth > parent.max_depth - 1:
            raise BudgetProtocolError("budget-child-limits-invalid", seq)

    return _ReservationDebit(
        BudgetAmounts(
            tokens=child.max_tokens or 0,
            steps=child.max_steps or 0,
            tool_calls=child.max_tool_calls or 0,
            wall_milliseconds=child.max_wall_milliseconds or 0,
        )
    )


def _ensure_capacity(
    account: BudgetAccount,
    debit: BudgetAmounts,
    seq: int,
    *,
    children: int = 0,
) -> None:
    used = _add(account.charged, account.delegated)
    for limit_field, amount_field in _CONSUMABLE_DIMENSIONS:
        limit = getattr(account.limits, limit_field)
        if limit is None:
            continue
        if getattr(used, amount_field) + getattr(debit, amount_field) > limit:
            raise BudgetProtocolError("budget-capacity-exceeded", seq)
    if (
        account.limits.max_children is not None
        and account.reserved_children + children > account.limits.max_children
    ):
        raise BudgetProtocolError("budget-capacity-exceeded", seq)


def _directory_child(
    directory: AgentDirectory,
    *,
    parent_agent_id: str,
    child_agent_id: str,
    creation_request_id: str,
    seq: int,
) -> AgentRecord | None:
    by_child = directory.get(child_agent_id)
    by_request = directory.for_request(creation_request_id)
    if by_child is None and by_request is None:
        return None
    if (
        by_child is None
        or by_request is None
        or by_child.agent_id != by_request.agent_id
        or by_child.agent_id != child_agent_id
        or by_child.request_id != creation_request_id
        or by_child.owner_agent_id != parent_agent_id
    ):
        raise BudgetProtocolError("budget-reservation-directory-conflict", seq)
    return by_child


class BudgetLedger:
    """An immutable view rebuilt from Budget facts and the Agent Directory."""

    __slots__ = (
        "_accounts",
        "_charges",
        "_head_seq",
        "_operations",
        "_reservations",
    )

    def __init__(
        self,
        *,
        accounts: dict[str, BudgetAccount],
        reservations: dict[str, BudgetReservation],
        charges: dict[str, BudgetCharge],
        operations: dict[str, _Operation],
        head_seq: int,
    ) -> None:
        self._accounts = dict(accounts)
        self._reservations = dict(reservations)
        self._charges = dict(charges)
        self._operations = dict(operations)
        self._head_seq = head_seq

    @classmethod
    def rebuild(
        cls,
        events: tuple[EventEnvelope, ...],
        directory: AgentDirectory,
    ) -> BudgetLedger:
        accounts: dict[str, BudgetAccount] = {}
        reservations: dict[str, BudgetReservation] = {}
        charges: dict[str, BudgetCharge] = {}
        operations: dict[str, _Operation] = {}
        child_reservations: dict[str, str] = {}
        request_reservations: dict[str, str] = {}
        head_seq = 0

        for expected_seq, event in enumerate(events, start=1):
            try:
                if type(event.seq) is not int or event.seq != expected_seq:
                    raise BudgetProtocolError("budget-sequence-invalid", expected_seq)
                fact = parse_budget_fact(event)
                operation_id = fact.operation_id
                if operation_id in operations:
                    raise BudgetProtocolError("budget-operation-duplicate", fact.seq)
                operations[operation_id] = _Operation(
                    event_type=_event_type(fact),
                    payload=canonical_json(event.data),
                    seq=fact.seq,
                )

                if isinstance(fact, RootGrantedFact):
                    record = directory.get(fact.agent_id)
                    if record is None or record.owner_agent_id is not None:
                        raise BudgetProtocolError("budget-root-agent-invalid", fact.seq)
                    if fact.agent_id in accounts:
                        raise BudgetProtocolError("budget-root-duplicate", fact.seq)
                    accounts[fact.agent_id] = BudgetAccount(
                        agent_id=fact.agent_id,
                        parent_agent_id=None,
                        limits=fact.limits,
                        status=BudgetAccountStatus.OPEN,
                        created_seq=fact.seq,
                    )
                    head_seq = fact.seq
                    continue

                if isinstance(fact, ChildReservedFact):
                    parent = accounts.get(fact.parent_agent_id)
                    if parent is None:
                        raise BudgetProtocolError("budget-account-unknown", fact.seq)
                    if parent.status is BudgetAccountStatus.CLOSED:
                        raise BudgetProtocolError("budget-account-closed", fact.seq)
                    if fact.reservation_id in reservations:
                        raise BudgetProtocolError("budget-reservation-duplicate", fact.seq)
                    if fact.child_agent_id == fact.parent_agent_id:
                        raise BudgetProtocolError(
                            "budget-reservation-directory-conflict", fact.seq
                        )
                    if fact.child_agent_id in accounts:
                        raise BudgetProtocolError("budget-child-account-duplicate", fact.seq)
                    if fact.child_agent_id in child_reservations:
                        raise BudgetProtocolError(
                            "budget-reservation-child-conflict", fact.seq
                        )
                    if fact.creation_request_id in request_reservations:
                        raise BudgetProtocolError(
                            "budget-reservation-request-conflict", fact.seq
                        )
                    debit = _child_debit(parent.limits, fact.child_limits, fact.seq)
                    _ensure_capacity(
                        parent, debit.amounts, fact.seq, children=debit.children
                    )
                    durable_child = _directory_child(
                        directory,
                        parent_agent_id=fact.parent_agent_id,
                        child_agent_id=fact.child_agent_id,
                        creation_request_id=fact.creation_request_id,
                        seq=fact.seq,
                    )
                    status = (
                        BudgetReservationStatus.COMMITTED
                        if durable_child is not None
                        else BudgetReservationStatus.PENDING
                    )
                    reservations[fact.reservation_id] = BudgetReservation(
                        reservation_id=fact.reservation_id,
                        parent_agent_id=fact.parent_agent_id,
                        child_agent_id=fact.child_agent_id,
                        creation_request_id=fact.creation_request_id,
                        child_limits=fact.child_limits,
                        status=status,
                        reserved_seq=fact.seq,
                        identity_seq=(
                            durable_child.created_seq if durable_child is not None else None
                        ),
                    )
                    child_reservations[fact.child_agent_id] = fact.reservation_id
                    request_reservations[fact.creation_request_id] = fact.reservation_id
                    accounts[fact.parent_agent_id] = replace(
                        parent,
                        delegated=_add(parent.delegated, debit.amounts),
                        reserved_children=parent.reserved_children + debit.children,
                    )
                    if durable_child is not None:
                        accounts[fact.child_agent_id] = BudgetAccount(
                            agent_id=fact.child_agent_id,
                            parent_agent_id=fact.parent_agent_id,
                            limits=fact.child_limits,
                            status=BudgetAccountStatus.OPEN,
                            created_seq=fact.seq,
                        )
                    head_seq = fact.seq
                    continue

                if isinstance(fact, ReservationCommittedFact):
                    reservation = reservations.get(fact.reservation_id)
                    if reservation is None:
                        raise BudgetProtocolError("budget-reservation-unknown", fact.seq)
                    if (
                        reservation.status is BudgetReservationStatus.RELEASED
                        or reservation.terminal_seq is not None
                    ):
                        raise BudgetProtocolError("budget-reservation-terminal", fact.seq)
                    durable_child = _directory_child(
                        directory,
                        parent_agent_id=reservation.parent_agent_id,
                        child_agent_id=reservation.child_agent_id,
                        creation_request_id=reservation.creation_request_id,
                        seq=fact.seq,
                    )
                    if durable_child is None:
                        raise BudgetProtocolError("budget-commit-without-agent", fact.seq)
                    reservations[fact.reservation_id] = replace(
                        reservation,
                        status=BudgetReservationStatus.COMMITTED,
                        terminal_seq=fact.seq,
                        identity_seq=durable_child.created_seq,
                    )
                    if reservation.child_agent_id not in accounts:
                        accounts[reservation.child_agent_id] = BudgetAccount(
                            agent_id=reservation.child_agent_id,
                            parent_agent_id=reservation.parent_agent_id,
                            limits=reservation.child_limits,
                            status=BudgetAccountStatus.OPEN,
                            created_seq=reservation.reserved_seq,
                        )
                    head_seq = fact.seq
                    continue

                if isinstance(fact, ReservationReleasedFact):
                    reservation = reservations.get(fact.reservation_id)
                    if reservation is None:
                        raise BudgetProtocolError("budget-reservation-unknown", fact.seq)
                    if (
                        reservation.status is not BudgetReservationStatus.PENDING
                        or reservation.terminal_seq is not None
                    ):
                        if reservation.status is BudgetReservationStatus.COMMITTED:
                            raise BudgetProtocolError("budget-release-after-agent", fact.seq)
                        raise BudgetProtocolError("budget-reservation-terminal", fact.seq)
                    durable_child = _directory_child(
                        directory,
                        parent_agent_id=reservation.parent_agent_id,
                        child_agent_id=reservation.child_agent_id,
                        creation_request_id=reservation.creation_request_id,
                        seq=fact.seq,
                    )
                    if durable_child is not None:
                        raise BudgetProtocolError("budget-release-after-agent", fact.seq)
                    parent = accounts[reservation.parent_agent_id]
                    debit = _child_debit(
                        parent.limits, reservation.child_limits, reservation.reserved_seq
                    )
                    accounts[parent.agent_id] = replace(
                        parent,
                        delegated=_subtract(parent.delegated, debit.amounts),
                        reserved_children=parent.reserved_children - debit.children,
                    )
                    reservations[fact.reservation_id] = replace(
                        reservation,
                        status=BudgetReservationStatus.RELEASED,
                        terminal_seq=fact.seq,
                    )
                    head_seq = fact.seq
                    continue

                if isinstance(fact, UsageChargedFact):
                    account = accounts.get(fact.agent_id)
                    if account is None:
                        raise BudgetProtocolError("budget-account-unknown", fact.seq)
                    if account.status is BudgetAccountStatus.CLOSED:
                        raise BudgetProtocolError("budget-account-closed", fact.seq)
                    for limit_field, amount_field in _CONSUMABLE_DIMENSIONS:
                        if (
                            getattr(fact.amounts, amount_field)
                            and getattr(account.limits, limit_field) is None
                        ):
                            raise BudgetProtocolError("budget-charge-inactive", fact.seq)
                    _ensure_capacity(account, fact.amounts, fact.seq)
                    accounts[fact.agent_id] = replace(
                        account, charged=_add(account.charged, fact.amounts)
                    )
                    charges[fact.operation_id] = BudgetCharge(
                        operation_id=fact.operation_id,
                        agent_id=fact.agent_id,
                        amounts=fact.amounts,
                        charged_seq=fact.seq,
                    )
                    head_seq = fact.seq
                    continue

                assert isinstance(fact, AccountClosedFact)
                account = accounts.get(fact.agent_id)
                if account is None:
                    raise BudgetProtocolError("budget-account-unknown", fact.seq)
                if account.status is BudgetAccountStatus.CLOSED:
                    raise BudgetProtocolError("budget-close-duplicate", fact.seq)
                if any(
                    reservation.parent_agent_id == fact.agent_id
                    and reservation.status is BudgetReservationStatus.PENDING
                    for reservation in reservations.values()
                ):
                    raise BudgetProtocolError(
                        "budget-close-with-pending-reservation", fact.seq
                    )
                accounts[fact.agent_id] = replace(
                    account,
                    status=BudgetAccountStatus.CLOSED,
                    closed_seq=fact.seq,
                )
                head_seq = fact.seq
            except BudgetProtocolError:
                raise
            except Exception:
                raise BudgetProtocolError("budget-payload-invalid", expected_seq) from None

        return cls(
            accounts=accounts,
            reservations=reservations,
            charges=charges,
            operations=operations,
            head_seq=head_seq,
        )

    @property
    def head_seq(self) -> int:
        return self._head_seq

    @property
    def accounts(self) -> tuple[BudgetAccount, ...]:
        return tuple(self._accounts.values())

    @property
    def reservations(self) -> tuple[BudgetReservation, ...]:
        return tuple(self._reservations.values())

    @property
    def charges(self) -> tuple[BudgetCharge, ...]:
        return tuple(self._charges.values())

    def account(self, agent_id: str) -> BudgetAccount | None:
        return self._accounts.get(agent_id)

    def reservation(self, reservation_id: str) -> BudgetReservation | None:
        return self._reservations.get(reservation_id)

    def charge(self, operation_id: str) -> BudgetCharge | None:
        return self._charges.get(operation_id)

    def operation_exists(self, operation_id: str) -> bool:
        return operation_id in self._operations

    def reservation_identity_available(
        self, *, child_agent_id: str, creation_request_id: str
    ) -> bool:
        """Whether neither durable reservation identity has ever been used.

        Released reservations remain history and retain both identities.  A
        later operation must use a fresh child/request pair rather than make
        one durable correlation mean two different creation attempts.
        """

        return not any(
            reservation.child_agent_id == child_agent_id
            or reservation.creation_request_id == creation_request_id
            for reservation in self._reservations.values()
        )

    def operation_matches(
        self,
        operation_id: str,
        event_type: str,
        data: dict[str, JsonValue],
    ) -> bool:
        operation = self._operations.get(operation_id)
        return operation is not None and operation == _Operation(
            event_type=event_type,
            payload=canonical_json(data),
            seq=operation.seq,
        )

    def require_open_account(self, agent_id: str) -> BudgetAccount:
        account = self.account(agent_id)
        if account is None:
            raise BudgetAccountNotFoundError
        if account.status is BudgetAccountStatus.CLOSED:
            raise BudgetAccountClosedError
        return account

    def ensure_reservation_capacity(
        self, parent_agent_id: str, child_limits: BudgetLimits
    ) -> _ReservationDebit:
        account = self.require_open_account(parent_agent_id)
        try:
            debit = _child_debit(account.limits, child_limits, self._head_seq + 1)
            _ensure_capacity(
                account,
                debit.amounts,
                self._head_seq + 1,
                children=debit.children,
            )
        except BudgetProtocolError as error:
            if error.code == "budget-depth-exhausted":
                raise BudgetExhaustedError("max_depth") from None
            if error.code == "budget-capacity-exceeded":
                used = _add(account.charged, account.delegated)
                for limit_field, amount_field in _CONSUMABLE_DIMENSIONS:
                    limit = getattr(account.limits, limit_field)
                    if limit is not None and (
                        getattr(used, amount_field)
                        + getattr(debit.amounts, amount_field)
                        > limit
                    ):
                        raise BudgetExhaustedError(limit_field) from None
                if (
                    account.limits.max_children is not None
                    and account.reserved_children + debit.children
                    > account.limits.max_children
                ):
                    raise BudgetExhaustedError("max_children") from None
            if error.code == "budget-child-limits-invalid":
                raise BudgetInputError(error.code, "child_limits") from None
            raise
        return debit

    def ensure_charge_capacity(
        self, agent_id: str, amounts: BudgetAmounts
    ) -> None:
        account = self.require_open_account(agent_id)
        for limit_field, amount_field in _CONSUMABLE_DIMENSIONS:
            if getattr(amounts, amount_field) and getattr(account.limits, limit_field) is None:
                raise BudgetInputError("budget-charge-inactive", "amounts")
        try:
            _ensure_capacity(account, amounts, self._head_seq + 1)
        except BudgetProtocolError as error:
            if error.code == "budget-capacity-exceeded":
                used = _add(account.charged, account.delegated)
                for limit_field, amount_field in _CONSUMABLE_DIMENSIONS:
                    limit = getattr(account.limits, limit_field)
                    if limit is not None and (
                        getattr(used, amount_field) + getattr(amounts, amount_field) > limit
                    ):
                        raise BudgetExhaustedError(limit_field) from None
            raise

    def available(self, agent_id: str) -> BudgetLimits:
        account = self.require_open_account(agent_id)
        used = _add(account.charged, account.delegated)

        def remaining(limit: int | None, amount: int) -> int | None:
            return None if limit is None else limit - amount

        return BudgetLimits(
            max_tokens=remaining(account.limits.max_tokens, used.tokens),
            max_steps=remaining(account.limits.max_steps, used.steps),
            max_tool_calls=remaining(account.limits.max_tool_calls, used.tool_calls),
            max_wall_milliseconds=remaining(
                account.limits.max_wall_milliseconds, used.wall_milliseconds
            ),
            max_children=remaining(
                account.limits.max_children, account.reserved_children
            ),
            max_depth=account.limits.max_depth,
            max_processes=account.limits.max_processes,
        )


def validate_budget_ledger_events(
    events: tuple[EventEnvelope, ...], directory: AgentDirectory
) -> tuple[BudgetLedgerIssue, ...]:
    """Return a stable first failure without exposing untrusted fact data."""

    try:
        BudgetLedger.rebuild(events, directory)
    except BudgetProtocolError as error:
        return (BudgetLedgerIssue(error.code, error.seq),)
    return ()


class BudgetLedgerReader:
    """Rebuild Budget state from one EventStore and its Agent Directory."""

    __slots__ = ("_directory_reader", "_store")

    def __init__(self, store: EventStore) -> None:
        self._store = store
        self._directory_reader = AgentDirectoryReader(store)

    @property
    def store(self) -> EventStore:
        return self._store

    async def read_events(self) -> tuple[EventEnvelope, ...]:
        return await self._store.read(BUDGET_LEDGER_STREAM)

    async def load_context(self) -> tuple[BudgetLedger, AgentDirectory]:
        # Budget facts may depend on Agent Directory facts that were durably
        # appended first.  Read the dependent stream before its prerequisite:
        # the subsequent Directory snapshot is then at least as fresh as the
        # Budget prefix being projected.  Reversing these reads can combine an
        # old Directory with a new root grant/commit and falsely report valid
        # concurrent history as corrupt.  A newer Directory with an older
        # Budget prefix is safe and fail-closed: unrelated Agent facts confer
        # no Budget authority until their ledger facts become visible.
        events = await self.read_events()
        directory = await self._directory_reader.load()
        ledger = BudgetLedger.rebuild(events, directory)
        return ledger, directory

    async def load(self) -> BudgetLedger:
        ledger, _ = await self.load_context()
        return ledger


__all__ = [
    "BUDGET_LEDGER_STREAM",
    "BudgetLedger",
    "BudgetLedgerIssue",
    "BudgetLedgerReader",
    "validate_budget_ledger_events",
]
