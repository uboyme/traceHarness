"""Step-frozen runtime composition provenance."""

from __future__ import annotations

import math
from dataclasses import dataclass

from traceh.api.json_types import JsonValue, canonical_json, fingerprint, to_json_value
from traceh.api.llm import ToolSchema
from traceh.api.plugins import PluginIdentity

EMPTY_SKILL_CATALOG_DIGEST = fingerprint([])


def _require_json(value: object) -> None:
    if value is None or type(value) in (bool, int):
        return
    if type(value) is str:
        value.encode("utf-8")
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _require_json(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _require_json(item)
        return
    raise ValueError("composition-snapshot-invalid")


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

    @property
    def skill_catalog(self) -> tuple[()]:
        """F0-B has no Skill contribution owner; nonempty catalogs are refused."""
        return ()

    @property
    def skill_catalog_digest(self) -> str:
        return EMPTY_SKILL_CATALOG_DIGEST

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "revision": self.revision,
            "provider": self.provider,
            "model": self.model,
            "system_prompt": self.system_prompt,
            "tools": [to_json_value(tool.to_dict()) for tool in self.tools],
            "plugins": [plugin.to_dict() for plugin in self.plugins],
            "policies": list(self.policies),
            "tool_middlewares": list(self.tool_middlewares),
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "skill_catalog": [],
            "skill_catalog_digest": self.skill_catalog_digest,
        }

    @classmethod
    def from_dict(cls, value: object) -> CompositionSnapshot:
        """Read the single current payload, including its content revision.

        Plugin eligibility/version semantics remain in the existing plugin
        identity reader. This parser owns Composition shape and content bytes.
        """
        keys = {
            "revision",
            "provider",
            "model",
            "system_prompt",
            "tools",
            "plugins",
            "policies",
            "tool_middlewares",
            "temperature",
            "max_output_tokens",
            "skill_catalog",
            "skill_catalog_digest",
        }
        if type(value) is not dict or set(value) != keys:
            raise ValueError("composition-snapshot-keys-invalid")
        _require_json(value)
        if type(value["skill_catalog"]) is not list or value["skill_catalog"]:
            raise ValueError("composition-skill-catalog-unsupported")
        if value["skill_catalog_digest"] != EMPTY_SKILL_CATALOG_DIGEST:
            raise ValueError("composition-skill-catalog-digest-mismatch")
        for name in ("revision", "provider", "model", "system_prompt"):
            if type(value[name]) is not str:
                raise ValueError("composition-snapshot-invalid")
        for name in ("policies", "tool_middlewares"):
            if type(value[name]) is not list or any(type(x) is not str for x in value[name]):
                raise ValueError("composition-snapshot-invalid")
        temperature = value["temperature"]
        if temperature is not None and type(temperature) not in (int, float):
            raise ValueError("composition-snapshot-invalid")
        output_limit = value["max_output_tokens"]
        if output_limit is not None and (type(output_limit) is not int or output_limit < 1):
            raise ValueError("composition-snapshot-invalid")
        raw_tools, raw_plugins = value["tools"], value["plugins"]
        if type(raw_tools) is not list or type(raw_plugins) is not list:
            raise ValueError("composition-snapshot-invalid")
        if any(type(t) is not dict for t in raw_tools):
            raise ValueError("composition-snapshot-invalid")
        if any(
            type(p) is not dict
            or set(p) != {"plugin_id", "version"}
            or any(type(v) is not str for v in p.values())
            for p in raw_plugins
        ):
            raise ValueError("composition-snapshot-invalid")
        snapshot = cls(
            revision=value["revision"],
            provider=value["provider"],
            model=value["model"],
            system_prompt=value["system_prompt"],
            tools=tuple(ToolSchema.from_dict(tool) for tool in raw_tools),
            plugins=tuple(PluginIdentity(p["plugin_id"], p["version"]) for p in raw_plugins),
            policies=tuple(value["policies"]),
            tool_middlewares=tuple(value["tool_middlewares"]),
            temperature=temperature,
            max_output_tokens=output_limit,
        )
        if canonical_json(snapshot.to_dict()) != canonical_json(value):
            raise ValueError("composition-snapshot-invalid")
        content = {key: item for key, item in value.items() if key != "revision"}
        if fingerprint(content) != snapshot.revision:
            raise ValueError("composition-revision-mismatch")
        return snapshot


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
            "skill_catalog": [],
            "skill_catalog_digest": EMPTY_SKILL_CATALOG_DIGEST,
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
