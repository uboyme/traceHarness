"""The activation transaction: load, order, setup, conflict, health, publish, rollback."""

from __future__ import annotations

import pytest
from plugin_fixtures import (
    FakeDistribution,
    FakeEntryPoint,
    LoadCounter,
    RecordingTool,
    ScriptedPlugin,
    entry_point_for,
    manifest,
    provider_for,
)

from traceh.api.plugins import CORE_PLUGIN_IDENTITY, PluginDependency, PluginManifest
from traceh.api.prompts import PromptSection
from traceh.api.services import ServiceKey
from traceh.kernel.registry import ServiceRegistry
from traceh.plugins.discovery import PluginDiscovery
from traceh.plugins.errors import PluginActivationError, PluginValidationError
from traceh.plugins.manager import PluginManager
from traceh.runtime.prompt import PromptAssembler, default_coding_prompt
from traceh.tools.registry import ToolRegistry
from traceh.version import __version__


def build_manager(
    *points: FakeEntryPoint,
    tools: ToolRegistry | None = None,
    prompt: PromptAssembler | None = None,
    services: ServiceRegistry | None = None,
    plugin_configs=None,
) -> PluginManager:
    return PluginManager(
        tools=tools if tools is not None else ToolRegistry(),
        prompt=prompt if prompt is not None else PromptAssembler(),
        services=services,
        discovery=PluginDiscovery(entry_points_provider=provider_for(*points)),
        plugin_configs=plugin_configs,
    )


# --------------------------------------------------------------------------
# Loading and explicit enablement
# --------------------------------------------------------------------------


async def test_only_explicitly_enabled_plugins_are_imported() -> None:
    counter = LoadCounter()
    wanted = ScriptedPlugin(manifest("a.wanted"))
    unwanted = ScriptedPlugin(manifest("b.unwanted"))
    manager = build_manager(
        entry_point_for(wanted, counter=counter),
        entry_point_for(unwanted, counter=counter),
    )

    await manager.activate(["a.wanted"])

    assert counter.loaded == ["a.wanted"]
    assert unwanted.setup_calls == 0


async def test_empty_selection_never_touches_discovery() -> None:
    counter = LoadCounter()
    plugin = ScriptedPlugin(manifest("a.plugin"))
    manager = build_manager(entry_point_for(plugin, counter=counter))

    identities = await manager.activate([])

    assert identities == (CORE_PLUGIN_IDENTITY,)
    assert counter.loaded == []


async def test_enabling_an_uninstalled_plugin_fails() -> None:
    manager = build_manager()
    with pytest.raises(PluginValidationError) as info:
        await manager.activate(["missing.plugin"])
    assert [f.code for f in info.value.failures] == ["plugin-not-discovered"]


async def test_discovery_issues_block_loading() -> None:
    plugin = ScriptedPlugin(manifest("a.plugin"))
    point = entry_point_for(plugin, dist=FakeDistribution(requires=("requests>=2",)))
    counter = LoadCounter()
    point.counter = counter
    manager = build_manager(point)

    with pytest.raises(PluginValidationError) as info:
        await manager.activate(["a.plugin"])

    assert "traceh-dependency-missing" in [f.code for f in info.value.failures]
    assert counter.loaded == [], "a plugin with metadata issues must not be imported"


async def test_import_failure_does_not_leak_exception_text() -> None:
    point = FakeEntryPoint(
        name="a.plugin",
        load_error=RuntimeError("boom token=sk-FAKE-FIXTURE"),
    )
    manager = build_manager(point)

    with pytest.raises(PluginValidationError) as info:
        await manager.activate(["a.plugin"])

    failures = info.value.failures
    assert [f.code for f in failures] == ["plugin-load-failed"]
    assert "sk-FAKE-FIXTURE" not in "".join(f.message for f in failures)
    assert "sk-FAKE-FIXTURE" not in str(info.value)


async def test_plugin_class_entry_point_is_instantiated() -> None:
    class PluginClass:
        manifest = PluginManifest("a.plugin", "1.0.0")

        async def setup(self, context, config) -> None:
            del context, config

    manager = build_manager(FakeEntryPoint(name="a.plugin", target=PluginClass))
    identities = await manager.activate(["a.plugin"])
    assert identities[1].plugin_id == "a.plugin"


async def test_object_without_setup_is_rejected() -> None:
    class NoSetup:
        manifest = PluginManifest("a.plugin", "1.0.0")

    manager = build_manager(FakeEntryPoint(name="a.plugin", target=NoSetup()))
    with pytest.raises(PluginValidationError) as info:
        await manager.activate(["a.plugin"])
    assert "plugin-setup-missing" in [f.code for f in info.value.failures]


async def test_duplicate_entry_point_name_blocks_activation() -> None:
    plugin = ScriptedPlugin(manifest("dup"))
    manager = build_manager(
        FakeEntryPoint(name="dup", value="one:P", target=plugin, dist=FakeDistribution("one")),
        FakeEntryPoint(name="dup", value="two:P", target=plugin, dist=FakeDistribution("two")),
    )
    with pytest.raises(PluginValidationError) as info:
        await manager.activate(["dup"])
    assert "duplicate-entry-point" in [f.code for f in info.value.failures]


# --------------------------------------------------------------------------
# Dependencies
# --------------------------------------------------------------------------


async def test_required_dependency_sets_up_first() -> None:
    order: list[str] = []
    base = ScriptedPlugin(manifest("a.base"), record=order)
    dependent = ScriptedPlugin(
        manifest("b.dependent", requires_plugins=(PluginDependency("a.base", ">=1.0"),)),
        record=order,
    )
    manager = build_manager(entry_point_for(base), entry_point_for(dependent))

    await manager.activate(["b.dependent", "a.base"])

    assert manager.activation_order == ("a.base", "b.dependent")
    assert order == ["setup:a.base", "setup:b.dependent"]


async def test_required_dependency_that_is_not_enabled_fails() -> None:
    """Installed is not enabled: a plugin cannot enable its dependency for you."""

    base = ScriptedPlugin(manifest("a.base"))
    dependent = ScriptedPlugin(
        manifest("b.dependent", requires_plugins=(PluginDependency("a.base", ">=1.0"),))
    )
    manager = build_manager(entry_point_for(base), entry_point_for(dependent))

    with pytest.raises(PluginValidationError) as info:
        await manager.activate(["b.dependent"])

    assert [f.code for f in info.value.failures] == ["required-plugin-missing"]


async def test_incompatible_dependency_version_fails() -> None:
    base = ScriptedPlugin(manifest("a.base", version="1.0.0"))
    dependent = ScriptedPlugin(
        manifest("b.dependent", requires_plugins=(PluginDependency("a.base", ">=2.0"),))
    )
    manager = build_manager(entry_point_for(base), entry_point_for(dependent))

    with pytest.raises(PluginValidationError) as info:
        await manager.activate(["a.base", "b.dependent"])

    assert "plugin-version-incompatible" in [f.code for f in info.value.failures]


async def test_core_dependency_is_checked_against_the_single_version() -> None:
    dependency = PluginDependency("traceh.core", f"=={__version__}")
    ok = ScriptedPlugin(manifest("a.plugin", requires_plugins=(dependency,)))
    manager = build_manager(entry_point_for(ok))
    identities = await manager.activate(["a.plugin"])
    assert identities[0] == CORE_PLUGIN_IDENTITY


async def test_incompatible_core_dependency_fails() -> None:
    plugin = ScriptedPlugin(
        manifest("a.plugin", requires_plugins=(PluginDependency("traceh.core", ">=99.0"),))
    )
    manager = build_manager(entry_point_for(plugin))
    with pytest.raises(PluginValidationError) as info:
        await manager.activate(["a.plugin"])
    assert "plugin-version-incompatible" in [f.code for f in info.value.failures]


async def test_missing_optional_dependency_is_a_notice_not_a_failure() -> None:
    plugin = ScriptedPlugin(
        manifest("a.plugin", optional_plugins=(PluginDependency("b.absent", ">=1.0"),))
    )
    manager = build_manager(entry_point_for(plugin))

    identities = await manager.activate(["a.plugin"])

    assert identities[1].plugin_id == "a.plugin"
    assert [notice.code for notice in manager.notices] == ["optional-plugin-missing"]


async def test_present_optional_dependency_with_wrong_version_fails() -> None:
    """Absent is fine; present-but-incompatible is a real conflict."""

    base = ScriptedPlugin(manifest("a.base", version="1.0.0"))
    plugin = ScriptedPlugin(
        manifest("b.plugin", optional_plugins=(PluginDependency("a.base", ">=2.0"),))
    )
    manager = build_manager(entry_point_for(base), entry_point_for(plugin))

    with pytest.raises(PluginValidationError) as info:
        await manager.activate(["a.base", "b.plugin"])

    assert "optional-plugin-version-incompatible" in [f.code for f in info.value.failures]


async def test_dependency_cycle_is_detected() -> None:
    one = ScriptedPlugin(manifest("a.one", requires_plugins=(PluginDependency("b.two", ">=1.0"),)))
    two = ScriptedPlugin(manifest("b.two", requires_plugins=(PluginDependency("a.one", ">=1.0"),)))
    manager = build_manager(entry_point_for(one), entry_point_for(two))

    with pytest.raises(PluginValidationError) as info:
        await manager.activate(["a.one", "b.two"])

    assert {f.code for f in info.value.failures} == {"plugin-dependency-cycle"}
    assert one.setup_calls == 0 and two.setup_calls == 0


async def test_two_plugins_providing_the_same_capability_conflict() -> None:
    one = ScriptedPlugin(manifest("a.one", provides=("shared.cap",)))
    two = ScriptedPlugin(manifest("b.two", provides=("shared.cap",)))
    manager = build_manager(entry_point_for(one), entry_point_for(two))

    with pytest.raises(PluginValidationError) as info:
        await manager.activate(["a.one", "b.two"])

    assert "provides-conflict" in [f.code for f in info.value.failures]


async def test_activation_order_is_deterministic_for_independent_plugins() -> None:
    plugins = [ScriptedPlugin(manifest(f"p.{letter}")) for letter in "cab"]
    manager = build_manager(*(entry_point_for(plugin) for plugin in plugins))

    await manager.activate(["p.c", "p.a", "p.b"])

    assert manager.activation_order == ("p.a", "p.b", "p.c")


# --------------------------------------------------------------------------
# Contributions reaching the real mainlines
# --------------------------------------------------------------------------


async def test_tool_prompt_and_service_are_published() -> None:
    tools = ToolRegistry()
    prompt = PromptAssembler()
    services = ServiceRegistry()
    key = ServiceKey("example.service")
    tool = RecordingTool("plugin_tool")
    section = PromptSection("plugin.section", "hello", 40)
    plugin = ScriptedPlugin(
        manifest("a.plugin"), tools=(tool,), prompts=(section,), services=((key, "value"),)
    )
    manager = build_manager(
        entry_point_for(plugin), tools=tools, prompt=prompt, services=services
    )

    await manager.activate(["a.plugin"])

    assert tools.get("plugin_tool") is tool
    assert "plugin.section" in prompt.section_ids()
    assert services.get(key) == "value"
    assert "hello" in prompt.assemble(workspace="/ws")


async def test_nothing_is_published_before_activation_returns() -> None:
    """Staged setup must be invisible to the real registries until publish."""

    tools = ToolRegistry()
    observed: list[tuple[str, ...]] = []

    class Observing(ScriptedPlugin):
        async def setup(self, context, config):
            await super().setup(context, config)
            observed.append(tools.names())

    plugin = Observing(manifest("a.plugin"), tools=(RecordingTool("plugin_tool"),))
    manager = build_manager(entry_point_for(plugin), tools=tools)

    await manager.activate(["a.plugin"])

    assert observed == [()], "core registry saw a staged tool during setup"
    assert tools.names() == ("plugin_tool",)


async def test_dispose_removes_every_contribution() -> None:
    tools = ToolRegistry()
    prompt = PromptAssembler()
    services = ServiceRegistry()
    key = ServiceKey("example.service")
    plugin = ScriptedPlugin(
        manifest("a.plugin"),
        tools=(RecordingTool("plugin_tool"),),
        prompts=(PromptSection("plugin.section", "hello"),),
        services=((key, "value"),),
    )
    manager = build_manager(
        entry_point_for(plugin), tools=tools, prompt=prompt, services=services
    )
    await manager.activate(["a.plugin"])

    await manager.dispose()

    assert tools.names() == ()
    assert prompt.section_ids() == ()
    assert services.get(key) is None
    assert plugin.cleanup_calls == 1


async def test_dispose_runs_in_reverse_dependency_order() -> None:
    order: list[str] = []
    base = ScriptedPlugin(manifest("a.base"), record=order)
    dependent = ScriptedPlugin(
        manifest("b.dependent", requires_plugins=(PluginDependency("a.base", ">=1.0"),)),
        record=order,
    )
    manager = build_manager(entry_point_for(base), entry_point_for(dependent))
    await manager.activate(["a.base", "b.dependent"])
    order.clear()

    await manager.dispose()

    assert order == ["cleanup:b.dependent", "cleanup:a.base"]


async def test_one_failing_cleanup_does_not_skip_the_others() -> None:
    order: list[str] = []
    broken = ScriptedPlugin(
        manifest("a.broken"), cleanup_error=RuntimeError("cleanup failed"), record=order
    )
    healthy = ScriptedPlugin(manifest("b.healthy"), record=order)
    manager = build_manager(entry_point_for(broken), entry_point_for(healthy))
    await manager.activate(["a.broken", "b.healthy"])
    order.clear()

    with pytest.raises(Exception) as info:
        await manager.dispose()

    assert "cleanup:a.broken" in order and "cleanup:b.healthy" in order
    assert healthy.cleanup_calls == 1
    assert "plugin-cleanup-failed" in [f.code for f in info.value.failures]


# --------------------------------------------------------------------------
# Setup failure and rollback
# --------------------------------------------------------------------------


async def test_setup_failure_rolls_everything_back() -> None:
    tools = ToolRegistry()
    prompt = PromptAssembler()
    good = ScriptedPlugin(manifest("a.good"), tools=(RecordingTool("good_tool"),))
    bad = ScriptedPlugin(manifest("b.bad"), setup_error=RuntimeError("nope"))
    manager = build_manager(
        entry_point_for(good), entry_point_for(bad), tools=tools, prompt=prompt
    )

    with pytest.raises(PluginActivationError) as info:
        await manager.activate(["a.good", "b.bad"])

    assert [f.code for f in info.value.failures] == ["plugin-setup-failed"]
    assert tools.names() == (), "a failed activation must publish nothing"
    assert prompt.section_ids() == ()
    assert good.cleanup_calls == 1


async def test_setup_exception_text_is_not_exposed() -> None:
    bad = ScriptedPlugin(
        manifest("a.bad"), setup_error=RuntimeError("password=hunter2-FAKE-FIXTURE")
    )
    manager = build_manager(entry_point_for(bad))

    with pytest.raises(PluginActivationError) as info:
        await manager.activate(["a.bad"])

    rendered = str(info.value) + "".join(f.message for f in info.value.failures)
    assert "hunter2" not in rendered


async def test_setup_must_return_an_awaitable() -> None:
    class SyncSetup:
        manifest = PluginManifest("a.plugin", "1.0.0")

        def setup(self, context, config):
            del context, config
            return None

    manager = build_manager(FakeEntryPoint(name="a.plugin", target=SyncSetup()))
    with pytest.raises(PluginActivationError) as info:
        await manager.activate(["a.plugin"])
    assert [f.code for f in info.value.failures] == ["plugin-setup-failed"]


async def test_owned_tasks_are_cancelled_on_dispose() -> None:
    plugin = ScriptedPlugin(manifest("a.plugin"), spawn_forever=True)
    manager = build_manager(entry_point_for(plugin))
    await manager.activate(["a.plugin"])
    await plugin.owned_task_started.wait()
    assert plugin.owned_task is not None and not plugin.owned_task.done()

    await manager.dispose()

    assert plugin.owned_task.done()
    assert plugin.owned_task_cancelled


async def test_owned_tasks_are_cancelled_when_a_later_plugin_fails_setup() -> None:
    plugin = ScriptedPlugin(manifest("a.plugin"), spawn_forever=True)
    bad = ScriptedPlugin(manifest("b.bad"), setup_error=RuntimeError("nope"))
    manager = build_manager(entry_point_for(plugin), entry_point_for(bad))

    with pytest.raises(PluginActivationError):
        await manager.activate(["a.plugin", "b.bad"])

    assert plugin.owned_task is not None
    assert plugin.owned_task.done()
    assert plugin.owned_task_cancelled


# --------------------------------------------------------------------------
# Conflict detection, and its ordering relative to health checks
# --------------------------------------------------------------------------


async def test_tool_conflicting_with_a_core_tool_is_rejected() -> None:
    tools = ToolRegistry()
    tools.register(RecordingTool("read_file"))
    plugin = ScriptedPlugin(manifest("a.plugin"), tools=(RecordingTool("read_file"),))
    manager = build_manager(entry_point_for(plugin), tools=tools)

    with pytest.raises(PluginActivationError) as info:
        await manager.activate(["a.plugin"])

    assert "tool-publish-conflict" in [f.code for f in info.value.failures]


async def test_prompt_conflicting_with_a_core_section_is_rejected() -> None:
    prompt = default_coding_prompt()
    plugin = ScriptedPlugin(
        manifest("a.plugin"), prompts=(PromptSection("traceh.identity", "hijack"),)
    )
    manager = build_manager(entry_point_for(plugin), prompt=prompt)

    with pytest.raises(PluginActivationError) as info:
        await manager.activate(["a.plugin"])

    assert "prompt-publish-conflict" in [f.code for f in info.value.failures]
    assert "hijack" not in prompt.assemble(workspace="/ws")


async def test_service_conflicting_with_an_existing_service_is_rejected() -> None:
    services = ServiceRegistry()
    key = ServiceKey("example.service")
    await services.provide(key, "core-value")
    plugin = ScriptedPlugin(manifest("a.plugin"), services=((key, "plugin-value"),))
    manager = build_manager(entry_point_for(plugin), services=services)

    with pytest.raises(PluginActivationError) as info:
        await manager.activate(["a.plugin"])

    assert "service-publish-conflict" in [f.code for f in info.value.failures]
    assert services.get(key) == "core-value"


async def test_conflicting_plugin_health_check_is_never_called() -> None:
    """The ordering fix: a doomed plugin does not get to run its health check."""

    tools = ToolRegistry()
    tools.register(RecordingTool("read_file"))
    plugin = ScriptedPlugin(
        manifest("a.plugin"),
        tools=(RecordingTool("read_file"),),
        health_result=True,
        has_health_check=True,
    )
    manager = build_manager(entry_point_for(plugin), tools=tools)

    with pytest.raises(PluginActivationError) as info:
        await manager.activate(["a.plugin"])

    assert plugin.health_calls == 0, "health check ran despite a known conflict"
    assert "tool-publish-conflict" in [f.code for f in info.value.failures]
    assert tools.names() == ("read_file",)
    assert tools.get("read_file") is not plugin  # staged registration rolled back
    assert plugin.cleanup_calls == 1


async def test_conflict_rolls_back_every_staged_registration() -> None:
    tools = ToolRegistry()
    tools.register(RecordingTool("read_file"))
    innocent = ScriptedPlugin(manifest("a.innocent"), tools=(RecordingTool("innocent_tool"),))
    conflicting = ScriptedPlugin(manifest("b.conflicting"), tools=(RecordingTool("read_file"),))
    manager = build_manager(
        entry_point_for(innocent), entry_point_for(conflicting), tools=tools
    )

    with pytest.raises(PluginActivationError):
        await manager.activate(["a.innocent", "b.conflicting"])

    assert tools.names() == ("read_file",)
    assert innocent.cleanup_calls == 1
    assert conflicting.cleanup_calls == 1


async def test_two_plugins_registering_the_same_tool_name_fail_during_setup() -> None:
    one = ScriptedPlugin(manifest("a.one"), tools=(RecordingTool("shared_tool"),))
    two = ScriptedPlugin(manifest("b.two"), tools=(RecordingTool("shared_tool"),))
    manager = build_manager(entry_point_for(one), entry_point_for(two))

    with pytest.raises(PluginActivationError) as info:
        await manager.activate(["a.one", "b.two"])

    assert [f.code for f in info.value.failures] == ["plugin-setup-failed"]
    assert info.value.failures[0].plugin_id == "b.two"


# --------------------------------------------------------------------------
# Health checks
# --------------------------------------------------------------------------


async def test_health_check_success_allows_publication() -> None:
    tools = ToolRegistry()
    plugin = ScriptedPlugin(
        manifest("a.plugin"), tools=(RecordingTool("plugin_tool"),), health_result=True
    )
    manager = build_manager(entry_point_for(plugin), tools=tools)

    await manager.activate(["a.plugin"])

    assert plugin.health_calls == 1
    assert tools.names() == ("plugin_tool",)


async def test_health_check_returning_false_fails_activation() -> None:
    tools = ToolRegistry()
    plugin = ScriptedPlugin(
        manifest("a.plugin"), tools=(RecordingTool("plugin_tool"),), health_result=False
    )
    manager = build_manager(entry_point_for(plugin), tools=tools)

    with pytest.raises(PluginActivationError) as info:
        await manager.activate(["a.plugin"])

    assert [f.code for f in info.value.failures] == ["plugin-health-check-failed"]
    assert tools.names() == ()
    assert plugin.cleanup_calls == 1


async def test_health_check_raising_fails_activation_without_leaking_text() -> None:
    plugin = ScriptedPlugin(
        manifest("a.plugin"), health_error=RuntimeError("db://user:pw-FAKE@host")
    )
    manager = build_manager(entry_point_for(plugin))

    with pytest.raises(PluginActivationError) as info:
        await manager.activate(["a.plugin"])

    rendered = str(info.value) + "".join(f.message for f in info.value.failures)
    assert "pw-FAKE" not in rendered


async def test_health_check_may_take_no_arguments() -> None:
    class ZeroArgHealth:
        manifest = PluginManifest("a.plugin", "1.0.0")
        calls = 0

        async def setup(self, context, config):
            del context, config

        def health_check(self):
            type(self).calls += 1
            return True

    manager = build_manager(FakeEntryPoint(name="a.plugin", target=ZeroArgHealth()))
    await manager.activate(["a.plugin"])
    assert ZeroArgHealth.calls == 1


async def test_plugin_without_health_check_activates() -> None:
    plugin = ScriptedPlugin(manifest("a.plugin"))
    manager = build_manager(entry_point_for(plugin))
    identities = await manager.activate(["a.plugin"])
    assert identities[1].plugin_id == "a.plugin"


async def test_health_runs_after_all_setups() -> None:
    order: list[str] = []
    one = ScriptedPlugin(manifest("a.one"), health_result=True, record=order)
    two = ScriptedPlugin(manifest("b.two"), health_result=True, record=order)
    manager = build_manager(entry_point_for(one), entry_point_for(two))

    await manager.activate(["a.one", "b.two"])

    assert order == ["setup:a.one", "setup:b.two", "health:a.one", "health:b.two"]


# --------------------------------------------------------------------------
# Context surface and configuration
# --------------------------------------------------------------------------


async def test_plugin_configuration_is_isolated_from_the_caller() -> None:
    captured: dict[str, object] = {}
    source = {"key": {"nested": [1, 2]}}

    class ConfigPlugin:
        manifest = PluginManifest("a.plugin", "1.0.0")

        async def setup(self, context, config):
            del config
            captured["value"] = context.get_config("key")
            captured["snapshot"] = context.config_snapshot()
            captured["default"] = context.get_config("absent", "fallback")

    manager = build_manager(
        FakeEntryPoint(name="a.plugin", target=ConfigPlugin()),
        plugin_configs={"a.plugin": source},
    )
    await manager.activate(["a.plugin"])

    assert captured["value"] == {"nested": [1, 2]}
    assert captured["default"] == "fallback"
    # Mutating the plugin's copy must not reach the manager's configuration.
    captured["value"]["nested"].append(3)  # type: ignore[index]
    assert source == {"key": {"nested": [1, 2]}}


async def test_missing_config_key_without_default_raises() -> None:
    class ConfigPlugin:
        manifest = PluginManifest("a.plugin", "1.0.0")

        async def setup(self, context, config):
            del config
            context.get_config("absent")

    manager = build_manager(FakeEntryPoint(name="a.plugin", target=ConfigPlugin()))
    with pytest.raises(PluginActivationError):
        await manager.activate(["a.plugin"])


async def test_plugin_can_require_a_service_provided_by_a_dependency() -> None:
    key = ServiceKey("shared.service")
    base = ScriptedPlugin(manifest("a.base"), services=((key, "from-base"),))
    seen: dict[str, object] = {}

    class Consumer:
        manifest = PluginManifest(
            "b.consumer", "1.0.0", requires_plugins=(PluginDependency("a.base", ">=1.0"),)
        )

        async def setup(self, context, config):
            del config
            seen["value"] = context.require(key)

    manager = build_manager(
        entry_point_for(base), FakeEntryPoint(name="b.consumer", target=Consumer())
    )
    await manager.activate(["a.base", "b.consumer"])
    assert seen["value"] == "from-base"


async def test_spawn_owned_rejects_non_coroutines() -> None:
    class BadSpawn:
        manifest = PluginManifest("a.plugin", "1.0.0")

        async def setup(self, context, config):
            del config
            context.spawn_owned(lambda: None, name="x")

    manager = build_manager(FakeEntryPoint(name="a.plugin", target=BadSpawn()))
    with pytest.raises(PluginActivationError):
        await manager.activate(["a.plugin"])


async def test_owned_task_name_does_not_use_plugin_supplied_text() -> None:
    """A plugin-chosen task name is untrusted text that would surface in diagnostics."""

    captured: dict[str, object] = {}

    class NamingPlugin:
        manifest = PluginManifest("a.plugin", "1.0.0")

        async def setup(self, context, config):
            del config

            async def work() -> None:
                return None

            captured["task"] = context.spawn_owned(work(), name="evil\nname\x1b[2J")

    manager = build_manager(FakeEntryPoint(name="a.plugin", target=NamingPlugin()))
    await manager.activate(["a.plugin"])

    name = captured["task"].get_name()  # type: ignore[union-attr]
    assert name == "traceh-plugin-a.plugin-task-1"
    assert "\n" not in name and "\x1b" not in name
    await manager.dispose()


@pytest.mark.parametrize(
    "tool_name",
    ["has space", "1leading-digit", "has/slash", "", "bad\nname"],
)
async def test_invalid_plugin_tool_names_are_rejected(tool_name: str) -> None:
    plugin = ScriptedPlugin(manifest("a.plugin"), tools=(RecordingTool(tool_name),))
    manager = build_manager(entry_point_for(plugin))
    with pytest.raises(PluginActivationError):
        await manager.activate(["a.plugin"])


async def test_invalid_prompt_section_is_rejected() -> None:
    plugin = ScriptedPlugin(manifest("a.plugin"), prompts=(PromptSection("bad id", "x"),))
    manager = build_manager(entry_point_for(plugin))
    with pytest.raises(PluginActivationError):
        await manager.activate(["a.plugin"])


# --------------------------------------------------------------------------
# Lifecycle guards
# --------------------------------------------------------------------------


async def test_activation_may_run_only_once() -> None:
    plugin = ScriptedPlugin(manifest("a.plugin"))
    manager = build_manager(entry_point_for(plugin))
    await manager.activate(["a.plugin"])
    with pytest.raises(RuntimeError):
        await manager.activate(["a.plugin"])


async def test_duplicate_ids_in_the_enabled_list_are_rejected() -> None:
    manager = build_manager()
    with pytest.raises(PluginValidationError) as info:
        await manager.activate(["a.plugin", "a.plugin"])
    assert [f.code for f in info.value.failures] == ["enabled-plugin-list-invalid"]


async def test_invalid_id_in_the_enabled_list_is_rejected_before_discovery() -> None:
    counter = LoadCounter()
    plugin = ScriptedPlugin(manifest("a.plugin"))
    manager = build_manager(entry_point_for(plugin, counter=counter))
    with pytest.raises(PluginValidationError):
        await manager.activate(["Bad Id"])
    assert counter.loaded == []


async def test_statuses_report_active_plugins() -> None:
    plugin = ScriptedPlugin(manifest("a.plugin"))
    manager = build_manager(entry_point_for(plugin))
    await manager.activate(["a.plugin"])

    statuses = manager.statuses
    assert [status.plugin_id for status in statuses] == ["a.plugin"]
    assert statuses[0].state == "active"
    assert statuses[0].failure is None
    assert statuses[0].manifest is not None


async def test_statuses_report_failures() -> None:
    plugin = ScriptedPlugin(manifest("a.plugin"), setup_error=RuntimeError("nope"))
    manager = build_manager(entry_point_for(plugin))
    with pytest.raises(PluginActivationError):
        await manager.activate(["a.plugin"])

    statuses = manager.statuses
    assert statuses[0].state == "failed"
    assert statuses[0].failure is not None
    assert statuses[0].failure.code == "plugin-setup-failed"


async def test_identities_include_core_and_real_plugin_versions() -> None:
    plugin = ScriptedPlugin(manifest("a.plugin", version="2.3.4"))
    manager = build_manager(entry_point_for(plugin))

    identities = await manager.activate(["a.plugin"])

    assert identities[0] == CORE_PLUGIN_IDENTITY
    assert identities[1].plugin_id == "a.plugin"
    assert identities[1].version == "2.3.4"
    assert manager.enabled_plugin_ids == ("a.plugin",)


async def test_repeated_dispose_is_safe() -> None:
    plugin = ScriptedPlugin(manifest("a.plugin"))
    manager = build_manager(entry_point_for(plugin))
    await manager.activate(["a.plugin"])

    await manager.dispose()
    await manager.dispose()

    assert plugin.cleanup_calls == 1
