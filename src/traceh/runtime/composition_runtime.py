"""Step-scoped composition leases.

The current implementation is static. Future plugin generations can provide the same
protocol while retaining provider/tool objects until the Step lease exits.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from traceh.api.llm import LlmProvider
from traceh.api.plugins import PluginIdentity
from traceh.kernel.composition import CompositionSnapshot, RuntimeComposition
from traceh.llm.registry import LlmRegistry
from traceh.runtime.prompt import PromptAssembler
from traceh.tools.runtime import ToolRuntime


@dataclass(frozen=True, slots=True)
class ActiveComposition:
    snapshot: CompositionSnapshot
    provider: LlmProvider
    tools: ToolRuntime


class CompositionRuntime(Protocol):
    def lease(
        self,
        *,
        workspace: Path,
        session_id: str,
        turn_id: str,
        step_id: str,
    ) -> "CompositionLease":
        ...


class CompositionLease(Protocol):
    async def __aenter__(self) -> ActiveComposition:
        ...

    async def __aexit__(self, exc_type, exc_value, traceback) -> bool | None:
        ...


class StaticCompositionRuntime:
    def __init__(
        self,
        *,
        llms: LlmRegistry,
        tools: ToolRuntime,
        prompt: PromptAssembler,
        provider: str,
        model: str,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> None:
        self.llms = llms
        self.tools = tools
        self.prompt = prompt
        self.provider = provider
        self.model = model
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens

    @asynccontextmanager
    async def lease(
        self,
        *,
        workspace: Path,
        session_id: str,
        turn_id: str,
        step_id: str,
    ) -> AsyncIterator[ActiveComposition]:
        del session_id, turn_id, step_id
        provider = self.llms.require(self.provider)
        snapshot = RuntimeComposition(
            provider=self.provider,
            model=self.model,
            system_prompt=self.prompt.assemble(workspace=str(workspace)),
            tools=self.tools.registry.schemas(),
            plugins=(PluginIdentity("traceh.core", "0.3.0"),),
            policies=tuple(policy.name for policy in self.tools.policies),
            tool_middlewares=tuple(
                middleware.name for middleware in self.tools.middlewares
            ),
            temperature=self.temperature,
            max_output_tokens=self.max_output_tokens,
        ).snapshot()
        yield ActiveComposition(snapshot=snapshot, provider=provider, tools=self.tools)
