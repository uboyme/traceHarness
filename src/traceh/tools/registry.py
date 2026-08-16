"""Tool registration and schema projection."""

from __future__ import annotations

from traceh.api.llm import ToolSchema
from traceh.api.tools import Tool


class ToolConflictError(RuntimeError):
    pass


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool, *, replace: bool = False) -> None:
        if tool.name in self._tools and not replace:
            raise ToolConflictError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def require(self, name: str) -> Tool:
        tool = self.get(name)
        if tool is None:
            raise LookupError(f"unknown tool: {name}")
        return tool

    def schemas(self) -> tuple[ToolSchema, ...]:
        return tuple(
            ToolSchema(tool.name, tool.description, tool.input_schema)
            for tool in sorted(self._tools.values(), key=lambda item: item.name)
        )

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))
