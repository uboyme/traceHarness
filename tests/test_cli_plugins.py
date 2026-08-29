"""`traceh plugins` output: human and JSON, and what it refuses to do.

Everything rendered here comes from third-party distribution metadata, so the
terminal-safety rules the Timeline and resume-command renderers already follow
apply: strictly one line per value, no control characters, bounded length.
"""

from __future__ import annotations

import json

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

from traceh.cli.plugins import doctor_plugins, inspect_plugin, list_plugins
from traceh.plugins.discovery import PluginDiscovery
from traceh.session.event_store import InMemoryEventStore

MALICIOUS = [
    "line\nbreak",
    "carriage\rreturn",
    "escape\x1b[2Jclear",
    "colour\x1b[31mred",
    "backspace\b\b\b",
    "bell\a",
    "override‮evil",
    "separator forged",
    "paragraph forged",
    "x" * 900,
]


def discovery_for(*points):
    return PluginDiscovery(entry_points_provider=provider_for(*points))


def json_from(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


# --------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------


def test_list_reports_discovered_plugins(capsys) -> None:
    code = list_plugins(discovery=discovery_for(FakeEntryPoint(name="a.plugin")))
    output = capsys.readouterr().out
    assert code == 0
    assert "a.plugin: discovered" in output
    assert "example-dist 1.0.0" in output
    assert "ok=true" in output


def test_list_json_is_valid_and_sorted(capsys) -> None:
    code = list_plugins(
        json_output=True,
        discovery=discovery_for(FakeEntryPoint(name="z.plugin"), FakeEntryPoint(name="a.plugin")),
    )
    payload = json_from(capsys)
    assert code == 0
    assert payload["command"] == "list"
    assert payload["safe_discovery_only"] is True
    assert [item["entry_point"]["name"] for item in payload["plugins"]] == [
        "a.plugin",
        "z.plugin",
    ]
    assert all(
        item["manifest"] == {"available": False, "requires_import": True}
        for item in payload["plugins"]
    )


def test_list_with_nothing_installed(capsys) -> None:
    code = list_plugins(discovery=discovery_for())
    assert code == 0
    assert "no plugins" in capsys.readouterr().out


def test_list_never_imports_a_plugin(capsys) -> None:
    counter = LoadCounter()
    list_plugins(discovery=discovery_for(FakeEntryPoint(name="a.plugin", counter=counter)))
    capsys.readouterr()
    assert counter.loaded == []


@pytest.mark.parametrize("hostile", MALICIOUS)
def test_list_output_is_terminal_safe(capsys, hostile: str) -> None:
    """Entry-point and distribution metadata are untrusted text."""

    list_plugins(
        discovery=discovery_for(
            FakeEntryPoint(
                name="a.plugin",
                value=hostile,
                dist=FakeDistribution(name=hostile, version="1.0.0"),
            )
        )
    )
    output = capsys.readouterr().out
    assert "\x1b" not in output
    assert "‮" not in output
    assert " " not in output and " " not in output
    assert "\b" not in output and "\a" not in output
    for line in output.splitlines():
        assert len(line) < 2000


@pytest.mark.parametrize("hostile", MALICIOUS)
def test_list_json_values_are_escaped(capsys, hostile: str) -> None:
    list_plugins(
        json_output=True,
        discovery=discovery_for(FakeEntryPoint(name="a.plugin", value=hostile)),
    )
    payload = json_from(capsys)
    rendered = payload["plugins"][0]["entry_point"]["value"]
    assert rendered.splitlines() in ([rendered], [])
    assert "\x1b" not in rendered


# --------------------------------------------------------------------------
# inspect
# --------------------------------------------------------------------------


def test_inspect_reports_one_plugin(capsys) -> None:
    code = inspect_plugin(
        "a.plugin",
        json_output=True,
        discovery=discovery_for(FakeEntryPoint(name="a.plugin"), FakeEntryPoint(name="b.other")),
    )
    payload = json_from(capsys)
    assert code == 0
    assert payload["ok"] is True
    assert [item["entry_point"]["name"] for item in payload["plugins"]] == ["a.plugin"]
    assert "requires importing" in payload["note"]


def test_inspect_unknown_plugin_reports_not_ok(capsys) -> None:
    code = inspect_plugin("missing", json_output=True, discovery=discovery_for())
    payload = json_from(capsys)
    assert code == 6
    assert payload["ok"] is False
    assert payload["plugins"] == []


def test_inspect_reports_metadata_issues(capsys) -> None:
    code = inspect_plugin(
        "a.plugin",
        json_output=True,
        discovery=discovery_for(
            FakeEntryPoint(name="a.plugin", dist=FakeDistribution(requires=("requests>=2",)))
        ),
    )
    payload = json_from(capsys)
    assert code == 6
    codes = [issue["code"] for issue in payload["plugins"][0]["issues"]]
    assert "traceh-dependency-missing" in codes


def test_inspect_never_imports_a_plugin(capsys) -> None:
    counter = LoadCounter()
    inspect_plugin(
        "a.plugin", discovery=discovery_for(FakeEntryPoint(name="a.plugin", counter=counter))
    )
    capsys.readouterr()
    assert counter.loaded == []


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------


async def test_doctor_activates_and_disposes(capsys) -> None:
    plugin = ScriptedPlugin(
        manifest("a.plugin"), tools=(RecordingTool("plugin_tool"),), health_result=True
    )
    code = await doctor_plugins(
        ["a.plugin"], json_output=True, discovery=discovery_for(entry_point_for(plugin))
    )
    payload = json_from(capsys)

    assert code == 0
    assert payload["ok"] is True
    assert payload["llm_used"] is False
    assert payload["session_created"] is False
    assert plugin.setup_calls == 1
    assert plugin.health_calls == 1
    # Disposed immediately: doctor must not leave the plugin loaded.
    assert plugin.cleanup_calls == 1


async def test_doctor_defaults_to_every_discovered_plugin(capsys) -> None:
    one = ScriptedPlugin(manifest("a.one"))
    two = ScriptedPlugin(manifest("b.two"))
    code = await doctor_plugins(
        [], json_output=True, discovery=discovery_for(entry_point_for(one), entry_point_for(two))
    )
    payload = json_from(capsys)
    assert code == 0
    assert payload["requested"] == ["a.one", "b.two"]


async def test_doctor_reports_failures_with_a_nonzero_code(capsys) -> None:
    plugin = ScriptedPlugin(manifest("a.plugin"), setup_error=RuntimeError("nope"))
    code = await doctor_plugins(
        ["a.plugin"], json_output=True, discovery=discovery_for(entry_point_for(plugin))
    )
    payload = json_from(capsys)

    assert code == 7
    assert payload["ok"] is False
    assert [f["code"] for f in payload["failures"]] == ["plugin-setup-failed"]
    assert plugin.cleanup_calls == 1


async def test_doctor_reports_a_plugin_that_is_not_installed(capsys) -> None:
    code = await doctor_plugins(["missing.plugin"], json_output=True, discovery=discovery_for())
    payload = json_from(capsys)
    assert code == 7
    ids = [item["plugin_id"] for item in payload["plugins"]]
    assert "missing.plugin" in ids


async def test_doctor_does_not_leak_plugin_exception_text(capsys) -> None:
    plugin = ScriptedPlugin(
        manifest("a.plugin"), setup_error=RuntimeError("token=ghp_FAKE_FIXTURE_VALUE")
    )
    await doctor_plugins(
        ["a.plugin"], json_output=True, discovery=discovery_for(entry_point_for(plugin))
    )
    output = capsys.readouterr().out
    assert "ghp_FAKE_FIXTURE_VALUE" not in output


async def test_doctor_reports_optional_dependency_notices(capsys) -> None:
    from traceh.api.plugins import PluginDependency

    plugin = ScriptedPlugin(
        manifest("a.plugin", optional_plugins=(PluginDependency("b.absent", ">=1.0"),))
    )
    code = await doctor_plugins(
        ["a.plugin"], json_output=True, discovery=discovery_for(entry_point_for(plugin))
    )
    payload = json_from(capsys)
    assert code == 0
    assert [n["code"] for n in payload["notices"]] == ["optional-plugin-missing"]


async def test_doctor_human_output_is_terminal_safe(capsys) -> None:
    plugin = ScriptedPlugin(manifest("a.plugin"))
    point = entry_point_for(plugin, dist=FakeDistribution(name="dist\x1b[2J\nbroken"))
    await doctor_plugins(["a.plugin"], discovery=discovery_for(point))
    output = capsys.readouterr().out
    assert "\x1b" not in output
    assert "ok=" in output


async def test_doctor_uses_throwaway_registries(capsys) -> None:
    """Whatever doctor activates must not be able to reach a real runtime."""

    from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime

    plugin = ScriptedPlugin(manifest("a.plugin"), tools=(RecordingTool("plugin_tool"),))
    await doctor_plugins(
        ["a.plugin"], json_output=True, discovery=discovery_for(entry_point_for(plugin))
    )
    capsys.readouterr()

    runtime = build_default_runtime(RuntimeConfig(), event_store=InMemoryEventStore())
    assert "plugin_tool" not in runtime.loop.compositions.tools.registry.names()
