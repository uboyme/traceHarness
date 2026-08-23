"""Stage D contracts for lifecycle ownership and child-first disposal."""

from __future__ import annotations

import asyncio

import pytest
from supervision_fixtures import RuntimeFactory

from traceh.agents import (
    AGENT_DIRECTORY_STREAM,
    AgentDirectory,
    AgentDirectoryProtocolError,
    AgentOwnerNotFoundError,
    AgentRegistrar,
)
from traceh.api.agents import AgentRecord, AgentSpec
from traceh.api.events import PendingEvent
from traceh.session.event_store import Durability, InMemoryEventStore
from traceh.supervision import (
    AgentLifecycleCoordinator,
    AgentOwnerNotActiveError,
    AgentOwnershipGraph,
    AgentOwnershipGraphError,
    ProcessAgentSupervisor,
)

pytestmark = pytest.mark.asyncio


def _record(
    agent_id: str,
    *,
    owner_agent_id: str | None = None,
    forked_from_session_id: str | None = None,
    seq: int,
) -> AgentRecord:
    return AgentRecord(
        agent_id=agent_id,
        session_id=f"session-{agent_id}",
        request_id=f"request-{agent_id}",
        preset="lifecycle-preset",
        workspace_id="lifecycle-workspace",
        owner_agent_id=owner_agent_id,
        forked_from_session_id=forked_from_session_id,
        capability_grants=(),
        metadata={},
        created_seq=seq,
    )


def _spec(*, owner_agent_id: str | None = None) -> AgentSpec:
    return AgentSpec(
        preset="lifecycle-preset",
        workspace_id="lifecycle-workspace",
        owner_agent_id=owner_agent_id,
    )


def _failure_leaves(error: BaseException) -> tuple[BaseException, ...]:
    if isinstance(error, BaseExceptionGroup):
        return tuple(
            leaf
            for nested in error.exceptions
            for leaf in _failure_leaves(nested)
        )
    return (error,)


class _ObservedExecution:
    def __init__(self, inner, agent_id: str, factory: _LifecycleFactory) -> None:
        self._inner = inner
        self._agent_id = agent_id
        self._factory = factory

    @property
    def session_id(self):
        return self._inner.session_id

    @property
    def event_store(self):
        return self._inner.event_store

    async def run_turn(self, turn_input):
        return await self._inner.run_turn(turn_input)

    async def cancel_turn(self, *, reason: str):
        return await self._inner.cancel_turn(reason=reason)

    async def dispose(self) -> None:
        self._factory.dispose_entered.setdefault(self._agent_id, asyncio.Event()).set()
        gate = self._factory.dispose_gates.get(self._agent_id)
        if gate is not None:
            await gate.wait()
        self._factory.dispose_order.append(self._agent_id)
        self._factory.dispose_counts[self._agent_id] = (
            self._factory.dispose_counts.get(self._agent_id, 0) + 1
        )
        await self._inner.dispose()
        error = self._factory.dispose_errors.get(self._agent_id)
        if error is not None:
            raise error


class _LifecycleFactory(RuntimeFactory):
    def __init__(self, store, root) -> None:
        super().__init__(store, root)
        self.dispose_order: list[str] = []
        self.dispose_counts: dict[str, int] = {}
        self.dispose_gates: dict[str, asyncio.Event] = {}
        self.dispose_entered: dict[str, asyncio.Event] = {}
        self.dispose_errors: dict[str, BaseException] = {}

    async def provision(self, spec, *, agent_id, session_id):
        execution = await super().provision(
            spec, agent_id=agent_id, session_id=session_id
        )
        wrapped = _ObservedExecution(execution, agent_id, self)
        self.executions[-1] = wrapped
        return wrapped

    async def activate(self, record):
        execution = await super().activate(record)
        wrapped = _ObservedExecution(execution, record.agent_id, self)
        self.executions[-1] = wrapped
        return wrapped


@pytest.fixture
async def world(tmp_path):
    store = InMemoryEventStore()
    factory = _LifecycleFactory(store, tmp_path)
    supervisor = ProcessAgentSupervisor(store=store, factory=factory)
    try:
        yield store, factory, supervisor
    finally:
        try:
            await supervisor.aclose()
        except BaseException:
            # Failure-path tests assert the exact cleanup result themselves.
            pass


async def _create(
    supervisor: ProcessAgentSupervisor,
    agent_id: str,
    *,
    owner_agent_id: str | None = None,
):
    return await supervisor.create(
        _spec(owner_agent_id=owner_agent_id),
        request_id=f"request-{agent_id}",
        agent_id=agent_id,
    )


async def test_graph_is_child_first_and_ignores_history_lineage() -> None:
    directory = AgentDirectory(
        (
            _record("root", forked_from_session_id="unrelated-history", seq=1),
            _record("child-a", owner_agent_id="root", seq=2),
            _record("grandchild", owner_agent_id="child-a", seq=3),
            _record(
                "child-b",
                owner_agent_id="root",
                forked_from_session_id="session-grandchild",
                seq=4,
            ),
            _record("other-root", owner_agent_id=None, seq=5),
        ),
        head_seq=5,
    )
    graph = AgentOwnershipGraph(directory)

    assert graph.lineage("grandchild") == ("root", "child-a", "grandchild")
    assert graph.subtree_postorder("root") == (
        "grandchild",
        "child-a",
        "child-b",
        "root",
    )
    assert graph.forest_postorder() == (
        "grandchild",
        "child-a",
        "child-b",
        "root",
        "other-root",
    )


@pytest.mark.parametrize(
    ("records", "code"),
    (
        ((_record("same", seq=1), _record("same", seq=2)), "agent-owner-id-duplicate"),
        ((_record("self", owner_agent_id="self", seq=1),), "agent-owner-self"),
        ((_record("child", owner_agent_id="missing", seq=1),), "agent-owner-unknown"),
        (
            (
                _record("left", owner_agent_id="right", seq=1),
                _record("right", owner_agent_id="left", seq=2),
            ),
            "agent-owner-cycle",
        ),
    ),
)
async def test_graph_rejects_contradictory_ownership(records, code) -> None:
    with pytest.raises(AgentOwnershipGraphError) as caught:
        AgentOwnershipGraph(AgentDirectory(records, head_seq=len(records)))

    assert caught.value.code == code


async def test_child_creation_requires_a_durable_and_live_owner(world) -> None:
    store, factory, supervisor = world

    with pytest.raises(AgentOwnerNotFoundError):
        await _create(supervisor, "unknown-child", owner_agent_id="missing")
    assert factory.provisions == 0

    await _create(supervisor, "root")
    await supervisor.dispose("root")
    before = factory.provisions
    with pytest.raises(AgentOwnerNotActiveError):
        await _create(supervisor, "inactive-child", owner_agent_id="root")

    assert factory.provisions == before
    assert (await supervisor.registrar.directory()).get("inactive-child") is None


async def test_parent_disposal_is_child_first(world) -> None:
    store, factory, supervisor = world
    await _create(supervisor, "root")
    await _create(supervisor, "child-a", owner_agent_id="root")
    await _create(supervisor, "grandchild", owner_agent_id="child-a")
    await _create(supervisor, "child-b", owner_agent_id="root")

    await supervisor.dispose("root")

    assert factory.dispose_order == ["grandchild", "child-a", "child-b", "root"]
    assert factory.dispose_counts == {
        "grandchild": 1,
        "child-a": 1,
        "child-b": 1,
        "root": 1,
    }


async def test_disposing_one_child_does_not_touch_owner_or_sibling(world) -> None:
    store, factory, supervisor = world
    root = await _create(supervisor, "root")
    await _create(supervisor, "child-a", owner_agent_id="root")
    sibling = await _create(supervisor, "child-b", owner_agent_id="root")
    await _create(supervisor, "grandchild", owner_agent_id="child-a")

    await supervisor.dispose("child-a")

    assert factory.dispose_order == ["grandchild", "child-a"]
    await supervisor.wait_idle(root.agent_id)
    await supervisor.wait_idle(sibling.agent_id)


async def test_parent_disposal_cancels_an_inflight_child_create(world) -> None:
    store, factory, supervisor = world
    await _create(supervisor, "root")
    factory.provision_gate = asyncio.Event()
    child = asyncio.create_task(
        _create(supervisor, "pending-child", owner_agent_id="root")
    )
    await factory.provision_entered.wait()

    await supervisor.dispose("root")

    assert child.done(), "subtree disposal returned while child creation survived"
    with pytest.raises(asyncio.CancelledError):
        await child
    assert (await supervisor.registrar.directory()).get("pending-child") is None
    assert factory.dispose_order == ["root"]


async def test_child_disposal_cancels_an_unpinned_request_retry(
    world, monkeypatch
) -> None:
    store, factory, supervisor = world
    await _create(supervisor, "root")
    child = await supervisor.create(
        _spec(owner_agent_id="root"), request_id="request-unpinned-child"
    )

    directory_entered = asyncio.Event()
    directory_release = asyncio.Event()
    original_directory = AgentRegistrar.directory

    async def gated_directory(registrar):
        task = asyncio.current_task()
        if (
            registrar is supervisor.registrar
            and task is not None
            and task.get_name() == "traceh-agent-create-request-unpinned-child"
        ):
            directory_entered.set()
            await directory_release.wait()
        return await original_directory(registrar)

    monkeypatch.setattr(AgentRegistrar, "directory", gated_directory)
    retry = asyncio.create_task(
        supervisor.create(
            _spec(owner_agent_id="root"), request_id="request-unpinned-child"
        )
    )
    await asyncio.wait_for(directory_entered.wait(), 30)

    cancel_entered = asyncio.Event()
    original_cancel_and_join = ProcessAgentSupervisor._cancel_and_join

    async def observed_cancel_and_join(task):
        if task.get_name() == "traceh-agent-create-request-unpinned-child":
            cancel_entered.set()
        return await original_cancel_and_join(task)

    monkeypatch.setattr(
        ProcessAgentSupervisor,
        "_cancel_and_join",
        staticmethod(observed_cancel_and_join),
    )
    disposing = asyncio.create_task(supervisor.dispose(child.agent_id))
    try:
        await asyncio.wait_for(cancel_entered.wait(), 30)
        await disposing
        assert retry.done(), "child disposal returned while its request retry survived"
        with pytest.raises(asyncio.CancelledError):
            await retry
    finally:
        directory_release.set()
        if not disposing.done():
            await disposing
        if not retry.done():
            retry.cancel()
            with pytest.raises(asyncio.CancelledError):
                await retry

    assert factory.dispose_counts == {child.agent_id: 1}


async def test_repeated_cancellation_cannot_escape_child_first_cleanup(world) -> None:
    store, factory, supervisor = world
    await _create(supervisor, "root")
    await _create(supervisor, "child", owner_agent_id="root")
    gate = asyncio.Event()
    factory.dispose_gates["child"] = gate

    disposing = asyncio.create_task(supervisor.dispose("root"))
    await factory.dispose_entered.setdefault("child", asyncio.Event()).wait()
    try:
        for _ in range(3):
            disposing.cancel()
            await asyncio.sleep(0)
            assert not disposing.done()
            assert "root" not in factory.dispose_order
    finally:
        gate.set()

    with pytest.raises(asyncio.CancelledError):
        await disposing
    assert factory.dispose_order == ["child", "root"]
    assert factory.dispose_counts == {"child": 1, "root": 1}


async def test_one_cleanup_failure_does_not_skip_siblings_or_owner(world) -> None:
    store, factory, supervisor = world
    await _create(supervisor, "root")
    await _create(supervisor, "child-a", owner_agent_id="root")
    await _create(supervisor, "child-b", owner_agent_id="root")
    factory.dispose_errors["child-a"] = RuntimeError("fixture cleanup failure")

    with pytest.raises(BaseExceptionGroup) as caught:
        await supervisor.dispose("root")

    assert factory.dispose_order == ["child-a", "child-b", "root"]
    assert factory.dispose_counts == {"child-a": 1, "child-b": 1, "root": 1}
    assert any(isinstance(item, BaseExceptionGroup) for item in caught.value.exceptions)


async def test_overlapping_parent_and_child_disposals_cleanup_once(world) -> None:
    store, factory, supervisor = world
    await _create(supervisor, "root")
    await _create(supervisor, "child", owner_agent_id="root")
    await _create(supervisor, "grandchild", owner_agent_id="child")

    await asyncio.gather(supervisor.dispose("root"), supervisor.dispose("child"))

    assert factory.dispose_counts == {"grandchild": 1, "child": 1, "root": 1}
    assert factory.dispose_order.index("grandchild") < factory.dispose_order.index("child")
    assert factory.dispose_order.index("child") < factory.dispose_order.index("root")


async def test_unrelated_trees_do_not_share_one_disposal_gate(world) -> None:
    store, factory, supervisor = world
    await _create(supervisor, "root-a")
    await _create(supervisor, "child-a", owner_agent_id="root-a")
    await _create(supervisor, "root-b")
    await _create(supervisor, "child-b", owner_agent_id="root-b")
    gate = asyncio.Event()
    factory.dispose_gates["child-a"] = gate

    first = asyncio.create_task(supervisor.dispose("root-a"))
    await factory.dispose_entered.setdefault("child-a", asyncio.Event()).wait()
    try:
        await supervisor.dispose("root-b")
        assert factory.dispose_order == ["child-b", "root-b"]
        assert not first.done()
    finally:
        gate.set()
    await first

    assert factory.dispose_order == ["child-b", "root-b", "child-a", "root-a"]


async def test_owned_child_resumes_only_after_its_owner(world) -> None:
    store, factory, supervisor = world
    root = await _create(supervisor, "root")
    child = await _create(supervisor, "child", owner_agent_id="root")
    await supervisor.dispose("root")

    with pytest.raises(AgentOwnerNotActiveError):
        await supervisor.resume(child.session_id)
    resumed_root = await supervisor.resume(root.session_id)
    resumed_child = await supervisor.resume(child.session_id)

    assert resumed_root.agent_id == "root"
    assert resumed_child.agent_id == "child"


async def test_aclose_disposes_each_forest_child_first(world) -> None:
    store, factory, supervisor = world
    await _create(supervisor, "root-a")
    await _create(supervisor, "child-a", owner_agent_id="root-a")
    await _create(supervisor, "root-b")
    await _create(supervisor, "child-b", owner_agent_id="root-b")

    await supervisor.aclose()

    assert factory.dispose_order == ["child-a", "root-a", "child-b", "root-b"]
    assert factory.dispose_counts == {
        "child-a": 1,
        "root-a": 1,
        "child-b": 1,
        "root-b": 1,
    }


async def test_aclose_releases_live_activations_when_directory_is_malformed(world) -> None:
    store, factory, supervisor = world
    await _create(supervisor, "root")
    head = await store.head(AGENT_DIRECTORY_STREAM)
    await store.append(
        AGENT_DIRECTORY_STREAM,
        expected_seq=head,
        events=(PendingEvent(type="agent/unsupported", data={}, schema_version=1),),
        durability=Durability.SYNC,
    )

    with pytest.raises(BaseExceptionGroup) as caught:
        await supervisor.aclose()

    assert any(
        isinstance(error, AgentDirectoryProtocolError)
        for error in _failure_leaves(caught.value)
    )
    assert factory.dispose_order == ["root"]
    assert factory.dispose_counts == {"root": 1}
    with pytest.raises(BaseExceptionGroup):
        await supervisor.aclose()
    assert factory.dispose_counts == {"root": 1}


async def test_aclose_reports_an_inflight_subtree_failure_once(world, monkeypatch) -> None:
    store, factory, supervisor = world
    await _create(supervisor, "root")
    await _create(supervisor, "child", owner_agent_id="root")
    gate = asyncio.Event()
    factory.dispose_gates["child"] = gate
    cleanup_error = RuntimeError("fixture cleanup failure")
    factory.dispose_errors["child"] = cleanup_error

    disposing = asyncio.create_task(supervisor.dispose("root"))
    await factory.dispose_entered.setdefault("child", asyncio.Event()).wait()

    close_quiescent = asyncio.Event()
    close_continue = asyncio.Event()
    original_wait_quiescent = AgentLifecycleCoordinator.wait_quiescent

    async def observed_wait_quiescent(coordinator):
        await original_wait_quiescent(coordinator)
        close_quiescent.set()
        await close_continue.wait()

    monkeypatch.setattr(
        AgentLifecycleCoordinator, "wait_quiescent", observed_wait_quiescent
    )
    closing = asyncio.create_task(supervisor.aclose())
    await asyncio.wait_for(close_quiescent.wait(), 30)
    close_continue.set()
    # Deliver an already-unblocked close continuation until it is waiting on
    # the registered subtree Task; no wall-clock timing decides the assertion.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    gate.set()

    with pytest.raises(BaseExceptionGroup) as subtree_failure:
        await disposing
    with pytest.raises(BaseExceptionGroup) as close_failure:
        await closing

    assert sum(
        error is cleanup_error for error in _failure_leaves(subtree_failure.value)
    ) == 1
    assert sum(
        error is cleanup_error for error in _failure_leaves(close_failure.value)
    ) == 1
    assert factory.dispose_counts == {"child": 1, "root": 1}


async def test_aclose_claims_tree_failure_before_public_waiter_removes_it(
    world, monkeypatch
) -> None:
    store, factory, supervisor = world
    await _create(supervisor, "root")
    tree_directory_entered = asyncio.Event()
    tree_directory_continue = asyncio.Event()
    close_quiescent = asyncio.Event()
    close_continue = asyncio.Event()
    tree_error = RuntimeError("fixture tree projection failure")
    original_directory = AgentRegistrar.directory
    original_wait_quiescent = AgentLifecycleCoordinator.wait_quiescent

    async def gated_directory(registrar):
        task = asyncio.current_task()
        if (
            task is not None
            and task.get_name() == "traceh-supervisor-dispose-tree-root"
            and not tree_directory_entered.is_set()
        ):
            tree_directory_entered.set()
            await tree_directory_continue.wait()
            raise tree_error
        return await original_directory(registrar)

    async def gated_wait_quiescent(coordinator):
        await original_wait_quiescent(coordinator)
        close_quiescent.set()
        await close_continue.wait()

    monkeypatch.setattr(AgentRegistrar, "directory", gated_directory)
    monkeypatch.setattr(
        AgentLifecycleCoordinator, "wait_quiescent", gated_wait_quiescent
    )

    disposing = asyncio.create_task(supervisor.dispose("root"))
    await asyncio.wait_for(tree_directory_entered.wait(), 30)
    closing = asyncio.create_task(supervisor.aclose())
    await asyncio.wait_for(close_quiescent.wait(), 30)

    # Close has linearized but is deliberately held before its tree snapshot.
    # Cancelling the public waiter must not let its `finally` erase the tree
    # Task and the failure that only close can now report.
    disposing.cancel()
    tree_directory_continue.set()
    with pytest.raises(asyncio.CancelledError):
        await disposing
    close_continue.set()

    with pytest.raises(BaseExceptionGroup) as caught:
        await closing

    assert sum(error is tree_error for error in _failure_leaves(caught.value)) == 1
    assert factory.dispose_counts == {"root": 1}


async def test_aclose_keeps_shared_exception_from_independent_cleanup_tasks(world) -> None:
    store, factory, supervisor = world
    await _create(supervisor, "root")
    await _create(supervisor, "child-a", owner_agent_id="root")
    await _create(supervisor, "child-b", owner_agent_id="root")
    shared_error = RuntimeError("fixture shared cleanup failure")
    factory.dispose_errors["child-a"] = shared_error
    factory.dispose_errors["child-b"] = shared_error

    with pytest.raises(BaseExceptionGroup) as caught:
        await supervisor.aclose()

    assert sum(error is shared_error for error in _failure_leaves(caught.value)) == 2
    assert factory.dispose_counts == {"child-a": 1, "child-b": 1, "root": 1}
