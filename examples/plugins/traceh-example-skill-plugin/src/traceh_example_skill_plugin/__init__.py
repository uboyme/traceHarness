"""Example TraceHarness plugin distribution.

This is a real, separately built distribution, not a test fixture living inside
the harness. It exists so the plugin path can be exercised the way an external
author would actually meet it: build a wheel, install it into a clean
environment, let ``importlib.metadata`` find the entry point, and enable it
explicitly.

It contributes exactly two things, both harmless:

* one :class:`PromptSection`, derived from a packaged ``SKILL.md`` resource;
* one read-only :class:`Tool` that reports that same packaged resource.

It touches no user directory, no environment variable and no network, and it is
never enabled by installation alone.
"""

from __future__ import annotations

from importlib import resources

from traceh.plugins import (
    EffectKind,
    PluginContext,
    PluginManifest,
    PromptSection,
    Tool,
    ToolExecutionContext,
    ToolOutput,
)

PLUGIN_ID = "traceh.example.skill"
SKILL_RESOURCE = "SKILL.md"


def read_skill_document() -> str:
    """Read the packaged skill document from the installed distribution.

    Uses ``importlib.resources`` rather than ``__file__`` arithmetic so the
    plugin works identically from a wheel, a zipimport and a source checkout.
    """

    return resources.files(__package__).joinpath(SKILL_RESOURCE).read_text(encoding="utf-8")


def skill_title(document: str) -> str:
    for line in document.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return "Example Skill"


class ExampleSkillTool:
    """Report the packaged skill document. Pure read: no workspace, no process."""

    name = "example_skill_info"
    description = (
        "Return the example plugin's packaged skill document. Reads no workspace "
        "file, runs no process and takes no arguments."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    # PURE_READ, not WORKSPACE_READ: the content comes from the installed
    # distribution, so it neither touches nor depends on the workspace.
    effect_kind = EffectKind.PURE_READ

    def __init__(self, document: str) -> None:
        self._document = document

    async def execute(
        self,
        arguments: dict[str, object],
        context: ToolExecutionContext,
    ) -> ToolOutput:
        del arguments, context
        return ToolOutput(
            content=self._document,
            data={"title": skill_title(self._document), "resource": SKILL_RESOURCE},
            evidence=(f"packaged resource {__package__}/{SKILL_RESOURCE}",),
        )


class ExampleSkillPlugin:
    """Entry point object for ``traceh.example.skill``."""

    manifest = PluginManifest(
        plugin_id=PLUGIN_ID,
        version="0.1.0",
        requires_traceh=">=0.4,<1.0",
        allowed_scopes=("application",),
        trust_mode="trusted",
        provides=("example.skill",),
    )

    async def setup(self, context: PluginContext, config: dict[str, object]) -> None:
        del config
        document = read_skill_document()
        # Both registrations return reversible Registrations owned by this
        # plugin's Activation, so a later failure unwinds them automatically.
        context.register_prompt(
            PromptSection(
                "traceh.example.skill",
                (
                    f"An example plugin skill named {skill_title(document)!r} is available. "
                    "Call example_skill_info to read its packaged document."
                ),
                priority=40,
            )
        )
        context.register_tool(ExampleSkillTool(document))

    async def health_check(self, context: PluginContext) -> bool:
        del context
        # Cheap and side-effect free: confirm the packaged resource is readable.
        return bool(read_skill_document().strip())


__all__ = [
    "PLUGIN_ID",
    "SKILL_RESOURCE",
    "ExampleSkillPlugin",
    "ExampleSkillTool",
    "read_skill_document",
    "skill_title",
]
