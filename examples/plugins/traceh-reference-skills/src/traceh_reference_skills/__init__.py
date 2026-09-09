"""Explicit example data, never core defaults; no Tool or prompt grants."""

from hashlib import sha256

from traceh.api.plugins import PluginIdentity, PluginManifest
from traceh.api.skills import SkillContribution, SkillDescriptor, SkillSection, SkillSectionContent


def contribution(plugin_id, skill_id, summary, body):
    data = body.encode("utf-8")
    return SkillContribution(
        SkillDescriptor(
            skill_id,
            "0.1.0",
            PluginIdentity(plugin_id, "0.1.0"),
            skill_id,
            summary,
            ("reference",),
            ">=0.8,<1.0",
            (
                SkillSection(
                    "guide",
                    "section",
                    sha256(data).hexdigest(),
                    len(data),
                    title=summary,
                    summary=summary,
                ),
            ),
            (),
        ),
        (SkillSectionContent("guide", body),),
    )


class CurrentPlugin:
    manifest = PluginManifest(
        plugin_id="traceh.reference.current",
        version="0.1.0",
        requires_traceh=">=0.8,<1.0",
        allowed_scopes=("application",),
        trust_mode="trusted",
        provides=("reference.current",),
    )
    skill = contribution(
        manifest.plugin_id,
        "reference.current",
        "项目阶段与目标查询规范",
        "Use approved project facts for the current delivery phase. "
        "Keep source provenance visible.",
    )

    async def setup(self, context, config):
        del config
        context.register_skill(self.skill)


class RetiredPlugin:
    manifest = PluginManifest(
        plugin_id="traceh.reference.retired",
        version="0.1.0",
        requires_traceh=">=0.8,<1.0",
        allowed_scopes=("application",),
        trust_mode="trusted",
        provides=("reference.retired",),
    )
    skill = contribution(
        manifest.plugin_id,
        "reference.retired",
        "Archived integration instructions",
        "Obsolete instructions must disappear when this plugin is retired.",
    )

    async def setup(self, context, config):
        del config
        context.register_skill(self.skill)
