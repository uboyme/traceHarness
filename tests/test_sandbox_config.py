"""Explicit host policy, launch persistence, and real CLI execution."""

import asyncio
import json
import os
import re
import shlex
from dataclasses import asdict

import pytest
from sandbox_fixtures import real_sandbox_policy

from traceh.cli.main import build_parser, main
from traceh.cli.tui_config import (
    LaunchConfigurationError,
    apply_values,
    form_values,
    load_profile,
    preflight,
    save_profile,
)
from traceh.sandbox.config import MAX_CONFIG_BYTES, load_sandbox_file, parse_sandbox_config
from traceh.session.service import SessionService
from traceh.session.sqlite import SqliteEventStore


def draft():
    # A synthetic identity is sufficient for parser tests; never execute it.
    return dict(format=2, plugin_grants=[], policy=dict(
        docker_context="explicit-test-connection", image="sha256:" + "a" * 64,
        network="none", read_paths=["src"], write_paths=["src"], excluded_paths=[],
        limits=dict(memory_bytes=134217728, workspace_bytes=1048576,
                    workspace_files=64, output_bytes=4096, pids=16,
                    cpus=0.5, wall_seconds=5),
    ))


@pytest.fixture(autouse=True)
def isolated_launch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in tuple(os.environ):
        if name.startswith("TRACEH_") and not name.startswith("TRACEH_SANDBOX_TEST_"):
            monkeypatch.delenv(name)


def launch(tmp_path, path):
    return build_parser().parse_args([
        "chat", str(tmp_path), "--provider", "scripted",
        "--env-file", str(tmp_path / "absent.env"),
        "--data-dir", str(tmp_path / "data"), "--sandbox-config", str(path),
    ])


def test_policy_file_profile_and_preflight_share_one_parser(tmp_path):
    path = tmp_path / "沙箱.json"
    path.write_text(json.dumps(draft()), encoding="utf-8")
    policy = load_sandbox_file(path).policy
    assert policy.docker_context == "explicit-test-connection"
    assert policy.write_paths == ("src",)
    args = launch(tmp_path, path)
    profile = tmp_path / "launch.json"
    save_profile(profile, form_values(args))
    restored = apply_values(args, load_profile(profile))
    assert restored.sandbox_config == path
    assert "校验通过" in preflight(restored)
    assert not (tmp_path / "data").exists()
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(LaunchConfigurationError, match="沙箱配置无效"):
        preflight(restored)
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("change", [
    lambda d: d.update(format=True),
    lambda d: d.update(format=1),
    lambda d: d.pop("plugin_grants"),
    lambda d: d["policy"].update(image="mutable:latest"),
    lambda d: d["policy"].pop("docker_context"),
    lambda d: d["policy"].update(network="host"),
    lambda d: d["policy"].update(write_paths=["outside"]),
    lambda d: d["policy"].update(read_paths=["../escape"]),
    lambda d: d["policy"]["limits"].update(pids=True),
    lambda d: d["policy"]["limits"].update(cpus=float("inf")),
])
def test_policy_rejects_missing_identity_expanded_scope_and_invalid_limits(change):
    raw = draft()
    change(raw)
    with pytest.raises(ValueError, match="^sandbox-host-config-invalid$"):
        parse_sandbox_config(raw)


def test_policy_loader_bounds_input_and_hides_untrusted_errors(tmp_path):
    path = tmp_path / "policy.json"
    for content in (b"x" * (MAX_CONFIG_BYTES + 1), b'not-json-synthetic-private-value'):
        path.write_bytes(content)
        with pytest.raises(ValueError, match="^sandbox-host-config-invalid$"):
            load_sandbox_file(path)
    with pytest.raises(ValueError, match="^sandbox-host-config-invalid$"):
        load_sandbox_file(tmp_path / "missing")


@pytest.mark.parametrize("change", [
    lambda g: g.update(workspace="relative"),
    lambda g: g.update(plugin_id=""),
    lambda g: g.update(version=""),
    lambda g: g.update(max_processes=True),
    lambda g: g.update(max_processes=0),
    lambda g: g["stdio"].update(frame_bytes=8192),
    lambda g: g["stdio"].update(extra=1),
    lambda g: g.update(environment={}),
])
def test_plugin_grants_reject_implicit_identity_path_or_unbounded_limits(tmp_path, change):
    raw = draft()
    grant = dict(plugin_id="grant.fixture", version="1.0", workspace=str(tmp_path),
                 max_processes=1, stdio=dict(input_bytes=4096, frame_bytes=1024))
    raw["plugin_grants"] = [grant]
    parsed = parse_sandbox_config(raw)
    assert parsed.plugin_grants[0].workspace == tmp_path
    change(grant)
    with pytest.raises(ValueError, match="^sandbox-host-config-invalid$"):
        parse_sandbox_config(raw)


def test_duplicate_plugin_grants_are_not_silently_selected(tmp_path):
    raw = draft()
    grant = dict(plugin_id="grant.fixture", version="1.0", workspace=str(tmp_path),
                 max_processes=1, stdio=dict(input_bytes=4096, frame_bytes=1024))
    raw["plugin_grants"] = [grant, dict(grant, version="2.0")]
    with pytest.raises(ValueError, match="^sandbox-host-config-invalid$"):
        parse_sandbox_config(raw)


async def test_eval_refuses_application_server_grants_before_creating_attempts(
    tmp_path, monkeypatch,
):
    from traceh.api.llm import ModelResponse
    from traceh.cli.main import CliConfigurationError, _eval
    from traceh.llm.scripted import ScriptedLlmProvider

    raw = draft()
    raw["plugin_grants"] = [dict(
        plugin_id="grant.fixture", version="1.0", workspace=str(tmp_path), max_processes=1,
        stdio=dict(input_bytes=4096, frame_bytes=1024),
    )]
    path = tmp_path / "sandbox.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    args = build_parser().parse_args([
        "eval", str(tmp_path / "manifest"), "--output", str(tmp_path / "attempts"),
        "--sandbox-config", str(path),
    ])
    monkeypatch.setattr("traceh.cli.main._provider_and_model", lambda args: (
        ScriptedLlmProvider((ModelResponse(content="unused"),)), "fixture",
    ))
    with pytest.raises(CliConfigurationError, match="eval-application-plugin-process-grants"):
        await _eval(args)
    assert not args.output.exists()


def test_real_cli_uses_selected_policy_and_original_sqlite_evidence(tmp_path, capsys):
    policy = real_sandbox_policy()
    workspace = tmp_path / "work"
    workspace.mkdir()
    configuration = tmp_path / "sandbox.json"
    configuration.write_text(
        json.dumps(dict(format=2, plugin_grants=[], policy=asdict(policy))), encoding="utf-8"
    )
    script = tmp_path / "responses.json"
    script.write_text(json.dumps([
        dict(tool_calls=[dict(id="cli-exec", name="shell", arguments=dict(command=shlex.join([
            "python", "-c",
            "from pathlib import Path; Path('result.txt').write_text('actual-cli'); "
            "print('真实沙箱')",
        ])))]),
        dict(content="CLI finished"),
    ]), encoding="utf-8")
    with pytest.raises(SystemExit) as exited:
        main([
            "run", str(workspace), "perform an isolated edit", "--provider", "scripted",
            "--script", str(script), "--sandbox-config", str(configuration),
            "--env-file", str(tmp_path / "absent.env"), "--data-dir", str(tmp_path / "data"),
        ])
    assert exited.value.code == 0
    assert (workspace / "result.txt").read_text() == "actual-cli"
    session_id = re.search(r"session_id=([^\s]+)", capsys.readouterr().out).group(1)

    async def inspect():
        store = SqliteEventStore(tmp_path / "data" / "events")
        try:
            events = await store.read(SessionService.effect_stream(session_id))
            request = next(e for e in events if e.type == "sandbox/request")
            assert request.data["owner"]["tool_call_id"] == "cli-exec"
            assert request.data["policy"]["image"] == policy.image
            assert any(e.type == "sandbox/publication" for e in events)
            outcome = next(e for e in events if e.type == "sandbox/outcome")
            assert outcome.data["converged"] is True
            from traceh.chat.sandbox_inspection import sandbox_report

            before = await store.read(SessionService.effect_stream(session_id))
            report = await sandbox_report(store, session_id=session_id, policy=None)
            assert "未启用" in report  # Current off does not erase actual historical evidence.
            assert "实际后端：Docker Engine" in report
            assert "结束状态：finished" in report
            assert "回写状态：completed" in report
            assert outcome.data["digest"] in report
            assert "actual-cli" not in report  # No source, argv or output body in the UI.
            assert await store.read(SessionService.effect_stream(session_id)) == before
        finally:
            await store.aclose()

    asyncio.run(inspect())


def test_real_cli_plugin_grant_reaches_original_activation_and_tool(tmp_path, monkeypatch, capsys):
    from plugin_fixtures import entry_point_for, provider_for

    from test_sandbox_plugins import ServerPlugin
    from traceh.plugins.discovery import PluginDiscovery

    plugin = ServerPlugin()
    discovery = PluginDiscovery(entry_points_provider=provider_for(entry_point_for(plugin)))
    # Replace installed-distribution discovery only; real CLI/Manager/setup,
    # Docker execution, SQLite, ToolRuntime and shutdown are exercised below.
    monkeypatch.setattr("traceh.plugins.manager.PluginDiscovery", lambda: discovery)
    workspace = tmp_path / "server"
    workspace.mkdir()
    raw = dict(format=2, policy=asdict(real_sandbox_policy()), plugin_grants=[dict(
        plugin_id=plugin.manifest.plugin_id, version=plugin.manifest.version,
        workspace=str(workspace), max_processes=1,
        stdio=dict(input_bytes=4096, frame_bytes=1024),
    )])
    config = tmp_path / "sandbox.json"
    config.write_text(json.dumps(raw), encoding="utf-8")
    script = tmp_path / "responses.json"
    script.write_text(json.dumps([
        dict(tool_calls=[
            dict(id="external-cli", name="server_echo", arguments=dict(text="hello")),
        ]),
        dict(content="completed"),
    ]), encoding="utf-8")
    with pytest.raises(SystemExit) as exited:
        main([
            "run", str(workspace), "test the configured server", "--provider", "scripted",
            "--plugin", plugin.manifest.plugin_id, "--script", str(script),
            "--sandbox-config", str(config), "--data-dir", str(tmp_path / "data"),
            "--env-file", str(tmp_path / "absent.env"),
        ])
    assert exited.value.code == 0
    session_id = re.search(r"session_id=([^\s]+)", capsys.readouterr().out).group(1)

    async def inspect():
        store = SqliteEventStore(tmp_path / "data" / "events")
        try:
            streams = await store.list_streams(prefix="plugin-activation:")
            assert len(streams) == 1
            events = await store.read(streams[0])
            assert [e.type for e in events] == ["sandbox/request", "sandbox/outcome"]
            assert events[-1].data["converged"] is True
            assert events[0].data["owner"]["plugin_version"] == plugin.manifest.version
            effects = await store.read(SessionService.effect_stream(session_id))
            assert any(e.type == "effect/outcome" for e in effects)
        finally:
            await store.aclose()
    asyncio.run(inspect())
