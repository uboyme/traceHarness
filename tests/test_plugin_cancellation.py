"""Cancellation during activation is cancellation, not a plugin failure.

The candidate implementation caught ``CancelledError`` inside a bare
``except BaseException`` and re-raised it as ``PluginActivationError`` with code
``plugin-setup-failed``. A user pressing Ctrl+C during startup was therefore told
their plugin configuration was broken.

Every test here drives the interrupt through an explicit gate rather than a
sleep, so "the caller was cancelled at exactly this point" is a fact rather than
a timing hope.
"""

from __future__ import annotations

import asyncio

import pytest
from plugin_fixtures import (
    RecordingTool,
    ScriptedPlugin,
    entry_point_for,
    manifest,
    provider_for,
)

from traceh.api.prompts import PromptSection
from traceh.api.services import ServiceKey
from traceh.kernel.registry import ServiceRegistry
from traceh.plugins.discovery import PluginDiscovery
from traceh.plugins.manager import PluginManager
from traceh.runtime.prompt import PromptAssembler
from traceh.tools.registry import ToolRegistry


def build_manager(*points, tools=None, prompt=None, services=None) -> PluginManager:
    return PluginManager(
        tools=tools if tools is not None else ToolRegistry(),
        prompt=prompt if prompt is not None else PromptAssembler(),
        services=services,
        discovery=PluginDiscovery(entry_points_provider=provider_for(*points)),
    )


async def _settle(times: int = 5) -> None:
    """Let the event loop actually run, rather than assuming it did."""

    for _ in range(times):
        await asyncio.sleep(0)


@pytest.fixture
async def loop_exceptions():
    """Record anything the loop reports, e.g. never-retrieved task exceptions.

    Async so it is set up inside the running loop the test will use.
    """

    loop = asyncio.get_running_loop()
    captured: list[dict] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: captured.append(context))
    yield captured
    loop.set_exception_handler(previous)


async def test_cancelling_setup_raises_cancelled_not_activation_error() -> None:
    gate = asyncio.Event()
    entered = asyncio.Event()
    plugin = ScriptedPlugin(manifest("a.plugin"), setup_gate=gate, setup_entered=entered)
    manager = build_manager(entry_point_for(plugin))

    task = asyncio.create_task(manager.activate(["a.plugin"]))
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_cancelling_setup_rolls_back_every_contribution() -> None:
    tools = ToolRegistry()
    prompt = PromptAssembler()
    services = ServiceRegistry()
    key = ServiceKey("example.service")
    gate = asyncio.Event()
    entered = asyncio.Event()

    done = ScriptedPlugin(
        manifest("a.done"),
        tools=(RecordingTool("first_tool"),),
        prompts=(PromptSection("first.section", "x"),),
        services=((key, "value"),),
    )
    blocking = ScriptedPlugin(
        manifest("b.blocking"),
        tools=(RecordingTool("second_tool"),),
        setup_gate=gate,
        setup_entered=entered,
    )
    manager = build_manager(
        entry_point_for(done),
        entry_point_for(blocking),
        tools=tools,
        prompt=prompt,
        services=services,
    )

    task = asyncio.create_task(manager.activate(["a.done", "b.blocking"]))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert tools.names() == ()
    assert prompt.section_ids() == ()
    assert services.get(key) is None
    assert done.cleanup_calls == 1
    assert blocking.cleanup_calls == 1


async def test_cancelling_setup_converges_owned_tasks() -> None:
    gate = asyncio.Event()
    entered = asyncio.Event()
    worker = ScriptedPlugin(manifest("a.worker"), spawn_forever=True)
    blocking = ScriptedPlugin(manifest("b.blocking"), setup_gate=gate, setup_entered=entered)
    manager = build_manager(entry_point_for(worker), entry_point_for(blocking))

    task = asyncio.create_task(manager.activate(["a.worker", "b.blocking"]))
    await entered.wait()
    await worker.owned_task_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert worker.owned_task is not None
    assert worker.owned_task.done(), "owned task still running after cancellation returned"
    assert worker.owned_task_cancelled


async def test_cancelling_health_check_raises_cancelled_not_activation_error() -> None:
    tools = ToolRegistry()
    gate = asyncio.Event()
    entered = asyncio.Event()
    plugin = ScriptedPlugin(
        manifest("a.plugin"),
        tools=(RecordingTool("plugin_tool"),),
        health_gate=gate,
        health_entered=entered,
        health_result=True,
    )
    manager = build_manager(entry_point_for(plugin), tools=tools)

    task = asyncio.create_task(manager.activate(["a.plugin"]))
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert tools.names() == ()
    assert plugin.cleanup_calls == 1


async def test_repeated_cancellation_cannot_cut_rollback_short() -> None:
    """A second and third Ctrl+C must not strand half the activations."""

    cleanup_gate = asyncio.Event()
    cleanup_entered = asyncio.Event()
    setup_gate = asyncio.Event()
    setup_entered = asyncio.Event()

    first = ScriptedPlugin(
        manifest("a.first"),
        tools=(RecordingTool("first_tool"),),
        cleanup_gate=cleanup_gate,
        cleanup_entered=cleanup_entered,
    )
    second = ScriptedPlugin(
        manifest("b.second"),
        tools=(RecordingTool("second_tool"),),
        setup_gate=setup_gate,
        setup_entered=setup_entered,
    )
    tools = ToolRegistry()
    manager = build_manager(
        entry_point_for(first), entry_point_for(second), tools=tools
    )

    task = asyncio.create_task(manager.activate(["a.first", "b.second"]))
    await setup_entered.wait()
    task.cancel()

    # Rollback has begun and is blocked inside a plugin's cleanup.
    await cleanup_entered.wait()
    for _ in range(3):
        task.cancel()
        await _settle()
        assert not task.done(), "repeated cancellation released the caller early"

    cleanup_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert first.cleanup_calls == 1
    assert second.cleanup_calls == 1
    assert tools.names() == ()


async def test_repeated_cancellation_during_publish_still_converges() -> None:
    cleanup_gate = asyncio.Event()
    cleanup_entered = asyncio.Event()
    health_gate = asyncio.Event()
    health_entered = asyncio.Event()

    plugin = ScriptedPlugin(
        manifest("a.plugin"),
        tools=(RecordingTool("plugin_tool"),),
        prompts=(PromptSection("plugin.section", "x"),),
        cleanup_gate=cleanup_gate,
        cleanup_entered=cleanup_entered,
        health_gate=health_gate,
        health_entered=health_entered,
        health_result=True,
        spawn_forever=True,
    )
    tools = ToolRegistry()
    prompt = PromptAssembler()
    manager = build_manager(entry_point_for(plugin), tools=tools, prompt=prompt)

    task = asyncio.create_task(manager.activate(["a.plugin"]))
    await health_entered.wait()
    await plugin.owned_task_started.wait()
    task.cancel()
    await cleanup_entered.wait()
    for _ in range(4):
        task.cancel()
        await _settle()
        assert not task.done()

    cleanup_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert tools.names() == ()
    assert prompt.section_ids() == ()
    assert plugin.owned_task is not None and plugin.owned_task.done()


async def test_cancellation_produces_no_never_retrieved_task_exception(
    loop_exceptions,
) -> None:
    gate = asyncio.Event()
    entered = asyncio.Event()
    worker = ScriptedPlugin(manifest("a.worker"), spawn_forever=True)
    blocking = ScriptedPlugin(manifest("b.blocking"), setup_gate=gate, setup_entered=entered)
    manager = build_manager(entry_point_for(worker), entry_point_for(blocking))

    task = asyncio.create_task(manager.activate(["a.worker", "b.blocking"]))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _settle(10)

    assert loop_exceptions == []


async def test_cancellation_is_not_recorded_as_a_plugin_failure() -> None:
    """Statuses must not blame the plugin for the operator's interrupt."""

    gate = asyncio.Event()
    entered = asyncio.Event()
    plugin = ScriptedPlugin(manifest("a.plugin"), setup_gate=gate, setup_entered=entered)
    manager = build_manager(entry_point_for(plugin))

    task = asyncio.create_task(manager.activate(["a.plugin"]))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    for status in manager.statuses:
        assert status.failure is None, "cancellation was recorded as a plugin failure"
        assert status.state != "failed"


async def test_cancelling_dispose_still_converges() -> None:
    cleanup_gate = asyncio.Event()
    cleanup_entered = asyncio.Event()
    plugin = ScriptedPlugin(
        manifest("a.plugin"),
        tools=(RecordingTool("plugin_tool"),),
        cleanup_gate=cleanup_gate,
        cleanup_entered=cleanup_entered,
        spawn_forever=True,
    )
    tools = ToolRegistry()
    manager = build_manager(entry_point_for(plugin), tools=tools)
    await manager.activate(["a.plugin"])
    await plugin.owned_task_started.wait()

    task = asyncio.create_task(manager.dispose())
    await cleanup_entered.wait()
    for _ in range(3):
        task.cancel()
        await _settle()
        assert not task.done()

    cleanup_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert tools.names() == ()
    assert plugin.owned_task is not None and plugin.owned_task.done()


async def test_a_genuine_setup_failure_is_still_reported_as_a_failure() -> None:
    """The cancellation fix must not turn real failures into silent cancellations."""

    from traceh.plugins.errors import PluginActivationError

    plugin = ScriptedPlugin(manifest("a.plugin"), setup_error=RuntimeError("nope"))
    manager = build_manager(entry_point_for(plugin))

    with pytest.raises(PluginActivationError) as info:
        await manager.activate(["a.plugin"])

    assert [f.code for f in info.value.failures] == ["plugin-setup-failed"]
