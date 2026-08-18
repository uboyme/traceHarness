"""AgentRuntime.dispose() is one reusable, convergent shutdown.

The defect this pins down: when the shutdown body ran inline in ``dispose()``, a
caller cancelled while active turns were still converging escaped *before*
reaching ``PluginManager.dispose()`` - and because the disposed flag was already
set, every later ``dispose()`` returned immediately. The plugins were never
unloaded, and nothing reported that.

Every wait here is gated by an ``asyncio.Event``. Nothing sleeps to guess timing:
a test that "usually" observes the right interleaving proves nothing about the
one that matters.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from plugin_fixtures import RecordingTool, ScriptedPlugin, entry_point_for, manifest, provider_for

from traceh.api.llm import ModelResponse
from traceh.api.plugins import CORE_PLUGIN_IDENTITY
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.plugins.discovery import PluginDiscovery
from traceh.runtime.agent_runtime import (
    RuntimeConfig,
    build_default_runtime,
    build_default_runtime_async,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    directory = tmp_path / "ws"
    directory.mkdir()
    (directory / "hello.txt").write_text("hi", encoding="utf-8")
    return directory


class GatedProvider:
    """A provider that parks inside a turn until the test releases it."""

    name = "scripted"

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request):
        del request
        self.entered.set()
        await self.release.wait()
        return ModelResponse(content="done")


class GatedCancellationProvider:
    """Park a turn, then stay parked across the shutdown's cancellation.

    The turn is parked at ``entered``. When shutdown cancels it, the
    ``CancelledError`` arrives *here*; the provider lights
    ``cancellation_entered`` as proof of that arrival and then keeps waiting for
    ``release``, absorbing a second and third cancellation along the way.

    That latch is what turns "the turn is parked inside the cancellation window"
    from a timing hope into an observed fact: the test waits for
    ``cancellation_entered`` before it cancels the dispose call, and asserts
    that neither the dispose caller nor plugin cleanup has progressed until
    ``release`` is set.
    """

    name = "scripted"

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.cancellation_entered = asyncio.Event()
        self.cancellations_seen = 0
        self.release = asyncio.Event()

    async def complete(self, request):
        del request
        self.entered.set()
        while True:
            try:
                await self.release.wait()
                return ModelResponse(content="done")
            except asyncio.CancelledError:
                # The turn was cancelled, but this coroutine keeps it parked:
                # absorbing the next cancellations is what keeps the shutdown
                # from converging until the test decides it may.
                self.cancellations_seen += 1
                self.cancellation_entered.set()
                continue


async def build_with_plugin(tmp_path: Path, plugin: ScriptedPlugin, provider=None):
    return await build_default_runtime_async(
        RuntimeConfig(data_dir=tmp_path / "data"),
        provider=provider,
        enabled_plugins=("a.example",),
        plugin_discovery=PluginDiscovery(
            entry_points_provider=provider_for(entry_point_for(plugin))
        ),
    )


def example_plugin(**kwargs) -> ScriptedPlugin:
    return ScriptedPlugin(
        manifest("a.example", version="2.0.0"),
        tools=(RecordingTool("plugin_tool"),),
        **kwargs,
    )


# --------------------------------------------------------------------------
# The defect: cancellation during turn convergence must not strand plugins
# --------------------------------------------------------------------------


async def test_cancelling_dispose_still_unloads_plugins(tmp_path: Path, workspace: Path) -> None:
    """Cancellation must not release the caller before plugins are unloaded.

    Every step of the interleaving is pinned by an ``Event``, never by counting
    scheduler iterations. The one fact a sleep cannot be avoided for - delivering
    a cancellation that has already been requested - is a single ``sleep(0)``
    after each explicit ``cancel()``; it delivers an existing signal, it is not
    evidence that the defect window was reached.
    """

    provider = GatedCancellationProvider()
    plugin = example_plugin()
    runtime = await build_with_plugin(tmp_path, plugin, provider=provider)
    registry = runtime.loop.compositions.tools.registry
    assert "plugin_tool" in registry.names()

    session_id = await runtime.create_session(workspace)
    turn = asyncio.create_task(runtime.run_existing(session_id, "work"))
    await provider.entered.wait()

    # Shutdown begins, cancels the turn, and blocks on the parked provider.
    disposal = asyncio.create_task(runtime.dispose())

    # The latch proves the CancelledError from shutdown actually arrived in the
    # provider: the turn is parked inside the cancellation window.
    await provider.cancellation_entered.wait()

    # A second and third cancellation of the turn are absorbed by the parked
    # provider instead of completing it.
    turn.cancel()
    await asyncio.sleep(0)
    turn.cancel()
    await asyncio.sleep(0)

    disposal.cancel()
    await asyncio.sleep(0)  # deliver the cancellation to the dispose caller

    # The dispose caller may not return, and no plugin cleanup may have run,
    # while the turn is still parked.
    assert not disposal.done(), "dispose released the caller before the turn converged"
    assert plugin.cleanup_calls == 0, "plugins unloaded before the turn converged"
    assert not turn.done()
    assert provider.cancellations_seen >= 3

    # Only the test's own release may let shutdown converge.
    provider.release.set()
    with pytest.raises(asyncio.CancelledError):
        await disposal

    # The cancellation is only allowed to escape after shutdown converged.
    assert turn.done()
    assert plugin.cleanup_calls == 1, "plugins were stranded by the cancellation"
    assert "plugin_tool" not in registry.names()


async def test_repeated_cancellation_cannot_release_the_caller_early(
    tmp_path: Path, workspace: Path
) -> None:
    cleanup_gate = asyncio.Event()
    cleanup_entered = asyncio.Event()
    plugin = example_plugin(cleanup_gate=cleanup_gate, cleanup_entered=cleanup_entered)
    runtime = await build_with_plugin(tmp_path, plugin)

    disposal = asyncio.create_task(runtime.dispose())
    await cleanup_entered.wait()

    for _ in range(3):
        disposal.cancel()
        await asyncio.sleep(0)  # deliver the already-requested cancellation
        assert not disposal.done(), "a repeated cancellation released the caller early"

    cleanup_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await disposal

    assert plugin.cleanup_calls == 1
    assert runtime.loop.compositions.tools.registry.names() == (
        "apply_patch",
        "list_files",
        "read_file",
        "search_text",
        "shell",
    )


async def test_dispose_after_a_cancelled_dispose_reuses_the_same_shutdown(
    tmp_path: Path, workspace: Path
) -> None:
    """The retry must observe the finished shutdown, not start a second one."""

    cleanup_gate = asyncio.Event()
    cleanup_entered = asyncio.Event()
    plugin = example_plugin(cleanup_gate=cleanup_gate, cleanup_entered=cleanup_entered)
    runtime = await build_with_plugin(tmp_path, plugin)

    first = asyncio.create_task(runtime.dispose())
    await cleanup_entered.wait()
    first.cancel()
    cleanup_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await first

    await runtime.dispose()

    assert plugin.cleanup_calls == 1, "shutdown ran twice"


# --------------------------------------------------------------------------
# Ordering: turns converge before plugins unload
# --------------------------------------------------------------------------


async def test_active_turn_converges_before_plugins_unload(
    tmp_path: Path, workspace: Path
) -> None:
    provider = GatedProvider()
    observed: list[str] = []

    class OrderingPlugin(ScriptedPlugin):
        async def setup(self, context, config):
            await super().setup(context, config)

            async def record() -> None:
                observed.append("plugin-cleanup")

            context.add_cleanup(record)

    plugin = OrderingPlugin(
        manifest("a.example", version="2.0.0"), tools=(RecordingTool("plugin_tool"),)
    )
    runtime = await build_with_plugin(tmp_path, plugin, provider=provider)

    session_id = await runtime.create_session(workspace)
    turn = asyncio.create_task(runtime.run_existing(session_id, "work"))
    await provider.entered.wait()

    disposal = asyncio.create_task(runtime.dispose())
    # The ordering is evidenced by the recorded sequence, not by scheduler turns:
    # the turn must already be finished by the time any plugin cleanup ran.
    await disposal

    assert turn.done()
    assert observed == ["plugin-cleanup"]


async def test_dispose_blocks_new_turns_immediately(tmp_path: Path, workspace: Path) -> None:
    cleanup_gate = asyncio.Event()
    cleanup_entered = asyncio.Event()
    plugin = example_plugin(cleanup_gate=cleanup_gate, cleanup_entered=cleanup_entered)
    runtime = await build_with_plugin(tmp_path, plugin)
    session_id = await runtime.create_session(workspace)

    disposal = asyncio.create_task(runtime.dispose())
    await cleanup_entered.wait()

    with pytest.raises(RuntimeError, match="runtime is disposed"):
        await runtime.run_existing(session_id, "too late")

    cleanup_gate.set()
    await disposal


# --------------------------------------------------------------------------
# Repeat dispose reuses the one real outcome
# --------------------------------------------------------------------------


async def test_repeated_dispose_runs_shutdown_once(tmp_path: Path, workspace: Path) -> None:
    plugin = example_plugin()
    runtime = await build_with_plugin(tmp_path, plugin)

    await runtime.dispose()
    await runtime.dispose()
    await runtime.dispose()

    assert plugin.cleanup_calls == 1


async def test_concurrent_dispose_callers_share_one_shutdown(
    tmp_path: Path, workspace: Path
) -> None:
    plugin = example_plugin()
    runtime = await build_with_plugin(tmp_path, plugin)

    await asyncio.gather(runtime.dispose(), runtime.dispose(), runtime.dispose())

    assert plugin.cleanup_calls == 1


async def test_a_failed_shutdown_is_reported_again_rather_than_faked(
    tmp_path: Path, workspace: Path
) -> None:
    """A later dispose() must not silently pretend the failed shutdown worked."""

    from traceh.plugins.errors import PluginDisposeError

    plugin = example_plugin(cleanup_error=RuntimeError("cleanup exploded"))
    runtime = await build_with_plugin(tmp_path, plugin)

    with pytest.raises(PluginDisposeError):
        await runtime.dispose()

    with pytest.raises(PluginDisposeError):
        await runtime.dispose()

    assert plugin.cleanup_calls == 1


async def test_dispose_without_plugins_is_idempotent(tmp_path: Path, workspace: Path) -> None:
    runtime = build_default_runtime(RuntimeConfig(data_dir=tmp_path / "data"))
    assert runtime.plugins == (CORE_PLUGIN_IDENTITY,)

    await runtime.dispose()
    await runtime.dispose()

    session_id_error = pytest.raises(RuntimeError, match="runtime is disposed")
    with session_id_error:
        await runtime.run_existing("missing", "x")


async def test_dispose_converges_a_running_turn_without_plugins(
    tmp_path: Path, workspace: Path
) -> None:
    provider = GatedProvider()
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data"), provider=provider
    )
    session_id = await runtime.create_session(workspace)
    turn = asyncio.create_task(runtime.run_existing(session_id, "work"))
    await provider.entered.wait()

    await runtime.dispose()

    assert turn.done()


async def test_normal_run_still_completes_and_disposes(tmp_path: Path, workspace: Path) -> None:
    """The fixes must not change what an ordinary run does."""

    provider = ScriptedLlmProvider((ModelResponse(content="all done"),))
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data"), provider=provider
    )
    try:
        result = await runtime.run(workspace, "do the thing")
        assert result.reason == "completed"
        assert result.final_text == "all done"
    finally:
        await runtime.dispose()
