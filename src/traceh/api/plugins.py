"""Stable plugin metadata and setup protocols.

v0.4 activates plugins for real: entry points in the ``traceh.plugins`` group are
discovered from installed distributions, explicitly enabled plugins are imported
and set up inside a transaction, and their contributions join the existing tool,
prompt and service mainlines. See :mod:`traceh.plugins.manager` for the
transaction and :doc:`docs/plugins` for the author-facing contract.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from traceh.api.llm import LlmProvider
from traceh.api.prompts import PromptSection
from traceh.api.services import Registration, ServiceKey
from traceh.api.tools import Tool
from traceh.version import CORE_PLUGIN_ID, DEFAULT_REQUIRES_TRACEH, __version__

if TYPE_CHECKING:
    from traceh.runtime.verification import CompletionVerifier
    from traceh.tools.middleware import ToolMiddleware
    from traceh.tools.policy import ToolPolicy

T = TypeVar("T")
_MISSING = object()


@dataclass(frozen=True, slots=True)
class PluginIdentity:
    """The ``(plugin_id, version)`` pair persisted in every Composition Snapshot."""

    plugin_id: str
    version: str

    def to_dict(self) -> dict[str, str]:
        return {"plugin_id": self.plugin_id, "version": self.version}


CORE_PLUGIN_IDENTITY = PluginIdentity(CORE_PLUGIN_ID, __version__)
"""Identity of the harness itself.

Both the plain runtime and the plugin-activated runtime stamp *this* object into
the Composition Snapshot, so two runtimes of the same build always agree on the
core version. See :mod:`traceh.version`.
"""


@dataclass(frozen=True, slots=True)
class PluginDependency:
    """A dependency on another plugin id, constrained by a PEP 440 specifier."""

    plugin_id: str
    version_spec: str


@dataclass(frozen=True, slots=True)
class PluginManifest:
    """What a plugin declares about itself before any of its code runs setup.

    ``allowed_scopes`` defaults to application scope only, and ``trust_mode``
    defaults to ``trusted``: v0.4 supports exactly application-scoped, trusted,
    in-process plugins. ``isolated`` is a declarable value that activation
    explicitly rejects, rather than a value that is silently treated as trusted.
    """

    plugin_id: str
    version: str
    requires_traceh: str = DEFAULT_REQUIRES_TRACEH
    requires_plugins: tuple[PluginDependency, ...] = ()
    optional_plugins: tuple[PluginDependency, ...] = ()
    allowed_scopes: tuple[str, ...] = ("application",)
    trust_mode: str = "trusted"
    provides: tuple[str, ...] = ()


class PluginContext(Protocol):
    """The controlled setup surface handed to a trusted in-process plugin.

    The context deliberately exposes no ``AgentRuntime``, ``AgentLoop``,
    ``EventStore``, ``ToolRegistry`` or ``PromptAssembler`` object. A plugin can
    contribute Generation-scoped providers, tools, prompt sections, policies,
    middleware and named verifiers, plus application services, configuration,
    cleanup and owned background tasks. Every contribution is reversible and
    owned by the plugin's Activation.

    ``EventStore`` is deliberately absent. It owns the durable Session fact
    source for the whole Runtime and cannot safely follow Step-generation hot
    replacement. A future process-lifetime plugin boundary must own that
    capability before it can enter this surface.
    """

    def register_tool(self, tool: Tool) -> Registration:
        ...

    def register_prompt(self, section: PromptSection) -> Registration:
        ...

    def register_provider(self, provider: LlmProvider) -> Registration:
        ...

    def register_policy(self, policy: ToolPolicy) -> Registration:
        ...

    def register_middleware(self, middleware: ToolMiddleware) -> Registration:
        ...

    def register_verifier(
        self,
        name: str,
        verifier: CompletionVerifier,
    ) -> Registration:
        ...

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

    def get_config(self, key: str, default: object = _MISSING) -> object:
        ...

    def config_snapshot(self) -> Mapping[str, object]:
        ...


class Plugin(Protocol):
    """What an entry point must resolve to.

    ``health_check`` is optional. When present it may be sync or async, take the
    context or take nothing, and returning ``False`` fails activation just as
    raising does.
    """

    manifest: PluginManifest

    async def setup(self, context: PluginContext, config: dict[str, object]) -> None:
        ...


__all__ = [
    "CORE_PLUGIN_IDENTITY",
    "Plugin",
    "PluginContext",
    "PluginDependency",
    "PluginIdentity",
    "PluginManifest",
]
