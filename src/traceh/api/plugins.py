"""Forward-compatible plugin metadata and setup context protocols.

v0.3 includes the protocol but intentionally defers entry-point discovery and a public
PluginManager to v0.4.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from traceh.api.services import Registration, ServiceKey

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class PluginIdentity:
    plugin_id: str
    version: str

    def to_dict(self) -> dict[str, str]:
        return {"plugin_id": self.plugin_id, "version": self.version}


@dataclass(frozen=True, slots=True)
class PluginDependency:
    plugin_id: str
    version_spec: str


@dataclass(frozen=True, slots=True)
class PluginManifest:
    plugin_id: str
    version: str
    requires_traceh: str = ">=0.3,<1.0"
    requires_plugins: tuple[PluginDependency, ...] = ()
    optional_plugins: tuple[PluginDependency, ...] = ()
    allowed_scopes: tuple[str, ...] = ("application", "workspace", "preset", "agent")
    trust_mode: str = "trusted"
    provides: tuple[str, ...] = ()


class PluginContext(Protocol):
    def require(self, key: ServiceKey[T]) -> T:
        ...

    async def provide(
        self,
        key: ServiceKey[T],
        service: T,
        *,
        replace: bool = False,
    ) -> Registration:
        ...

    def add_cleanup(self, callback: Callable[[], Awaitable[None]]) -> Registration:
        ...

    def spawn_owned(
        self,
        coroutine: Coroutine[Any, Any, Any],
        *,
        name: str,
    ) -> asyncio.Task[Any]:
        ...


class Plugin(Protocol):
    manifest: PluginManifest

    async def setup(self, context: PluginContext, config: dict[str, object]) -> None:
        ...
