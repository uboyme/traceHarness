"""Forward-compatible multi-agent control-plane protocols.

v0.3 does not ship an AgentSupervisor implementation. These immutable values define the
boundary that later subagent tools and workflow engines can target without importing the
single-Agent loop internals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from traceh.api.json_types import JsonValue


@dataclass(frozen=True, slots=True)
class Budget:
    max_tokens: int = 100_000
    max_steps: int = 50
    max_tool_calls: int = 200
    max_wall_seconds: float = 900.0
    max_children: int = 4
    max_depth: int = 3
    max_processes: int = 4


class MessageTarget(str, Enum):
    NEW_TURN = "new_turn"
    NEXT_STEP = "next_step"


@dataclass(frozen=True, slots=True)
class AgentMessage:
    message_id: str
    content: str
    source: str
    correlation_id: str | None = None
    causation_id: str | None = None


@dataclass(frozen=True, slots=True)
class AgentSpec:
    preset: str
    workspace_id: str
    owner_agent_id: str | None = None
    forked_from_session_id: str | None = None
    capability_grants: tuple[str, ...] = ()
    budget: Budget = field(default_factory=Budget)
    metadata: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MessageReceipt:
    message_id: str
    agent_id: str
    accepted_seq: int


@dataclass(frozen=True, slots=True)
class AgentRunReport:
    agent_id: str
    session_id: str
    reason: str
    final_text: str
    artifact_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()


class AgentHandle(Protocol):
    agent_id: str
    session_id: str


class AgentSupervisor(Protocol):
    async def create(self, spec: AgentSpec) -> AgentHandle:
        ...

    async def resume(self, session_id: str) -> AgentHandle:
        ...

    async def send(
        self,
        agent_id: str,
        message: AgentMessage,
        *,
        target: MessageTarget,
        wakeup: bool,
    ) -> MessageReceipt:
        ...

    async def interrupt(self, agent_id: str, reason: str) -> None:
        ...

    async def wait_idle(self, agent_id: str) -> None:
        ...

    async def dispose(self, agent_id: str) -> None:
        ...
