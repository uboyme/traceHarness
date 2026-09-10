"""Actual settings UI paths: discovery, manual references, save and cancel."""

import asyncio
import json
import os

import pytest

pytest.importorskip("textual")
from test_tui_config_forms import click, edit, open_form, select
from test_tui_settings import isolated_environment, launch_args  # noqa: F401
from textual.widgets import Input, Select, Static

from traceh.cli.tui_config import form_values
from traceh.sandbox.config import load_sandbox_file
from traceh.tui import docker_choices
from traceh.tui.config_forms import ConfigForm
from traceh.tui.settings import ConfigurationApp, SettingsScreen

IMAGE = "sha256:" + "c" * 64
OTHER = "sha256:" + "d" * 64
REAL_CONTEXT = os.environ.get("TRACEH_SANDBOX_TEST_CONTEXT")
REAL_IMAGE = os.environ.get("TRACEH_SANDBOX_TEST_IMAGE")


async def finish_query(app, pilot):
    screen = app.screen
    if isinstance(screen, ConfigForm) and screen._docker_task is not None:
        await asyncio.wait_for(asyncio.shield(screen._docker_task), 15)
    await pilot.pause()


async def refresh_pick(app, pilot, key, value):
    await select(app, pilot, ("policy", key))
    await click(app, pilot, "#docker-refresh")
    await finish_query(app, pilot)
    picker = app.screen.query_one("#docker-options", Select)
    picker.value = value
    await pilot.pause()
    assert app.screen.query_one("#config-value", Input).value == value
    await click(app, pilot, "#config-update")
    await finish_query(app, pilot)


async def scopes(app, pilot):
    for key in ("read_paths", "write_paths"):
        await select(app, pilot, ("policy", key))
        await click(app, pilot, "#config-add")
        await edit(app, pilot, ("policy", key, 0), "editable")


@pytest.fixture
def docker(monkeypatch):
    calls = []
    state = {"tag_id": IMAGE, "fail": False}

    async def query(*argv):
        calls.append(argv)
        if state["fail"]:
            raise docker_choices.DockerChoiceError("Docker 查询失败，请检查连接。")
        if argv[:2] == ("context", "ls"):
            return '"engine-a"\n"engine-b"\n'
        if "ls" in argv:
            return json.dumps(dict(ID=IMAGE, Repository="work/python", Tag="ready"))
        ref = argv[-1]
        return json.dumps(dict(
            id=state["tag_id"] if ref == "work/python:ready" else ref,
            os="linux", volumes=None,
        ))

    monkeypatch.setattr(docker_choices, "_query", query)
    return calls, state


@pytest.mark.parametrize("manual", [False, True])
async def test_dropdown_or_manual_name_saves_pinned_id_and_keeps_it_after_tag_moves(
    tmp_path, docker, manual,
):
    calls, state = docker
    args = launch_args(tmp_path)
    app = ConfigurationApp(args, form_values(args), tmp_path / "profile.json")
    path = tmp_path / ".traceh-sandbox.json"
    async with app.run_test(size=(110, 40)) as pilot:
        await open_form(app, pilot, "sandbox")
        if manual:
            await edit(app, pilot, ("policy", "docker_context"), "engine-a")
            await edit(app, pilot, ("policy", "image"), "work/python:ready")
            await finish_query(app, pilot)
        else:
            await refresh_pick(app, pilot, "docker_context", "engine-a")
            # Observe a name, then change its tag before the explicit selection.
            state["tag_id"] = OTHER
            await refresh_pick(app, pilot, "image", IMAGE)
        assert app.screen.raw["policy"]["image"] == IMAGE
        await scopes(app, pilot)
        state["tag_id"] = OTHER
        await click(app, pilot, "#config-save")
        await finish_query(app, pilot)
        assert isinstance(app.screen, SettingsScreen)
        assert load_sandbox_file(path).policy.image == IMAGE
        assert calls[-1][-1] == IMAGE  # Save rechecks the fixed ID, not the tag.
        before = path.read_bytes()
        await open_form(app, pilot, "sandbox")
        await click(app, pilot, "#config-save")
        await finish_query(app, pilot)
        assert path.read_bytes() == before
        assert not args.data_dir.exists()


async def test_switch_connection_clears_image_and_failed_refresh_cannot_restore_it(
    tmp_path, docker,
):
    _, state = docker
    args = launch_args(tmp_path)
    app = ConfigurationApp(args, form_values(args), tmp_path / "profile.json")
    async with app.run_test(size=(110, 40)) as pilot:
        await open_form(app, pilot, "sandbox")
        await refresh_pick(app, pilot, "docker_context", "engine-a")
        await refresh_pick(app, pilot, "image", IMAGE)
        await edit(app, pilot, ("policy", "docker_context"), "engine-b")
        await select(app, pilot, ("policy", "image"))
        assert app.screen.query_one("#docker-options", Select).is_blank()
        state["fail"] = True
        await click(app, pilot, "#docker-refresh")
        await finish_query(app, pilot)
        assert "查询失败" in str(app.screen.query_one("#config-form-status", Static).render())
        state["fail"] = False
        await scopes(app, pilot)
        await click(app, pilot, "#config-save")
        await finish_query(app, pilot)
        assert isinstance(app.screen, ConfigForm)
        assert not (tmp_path / ".traceh-sandbox.json").exists()


async def test_existing_file_is_preserved_on_failed_lookup_stale_save_and_cancel(tmp_path, docker):
    _, state = docker
    args = launch_args(tmp_path)
    app = ConfigurationApp(args, form_values(args), tmp_path / "profile.json")
    path = tmp_path / ".traceh-sandbox.json"
    async with app.run_test(size=(110, 40)) as pilot:
        await open_form(app, pilot, "sandbox")
        await refresh_pick(app, pilot, "docker_context", "engine-a")
        await refresh_pick(app, pilot, "image", IMAGE)
        await scopes(app, pilot)
        await click(app, pilot, "#config-save")
        await finish_query(app, pilot)
        before = path.read_bytes()
        await open_form(app, pilot, "sandbox")
        state["fail"] = True
        await click(app, pilot, "#config-save")
        await finish_query(app, pilot)
        assert path.read_bytes() == before
        assert isinstance(app.screen, ConfigForm)
        state["fail"] = False
        path.write_bytes(before + b"\n")
        await click(app, pilot, "#config-save")
        await finish_query(app, pilot)
        assert "重新加载" in str(app.screen.query_one("#config-form-status", Static).render())
        assert path.read_bytes() == before + b"\n"
        await pilot.press("escape")
        assert isinstance(app.screen, SettingsScreen)


async def test_escape_waits_for_query_cleanup_and_never_saves(tmp_path, monkeypatch):
    started, cleaning, release, done = (asyncio.Event() for _ in range(4))

    async def query(*argv):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            done.set()

    monkeypatch.setattr(docker_choices, "_query", query)
    args = launch_args(tmp_path)
    app = ConfigurationApp(args, form_values(args), tmp_path / "profile.json")
    async with app.run_test(size=(110, 40)) as pilot:
        await open_form(app, pilot, "sandbox")
        await select(app, pilot, ("policy", "docker_context"))
        await click(app, pilot, "#docker-refresh")
        await asyncio.wait_for(started.wait(), 2)
        escape = asyncio.create_task(pilot.press("escape"))
        try:
            await asyncio.wait_for(cleaning.wait(), 2)
            assert isinstance(app.screen, ConfigForm)
            assert not done.is_set()
        finally:
            release.set()
            await asyncio.wait_for(escape, 3)
        assert done.is_set()
        assert isinstance(app.screen, SettingsScreen)
        assert not (tmp_path / ".traceh-sandbox.json").exists()


@pytest.mark.skipif(not REAL_CONTEXT or not REAL_IMAGE, reason="explicit local Docker selection")
async def test_real_docker_dropdown_to_saved_configuration(tmp_path):
    args = launch_args(tmp_path)
    app = ConfigurationApp(args, form_values(args), tmp_path / "profile.json")
    async with app.run_test(size=(120, 42)) as pilot:
        await open_form(app, pilot, "sandbox")
        await refresh_pick(app, pilot, "docker_context", REAL_CONTEXT)
        await refresh_pick(app, pilot, "image", REAL_IMAGE)
        await scopes(app, pilot)
        await click(app, pilot, "#config-save")
        await finish_query(app, pilot)
        assert isinstance(app.screen, SettingsScreen)
        policy = load_sandbox_file(tmp_path / ".traceh-sandbox.json").policy
        assert policy.docker_context == REAL_CONTEXT and policy.image == REAL_IMAGE
        assert not args.data_dir.exists()
