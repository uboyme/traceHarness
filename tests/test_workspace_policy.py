"""Tool capability enforcement for read-only and writable workspaces."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from traceh.agents import AgentRegistrar
from traceh.api.agents import AgentSpec
from traceh.api.llm import ToolCall
from traceh.api.tools import EffectKind, ToolExecutionContext
from traceh.api.workspaces import (
    WorkspaceAccess,
    WorkspaceHandle,
    WorkspaceLocalState,
    WorkspaceProvisioningRequest,
    WorkspaceRecord,
    WorkspaceSourceSnapshot,
)
from traceh.session.event_store import InMemoryEventStore
from traceh.session.service import SessionService
from traceh.tools.policy import DecisionKind
from traceh.workspaces import (
    ManagedWorkspaceAccessPolicy,
    WorkspaceService,
    WorkspaceSessionMismatchError,
)

_REPOSITORY_FINGERPRINT = "a" * 64
_BASE_REVISION = "b" * 40


class _Provider:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._present: set[str] = set()

    @property
    def managed_root(self) -> Path:
        return self._root

    async def resolve_source(
        self, source_id: str, revision: str
    ) -> WorkspaceSourceSnapshot:
        return WorkspaceSourceSnapshot(
            source_id=source_id,
            requested_revision=revision,
            repository_fingerprint=_REPOSITORY_FINGERPRINT,
            base_revision=_BASE_REVISION,
        )

    async def materialize(self, record: WorkspaceRecord) -> WorkspaceHandle:
        root = self._root / record.workspace_id
        root.mkdir(parents=True, exist_ok=True)
        self._present.add(record.workspace_id)
        return WorkspaceHandle(
            workspace_id=record.workspace_id,
            root=root,
            source_id=record.source_id,
            base_revision=record.base_revision,
            access=record.access,
            status=record.status,
        )

    async def inspect(self, record: WorkspaceRecord) -> WorkspaceLocalState:
        return (
            WorkspaceLocalState.CLEAN
            if record.workspace_id in self._present
            else WorkspaceLocalState.MISSING
        )

    async def remove(self, record: WorkspaceRecord) -> None:
        self._present.discard(record.workspace_id)


@dataclass
class _Tool:
    name: str
    effect_kind: EffectKind


async def _attached(
    tmp_path: Path, access: WorkspaceAccess
) -> tuple[WorkspaceService, str, Path]:
    store = InMemoryEventStore()
    service = WorkspaceService(store, _Provider(tmp_path / "managed"))
    handle = await service.provision(
        operation_id="provision-op",
        creation_request_id="create-request",
        request=WorkspaceProvisioningRequest("source-main", "main", access),
        owner_agent_id=None,
    )
    session_id = await SessionService(store).create_session(handle.root)
    await AgentRegistrar(store).create_agent(
        AgentSpec(preset="role", workspace_id=handle.workspace_id),
        request_id="create-request",
        agent_id="child-agent",
        session_id=session_id,
    )
    await service.finish_agent_creation(
        provision_operation_id="provision-op", primary=None
    )
    return service, session_id, handle.root


def _context(session_id: str, workspace: Path, tmp_path: Path) -> ToolExecutionContext:
    return ToolExecutionContext(
        session_id=session_id,
        turn_id="turn",
        step_id="step",
        tool_call_id="call",
        workspace=workspace,
        data_dir=tmp_path,
    )


async def test_read_only_workspace_allows_reads_and_denies_mutation(
    tmp_path: Path,
) -> None:
    service, session_id, root = await _attached(
        tmp_path, WorkspaceAccess.READ_ONLY
    )
    policy = ManagedWorkspaceAccessPolicy(service)
    call = ToolCall("call", "tool", {})

    read = await policy.check(
        call,
        _Tool("read", EffectKind.WORKSPACE_READ),
        _context(session_id, root, tmp_path),
    )
    write = await policy.check(
        call,
        _Tool("write", EffectKind.WORKSPACE_WRITE),
        _context(session_id, root, tmp_path),
    )
    process = await policy.check(
        call,
        _Tool("shell", EffectKind.PROCESS),
        _context(session_id, root, tmp_path),
    )

    assert read.kind is DecisionKind.ALLOW
    assert write.kind is DecisionKind.DENY
    assert process.kind is DecisionKind.DENY


async def test_writable_workspace_defers_to_the_remaining_host_policies(
    tmp_path: Path,
) -> None:
    service, session_id, root = await _attached(
        tmp_path, WorkspaceAccess.WRITABLE
    )
    decision = await ManagedWorkspaceAccessPolicy(service).check(
        ToolCall("call", "write", {}),
        _Tool("write", EffectKind.WORKSPACE_WRITE),
        _context(session_id, root, tmp_path),
    )
    assert decision.kind is DecisionKind.DEFER


async def test_tool_context_cannot_substitute_an_uncatalogued_path(
    tmp_path: Path,
) -> None:
    service, session_id, _ = await _attached(
        tmp_path, WorkspaceAccess.READ_ONLY
    )
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(WorkspaceSessionMismatchError):
        await ManagedWorkspaceAccessPolicy(service).check(
            ToolCall("call", "read", {}),
            _Tool("read", EffectKind.WORKSPACE_READ),
            _context(session_id, other, tmp_path),
        )
