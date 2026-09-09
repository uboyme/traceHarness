"""Simple launch through production CLI, transport, Session and project owners."""

import asyncio
import os
from copy import copy

import pytest
from test_memory_context import memory_case
from test_tui_settings import launch_args, local_provider  # noqa: F401 - shared pytest fixture

pytest.importorskip("textual")

from textual.widgets import Button, Input, Select, Static

from traceh.chat.activity import default_clock
from traceh.chat.driver import TurnCompletedUpdate
from traceh.chat.workspace_project import restore_workspace_project
from traceh.cli import credentials
from traceh.cli.main import main
from traceh.cli.startup import contains_old_data, start_fresh
from traceh.cli.tui_config import LaunchConfigurationError, form_values, load_profile, save_profile
from traceh.cli.tui_entry import initial_settings
from traceh.session.protocol import CONTEXT_PROTOCOL
from traceh.tui.app import TracehTuiApp
from traceh.tui.onboarding import OldDataApp, QuickSetupApp
from traceh.tui.session_picker import ProjectChoiceApp, SessionPicker, session_choices
from traceh.tui.settings import ConfigurationApp


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("TRACEH_") or name == "OPENAI_API_KEY":
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "traceh.cli.tui_entry.personal_profile_path",
        lambda: tmp_path / "personal" / "settings.json",
    )
    monkeypatch.setattr(credentials, "credential_root", lambda: tmp_path / "vault")


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI contract")
def test_encrypted_key_roundtrip_exact_endpoint_and_corruption(tmp_path):
    args = launch_args(tmp_path)
    args.provider, args.base_url = "openai-compatible", "https://fixture.invalid/v1"
    secret = "synthetic-local-secret-非真实"
    credentials.save_key(args, secret)
    assert credentials.load_key(args) == secret
    path = next((tmp_path / "vault").glob("*.json"))
    assert secret not in path.read_text(encoding="utf-8")
    changed = copy(args)
    changed.base_url = "https://another.invalid/v1"
    assert credentials.load_key(changed) is None
    path.write_text('{"format":1,"encrypted":"00"}', encoding="utf-8")
    with pytest.raises(LaunchConfigurationError):
        credentials.load_key(args)


async def test_quick_setup_cancel_and_selected_service_does_not_choose_model(tmp_path):
    args = launch_args(tmp_path)
    args.provider = "openai-compatible"
    app = QuickSetupApp(args)
    async with app.run_test(size=(100, 42)) as pilot:
        app.query_one("#setup-service", Select).value = "bailian-cn"
        await pilot.pause()
        assert app.query_one("#setup-url", Input).value.endswith("/compatible-mode/v1")
        assert not app.query_one("#setup-model", Input).value
        app.query_one("#setup-model-choice", Select).value = "qwen-plus"
        await pilot.pause()
        assert app.query_one("#setup-model", Input).value == "qwen-plus"
        await pilot.press("escape")
    assert not (tmp_path / "personal").exists()
    assert not (tmp_path / "vault").exists()
    assert not args.data_dir.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows remembered first-run credential")
def test_first_setup_then_bare_launch_sends_real_http_without_setup(
    tmp_path,
    monkeypatch,
    local_provider,  # noqa: F811 - shared fixture
):
    setups, chats = [], []

    async def setup(app):
        setups.append(True)
        assert len(setups) == 1
        async with app.run_test(size=(110, 45)) as pilot:
            app.query_one("#setup-model", Input).value = "fixture-custom-model"
            app.query_one("#setup-url", Input).value = local_provider[0]
            app.query_one("#setup-key", Input).value = "synthetic-remembered"
            await pilot.click("#setup-start")
            await pilot.pause()
        assert app.return_value is not None
        return app.return_value

    async def chat(app):
        completed = asyncio.Event()
        receive = app._receive_chat_update

        async def observe(update):
            await receive(update)
            if isinstance(update, TurnCompletedUpdate):
                completed.set()

        app._chat_driver._sink = observe
        async with app.run_test(size=(110, 38)) as pilot:
            chats.append(app._session.session_id)
            app.query_one("#chat-input", Input).value = "确认新启动连接"
            await pilot.press("enter")
            await asyncio.wait_for(completed.wait(), 10)
            await pilot.press("ctrl+q")
            await asyncio.wait_for(app._shutdown_task, 10)
        return app.return_value

    async def forbidden(_):
        pytest.fail("complete setup must not open advanced settings")

    monkeypatch.setattr(QuickSetupApp, "run_async", setup)
    monkeypatch.setattr(ConfigurationApp, "run_async", forbidden)
    monkeypatch.setattr(TracehTuiApp, "run_async", chat)
    for _ in range(2):
        with pytest.raises(SystemExit) as done:
            main([])
        assert done.value.code == 130
    assert len(chats) == 2 and chats[0] != chats[1] and len(setups) == 1
    assert len(local_provider[1]) == 2
    assert all(row[1] == "Bearer synthetic-remembered" for row in local_provider[1])
    assert "synthetic-remembered" not in (tmp_path / "personal/settings.json").read_text()


async def test_project_choice_remember_fresh_binding_and_index(tmp_path):
    async with memory_case(tmp_path) as (runtime, _, _, first, _, _):
        workspace = await runtime.sessions.workspace_for(first)
        args = launch_args(tmp_path)
        args.tui_profile = tmp_path / ".traceh-tui.json"
        second = await runtime.create_session(workspace)
        seen = []

        async def choose(options):
            seen.append(options)
            return "context-project", "different-operator", True

        await restore_workspace_project(runtime, second, args, choose=choose)
        assert len(seen) == 1
        assert (await runtime.project_scope.resolve(second)).data[
            "actor_id"
        ] == "different-operator"
        await runtime.run_existing(second, "context-fact")
        events = await runtime.sessions.read_session(second)
        assert next(e for e in events if e.type == "context/input").data["blocks"]
        third = await runtime.create_session(workspace)
        await restore_workspace_project(runtime, third, args, choose=choose)
        assert len(seen) == 1
        assert (await runtime.project_scope.resolve(third)).data["project_id"] == "context-project"
        assert load_profile(args.tui_profile)["project_workspace"] == str(workspace)


@pytest.mark.parametrize("mode", ["cancel", "drift", "wrong-workspace"])
async def test_project_cancel_or_invalid_preference_never_binds(tmp_path, mode):
    async with memory_case(tmp_path) as (runtime, _, _, first, _, _):
        workspace = await runtime.sessions.workspace_for(first)
        args = launch_args(tmp_path)
        args.tui_profile = tmp_path / "profile.json"
        new = await runtime.create_session(workspace)
        before = (await runtime.project_scope.catalog()).head

        async def cancel(_):
            return None

        if mode == "cancel":
            await restore_workspace_project(runtime, new, args, choose=cancel)
        else:
            args.default_project_id, args.project_actor_id = "context-project", "operator"
            args.project_workspace = workspace if mode == "drift" else tmp_path
            if mode == "drift":
                runtime.project_scope.resolver.mappings["context-source"] = tmp_path
            with pytest.raises(ValueError, match="workspace-project-preference-invalid"):
                await restore_workspace_project(runtime, new, args, choose=cancel)
        assert (await runtime.project_scope.catalog()).head == before
        assert not args.tui_profile.exists()


async def test_project_ambiguous_ui_requires_explicit_choice(tmp_path):
    app = ProjectChoiceApp((("one", "项目甲"), ("two", "项目乙")))
    async with app.run_test(size=(100, 36)) as pilot:
        app.query_one("#project-actor", Input).value = "tester"
        await pilot.click("#project-bind")
        assert app.return_value is None
        assert "请选择" in str(app.query_one("#project-choice-status", Static).render())
        await pilot.press("escape")


def test_project_preference_not_carried_by_copied_profile(tmp_path):
    args = launch_args(tmp_path)
    args.tui_explicit = {"workspace", "provider", "env_file", "data_dir"}
    values = form_values(args)
    values.update(
        default_project_id="other-project",
        project_actor_id="operator",
        project_workspace=str(tmp_path / "other"),
    )
    save_profile(tmp_path / ".traceh-tui.json", values)
    loaded, error = initial_settings(args)
    assert not error and loaded.default_project_id is None


async def test_session_list_current_workspace_and_public_selection(tmp_path):
    from test_tui import _Provider, _runtime

    from traceh.chat.session import open_chat_session
    from traceh.cli.tui_entry import RestartChat

    runtime, store = _runtime(tmp_path, _Provider())
    try:
        opened = await open_chat_session(runtime, workspace=tmp_path, session_id=None)
        old = await runtime.create_session(tmp_path)
        other = tmp_path / "other"
        other.mkdir()
        foreign = await runtime.create_session(other)
        choices = await session_choices(runtime, tmp_path)
        assert old in dict((sid, label) for label, sid in choices)
        assert foreign not in dict((sid, label) for label, sid in choices)
        args = launch_args(tmp_path)
        args.data_dir = runtime.config.data_dir
        app = TracehTuiApp(
            runtime,
            opened,
            timeline=False,
            heartbeat_seconds=0,
            settings_args=args,
            product=None,
            clock=default_clock(),
        )
        async with app.run_test(size=(110, 38)) as pilot:
            await pilot.press("ctrl+o")
            assert isinstance(app.screen, SessionPicker)
            app.screen.query_one("#session-choice", Select).value = old
            await pilot.click("#session-resume")
            await pilot.pause()
            await asyncio.wait_for(app._shutdown_task, 10)
        assert isinstance(app.return_value, RestartChat)
        assert app.return_value.args.session_id == old
        assert app.return_value.args.workspace is None
    finally:
        await runtime.dispose()


@pytest.mark.parametrize("choice", ["fresh", "cancel"])
async def test_old_data_button_chooses_new_path_without_mutating_old_events(tmp_path, choice):
    from traceh.api.events import PendingEvent
    from traceh.session.sqlite import SqliteEventStore

    args = launch_args(tmp_path)
    args.tui_profile = tmp_path / "profile.json"
    args._entry_workspace = tmp_path
    store = SqliteEventStore(args.data_dir / "events")
    try:
        await store.append(
            "session:old",
            events=(
                PendingEvent(
                    "session/created",
                    {
                        "session_id": "old",
                        "workspace": str(tmp_path),
                        "metadata": {},
                        "context_protocol": CONTEXT_PROTOCOL - 1,
                    },
                ),
            ),
            expected_seq=0,
        )
        before = await store.read("session:old")
        assert contains_old_data(args.data_dir)
        app = OldDataApp()
        async with app.run_test(size=(100, 30)) as pilot:
            if choice == "fresh":
                await pilot.click("#fresh-data")
            else:
                await pilot.press("escape")
        if choice == "fresh":
            assert app.return_value == "fresh"
            fresh = start_fresh(args)
            assert fresh.data_dir != args.data_dir and fresh.session_id is None
            assert not contains_old_data(fresh.data_dir)
            assert load_profile(args.tui_profile)["data_dir"] == str(fresh.data_dir)
        else:
            assert app.return_value is None
            assert not args.tui_profile.exists()
        assert await store.read("session:old") == before
    finally:
        await store.aclose()


@pytest.mark.parametrize("size", [(80, 24), (110, 38)])
async def test_quick_setup_primary_actions_remain_visible(tmp_path, size):
    args = launch_args(tmp_path)
    app = QuickSetupApp(args)
    async with app.run_test(size=size) as pilot:
        button = app.query_one("#setup-start", Button)
        assert app.screen.region.contains_region(button.region)
        await pilot.click("#setup-advanced")
    assert app.return_value == "advanced"


async def test_binding_index_failure_can_resume_without_duplicate_authority(tmp_path, monkeypatch):
    async with memory_case(tmp_path) as (runtime, _, _, first, _, _):
        workspace = await runtime.sessions.workspace_for(first)
        args = launch_args(tmp_path)
        args.default_project_id, args.project_actor_id = "context-project", "operator"
        args.project_workspace = workspace
        new = await runtime.create_session(workspace)
        original = runtime.memory.rebuild_index

        async def fail(_):
            raise ValueError("fixture-index-failure")

        async def never(_):
            pytest.fail("saved choice should not ask again")

        monkeypatch.setattr(runtime.memory, "rebuild_index", fail)
        with pytest.raises(ValueError, match="fixture-index-failure"):
            await restore_workspace_project(runtime, new, args, choose=never)
        committed = await runtime.project_scope.resolve(new)
        head = (await runtime.project_scope.catalog()).head
        monkeypatch.setattr(runtime.memory, "rebuild_index", original)
        await restore_workspace_project(runtime, new, args, choose=never)
        assert (await runtime.project_scope.catalog()).head == head
        assert await runtime.project_scope.resolve(new) == committed
        await runtime.run_existing(new, "context-fact")
        assert next(
            e for e in await runtime.sessions.read_session(new) if e.type == "context/input"
        ).data["blocks"]


async def test_project_automatic_binding_uses_real_git_source_proof(tmp_path):
    import shutil

    from memory_fixtures import bind, config
    from test_local_git_workspaces import _repository

    from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
    from traceh.session.sqlite import SqliteEventStore
    from traceh.workspaces.local_git import LocalGitWorkspaceProvider

    source, _ = _repository(tmp_path / "repo")
    resolver = LocalGitWorkspaceProvider(
        managed_root=tmp_path / "managed", sources={"source-orion": source}
    )
    store = SqliteEventStore(tmp_path / "events")
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data", memory=config(resolver)), event_store=store
    )
    try:
        first = await runtime.create_session(source)
        await bind(runtime.project_scope, first)
        args = launch_args(tmp_path)
        args.default_project_id, args.project_actor_id = "project-orion", "operator"
        args.project_workspace = source

        async def never(_):
            pytest.fail("saved preference must not require another choice")

        new = await runtime.create_session(source)
        await restore_workspace_project(runtime, new, args, choose=never)
        assert (await runtime.project_scope.resolve(new)).data["project_id"] == "project-orion"
        clone = tmp_path / "copy"
        shutil.copytree(source, clone)
        other = await runtime.create_session(clone)
        args.project_workspace = clone
        head = (await runtime.project_scope.catalog()).head
        with pytest.raises(ValueError, match="workspace-project-preference-invalid"):
            await restore_workspace_project(runtime, other, args, choose=never)
        assert (await runtime.project_scope.catalog()).head == head
        assert not (tmp_path / "managed").exists()
    finally:
        await runtime.dispose()
        await store.aclose()
