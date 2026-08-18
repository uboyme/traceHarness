"""Shared fakes for plugin tests.

Not named ``test_*`` so pytest does not collect it.

These fakes stand in for ``importlib.metadata`` entry points and distributions so
the manager can be driven deterministically. They are *not* a substitute for
proving the real entry-point path works: ``tests/test_plugin_wheel_e2e.py`` builds
real wheels, installs them into a clean virtual environment and lets the actual
``importlib.metadata`` machinery find them.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from traceh.api.tools import EffectKind, ToolExecutionContext, ToolOutput
from traceh.plugins import PluginContext, PluginManifest, PromptSection
from traceh.plugins.discovery import ENTRY_POINT_GROUP
from traceh.version import __version__

COMPATIBLE_REQUIREMENT = f"traceharness-py>={__version__},<1.0"


class FakeDistribution:
    """Minimal stand-in for ``importlib.metadata.Distribution``."""

    def __init__(
        self,
        name: str | None = "example-dist",
        version: str | None = "1.0.0",
        requires: tuple[str, ...] | None = (COMPATIBLE_REQUIREMENT,),
    ) -> None:
        self.metadata = {"Name": name} if name is not None else {}
        self.version = version
        self.requires = list(requires) if requires is not None else None
        self.entry_points = ()


class LoadCounter:
    """Records which plugin modules discovery caused to be imported."""

    def __init__(self) -> None:
        self.loaded: list[str] = []


@dataclass
class FakeEntryPoint:
    """Stand-in for an entry point, tracking whether ``load()`` was called."""

    name: str
    value: str = "fake_module:Plugin"
    group: str = ENTRY_POINT_GROUP
    dist: Any = field(default_factory=FakeDistribution)
    target: Any = None
    load_error: BaseException | None = None
    counter: LoadCounter | None = None

    def load(self) -> Any:
        if self.counter is not None:
            self.counter.loaded.append(self.name)
        if self.load_error is not None:
            raise self.load_error
        return self.target


def provider_for(*points: FakeEntryPoint):
    """Build an ``entry_points_provider`` returning exactly ``points``."""

    def provider(**kwargs: Any):
        group = kwargs.get("group")
        if group is None:
            return tuple(points)
        return tuple(point for point in points if point.group == group)

    return provider


class RecordingTool:
    """A read-only tool with a configurable name, safe to register anywhere."""

    description = "Test tool contributed by a plugin."
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    effect_kind = EffectKind.PURE_READ

    def __init__(self, name: str, content: str = "ok") -> None:
        self.name = name
        self._content = content
        self.calls = 0

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolOutput:
        del arguments, context
        self.calls += 1
        return ToolOutput(content=self._content, data={"tool": self.name})


class ScriptedPlugin:
    """A configurable plugin object driven entirely by constructor arguments.

    Everything a test wants to vary - what it registers, whether setup fails or
    blocks, what health_check does - is a parameter, so individual tests stay
    about the manager's behaviour rather than about defining a new plugin class.
    """

    def __init__(
        self,
        manifest: PluginManifest,
        *,
        tools: tuple[RecordingTool, ...] = (),
        prompts: tuple[PromptSection, ...] = (),
        services: tuple[tuple[Any, Any], ...] = (),
        setup_error: BaseException | None = None,
        setup_gate: asyncio.Event | None = None,
        setup_entered: asyncio.Event | None = None,
        health_result: Any = None,
        health_error: BaseException | None = None,
        health_gate: asyncio.Event | None = None,
        health_entered: asyncio.Event | None = None,
        has_health_check: bool = False,
        cleanup_error: BaseException | None = None,
        cleanup_gate: asyncio.Event | None = None,
        cleanup_entered: asyncio.Event | None = None,
        spawn_forever: bool = False,
        record: list[str] | None = None,
    ) -> None:
        self.manifest = manifest
        self._tools = tools
        self._prompts = prompts
        self._services = services
        self._setup_error = setup_error
        self._setup_gate = setup_gate
        self._setup_entered = setup_entered
        self._health_result = health_result
        self._health_error = health_error
        self._health_gate = health_gate
        self._health_entered = health_entered
        self._cleanup_error = cleanup_error
        self._cleanup_gate = cleanup_gate
        self._cleanup_entered = cleanup_entered
        self._spawn_forever = spawn_forever
        self.record = record if record is not None else []
        self.setup_calls = 0
        self.health_calls = 0
        self.cleanup_calls = 0
        self.owned_task: asyncio.Task[Any] | None = None
        self.owned_task_started = asyncio.Event()
        self.owned_task_cancelled = False
        if has_health_check or health_error is not None or health_result is not None:
            self.health_check = self._health_check  # type: ignore[method-assign]

    async def _forever(self) -> None:
        self.owned_task_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.owned_task_cancelled = True
            raise

    async def setup(self, context: PluginContext, config: dict[str, Any]) -> None:
        del config
        self.setup_calls += 1
        self.record.append(f"setup:{self.manifest.plugin_id}")
        for tool in self._tools:
            context.register_tool(tool)
        for section in self._prompts:
            context.register_prompt(section)
        for key, value in self._services:
            await context.provide(key, value)
        if self._spawn_forever:
            self.owned_task = context.spawn_owned(self._forever(), name="forever")

        async def cleanup() -> None:
            self.cleanup_calls += 1
            self.record.append(f"cleanup:{self.manifest.plugin_id}")
            if self._cleanup_entered is not None:
                self._cleanup_entered.set()
            if self._cleanup_gate is not None:
                await self._cleanup_gate.wait()
            if self._cleanup_error is not None:
                raise self._cleanup_error

        context.add_cleanup(cleanup)

        if self._setup_entered is not None:
            self._setup_entered.set()
        if self._setup_gate is not None:
            await self._setup_gate.wait()
        if self._setup_error is not None:
            raise self._setup_error

    async def _health_check(self, context: PluginContext) -> Any:
        del context
        self.health_calls += 1
        self.record.append(f"health:{self.manifest.plugin_id}")
        if self._health_entered is not None:
            self._health_entered.set()
        if self._health_gate is not None:
            await self._health_gate.wait()
        if self._health_error is not None:
            raise self._health_error
        return self._health_result


def manifest(plugin_id: str, version: str = "1.0.0", **overrides: Any) -> PluginManifest:
    return PluginManifest(plugin_id=plugin_id, version=version, **overrides)


def entry_point_for(
    plugin: Any,
    plugin_id: str | None = None,
    *,
    counter: LoadCounter | None = None,
    dist: Any = None,
) -> FakeEntryPoint:
    resolved = plugin_id or plugin.manifest.plugin_id
    return FakeEntryPoint(
        name=resolved,
        value=f"{resolved}:Plugin",
        dist=dist if dist is not None else FakeDistribution(),
        target=plugin,
        counter=counter,
    )
