"""Monotonic tool admission policies."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from traceh.api.llm import ToolCall
from traceh.api.tools import Tool, ToolExecutionContext


class DecisionKind(str, Enum):
    DEFER = "defer"
    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class ToolDecision:
    kind: DecisionKind
    reason: str = ""
    policy: str = ""


class ToolPolicy(Protocol):
    name: str

    async def check(
        self,
        call: ToolCall,
        tool: Tool,
        context: ToolExecutionContext,
    ) -> ToolDecision:
        ...


class AllowByDefaultPolicy:
    name = "allow-by-default"

    async def check(self, call: ToolCall, tool: Tool, context: ToolExecutionContext) -> ToolDecision:
        del call, tool, context
        return ToolDecision(DecisionKind.ALLOW, "default allow", self.name)


class DangerousShellPolicy:
    name = "dangerous-shell"
    _blocked = {
        "rm",
        "sudo",
        "su",
        "shutdown",
        "reboot",
        "poweroff",
        "mkfs",
        "mount",
        "umount",
        "dd",
        "chown",
        "chmod",
    }

    async def check(self, call: ToolCall, tool: Tool, context: ToolExecutionContext) -> ToolDecision:
        del context
        if tool.name != "shell":
            return ToolDecision(DecisionKind.DEFER, policy=self.name)
        command = call.arguments.get("command")
        if not isinstance(command, str):
            return ToolDecision(DecisionKind.DENY, "shell command must be a string", self.name)
        try:
            argv = shlex.split(command)
        except ValueError as error:
            return ToolDecision(DecisionKind.DENY, f"invalid shell command: {error}", self.name)
        if not argv:
            return ToolDecision(DecisionKind.DENY, "empty shell command", self.name)
        executable = argv[0].rsplit("/", 1)[-1]
        if executable in self._blocked:
            return ToolDecision(
                DecisionKind.DENY,
                f"command {executable!r} is blocked by the default safety policy",
                self.name,
            )
        return ToolDecision(DecisionKind.DEFER, policy=self.name)


async def evaluate_policies(
    policies: tuple[ToolPolicy, ...],
    call: ToolCall,
    tool: Tool,
    context: ToolExecutionContext,
) -> ToolDecision:
    allowed: ToolDecision | None = None
    for policy in policies:
        decision = await policy.check(call, tool, context)
        if decision.kind is DecisionKind.DENY:
            return decision
        if decision.kind is DecisionKind.ALLOW:
            allowed = decision
    return allowed or ToolDecision(DecisionKind.DENY, "no policy allowed the tool", "kernel")
