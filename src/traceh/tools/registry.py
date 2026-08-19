"""Tool registration and schema projection."""

from __future__ import annotations

from traceh.api.llm import ToolSchema
from traceh.api.tools import Tool
from traceh.kernel.lifespan import CallbackRegistration


class ToolConflictError(RuntimeError):
    pass


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._composition_resource_binding = None

    def register(self, tool: Tool, *, replace: bool = False) -> CallbackRegistration:
        """Register a tool and return the registration that reverses it.

        Core assembly ignores the return value; plugin activation owns it so a
        failed or disposed activation can put the registry back exactly as it
        was, including restoring a tool that ``replace=True`` shadowed.
        """

        tool_binding = getattr(tool, "_composition_resource_binding", None)
        if (
            self._composition_resource_binding is not None
            and tool_binding is not None
            and self._composition_resource_binding is not tool_binding
        ):
            raise ValueError("tool registry mixes composition resource lineages")
        if self._composition_resource_binding is None and tool_binding is not None:
            self._composition_resource_binding = tool_binding

        previous = self._tools.get(tool.name)
        if previous is not None and not replace:
            raise ToolConflictError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

        async def cleanup() -> None:
            # Only undo our own entry: if something later replaced this tool, the
            # newer registration owns the slot and reversing it is its job.
            current = self._tools.get(tool.name)
            if current is tool:
                if previous is None:
                    self._tools.pop(tool.name, None)
                else:
                    self._tools[tool.name] = previous

        return CallbackRegistration(cleanup)

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

    def fork(self) -> ToolRegistry:
        """Return an independent registry view of the current tools.

        The registered Tool objects are intentionally borrowed.  Registry
        membership is the mutable boundary owned by the caller, while the
        tools themselves remain application/core resources unless a plugin
        Activation explicitly owns their cleanup.
        """

        forked = ToolRegistry()
        forked._tools = dict(self._tools)
        forked._composition_resource_binding = self._composition_resource_binding
        return forked
