"""Plugins on the real mainline: composition, session identity, replay, dispose.

These tests use the Scripted Provider, so they need no API key and call no model
service, but they run the actual AgentLoop, ToolRuntime and event log.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from plugin_fixtures import RecordingTool, ScriptedPlugin, entry_point_for, manifest, provider_for

from traceh.api.llm import ModelResponse, ToolCall
from traceh.api.plugins import CORE_PLUGIN_IDENTITY
from traceh.api.prompts import PromptSection
from traceh.llm.registry import LlmRegistry
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.plugins.discovery import PluginDiscovery
from traceh.runtime.agent_runtime import (
    RuntimeConfig,
    SessionPluginMismatchError,
    build_default_runtime,
    build_default_runtime_async,
)
from traceh.runtime.composition_runtime import CompositionGeneration, CompositionResourceOwner
from traceh.runtime.prompt import PromptAssembler
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.sqlite import SqliteEventStore
from traceh.tools.registry import ToolRegistry
from traceh.tools.runtime import ToolRuntime
from traceh.version import __version__


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    directory = tmp_path / "ws"
    directory.mkdir()
    (directory / "hello.txt").write_text("hi", encoding="utf-8")
    return directory


def discovery_for(*plugins):
    return PluginDiscovery(
        entry_points_provider=provider_for(*(entry_point_for(p) for p in plugins))
    )


def example_plugin(tool_name: str = "plugin_tool") -> ScriptedPlugin:
    return ScriptedPlugin(
        manifest("a.example", version="2.0.0"),
        tools=(RecordingTool(tool_name, content="plugin tool output"),),
        prompts=(PromptSection("a.example.section", "Plugin guidance section.", 40),),
    )


async def build(tmp_path: Path, plugins=(), enabled=(), provider=None):
    return await build_default_runtime_async(
        RuntimeConfig(data_dir=tmp_path / "data"),
        provider=provider,
        enabled_plugins=enabled,
        plugin_discovery=discovery_for(*plugins) if plugins else None,
        event_store=SqliteEventStore((tmp_path / "data") / "events"),
    )


# --------------------------------------------------------------------------
# The no-plugin path must be untouched
# --------------------------------------------------------------------------


async def test_without_plugins_prompt_and_tools_are_unchanged(
    tmp_path: Path, workspace: Path
) -> None:
    plain = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "plain"),
        event_store=SqliteEventStore((tmp_path / "plain") / "events"),
    )
    through = await build(tmp_path / "async")

    plain_composition = plain.loop.compositions
    through_composition = through.loop.compositions
    assert plain_composition.tools.registry.names() == through_composition.tools.registry.names()
    assert plain_composition.prompt.assemble(workspace=str(workspace)) == (
        through_composition.prompt.assemble(workspace=str(workspace))
    )
    assert through.plugins == (CORE_PLUGIN_IDENTITY,)
    assert through.enabled_plugin_ids == ()


async def test_runtime_dispose_drains_generation_before_plugin_manager(
    tmp_path: Path,
) -> None:
    record: list[str] = []
    plugin = ScriptedPlugin(
        manifest("order.example"),
        tools=(RecordingTool("order_tool"),),
        record=record,
    )
    runtime = await build(tmp_path, plugins=(plugin,), enabled=("order.example",))
    compositions = runtime.loop.compositions
    initial = compositions.current_generation

    async def generation_cleanup() -> None:
        record.append("cleanup:generation")

    llms = LlmRegistry()
    llms.register(
        ScriptedLlmProvider(
            (ModelResponse(content="unused"),),
            repeat_last=True,
        )
    )
    replacement_tools = ToolRegistry()
    replacement_tools.register(RecordingTool("order_tool"))
    replacement_runtime = ToolRuntime(
        replacement_tools,
        initial.tools.sessions,
        policies=(),
        middlewares=(),
        timeout_seconds=initial.tools.timeout_seconds,
        max_output_chars=initial.tools.max_output_chars,
    )
    replacement = CompositionGeneration(
        llms=llms,
        tools=replacement_runtime,
        prompt=PromptAssembler(initial.prompt.sections()),
        provider=initial.provider,
        model=initial.model,
        temperature=initial.temperature,
        max_output_tokens=initial.max_output_tokens,
        plugins=initial.plugins,
        resource_owner=CompositionResourceOwner(generation_cleanup),
        cleanup=generation_cleanup,
    )
    await compositions.publish(replacement)

    await runtime.dispose()

    assert record == [
        "setup:order.example",
        "cleanup:generation",
        "cleanup:order.example",
    ]


async def test_installed_but_not_enabled_plugin_changes_nothing(
    tmp_path: Path, workspace: Path
) -> None:
    """Installation alone must not alter the default runtime in any way."""

    plugin = example_plugin()
    plain = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "plain"),
        event_store=SqliteEventStore((tmp_path / "plain") / "events"),
    )
    runtime = await build(tmp_path / "with", plugins=(plugin,), enabled=())

    assert plugin.setup_calls == 0
    assert runtime.loop.compositions.tools.registry.names() == (
        plain.loop.compositions.tools.registry.names()
    )
    assert "a.example.section" not in runtime.loop.compositions.prompt.section_ids()


# --------------------------------------------------------------------------
# Enabled plugins reach the model
# --------------------------------------------------------------------------


async def test_enabled_plugin_tool_and_prompt_are_visible_to_the_model(
    tmp_path: Path, workspace: Path
) -> None:
    plugin = example_plugin()
    runtime = await build(tmp_path, plugins=(plugin,), enabled=("a.example",))
    try:
        composition = runtime.loop.compositions
        assert "plugin_tool" in composition.tools.registry.names()
        prompt = composition.prompt.assemble(workspace=str(workspace))
        assert "Plugin guidance section." in prompt
        schema_names = [schema.name for schema in composition.tools.registry.schemas()]
        assert "plugin_tool" in schema_names
    finally:
        await runtime.dispose()


async def test_model_can_actually_call_the_plugin_tool(tmp_path: Path, workspace: Path) -> None:
    tool = RecordingTool("plugin_tool", content="plugin tool output")
    plugin = ScriptedPlugin(
        manifest("a.example", version="2.0.0"),
        tools=(tool,),
        prompts=(PromptSection("a.example.section", "Plugin guidance.", 40),),
    )
    provider = ScriptedLlmProvider(
        (
            ModelResponse(
                content="",
                tool_calls=(ToolCall(id="call-1", name="plugin_tool", arguments={}),),
            ),
            ModelResponse(content="Done using the plugin tool."),
        )
    )
    runtime = await build(tmp_path, plugins=(plugin,), enabled=("a.example",), provider=provider)
    try:
        result = await runtime.run(workspace, "use the plugin tool")
        assert result.reason == "completed"
        assert tool.calls == 1

        session_id = (await runtime.sessions.list_sessions())[0]
        events = await runtime.sessions.read_session(session_id)
        effects = await runtime.sessions.read_effects(session_id)

        calls = [e for e in events if e.type == "tool/call"]
        results = [e for e in events if e.type == "tool/result"]
        assert len(calls) == len(results) == 1
        assert calls[0].data["tool_name"] == "plugin_tool"
        assert "plugin tool output" in str(results[0].data)

        intents = [e for e in effects if e.type == "effect/intent"]
        outcomes = [e for e in effects if e.type == "effect/outcome"]
        assert len(intents) == len(outcomes) == 1

        violations = runtime.invariants.check(events, effects)
        assert violations == ()

        reconstruction = await verify_request_snapshots(
            runtime.sessions, runtime.surface, session_id
        )
        assert reconstruction == ()
    finally:
        await runtime.dispose()


async def test_composition_snapshot_records_real_plugin_identity(
    tmp_path: Path, workspace: Path
) -> None:
    plugin = example_plugin()
    runtime = await build(tmp_path, plugins=(plugin,), enabled=("a.example",))
    try:
        await runtime.run(workspace, "hello")
        session_id = (await runtime.sessions.list_sessions())[0]
        events = await runtime.sessions.read_session(session_id)
        snapshots = [e for e in events if e.type == "composition/snapshot"]
        assert snapshots
        for snapshot in snapshots:
            assert snapshot.data["plugins"] == [
                {"plugin_id": "traceh.core", "version": __version__},
                {"plugin_id": "a.example", "version": "2.0.0"},
            ]
    finally:
        await runtime.dispose()


async def test_reconstructed_composition_keeps_plugin_identities(
    tmp_path: Path, workspace: Path
) -> None:
    """Replay must rebuild the plugin list, not assume a plugin-free runtime."""

    from traceh.runtime.request_builder import composition_from_event

    plugin = example_plugin()
    runtime = await build(tmp_path, plugins=(plugin,), enabled=("a.example",))
    try:
        await runtime.run(workspace, "hello")
        session_id = (await runtime.sessions.list_sessions())[0]
        events = await runtime.sessions.read_session(session_id)
        snapshot = next(e for e in events if e.type == "composition/snapshot")

        rebuilt = composition_from_event(snapshot)

        assert [identity.plugin_id for identity in rebuilt.plugins] == [
            "traceh.core",
            "a.example",
        ]
        assert rebuilt.plugins[1].version == "2.0.0"
    finally:
        await runtime.dispose()


# --------------------------------------------------------------------------
# Session plugin identity
# --------------------------------------------------------------------------


async def test_session_records_external_plugin_identities(tmp_path: Path, workspace: Path) -> None:
    plugin = example_plugin()
    runtime = await build(tmp_path, plugins=(plugin,), enabled=("a.example",))
    try:
        session_id = await runtime.create_session(workspace)
        events = await runtime.sessions.read_session(session_id)
        metadata = events[0].data["metadata"]
        assert metadata["traceh_plugins"] == [{"plugin_id": "a.example", "version": "2.0.0"}]
    finally:
        await runtime.dispose()


async def test_plugin_free_session_records_an_empty_list(tmp_path: Path, workspace: Path) -> None:
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data"),
        event_store=SqliteEventStore((tmp_path / "data") / "events"),
    )
    session_id = await runtime.create_session(workspace)
    events = await runtime.sessions.read_session(session_id)
    assert events[0].data["metadata"]["traceh_plugins"] == []


async def test_matching_composition_can_continue_a_session(tmp_path: Path, workspace: Path) -> None:
    data_dir = tmp_path / "data"
    first = await build_default_runtime_async(
        RuntimeConfig(data_dir=data_dir),
        enabled_plugins=("a.example",),
        plugin_discovery=discovery_for(example_plugin()),
        event_store=SqliteEventStore((data_dir) / "events"),
    )
    try:
        session_id = await first.create_session(workspace)
        await first.run_existing(session_id, "one")
    finally:
        await first.dispose()

    second = await build_default_runtime_async(
        RuntimeConfig(data_dir=data_dir),
        enabled_plugins=("a.example",),
        plugin_discovery=discovery_for(example_plugin()),
        event_store=SqliteEventStore((data_dir) / "events"),
    )
    try:
        await second.verify_session_plugins(session_id)
        result = await second.run_existing(session_id, "two")
        assert result.reason == "completed"
    finally:
        await second.dispose()


async def test_dropping_a_plugin_refuses_to_continue_the_session(
    tmp_path: Path, workspace: Path
) -> None:
    data_dir = tmp_path / "data"
    first = await build_default_runtime_async(
        RuntimeConfig(data_dir=data_dir),
        enabled_plugins=("a.example",),
        plugin_discovery=discovery_for(example_plugin()),
        event_store=SqliteEventStore((data_dir) / "events"),
    )
    try:
        session_id = await first.create_session(workspace)
    finally:
        await first.dispose()

    plain = build_default_runtime(
        RuntimeConfig(data_dir=data_dir), event_store=SqliteEventStore((data_dir) / "events")
    )
    with pytest.raises(SessionPluginMismatchError) as info:
        await plain.run_existing(session_id, "continue")
    assert "a.example==2.0.0" in str(info.value)
    assert "none" in str(info.value)


async def test_adding_a_plugin_refuses_to_continue_the_session(
    tmp_path: Path, workspace: Path
) -> None:
    data_dir = tmp_path / "data"
    plain = build_default_runtime(
        RuntimeConfig(data_dir=data_dir), event_store=SqliteEventStore((data_dir) / "events")
    )
    session_id = await plain.create_session(workspace)

    with_plugin = await build_default_runtime_async(
        RuntimeConfig(data_dir=data_dir),
        enabled_plugins=("a.example",),
        plugin_discovery=discovery_for(example_plugin()),
        event_store=SqliteEventStore((data_dir) / "events"),
    )
    try:
        with pytest.raises(SessionPluginMismatchError):
            await with_plugin.run_existing(session_id, "continue")
    finally:
        await with_plugin.dispose()


async def test_changed_plugin_version_refuses_to_continue_the_session(
    tmp_path: Path, workspace: Path
) -> None:
    data_dir = tmp_path / "data"
    first = await build_default_runtime_async(
        RuntimeConfig(data_dir=data_dir),
        enabled_plugins=("a.example",),
        plugin_discovery=discovery_for(example_plugin()),
        event_store=SqliteEventStore((data_dir) / "events"),
    )
    try:
        session_id = await first.create_session(workspace)
    finally:
        await first.dispose()

    upgraded = ScriptedPlugin(
        manifest("a.example", version="3.0.0"),
        tools=(RecordingTool("plugin_tool"),),
        prompts=(PromptSection("a.example.section", "Plugin guidance section.", 40),),
    )
    second = await build_default_runtime_async(
        RuntimeConfig(data_dir=data_dir),
        enabled_plugins=("a.example",),
        plugin_discovery=discovery_for(upgraded),
        event_store=SqliteEventStore((data_dir) / "events"),
    )
    try:
        with pytest.raises(SessionPluginMismatchError) as info:
            await second.run_existing(session_id, "continue")
        assert "a.example==2.0.0" in str(info.value)
        assert "a.example==3.0.0" in str(info.value)
    finally:
        await second.dispose()


async def test_pre_v04_session_without_the_key_is_treated_as_plugin_free(
    tmp_path: Path, workspace: Path
) -> None:
    """Sessions written before v0.4 have no key; that must read as 'no plugins'."""

    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data"),
        event_store=SqliteEventStore((tmp_path / "data") / "events"),
    )
    session_id = await runtime.sessions.create_session(workspace, metadata={"cli": True})

    await runtime.verify_session_plugins(session_id)
    result = await runtime.run_existing(session_id, "continue")
    assert result.reason == "completed"


async def test_malformed_plugin_metadata_is_rejected(tmp_path: Path, workspace: Path) -> None:
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data"),
        event_store=SqliteEventStore((tmp_path / "data") / "events"),
    )
    session_id = await runtime.sessions.create_session(
        workspace, metadata={"traceh_plugins": "not-a-list"}
    )
    with pytest.raises(SessionPluginMismatchError):
        await runtime.verify_session_plugins(session_id)


async def test_reserved_metadata_key_cannot_be_supplied_by_callers(
    tmp_path: Path, workspace: Path
) -> None:
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data"),
        event_store=SqliteEventStore((tmp_path / "data") / "events"),
    )
    with pytest.raises(ValueError, match="reserved"):
        await runtime.create_session(workspace, metadata={"traceh_plugins": [{"a": "b"}]})


# --------------------------------------------------------------------------
# Runtime dispose ordering
# --------------------------------------------------------------------------


async def test_runtime_dispose_unloads_plugins(tmp_path: Path, workspace: Path) -> None:
    plugin = example_plugin()
    runtime = await build(tmp_path, plugins=(plugin,), enabled=("a.example",))
    registry = runtime.loop.compositions.tools.registry
    assert "plugin_tool" in registry.names()

    await runtime.dispose()

    assert "plugin_tool" not in registry.names()
    assert plugin.cleanup_calls == 1


async def test_runtime_dispose_is_idempotent(tmp_path: Path, workspace: Path) -> None:
    plugin = example_plugin()
    runtime = await build(tmp_path, plugins=(plugin,), enabled=("a.example",))

    await runtime.dispose()
    await runtime.dispose()

    assert plugin.cleanup_calls == 1


async def test_runtime_dispose_converges_the_turn_before_unloading(
    tmp_path: Path, workspace: Path
) -> None:
    """Unloading first would pull a tool out from under a running turn."""

    plugin = ScriptedPlugin(
        manifest("a.example", version="2.0.0"),
        tools=(RecordingTool("plugin_tool"),),
        spawn_forever=True,
    )
    runtime = await build(tmp_path, plugins=(plugin,), enabled=("a.example",))
    await plugin.owned_task_started.wait()

    await runtime.dispose()

    assert plugin.owned_task is not None and plugin.owned_task.done()
    assert runtime.loop.compositions.tools.registry.names() == (
        "apply_patch",
        "list_files",
        "read_file",
        "search_text",
        "shell",
    )
