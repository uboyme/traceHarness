from __future__ import annotations

from pathlib import Path

import pytest

from traceh.runtime.agent_runtime import (
    PluginCompositionReplacement,
    RuntimeConfig,
    build_default_runtime,
)
from traceh.runtime.composition_runtime import CompositionGeneration
from traceh.runtime.plugin_composition import PluginCompositionCoordinator

pytestmark = pytest.mark.asyncio


class _PublisherWithoutPoisoned:
    """A custom publisher implementing the pre-D0 replacement surface only."""

    def __init__(self, delegate) -> None:
        self._delegate = delegate

    @property
    def current_generation(self) -> CompositionGeneration:
        return self._delegate.current_generation

    async def publish(self, generation: CompositionGeneration) -> int:
        return await self._delegate.publish(generation)


async def test_agent_runtime_does_not_own_plugin_control_plane_state(
    tmp_path: Path,
) -> None:
    runtime = build_default_runtime(RuntimeConfig(data_dir=tmp_path / "data"))
    try:
        assert isinstance(runtime._plugin_compositions, PluginCompositionCoordinator)
        facade_state = vars(runtime)
        assert "_composition_gate" not in facade_state
        assert "_composition_replacement_lock" not in facade_state
        assert "_replacement_tasks" not in facade_state
        assert "_turn_admission_tasks" not in facade_state
        assert "_plugin_builder" not in facade_state
        assert "_assembly_llms" not in facade_state
        assert "_assembly_policies" not in facade_state
        assert "_assembly_middlewares" not in facade_state
    finally:
        await runtime.dispose()


async def test_runtime_shutdown_converges_control_plane_before_composition_drain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = build_default_runtime(RuntimeConfig(data_dir=tmp_path / "data"))
    order: list[str] = []
    original_control_shutdown = runtime._plugin_compositions.shutdown_inflight
    original_composition_dispose = runtime.loop.compositions.dispose

    async def control_shutdown():
        order.append("control")
        return await original_control_shutdown()

    async def composition_dispose():
        order.append("composition")
        await original_composition_dispose()

    monkeypatch.setattr(
        runtime._plugin_compositions,
        "shutdown_inflight",
        control_shutdown,
    )
    monkeypatch.setattr(runtime.loop.compositions, "dispose", composition_dispose)

    await runtime.dispose()

    assert order == ["control", "composition"]


async def test_custom_generation_publisher_does_not_need_a_poisoned_property(
    tmp_path: Path,
) -> None:
    runtime = build_default_runtime(RuntimeConfig(data_dir=tmp_path / "data"))
    session_id = await runtime.create_session(tmp_path)
    runtime._plugin_compositions._compositions = _PublisherWithoutPoisoned(
        runtime.loop.compositions
    )
    try:
        replacement = await runtime.reload_plugin_composition(session_id)

        assert replacement.enabled_plugin_ids == ()
    finally:
        await runtime.dispose()


async def test_reload_preserves_the_public_migration_dispatch_point(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = build_default_runtime(RuntimeConfig(data_dir=tmp_path / "data"))
    expected_plugin_ids = ("facade-dispatch.example",)
    expected = PluginCompositionReplacement(generation_id=37, plugins=())
    calls: list[tuple[str, tuple[str, ...]]] = []

    async def migrate(
        session_id: str,
        enabled_plugin_ids: tuple[str, ...],
        **_kwargs: object,
    ) -> PluginCompositionReplacement:
        calls.append((session_id, enabled_plugin_ids))
        return expected

    monkeypatch.setattr(
        type(runtime),
        "enabled_plugin_ids",
        property(lambda _runtime: expected_plugin_ids),
    )
    monkeypatch.setattr(runtime, "migrate_session_plugin_composition", migrate)
    try:
        result = await runtime.reload_plugin_composition("dispatch-session")

        assert result is expected
        assert calls == [("dispatch-session", expected_plugin_ids)]
    finally:
        await runtime.dispose()
