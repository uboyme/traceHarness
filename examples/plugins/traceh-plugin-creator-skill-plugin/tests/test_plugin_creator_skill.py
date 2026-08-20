from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from traceh.plugins import EffectKind, ToolExecutionContext

from traceh_plugin_creator_skill import (
    GUIDE_TOOL_NAME,
    PLUGIN_ID,
    PLUGIN_VERSION,
    PluginCreatorGuideTool,
    PluginCreatorSkillPlugin,
    read_guide,
)


class RecordingContext:
    def __init__(self) -> None:
        self.prompts: list[object] = []
        self.tools: list[object] = []

    def register_prompt(self, section: object) -> object:
        self.prompts.append(section)
        return object()

    def register_tool(self, tool: object) -> object:
        self.tools.append(tool)
        return object()


def test_manifest_and_entry_point_use_one_identity() -> None:
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    entry_points = project["project"]["entry-points"]["traceh.plugins"]

    assert PluginCreatorSkillPlugin.manifest.plugin_id == PLUGIN_ID
    assert PluginCreatorSkillPlugin.manifest.version == PLUGIN_VERSION
    assert entry_points == {
        PLUGIN_ID: "traceh_plugin_creator_skill:PluginCreatorSkillPlugin"
    }
    assert project["project"]["dependencies"] == ["traceharness-py>=0.5,<0.6"]


@pytest.mark.parametrize(
    ("topic", "contract_marker"),
    [
        ("workflow", "UNVALIDATED (L1 SOURCE ONLY)"),
        ("contract", "Import author-facing values"),
        ("template", "<distribution-root>/"),
        ("checklist", "Do not build, import, install, enable or execute it"),
    ],
)
def test_every_packaged_guide_is_nonempty_and_specific(
    topic: str,
    contract_marker: str,
) -> None:
    document = read_guide(topic)

    assert document.strip()
    assert contract_marker in document
    if topic == "contract":
        assert "`CompositionSnapshot` does not include Verifier identity" in document
        assert "persisted as `verification/result` evidence" in document


def test_unknown_topic_fails_closed() -> None:
    with pytest.raises(ValueError, match="unknown guide topic"):
        read_guide("unregistered-topic")


async def test_setup_registers_only_prompt_and_pure_read_guide() -> None:
    context = RecordingContext()

    await PluginCreatorSkillPlugin().setup(context, {})  # type: ignore[arg-type]

    assert len(context.prompts) == 1
    assert len(context.tools) == 1
    tool = context.tools[0]
    assert isinstance(tool, PluginCreatorGuideTool)
    assert tool.name == GUIDE_TOOL_NAME
    assert tool.effect_kind is EffectKind.PURE_READ
    assert "do not install" in str(context.prompts[0]).lower()


async def test_guide_tool_returns_bounded_topic_identity(tmp_path: Path) -> None:
    tool = PluginCreatorGuideTool()
    context = ToolExecutionContext("s", "t", "p", "c", tmp_path, tmp_path / "data")

    result = await tool.execute({"topic": "contract"}, context)

    assert result.data == {
        "topic": "contract",
        "resource": "references/plugin-contract.md",
    }
    assert "traceh.plugins" in result.content
    assert str(tmp_path) not in result.content
    assert result.evidence == (
        "packaged resource traceh_plugin_creator_skill/references/plugin-contract.md",
    )


async def test_guide_tool_rejects_non_string_topic(tmp_path: Path) -> None:
    tool = PluginCreatorGuideTool()
    context = ToolExecutionContext("s", "t", "p", "c", tmp_path, tmp_path / "data")

    with pytest.raises(ValueError, match="topic must be a string"):
        await tool.execute({"topic": 7}, context)


async def test_health_check_reads_all_packaged_resources() -> None:
    assert await PluginCreatorSkillPlugin().health_check(RecordingContext()) is True  # type: ignore[arg-type]
