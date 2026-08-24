"""Tool protocol and effect semantics."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from traceh.api.json_types import JsonValue


class EffectKind(str, Enum):
    PURE_READ = "pure_read"
    WORKSPACE_READ = "workspace_read"
    WORKSPACE_WRITE = "workspace_write"
    PROCESS = "process"
    NETWORK_WRITE = "network_write"
    EXTERNAL_TRANSACTION = "external_transaction"

    @property
    def is_parallel_safe(self) -> bool:
        return self in {EffectKind.PURE_READ, EffectKind.WORKSPACE_READ}

    @property
    def is_retry_safe(self) -> bool:
        return self in {EffectKind.PURE_READ, EffectKind.WORKSPACE_READ}


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    session_id: str
    turn_id: str
    step_id: str
    tool_call_id: str
    workspace: Path
    data_dir: Path


@dataclass(frozen=True, slots=True)
class PreparedToolCall:
    """A call that already passed lookup, schema validation and normal Policy."""

    tool_call_id: str
    tool_name: str
    arguments: dict[str, JsonValue]
    effect_kind: EffectKind
    policy: str | None


@dataclass(frozen=True, slots=True)
class ToolAdmissionDecision:
    """One host admission answer corresponding to one prepared call."""

    tool_call_id: str
    admitted: bool
    code: str | None = None


@runtime_checkable
class ToolAdmissionGate(Protocol):
    """Stateful host admission after normal Policy and before dispatch."""

    async def admit(
        self,
        calls: tuple[PreparedToolCall, ...],
        context: ToolExecutionContext,
    ) -> tuple[ToolAdmissionDecision, ...]:
        ...


@dataclass(frozen=True, slots=True)
class ToolOutput:
    content: str
    data: dict[str, JsonValue] = field(default_factory=dict)
    evidence: tuple[str, ...] = ()


class Tool(Protocol):
    name: str
    description: str
    input_schema: dict[str, JsonValue]
    effect_kind: EffectKind

    async def execute(
        self,
        arguments: dict[str, JsonValue],
        context: ToolExecutionContext,
    ) -> ToolOutput:
        ...
