"""Real external servers on Plugin setup, rollback, Lease and Runtime shutdown."""

import asyncio
import subprocess
import tempfile
from dataclasses import replace
from types import SimpleNamespace

import pytest
from plugin_fixtures import entry_point_for, manifest, provider_for

from tests.sandbox_fixtures import real_sandbox_policy
from traceh.api.llm import ModelResponse, ToolCall
from traceh.api.sandbox import SandboxConfiguration, SandboxPluginGrant, SandboxStdioLimits
from traceh.api.tools import EffectKind, ToolOutput
from traceh.chat.sandbox_inspection import sandbox_report
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.plugins.discovery import PluginDiscovery
from traceh.plugins.errors import PluginActivationError
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime_async
from traceh.runtime.composition_runtime import CompositionDrainError
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.sandbox.stdio import SandboxStdioError
from traceh.session.event_store import InMemoryEventStore


class ServerPlugin:
    def __init__(self, *, failure=False, gate=None, check_quota=False):
        self.manifest = manifest("server.fixture", version="1.0.0")
        self.failure = failure
        self.gate = gate
        self.check_quota = check_quota
        self.ready = asyncio.Event()
        self.process = None
        self.context = None

    async def setup(self, context, config):
        self.context = context
        self.process = await context.open_process(
            ("python", "-u", "-c",
             "import sys,subprocess;subprocess.Popen(['python','-c','while True: pass'],"
             "start_new_session=True);print('server-ready',flush=True);"
             "exec('for line in sys.stdin.buffer:\\n sys.stdout.buffer.write(line);"
             "sys.stdout.buffer.flush()'.replace('\\\\n','\\n'))"),
            timeout_seconds=60,
        )
        assert await self.process.read(1024) == b"server-ready\n"
        self.ready.set()
        if self.check_quota:
            with pytest.raises(ValueError, match="process-limit"):
                await context.open_process(("python", "-c", "print('second')"), timeout_seconds=1)
        context.register_tool(ServerEchoTool(self.process))
        if self.gate:
            await self.gate.wait()
        if self.failure:
            raise RuntimeError("explicit fixture setup failure")


class ServerEchoTool:
    name = "server_echo"
    description = "Echo a test message through the activation-owned server."
    effect_kind = EffectKind.EXTERNAL_TRANSACTION
    input_schema = {
        "type": "object", "properties": {"text": {"type": "string"}},
        "required": ["text"], "additionalProperties": False,
    }

    def __init__(self, process):
        self.process = process

    async def execute(self, arguments, context):
        await self.process.write((arguments["text"] + "\n").encode())
        return ToolOutput(content=(await self.process.read(1024)).decode())


def parameters(tmp_path, plugin, *, grant=True, version="1.0.0"):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = InMemoryEventStore()
    policy = real_sandbox_policy()
    policy = replace(policy, limits=replace(policy.limits, wall_seconds=60))
    grants = (SandboxPluginGrant(
        plugin.manifest.plugin_id, version, workspace, SandboxStdioLimits(4096, 1024), 1,
    ),) if grant else ()
    return store, workspace, policy, dict(
        config=RuntimeConfig(
            data_dir=tmp_path / "data",
            sandbox=SandboxConfiguration(policy, tmp_path / "cas", grants),
        ),
        event_store=store,
        enabled_plugins=(plugin.manifest.plugin_id,),
        plugin_discovery=PluginDiscovery(
            entry_points_provider=provider_for(entry_point_for(plugin))
        ),
    )


async def assert_closed(store, policy, plugin):
    streams = await store.list_streams(prefix="plugin-activation:")
    assert len(streams) == 1
    events = await store.read(streams[0])
    assert [e.type for e in events] == ["sandbox/request", "sandbox/outcome"]
    assert events[0].data["owner"]["plugin_id"] == plugin.manifest.plugin_id
    assert events[0].data["owner"]["plugin_version"] == plugin.manifest.version
    assert streams[0] == "plugin-activation:" + events[0].data["owner"]["activation_id"]
    assert events[1].data["converged"] is True
    report = await sandbox_report(store, session_id="another-session", policy=policy)
    assert plugin.manifest.plugin_id in report
    assert "应用级插件进程" in report and "激活实例" in report
    assert "标准输入总量：4096 字节" in report
    assert "server-ready" not in report
    name = "traceh-exec-" + events[0].data["execution_id"]

    def inspect():
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(
                ["docker", "--context", policy.docker_context, "ps", "-a", "--filter",
                 f"name=^/{name}$", "--format", "{{.ID}}"],
                stdout=output, stderr=subprocess.DEVNULL, timeout=10,
            )
            assert result.returncode == 0
            output.seek(0)
            assert output.read() == b""
    await asyncio.to_thread(inspect)
    with pytest.raises(SandboxStdioError, match="closed"):
        await plugin.process.write(b"after-close\n")


async def test_real_generation_lease_keeps_server_until_drain(tmp_path):
    plugin = ServerPlugin()
    store, workspace, policy, kwargs = parameters(tmp_path, plugin)
    runtime = await build_default_runtime_async(**kwargs)
    try:
        with pytest.raises(RuntimeError, match="closed after setup"):
            await plugin.context.open_process(("python",), timeout_seconds=1)
        async with runtime.loop.compositions.lease(
            workspace=workspace, session_id="lease-fixture", turn_id="turn", step_id="step"
        ):
            await runtime.replace_plugin_composition(())
            await plugin.process.write(b"old-lease-still-works\n")
            assert await plugin.process.read(1024) == b"old-lease-still-works\n"
            streams = await store.list_streams(prefix="plugin-activation:")
            assert [e.type for e in await store.read(streams[0])] == ["sandbox/request"]
        await runtime.loop.compositions.drain()
        await assert_closed(store, policy, plugin)
    finally:
        await runtime.dispose()


async def test_real_setup_failure_rolls_back_started_server(tmp_path):
    plugin = ServerPlugin(failure=True)
    store, _, policy, kwargs = parameters(tmp_path, plugin)
    with pytest.raises(PluginActivationError):
        await build_default_runtime_async(**kwargs)
    assert plugin.ready.is_set()
    await assert_closed(store, policy, plugin)


async def test_real_cancel_setup_converges_server_before_return(tmp_path):
    plugin = ServerPlugin(gate=asyncio.Event())
    store, _, policy, kwargs = parameters(tmp_path, plugin)
    building = asyncio.create_task(build_default_runtime_async(**kwargs))
    async with asyncio.timeout(45):
        await plugin.ready.wait()
    building.cancel()
    building.cancel()
    with pytest.raises(asyncio.CancelledError):
        await building
    await assert_closed(store, policy, plugin)


async def test_real_runtime_shutdown_closes_server(tmp_path):
    plugin = ServerPlugin(check_quota=True)
    store, _, policy, kwargs = parameters(tmp_path, plugin)
    runtime = await build_default_runtime_async(**kwargs)
    await plugin.process.write(b"live-before-shutdown\n")
    assert await plugin.process.read(1024) == b"live-before-shutdown\n"
    await runtime.dispose()
    await runtime.dispose()
    await assert_closed(store, policy, plugin)


async def test_real_plugin_cleanup_failure_is_durable_and_drain_reports_it(tmp_path, monkeypatch):
    import traceh.sandbox.docker as docker

    class FailedControlCleanup:
        def __init__(self, **kwargs):
            self.real = tempfile.TemporaryDirectory(**kwargs)
            self.name = self.real.name

        def cleanup(self):
            self.real.cleanup()
            raise OSError("synthetic cleanup boundary failure")

    monkeypatch.setattr(docker, "tempfile", SimpleNamespace(
        TemporaryDirectory=FailedControlCleanup, TemporaryFile=tempfile.TemporaryFile,
    ))
    plugin = ServerPlugin()
    store, _, policy, kwargs = parameters(tmp_path, plugin)
    runtime = await build_default_runtime_async(**kwargs)
    assert plugin.ready.is_set()
    with pytest.raises(CompositionDrainError):
        await runtime.dispose()
    streams = await store.list_streams(prefix="plugin-activation:")
    outcome = (await store.read(streams[0]))[-1]
    assert outcome.type == "sandbox/outcome"
    assert outcome.data["converged"] is True
    assert outcome.data["cleanup_failures"] == ["sandbox-control-cleanup-failed"]
    assert runtime.loop.compositions.cleanup_failures
    with pytest.raises(CompositionDrainError):
        await runtime.dispose()
    await assert_closed(store, policy, plugin)


async def test_real_agent_tool_uses_server_with_original_effect_and_replay(tmp_path):
    plugin = ServerPlugin()
    store, workspace, policy, kwargs = parameters(tmp_path, plugin)
    kwargs["provider"] = ScriptedLlmProvider((
        ModelResponse(content="", tool_calls=(
            ToolCall(id="server-call", name="server_echo", arguments={"text": "传输主线"}),
        )), ModelResponse(content="completed"),
    ))
    runtime = await build_default_runtime_async(**kwargs)
    try:
        result = await runtime.run(workspace, "exercise the external server")
        assert result.reason == "completed"
        session_id = (await runtime.sessions.list_sessions())[0]
        events = await runtime.sessions.read_session(session_id)
        effects = await runtime.sessions.read_effects(session_id)
        assert "传输主线" in str(next(e.data for e in events if e.type == "tool/result"))
        assert len([e for e in effects if e.type == "effect/intent"]) == 1
        assert len([e for e in effects if e.type == "effect/outcome"]) == 1
        assert runtime.invariants.check(events, effects) == ()
        assert await verify_request_snapshots(runtime.sessions, runtime.surface, session_id) == ()
    finally:
        await runtime.dispose()
    await assert_closed(store, policy, plugin)


@pytest.mark.parametrize("grant,version", [(False, "1.0.0"), (True, "2.0.0")])
async def test_missing_or_wrong_version_grant_fails_before_external_start(tmp_path, grant, version):
    # Even with the admission check removed, this fixture rolls its actual
    # process back before asserting rejection. A reverse proof cannot leak it.
    plugin = ServerPlugin(failure=True)
    store, _, _, kwargs = parameters(tmp_path, plugin, grant=grant, version=version)
    with pytest.raises(PluginActivationError):
        await build_default_runtime_async(**kwargs)
    assert plugin.process is None
    assert await store.list_streams(prefix="plugin-activation:") == ()
