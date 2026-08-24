"""v0.7-A contracts for the one append-only hierarchical Budget ledger."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from traceh.agents import AgentDirectoryProtocolError, AgentRegistrar
from traceh.agents.directory import AGENT_DIRECTORY_STREAM
from traceh.api.agents import AgentSpec
from traceh.api.budgets import (
    BudgetAccountStatus,
    BudgetAmounts,
    BudgetLimits,
    BudgetReservationStatus,
    BudgetUsageReservationStatus,
)
from traceh.api.events import EventEnvelope, PendingEvent
from traceh.api.llm import UsageQuality
from traceh.budgets import (
    BUDGET_LEDGER_STREAM,
    BudgetAccountClosedError,
    BudgetDirectoryMismatchError,
    BudgetExhaustedError,
    BudgetInputError,
    BudgetLedger,
    BudgetLedgerConflictError,
    BudgetLedgerReader,
    BudgetLedgerService,
    BudgetOperationConflictError,
    BudgetProtocolError,
    BudgetReservationStateError,
    BudgetWriteError,
    validate_budget_ledger_events,
)
from traceh.budgets.events import (
    BUDGET_ACCOUNT_CLOSED,
    BUDGET_CHILD_RESERVED,
    BUDGET_RESERVATION_RELEASED,
    BUDGET_ROOT_GRANTED,
    BUDGET_SCHEMA_VERSION,
    BUDGET_USAGE_CHARGED,
    BUDGET_USAGE_SETTLED,
    account_closed_data,
    child_reserved_data,
    root_granted_data,
    usage_settled_data,
)
from traceh.session.event_store import Durability, EventStore, InMemoryEventStore

pytestmark = pytest.mark.asyncio


def limits(**overrides: int | None) -> BudgetLimits:
    values: dict[str, int | None] = {
        "max_tokens": 100,
        "max_steps": 20,
        "max_tool_calls": 30,
        "max_wall_milliseconds": 10_000,
        "max_children": 3,
        "max_depth": 3,
        "max_processes": 4,
    }
    values.update(overrides)
    return BudgetLimits(**values)


def spec(*, owner_agent_id: str | None = None) -> AgentSpec:
    return AgentSpec(
        preset="budget-test",
        workspace_id="budget-workspace",
        owner_agent_id=owner_agent_id,
    )


async def create_agent(
    store: EventStore,
    agent_id: str,
    *,
    owner_agent_id: str | None = None,
    request_id: str | None = None,
) -> None:
    await AgentRegistrar(store).create_agent(
        spec(owner_agent_id=owner_agent_id),
        request_id=request_id or f"create-{agent_id}",
        agent_id=agent_id,
        session_id=f"session-{agent_id}",
    )


async def granted_root(
    store: EventStore, *, agent_id: str = "root", root_limits: BudgetLimits | None = None
) -> BudgetLedgerService:
    await create_agent(store, agent_id)
    service = BudgetLedgerService(store)
    await service.grant_root(
        operation_id=f"grant-{agent_id}",
        agent_id=agent_id,
        limits=root_limits or limits(),
    )
    return service


def envelope(
    seq: int,
    event_type: str,
    data: dict,
    *,
    stream_id: str = BUDGET_LEDGER_STREAM,
    schema_version: int = BUDGET_SCHEMA_VERSION,
) -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid4(),
        stream_id=stream_id,
        seq=seq,
        type=event_type,
        schema_version=schema_version,
        data=data,
        occurred_at=datetime.now(UTC),
    )


class RaisingComparisonString(str):
    def __eq__(self, other):
        raise ValueError("untrusted comparison")

    def __ne__(self, other):
        raise ValueError("untrusted comparison")


class RaisingComparisonInteger(int):
    def __eq__(self, other):
        raise ValueError("untrusted comparison")

    def __ne__(self, other):
        raise ValueError("untrusted comparison")


class RaisingOrderInteger(int):
    def __ge__(self, other):
        raise RuntimeError("untrusted lower-bound comparison")

    def __le__(self, other):
        raise RuntimeError("untrusted upper-bound comparison")


class InterruptingComparisonString(str):
    def __eq__(self, other):
        raise KeyboardInterrupt

    def __ne__(self, other):
        raise KeyboardInterrupt


class InterruptingComparisonInteger(int):
    def __eq__(self, other):
        raise KeyboardInterrupt

    def __ne__(self, other):
        raise KeyboardInterrupt


class RaisingIteration(dict):
    def __iter__(self):
        raise ValueError("untrusted iteration")


async def test_root_grant_rebuilds_from_fresh_readers() -> None:
    store = InMemoryEventStore()
    service = await granted_root(store)

    first = await service.ledger()
    fresh = await BudgetLedgerReader(store).load()
    assert fresh.accounts == first.accounts
    assert fresh.account("root").limits == limits()
    assert fresh.available("root") == limits()
    assert len(await store.read(BUDGET_LEDGER_STREAM)) == 1


class CrossStreamSnapshotStore:
    """Fix the first cross-stream read while a legal writer advances both.

    If the reader starts with Directory, that first result is deliberately the
    old snapshot and the following Budget read is new.  If it starts with the
    dependent Budget stream, both returned snapshots are new.  This pins the
    dependency order without sleeping or changing production code.
    """

    def __init__(self, inner: EventStore) -> None:
        self.inner = inner
        self.first_read_started = asyncio.Event()
        self.release_first_read = asyncio.Event()
        self.first_stream: str | None = None

    async def append(self, stream_id, *, expected_seq, events, durability=Durability.SYNC):
        return await self.inner.append(
            stream_id,
            expected_seq=expected_seq,
            events=events,
            durability=durability,
        )

    async def read(self, stream_id, *, from_seq=1):
        if self.first_stream is None and stream_id in {
            AGENT_DIRECTORY_STREAM,
            BUDGET_LEDGER_STREAM,
        }:
            self.first_stream = stream_id
            captured = None
            if stream_id == AGENT_DIRECTORY_STREAM:
                captured = await self.inner.read(stream_id, from_seq=from_seq)
            self.first_read_started.set()
            await self.release_first_read.wait()
            if captured is not None:
                return captured
        return await self.inner.read(stream_id, from_seq=from_seq)

    async def head(self, stream_id):
        return await self.inner.head(stream_id)

    async def list_streams(self, *, prefix=None):
        return await self.inner.list_streams(prefix=prefix)


async def test_cross_stream_reader_never_pairs_old_directory_with_new_budget() -> None:
    inner = InMemoryEventStore()
    store = CrossStreamSnapshotStore(inner)
    loading = asyncio.create_task(BudgetLedgerReader(store).load())
    await store.first_read_started.wait()

    await create_agent(inner, "concurrent-root")
    await inner.append(
        BUDGET_LEDGER_STREAM,
        expected_seq=0,
        events=(
            PendingEvent(
                type=BUDGET_ROOT_GRANTED,
                data=root_granted_data(
                    operation_id="concurrent-grant",
                    agent_id="concurrent-root",
                    limits=limits(),
                ),
            ),
        ),
    )
    store.release_first_read.set()

    ledger = await loading
    assert store.first_stream == BUDGET_LEDGER_STREAM
    assert ledger.account("concurrent-root") is not None


async def test_root_grant_requires_a_durable_root_agent() -> None:
    store = InMemoryEventStore()
    service = BudgetLedgerService(store)
    with pytest.raises(BudgetDirectoryMismatchError):
        await service.grant_root(
            operation_id="grant-missing", agent_id="missing", limits=limits()
        )
    assert await store.read(BUDGET_LEDGER_STREAM) == ()

    await create_agent(store, "parent")
    await create_agent(store, "child", owner_agent_id="parent")
    with pytest.raises(BudgetDirectoryMismatchError):
        await service.grant_root(
            operation_id="grant-child", agent_id="child", limits=limits()
        )


async def test_reservation_holds_capacity_until_released() -> None:
    store = InMemoryEventStore()
    service = await granted_root(store)
    child_limits = limits(
        max_tokens=40,
        max_steps=5,
        max_tool_calls=7,
        max_wall_milliseconds=2_000,
        max_children=1,
        max_depth=2,
        max_processes=2,
    )
    reservation = await service.reserve_child(
        operation_id="reserve-child",
        reservation_id="reservation-child",
        parent_agent_id="root",
        child_agent_id="child",
        creation_request_id="create-child",
        child_limits=child_limits,
    )
    assert reservation.status is BudgetReservationStatus.PENDING
    assert (await service.ledger()).available("root") == limits(
        max_tokens=60,
        max_steps=15,
        max_tool_calls=23,
        max_wall_milliseconds=8_000,
        max_children=2,
    )

    released = await service.release_reservation(
        operation_id="release-child",
        reservation_id="reservation-child",
        creation_converged=True,
    )
    assert released.status is BudgetReservationStatus.RELEASED
    assert (await service.ledger()).available("root") == limits()


async def test_directory_identity_is_the_only_child_commit_point() -> None:
    store = InMemoryEventStore()
    service = await granted_root(store)
    child_limits = limits(max_tokens=30, max_depth=2)
    await service.reserve_child(
        operation_id="reserve-child",
        reservation_id="reservation-child",
        parent_agent_id="root",
        child_agent_id="child",
        creation_request_id="create-child",
        child_limits=child_limits,
    )
    await create_agent(
        store,
        "child",
        owner_agent_id="root",
        request_id="create-child",
    )

    # No budget/committed event exists yet, but a fresh projector sees the
    # exact durable Directory fact and therefore opens the child account.
    ledger = await BudgetLedgerReader(store).load()
    assert ledger.reservation("reservation-child").status is BudgetReservationStatus.COMMITTED
    assert ledger.account("child").limits == child_limits
    assert len(await store.read(BUDGET_LEDGER_STREAM)) == 2

    audited = await service.commit_reservation(
        operation_id="commit-child", reservation_id="reservation-child"
    )
    assert audited.status is BudgetReservationStatus.COMMITTED
    assert audited.terminal_seq == 3


async def test_commit_without_exact_directory_identity_is_rejected() -> None:
    store = InMemoryEventStore()
    service = await granted_root(store)
    await service.reserve_child(
        operation_id="reserve-child",
        reservation_id="reservation-child",
        parent_agent_id="root",
        child_agent_id="child",
        creation_request_id="create-child",
        child_limits=limits(max_depth=2),
    )
    with pytest.raises(BudgetDirectoryMismatchError):
        await service.commit_reservation(
            operation_id="commit-child", reservation_id="reservation-child"
        )
    assert len(await store.read(BUDGET_LEDGER_STREAM)) == 2


async def test_release_requires_convergence_and_refuses_a_durable_child() -> None:
    store = InMemoryEventStore()
    service = await granted_root(store)
    await service.reserve_child(
        operation_id="reserve-child",
        reservation_id="reservation-child",
        parent_agent_id="root",
        child_agent_id="child",
        creation_request_id="create-child",
        child_limits=limits(max_depth=2),
    )
    with pytest.raises(BudgetReservationStateError):
        await service.release_reservation(
            operation_id="release-child",
            reservation_id="reservation-child",
            creation_converged=False,
        )
    await create_agent(
        store, "child", owner_agent_id="root", request_id="create-child"
    )
    with pytest.raises(BudgetReservationStateError):
        await service.release_reservation(
            operation_id="release-child",
            reservation_id="reservation-child",
            creation_converged=True,
        )


@pytest.mark.parametrize(
    "child_limits",
    [
        limits(max_tokens=101, max_depth=2),
        limits(max_tokens=None, max_depth=2),
        limits(max_depth=3),
        limits(max_processes=5, max_depth=2),
        limits(max_children=4, max_depth=2),
    ],
)
async def test_child_authority_cannot_exceed_parent_constraints(
    child_limits: BudgetLimits,
) -> None:
    store = InMemoryEventStore()
    service = await granted_root(store)
    with pytest.raises(BudgetInputError) as error:
        await service.reserve_child(
            operation_id="reserve-child",
            reservation_id="reservation-child",
            parent_agent_id="root",
            child_agent_id="child",
            creation_request_id="create-child",
            child_limits=child_limits,
        )
    assert error.value.code == "budget-child-limits-invalid"
    assert len(await store.read(BUDGET_LEDGER_STREAM)) == 1


async def test_usage_and_delegation_share_one_conserved_balance() -> None:
    store = InMemoryEventStore()
    service = await granted_root(store)
    await service.admit_usage(
        operation_id="charge-root",
        agent_id="root",
        amounts=BudgetAmounts(tokens=61),
    )
    with pytest.raises(BudgetExhaustedError) as error:
        await service.reserve_child(
            operation_id="reserve-child",
            reservation_id="reservation-child",
            parent_agent_id="root",
            child_agent_id="child",
            creation_request_id="create-child",
            child_limits=limits(max_tokens=40, max_depth=2),
        )
    assert error.value.dimension == "max_tokens"
    assert len(await store.read(BUDGET_LEDGER_STREAM)) == 2


@pytest.mark.parametrize("reuse", ["child", "request"])
async def test_reservation_correlation_identities_cannot_be_reused(
    reuse: str,
) -> None:
    store = InMemoryEventStore()
    service = await granted_root(store)
    await service.reserve_child(
        operation_id="reserve-first",
        reservation_id="reservation-first",
        parent_agent_id="root",
        child_agent_id="child-a",
        creation_request_id="create-child-a",
        child_limits=limits(max_tokens=10, max_depth=2),
    )
    await service.release_reservation(
        operation_id="release-first",
        reservation_id="reservation-first",
        creation_converged=True,
    )

    with pytest.raises(BudgetOperationConflictError):
        await service.reserve_child(
            operation_id="reserve-second",
            reservation_id="reservation-second",
            parent_agent_id="root",
            child_agent_id="child-a" if reuse == "child" else "child-b",
            creation_request_id=(
                "create-child-b" if reuse == "child" else "create-child-a"
            ),
            child_limits=limits(max_tokens=10, max_depth=2),
        )

    assert len(await store.read(BUDGET_LEDGER_STREAM)) == 3


@pytest.mark.parametrize("reuse", ["child", "request"])
async def test_replay_rejects_reused_reservation_correlation_identities(
    reuse: str,
) -> None:
    store = InMemoryEventStore()
    await create_agent(store, "root")
    directory = await AgentRegistrar(store).directory()
    first = child_reserved_data(
        operation_id="reserve-first",
        reservation_id="reservation-first",
        parent_agent_id="root",
        child_agent_id="child-a",
        creation_request_id="create-child-a",
        child_limits=limits(max_tokens=10, max_depth=2),
    )
    second = child_reserved_data(
        operation_id="reserve-second",
        reservation_id="reservation-second",
        parent_agent_id="root",
        child_agent_id="child-a" if reuse == "child" else "child-b",
        creation_request_id=(
            "create-child-b" if reuse == "child" else "create-child-a"
        ),
        child_limits=limits(max_tokens=10, max_depth=2),
    )
    events = (
        envelope(
            1,
            BUDGET_ROOT_GRANTED,
            root_granted_data(
                operation_id="grant-root", agent_id="root", limits=limits()
            ),
        ),
        envelope(2, BUDGET_CHILD_RESERVED, first),
        envelope(3, BUDGET_CHILD_RESERVED, second),
    )

    with pytest.raises(BudgetProtocolError) as error:
        BudgetLedger.rebuild(events, directory)
    assert error.value.code == (
        "budget-reservation-child-conflict"
        if reuse == "child"
        else "budget-reservation-request-conflict"
    )


async def test_direct_child_limit_counts_one_per_durable_reservation() -> None:
    store = InMemoryEventStore()
    service = await granted_root(store, root_limits=limits(max_children=1))
    await service.reserve_child(
        operation_id="reserve-first",
        reservation_id="reservation-first",
        parent_agent_id="root",
        child_agent_id="child-a",
        creation_request_id="create-child-a",
        child_limits=limits(
            max_tokens=0,
            max_steps=0,
            max_tool_calls=0,
            max_wall_milliseconds=0,
            max_children=1,
            max_depth=2,
        ),
    )
    with pytest.raises(BudgetExhaustedError) as error:
        await service.reserve_child(
            operation_id="reserve-second",
            reservation_id="reservation-second",
            parent_agent_id="root",
            child_agent_id="child-b",
            creation_request_id="create-child-b",
            child_limits=limits(
                max_tokens=0,
                max_steps=0,
                max_tool_calls=0,
                max_wall_milliseconds=0,
                max_children=1,
                max_depth=2,
            ),
        )
    assert error.value.dimension == "max_children"


async def test_zero_parent_depth_is_exhaustion_not_invalid_child_input() -> None:
    store = InMemoryEventStore()
    service = await granted_root(store, root_limits=limits(max_depth=0))
    with pytest.raises(BudgetExhaustedError) as error:
        await service.reserve_child(
            operation_id="reserve-child",
            reservation_id="reservation-child",
            parent_agent_id="root",
            child_agent_id="child",
            creation_request_id="create-child",
            child_limits=limits(max_depth=0),
        )
    assert error.value.dimension == "max_depth"
    assert len(await store.read(BUDGET_LEDGER_STREAM)) == 1


async def test_inactive_dimensions_are_explicit_and_are_not_charged() -> None:
    store = InMemoryEventStore()
    service = await granted_root(
        store, root_limits=limits(max_tokens=None, max_steps=None)
    )
    with pytest.raises(BudgetInputError) as error:
        await service.admit_usage(
            operation_id="charge-inactive",
            agent_id="root",
            amounts=BudgetAmounts(tokens=1),
        )
    assert error.value.code == "budget-charge-inactive"


async def test_close_is_terminal_and_refuses_pending_reservations() -> None:
    store = InMemoryEventStore()
    service = await granted_root(store)
    await service.reserve_child(
        operation_id="reserve-child",
        reservation_id="reservation-child",
        parent_agent_id="root",
        child_agent_id="child",
        creation_request_id="create-child",
        child_limits=limits(max_depth=2),
    )
    with pytest.raises(BudgetReservationStateError):
        await service.close_account(operation_id="close-root", agent_id="root")
    await service.release_reservation(
        operation_id="release-child",
        reservation_id="reservation-child",
        creation_converged=True,
    )
    account = await service.close_account(
        operation_id="close-root", agent_id="root"
    )
    assert account.status is BudgetAccountStatus.CLOSED
    with pytest.raises(BudgetAccountClosedError):
        await service.admit_usage(
            operation_id="charge-closed",
            agent_id="root",
            amounts=BudgetAmounts(steps=1),
        )


async def test_same_operation_is_idempotent_and_different_payload_conflicts() -> None:
    store = InMemoryEventStore()
    await create_agent(store, "root")
    service = BudgetLedgerService(store)
    first = await service.grant_root(
        operation_id="grant-root", agent_id="root", limits=limits()
    )
    assert (
        await service.grant_root(
            operation_id="grant-root", agent_id="root", limits=limits()
        )
        == first
    )
    with pytest.raises(BudgetOperationConflictError):
        await service.grant_root(
            operation_id="grant-root",
            agent_id="root",
            limits=limits(max_tokens=99),
        )
    assert len(await store.read(BUDGET_LEDGER_STREAM)) == 1


class AppendBarrierStore:
    def __init__(self, inner: EventStore) -> None:
        self.inner = inner
        self.enabled = False
        self.entered = 0
        self.both_entered = asyncio.Event()
        self.release = asyncio.Event()

    async def append(self, stream_id, *, expected_seq, events, durability=Durability.SYNC):
        if self.enabled and stream_id == BUDGET_LEDGER_STREAM:
            self.entered += 1
            if self.entered == 2:
                self.both_entered.set()
            await self.release.wait()
        return await self.inner.append(
            stream_id,
            expected_seq=expected_seq,
            events=events,
            durability=durability,
        )

    async def read(self, stream_id, *, from_seq=1):
        return await self.inner.read(stream_id, from_seq=from_seq)

    async def head(self, stream_id):
        return await self.inner.head(stream_id)

    async def list_streams(self, *, prefix=None):
        return await self.inner.list_streams(prefix=prefix)


async def test_concurrent_reservations_cannot_overspend_one_parent() -> None:
    inner = InMemoryEventStore()
    barrier = AppendBarrierStore(inner)
    await granted_root(barrier, root_limits=limits(max_tokens=50, max_children=1))
    barrier.enabled = True
    services = (BudgetLedgerService(barrier), BudgetLedgerService(barrier))

    async def reserve(index: int):
        return await services[index].reserve_child(
            operation_id=f"reserve-{index}",
            reservation_id=f"reservation-{index}",
            parent_agent_id="root",
            child_agent_id=f"child-{index}",
            creation_request_id=f"create-child-{index}",
            child_limits=limits(
                max_tokens=50, max_children=1, max_depth=2
            ),
        )

    calls = [asyncio.create_task(reserve(index)) for index in range(2)]
    await barrier.both_entered.wait()
    barrier.release.set()
    results = await asyncio.gather(*calls, return_exceptions=True)
    assert sum(isinstance(item, BudgetLedgerConflictError) for item in results) == 1
    assert sum(not isinstance(item, BaseException) for item in results) == 1
    ledger = await BudgetLedgerReader(inner).load()
    assert len(ledger.reservations) == 1
    assert ledger.available("root").max_tokens == 0


async def test_concurrent_identical_operation_returns_one_fact_to_both_callers() -> None:
    inner = InMemoryEventStore()
    barrier = AppendBarrierStore(inner)
    await create_agent(barrier, "root")
    barrier.enabled = True
    services = (BudgetLedgerService(barrier), BudgetLedgerService(barrier))
    calls = [
        asyncio.create_task(
            service.grant_root(
                operation_id="grant-root", agent_id="root", limits=limits()
            )
        )
        for service in services
    ]
    await barrier.both_entered.wait()
    barrier.release.set()
    first, second = await asyncio.gather(*calls)
    assert first == second
    assert len(await inner.read(BUDGET_LEDGER_STREAM)) == 1


class CommitThenFailStore:
    def __init__(self, inner: EventStore, error: BaseException) -> None:
        self.inner = inner
        self.error = error
        self.failed = False

    async def append(self, stream_id, *, expected_seq, events, durability=Durability.SYNC):
        result = await self.inner.append(
            stream_id,
            expected_seq=expected_seq,
            events=events,
            durability=durability,
        )
        if stream_id == BUDGET_LEDGER_STREAM and not self.failed:
            self.failed = True
            raise self.error
        return result

    async def read(self, stream_id, *, from_seq=1):
        return await self.inner.read(stream_id, from_seq=from_seq)

    async def head(self, stream_id):
        return await self.inner.head(stream_id)

    async def list_streams(self, *, prefix=None):
        return await self.inner.list_streams(prefix=prefix)


class CommitThenUnreadableStore(CommitThenFailStore):
    async def read(self, stream_id, *, from_seq=1):
        if self.failed and stream_id == BUDGET_LEDGER_STREAM:
            raise OSError("the reconciliation read cannot answer")
        return await self.inner.read(stream_id, from_seq=from_seq)


async def test_failed_append_reports_committed_and_retry_is_idempotent() -> None:
    inner = InMemoryEventStore()
    await create_agent(inner, "root")
    store = CommitThenFailStore(inner, OSError("failed after durable append"))
    service = BudgetLedgerService(store)
    with pytest.raises(BudgetWriteError) as error:
        await service.grant_root(
            operation_id="grant-root", agent_id="root", limits=limits()
        )
    assert error.value.committed is True
    account = await service.grant_root(
        operation_id="grant-root", agent_id="root", limits=limits()
    )
    assert account.agent_id == "root"
    assert len(await inner.read(BUDGET_LEDGER_STREAM)) == 1


async def test_failed_append_with_unreadable_history_reports_unknown() -> None:
    inner = InMemoryEventStore()
    await create_agent(inner, "root")
    store = CommitThenUnreadableStore(inner, OSError("failed after durable append"))
    with pytest.raises(BudgetWriteError) as error:
        await BudgetLedgerService(store).grant_root(
            operation_id="grant-root", agent_id="root", limits=limits()
        )
    assert error.value.committed is None
    assert len(await inner.read(BUDGET_LEDGER_STREAM)) == 1


async def test_cancelled_append_reconciles_then_rethrows_cancellation() -> None:
    inner = InMemoryEventStore()
    await create_agent(inner, "root")
    store = CommitThenFailStore(inner, asyncio.CancelledError())
    service = BudgetLedgerService(store)
    with pytest.raises(asyncio.CancelledError):
        await service.grant_root(
            operation_id="grant-root", agent_id="root", limits=limits()
        )
    assert (await BudgetLedgerReader(inner).load()).account("root") is not None


async def test_malformed_and_contradictory_history_fails_closed() -> None:
    store = InMemoryEventStore()
    await create_agent(store, "root")
    directory = await AgentRegistrar(store).directory()
    root = root_granted_data(
        operation_id="grant-root", agent_id="root", limits=limits()
    )
    cases = [
        (
            (envelope(1, BUDGET_ROOT_GRANTED, root, stream_id="other"),),
            "budget-stream-unexpected",
        ),
        (
            (envelope(1, BUDGET_ROOT_GRANTED, root, schema_version=99),),
            "budget-schema-version-unsupported",
        ),
        (
            (envelope(2, BUDGET_ROOT_GRANTED, root),),
            "budget-sequence-invalid",
        ),
        (
            (
                envelope(1, BUDGET_ROOT_GRANTED, root),
                envelope(2, BUDGET_ACCOUNT_CLOSED, account_closed_data(
                    operation_id="grant-root", agent_id="root"
                )),
            ),
            "budget-operation-duplicate",
        ),
    ]
    for events, code in cases:
        with pytest.raises(BudgetProtocolError) as error:
            BudgetLedger.rebuild(events, directory)
        assert error.value.code == code
        assert [item.code for item in validate_budget_ledger_events(events, directory)] == [
            code
        ]


@pytest.mark.parametrize(
    ("field", "hostile"),
    [
        ("type", RaisingComparisonString(BUDGET_ROOT_GRANTED)),
        ("stream_id", RaisingComparisonString(BUDGET_LEDGER_STREAM)),
        ("schema_version", RaisingComparisonInteger(BUDGET_SCHEMA_VERSION)),
    ],
)
async def test_hostile_envelope_fields_become_stable_protocol_errors(
    field: str, hostile: object
) -> None:
    store = InMemoryEventStore()
    await create_agent(store, "root")
    directory = await AgentRegistrar(store).directory()
    values = {
        "event_type": BUDGET_ROOT_GRANTED,
        "stream_id": BUDGET_LEDGER_STREAM,
        "schema_version": BUDGET_SCHEMA_VERSION,
    }
    values["event_type" if field == "type" else field] = hostile
    event = envelope(
        1,
        values["event_type"],
        root_granted_data(
            operation_id="grant-root", agent_id="root", limits=limits()
        ),
        stream_id=values["stream_id"],
        schema_version=values["schema_version"],
    )

    with pytest.raises(BudgetProtocolError) as error:
        BudgetLedger.rebuild((event,), directory)
    assert error.value.code == "budget-payload-invalid"
    issues = validate_budget_ledger_events((event,), directory)
    assert len(issues) == 1
    assert issues[0].code == "budget-payload-invalid"


@pytest.mark.parametrize(
    ("field", "hostile"),
    [
        ("type", InterruptingComparisonString(BUDGET_ROOT_GRANTED)),
        ("stream_id", InterruptingComparisonString(BUDGET_LEDGER_STREAM)),
        ("schema_version", InterruptingComparisonInteger(BUDGET_SCHEMA_VERSION)),
    ],
)
async def test_interpreter_interrupts_from_envelope_fields_propagate(
    field: str, hostile: object
) -> None:
    store = InMemoryEventStore()
    await create_agent(store, "root")
    directory = await AgentRegistrar(store).directory()
    values = {
        "event_type": BUDGET_ROOT_GRANTED,
        "stream_id": BUDGET_LEDGER_STREAM,
        "schema_version": BUDGET_SCHEMA_VERSION,
    }
    values["event_type" if field == "type" else field] = hostile
    event = envelope(
        1,
        values["event_type"],
        root_granted_data(
            operation_id="grant-root", agent_id="root", limits=limits()
        ),
        stream_id=values["stream_id"],
        schema_version=values["schema_version"],
    )

    with pytest.raises(KeyboardInterrupt):
        BudgetLedger.rebuild((event,), directory)


async def test_hostile_payload_iteration_is_normalized() -> None:
    store = InMemoryEventStore()
    await create_agent(store, "root")
    directory = await AgentRegistrar(store).directory()
    event = envelope(
        1,
        BUDGET_ROOT_GRANTED,
        RaisingIteration(
            root_granted_data(
                operation_id="grant-root", agent_id="root", limits=limits()
            )
        ),
    )
    with pytest.raises(BudgetProtocolError) as error:
        BudgetLedger.rebuild((event,), directory)
    assert error.value.code == "budget-payload-invalid"


async def test_release_fact_plus_durable_child_is_a_protocol_contradiction() -> None:
    store = InMemoryEventStore()
    await create_agent(store, "root")
    await create_agent(
        store, "child", owner_agent_id="root", request_id="create-child"
    )
    directory = await AgentRegistrar(store).directory()
    events = (
        envelope(
            1,
            BUDGET_ROOT_GRANTED,
            root_granted_data(
                operation_id="grant-root", agent_id="root", limits=limits()
            ),
        ),
        envelope(
            2,
            BUDGET_CHILD_RESERVED,
            child_reserved_data(
                operation_id="reserve-child",
                reservation_id="reservation-child",
                parent_agent_id="root",
                child_agent_id="child",
                creation_request_id="create-child",
                child_limits=limits(max_depth=2),
            ),
        ),
        envelope(
            3,
            BUDGET_RESERVATION_RELEASED,
            {
                "operation_id": "release-child",
                "reservation_id": "reservation-child",
            },
        ),
    )
    with pytest.raises(BudgetProtocolError) as error:
        BudgetLedger.rebuild(events, directory)
    assert error.value.code == "budget-release-after-agent"


async def test_old_agent_budget_history_is_explicitly_unsupported() -> None:
    store = InMemoryEventStore()
    old_payload = {
        "agent_id": "old-agent",
        "session_id": "old-session",
        "request_id": "old-request",
        "preset": "old-preset",
        "workspace_id": "old-workspace",
        "owner_agent_id": None,
        "forked_from_session_id": None,
        "capability_grants": [],
        "budget": {
            "max_tokens": 100_000,
            "max_steps": 50,
            "max_tool_calls": 200,
            "max_wall_seconds": 900.0,
            "max_children": 4,
            "max_depth": 3,
            "max_processes": 4,
        },
        "metadata": {},
    }
    await store.append(
        "agents:directory",
        expected_seq=0,
        events=(PendingEvent(type="agent/created", data=old_payload, schema_version=1),),
    )
    with pytest.raises(AgentDirectoryProtocolError) as error:
        await AgentRegistrar(store).directory()
    assert error.value.code == "agent-budget-history-unsupported"
    assert len(await store.read("agents:directory")) == 1


async def test_agent_spec_cannot_carry_budget_authority() -> None:
    assert "budget" not in AgentSpec.__dataclass_fields__
    assert "children" not in BudgetAmounts.__dataclass_fields__
    assert "budget" not in __import__("traceh.api", fromlist=["__all__"]).__all__
    with pytest.raises(TypeError):
        BudgetLimits()  # type: ignore[call-arg]


async def test_public_builders_reject_non_json_safe_budget_numbers() -> None:
    store = InMemoryEventStore()
    await create_agent(store, "root")
    service = BudgetLedgerService(store)
    with pytest.raises(BudgetInputError):
        await service.grant_root(
            operation_id="grant-root",
            agent_id="root",
            limits=limits(max_tokens=2**53),
        )
    with pytest.raises(BudgetInputError):
        await service.grant_root(
            operation_id="grant-root",
            agent_id="root",
            limits=limits(max_steps=True),
        )
    assert await store.read(BUDGET_LEDGER_STREAM) == ()


async def test_public_builders_normalize_hostile_integer_subclasses() -> None:
    store = InMemoryEventStore()
    await create_agent(store, "root")
    service = BudgetLedgerService(store)

    with pytest.raises(BudgetInputError) as limit_error:
        await service.grant_root(
            operation_id="grant-hostile-limit",
            agent_id="root",
            limits=limits(max_tokens=RaisingOrderInteger(1)),
        )
    assert limit_error.value.code == "budget-limits-invalid"
    assert await store.read(BUDGET_LEDGER_STREAM) == ()

    await service.grant_root(
        operation_id="grant-root",
        agent_id="root",
        limits=limits(),
    )
    with pytest.raises(BudgetInputError) as amount_error:
        await service.admit_usage(
            operation_id="charge-hostile-amount",
            agent_id="root",
            amounts=BudgetAmounts(steps=RaisingOrderInteger(1)),
        )
    assert amount_error.value.code == "budget-amounts-invalid"
    assert len(await store.read(BUDGET_LEDGER_STREAM)) == 1


async def test_manual_charge_cannot_exceed_the_projected_balance() -> None:
    store = InMemoryEventStore()
    service = await granted_root(store)
    await service.admit_usage(
        operation_id="charge-one",
        agent_id="root",
        amounts=BudgetAmounts(steps=20),
    )
    with pytest.raises(BudgetExhaustedError):
        await service.admit_usage(
            operation_id="charge-two",
            agent_id="root",
            amounts=BudgetAmounts(steps=1),
        )
    events = await store.read(BUDGET_LEDGER_STREAM)
    assert [event.type for event in events] == [BUDGET_ROOT_GRANTED, BUDGET_USAGE_CHARGED]


async def test_usage_start_has_one_execution_owner_and_exact_settlement() -> None:
    store = InMemoryEventStore()
    service = await granted_root(store, root_limits=limits(max_tokens=10))
    await service.reserve_usage(
        operation_id="reserve-model",
        reservation_id="model-reservation",
        agent_id="root",
        amounts=BudgetAmounts(tokens=8),
    )

    outcomes = await asyncio.gather(
        service.start_usage(
            operation_id="start-model",
            reservation_id="model-reservation",
        ),
        BudgetLedgerService(store).start_usage(
            operation_id="start-model",
            reservation_id="model-reservation",
        ),
        return_exceptions=True,
    )

    started = [item for item in outcomes if not isinstance(item, BaseException)]
    failures = [item for item in outcomes if isinstance(item, BaseException)]
    assert len(started) == 1
    assert started[0].status is BudgetUsageReservationStatus.STARTED
    assert len(failures) == 1
    assert isinstance(failures[0], BudgetReservationStateError)

    settled = await service.settle_usage(
        operation_id="settle-model",
        reservation_id="model-reservation",
        amounts=BudgetAmounts(tokens=3),
        usage_quality=UsageQuality.EXACT,
    )
    assert settled.status is BudgetUsageReservationStatus.SETTLED
    assert settled.settled_amounts == BudgetAmounts(tokens=3)
    account = (await service.ledger()).account("root")
    assert account is not None
    assert account.reserved.tokens == 0
    assert account.charged.tokens == 3


async def test_usage_settlement_is_validated_before_append() -> None:
    store = InMemoryEventStore()
    service = await granted_root(store, root_limits=limits(max_tokens=10))
    await service.reserve_usage(
        operation_id="reserve-model",
        reservation_id="model-reservation",
        agent_id="root",
        amounts=BudgetAmounts(tokens=5),
    )
    await service.start_usage(
        operation_id="start-model",
        reservation_id="model-reservation",
    )
    head = await store.head(BUDGET_LEDGER_STREAM)

    with pytest.raises(BudgetInputError) as wrong_dimension:
        await service.settle_usage(
            operation_id="settle-wall",
            reservation_id="model-reservation",
            amounts=BudgetAmounts(wall_milliseconds=1),
            usage_quality=None,
        )
    assert wrong_dimension.value.code == "budget-usage-settlement-invalid"
    with pytest.raises(BudgetInputError) as overage:
        await service.settle_usage(
            operation_id="settle-overage",
            reservation_id="model-reservation",
            amounts=BudgetAmounts(tokens=6),
            usage_quality=UsageQuality.EXACT,
        )
    assert overage.value.code == "budget-usage-settlement-invalid"
    assert await store.head(BUDGET_LEDGER_STREAM) == head


async def test_only_pending_usage_can_be_released() -> None:
    store = InMemoryEventStore()
    service = await granted_root(store)
    pending = await service.reserve_usage(
        operation_id="reserve-unused",
        reservation_id="unused-reservation",
        agent_id="root",
        amounts=BudgetAmounts(tokens=2),
    )
    assert pending.status is BudgetUsageReservationStatus.PENDING
    released = await service.release_usage(
        operation_id="release-unused",
        reservation_id="unused-reservation",
    )
    assert released.status is BudgetUsageReservationStatus.RELEASED

    await service.reserve_usage(
        operation_id="reserve-started",
        reservation_id="started-reservation",
        agent_id="root",
        amounts=BudgetAmounts(wall_milliseconds=20),
    )
    await service.start_usage(
        operation_id="start-wall",
        reservation_id="started-reservation",
    )
    with pytest.raises(BudgetReservationStateError):
        await service.release_usage(
            operation_id="release-started",
            reservation_id="started-reservation",
        )


async def test_replay_rejects_settlement_above_the_reserved_amount() -> None:
    store = InMemoryEventStore()
    service = await granted_root(store, root_limits=limits(max_tokens=10))
    await service.reserve_usage(
        operation_id="reserve-model",
        reservation_id="model-reservation",
        agent_id="root",
        amounts=BudgetAmounts(tokens=5),
    )
    await service.start_usage(
        operation_id="start-model",
        reservation_id="model-reservation",
    )
    head = await store.head(BUDGET_LEDGER_STREAM)
    await store.append(
        BUDGET_LEDGER_STREAM,
        expected_seq=head,
        events=(
            PendingEvent(
                type=BUDGET_USAGE_SETTLED,
                data=usage_settled_data(
                    operation_id="settle-overage",
                    reservation_id="model-reservation",
                    amounts=BudgetAmounts(tokens=6),
                    usage_quality=UsageQuality.EXACT,
                ),
            ),
        ),
    )

    with pytest.raises(BudgetProtocolError) as error:
        await BudgetLedgerReader(store).load()
    assert error.value.code == "budget-usage-settlement-invalid"
