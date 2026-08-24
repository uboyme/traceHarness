"""Tool capability policy for catalogued managed workspaces."""

from __future__ import annotations

import os
from pathlib import Path

from traceh.api.llm import ToolCall
from traceh.api.tools import EffectKind, Tool, ToolExecutionContext
from traceh.api.workspaces import WorkspaceAccess
from traceh.tools.policy import DecisionKind, ToolDecision
from traceh.workspaces.errors import WorkspaceSessionMismatchError
from traceh.workspaces.service import WorkspaceService


class ManagedWorkspaceAccessPolicy:
    """Enforce a catalogued workspace's read/write capability at Tool admission.

    This is deliberately a Tool policy, not an operating-system sandbox.
    Read-only Agents may use only pure and workspace-read tools; process and
    write effects are denied even if a host accidentally registered them in
    the same Composition. Other policies remain free to deny a read.
    """

    name = "managed-workspace-access"

    __slots__ = ("_service",)

    def __init__(self, service: WorkspaceService) -> None:
        self._service = service

    async def check(
        self,
        call: ToolCall,
        tool: Tool,
        context: ToolExecutionContext,
    ) -> ToolDecision:
        del call
        handle = await self._service.resolve_for_session(context.session_id)
        if _path_key(context.workspace) != _path_key(handle.root):
            raise WorkspaceSessionMismatchError
        if handle.access is WorkspaceAccess.WRITABLE:
            return ToolDecision(DecisionKind.DEFER, policy=self.name)
        if tool.effect_kind in {EffectKind.PURE_READ, EffectKind.WORKSPACE_READ}:
            return ToolDecision(
                DecisionKind.ALLOW,
                "managed workspace permits read-only tools",
                self.name,
            )
        return ToolDecision(
            DecisionKind.DENY,
            "managed workspace is read-only",
            self.name,
        )


def _path_key(path: Path) -> str:
    try:
        value = Path(path).absolute()
    except Exception:
        raise WorkspaceSessionMismatchError from None
    return os.path.normcase(os.path.normpath(str(value)))


__all__ = ["ManagedWorkspaceAccessPolicy"]
