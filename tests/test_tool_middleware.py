from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from traceh.api.json_types import JsonValue
from traceh.api.llm import ToolCall
from traceh.api.tools import EffectKind, ToolExecutionContext, ToolOutput
from traceh.tools.middleware import ToolInvocation, invoke_middleware_chain


@dataclass(slots=True)
class BaseTool:
    name: str = "base"
    description: str = "base"
    effect_kind: EffectKind = EffectKind.PURE_READ
    input_schema: dict[str, JsonValue] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.input_schema = {"type": "object"}

    async def execute(self, arguments, context) -> ToolOutput:
        del arguments, context
        return ToolOutput("base")


class SuffixMiddleware:
    name = "suffix"

    async def invoke(self, invocation, call_next) -> ToolOutput:
        del invocation
        output = await call_next()
        return ToolOutput(output.content + ":wrapped")


class BadMiddleware:
    name = "bad"

    async def invoke(self, invocation, call_next) -> ToolOutput:
        del invocation
        await call_next()
        return await call_next()


@pytest.mark.asyncio
async def test_tool_middleware_can_transform_output_and_next_is_single_use(tmp_path) -> None:
    invocation = ToolInvocation(
        ToolCall("c", "base", {}),
        BaseTool(),
        ToolExecutionContext("s", "t", "p", "c", tmp_path, tmp_path),
    )
    output = await invoke_middleware_chain((SuffixMiddleware(),), invocation)
    assert output.content == "base:wrapped"
    with pytest.raises(RuntimeError, match="more than once"):
        await invoke_middleware_chain((BadMiddleware(),), invocation)
