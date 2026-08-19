"""Stage B contracts for Generation-owned plugin ActivationSets.

These tests drive the real default runtime and its internal replacement API.
They deliberately use Events and observable cleanup counters instead of timing
guesses: a retired generation must remain usable until its Lease exits.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from plugin_fixtures import RecordingTool, ScriptedPlugin, entry_point_for, manifest, provider_for

from traceh.api.services import ServiceKey
from traceh.api.tools import EffectKind, ToolExecutionContext, ToolOutput
from traceh.plugins import (
    PluginActivationError,
    PluginActivationSet,
    PluginDiscovery,
    PluginDisposeError,
)
from traceh.runtime.agent_runtime import (
    RuntimeConfig,
    SessionPluginMismatchError,
    build_default_runtime_async,
)
from traceh.runtime.composition_runtime import CompositionDrainError, CompositionGeneration
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.event_store import InMemoryEventStore
from traceh.session.service import SessionService
from traceh.tools.runtime import ToolRuntime


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    directory = tmp_path / "workspace"
    directory.mkdir()
    return directory


def discovery_for(*plugins: object) -> PluginDiscovery:
    return PluginDiscovery(
        entry_points_provider=provider_for(*(entry_point_for(plugin) for plugin in plugins))
    )


async def build_plain(tmp_path: Path):
    return await build_default_runtime_async(RuntimeConfig(data_dir=tmp_path / "data"))


async def build_with_plugin(tmp_path: Path, plugin: object, plugin_id: str = "a.example"):
    return await build_default_runtime_async(
        RuntimeConfig(data_dir=tmp_path / "data"),
        enabled_plugins=(plugin_id,),
        plugin_discovery=discovery_for(plugin),
    )


async def test_default_runtime_and_startup_plugin_use_one_owned_activation_set(
    tmp_path: Path,
) -> None:
    plain = await build_plain(tmp_path / "plain")
    plugin = ScriptedPlugin(
        manifest("a.example"),
        tools=(RecordingTool("plugin_tool"),),
    )
    with_plugin = await build_with_plugin(tmp_path / "plugin", plugin)
    try:
        plain_set = plain.loop.compositions.current_activation_set
        plugin_set = with_plugin.loop.compositions.current_activation_set
        assert isinstance(plain_set, PluginActivationSet)
        assert isinstance(plugin_set, PluginActivationSet)
        assert plain_set.state == "owned"
        assert plugin_set.state == "owned"
        assert with_plugin._plugin_manager is None
        assert with_plugin.plugins == plugin_set.identities
        assert with_plugin.services is plugin_set.services
    finally:
        await plain.dispose()
        await with_plugin.dispose()

    assert plain_set.state == "disposed"
    assert plugin_set.state == "disposed"
    assert plugin.cleanup_calls == 1


async def test_setup_failure_keeps_current_generation_and_rolls_back_candidate(
    tmp_path: Path,
) -> None:
    runtime = await build_plain(tmp_path)
    before = runtime.loop.compositions.current_generation
    plugin = ScriptedPlugin(
        manifest("a.example"),
        tools=(RecordingTool("candidate_tool"),),
        setup_error=RuntimeError("secret setup detail"),
    )
    try:
        with pytest.raises(PluginActivationError) as caught:
            await runtime.replace_plugin_composition(
                ("a.example",), plugin_discovery=discovery_for(plugin)
            )
        assert "secret setup detail" not in str(caught.value)
        assert runtime.loop.compositions.current_generation is before
        assert runtime.loop.compositions.plugins == (before.plugins[0],)
        assert "candidate_tool" not in runtime.loop.compositions.tools.registry.names()
        assert plugin.cleanup_calls == 1
    finally:
        await runtime.dispose()


async def test_conflict_is_rejected_before_health_and_current_is_untouched(
    tmp_path: Path,
) -> None:
    runtime = await build_plain(tmp_path)
    before = runtime.loop.compositions.current_generation
    plugin = ScriptedPlugin(
        manifest("a.example"),
        tools=(RecordingTool("shell"),),
        has_health_check=True,
        health_result=True,
    )
    try:
        with pytest.raises(PluginActivationError):
            await runtime.replace_plugin_composition(
                ("a.example",), plugin_discovery=discovery_for(plugin)
            )
        assert plugin.health_calls == 0
        assert plugin.cleanup_calls == 1
        assert runtime.loop.compositions.current_generation is before
    finally:
        await runtime.dispose()


async def test_candidate_setup_cancellation_rolls_back_before_rethrowing(
    tmp_path: Path,
) -> None:
    setup_entered = asyncio.Event()
    setup_gate = asyncio.Event()
    plugin = ScriptedPlugin(
        manifest("a.example"),
        setup_entered=setup_entered,
        setup_gate=setup_gate,
    )
    runtime = await build_plain(tmp_path)
    before = runtime.loop.compositions.current_generation
    replacement = asyncio.create_task(
        runtime.replace_plugin_composition(
            ("a.example",), plugin_discovery=discovery_for(plugin)
        )
    )
    await setup_entered.wait()
    replacement.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await replacement
        assert runtime.loop.compositions.current_generation is before
        assert plugin.cleanup_calls == 1
    finally:
        await runtime.dispose()


async def test_candidate_health_cancellation_rolls_back_before_rethrowing(
    tmp_path: Path,
) -> None:
    health_entered = asyncio.Event()
    health_gate = asyncio.Event()
    plugin = ScriptedPlugin(
        manifest("a.example"),
        health_entered=health_entered,
        health_gate=health_gate,
        health_result=True,
    )
    runtime = await build_plain(tmp_path)
    before = runtime.loop.compositions.current_generation
    replacement = asyncio.create_task(
        runtime.replace_plugin_composition(
            ("a.example",), plugin_discovery=discovery_for(plugin)
        )
    )
    await health_entered.wait()
    replacement.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await replacement
        assert runtime.loop.compositions.current_generation is before
        assert plugin.health_calls == 1
        assert plugin.cleanup_calls == 1
    finally:
        await runtime.dispose()


async def test_candidate_rollback_absorbs_repeated_cancellation(
    tmp_path: Path,
) -> None:
    setup_entered = asyncio.Event()
    cleanup_entered = asyncio.Event()
    cleanup_gate = asyncio.Event()
    plugin = ScriptedPlugin(
        manifest("a.example"),
        setup_entered=setup_entered,
        setup_gate=asyncio.Event(),
        cleanup_entered=cleanup_entered,
        cleanup_gate=cleanup_gate,
    )
    runtime = await build_plain(tmp_path)
    replacement = asyncio.create_task(
        runtime.replace_plugin_composition(
            ("a.example",), plugin_discovery=discovery_for(plugin)
        )
    )
    await setup_entered.wait()
    replacement.cancel()
    await asyncio.wait_for(cleanup_entered.wait(), timeout=5)
    for _ in range(3):
        replacement.cancel()
        await asyncio.sleep(0)
        assert not replacement.done()
    cleanup_gate.set()
    try:
        with pytest.raises(asyncio.CancelledError):
            await replacement
    finally:
        await runtime.dispose()
    assert plugin.cleanup_calls == 1


async def test_dispose_converges_an_inflight_replacement_before_return(
    tmp_path: Path,
) -> None:
    setup_entered = asyncio.Event()
    setup_gate = asyncio.Event()
    cleanup_entered = asyncio.Event()
    cleanup_gate = asyncio.Event()
    plugin = ScriptedPlugin(
        manifest("a.example"),
        setup_entered=setup_entered,
        setup_gate=setup_gate,
        cleanup_entered=cleanup_entered,
        cleanup_gate=cleanup_gate,
        spawn_forever=True,
    )
    runtime = await build_plain(tmp_path)
    replacement = asyncio.create_task(
        runtime.replace_plugin_composition(
            ("a.example",), plugin_discovery=discovery_for(plugin)
        )
    )
    await setup_entered.wait()
    await plugin.owned_task_started.wait()

    disposing = asyncio.create_task(runtime.dispose())
    await cleanup_entered.wait()
    assert not disposing.done()
    assert not replacement.done()
    with pytest.raises(RuntimeError, match="runtime is disposed"):
        await runtime.replace_plugin_composition(
            ("a.example",), plugin_discovery=discovery_for(plugin)
        )

    cleanup_gate.set()
    await disposing
    with pytest.raises(asyncio.CancelledError):
        await replacement
    assert plugin.owned_task is not None and plugin.owned_task.done()
    assert plugin.cleanup_calls == 1


async def test_dispose_absorbs_repeated_cancellation_during_replacement_rollback(
    tmp_path: Path,
) -> None:
    setup_entered = asyncio.Event()
    setup_gate = asyncio.Event()
    cleanup_entered = asyncio.Event()
    cleanup_gate = asyncio.Event()
    plugin = ScriptedPlugin(
        manifest("a.example"),
        setup_entered=setup_entered,
        setup_gate=setup_gate,
        cleanup_entered=cleanup_entered,
        cleanup_gate=cleanup_gate,
        spawn_forever=True,
    )
    runtime = await build_plain(tmp_path)
    replacement = asyncio.create_task(
        runtime.replace_plugin_composition(
            ("a.example",), plugin_discovery=discovery_for(plugin)
        )
    )
    await setup_entered.wait()
    await plugin.owned_task_started.wait()
    disposing = asyncio.create_task(runtime.dispose())
    await cleanup_entered.wait()

    for _ in range(3):
        disposing.cancel()
        await asyncio.sleep(0)
        assert not disposing.done()

    cleanup_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await disposing
    with pytest.raises(asyncio.CancelledError):
        await replacement
    assert plugin.owned_task is not None and plugin.owned_task.done()
    assert plugin.cleanup_calls == 1


async def test_dispose_reports_inflight_replacement_rollback_failure(
    tmp_path: Path,
) -> None:
    """Runtime shutdown must own and report a cancelled candidate's cleanup failure."""

    setup_entered = asyncio.Event()
    record: list[str] = []
    healthy = ScriptedPlugin(manifest("a.healthy"), record=record)
    broken = ScriptedPlugin(
        manifest("b.broken"),
        setup_gate=asyncio.Event(),
        setup_entered=setup_entered,
        cleanup_error=RuntimeError("FAKE-FIXTURE rollback secret"),
        record=record,
    )
    runtime = await build_plain(tmp_path)
    replacement = asyncio.create_task(
        runtime.replace_plugin_composition(
            ("a.healthy", "b.broken"),
            plugin_discovery=discovery_for(healthy, broken),
        )
    )
    await setup_entered.wait()

    with pytest.raises(PluginDisposeError) as first:
        await runtime.dispose()
    with pytest.raises(PluginDisposeError) as replacement_failure:
        await replacement
    with pytest.raises(PluginDisposeError) as second:
        await runtime.dispose()

    expected = [("plugin-rollback-failed", "b.broken")]
    assert [(item.code, item.plugin_id) for item in first.value.failures] == expected
    assert [
        (item.code, item.plugin_id) for item in replacement_failure.value.failures
    ] == expected
    assert [(item.code, item.plugin_id) for item in second.value.failures] == expected
    assert "FAKE-FIXTURE rollback secret" not in str(first.value)
    assert healthy.cleanup_calls == 1
    assert broken.cleanup_calls == 1
    assert record[-2:] == ["cleanup:b.broken", "cleanup:a.healthy"]


async def test_generation_construction_failure_cleans_candidate_once_and_is_retryable(
    tmp_path: Path,
) -> None:
    runtime = await build_plain(tmp_path)
    plugin = ScriptedPlugin(
        manifest("a.example"),
        tools=(RecordingTool("candidate_tool"),),
    )
    try:
        builder = runtime._plugin_compositions._plugin_builder
        candidate = await builder.prepare(
            ("a.example",), discovery=discovery_for(plugin)
        )
        candidate_tools = ToolRuntime(
            candidate.tools,
            runtime.sessions,
            policies=runtime._plugin_compositions._policies,
            middlewares=runtime._plugin_compositions._middlewares,
        )
        with pytest.raises(LookupError):
            CompositionGeneration(
                llms=runtime._plugin_compositions._llms,
                tools=candidate_tools,
                prompt=candidate.prompt,
                provider="missing-provider",
                model="scripted-model",
                plugins=candidate.identities,
                activation_set=candidate,
            )
        await candidate.dispose()
        assert candidate.state == "disposed"
        assert plugin.cleanup_calls == 1

        # The same builder and fresh plugin resources remain usable after the
        # failed constructor; no owner state was committed by the bad attempt.
        retry_plugin = ScriptedPlugin(
            manifest("a.example"),
            tools=(RecordingTool("candidate_tool"),),
        )
        retry = await builder.prepare(
            ("a.example",), discovery=discovery_for(retry_plugin)
        )
        await retry.dispose()
        assert retry_plugin.cleanup_calls == 1
    finally:
        await runtime.dispose()


async def test_publish_rejection_rolls_back_candidate_without_changing_current(
    tmp_path: Path,
) -> None:
    runtime = await build_plain(tmp_path)
    before = runtime.loop.compositions.current_generation
    plugin = ScriptedPlugin(
        manifest("a.example"),
        tools=(RecordingTool("candidate_tool"),),
    )
    candidate = None
    try:
        candidate = await runtime._plugin_compositions._plugin_builder.prepare(
            ("a.example",), discovery=discovery_for(plugin)
        )
        other_sessions = SessionService(InMemoryEventStore())
        candidate_tools = ToolRuntime(
            candidate.tools,
            other_sessions,
            policies=runtime._plugin_compositions._policies,
            middlewares=runtime._plugin_compositions._middlewares,
        )
        generation = CompositionGeneration(
            llms=runtime._plugin_compositions._llms,
            tools=candidate_tools,
            prompt=candidate.prompt,
            provider="scripted",
            model="scripted-model",
            plugins=candidate.identities,
            activation_set=candidate,
        )
        with pytest.raises(ValueError, match="session service"):
            await runtime.loop.compositions.publish(generation)
        await candidate.dispose()
        assert runtime.loop.compositions.current_generation is before
        assert plugin.cleanup_calls == 1
    finally:
        if candidate is not None and candidate.state == "candidate":
            await candidate.dispose()
        await runtime.dispose()


async def test_one_activation_set_cannot_be_published_to_two_runtimes(
    tmp_path: Path,
) -> None:
    first = await build_plain(tmp_path / "first")
    second = await build_plain(tmp_path / "second")
    plugin = ScriptedPlugin(
        manifest("a.example"),
        tools=(RecordingTool("shared_candidate_tool"),),
    )
    candidate = await first._plugin_compositions._plugin_builder.prepare(
        ("a.example",), discovery=discovery_for(plugin)
    )
    try:
        first_tools = ToolRuntime(
            candidate.tools,
            first.sessions,
            policies=first._plugin_compositions._policies,
            middlewares=first._plugin_compositions._middlewares,
        )
        first_generation = CompositionGeneration(
            llms=first._plugin_compositions._llms,
            tools=first_tools,
            prompt=candidate.prompt,
            provider="scripted",
            model="scripted-model",
            plugins=candidate.identities,
            activation_set=candidate,
        )
        await first.loop.compositions.publish(first_generation)

        second_tools = ToolRuntime(
            candidate.tools,
            second.sessions,
            policies=second._plugin_compositions._policies,
            middlewares=second._plugin_compositions._middlewares,
        )
        second_generation = CompositionGeneration(
            llms=second._plugin_compositions._llms,
            tools=second_tools,
            prompt=candidate.prompt,
            provider="scripted",
            model="scripted-model",
            plugins=candidate.identities,
            activation_set=candidate,
        )
        with pytest.raises(ValueError, match="ActivationSet"):
            await second.loop.compositions.publish(second_generation)
        assert second.loop.compositions.current_generation.plugins == (
            second.loop.compositions.current_generation.plugins[0],
        )
    finally:
        await first.dispose()
        await second.dispose()
    assert plugin.cleanup_calls == 1


class _ServiceState:
    def __init__(self, label: str) -> None:
        self.label = label
        self.closed = False


class _ServiceTool:
    description = "Stage B service-bound test tool."
    input_schema = {"type": "object", "additionalProperties": False}
    effect_kind = EffectKind.PURE_READ

    def __init__(self, name: str, service: _ServiceState) -> None:
        self.name = name
        self.service = service

    async def execute(self, arguments, context: ToolExecutionContext) -> ToolOutput:
        del arguments, context
        if self.service.closed:
            raise RuntimeError(f"{self.service.label} service is closed")
        return ToolOutput(content=self.service.label)


class _ServicePlugin:
    def __init__(self, version: str, label: str) -> None:
        self.manifest = manifest("a.example", version=version)
        self.label = label
        self.services: list[_ServiceState] = []
        self.tools: list[_ServiceTool] = []
        self.cleanup_calls = 0

    async def setup(self, context, config) -> None:
        del config
        key = ServiceKey("stage_b.service")
        service = _ServiceState(self.label)
        await context.provide(key, service)
        tool = _ServiceTool(f"{self.label}_tool", context.require(key))
        context.register_tool(tool)
        self.services.append(service)
        self.tools.append(tool)

        async def cleanup() -> None:
            self.cleanup_calls += 1
            service.closed = True

        context.add_cleanup(cleanup)

async def test_old_lease_keeps_old_service_and_tool_until_exit(
    tmp_path: Path, workspace: Path
) -> None:
    first_plugin = _ServicePlugin("1.0.0", "v1")
    second_plugin = _ServicePlugin("2.0.0", "v2")
    runtime = await build_with_plugin(tmp_path, first_plugin)
    try:
        lease = runtime.loop.compositions.lease(
            workspace=workspace,
            session_id="session",
            turn_id="turn",
            step_id="step",
        )
        async with lease as old:
            replacement = await runtime.replace_plugin_composition(
                ("a.example",), plugin_discovery=discovery_for(second_plugin)
            )
            assert replacement.plugins[1].version == "2.0.0"
            assert first_plugin.cleanup_calls == 0
            assert runtime.services.require(ServiceKey("stage_b.service")).label == "v2"
            assert old.services is not None
            assert old.services.require(ServiceKey("stage_b.service")).label == "v1"
            assert [
                schema.name for schema in old.snapshot.tools if schema.name.endswith("_tool")
            ] == ["v1_tool"]
            output = await old.tools.registry.require("v1_tool").execute(
                {},
                ToolExecutionContext(
                    "session", "turn", "step", "call", workspace, tmp_path
                ),
            )
            assert output.content == "v1"
        await runtime.loop.compositions.drain()
        assert first_plugin.cleanup_calls == 1
        assert first_plugin.services[0].closed

        async with runtime.loop.compositions.lease(
            workspace=workspace,
            session_id="session",
            turn_id="turn-2",
            step_id="step-2",
        ) as current:
            assert current.snapshot.plugins[1].version == "2.0.0"
            output = await current.tools.registry.require("v2_tool").execute(
                {},
                ToolExecutionContext(
                    "session", "turn-2", "step-2", "call", workspace, tmp_path
                ),
            )
            assert output.content == "v2"
    finally:
        await runtime.dispose()
    assert second_plugin.cleanup_calls == 1


async def test_old_owned_task_survives_switch_and_is_joined_after_old_lease(
    tmp_path: Path, workspace: Path
) -> None:
    first_plugin = ScriptedPlugin(
        manifest("a.example", version="1.0.0"),
        tools=(RecordingTool("v1_tool"),),
        spawn_forever=True,
    )
    second_plugin = ScriptedPlugin(
        manifest("a.example", version="2.0.0"),
        tools=(RecordingTool("v2_tool"),),
        spawn_forever=True,
    )
    runtime = await build_with_plugin(tmp_path, first_plugin)
    try:
        await first_plugin.owned_task_started.wait()
        async with runtime.loop.compositions.lease(
            workspace=workspace,
            session_id="session",
            turn_id="turn",
            step_id="step",
        ):
            await runtime.replace_plugin_composition(
                ("a.example",), plugin_discovery=discovery_for(second_plugin)
            )
            assert first_plugin.owned_task is not None
            assert not first_plugin.owned_task.done()
            assert first_plugin.cleanup_calls == 0
        await runtime.loop.compositions.drain()
        assert first_plugin.owned_task is not None and first_plugin.owned_task.done()
        assert first_plugin.owned_task_cancelled
    finally:
        await runtime.dispose()
    assert second_plugin.cleanup_calls == 1


async def test_replacement_does_not_migrate_an_existing_session(
    tmp_path: Path, workspace: Path
) -> None:
    data_dir = tmp_path / "data"
    first_plugin = ScriptedPlugin(
        manifest("a.example", version="1.0.0"),
        tools=(RecordingTool("v1_tool"),),
    )
    second_plugin = ScriptedPlugin(
        manifest("a.example", version="2.0.0"),
        tools=(RecordingTool("v2_tool"),),
    )
    runtime = await build_default_runtime_async(
        RuntimeConfig(data_dir=data_dir),
        enabled_plugins=("a.example",),
        plugin_discovery=discovery_for(first_plugin),
    )
    session_id = await runtime.create_session(workspace)
    try:
        await runtime.run_existing(session_id, "v1")
        await runtime.replace_plugin_composition(
            ("a.example",), plugin_discovery=discovery_for(second_plugin)
        )
        with pytest.raises(SessionPluginMismatchError):
            await runtime.run_existing(session_id, "v2")

        replacement_session_id = await runtime.create_session(workspace)
        await runtime.run_existing(replacement_session_id, "v2")
        events = await runtime.sessions.read_session(replacement_session_id)
        snapshots = [event for event in events if event.type == "composition/snapshot"]
        assert snapshots[-1].data["plugins"][-1] == {
            "plugin_id": "a.example",
            "version": "2.0.0",
        }
        assert all("generation_id" not in event.data for event in events)
        assert (
            await verify_request_snapshots(
                runtime.sessions, runtime.surface, replacement_session_id
            )
            == ()
        )
    finally:
        await runtime.dispose()

    recovered = await build_default_runtime_async(
        RuntimeConfig(data_dir=data_dir),
        enabled_plugins=("a.example",),
        plugin_discovery=discovery_for(
            ScriptedPlugin(
                manifest("a.example", version="2.0.0"),
                tools=(RecordingTool("v2_tool"),),
            )
        ),
    )
    try:
        await recovered.verify_session_plugins(replacement_session_id)
    finally:
        await recovered.dispose()


async def test_replacement_does_not_bypass_an_incompatible_session(
    tmp_path: Path, workspace: Path
) -> None:
    data_dir = tmp_path / "data"
    incompatible = await build_default_runtime_async(
        RuntimeConfig(data_dir=data_dir),
        enabled_plugins=("a.example",),
        plugin_discovery=discovery_for(
            ScriptedPlugin(manifest("a.example", version="3.0.0"))
        ),
    )
    session_id = await incompatible.create_session(workspace)
    await incompatible.dispose()

    runtime = await build_default_runtime_async(
        RuntimeConfig(data_dir=data_dir),
        enabled_plugins=("a.example",),
        plugin_discovery=discovery_for(
            ScriptedPlugin(manifest("a.example", version="1.0.0"))
        ),
    )
    try:
        await runtime.replace_plugin_composition(
            ("a.example",),
            plugin_discovery=discovery_for(
                ScriptedPlugin(manifest("a.example", version="2.0.0"))
            ),
        )
        with pytest.raises(SessionPluginMismatchError):
            await runtime.verify_session_plugins(session_id)
    finally:
        await runtime.dispose()


async def test_publish_without_a_step_is_not_a_durable_session_switch(
    tmp_path: Path, workspace: Path
) -> None:
    data_dir = tmp_path / "data"
    first_plugin = ScriptedPlugin(
        manifest("a.example", version="1.0.0"),
        tools=(RecordingTool("v1_tool"),),
    )
    second_plugin = ScriptedPlugin(
        manifest("a.example", version="2.0.0"),
        tools=(RecordingTool("v2_tool"),),
    )
    runtime = await build_default_runtime_async(
        RuntimeConfig(data_dir=data_dir),
        enabled_plugins=("a.example",),
        plugin_discovery=discovery_for(first_plugin),
    )
    session_id = await runtime.create_session(workspace)
    try:
        await runtime.run_existing(session_id, "v1")
        await runtime.replace_plugin_composition(
            ("a.example",), plugin_discovery=discovery_for(second_plugin)
        )
    finally:
        await runtime.dispose()

    old_runtime = await build_default_runtime_async(
        RuntimeConfig(data_dir=data_dir),
        enabled_plugins=("a.example",),
        plugin_discovery=discovery_for(
            ScriptedPlugin(
                manifest("a.example", version="1.0.0"),
                tools=(RecordingTool("v1_tool"),),
            )
        ),
    )
    try:
        await old_runtime.verify_session_plugins(session_id)
    finally:
        await old_runtime.dispose()


async def test_replacement_cleanup_failure_does_not_skip_other_plugin_cleanup(
    tmp_path: Path,
) -> None:
    broken = ScriptedPlugin(
        manifest("a.broken"),
        cleanup_error=RuntimeError("do not expose this"),
    )
    healthy = ScriptedPlugin(manifest("b.healthy"))
    runtime = await build_default_runtime_async(
        RuntimeConfig(data_dir=tmp_path / "data"),
        enabled_plugins=("a.broken", "b.healthy"),
        plugin_discovery=discovery_for(broken, healthy),
    )
    try:
        with pytest.raises(CompositionDrainError) as caught:
            await runtime.dispose()
        assert broken.cleanup_calls == 1
        assert healthy.cleanup_calls == 1
        assert "do not expose this" not in str(caught.value)
    finally:
        # The failed dispose task is idempotent and already converged; a second
        # call observes the same structured result without running cleanup.
        with pytest.raises(CompositionDrainError):
            await runtime.dispose()


async def test_activation_set_dispose_absorbs_repeated_cancellation(
    tmp_path: Path,
) -> None:
    gate = asyncio.Event()
    entered = asyncio.Event()
    plugin = ScriptedPlugin(
        manifest("a.example"),
        cleanup_gate=gate,
        cleanup_entered=entered,
    )
    runtime = await build_plain(tmp_path)
    candidate = await runtime._plugin_compositions._plugin_builder.prepare(
        ("a.example",), discovery=discovery_for(plugin)
    )
    try:
        disposing = asyncio.create_task(candidate.dispose())
        await entered.wait()
        for _ in range(3):
            disposing.cancel()
            await asyncio.sleep(0)
            assert not disposing.done()
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await disposing
        assert candidate.state == "disposed"
        assert plugin.cleanup_calls == 1
    finally:
        await runtime.dispose()
