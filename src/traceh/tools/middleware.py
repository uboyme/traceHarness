"""Around middleware for tool execution."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from traceh.api.llm import ToolCall
from traceh.api.tools import Tool, ToolExecutionContext, ToolOutput

ToolCallNext = Callable[[], Awaitable[ToolOutput]]


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    call: ToolCall
    tool: Tool
    context: ToolExecutionContext


class ToolMiddleware(Protocol):
    name: str

    async def invoke(
        self,
        invocation: ToolInvocation,
        call_next: ToolCallNext,
    ) -> ToolOutput:
        ...


async def invoke_middleware_chain(
    middlewares: tuple[ToolMiddleware, ...],
    invocation: ToolInvocation,
) -> ToolOutput:
    async def invoke_at(index: int) -> ToolOutput:
        if index >= len(middlewares):
            return await invocation.tool.execute(invocation.call.arguments, invocation.context)
        middleware = middlewares[index]
        called = False

        async def call_next() -> ToolOutput:
            nonlocal called
            if called:
                raise RuntimeError(
                    f"tool middleware {middleware.name!r} called call_next more than once"
                )
            called = True
            return await invoke_at(index + 1)

        return await middleware.invoke(invocation, call_next)

    return await invoke_at(0)
