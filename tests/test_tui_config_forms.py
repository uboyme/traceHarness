"""Human settings edits reach the production parsers without executing tasks."""

import json
import os
from types import SimpleNamespace

import pytest

pytest.importorskip("textual")
from test_tui_settings import isolated_environment, launch_args  # noqa: F401
from textual.widgets import (
    Button,
    Input,
    Select,
    SelectionList,
    Static,
    Switch,
    TabbedContent,
    Tree,
)

from traceh.chat.config import load_context_host_file
from traceh.cli.main import _compaction_policy, _configure_from_environment
from traceh.cli.tui_config import BASE_FIELDS, apply_values, form_values, load_profile, save_profile
from traceh.product.config import load_product_host_file
from traceh.tui.config_forms import ConfigForm
from traceh.tui.settings import ConfigurationApp, SettingsScreen


async def click(app, pilot, selector):
    button = app.screen.query_one(selector, Button)
    button.active_effect_duration = 0
    button.scroll_visible(animate=False)
    await pilot.pause()
    assert await pilot.click(selector)
    await pilot.pause()


async def select(app, pilot, path):
    tree = app.screen.query_one(Tree)

    def find(node):
        if node.data == path:
            return node
        for child in node.children:
            result = find(child)
            if result is not None:
                return result
        return None

    node = find(tree.root)
    assert node is not None, path
    tree.select_node(node)
    await pilot.pause()
    assert app.screen.selected_path == path


async def edit(app, pilot, path, value):
    await select(app, pilot, path)
    app.screen.query_one("#config-value", Input).value = str(value)
    await click(app, pilot, "#config-update")


async def open_form(app, pilot, kind):
    app.screen.query_one(TabbedContent).active = f"settings-{kind}"
    await pilot.pause()
    await click(app, pilot, f"#{kind}-form")
    assert isinstance(app.screen, ConfigForm)


async def test_context_form_enables_readers_edits_lists_sources_and_saves(tmp_path):
    args = launch_args(tmp_path)
    app = ConfigurationApp(args, form_values(args), tmp_path / "profile.json")
    async with app.run_test(size=(125, 45)) as pilot:
        await open_form(app, pilot, "context")
        for kind in ("history", "skills", "memory"):
            await select(app, pilot, ("context", kind))
            await click(app, pilot, "#config-toggle")
        await edit(app, pilot, ("context", "history", "page_messages"), 7)
        await select(app, pilot, ("project", "sources"))
        app.screen.query_one("#config-new-key", Input).value = "field-notes"
        await click(app, pilot, "#config-add")
        await select(app, pilot, ("skill_policy", "resource_roots"))
        await click(app, pilot, "#config-add")
        await edit(
            app,
            pilot,
            ("skill_policy", "resource_roots", 0, "plugin", "plugin_id"),
            "notes.reference",
        )
        await edit(app, pilot, ("skill_policy", "resource_roots", 0, "plugin", "version"), "1.2.0")
        await edit(app, pilot, ("skill_policy", "resource_roots", 0, "path"), str(tmp_path))
        await click(app, pilot, "#config-save")
        assert isinstance(app.screen, SettingsScreen)
        assert app.screen.query_one("#context-enabled", Switch).value
        path = tmp_path / ".traceh-context.json"
        parsed = load_context_host_file(path)
        assert parsed.context.history.page_messages == 7
        assert parsed.sources == (("field-notes", tmp_path),)
        assert parsed.skill_policy is not None
        await click(app, pilot, "#settings-save")
        assert load_profile(tmp_path / "profile.json")["context_config"] == str(path)
        # Disable is a launch choice, not a destructive edit to the knowledge configuration.
        before = path.read_bytes()
        app.screen.query_one("#context-enabled", Switch).value = False
        await click(app, pilot, "#settings-start")
    assert app.return_value.context_config is None
    assert path.read_bytes() == before
    assert not args.data_dir.exists()


async def test_new_product_form_required_fields_and_command_arguments(tmp_path):
    args = launch_args(tmp_path)
    args.model = "scripted-model"
    app = ConfigurationApp(args, form_values(args), tmp_path / "profile.json")
    path = tmp_path / ".traceh-product.json"
    async with app.run_test(size=(125, 45)) as pilot:
        await open_form(app, pilot, "product")
        await click(app, pilot, "#config-save")
        assert isinstance(app.screen, ConfigForm)
        assert not path.exists()
        assert "校验" in str(app.screen.query_one("#config-form-status", Static).render())
        await edit(app, pilot, ("approver_id",), "review-owner")
        await edit(app, pilot, ("promotion_target", "repository"), str(tmp_path / "receive.git"))
        await edit(app, pilot, ("promotion_target", "ref"), "refs/heads/review")
        argv = ("verification", "commands", 0, "argv")
        for index, value in enumerate(("python", "-c", "raise RuntimeError('must not execute')")):
            await select(app, pilot, argv)
            await click(app, pilot, "#config-add")
            await edit(app, pilot, (*argv, index), value)
        await select(app, pilot, ("default_mode",))
        app.screen.query_one("#config-choice", Select).value = "single"
        await click(app, pilot, "#config-update")
        await edit(app, pilot, ("task_budget", "max_tokens"), "")
        await click(app, pilot, "#config-save")
        assert isinstance(app.screen, SettingsScreen)
        assert app.screen.query_one("#product-enabled", Switch).value
        parsed = load_product_host_file(path)
        assert parsed.approver_id == "review-owner"
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["default_mode"] == "single"
        assert raw["task_budget"]["max_tokens"] is None
        assert len(raw["verification"]["commands"][0]["argv"]) == 3
        await pilot.press("escape")
    assert not args.data_dir.exists()
    assert not (tmp_path / "receive.git").exists()


@pytest.mark.parametrize("operation", ["cancel", "invalid", "stale"])
async def test_form_cancel_invalid_and_stale_preserve_files(tmp_path, operation):
    args = launch_args(tmp_path)
    app = ConfigurationApp(args, form_values(args), tmp_path / "profile.json")
    path = tmp_path / ".traceh-context.json"
    async with app.run_test(size=(110, 38)) as pilot:
        await open_form(app, pilot, "context")
        await select(app, pilot, ("context", "history"))
        await click(app, pilot, "#config-toggle")
        if operation == "invalid":
            await edit(app, pilot, ("context", "history", "page_messages"), -1)
            await click(app, pilot, "#config-save")
            assert isinstance(app.screen, ConfigForm)
            assert "校验" in str(app.screen.query_one("#config-form-status", Static).render())
        if operation == "stale":
            path.write_text("external edit", encoding="utf-8")
            await click(app, pilot, "#config-save")
            assert "重新加载" in str(app.screen.query_one("#config-form-status", Static).render())
            assert path.read_text(encoding="utf-8") == "external edit"
        await pilot.press("escape")
        assert isinstance(app.screen, SettingsScreen)
        assert not app.screen.query_one("#context-enabled", Switch).value
        await pilot.press("escape")
    if operation != "stale":
        assert not path.exists()
    assert not args.data_dir.exists()


def test_compaction_profile_roundtrip_and_explicit_off_over_environment(tmp_path, monkeypatch):
    args = launch_args(tmp_path)
    values = {
        **form_values(args),
        "auto_compact": "on",
        "auto_compact_bytes": "24000",
        "auto_compact_summary_bytes": "3000",
        "auto_compact_keep_turns": "2",
    }
    path = tmp_path / "profile.json"
    save_profile(path, values)
    configured = apply_values(args, load_profile(path))
    policy = _compaction_policy(configured)
    assert policy.keep_recent_turns == 2 and policy.trigger_utf8_bytes == 24000
    monkeypatch.setenv("TRACEH_AUTO_COMPACT_BYTES", "50000")
    configured = apply_values(args, {**values, "auto_compact": "off"})
    _configure_from_environment(configured, environment=dict(os.environ))
    assert configured.compaction is None
    # Additive optional inputs: an existing exact base profile still loads without new fields.
    path.write_text(
        json.dumps({"format": 1, "launch": {k: values[k] for k in BASE_FIELDS}}), encoding="utf-8"
    )
    assert load_profile(path)["auto_compact"] == ""


async def test_plugin_selection_and_off_switch_reach_saved_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "traceh.plugins.discovery.PluginDiscovery.discover",
        lambda self: (SimpleNamespace(entry_name="notes.helper", issues=()),),
    )
    args = launch_args(tmp_path)
    path = tmp_path / "profile.json"
    app = ConfigurationApp(args, form_values(args), path)
    async with app.run_test(size=(120, 45)) as pilot:
        app.screen.query_one(TabbedContent).active = "settings-runtime"
        await pilot.pause()
        app.screen.query_one("#installed-plugins").ancestors[1].collapsed = False
        await pilot.pause()
        choices = app.screen.query_one("#installed-plugins", SelectionList)
        choices.select("notes.helper")
        await click(app, pilot, "#plugins-use")
        await click(app, pilot, "#settings-save")
        assert load_profile(path)["plugins"] == "notes.helper"
        app.screen.query_one("#plugins-enabled", Switch).value = False
        await click(app, pilot, "#settings-save")
        assert load_profile(path)["plugins"] == ""
        await pilot.press("escape")


async def test_existing_form_cancel_keeps_document_and_disabled_readers_can_reenable(tmp_path):
    from traceh.session.context_input import ContextInputPolicy

    args = launch_args(tmp_path)
    path = tmp_path / "context.json"
    raw = dict(
        format=1, context=ContextInputPolicy.empty().to_dict(), skill_policy=None, project=None
    )
    path.write_text(json.dumps(raw), encoding="utf-8")
    before = path.read_bytes()
    args.context_config = path
    app = ConfigurationApp(args, form_values(args), tmp_path / "profile.json")
    async with app.run_test(size=(110, 38)) as pilot:
        await open_form(app, pilot, "context")
        await select(app, pilot, ("context", "history"))
        await click(app, pilot, "#config-toggle")
        await pilot.press("escape")
        assert path.read_bytes() == before
        await open_form(app, pilot, "context")
        await select(app, pilot, ("context", "history"))
        await click(app, pilot, "#config-toggle")
        await click(app, pilot, "#config-save")
        assert load_context_host_file(path).context.history is not None
        await open_form(app, pilot, "context")
        await select(app, pilot, ("context", "history"))
        await click(app, pilot, "#config-toggle")
        await click(app, pilot, "#config-save")
        assert load_context_host_file(path).context.history is None
        await pilot.press("escape")


async def test_mistaken_secret_file_is_never_shown_in_form(tmp_path):
    args = launch_args(tmp_path)
    path = tmp_path / "fixture.env"
    path.write_text("FAKE_TOKEN=synthetic-only", encoding="utf-8")
    args.product_config = path
    app = ConfigurationApp(args, form_values(args), tmp_path / "profile.json")
    async with app.run_test(size=(80, 24)) as pilot:
        app.screen.query_one(TabbedContent).active = "settings-product"
        await pilot.pause()
        await click(app, pilot, "#product-form")
        assert isinstance(app.screen, SettingsScreen)
        assert "synthetic-only" not in str(
            app.screen.query_one("#settings-status", Static).render()
        )
        assert path.read_text(encoding="utf-8") == "FAKE_TOKEN=synthetic-only"
        await pilot.press("escape")
