"""v0.7 D0: protocol seams stay outside the v0.6 concurrency kernel."""

from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass, fields
from pathlib import Path
from typing import get_type_hints

import pytest

from traceh.agents import AgentDirectoryReader, AgentRegistrar
from traceh.api.agents import (
    AgentHandle,
    AgentMessage,
    AgentRunReport,
    AgentSpec,
    AgentSupervisor,
    MessageReceipt,
    MessageTarget,
)
from traceh.api.tools import ToolExecutionContext
from traceh.session.event_store import EventStore, InMemoryEventStore
from traceh.supervision import (
    AgentToolAuthority,
    AgentToolAuthorizationError,
    AgentToolBindingError,
    ChildProvisioningProposal,
    SupervisorToolset,
)


@dataclass(frozen=True, slots=True)
class _Handle:
    agent_id: str
    session_id: str


class _ProtocolSupervisor:
    """A structural Supervisor with no ProcessAgentSupervisor internals."""

    def __init__(self, store: EventStore) -> None:
        self._store = store
        self.created_specs: list[AgentSpec] = []

    @property
    def store(self) -> EventStore:
        return self._store

    async def create(
        self,
        spec: AgentSpec,
        *,
        request_id: str,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> AgentHandle:
        del request_id
        self.created_specs.append(spec)
        return _Handle(agent_id or "host-assigned-agent", session_id or "host-session")

    async def resume(self, session_id: str) -> AgentHandle:
        return _Handle("resumed-agent", session_id)

    async def send(
        self,
        agent_id: str,
        message: AgentMessage,
        *,
        target: MessageTarget,
        wakeup: bool,
    ) -> MessageReceipt:
        del target, wakeup
        return MessageReceipt(message.message_id, agent_id, 1)

    async def interrupt(self, agent_id: str, reason: str = "interrupted") -> bool:
        del agent_id, reason
        return False

    async def wait_idle(self, agent_id: str) -> None:
        del agent_id

    async def wait_message(
        self, agent_id: str, message_id: str
    ) -> AgentRunReport:
        return AgentRunReport(
            agent_id=agent_id,
            session_id="host-session",
            message_id=message_id,
            reason="completed",
            final_text="done",
        )

    async def report(self, agent_id: str, message_id: str) -> AgentRunReport:
        return await self.wait_message(agent_id, message_id)

    async def dispose(self, agent_id: str) -> None:
        del agent_id

    async def aclose(self) -> None:
        return None


class _MappingPolicy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def propose_child(
        self,
        *,
        owner,
        requested_preset: str,
        requested_workspace_id: str,
    ) -> ChildProvisioningProposal:
        self.calls.append(
            (owner.agent_id, requested_preset, requested_workspace_id)
        )
        return ChildProvisioningProposal(
            preset="approved-preset",
            workspace_id="approved-workspace",
            metadata={"decision": {"source": "host-policy"}},
        )


class _InvalidPolicy:
    def propose_child(self, **kwargs):
        del kwargs
        return object()


def _context(tmp_path: Path) -> ToolExecutionContext:
    return ToolExecutionContext(
        session_id="owner-session",
        turn_id="turn-1",
        step_id="step-1",
        tool_call_id="call-1",
        workspace=tmp_path,
        data_dir=tmp_path,
    )


async def _register(
    store: EventStore,
    *,
    agent_id: str,
    session_id: str,
    request_id: str,
    owner_agent_id: str | None = None,
) -> None:
    await AgentRegistrar(store).create_agent(
        AgentSpec(
            preset="directory-preset",
            workspace_id="directory-workspace",
            owner_agent_id=owner_agent_id,
        ),
        request_id=request_id,
        agent_id=agent_id,
        session_id=session_id,
    )


def test_toolset_imports_the_public_protocol_not_the_process_supervisor() -> None:
    import traceh.plugins.manager as plugin_manager_module
    import traceh.runtime.agent_loop as loop_module
    import traceh.runtime.agent_runtime as runtime_module
    import traceh.supervision.authority as authority_module
    import traceh.supervision.tools as tools_module

    hints = get_type_hints(SupervisorToolset.__init__)
    assert hints["supervisor"] is AgentSupervisor
    assert get_type_hints(AgentSupervisor.store.fget)["return"] is EventStore

    tools_tree = ast.parse(Path(tools_module.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tools_tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "ProcessAgentSupervisor" not in imported

    authority_tree = ast.parse(
        Path(authority_module.__file__).read_text(encoding="utf-8")
    )
    authority_imported = {
        alias.name
        for node in ast.walk(authority_tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "ProcessAgentSupervisor" not in authority_imported
    assert "AgentRegistrar" not in authority_imported
    for module in (loop_module, runtime_module, plugin_manager_module):
        text = Path(module.__file__).read_text(encoding="utf-8")
        assert "AgentToolAuthority" not in text
        assert "ChildProvisioningPolicy" not in text


async def test_protocol_only_supervisor_uses_host_proposal_without_a_parallel_path(
    tmp_path: Path,
) -> None:
    store = InMemoryEventStore()
    await _register(
        store,
        agent_id="owner-agent",
        session_id="owner-session",
        request_id="owner-request",
    )
    supervisor = _ProtocolSupervisor(store)
    policy = _MappingPolicy()
    assert isinstance(supervisor, AgentSupervisor)

    toolset = SupervisorToolset(
        supervisor=supervisor,
        owner_agent_id="owner-agent",
        event_store=store,
        provisioning_policy=policy,
    )
    spawn = next(tool for tool in toolset.tools if tool.name == "spawn_agent")
    result = await spawn.execute(
        {"preset": "requested-preset", "workspace_id": "requested-workspace"},
        _context(tmp_path),
    )

    assert result.data["agent_id"] == "host-assigned-agent"
    assert policy.calls == [
        ("owner-agent", "requested-preset", "requested-workspace")
    ]
    assert len(supervisor.created_specs) == 1
    created = supervisor.created_specs[0]
    assert created.preset == "approved-preset"
    assert created.workspace_id == "approved-workspace"
    assert created.owner_agent_id == "owner-agent"
    assert created.metadata == {"decision": {"source": "host-policy"}}
    assert created.capability_grants == ()


def test_child_proposal_and_spawn_schema_keep_task_and_runtime_choices_out() -> None:
    assert {item.name for item in fields(ChildProvisioningProposal)} == {
        "preset",
        "workspace_id",
        "metadata",
    }
    parameters = inspect.signature(_MappingPolicy.propose_child).parameters
    for forbidden in (
        "task",
        "message",
        "provider",
        "model",
        "prompt",
        "budget",
        "capability_grants",
    ):
        assert forbidden not in parameters


async def test_invalid_host_proposal_fails_before_supervisor_create(
    tmp_path: Path,
) -> None:
    store = InMemoryEventStore()
    await _register(
        store,
        agent_id="owner-agent",
        session_id="owner-session",
        request_id="owner-request",
    )
    supervisor = _ProtocolSupervisor(store)
    toolset = SupervisorToolset(
        supervisor=supervisor,
        owner_agent_id="owner-agent",
        event_store=store,
        provisioning_policy=_InvalidPolicy(),
    )
    spawn = next(tool for tool in toolset.tools if tool.name == "spawn_agent")

    with pytest.raises(AgentToolBindingError):
        await spawn.execute(
            {"preset": "requested-preset", "workspace_id": "requested-workspace"},
            _context(tmp_path),
        )
    assert supervisor.created_specs == []


async def test_authority_reloads_the_directory_and_never_caches_ownership() -> None:
    store = InMemoryEventStore()
    await _register(
        store,
        agent_id="owner-agent",
        session_id="owner-session",
        request_id="owner-request",
    )
    authority = AgentToolAuthority(
        directory_reader=AgentDirectoryReader(store),
        owner_agent_id="owner-agent",
    )

    owner = await authority.require_caller("owner-session")
    assert owner.agent_id == "owner-agent"
    with pytest.raises(AgentToolAuthorizationError):
        await authority.require_owned("owner-agent", "owner-session")

    await _register(
        store,
        agent_id="later-child",
        session_id="child-session",
        request_id="child-request",
        owner_agent_id="owner-agent",
    )
    child = await authority.require_owned("later-child", "owner-session")
    assert child.agent_id == "later-child"
    assert set(AgentToolAuthority.__slots__) == {
        "_directory_reader",
        "_owner_agent_id",
    }
