"""Public settings interactions and CLI assembly, offline and without real keys."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

pytest.importorskip("textual")

from textual.widgets import Button, Collapsible, Input, Static, TabbedContent, TextArea

from traceh.chat.driver import TurnCompletedUpdate
from traceh.cli.errors import CliConfigurationError
from traceh.cli.main import _provider_and_model, build_parser, main
from traceh.cli.tui_config import (
    FIELDS,
    LaunchConfigurationError,
    apply_values,
    form_values,
    load_profile,
    preflight,
    save_profile,
)
from traceh.cli.tui_entry import initial_settings, save_personal_profile
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.context_input import ContextInputPolicy
from traceh.session.service import SessionService
from traceh.session.sqlite import SqliteEventStore
from traceh.tui.app import TracehTuiApp
from traceh.tui.settings import ConfigurationApp, SettingsScreen


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    for name in tuple(os.environ):
        if name.startswith("TRACEH_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "traceh.cli.tui_entry.personal_profile_path",
        lambda: tmp_path / "personal" / "settings.json",
    )


def launch_args(tmp_path):
    return build_parser().parse_args(
        [
            "chat",
            str(tmp_path),
            "--tui",
            "--configure",
            "--provider",
            "scripted",
            "--env-file",
            str(tmp_path / "absent.env"),
            "--data-dir",
            str(tmp_path / "data"),
        ]
    )


def test_profile_round_trip_paths_and_explicit_empty_plugins(tmp_path, monkeypatch):
    values = form_values(launch_args(tmp_path))
    values.update(model="arbitrary-model-id", data_dir="账本", plugins="", workspace=".")
    path = tmp_path / "用户配置.json"
    save_profile(path, values)
    monkeypatch.chdir(tmp_path.parent)
    loaded = load_profile(path)
    assert loaded["workspace"] == str(tmp_path)
    assert loaded["data_dir"] == str(tmp_path / "账本")
    assert loaded["model"] == "arbitrary-model-id"
    assert apply_values(argparse.Namespace(), loaded).plugins == []
    assert set(json.loads(path.read_text(encoding="utf-8"))["launch"]) == set(FIELDS)


@pytest.mark.parametrize(
    "change",
    [
        {"base_url": "https://user:private@service.invalid/v1"},
        {"base_url": "https://service.invalid/v1?api_key=private"},
        {"api_key_env": "not-an-env-name"},
        {"workspace": "bad\npath"},
    ],
)
def test_profile_rejects_invalid_or_credential_shaped_fields(tmp_path, change):
    values = {**form_values(launch_args(tmp_path)), **change}
    path = tmp_path / "profile.json"
    with pytest.raises((ValueError, CliConfigurationError)):
        save_profile(path, values)
    assert not path.exists()


def test_failed_write_preserves_old_profile_and_cleans_temporary(tmp_path, monkeypatch):
    path = tmp_path / "profile.json"
    values = form_values(launch_args(tmp_path))
    save_profile(path, values)
    before = path.read_bytes()

    def fail(*args):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr("traceh.cli.tui_config.os.replace", fail)
    with pytest.raises(OSError):
        save_profile(path, {**values, "model": "different-model"})
    assert path.read_bytes() == before
    assert not tuple(tmp_path.glob(".profile.json.*"))


def test_profile_cannot_overwrite_dotenv_or_accept_secret_field(tmp_path):
    # This is a generated fixture, never the user's actual dotenv.
    path = tmp_path / "fixture.env"
    path.write_text("FAKE_KEY=synthetic-private-value", encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValueError):
        save_profile(path, form_values(launch_args(tmp_path)))
    assert path.read_bytes() == before
    path.write_text(
        json.dumps(
            {
                "format": 1,
                "launch": {
                    **form_values(launch_args(tmp_path)),
                    "api_key": "synthetic-private-value",
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_profile(path)


def test_preflight_restores_environment_on_success_and_failure(tmp_path, monkeypatch):
    args = launch_args(tmp_path)
    args.env_file = tmp_path / "fixture.env"
    args.env_file.write_text("SETTINGS_TEST_TOKEN=synthetic-private-value", encoding="utf-8")
    monkeypatch.delenv("SETTINGS_TEST_TOKEN", raising=False)
    before = dict(os.environ)
    assert "校验通过" in preflight(args)
    assert dict(os.environ) == before
    args.context_config = tmp_path / "missing.json"
    with pytest.raises(LaunchConfigurationError):
        preflight(args)
    assert dict(os.environ) == before
    assert not args.data_dir.exists()


async def test_startup_validation_stays_open_then_cancel_writes_nothing(tmp_path):
    args = launch_args(tmp_path)
    values = {**form_values(args), "workspace": "", "session_id": ""}
    app = ConfigurationApp(args, values, tmp_path / "profile.json")
    async with app.run_test(size=(110, 36)) as pilot:
        await pilot.click("#settings-start")
        assert isinstance(app.screen, SettingsScreen)
        assert "操作失败" in str(app.screen.query_one("#settings-status", Static).render())
        await pilot.press("escape")
    assert app.return_value is None
    assert not args.data_dir.exists()
    assert not (tmp_path / "profile.json").exists()


async def test_token_budget_tab_saves_explicit_inputs_and_rejects_incomplete_policy(tmp_path):
    args = launch_args(tmp_path)
    path = tmp_path / "profile.json"
    app = ConfigurationApp(args, form_values(args), path)
    async with app.run_test(size=(110, 38)) as pilot:
        app.screen.query_one(TabbedContent).active = "settings-tokens"
        await pilot.pause()
        app.screen.query_one("#setting-token_encoding", Input).value = "cl100k_base"
        await pilot.click("#settings-start")
        assert isinstance(app.screen, SettingsScreen)
        assert "操作失败" in str(app.screen.query_one("#settings-status", Static).render())
        for name, value in (
            ("context_window_tokens", "40000"),
            ("context_output_reserve", "1024"),
            ("context_safety_margin", "2048"),
            ("context_trigger_percent", "75"),
        ):
            app.screen.query_one(f"#setting-{name}", Input).value = value
        await pilot.click("#settings-save")
        assert load_profile(path)["context_trigger_percent"] == "75"
        await pilot.click("#settings-start")
    from traceh.cli.main import _configure_from_environment

    _configure_from_environment(app.return_value, environment={})
    assert app.return_value.token_budget.trigger_percent == 75
    assert not args.data_dir.exists()


async def test_semantic_summary_selection_validates_and_persists(tmp_path):
    from textual.widgets import Select
    values = form_values(launch_args(tmp_path))
    values.update(auto_compact="on", auto_compact_bytes="100000", auto_compact_summary_bytes="3000",
                  auto_compact_keep_turns="2")
    path = tmp_path / "semantic-profile.json"
    app = ConfigurationApp(launch_args(tmp_path), values, path)
    async with app.run_test(size=(110, 38)) as pilot:
        app.screen.query_one(TabbedContent).active = "settings-compaction"
        await pilot.pause()
        app.screen.query_one("#setting-auto_compact_method", Select).value = "semantic"
        await pilot.click("#settings-start")
        assert isinstance(app.screen, SettingsScreen)
        assert "Token" in str(app.screen.query_one("#settings-status", Static).render())
        app.screen.query_one(TabbedContent).active = "settings-tokens"
        for name, value in (("token_encoding", "cl100k_base"), ("context_window_tokens", "40000"),
                            ("context_output_reserve", "2048"), ("context_safety_margin", "2048"),
                            ("context_trigger_percent", "75")):
            app.screen.query_one(f"#setting-{name}", Input).value = value
        await pilot.click("#settings-save")
        assert load_profile(path)["auto_compact_method"] == "semantic"
        await pilot.click("#settings-start")
    assert app.return_value.auto_compact_method == "semantic"
    assert not app.return_value.data_dir.exists()


async def test_password_not_saved_and_success_returns_original_launch_inputs(tmp_path):
    args = launch_args(tmp_path)
    path = tmp_path / "profile.json"
    app = ConfigurationApp(args, form_values(args), path)
    async with app.run_test(size=(110, 38)) as pilot:
        secret = app.screen.query_one("#settings-key", Input)
        assert secret.password
        secret.value = "synthetic-private-value"
        app.screen.query_one("#setting-provider", Input).value = "openai-compatible"
        app.screen.query_one("#setting-model", Input).value = "unrelated-model-id"
        app.screen.query_one("#setting-base_url", Input).value = "http://127.0.0.1:9/v1"
        await pilot.click("#settings-save")
        assert "synthetic-private-value" not in path.read_text(encoding="utf-8")
        await pilot.click("#settings-start")
    result = app.return_value
    assert result is not None
    assert result.model == "unrelated-model-id"
    provider, model = _provider_and_model(result)
    assert provider.api_key == "synthetic-private-value"
    assert model == result.model
    assert not args.data_dir.exists()  # No Store or API created by preflight.


async def test_context_editor_validates_before_showing_and_refuses_stale_save(tmp_path):
    args = launch_args(tmp_path)
    path = tmp_path / "context.json"
    raw = {
        "format": 1,
        "context": ContextInputPolicy.empty().to_dict(),
        "skill_policy": None,
        "project": None,
    }
    path.write_text(json.dumps(raw), encoding="utf-8")
    args.context_config = path
    app = ConfigurationApp(args, form_values(args), tmp_path / "profile.json")
    async with app.run_test(size=(120, 45)) as pilot:
        app.screen.query_one(TabbedContent).active = "settings-context"
        await pilot.pause()
        app.screen.query_one("#context-advanced", Collapsible).collapsed = False
        await pilot.pause()
        # Disable visual click debounce, not the actual Button/handler operation.
        for button in app.screen.query(Button):
            button.active_effect_duration = 0
        async def activate(selector):
            # Focus/Enter exercises the button without racing Collapsible scroll animation.
            app.screen.query_one(selector, Button).focus()
            await pilot.pause()
            await pilot.press("enter")

        await activate("#context-load")
        editor = app.screen.query_one("#context-editor", TextArea)
        assert editor.text, str(app.screen.query_one("#settings-status", Static).render())
        assert json.loads(editor.text) == raw
        changed = {**raw, "context": {**raw["context"], "max_exclusions": 5}}
        editor.load_text(json.dumps(changed))
        await activate("#context-save")
        assert json.loads(path.read_text(encoding="utf-8")) == changed
        external = {**raw, "context": {**raw["context"], "max_exclusions": 6}}
        path.write_text(json.dumps(external), encoding="utf-8")
        await activate("#context-save")
        assert json.loads(path.read_text(encoding="utf-8")) == external
        assert "重新加载" in str(app.screen.query_one("#settings-status", Static).render())
        fixture = tmp_path / "fixture.env"
        fixture.write_text("FAKE_KEY=synthetic-private-value", encoding="utf-8")
        app.screen.query_one("#setting-context_config", Input).value = str(fixture)
        await activate("#context-load")
        assert "synthetic-private-value" not in editor.text
        await pilot.press("escape")


def test_saved_profile_cli_target_override(tmp_path, monkeypatch):
    args = launch_args(tmp_path)
    args.workspace = None
    args.session_id = "explicit-session"
    args.tui_explicit = {"session_id"}
    path = tmp_path / "profile.json"
    save_profile(path, form_values(launch_args(tmp_path)))
    args.tui_profile = path

    configured, error = initial_settings(args)
    assert not error
    assert configured.workspace is None
    assert configured.session_id == "explicit-session"


async def test_loading_profile_clears_transient_key_and_context_draft(tmp_path):
    args = launch_args(tmp_path)
    path = tmp_path / "profile.json"
    save_profile(path, {**form_values(args), "model": "different-model"})
    app = ConfigurationApp(args, form_values(args), path)
    async with app.run_test(size=(100, 35)) as pilot:
        app.screen.query_one("#settings-key", Input).value = "synthetic-private-value"
        app.screen.query_one("#context-editor", TextArea).load_text("old context draft")
        app.screen.query_one(TabbedContent).active = "settings-profile"
        await pilot.pause()
        await pilot.click("#settings-load")
        assert app.screen.query_one("#setting-model", Input).value == "different-model"
        assert app.screen.query_one("#settings-key", Input).value == ""
        assert app.screen.query_one("#context-editor", TextArea).text == ""
        await pilot.press("escape")


@pytest.fixture
def local_provider():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            calls.append(
                (
                    self.path,
                    self.headers.get("Authorization"),
                    json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                )
            )
            body = json.dumps(
                {
                    "id": "local-settings-response",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "本地连接验证完成",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", calls
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()
        assert not worker.is_alive()


@pytest.mark.parametrize("provider_kind", ["scripted", "openai-compatible"])
def test_cli_configure_to_real_tui_turn_and_next_launch_settings(
    tmp_path,
    monkeypatch,
    local_provider,
    provider_kind,
):
    observed = {}

    async def setup(app):
        async with app.run_test(size=(110, 40)) as pilot:
            app.screen.query_one("#setting-provider", Input).value = provider_kind
            app.screen.query_one("#setting-model", Input).value = "configured-scripted-model"
            if provider_kind == "openai-compatible":
                app.screen.query_one("#setting-base_url", Input).value = local_provider[0]
                app.screen.query_one("#settings-key", Input).value = "synthetic-private-value"
            await pilot.click("#settings-start")
        return app.return_value

    async def chat(app):
        completed = asyncio.Event()
        receive = app._receive_chat_update

        async def tracked(update):
            await receive(update)
            if isinstance(update, TurnCompletedUpdate):
                completed.set()

        # Observe original ChatDriver sink; don't substitute the operation.
        app._chat_driver._sink = tracked
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.press("f2")
            assert isinstance(app.screen, SettingsScreen)
            assert app.screen.query_one("#settings-key", Input).password
            assert app.screen.query("#settings-start")
            app.screen.query_one("#setting-model", Input).value = "next-launch-model"
            await pilot.click("#settings-save")
            await pilot.press("escape")
            entry = app.query_one("#chat-input", Input)
            entry.value = "你好，配置面板验证"
            await pilot.press("enter")
            await asyncio.wait_for(completed.wait(), 20)
            observed["session"] = app._session.session_id
            await pilot.press("ctrl+q")
        return app.return_value

    monkeypatch.setattr(ConfigurationApp, "run_async", setup)
    monkeypatch.setattr(TracehTuiApp, "run_async", chat)
    with pytest.raises(SystemExit) as exited:
        main(
            [
                "chat",
                str(tmp_path),
                "--tui",
                "--configure",
                "--provider",
                "scripted",
                "--env-file",
                str(tmp_path / "absent.env"),
                "--data-dir",
                str(tmp_path / "data"),
            ]
        )
    assert exited.value.code == 130

    async def evidence():
        async with SqliteEventStore(tmp_path / "data" / "events") as store:
            return await store.read(SessionService.session_stream(observed["session"]))

    events = asyncio.run(evidence())
    requests = [event for event in events if event.type == "request/snapshot"]
    assert len(requests) == 1
    assert "configured-scripted-model" in json.dumps(requests[0].data)
    assert "next-launch-model" not in json.dumps(requests[0].data)
    assert any(event.type == "turn/end" for event in events)
    assert "synthetic-private-value" not in json.dumps([event.data for event in events])
    saved = load_profile(tmp_path / ".traceh-tui.json")
    assert saved["model"] == "next-launch-model"
    assert "synthetic-private-value" not in json.dumps(saved)
    if provider_kind == "openai-compatible":
        assert len(local_provider[1]) == 1
        path, authorization, body = local_provider[1][0]
        assert path == "/v1/chat/completions"
        assert authorization == "Bearer synthetic-private-value"
        assert body["model"] == "configured-scripted-model"


async def test_small_terminal_can_cancel_and_does_not_save(tmp_path):
    args = launch_args(tmp_path)
    app = ConfigurationApp(args, form_values(args), tmp_path / "profile.json")
    async with app.run_test(size=(80, 24)) as pilot:
        assert app.screen.query_one("#settings-start", Button).region.right <= 80
        assert await pilot.click("#settings-back")
    assert app.return_value is None
    assert not args.data_dir.exists()


def test_cli_cancel_setup_precedes_environment_loading(tmp_path, monkeypatch):
    env = tmp_path / "fixture.env"
    env.write_text("this is deliberately invalid", encoding="utf-8")

    async def cancel(app):
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("escape")
        return app.return_value

    monkeypatch.setattr(ConfigurationApp, "run_async", cancel)
    with pytest.raises(SystemExit) as exited:
        main(["chat", "--tui", "--configure", "--env-file", str(env)])
    assert exited.value.code == 0
    assert not (tmp_path / ".traceh").exists()


def test_configuration_flag_requires_tui(tmp_path, capsys):
    with pytest.raises(SystemExit) as exited:
        main(["chat", str(tmp_path), "--configure"])
    assert exited.value.code == 2
    assert "requires --tui" in capsys.readouterr().err


def test_personal_defaults_do_not_transport_workspace_or_session(tmp_path, monkeypatch):
    values = form_values(launch_args(tmp_path))
    values.update(
        model="personal-model", session_id="must-not-be-inherited", plugins="must-not-be-inherited"
    )
    path = save_personal_profile(values)
    saved = load_profile(path)
    assert saved["workspace"] == saved["session_id"] == saved["plugins"] == ""
    another = tmp_path / "another-workspace"
    another.mkdir()
    monkeypatch.chdir(another)
    args = build_parser().parse_args(["chat", "--tui"])
    candidate, error = initial_settings(args)
    assert not error
    assert candidate.workspace == another
    assert candidate.session_id is None
    assert candidate.model == "personal-model"
    assert candidate.data_dir is None  # Original default resolves in the new cwd.


def test_project_then_explicit_cli_override_personal_defaults(tmp_path):
    values = form_values(launch_args(tmp_path))
    save_personal_profile({**values, "model": "personal-model"})
    save_profile(tmp_path / ".traceh-tui.json", {**values, "model": "project-model"})
    args = build_parser().parse_args(["chat", "--tui", "--model", "explicit-model"])
    args.tui_explicit = {"model"}
    candidate, error = initial_settings(args)
    assert not error and candidate.model == "explicit-model"
    args.tui_explicit = set()
    candidate, error = initial_settings(args)
    assert not error and candidate.model == "project-model"


def test_personal_environment_only_supplies_connection_fields(tmp_path):
    from traceh.cli.main import _configure_from_environment

    env = tmp_path / "personal.fixture.env"
    env.write_text(
        "TRACEH_PROVIDER=scripted\nTRACEH_MODEL=personal-model\n"
        "TRACEH_API_KEY_ENV=SETTINGS_KEY\nSETTINGS_KEY=synthetic-key\n"
        "TRACEH_DATA_DIR=another-project\nTRACEH_VERIFY_COMMAND=fixture-verifier\n",
        encoding="utf-8",
    )
    values = {name: "" for name in FIELDS}
    values["env_file"] = str(env)
    save_personal_profile(values)
    args = build_parser().parse_args(["chat", "--tui"])
    candidate, error = initial_settings(args)
    assert not error
    environment = {}
    _configure_from_environment(candidate, environment=environment)
    assert candidate.model == "personal-model"
    assert environment["SETTINGS_KEY"] == "synthetic-key"
    assert candidate.data_dir == Path(".traceh")
    assert candidate.verify_command is None
    assert "TRACEH_VERIFY_COMMAND" not in environment
    args.env_file = env
    args.tui_explicit = {"env_file"}
    candidate, error = initial_settings(args)
    _configure_from_environment(candidate, environment={})
    assert candidate.data_dir == Path("another-project")
    assert candidate.verify_command == "fixture-verifier"


def test_no_configuration_opens_settings_before_creating_runtime(tmp_path, monkeypatch):
    from traceh.tui.onboarding import QuickSetupApp
    seen = []

    async def waiting(app):
        async with app.run_test(size=(80, 24)) as pilot:
            assert not (tmp_path / ".traceh" / "events").exists()
            assert app.query_one("#setup-start", Button)
            assert app.args.provider == "openai-compatible"
            assert app.args.workspace == tmp_path
            await pilot.press("escape")
            assert app.return_value is None
            seen.append(True)
        return app.return_value

    monkeypatch.setattr(QuickSetupApp, "run_async", waiting)
    with pytest.raises(SystemExit) as exited:
        main([])
    assert exited.value.code == 0 and seen == [True]


def test_preview_never_exposes_draft_environment_to_live_provider(tmp_path, monkeypatch):
    args = launch_args(tmp_path)
    args.env_file = tmp_path / "fixture.env"
    args.env_file.write_text("SETTINGS_DRAFT_KEY=synthetic-draft", encoding="utf-8")
    monkeypatch.delenv("SETTINGS_DRAFT_KEY", raising=False)
    original = _provider_and_model
    observed = []

    def inspect(resolved):
        observed.append("SETTINGS_DRAFT_KEY" in os.environ)
        return original(resolved)

    monkeypatch.setattr("traceh.cli.main._provider_and_model", inspect)
    assert "校验通过" in preflight(args)
    assert observed == [False]


def test_environment_resolution_failure_has_no_partial_process_changes(tmp_path, monkeypatch):
    from traceh.cli.main import _configure_from_environment

    args = launch_args(tmp_path)
    args.env_file = tmp_path / "fixture.env"
    args.env_file.write_text(
        "SETTINGS_DRAFT_KEY=synthetic\nTRACEH_MAX_STEPS=invalid", encoding="utf-8"
    )
    before = dict(os.environ)
    with pytest.raises(CliConfigurationError):
        _configure_from_environment(args)
    assert dict(os.environ) == before


@pytest.mark.parametrize("credential_source", ["process", "env-file", "vault"])
def test_bare_entry_live_model_apply_preserves_session_and_request_evidence(
    tmp_path,
    monkeypatch,
    local_provider,
    credential_source,
):
    values = form_values(launch_args(tmp_path))
    values.update(
        provider="openai-compatible",
        model="model-before",
        base_url=local_provider[0],
        api_key_env="SETTINGS_LIVE_KEY",
    )
    if credential_source == "process":
        monkeypatch.setenv("SETTINGS_LIVE_KEY", "synthetic-live-key")
    else:
        monkeypatch.delenv("SETTINGS_LIVE_KEY", raising=False)
        first = tmp_path / "first.fixture.env"
        first.write_text(
            "SETTINGS_LIVE_KEY=synthetic-live-key\nTRACEH_MODEL_RETRY_MAX_ATTEMPTS=2",
            encoding="utf-8",
        )
        second = tmp_path / "second.fixture.env"
        second.write_text(
            "SETTINGS_LIVE_KEY=synthetic-next-key\nTRACEH_MODEL_RETRY_MAX_ATTEMPTS=4",
            encoding="utf-8",
        )
        values["env_file"] = str(first)
        if credential_source == "vault":
            from traceh.cli import credentials

            if not credentials.available():
                pytest.skip("Windows current-user credential storage")
            monkeypatch.setattr(credentials, "credential_root", lambda: tmp_path / "vault")
            first.write_text("TRACEH_MODEL_RETRY_MAX_ATTEMPTS=2", encoding="utf-8")
            credentials.save_key(apply_values(launch_args(tmp_path), values), "synthetic-live-key")
    save_personal_profile(values)
    apps = []
    session_ids = []
    context_file = tmp_path / "context.json"
    context_file.write_text(
        json.dumps(
            {
                "format": 1,
                "context": ContextInputPolicy.empty().to_dict(),
                "skill_policy": None,
                "project": None,
            }
        ),
        encoding="utf-8",
    )
    save_profile(
        tmp_path / ".traceh-tui.json",
        {
            **values,
            "context_config": str(context_file),
            "data_dir": "",
        },
    )

    async def chat(app):
        completed = asyncio.Event()
        shutdown_started = asyncio.Event()
        start_shutdown = app._start_shutdown

        async def observe_shutdown():
            await start_shutdown()
            shutdown_started.set()

        app._start_shutdown = observe_shutdown
        receive = app._receive_chat_update

        async def observe(update):
            await receive(update)
            if isinstance(update, TurnCompletedUpdate):
                completed.set()

        app._chat_driver._sink = observe
        if apps:
            with pytest.raises(RuntimeError, match="disposed"):
                await apps[0]._runtime.run_existing(session_ids[0], "must not run")
        apps.append(app)
        async with app.run_test(size=(110, 38)) as pilot:
            session_ids.append(app._session.session_id)
            app.query_one("#chat-input", Input).value = "验证当前模型"
            await pilot.press("enter")
            await asyncio.wait_for(completed.wait(), 20)
            if len(apps) == 1:
                await pilot.press("f2")
                app.screen.query_one("#setting-model", Input).value = "model-after"
                app.screen.query_one("#setting-context_config", Input).value = ""
                if credential_source in {"env-file", "vault"}:
                    app.screen.query_one("#setting-env_file", Input).value = str(second)
                await pilot.click("#settings-start")
                # Screen.dismiss schedules the App callback on a later message.
                # Synchronize with the real shutdown owner, not click timing.
                await asyncio.wait_for(shutdown_started.wait(), 10)
                assert app._shutdown_task is not None, str(
                    app.screen.query_one("#settings-status", Static).render()
                )
                await asyncio.wait_for(app._shutdown_task, 20)
                await pilot.pause()
            else:
                assert session_ids[0] == session_ids[1]
                assert app._runtime.config.context_input is None
                if credential_source in {"env-file", "vault"}:
                    assert app._runtime.config.model_retry_policy.max_attempts == 4
                assert not await verify_request_snapshots(
                    app._runtime.sessions,
                    app._runtime.surface,
                    session_ids[1],
                )
                await pilot.press("ctrl+q")
                await asyncio.wait_for(shutdown_started.wait(), 10)
                await asyncio.wait_for(app._shutdown_task, 20)
                await pilot.pause()
        return app.return_value

    async def startup(app):
        assert not apps  # F2 restart does not reopen the startup sheet.
        async with app.run_test(size=(110, 38)) as pilot:
            assert isinstance(app.screen, SettingsScreen)
            await pilot.click("#settings-start")
            await pilot.pause()
        assert app.return_value is not None
        return app.return_value

    monkeypatch.setattr(TracehTuiApp, "run_async", chat)
    monkeypatch.setattr(ConfigurationApp, "run_async", startup)
    with pytest.raises(SystemExit) as exited:
        main([])
    assert exited.value.code == 130
    assert len(apps) == 2
    assert [call[2]["model"] for call in local_provider[1]] == ["model-before", "model-after"]
    assert local_provider[1][0][1] == "Bearer synthetic-live-key"
    assert local_provider[1][1][1] == (
        "Bearer synthetic-live-key"
        if credential_source == "process"
        else "Bearer synthetic-next-key"
    )
    if credential_source in {"env-file", "vault"}:
        assert "SETTINGS_LIVE_KEY" not in os.environ


async def test_cleanup_failure_cannot_return_a_restart_request(tmp_path, monkeypatch):
    from test_tui import _Provider, _runtime

    from traceh.cli.tui_entry import RestartChat
    from traceh.tui.runner import run_tui

    runtime, store = _runtime(tmp_path, _Provider())
    original = runtime.dispose

    async def fail_close():
        await original()
        raise CliConfigurationError("synthetic-close-failure")

    class App:
        def __init__(self, *args, **kwargs):
            pass

        async def run_async(self):
            return RestartChat(launch_args(tmp_path))

    monkeypatch.setattr("traceh.tui.app.TracehTuiApp", App)
    monkeypatch.setattr(runtime, "dispose", fail_close)
    with pytest.raises(BaseExceptionGroup, match="did not close cleanly"):
        await run_tui(
            runtime, workspace=tmp_path, session_id=None, timeline=False, heartbeat_seconds=0
        )
