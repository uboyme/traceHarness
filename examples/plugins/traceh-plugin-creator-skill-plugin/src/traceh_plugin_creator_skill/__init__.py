"""TraceHarness Plugin Creator Skill.

This independent distribution teaches a TraceHarness coding agent how to author
a plugin candidate without adding a second loader, generator runtime, installer
or approval system.  It contributes one short prompt section and one pure-read
tool backed by packaged Markdown resources.  Candidate files are still written
through the harness's existing workspace tools and recorded by the normal event
and effect paths.

The skill intentionally has no write, process, network or environment access.
It does not install, enable, import, build or test the candidate it describes.
"""

from __future__ import annotations

from importlib import resources
from types import MappingProxyType

from traceh.plugins import (
    EffectKind,
    PluginContext,
    PluginManifest,
    PromptSection,
    ToolExecutionContext,
    ToolOutput,
)

PLUGIN_ID = "traceh.plugin.creator"
PLUGIN_VERSION = "0.2.0"
GUIDE_TOOL_NAME = "traceh_plugin_creator_guide"

_GUIDE_RESOURCES = MappingProxyType(
    {
        "workflow": "SKILL.md",
        "contract": "references/plugin-contract.md",
        "template": "references/package-template.md",
        "checklist": "references/candidate-checklist.md",
    }
)
GUIDE_TOPICS = tuple(_GUIDE_RESOURCES)


def _resource_text(relative_path: str) -> str:
    target = resources.files(__package__)
    for part in relative_path.split("/"):
        target = target.joinpath(part)
    return target.read_text(encoding="utf-8")


def read_guide(topic: str) -> str:
    """Read one immutable authoring guide from this installed distribution."""

    resource = _GUIDE_RESOURCES.get(topic)
    if resource is None:
        choices = ", ".join(GUIDE_TOPICS)
        raise ValueError(f"unknown guide topic; expected one of: {choices}")
    return _resource_text(resource)


class PluginCreatorGuideTool:
    """Return one part of the packaged source-only plugin-authoring contract."""

    name = GUIDE_TOOL_NAME
    description = (
        "Read one packaged part of the TraceHarness plugin-candidate authoring skill. "
        "This is a pure read: it writes no files, runs no command, installs nothing and "
        "uses no network or environment variable."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "enum": list(GUIDE_TOPICS),
                "description": "Guide section to read.",
            }
        },
        "required": ["topic"],
        "additionalProperties": False,
    }
    effect_kind = EffectKind.PURE_READ

    async def execute(
        self,
        arguments: dict[str, object],
        context: ToolExecutionContext,
    ) -> ToolOutput:
        del context
        topic = arguments.get("topic")
        if not isinstance(topic, str):
            raise ValueError("topic must be a string")
        document = read_guide(topic)
        resource = _GUIDE_RESOURCES[topic]
        return ToolOutput(
            content=document,
            data={"topic": topic, "resource": resource},
            evidence=(f"packaged resource {__package__}/{resource}",),
        )


class PluginCreatorSkillPlugin:
    """Entry point for the source-only plugin-candidate authoring skill."""

    manifest = PluginManifest(
        plugin_id=PLUGIN_ID,
        version=PLUGIN_VERSION,
        requires_traceh=">=0.6,<0.7",
        allowed_scopes=("application",),
        trust_mode="trusted",
        provides=("plugin.authoring.skill",),
    )

    async def setup(self, context: PluginContext, config: dict[str, object]) -> None:
        del config
        context.register_prompt(
            PromptSection(
                "traceh.plugin.creator",
                (
                    "A source-only TraceHarness Plugin Creator Skill is available. Use it "
                    "only when the user explicitly asks for a TraceHarness plugin candidate. "
                    f"First call {GUIDE_TOOL_NAME} for workflow, contract, template and "
                    "checklist guidance. Work only in a dedicated candidate workspace; do "
                    "not install, enable, import, build, test or execute the candidate, do "
                    "not read secrets, and do not modify the TraceHarness core repository."
                ),
                priority=30,
            )
        )
        context.register_tool(PluginCreatorGuideTool())

    async def health_check(self, context: PluginContext) -> bool:
        del context
        return all(read_guide(topic).strip() for topic in GUIDE_TOPICS)


__all__ = [
    "GUIDE_TOOL_NAME",
    "GUIDE_TOPICS",
    "PLUGIN_ID",
    "PLUGIN_VERSION",
    "PluginCreatorGuideTool",
    "PluginCreatorSkillPlugin",
    "read_guide",
]
