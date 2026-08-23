"""Host-owned mutations for the append-only Budget ledger."""

from __future__ import annotations

import asyncio

from traceh.agents.commit_reconciliation import committed_after_failure
from traceh.api.budgets import (
    BudgetAccount,
    BudgetAmounts,
    BudgetCharge,
    BudgetLimits,
    BudgetReservation,
    BudgetReservationStatus,
)
from traceh.api.events import EventEnvelope, PendingEvent
from traceh.api.json_types import JsonValue
from traceh.budgets.errors import (
    BudgetDirectoryMismatchError,
    BudgetLedgerConflictError,
    BudgetOperationConflictError,
    BudgetReservationNotFoundError,
    BudgetReservationStateError,
    BudgetWriteError,
)
from traceh.budgets.events import (
    BUDGET_ACCOUNT_CLOSED,
    BUDGET_CHILD_RESERVED,
    BUDGET_LEDGER_STREAM,
    BUDGET_RESERVATION_COMMITTED,
    BUDGET_RESERVATION_RELEASED,
    BUDGET_ROOT_GRANTED,
    BUDGET_USAGE_CHARGED,
    account_closed_data,
    child_reserved_data,
    freeze_amounts,
    freeze_limits,
    is_budget_fact,
    reservation_terminal_data,
    root_granted_data,
    usage_charged_data,
)
from traceh.budgets.projection import BudgetLedger, BudgetLedgerReader
from traceh.session.event_store import ConcurrencyConflict, Durability, EventStore


class BudgetLedgerService:
    """Append Budget facts with idempotency, CAS and unknown-outcome repair.

    This is a host control surface, not a model Tool.  It creates no Agent and
    performs no Runtime work.  Stage B will compose these operations around
    the existing owned execution boundaries.
    """

    __slots__ = ("_lock", "_reader", "_store")

    def __init__(self, store: EventStore) -> None:
        self._store = store
        self._reader = BudgetLedgerReader(store)
        self._lock = asyncio.Lock()

    @property
    def store(self) -> EventStore:
        return self._store

    async def ledger(self) -> BudgetLedger:
        return await self._reader.load()

    async def grant_root(
        self,
        *,
        operation_id: str,
        agent_id: str,
        limits: BudgetLimits,
    ) -> BudgetAccount:
        data = root_granted_data(
            operation_id=operation_id,
            agent_id=agent_id,
            limits=freeze_limits(limits),
        )
        async with self._lock:
            ledger, directory = await self._reader.load_context()
            if self._operation_matches_or_raise(
                ledger, operation_id, BUDGET_ROOT_GRANTED, data
            ):
                account = ledger.account(agent_id)
                assert account is not None
                return account
            record = directory.get(agent_id)
            if record is None or record.owner_agent_id is not None:
                raise BudgetDirectoryMismatchError
            if ledger.account(agent_id) is not None:
                raise BudgetOperationConflictError
            await self._append(
                expected_seq=ledger.head_seq,
                event_type=BUDGET_ROOT_GRANTED,
                data=data,
            )
            account = (await self._reader.load()).account(agent_id)
            assert account is not None
            return account

    async def reserve_child(
        self,
        *,
        operation_id: str,
        reservation_id: str,
        parent_agent_id: str,
        child_agent_id: str,
        creation_request_id: str,
        child_limits: BudgetLimits,
    ) -> BudgetReservation:
        data = child_reserved_data(
            operation_id=operation_id,
            reservation_id=reservation_id,
            parent_agent_id=parent_agent_id,
            child_agent_id=child_agent_id,
            creation_request_id=creation_request_id,
            child_limits=freeze_limits(child_limits, field="child_limits"),
        )
        async with self._lock:
            ledger, directory = await self._reader.load_context()
            if self._operation_matches_or_raise(
                ledger, operation_id, BUDGET_CHILD_RESERVED, data
            ):
                reservation = ledger.reservation(reservation_id)
                assert reservation is not None
                return reservation
            if ledger.reservation(reservation_id) is not None:
                raise BudgetOperationConflictError
            if ledger.account(child_agent_id) is not None:
                raise BudgetOperationConflictError
            if not ledger.reservation_identity_available(
                child_agent_id=child_agent_id,
                creation_request_id=creation_request_id,
            ):
                raise BudgetOperationConflictError
            # Reserve-before-create is an authority boundary, not merely a
            # happy-path ordering convention.  A pre-existing Directory fact
            # cannot be adopted after the effect and assigned a grant later.
            if (
                directory.get(child_agent_id) is not None
                or directory.for_request(creation_request_id) is not None
            ):
                raise BudgetDirectoryMismatchError
            ledger.ensure_reservation_capacity(parent_agent_id, child_limits)
            await self._append(
                expected_seq=ledger.head_seq,
                event_type=BUDGET_CHILD_RESERVED,
                data=data,
            )
            reservation = (await self._reader.load()).reservation(reservation_id)
            assert reservation is not None
            return reservation

    async def commit_reservation(
        self, *, operation_id: str, reservation_id: str
    ) -> BudgetReservation:
        data = reservation_terminal_data(
            operation_id=operation_id, reservation_id=reservation_id
        )
        async with self._lock:
            ledger, directory = await self._reader.load_context()
            if self._operation_matches_or_raise(
                ledger, operation_id, BUDGET_RESERVATION_COMMITTED, data
            ):
                reservation = ledger.reservation(reservation_id)
                assert reservation is not None
                return reservation
            reservation = ledger.reservation(reservation_id)
            if reservation is None:
                raise BudgetReservationNotFoundError
            if reservation.terminal_seq is not None:
                raise BudgetReservationStateError
            child = directory.get(reservation.child_agent_id)
            by_request = directory.for_request(reservation.creation_request_id)
            if (
                child is None
                or by_request is None
                or child.agent_id != by_request.agent_id
                or child.request_id != reservation.creation_request_id
                or child.owner_agent_id != reservation.parent_agent_id
            ):
                raise BudgetDirectoryMismatchError
            await self._append(
                expected_seq=ledger.head_seq,
                event_type=BUDGET_RESERVATION_COMMITTED,
                data=data,
            )
            result = (await self._reader.load()).reservation(reservation_id)
            assert result is not None
            return result

    async def release_reservation(
        self,
        *,
        operation_id: str,
        reservation_id: str,
        creation_converged: bool,
    ) -> BudgetReservation:
        """Release a hold only after its Agent creation operation converged.

        The boolean is an explicit trusted-host assertion; it is not exposed as
        a model argument.  Stage B's managed creation saga will supply it only
        after the Supervisor operation and cleanup have both settled.  The
        durable Directory is re-read here as the second, independent guard.
        """

        if creation_converged is not True:
            raise BudgetReservationStateError
        data = reservation_terminal_data(
            operation_id=operation_id, reservation_id=reservation_id
        )
        async with self._lock:
            ledger, directory = await self._reader.load_context()
            if self._operation_matches_or_raise(
                ledger, operation_id, BUDGET_RESERVATION_RELEASED, data
            ):
                reservation = ledger.reservation(reservation_id)
                assert reservation is not None
                return reservation
            reservation = ledger.reservation(reservation_id)
            if reservation is None:
                raise BudgetReservationNotFoundError
            if (
                reservation.status is not BudgetReservationStatus.PENDING
                or reservation.terminal_seq is not None
            ):
                raise BudgetReservationStateError
            if (
                directory.get(reservation.child_agent_id) is not None
                or directory.for_request(reservation.creation_request_id) is not None
            ):
                raise BudgetDirectoryMismatchError
            await self._append(
                expected_seq=ledger.head_seq,
                event_type=BUDGET_RESERVATION_RELEASED,
                data=data,
            )
            result = (await self._reader.load()).reservation(reservation_id)
            assert result is not None
            return result

    async def charge(
        self,
        *,
        operation_id: str,
        agent_id: str,
        amounts: BudgetAmounts,
    ) -> BudgetCharge:
        amounts = freeze_amounts(amounts)
        data = usage_charged_data(
            operation_id=operation_id, agent_id=agent_id, amounts=amounts
        )
        async with self._lock:
            ledger, _ = await self._reader.load_context()
            if self._operation_matches_or_raise(
                ledger, operation_id, BUDGET_USAGE_CHARGED, data
            ):
                charge = ledger.charge(operation_id)
                assert charge is not None
                return charge
            ledger.ensure_charge_capacity(agent_id, amounts)
            await self._append(
                expected_seq=ledger.head_seq,
                event_type=BUDGET_USAGE_CHARGED,
                data=data,
            )
            charge = (await self._reader.load()).charge(operation_id)
            assert charge is not None
            return charge

    async def close_account(
        self, *, operation_id: str, agent_id: str
    ) -> BudgetAccount:
        data = account_closed_data(operation_id=operation_id, agent_id=agent_id)
        async with self._lock:
            ledger, _ = await self._reader.load_context()
            if self._operation_matches_or_raise(
                ledger, operation_id, BUDGET_ACCOUNT_CLOSED, data
            ):
                account = ledger.account(agent_id)
                assert account is not None
                return account
            account = ledger.require_open_account(agent_id)
            if any(
                reservation.parent_agent_id == account.agent_id
                and reservation.status is BudgetReservationStatus.PENDING
                for reservation in ledger.reservations
            ):
                raise BudgetReservationStateError
            await self._append(
                expected_seq=ledger.head_seq,
                event_type=BUDGET_ACCOUNT_CLOSED,
                data=data,
            )
            result = (await self._reader.load()).account(agent_id)
            assert result is not None
            return result

    @staticmethod
    def _operation_matches_or_raise(
        ledger: BudgetLedger,
        operation_id: str,
        event_type: str,
        data: dict[str, JsonValue],
    ) -> bool:
        if not ledger.operation_exists(operation_id):
            return False
        if ledger.operation_matches(operation_id, event_type, data):
            return True
        raise BudgetOperationConflictError

    async def _append(
        self,
        *,
        expected_seq: int,
        event_type: str,
        data: dict[str, JsonValue],
    ) -> EventEnvelope | None:
        try:
            appended = await self._store.append(
                BUDGET_LEDGER_STREAM,
                expected_seq=expected_seq,
                events=(PendingEvent(type=event_type, data=data),),
                durability=Durability.SYNC,
            )
        except asyncio.CancelledError as error:
            await self._committed(event_type, data)
            raise error
        except Exception as error:
            committed = await self._committed(event_type, data)
            if isinstance(error, ConcurrencyConflict):
                if committed is True:
                    # Another writer linearized the exact same idempotent fact.
                    # The public operation reloads the projected result next.
                    return None
                if committed is False:
                    raise BudgetLedgerConflictError from None
            raise BudgetWriteError(committed=committed) from None
        return appended[0]

    async def _committed(
        self, event_type: str, data: dict[str, JsonValue]
    ) -> bool | None:
        def matches(event: EventEnvelope) -> bool:
            return is_budget_fact(event, event_type, data)

        return await committed_after_failure(self._reader.read_events, matches)


__all__ = ["BudgetLedgerService"]
