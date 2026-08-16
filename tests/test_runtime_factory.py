from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from traceh.api.json_types import JsonValue
from traceh.api.llm import ModelResponse, ToolCall
from traceh.api.tools import EffectKind, ToolExecutionContext, ToolOutput
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.session.event_store import InMemoryEventStore


@dataclass(slots=True)
class PingTool:
    name: str = "ping"
    description: str = "return pong"
    effect_kind: EffectKind = EffectKind.PURE_READ
    input_schema: dict[str, JsonValue] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.input_schema = {"type": "object", "properties": {}, "additionalProperties": False}

    async def execute(
        self,
        arguments: dict[str, JsonValue],
        context: ToolExecutionContext,
    ) -> ToolOutput:
        del arguments, context
        return ToolOutput("pong")


@pytest.mark.asyncio
async def test_factory_accepts_custom_store_and_tools(tmp_path) -> None:
    provider = ScriptedLlmProvider(
        (
            ModelResponse(
                content="call ping",
                tool_calls=(ToolCall("p", "ping", {}),),
                finish_reason="tool_calls",
            ),
            ModelResponse(content="done"),
        )
    )
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data", provider="scripted", model="custom"),
        provider=provider,
        event_store=InMemoryEventStore(),
        additional_tools=(PingTool(),),
        include_default_tools=False,
    )
    result = await runtime.run(tmp_path, "ping")
    assert result.reason == "completed"
    assert [tool.name for tool in provider.requests[0].tools] == ["ping"]
    await runtime.dispose()
