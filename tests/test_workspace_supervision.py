"""Stage C integration with the existing public Agent Supervisor mainline."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from traceh.agents import AgentDirectoryReader
from traceh.api.agents import AgentRecord, AgentSpec
from traceh.api.turns import TurnInput
from traceh.api.workspaces import (
    WorkspaceAccess,
    WorkspaceProvisioningRequest,
    WorkspaceStatus,
)
from traceh.session.event_store import EventStore, InMemoryEventStore
from traceh.session.service import SessionService
from traceh.supervision import ProcessAgentSupervisor
from traceh.workspaces import (
    LocalGitWorkspaceProvider,
    WorkspaceManagedAgentSupervisor,
    WorkspaceService,
    WorkspaceSessionMismatchError,
)


def _git(*argv: str, cwd: Path) -> str:
    completed = subprocess.run(
        ("git", *argv),
        cwd=cwd,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _repository(root: Path) -> Path:
    root.mkdir()
    _git("init", "--initial-branch=main", cwd=root)
    _git("config", "user.name", "TraceHarness Fixture", cwd=root)
    _git("config", "user.email", "fixture@example.invalid", cwd=root)
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git("add", "tracked.txt", cwd=root)
    _git("commit", "-m", "fixture base", cwd=root)
    return root


class _Policy:
    def __init__(self, access: WorkspaceAccess = WorkspaceAccess.WRITABLE) -> None:
        self.access = access
        self.specs: list[AgentSpec] = []

    def workspace_for_agent(self, spec: AgentSpec) -> WorkspaceProvisioningRequest:
        self.specs.append(spec)
        return WorkspaceProvisioningRequest(
            source_id="trusted-source",
            revision="main",
            access=self.access,
        )


class _Execution:
    def __init__(self, store: EventStore, session_id: str) -> None:
        self._store = store
        self._session_id = session_id
        self.dispose_calls = 0

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def event_store(self) -> EventStore:
        return self._store

    async def run_turn(self, turn_input: TurnInput):
        del turn_input
        raise NotImplementedError

    async def cancel_turn(self, *, reason: str) -> bool:
        del reason
        return False

    async def dispose(self) -> None:
        self.dispose_calls += 1


class _Factory:
    def __init__(self, store: EventStore, managed_root: Path) -> None:
        self.store = store
        self.managed_root = managed_root
        self.provision_entered = asyncio.Event()
        self.provision_gate: asyncio.Event | None = None
        self.provision_error: BaseException | None = None
        self.session_root: Path | None = None
        self.executions: list[_Execution] = []

    async def provision(
        self,
        spec: AgentSpec,
        *,
        agent_id: str,
        session_id: str | None,
    ) -> _Execution:
        del agent_id
        self.provision_entered.set()
        if self.provision_gate is not None:
            await self.provision_gate.wait()
        if self.provision_error is not None:
            raise self.provision_error
        workspace = self.session_root or (self.managed_root / spec.workspace_id)
        assert workspace.is_dir()
        created = await SessionService(self.store).create_session(
            workspace, session_id=session_id
        )
        execution = _Execution(self.store, created)
        self.executions.append(execution)
        return execution

    async def activate(self, record: AgentRecord) -> _Execution:
        assert (self.managed_root / record.workspace_id).is_dir()
        execution = _Execution(self.store, record.session_id)
        self.executions.append(execution)
        return execution


def _assembly(tmp_path: Path):
    source = _repository(tmp_path / "source")
    managed = tmp_path / "managed"
    store = InMemoryEventStore()
    provider = LocalGitWorkspaceProvider(
        managed_root=managed,
        sources={"trusted-source": source},
    )
    service = WorkspaceService(store, provider)
    factory = _Factory(store, managed)
    inner = ProcessAgentSupervisor(store=store, factory=factory)
    policy = _Policy()
    supervisor = WorkspaceManagedAgentSupervisor(
        inner, service, workspace_policy=policy
    )
    return store, service, factory, inner, policy, supervisor


async def test_managed_create_attaches_exact_directory_session_and_worktree(
    tmp_path: Path,
) -> None:
    store, service, factory, _, policy, supervisor = _assembly(tmp_path)
    requested = AgentSpec(preset="coder", workspace_id="model-workspace-intent")
    handle = await supervisor.create(
        requested,
        request_id="create-request",
        agent_id="child-agent",
        session_id="child-session",
    )

    durable = (await AgentDirectoryReader(store).load()).get(handle.agent_id)
    assert durable is not None
    assert durable.workspace_id != requested.workspace_id
    workspace = await service.resolve_for_agent(handle.agent_id)
    assert workspace.root == service.managed_root / durable.workspace_id
    assert workspace.status is WorkspaceStatus.ATTACHED
    assert policy.specs == [requested]
    assert len(factory.executions) == 1

    await supervisor.dispose(handle.agent_id)
    assert workspace.root.exists()
    assert (await service.catalog()).for_agent(handle.agent_id).status is (
        WorkspaceStatus.ATTACHED
    )
    await supervisor.aclose()
    assert workspace.root.exists()


async def test_idempotent_create_reuses_one_workspace_and_agent(tmp_path: Path) -> None:
    _, service, _, _, _, supervisor = _assembly(tmp_path)
    spec = AgentSpec(preset="coder", workspace_id="approved-source-intent")
    first = await supervisor.create(spec, request_id="create-request")
    await supervisor.dispose(first.agent_id)
    second = await supervisor.create(spec, request_id="create-request")

    assert second.agent_id == first.agent_id
    catalog = await service.catalog()
    assert len(catalog.workspaces) == 1
    assert catalog.workspaces[0].status is WorkspaceStatus.ATTACHED
    await supervisor.aclose()


async def test_attached_dirty_workspace_remains_available_to_an_idempotent_retry(
    tmp_path: Path,
) -> None:
    _, service, _, _, _, supervisor = _assembly(tmp_path)
    spec = AgentSpec(preset="coder", workspace_id="approved-source-intent")
    first = await supervisor.create(spec, request_id="create-request")
    workspace = await service.resolve_for_agent(first.agent_id)
    marker = workspace.root / "candidate-change.txt"
    marker.write_text("candidate\n", encoding="utf-8")
    await supervisor.dispose(first.agent_id)

    second = await supervisor.create(spec, request_id="create-request")
    assert second.agent_id == first.agent_id
    assert marker.read_text(encoding="utf-8") == "candidate\n"
    await supervisor.aclose()


async def test_two_coding_children_receive_distinct_writable_worktrees(
    tmp_path: Path,
) -> None:
    _, service, _, _, _, supervisor = _assembly(tmp_path)
    spec = AgentSpec(preset="coder", workspace_id="approved-source-intent")
    first = await supervisor.create(
        spec,
        request_id="create-first",
        agent_id="agent-first",
        session_id="session-first",
    )
    second = await supervisor.create(
        spec,
        request_id="create-second",
        agent_id="agent-second",
        session_id="session-second",
    )

    first_workspace = await service.resolve_for_agent(first.agent_id)
    second_workspace = await service.resolve_for_agent(second.agent_id)
    assert first_workspace.root != second_workspace.root
    assert first_workspace.root.is_dir()
    assert second_workspace.root.is_dir()
    await supervisor.aclose()


async def test_provision_failure_releases_clean_worktree_without_agent(
    tmp_path: Path,
) -> None:
    store, service, factory, _, _, supervisor = _assembly(tmp_path)
    factory.provision_error = RuntimeError("fixture failure")
    with pytest.raises(RuntimeError, match="fixture failure"):
        await supervisor.create(
            AgentSpec(preset="coder", workspace_id="intent"),
            request_id="create-request",
        )

    catalog = await service.catalog()
    assert len(catalog.workspaces) == 1
    record = catalog.workspaces[0]
    assert record.status is WorkspaceStatus.RELEASED
    assert not (service.managed_root / record.workspace_id).exists()
    assert (await AgentDirectoryReader(store).load()).records == ()
    await supervisor.aclose()


async def test_cancellation_during_factory_provision_converges_then_releases(
    tmp_path: Path,
) -> None:
    store, service, factory, _, _, supervisor = _assembly(tmp_path)
    factory.provision_gate = asyncio.Event()
    factory.provision_error = RuntimeError("fixture provision failed")
    creating = asyncio.create_task(
        supervisor.create(
            AgentSpec(preset="coder", workspace_id="intent"),
            request_id="create-request",
        )
    )
    await factory.provision_entered.wait()
    creating.cancel()
    creating.cancel()
    creating.cancel()
    factory.provision_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await creating

    record = (await service.catalog()).workspaces[0]
    assert record.status is WorkspaceStatus.RELEASED
    assert not (service.managed_root / record.workspace_id).exists()
    assert (await AgentDirectoryReader(store).load()).records == ()
    await supervisor.aclose()


async def test_session_workspace_mismatch_fails_closed_and_disposes_activation(
    tmp_path: Path,
) -> None:
    store, service, factory, _, _, supervisor = _assembly(tmp_path)
    wrong = tmp_path / "wrong-session-root"
    wrong.mkdir()
    factory.session_root = wrong

    with pytest.raises(WorkspaceSessionMismatchError):
        await supervisor.create(
            AgentSpec(preset="coder", workspace_id="intent"),
            request_id="create-request",
            agent_id="child-agent",
        )
    durable = (await AgentDirectoryReader(store).load()).get("child-agent")
    assert durable is not None
    record = (await service.catalog()).get(durable.workspace_id)
    assert record is not None
    assert record.status is WorkspaceStatus.QUARANTINED
    assert factory.executions[0].dispose_calls == 1
    await supervisor.aclose()


async def test_close_waits_for_the_inflight_workspace_create_saga(
    tmp_path: Path,
) -> None:
    _, service, factory, _, _, supervisor = _assembly(tmp_path)
    factory.provision_gate = asyncio.Event()
    creating = asyncio.create_task(
        supervisor.create(
            AgentSpec(preset="coder", workspace_id="intent"),
            request_id="create-request",
        )
    )
    await factory.provision_entered.wait()
    close_started = asyncio.Event()
    close_returned = asyncio.Event()

    async def close_and_signal() -> None:
        close_started.set()
        await supervisor.aclose()
        close_returned.set()

    closing = asyncio.create_task(close_and_signal())
    await close_started.wait()
    assert not close_returned.is_set()
    factory.provision_gate.set()
    handle = await creating
    await closing

    record = (await service.catalog()).for_agent(handle.agent_id)
    assert record is not None
    assert record.status is WorkspaceStatus.ATTACHED
    assert (service.managed_root / record.workspace_id).exists()


async def test_close_waits_for_resume_post_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, service, factory, _, _, supervisor = _assembly(tmp_path)
    handle = await supervisor.create(
        AgentSpec(preset="coder", workspace_id="intent"),
        request_id="create-request",
        agent_id="child-agent",
        session_id="child-session",
    )
    await supervisor.dispose(handle.agent_id)
    entered = asyncio.Event()
    gate = asyncio.Event()
    inner_close_entered = asyncio.Event()
    original = WorkspaceService.resolve_for_agent
    original_inner_close = ProcessAgentSupervisor.aclose
    calls = 0

    async def gated_resolve_for_agent(
        self: WorkspaceService, agent_id: str
    ):
        nonlocal calls
        calls += 1
        if calls == 1:
            return await original(self, agent_id)
        entered.set()
        await gate.wait()
        return await original(self, agent_id)

    monkeypatch.setattr(
        WorkspaceService, "resolve_for_agent", gated_resolve_for_agent
    )

    async def observed_inner_close(self: ProcessAgentSupervisor) -> None:
        inner_close_entered.set()
        await original_inner_close(self)

    monkeypatch.setattr(
        ProcessAgentSupervisor, "aclose", observed_inner_close
    )
    resuming = asyncio.create_task(supervisor.resume(handle.session_id))
    await entered.wait()
    closing = asyncio.create_task(supervisor.aclose())
    # One yield starts public aclose(); the second starts its owned close Task.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert not closing.done()
    assert not resuming.done()
    assert not inner_close_entered.is_set()
    gate.set()
    resumed = await resuming
    await closing

    assert resumed.agent_id == handle.agent_id
    assert calls == 2
    assert inner_close_entered.is_set()
    assert factory.executions[-1].dispose_calls == 1
