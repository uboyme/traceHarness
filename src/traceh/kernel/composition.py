"""Step-frozen runtime composition provenance."""

from __future__ import annotations

from dataclasses import dataclass

from traceh.api.json_types import JsonValue, fingerprint
from traceh.api.llm import ToolSchema
from traceh.api.plugins import PluginIdentity


@dataclass(frozen=True, slots=True)
class CompositionSnapshot:
    revision: str
    provider: str
    model: str
    system_prompt: str
    tools: tuple[ToolSchema, ...]
    plugins: tuple[PluginIdentity, ...]
    policies: tuple[str, ...]
    tool_middlewares: tuple[str, ...] = ()
    temperature: float | None = None
    max_output_tokens: int | None = None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "revision": self.revision,
            "provider": self.provider,
            "model": self.model,
            "system_prompt": self.system_prompt,
            "tools": [tool.to_dict() for tool in self.tools],
            "plugins": [plugin.to_dict() for plugin in self.plugins],
            "policies": list(self.policies),
            "tool_middlewares": list(self.tool_middlewares),
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
        }


@dataclass(frozen=True, slots=True)
class RuntimeComposition:
    provider: str
    model: str
    system_prompt: str
    tools: tuple[ToolSchema, ...]
    plugins: tuple[PluginIdentity, ...] = ()
    policies: tuple[str, ...] = ()
    tool_middlewares: tuple[str, ...] = ()
    temperature: float | None = None
    max_output_tokens: int | None = None

    def snapshot(self) -> CompositionSnapshot:
        payload: dict[str, object] = {
            "provider": self.provider,
            "model": self.model,
            "system_prompt": self.system_prompt,
            "tools": [tool.to_dict() for tool in self.tools],
            "plugins": [plugin.to_dict() for plugin in self.plugins],
            "policies": list(self.policies),
            "tool_middlewares": list(self.tool_middlewares),
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
        }
        return CompositionSnapshot(
            revision=fingerprint(payload),
            provider=self.provider,
            model=self.model,
            system_prompt=self.system_prompt,
            tools=self.tools,
            plugins=self.plugins,
            policies=self.policies,
            tool_middlewares=self.tool_middlewares,
            temperature=self.temperature,
            max_output_tokens=self.max_output_tokens,
        )
