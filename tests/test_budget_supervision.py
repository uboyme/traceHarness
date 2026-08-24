"""v0.7-B child-grant and process-slot contracts on the real Supervisor path."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest

from traceh.agents import AgentDirectoryReader, AgentRegistrar
from traceh.api.agents import AgentRecord, AgentSpec
from traceh.api.budgets import BudgetLimits, BudgetReservationStatus
from traceh.api.turns import TurnInput
from traceh.budgets import (
    BudgetDirectoryMismatchError,
    BudgetedActivationFactory,
    BudgetedAgentSupervisor,
    BudgetExhaustedError,
    BudgetLedgerService,
    BudgetReservationStateError,
    ProcessSlotAuthority,
)
from traceh.budgets.events import BUDGET_CHILD_RESERVED
from traceh.runtime.agent_loop import TurnResult
from traceh.session.event_store import Durability, EventStore, InMemoryEventStore
from traceh.session.service import SessionService
from traceh.supervision import ProcessAgentSupervisor

pytestmark = pytest.mark.asyncio


def limits(**overrides: int | None) -> BudgetLimits:
    values: dict[str, int | None] = {
        "max_tokens": 100,
        "max_steps": 10,
        "max_tool_calls": 10,
        "max_wall_milliseconds": 10_000,
        "max_children": 3,
        "max_depth": 3,
        "max_processes": 3,
    }
    values.update(overrides)
    return BudgetLimits(**values)


@dataclass(frozen=True, slots=True)
class FixedChildPolicy:
    child_limits: BudgetLimits

    def limits_for_child(
        self, *, parent: AgentRecord, child: AgentSpec
    ) -> BudgetLimits:
        del parent, child
        return self.child_limits


class StubExecution:
    def __init__(
        self,
        store: EventStore,
        session_id: str,
        *,
        fail_dispose: bool = False,
    ) -> None:
        self._store = store
        self._session_id = session_id
        self.fail_dispose = fail_dispose
        self.dispose_calls = 0

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def event_store(self) -> EventStore:
        return self._store

    async def run_turn(self, turn_input: TurnInput) -> TurnResult:
        raise AssertionError(f"unexpected Turn: {turn_input.message_id}")

    async def cancel_turn(self, *, reason: str) -> bool:
        del reason
        return False

    async def dispose(self) -> None:
        self.dispose_calls += 1
        if self.fail_dispose:
            raise RuntimeError("execution cleanup failed")


class StubFactory:
    def __init__(
        self,
        store: EventStore,
        workspace: Path,
        *,
        fail_provision: bool = False,
        fail_dispose: bool = False,
    ) -> None:
        self.store = store
        self.workspace = workspace
        self.fail_provision = fail_provision
        self.fail_dispose = fail_dispose
        self.executions: dict[str, StubExecution] = {}
        self.provision_calls: list[str] = []
        self.provision_entered: asyncio.Event | None = None
        self.provision_release: asyncio.Event | None = None

    async def provision(
        self,
        spec: AgentSpec,
        *,
        agent_id: str,
        session_id: str | None,
    ) -> StubExecution:
        del spec
        self.provision_calls.append(agent_id)
        if self.provision_entered is not None:
            self.provision_entered.set()
        if self.provision_release is not None:
            await self.provision_release.wait()
        if self.fail_provision:
            raise RuntimeError("provision failed")
        assigned_session = session_id or f"session-{agent_id}"
        await SessionService(self.store).create_session(
            self.workspace,
            session_id=assigned_session,
        )
        execution = StubExecution(
            self.store,
            assigned_session,
            fail_dispose=self.fail_dispose,
        )
        self.executions[agent_id] = execution
        return execution


class CommitThenWaitStore(InMemoryEventStore):
    """Commit one selected fact, then hold its append return behind a Gate."""

    def __init__(self, event_type: str) -> None:
        super().__init__()
        self.event_type = event_type
        self.committed = asyncio.Event()
        self.release = asyncio.Event()

    async def append(
        self,
        stream_id,
        *,
        expected_seq,
        events,
        durability=Durability.SYNC,
    ):
        appended = await super().append(
            stream_id,
            expected_seq=expected_seq,
            events=events,
            durability=durability,
        )
        if any(event.type == self.event_type for event in events):
            self.committed.set()
            await self.release.wait()
        return appended

    async def activate(self, record: AgentRecord) -> StubExecution:
        execution = StubExecution(
            self.store,
            record.session_id,
            fail_dispose=self.fail_dispose,
        )
        self.executions[record.agent_id] = execution
        return execution


async def managed_supervisor(
    tmp_path: Path,
    *,
    root_limits: BudgetLimits,
    child_limits: BudgetLimits,
) -> tuple[
    BudgetedAgentSupervisor,
    BudgetLedgerService,
    ProcessSlotAuthority,
]:
    store = InMemoryEventStore()
    service = BudgetLedgerService(store)
    slots = ProcessSlotAuthority(service)
    factory = BudgetedActivationFactory(
        StubFactory(store, tmp_path),
        slots,
    )
    inner = ProcessAgentSupervisor(store=store, factory=factory)
    supervisor = BudgetedAgentSupervisor(
        inner,
        service,
        child_budget_policy=FixedChildPolicy(child_limits),
    )
    await supervisor.create(
        AgentSpec(preset="root", workspace_id="workspace"),
        request_id="create-root",
        agent_id="agent-root",
        session_id="session-root",
    )
    await service.grant_root(
        operation_id="grant-root",
        agent_id="agent-root",
        limits=root_limits,
    )
    return supervisor, service, slots


async def test_managed_child_grant_commits_once_and_retry_is_idempotent(
    tmp_path: Path,
) -> None:
    supervisor, service, slots = await managed_supervisor(
        tmp_path,
        root_limits=limits(),
        child_limits=limits(
            max_tokens=20,
            max_steps=3,
            max_tool_calls=2,
            max_wall_milliseconds=2_000,
            max_children=0,
            max_depth=2,
            max_processes=1,
        ),
    )
    child_spec = AgentSpec(
        preset="child",
        workspace_id="workspace",
        owner_agent_id="agent-root",
    )

    first = await supervisor.create(child_spec, request_id="create-child")
    second = await supervisor.create(child_spec, request_id="create-child")

    assert second.agent_id == first.agent_id
    ledger = await service.ledger()
    assert len(ledger.reservations) == 1
    assert ledger.reservations[0].status is BudgetReservationStatus.COMMITTED
    assert ledger.account(first.agent_id) is not None
    assert ledger.account("agent-root").reserved_children == 1
    assert await slots.held("agent-root") == 1
    await supervisor.aclose()
    assert await slots.held("agent-root") == 0


async def test_concurrent_child_grants_cannot_exceed_cumulative_capacity(
    tmp_path: Path,
) -> None:
    supervisor, service, _ = await managed_supervisor(
        tmp_path,
        root_limits=limits(max_children=1),
        child_limits=limits(
            max_tokens=10,
            max_steps=2,
            max_tool_calls=2,
            max_wall_milliseconds=1_000,
            max_children=0,
            max_depth=2,
            max_processes=1,
        ),
    )

    async def create(request_id: str):
        return await supervisor.create(
            AgentSpec(
                preset="child",
                workspace_id="workspace",
                owner_agent_id="agent-root",
            ),
            request_id=request_id,
        )

    outcomes = await asyncio.gather(
        create("create-a"),
        create("create-b"),
        return_exceptions=True,
    )

    assert sum(not isinstance(item, BaseException) for item in outcomes) == 1
    errors = [item for item in outcomes if isinstance(item, BaseException)]
    assert len(errors) == 1
    assert isinstance(errors[0], BudgetExhaustedError)
    assert errors[0].dimension == "max_children"
    assert (await service.ledger()).account("agent-root").reserved_children == 1
    await supervisor.aclose()


async def test_process_slot_failure_releases_child_hold_and_slot_is_reusable(
    tmp_path: Path,
) -> None:
    supervisor, service, slots = await managed_supervisor(
        tmp_path,
        root_limits=limits(max_processes=1),
        child_limits=limits(
            max_tokens=10,
            max_steps=2,
            max_tool_calls=2,
            max_wall_milliseconds=1_000,
            max_children=0,
            max_depth=2,
            max_processes=0,
        ),
    )
    child_spec = AgentSpec(
        preset="child",
        workspace_id="workspace",
        owner_agent_id="agent-root",
    )
    first = await supervisor.create(child_spec, request_id="create-first")
    assert await slots.held("agent-root") == 1

    with pytest.raises(BudgetExhaustedError) as exhausted:
        await supervisor.create(child_spec, request_id="create-blocked")
    assert exhausted.value.dimension == "max_processes"
    released = [
        reservation
        for reservation in (await service.ledger()).reservations
        if reservation.creation_request_id == "create-blocked"
    ]
    assert len(released) == 1
    assert released[0].status is BudgetReservationStatus.RELEASED

    await supervisor.dispose(first.agent_id)
    assert await slots.held("agent-root") == 0
    await supervisor.create(child_spec, request_id="create-after-release")
    assert await slots.held("agent-root") == 1
    await supervisor.aclose()
    assert await slots.held("agent-root") == 0


async def test_released_child_reservation_cannot_reenter_creation(
    tmp_path: Path,
) -> None:
    store = InMemoryEventStore()
    service = BudgetLedgerService(store)
    slots = ProcessSlotAuthority(service)
    raw_factory = StubFactory(store, tmp_path)
    inner = ProcessAgentSupervisor(
        store=store,
        factory=BudgetedActivationFactory(raw_factory, slots),
    )
    supervisor = BudgetedAgentSupervisor(
        inner,
        service,
        child_budget_policy=FixedChildPolicy(
            limits(
                max_tokens=10,
                max_steps=2,
                max_tool_calls=2,
                max_wall_milliseconds=1_000,
                max_children=0,
                max_depth=2,
                max_processes=1,
            )
        ),
    )
    await supervisor.create(
        AgentSpec(preset="root", workspace_id="workspace"),
        request_id="create-root",
        agent_id="agent-root",
        session_id="session-root",
    )
    await service.grant_root(
        operation_id="grant-root",
        agent_id="agent-root",
        limits=limits(),
    )
    child_spec = AgentSpec(
        preset="child",
        workspace_id="workspace",
        owner_agent_id="agent-root",
    )
    raw_factory.fail_provision = True

    with pytest.raises(RuntimeError, match="provision failed"):
        await supervisor.create(child_spec, request_id="create-released")

    ledger = await service.ledger()
    assert len(ledger.reservations) == 1
    released = ledger.reservations[0]
    assert released.status is BudgetReservationStatus.RELEASED
    assert (await AgentDirectoryReader(store).load()).get(
        released.child_agent_id
    ) is None
    calls_after_release = tuple(raw_factory.provision_calls)
    raw_factory.fail_provision = False

    with pytest.raises(BudgetReservationStateError):
        await supervisor.create(child_spec, request_id="create-released")

    assert tuple(raw_factory.provision_calls) == calls_after_release
    directory = await AgentDirectoryReader(store).load()
    assert directory.get(released.child_agent_id) is None
    assert directory.for_request("create-released") is None
    rebuilt = await service.ledger()
    assert rebuilt.reservation(released.reservation_id) == released
    parent = rebuilt.account("agent-root")
    assert parent is not None
    assert parent.reserved_children == 0
    assert parent.reserved.tokens == 0
    assert parent.delegated.tokens == 0
    await supervisor.aclose()


async def test_cancel_after_child_reserve_commit_releases_before_provision(
    tmp_path: Path,
) -> None:
    store = CommitThenWaitStore(BUDGET_CHILD_RESERVED)
    service = BudgetLedgerService(store)
    slots = ProcessSlotAuthority(service)
    raw_factory = StubFactory(store, tmp_path)
    inner = ProcessAgentSupervisor(
        store=store,
        factory=BudgetedActivationFactory(raw_factory, slots),
    )
    supervisor = BudgetedAgentSupervisor(
        inner,
        service,
        child_budget_policy=FixedChildPolicy(
            limits(
                max_tokens=10,
                max_steps=2,
                max_tool_calls=2,
                max_wall_milliseconds=1_000,
                max_children=0,
                max_depth=2,
                max_processes=1,
            )
        ),
    )
    await supervisor.create(
        AgentSpec(preset="root", workspace_id="workspace"),
        request_id="create-root",
        agent_id="agent-root",
        session_id="session-root",
    )
    await service.grant_root(
        operation_id="grant-root",
        agent_id="agent-root",
        limits=limits(),
    )
    calls_before = tuple(raw_factory.provision_calls)
    creating = asyncio.create_task(
        supervisor.create(
            AgentSpec(
                preset="child",
                workspace_id="workspace",
                owner_agent_id="agent-root",
            ),
            request_id="create-cancelled-before-provision",
        )
    )
    await store.committed.wait()

    creating.cancel()
    await asyncio.sleep(0)
    creating.cancel()
    await asyncio.sleep(0)
    store.release.set()

    with pytest.raises(asyncio.CancelledError):
        await creating
    assert tuple(raw_factory.provision_calls) == calls_before
    ledger = await service.ledger()
    assert len(ledger.reservations) == 1
    assert ledger.reservations[0].status is BudgetReservationStatus.RELEASED
    account = ledger.account("agent-root")
    assert account is not None
    assert account.reserved_children == 0
    assert account.reserved.tokens == 0
    assert account.delegated.tokens == 0
    await supervisor.aclose()


async def test_activation_failure_and_cleanup_failure_never_leak_slots(
    tmp_path: Path,
) -> None:
    store = InMemoryEventStore()
    sessions = SessionService(store)
    await sessions.create_session(tmp_path, session_id="session-root")
    await AgentRegistrar(store).create_agent(
        AgentSpec(preset="root", workspace_id="workspace"),
        request_id="create-root",
        agent_id="agent-root",
        session_id="session-root",
    )
    service = BudgetLedgerService(store)
    await service.grant_root(
        operation_id="grant-root",
        agent_id="agent-root",
        limits=limits(max_processes=1),
    )
    slots = ProcessSlotAuthority(service)
    child_spec = AgentSpec(
        preset="child",
        workspace_id="workspace",
        owner_agent_id="agent-root",
    )

    failing_factory = BudgetedActivationFactory(
        StubFactory(store, tmp_path, fail_provision=True),
        slots,
    )
    with pytest.raises(RuntimeError, match="provision failed"):
        await failing_factory.provision(
            child_spec,
            agent_id="agent-failed",
            session_id="session-failed",
        )
    assert await slots.held("agent-root") == 0

    cleanup_factory = BudgetedActivationFactory(
        StubFactory(store, tmp_path, fail_dispose=True),
        slots,
    )
    execution = await cleanup_factory.provision(
        child_spec,
        agent_id="agent-cleanup",
        session_id="session-cleanup",
    )
    assert await slots.held("agent-root") == 1
    with pytest.raises(RuntimeError, match="execution cleanup failed"):
        await execution.dispose()
    assert await slots.held("agent-root") == 0


async def test_activation_store_mismatch_cleans_execution_and_slot(
    tmp_path: Path,
) -> None:
    authority_store = InMemoryEventStore()
    await SessionService(authority_store).create_session(
        tmp_path,
        session_id="session-root",
    )
    await AgentRegistrar(authority_store).create_agent(
        AgentSpec(preset="root", workspace_id="workspace"),
        request_id="create-root",
        agent_id="agent-root",
        session_id="session-root",
    )
    service = BudgetLedgerService(authority_store)
    await service.grant_root(
        operation_id="grant-root",
        agent_id="agent-root",
        limits=limits(max_processes=1),
    )
    slots = ProcessSlotAuthority(service)
    foreign_factory = StubFactory(InMemoryEventStore(), tmp_path)
    factory = BudgetedActivationFactory(foreign_factory, slots)

    with pytest.raises(BudgetDirectoryMismatchError):
        await factory.provision(
            AgentSpec(
                preset="child",
                workspace_id="workspace",
                owner_agent_id="agent-root",
            ),
            agent_id="agent-child",
            session_id="session-child",
        )

    assert await slots.held("agent-root") == 0
    assert foreign_factory.executions["agent-child"].dispose_calls == 1


async def test_close_waits_for_child_grant_reconciliation(
    tmp_path: Path,
) -> None:
    store = InMemoryEventStore()
    service = BudgetLedgerService(store)
    slots = ProcessSlotAuthority(service)
    raw_factory = StubFactory(store, tmp_path)
    inner = ProcessAgentSupervisor(
        store=store,
        factory=BudgetedActivationFactory(raw_factory, slots),
    )
    supervisor = BudgetedAgentSupervisor(
        inner,
        service,
        child_budget_policy=FixedChildPolicy(
            limits(
                max_tokens=10,
                max_steps=2,
                max_tool_calls=2,
                max_wall_milliseconds=1_000,
                max_children=0,
                max_depth=2,
                max_processes=1,
            )
        ),
    )
    await supervisor.create(
        AgentSpec(preset="root", workspace_id="workspace"),
        request_id="create-root",
        agent_id="agent-root",
        session_id="session-root",
    )
    await service.grant_root(
        operation_id="grant-root",
        agent_id="agent-root",
        limits=limits(),
    )
    raw_factory.provision_entered = asyncio.Event()
    raw_factory.provision_release = asyncio.Event()
    creating = asyncio.create_task(
        supervisor.create(
            AgentSpec(
                preset="child",
                workspace_id="workspace",
                owner_agent_id="agent-root",
            ),
            request_id="create-child",
        )
    )
    await raw_factory.provision_entered.wait()
    closing = asyncio.create_task(supervisor.aclose())
    await asyncio.sleep(0)
    assert not closing.done()

    raw_factory.provision_release.set()
    child = await creating
    await closing

    reservation = (await service.ledger()).reservations[0]
    assert reservation.status is BudgetReservationStatus.COMMITTED
    assert reservation.child_agent_id == child.agent_id
    assert await slots.held("agent-root") == 0
