"""Explicit data/limits for F1 tests, never production defaults."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

from plugin_fixtures import entry_point_for, provider_for

from traceh.api.plugins import PluginIdentity
from traceh.api.skills import (
    SkillChunk,
    SkillContribution,
    SkillDescriptor,
    SkillLimits,
    SkillPolicy,
    SkillResource,
    SkillSection,
    SkillSectionContent,
)
from traceh.kernel.registry import ServiceRegistry
from traceh.plugins.discovery import PluginDiscovery
from traceh.plugins.manager import PluginGenerationBuilder
from traceh.runtime.prompt import PromptAssembler
from traceh.tools.registry import ToolRegistry
from traceh.version import DEFAULT_REQUIRES_TRACEH


def digest(body: str | bytes) -> str:
    return hashlib.sha256(body.encode("utf-8") if isinstance(body, str) else body).hexdigest()


def contribution(plugin_id: str, skill_id: str, body: str, *, version: str = "1.0.0"):
    return SkillContribution(
        SkillDescriptor(
            skill_id,
            version,
            PluginIdentity(plugin_id, version),
            "Fixture title",
            "Fixture summary",
            ("fixture",),
            DEFAULT_REQUIRES_TRACEH,
            (
                SkillSection(
                    "guide",
                    "section",
                    digest(body),
                    len(body.encode("utf-8")),
                    title="Fixture navigation",
                    summary="Explicit fixture content description",
                ),
            ),
            (),
        ),
        (SkillSectionContent("guide", body),),
    )


def with_resource(value: SkillContribution, relative: str, body: str) -> SkillContribution:
    data = body.encode("utf-8")
    resource = SkillResource(
        "reference",
        relative,
        digest(data),
        len(data),
        (
            SkillChunk(
                "whole",
                0,
                len(data),
                digest(data),
                len(data),
                title="Fixture navigation",
                summary="Explicit fixture content description",
            ),
        ),
        title="Fixture navigation",
        summary="Explicit fixture content description",
    )
    return replace(value, descriptor=replace(value.descriptor, resources=(resource,)))


def policy(*roots, **limits) -> SkillPolicy:
    return SkillPolicy(
        SkillLimits(
            **{
                "max_skills": 8,
                "max_catalog_bytes": 16_000,
                "max_summary_bytes": 300,
                "max_content_bytes": 8_000,
                "max_resource_bytes": 4_000,
                **limits,
            }
        ),
        tuple(roots),
    )


def discovery(*plugins) -> PluginDiscovery:
    return PluginDiscovery(
        entry_points_provider=provider_for(*(entry_point_for(plugin) for plugin in plugins))
    )


def builder(*plugins, skill_policy: SkillPolicy | None) -> PluginGenerationBuilder:
    return PluginGenerationBuilder(
        tools=ToolRegistry(),
        prompt=PromptAssembler(),
        services=ServiceRegistry(),
        discovery=discovery(*plugins),
        skill_policy=skill_policy,
    )


def lease(runtime, workspace: Path):
    return runtime.loop.compositions.lease(
        workspace=workspace,
        session_id="fixture-session",
        turn_id="fixture-turn",
        step_id="fixture-step",
    )
