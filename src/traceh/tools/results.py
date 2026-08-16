"""Structured tool execution results."""

from __future__ import annotations

from dataclasses import dataclass, field

from traceh.api.json_types import JsonValue


@dataclass(frozen=True, slots=True)
class ToolRunResult:
    tool_call_id: str
    tool_name: str
    status: str
    content: str
    data: dict[str, JsonValue] = field(default_factory=dict)
    effect_id: str | None = None
    error_type: str | None = None

    def to_event_data(self, *, step_id: str) -> dict[str, JsonValue]:
        return {
            "step_id": step_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "status": self.status,
            "content": self.content,
            "data": self.data,
            "effect_id": self.effect_id,
            "error_type": self.error_type,
        }
