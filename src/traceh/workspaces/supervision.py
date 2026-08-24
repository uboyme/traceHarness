"""Workspace lifecycle adapter around the one public Agent Supervisor."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Protocol

from traceh.agents.identity import freeze_agent_spec
from traceh.api.agents import (
    AgentHandle,
    AgentMessage,
    AgentRunReport,
    AgentSpec,
    AgentSupervisor,
    MessageReceipt,
    MessageTarget,
)
from traceh.api.workspaces import WorkspaceHandle, WorkspaceProvisioningRequest
from traceh.session.event_store import EventStore
from traceh.supervision.delivery_identity import require_delivery_identifier
from traceh.supervision.errors import SupervisorDisposedError
from traceh.supervision.execution import durable_log_identity
from traceh.workspaces.errors import WorkspaceDirectoryMismatchError
from traceh.workspaces.events import freeze_provisioning_request
from traceh.workspaces.service import (
    WorkspaceService,
    converge_workspace_operation,
    workspace_operation_id,
)


class AgentWorkspacePolicy(Protocol):
    """Host-only mapping from an approved Agent spec to a Git source request."""

    def workspace_for_agent(self, spec: AgentSpec) -> WorkspaceProvisioningRequest:
        ...


class WorkspaceManagedAgentSupervisor:
    """Bind one Supervisor create saga to one managed workspace lifecycle.

    This adapter owns no Activation table or Directory cache. It serializes the
    actual cross-domain create mutation and the complete wrapper-owned resume
    validation, delegates execution to ``inner``, and asks ``WorkspaceService``
    to reconcile only after that public operation has converged.
    """

    __slots__ = (
        "_close_task",
        "_closed",
        "_inner",
        "_lock",
        "_policy",
        "_service",
    )

    def __init__(
        self,
        inner: AgentSupervisor,
        service: WorkspaceService,
        *,
        workspace_policy: AgentWorkspacePolicy,
    ) -> None:
        if durable_log_identity(inner.store) is not durable_log_identity(service.store):
            raise WorkspaceDirectoryMismatchError
        self._inner = inner
        self._service = service
        self._policy = workspace_policy
        self._lock = asyncio.Lock()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    @property
    def store(self) -> EventStore:
        return self._inner.store

    async def create(
        self,
        spec: AgentSpec,
        *,
        request_id: str,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> AgentHandle:
        frozen = freeze_agent_spec(spec)
        request_id = require_delivery_identifier(request_id, field="request_id")
        agent_id = (
            None
            if agent_id is None
            else require_delivery_identifier(agent_id, field="agent_id")
        )
        session_id = (
            None
            if session_id is None
            else require_delivery_identifier(session_id, field="session_id")
        )
        try:
            request = freeze_provisioning_request(
                self._policy.workspace_for_agent(frozen)
            )
        except BaseException:
            # Host policy is synchronous and runs before any mutation. Never
            # turn an interrupt into an input verdict.
            raise
        operation_id = workspace_operation_id(
            "provision",
            creation_request_id=request_id,
            owner_agent_id=frozen.owner_agent_id,
        )

        async with self._lock:
            if self._closed:
                raise SupervisorDisposedError
            try:
                workspace = await self._service.provision(
                    operation_id=operation_id,
                    creation_request_id=request_id,
                    request=request,
                    owner_agent_id=frozen.owner_agent_id,
                )
            except BaseException as error:
                await self._service.finish_agent_creation(
                    provision_operation_id=operation_id,
                    primary=error,
                )
                raise AssertionError(
                    "workspace reconciliation did not re-raise"
                ) from None

            managed_spec = replace(frozen, workspace_id=workspace.workspace_id)
            try:
                handle = await self._inner.create(
                    managed_spec,
                    request_id=request_id,
                    agent_id=agent_id,
                    session_id=session_id,
                )
            except BaseException as error:
                await self._service.finish_agent_creation(
                    provision_operation_id=operation_id,
                    primary=error,
                )
                raise AssertionError(
                    "workspace reconciliation did not re-raise"
                ) from None
            try:
                await self._service.finish_agent_creation(
                    provision_operation_id=operation_id,
                    primary=None,
                )
            except BaseException as error:
                try:
                    await converge_workspace_operation(
                        self._inner.dispose(handle.agent_id),
                        name="traceh-workspace-unattached-agent-dispose",
                    )
                except BaseException as cleanup_error:
                    if isinstance(error, asyncio.CancelledError):
                        raise error from cleanup_error
                    raise BaseExceptionGroup(
                        "workspace attachment and Agent disposal both failed",
                        (error, cleanup_error),
                    ) from None
                raise
            return handle

    async def resume(self, session_id: str) -> AgentHandle:
        session_id = require_delivery_identifier(session_id, field="session_id")
        async with self._lock:
            if self._closed:
                raise SupervisorDisposedError
            await self._service.resolve_for_session(session_id)
            handle = await self._inner.resume(session_id)
            try:
                await self._service.resolve_for_agent(handle.agent_id)
            except BaseException as error:
                try:
                    await converge_workspace_operation(
                        self._inner.dispose(handle.agent_id),
                        name="traceh-workspace-invalid-resume-dispose",
                    )
                except BaseException as cleanup_error:
                    if isinstance(error, asyncio.CancelledError):
                        raise error from cleanup_error
                    raise BaseExceptionGroup(
                        "workspace validation and resumed Agent disposal both failed",
                        (error, cleanup_error),
                    ) from None
                raise
            return handle

    async def send(
        self,
        agent_id: str,
        message: AgentMessage,
        *,
        target: MessageTarget,
        wakeup: bool,
    ) -> MessageReceipt:
        async with self._lock:
            if self._closed:
                raise SupervisorDisposedError
            await self._service.resolve_for_agent(agent_id)
            return await self._inner.send(
                agent_id, message, target=target, wakeup=wakeup
            )

    @asynccontextmanager
    async def capture_workspace(
        self, agent_id: str
    ) -> AsyncIterator[WorkspaceHandle]:
        """Hold the managed writer/close gate across one host-owned snapshot.

        The Artifact domain consumes this through its public
        ``WorkspaceCaptureGate`` Protocol.  No Artifact type enters this
        module, and the lease owns no second Activation or workspace fact.
        """

        agent_id = require_delivery_identifier(agent_id, field="agent_id")
        async with self._lock:
            if self._closed:
                raise SupervisorDisposedError
            yield await self._service.resolve_for_agent(agent_id)

    async def interrupt(self, agent_id: str, reason: str = "interrupted") -> bool:
        return await self._inner.interrupt(agent_id, reason)

    async def wait_idle(self, agent_id: str) -> None:
        await self._inner.wait_idle(agent_id)

    async def wait_message(
        self, agent_id: str, message_id: str
    ) -> AgentRunReport:
        return await self._inner.wait_message(agent_id, message_id)

    async def report(self, agent_id: str, message_id: str) -> AgentRunReport:
        return await self._inner.report(agent_id, message_id)

    async def dispose(self, agent_id: str) -> None:
        # Activation lifetime and workspace lifetime are intentionally
        # different. A stopped Agent's worktree remains until explicit release.
        await self._inner.dispose(agent_id)

    async def aclose(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._close(), name="traceh-workspace-supervisor-close"
            )
        task = self._close_task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as cancellation:
            from traceh.concurrency import await_worker_convergence

            await await_worker_convergence(task)
            if not task.cancelled():
                failure = task.exception()
                if failure is not None:
                    raise cancellation from failure
            raise cancellation

    async def _close(self) -> None:
        async with self._lock:
            self._closed = True
            await self._inner.aclose()


__all__ = ["AgentWorkspacePolicy", "WorkspaceManagedAgentSupervisor"]
