"""Public runtime facade and default composition factories."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from packaging.version import InvalidVersion, Version

from traceh.api.json_types import JsonValue
from traceh.api.llm import LlmProvider, ModelResponse
from traceh.api.plugins import CORE_PLUGIN_IDENTITY, PluginIdentity
from traceh.api.tools import Tool
from traceh.concurrency import await_worker_convergence
from traceh.kernel.hooks import HookDispatcher
from traceh.kernel.registry import ServiceRegistry
from traceh.llm.registry import LlmRegistry
from traceh.llm.runtime import LlmRuntime
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_loop import AgentLoop, TurnResult
from traceh.runtime.composition_runtime import GenerationCompositionRuntime
from traceh.runtime.continuation import ContinuationRuntime
from traceh.runtime.prompt import PromptAssembler, default_coding_prompt
from traceh.runtime.request_builder import RequestBuilder
from traceh.runtime.verification import CommandVerifier, CompletionVerifier
from traceh.session.compaction import CompactionService
from traceh.session.event_feed import EventFeed, PublishingEventStore, SessionEventFeed
from traceh.session.event_store import EventStore
from traceh.session.invariants import CoreInvariantChecker
from traceh.session.jsonl import JsonlEventStore
from traceh.session.recovery import RecoveryReport, RecoveryService
from traceh.session.service import SessionService
from traceh.session.surface import SurfaceProjector
from traceh.tools.builtins import (
    ApplyPatchTool,
    ListFilesTool,
    ReadFileTool,
    SearchTextTool,
    ShellTool,
)
from traceh.tools.middleware import ToolMiddleware
from traceh.tools.policy import AllowByDefaultPolicy, DangerousShellPolicy, ToolPolicy
from traceh.tools.registry import ToolRegistry
from traceh.tools.runtime import ToolRuntime

if TYPE_CHECKING:
    from traceh.plugins.discovery import PluginDiscovery
    from traceh.plugins.manager import PluginManager

#: Session metadata key holding the external plugin identities a session was
#: created under. Reserved: callers may not set it themselves.
_PLUGIN_METADATA_KEY = "traceh_plugins"

#: Sentinel for "the key is genuinely absent", which is what a pre-v0.4 session
#: looks like. It exists because `dict.get()` returns `None` both for an absent
#: key and for a key explicitly recorded as `null` - two different facts that
#: must not share an answer. Absent means "written before this runtime recorded
#: plugins"; an explicit `null` is corrupt data.
_PLUGIN_METADATA_MISSING = object()


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    data_dir: Path = Path(".traceh")
    provider: str = "scripted"
    model: str = "scripted-model"
    max_steps: int = 20
    tool_timeout_seconds: float = 60.0
    max_tool_output_chars: int = 24_000
    temperature: float | None = None
    max_output_tokens: int | None = None
    verification_command: str | None = None
    verification_timeout_seconds: float = 60.0
    max_verification_retries: int = 1

    def __post_init__(self) -> None:
        if self.max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        if self.tool_timeout_seconds <= 0:
            raise ValueError("tool_timeout_seconds must be positive")
        if self.max_tool_output_chars < 1:
            raise ValueError("max_tool_output_chars must be positive")
        if self.verification_timeout_seconds <= 0:
            raise ValueError("verification_timeout_seconds must be positive")
        if self.max_verification_retries < 0:
            raise ValueError("max_verification_retries cannot be negative")


class AgentAlreadyRunningError(RuntimeError):
    pass


class SessionPluginMismatchError(RuntimeError):
    """A persisted session was created under a different plugin composition.

    Continuing anyway would let a turn run against tools and prompt sections the
    earlier turns never had - or, worse, silently drop a plugin whose tool results
    are already part of the session's history.
    """


class AgentRuntime:
    def __init__(
        self,
        *,
        config: RuntimeConfig,
        sessions: SessionService,
        loop: AgentLoop,
        recovery: RecoveryService,
        surface: SurfaceProjector,
        invariants: CoreInvariantChecker,
        hooks: HookDispatcher,
        compaction: CompactionService,
        events: EventFeed,
        plugins: tuple[PluginIdentity, ...] = (CORE_PLUGIN_IDENTITY,),
        plugin_manager: PluginManager | None = None,
        services: ServiceRegistry | None = None,
    ) -> None:
        self.config = config
        self.sessions = sessions
        self.loop = loop
        self.recovery = recovery
        self.surface = surface
        self.invariants = invariants
        self.hooks = hooks
        self.compaction = compaction
        #: Read-only subscription surface for events this runtime's store has
        #: accepted (see `session/event_feed.py`). A user interface may subscribe
        #: to watch a turn as it happens; it adds no persisted fact, no history
        #: and no state, so nothing in the runtime reads from it.
        #:
        #: Required, and required to be *connected*: it must be the same feed the
        #: `PublishingEventStore` behind ``sessions`` publishes to. Defaulting it
        #: would hand callers a subscribable object that stays silent forever -
        #: an interface that exists while the capability does not. Custom
        #: assemblies must therefore pair the two explicitly, as
        #: `build_default_runtime()` does.
        self.events = events
        #: Full composition identity for this runtime: the core plus whichever
        #: external plugins were activated. Stamped into every Composition
        #: Snapshot by the generation-backed CompositionRuntime.
        self.plugins = plugins
        self.services = services or ServiceRegistry()
        self._plugin_manager = plugin_manager
        self._active: dict[str, asyncio.Task[TurnResult]] = {}
        self._lock = asyncio.Lock()
        self._disposed = False
        self._dispose_task: asyncio.Task[None] | None = None

    @property
    def external_plugin_identities(self) -> tuple[PluginIdentity, ...]:
        return tuple(
            identity
            for identity in self.plugins
            if identity.plugin_id != CORE_PLUGIN_IDENTITY.plugin_id
        )

    @property
    def enabled_plugin_ids(self) -> tuple[str, ...]:
        return tuple(identity.plugin_id for identity in self.external_plugin_identities)

    def _plugin_metadata(self) -> list[dict[str, str]]:
        return [identity.to_dict() for identity in self.external_plugin_identities]

    async def create_session(
        self,
        workspace: Path,
        *,
        metadata: dict[str, JsonValue] | None = None,
    ) -> str:
        workspace = workspace.resolve()
        if not workspace.exists() or not workspace.is_dir():
            raise NotADirectoryError(workspace)
        actual_metadata = dict(metadata or {})
        # Rejected on *presence*, not on value. Comparing against the expected
        # list would let a caller supply `[]`, `None`, or a copy that happens to
        # match today - and a caller who can write the key at all is asserting
        # something only the runtime is entitled to assert. Every other metadata
        # key the caller provides is stored unchanged.
        if _PLUGIN_METADATA_KEY in actual_metadata:
            raise ValueError(f"{_PLUGIN_METADATA_KEY} is reserved by TraceHarness")
        expected: JsonValue = self._plugin_metadata()
        actual_metadata[_PLUGIN_METADATA_KEY] = expected
        return await self.sessions.create_session(workspace, metadata=actual_metadata)

    @staticmethod
    def _identities_from_metadata(value: object) -> tuple[PluginIdentity, ...]:
        """Parse persisted plugin identities, refusing anything malformed.

        Only :data:`_PLUGIN_METADATA_MISSING` - the sentinel meaning the key was
        genuinely absent - stands for a pre-v0.4 plugin-free session. An explicit
        ``None`` is a recorded value, and a recorded ``null`` is not a claim this
        runtime ever wrote, so it is corrupt data rather than "no plugins".
        """

        # Imported here rather than at module scope: `traceh.plugins` pulls in the
        # manager, which imports `traceh.runtime.prompt` and therefore re-enters
        # `traceh.runtime.__init__` while this module is still executing.
        from traceh.plugins.selection import is_plugin_id

        if value is _PLUGIN_METADATA_MISSING:
            return ()
        if not isinstance(value, list):
            raise SessionPluginMismatchError("session plugin metadata is malformed")
        identities: list[PluginIdentity] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, dict):
                raise SessionPluginMismatchError("session plugin metadata is malformed")
            plugin_id = item.get("plugin_id")
            version = item.get("version")
            if not isinstance(plugin_id, str) or not isinstance(version, str):
                raise SessionPluginMismatchError("session plugin metadata is malformed")
            if not is_plugin_id(plugin_id):
                raise SessionPluginMismatchError("session plugin metadata is malformed")
            try:
                # Parsed to reject anything that is not a version at all; the
                # original text is kept so the mismatch message reports what the
                # session actually recorded. Equivalence is decided by
                # `_comparable()`, not by this string.
                Version(version)
            except InvalidVersion:
                raise SessionPluginMismatchError("session plugin metadata is malformed") from None
            if plugin_id in seen:
                raise SessionPluginMismatchError("session plugin metadata contains duplicate ids")
            seen.add(plugin_id)
            identities.append(PluginIdentity(plugin_id, version))
        return tuple(identities)

    @staticmethod
    def _comparable(
        identities: tuple[PluginIdentity, ...],
    ) -> tuple[tuple[str, Version], ...]:
        """Key used to decide whether two plugin compositions are the same.

        Versions compare as :class:`~packaging.version.Version` objects, not as
        strings. ``str(Version("1.0"))`` is ``"1.0"`` and ``str(Version("1.0.0"))``
        is ``"1.0.0"``, so string comparison - even after parsing - rejects two
        PEP 440 equivalent versions as a composition change. ``Version("1.0") ==
        Version("1.0.0")`` is True, while ``Version("1.0") == Version("1.0.1")``
        is correctly False.
        """

        return tuple((item.plugin_id, Version(item.version)) for item in identities)

    async def verify_session_plugins(self, session_id: str) -> None:
        """Refuse to continue a session whose plugin composition no longer matches."""

        events = await self.sessions.read_session(session_id)
        if not events or events[0].type != "session/created":
            # Not our call to diagnose: let the session layer raise its own
            # "session not found" rather than inventing a plugin verdict.
            await self.sessions.ensure_session(session_id)
            return
        metadata = events[0].data.get("metadata", {})
        if not isinstance(metadata, dict):
            raise SessionPluginMismatchError("session metadata is malformed")
        # `get()` with a default would collapse "absent" and "recorded as null"
        # into the same answer; the sentinel keeps them distinguishable.
        persisted = self._identities_from_metadata(
            metadata.get(_PLUGIN_METADATA_KEY, _PLUGIN_METADATA_MISSING)
        )
        active = self.external_plugin_identities
        if self._comparable(persisted) != self._comparable(active):
            required = (
                ", ".join(f"{item.plugin_id}=={item.version}" for item in persisted) or "none"
            )
            current = ", ".join(f"{item.plugin_id}=={item.version}" for item in active) or "none"
            raise SessionPluginMismatchError(
                "session plugin composition does not match the active runtime; "
                f"session requires [{required}], current runtime has [{current}]"
            )

    async def run(self, workspace: Path, task: str) -> TurnResult:
        session_id = await self.create_session(workspace)
        return await self.run_existing(session_id, task)

    async def run_existing(self, session_id: str, task: str) -> TurnResult:
        if self._disposed:
            raise RuntimeError("runtime is disposed")
        await self.verify_session_plugins(session_id)
        async with self._lock:
            existing = self._active.get(session_id)
            if existing is not None and not existing.done():
                raise AgentAlreadyRunningError(session_id)
            task_handle = asyncio.create_task(
                self.loop.run_turn(session_id, task),
                name=f"traceh-turn-{session_id}",
            )
            self._active[session_id] = task_handle
        try:
            return await task_handle
        finally:
            async with self._lock:
                if self._active.get(session_id) is task_handle:
                    self._active.pop(session_id, None)

    async def resume(
        self,
        session_id: str,
        *,
        instruction: str = (
            "Continue the previous task. Re-inspect the workspace and the recovered tool results "
            "before repeating any write or process side effect."
        ),
    ) -> tuple[RecoveryReport, TurnResult]:
        # Checked before recovery: recovery appends events, and appending to a
        # session under a composition it was not created with is the thing this
        # check exists to prevent.
        await self.verify_session_plugins(session_id)
        report = await self.recovery.recover(session_id)
        result = await self.run_existing(session_id, instruction)
        return report, result

    async def cancel(self, session_id: str, *, reason: str = "cancel requested") -> bool:
        async with self._lock:
            task = self._active.get(session_id)
        if task is None or task.done():
            return False
        await self.sessions.append_session(
            session_id,
            "runtime/cancel-requested",
            {"reason": reason},
        )
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return True

    async def _shutdown(self) -> None:
        """The whole shutdown, as one unit of work owned by a single task."""

        async with self._lock:
            tasks = tuple(self._active.values())
            self._active.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Turns converge first, then retired/current generations converge, then
        # the PluginManager unloads the startup activation it uniquely owns.
        # Unloading first would pull a tool or service out from under a turn or
        # a still-live generation.  The default generation has no cleanup
        # callback: its plugin resources remain owned by PluginManager, so this
        # does not create a second cleanup owner.  If a generation cleanup
        # fails, still give PluginManager its cleanup opportunity before
        # reporting the shutdown failure.
        shutdown_errors: list[BaseException] = []
        composition_dispose = getattr(self.loop.compositions, "dispose", None)
        if composition_dispose is not None:
            try:
                await composition_dispose()
            except BaseException as error:
                shutdown_errors.append(error)
        if self._plugin_manager is not None:
            try:
                await self._plugin_manager.dispose()
            except BaseException as error:
                shutdown_errors.append(error)
            finally:
                finalize_external_cleanup = getattr(
                    self.loop.compositions, "finalize_external_cleanup", None
                )
                if finalize_external_cleanup is not None:
                    finalize_external_cleanup()
        if len(shutdown_errors) == 1:
            raise shutdown_errors[0]
        if shutdown_errors:
            raise ExceptionGroup("runtime shutdown failed", shutdown_errors)

    async def dispose(self) -> None:
        """Shut the runtime down exactly once, converging on repeat cancellation.

        The entire shutdown lives in one internal task rather than in this
        coroutine's own frame. That placement is the fix for a real defect: when
        the body ran inline, a caller cancelled while active turns were still
        converging escaped *before* reaching ``PluginManager.dispose()``, yet the
        disposed flag was already set - so every later ``dispose()`` returned
        immediately and the plugins were never unloaded at all.

        Now the work belongs to the task, not to whoever is waiting on it:

        * the caller waits through ``shield``, so its cancellation never touches
          the shutdown itself;
        * repeat cancellation is absorbed by ``await_worker_convergence`` - a
          second and third Ctrl+C cannot release the caller early;
        * the original ``CancelledError`` is re-raised only after shutdown has
          fully converged;
        * a later ``dispose()`` awaits the *same* task, so it reuses the one real
          outcome. If shutdown failed, that failure is raised again rather than
          being silently reported as success.
        """

        # Set before the task exists: no new turn may start from this point on,
        # even while shutdown is still running.
        self._disposed = True
        if self._dispose_task is None:
            self._dispose_task = asyncio.create_task(
                self._shutdown(),
                name="traceh-runtime-dispose",
            )
        task = self._dispose_task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await await_worker_convergence(task)
            raise

    async def check_invariants(self, session_id: str):
        return self.invariants.check(
            await self.sessions.read_session(session_id),
            await self.sessions.read_effects(session_id),
        )



@dataclass(slots=True)
class _PreparedRuntime:
    """Everything assembled before plugins may contribute, and before the loop exists.

    Splitting assembly in two is what lets plugin activation happen *between* the
    registries being created and the composition being frozen around them, without
    duplicating the assembly itself.
    """

    config: RuntimeConfig
    data_dir: Path
    sessions: SessionService
    surface: SurfaceProjector
    event_feed: SessionEventFeed
    llms: LlmRegistry
    tool_registry: ToolRegistry
    prompt: PromptAssembler
    policies: tuple[ToolPolicy, ...]
    tool_middlewares: tuple[ToolMiddleware, ...]
    verifier: CompletionVerifier | None
    continuation: ContinuationRuntime | None


def _prepare_default_runtime(
    config: RuntimeConfig | None = None,
    *,
    provider: LlmProvider | None = None,
    prompt: PromptAssembler | None = None,
    policies: tuple[ToolPolicy, ...] | None = None,
    tool_middlewares: tuple[ToolMiddleware, ...] = (),
    verifier: CompletionVerifier | None = None,
    continuation: ContinuationRuntime | None = None,
    event_store: EventStore | None = None,
    additional_tools: tuple[Tool, ...] = (),
    include_default_tools: bool = True,
) -> _PreparedRuntime:
    config = config or RuntimeConfig()
    data_dir = config.data_dir.resolve()
    actual_event_store = event_store or JsonlEventStore(data_dir / "events")
    # Wrapping is unconditional so that every writer - the loop, the tool
    # runtime, recovery, compaction - is observable through one boundary rather
    # than through whichever of them remembered to announce itself. With no
    # subscribers the decorator costs one uncontended lock per append.
    #
    # This one object is both the store's publication target and the runtime's
    # subscription surface. Handing `AgentRuntime` any other instance would give
    # callers a feed that never receives anything.
    event_feed = SessionEventFeed()
    sessions = SessionService(PublishingEventStore(actual_event_store, event_feed))
    surface = SurfaceProjector()

    llms = LlmRegistry()
    # The built-in placeholder answers every turn; an explicitly supplied
    # provider keeps whatever exhaustion behaviour its caller configured.
    actual_provider = provider or ScriptedLlmProvider(
        (ModelResponse(content="TraceHarness scripted runtime is ready."),),
        repeat_last=True,
    )
    llms.register(actual_provider)
    if actual_provider.name != config.provider:
        raise ValueError(
            f"configured provider {config.provider!r} does not match provider object "
            f"{actual_provider.name!r}"
        )

    tool_registry = ToolRegistry()
    default_tools: tuple[Tool, ...] = (
        ListFilesTool(),
        ReadFileTool(),
        SearchTextTool(),
        ApplyPatchTool(),
        ShellTool(),
    )
    selected_tools = (default_tools if include_default_tools else ()) + additional_tools
    for tool in selected_tools:
        tool_registry.register(tool)

    effective_policies = policies or (DangerousShellPolicy(), AllowByDefaultPolicy())
    effective_verifier = verifier
    if effective_verifier is None and config.verification_command:
        effective_verifier = CommandVerifier(
            config.verification_command,
            config.verification_timeout_seconds,
        )
    return _PreparedRuntime(
        config=config,
        data_dir=data_dir,
        sessions=sessions,
        surface=surface,
        event_feed=event_feed,
        llms=llms,
        tool_registry=tool_registry,
        prompt=prompt or default_coding_prompt(),
        policies=effective_policies,
        tool_middlewares=tool_middlewares,
        verifier=effective_verifier,
        continuation=continuation,
    )


def _finish_default_runtime(
    prepared: _PreparedRuntime,
    *,
    plugins: tuple[PluginIdentity, ...] = (CORE_PLUGIN_IDENTITY,),
    plugin_manager: PluginManager | None = None,
    services: ServiceRegistry | None = None,
) -> AgentRuntime:
    config = prepared.config
    tool_runtime = ToolRuntime(
        prepared.tool_registry,
        prepared.sessions,
        policies=prepared.policies,
        middlewares=prepared.tool_middlewares,
        timeout_seconds=config.tool_timeout_seconds,
        max_output_chars=config.max_tool_output_chars,
    )
    request_builder = RequestBuilder(prepared.sessions, prepared.surface)
    hooks = HookDispatcher()
    composition_runtime = GenerationCompositionRuntime(
        llms=prepared.llms,
        tools=tool_runtime,
        prompt=prepared.prompt,
        provider=config.provider,
        model=config.model,
        temperature=config.temperature,
        max_output_tokens=config.max_output_tokens,
        plugins=plugins,
        defer_external_cleanup=plugin_manager is not None,
    )
    loop = AgentLoop(
        sessions=prepared.sessions,
        compositions=composition_runtime,
        request_builder=request_builder,
        llm_runtime=LlmRuntime(),
        data_dir=prepared.data_dir,
        max_steps=config.max_steps,
        continuation=prepared.continuation,
        verifier=prepared.verifier,
        max_verification_retries=config.max_verification_retries,
        hooks=hooks,
    )
    return AgentRuntime(
        config=config,
        sessions=prepared.sessions,
        loop=loop,
        recovery=RecoveryService(prepared.sessions),
        surface=prepared.surface,
        invariants=CoreInvariantChecker(),
        hooks=hooks,
        compaction=CompactionService(prepared.sessions),
        events=prepared.event_feed,
        plugins=plugins,
        plugin_manager=plugin_manager,
        services=services,
    )


def build_default_runtime(
    config: RuntimeConfig | None = None,
    *,
    provider: LlmProvider | None = None,
    prompt: PromptAssembler | None = None,
    policies: tuple[ToolPolicy, ...] | None = None,
    tool_middlewares: tuple[ToolMiddleware, ...] = (),
    verifier: CompletionVerifier | None = None,
    continuation: ContinuationRuntime | None = None,
    event_store: EventStore | None = None,
    additional_tools: tuple[Tool, ...] = (),
    include_default_tools: bool = True,
) -> AgentRuntime:
    """Build the no-plugin runtime synchronously through the Generation path."""

    prepared = _prepare_default_runtime(
        config,
        provider=provider,
        prompt=prompt,
        policies=policies,
        tool_middlewares=tool_middlewares,
        verifier=verifier,
        continuation=continuation,
        event_store=event_store,
        additional_tools=additional_tools,
        include_default_tools=include_default_tools,
    )
    return _finish_default_runtime(prepared)


async def build_default_runtime_async(
    config: RuntimeConfig | None = None,
    *,
    provider: LlmProvider | None = None,
    prompt: PromptAssembler | None = None,
    policies: tuple[ToolPolicy, ...] | None = None,
    tool_middlewares: tuple[ToolMiddleware, ...] = (),
    verifier: CompletionVerifier | None = None,
    continuation: ContinuationRuntime | None = None,
    event_store: EventStore | None = None,
    additional_tools: tuple[Tool, ...] = (),
    include_default_tools: bool = True,
    enabled_plugins: Sequence[str] = (),
    plugin_configs: Mapping[str, Mapping[str, object]] | None = None,
    plugin_discovery: PluginDiscovery | None = None,
) -> AgentRuntime:
    """Build a runtime after transactional startup-time plugin activation.

    With ``enabled_plugins`` empty this is exactly ``build_default_runtime``:
    no discovery runs, no plugin module is imported, and the resulting runtime
    still uses the same single initial Generation/Lease path.
    """

    prepared = _prepare_default_runtime(
        config,
        provider=provider,
        prompt=prompt,
        policies=policies,
        tool_middlewares=tool_middlewares,
        verifier=verifier,
        continuation=continuation,
        event_store=event_store,
        additional_tools=additional_tools,
        include_default_tools=include_default_tools,
    )
    if not enabled_plugins:
        return _finish_default_runtime(prepared)

    # Imported here so the stable runtime surface does not depend on discovery
    # internals, and so `AgentLoop` never acquires a path to `PluginManager`.
    from traceh.plugins.manager import PluginManager

    services = ServiceRegistry()
    manager = PluginManager(
        tools=prepared.tool_registry,
        prompt=prepared.prompt,
        services=services,
        discovery=plugin_discovery,
        plugin_configs=plugin_configs,
    )
    await manager.activate(tuple(enabled_plugins))
    try:
        return _finish_default_runtime(
            prepared,
            plugins=manager.identities,
            plugin_manager=manager,
            services=services,
        )
    except BaseException:
        # Activation succeeded but assembly did not: the plugins are live and
        # nobody owns them, so unload before the exception leaves.
        await manager.dispose()
        raise
