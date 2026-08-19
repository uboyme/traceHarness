"""Public runtime facade and default composition factories."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from traceh.api.json_types import JsonValue
from traceh.api.llm import LlmProvider, ModelResponse
from traceh.api.plugins import CORE_PLUGIN_IDENTITY, PluginIdentity
from traceh.api.tools import Tool
from traceh.concurrency import await_worker_convergence
from traceh.kernel.hooks import HookDispatcher
from traceh.kernel.registry import ServiceRegistry, ServiceView
from traceh.kernel.scope import Scope, ScopedServiceBinding
from traceh.llm.registry import LlmRegistry
from traceh.llm.runtime import LlmRuntime
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_loop import AgentLoop, TurnResult
from traceh.runtime.composition_runtime import GenerationCompositionRuntime
from traceh.runtime.continuation import ContinuationRuntime
from traceh.runtime.plugin_composition import (
    AgentAlreadyRunningError,
    PluginCompositionCoordinator,
    PluginCompositionReplacement,
    SessionPluginMigrationError,
    SessionPluginMismatchError,
)
from traceh.runtime.prompt import PromptAssembler, default_coding_prompt
from traceh.runtime.request_builder import RequestBuilder
from traceh.runtime.verification import CommandVerifier, CompletionVerifier
from traceh.session.compaction import CompactionService
from traceh.session.event_feed import EventFeed, PublishingEventStore, SessionEventFeed
from traceh.session.event_store import EventStore
from traceh.session.invariants import CoreInvariantChecker
from traceh.session.jsonl import JsonlEventStore
from traceh.session.plugin_identity import PLUGIN_METADATA_KEY
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
    from traceh.plugins.manager import (
        PluginActivationSet,
        PluginGenerationBuilder,
        PluginManager,
    )

__all__ = [
    "AgentAlreadyRunningError",
    "AgentRuntime",
    "PluginCompositionReplacement",
    "RuntimeConfig",
    "SessionPluginMigrationError",
    "SessionPluginMismatchError",
    "build_default_runtime",
    "build_default_runtime_async",
]

#: Session metadata key holding the external plugin identities a session was
#: created under. Reserved: callers may not set it themselves.
_PLUGIN_METADATA_KEY = PLUGIN_METADATA_KEY

#: Sentinel for "the key is genuinely absent", which is what a pre-v0.4 session
#: looks like. It exists because `dict.get()` returns `None` both for an absent
#: key and for a key explicitly recorded as `null` - two different facts that
#: must not share an answer. Absent means "written before this runtime recorded
#: plugins"; an explicit `null` is corrupt data.
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
        plugin_builder: object | None = None,
        assembly_llms: LlmRegistry | None = None,
        assembly_policies: tuple[ToolPolicy, ...] = (),
        assembly_middlewares: tuple[ToolMiddleware, ...] = (),
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
        #: The initial tuple is only a fallback for custom legacy assemblies.
        #: The normal source of current plugin identity is the current
        #: Composition Generation, so AgentRuntime does not keep a second
        #: mutable plugin-composition fact.
        self._initial_plugins = tuple(plugins)
        self._services_base = services or ServiceRegistry()
        self._plugin_manager = plugin_manager
        self._active: dict[str, asyncio.Task[TurnResult]] = {}
        self._lock = asyncio.Lock()
        self._disposed = False
        self._dispose_started = asyncio.Event()
        self._dispose_task: asyncio.Task[None] | None = None
        self._plugin_compositions = PluginCompositionCoordinator(
            sessions=sessions,
            compositions=loop.compositions,
            plugin_builder=plugin_builder,
            llms=assembly_llms,
            policies=tuple(assembly_policies),
            middlewares=tuple(assembly_middlewares),
            tool_timeout_seconds=config.tool_timeout_seconds,
            max_tool_output_chars=config.max_tool_output_chars,
            current_external_plugins=lambda: self.external_plugin_identities,
            runtime_is_disposed=lambda: self._disposed,
            runtime_has_active_turn=self._has_active_turn,
        )

    @property
    def plugins(self) -> tuple[PluginIdentity, ...]:
        composition_plugins = getattr(self.loop.compositions, "plugins", None)
        if composition_plugins is None:
            return self._initial_plugins
        return tuple(composition_plugins)

    @property
    def services(self) -> ServiceView:
        try:
            generation = self.loop.compositions.current_generation
        except (AttributeError, RuntimeError):
            generation = None
        candidate_services = getattr(generation, "services", None)
        if isinstance(candidate_services, ServiceView):
            return candidate_services
        return self._services_base.view()

    @property
    def scope(self) -> Scope | None:
        """Return the effective Agent scope of the current Generation."""

        try:
            generation = self.loop.compositions.current_generation
        except (AttributeError, RuntimeError):
            generation = None
        candidate_scope = getattr(generation, "scope", None)
        return candidate_scope if isinstance(candidate_scope, Scope) else None

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

    async def _has_active_turn(self) -> bool:
        async with self._lock:
            return any(not task.done() for task in self._active.values())

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

    async def persisted_external_plugin_ids(self, session_id: str) -> tuple[str, ...]:
        """Return the Session's durable plugin identity for safe resume output."""

        return await self._plugin_compositions.persisted_external_plugin_ids(session_id)

    async def verify_session_plugins(self, session_id: str) -> None:
        """Refuse a Session whose durable identity differs from the current Generation."""

        await self._plugin_compositions.verify_session_plugins(session_id)

    async def replace_plugin_composition(
        self,
        enabled_plugin_ids: Sequence[str],
        *,
        plugin_configs: Mapping[str, Mapping[str, object]] | None = None,
        plugin_discovery: PluginDiscovery | None = None,
    ) -> PluginCompositionReplacement:
        """Delegate one internal replacement to the composition control plane."""

        return await self._plugin_compositions.replace_plugin_composition(
            enabled_plugin_ids,
            plugin_configs=plugin_configs,
            plugin_discovery=plugin_discovery,
        )

    async def migrate_session_plugin_composition(
        self,
        session_id: str,
        enabled_plugin_ids: Sequence[str],
        *,
        plugin_configs: Mapping[str, Mapping[str, object]] | None = None,
        plugin_discovery: PluginDiscovery | None = None,
    ) -> PluginCompositionReplacement:
        """Delegate one explicit Session migration to the control plane."""

        return await self._plugin_compositions.migrate_session_plugin_composition(
            session_id,
            enabled_plugin_ids,
            plugin_configs=plugin_configs,
            plugin_discovery=plugin_discovery,
        )

    async def reload_plugin_composition(
        self,
        session_id: str,
    ) -> PluginCompositionReplacement:
        """Rebuild the current external identity set without authorizing migration."""

        return await self.migrate_session_plugin_composition(
            session_id,
            self.enabled_plugin_ids,
        )

    async def run(self, workspace: Path, task: str) -> TurnResult:
        session_id = await self.create_session(workspace)
        return await self.run_existing(session_id, task)

    async def run_existing(self, session_id: str, task: str) -> TurnResult:
        task_handle: asyncio.Task[TurnResult] | None = None
        try:
            async with self._plugin_compositions.turn_admission():
                await self.verify_session_plugins(session_id)
                async with self._lock:
                    # This is the admission linearization point.  dispose()
                    # flips the same flag while holding this lock, so an
                    # identity-verification await cannot let a new Turn slip
                    # through after shutdown has begun.
                    if self._disposed:
                        raise RuntimeError("runtime is disposed")
                    existing = self._active.get(session_id)
                    if existing is not None and not existing.done():
                        raise AgentAlreadyRunningError(session_id)
                    task_handle = asyncio.create_task(
                        self.loop.run_turn(session_id, task),
                        name=f"traceh-turn-{session_id}",
                    )
                    self._active[session_id] = task_handle
            return await task_handle
        finally:
            if task_handle is not None:
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

        shutdown_errors: list[BaseException] = []
        async with self._lock:
            tasks = tuple(self._active.values())
            self._active.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Candidate setup/health/publish and pre-registration Turn admission
        # belong to the plugin composition control plane.  It converges both
        # sets before any Generation can be drained.
        shutdown_errors.extend(await self._plugin_compositions.shutdown_inflight())
        # Turns converge first.  Composition Drain then retires and unloads
        # every Generation-owned ActivationSet only after its leases end.  The
        # default Stage B path has no PluginManager cleanup owner; the optional
        # manager below exists only for custom legacy assemblies and is cleaned
        # after Drain so it cannot pull resources from a live Generation.
        # Cleanup errors are accumulated so a legacy owner still gets its one
        # cleanup opportunity before shutdown reports failure.
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
        immediately and Generation-owned cleanup was never reached.

        Now the work belongs to the task, not to whoever is waiting on it:

        * the caller waits through ``shield``, so its cancellation never touches
          the shutdown itself;
        * repeat cancellation is absorbed by ``await_worker_convergence`` - a
          second and third Ctrl+C cannot release the caller early;
        * the original ``CancelledError`` is re-raised only after shutdown has
          fully converged;
        * in-flight plugin replacement Tasks are canceled and their candidate
          rollback is awaited before Composition Drain starts;
        * a later ``dispose()`` awaits the *same* task, so it reuses the one real
          outcome. If shutdown failed, that failure is raised again rather than
          being silently reported as success.
        """

        # The flag and Turn admission's final check share ``_lock``.  This
        # gives dispose and creation of a new Turn one explicit linearization
        # point rather than leaving a verification await between two checks.
        async with self._lock:
            self._disposed = True
            self._dispose_started.set()
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
    services: ServiceRegistry
    service_bindings: tuple[ScopedServiceBinding, ...]
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
    service_bindings: Sequence[ScopedServiceBinding] = (),
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
    services = ServiceRegistry()
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
        services=services,
        service_bindings=tuple(service_bindings),
        policies=effective_policies,
        tool_middlewares=tool_middlewares,
        verifier=effective_verifier,
        continuation=continuation,
    )


def _finish_default_runtime(
    prepared: _PreparedRuntime,
    *,
    activation_set: PluginActivationSet,
    plugin_builder: PluginGenerationBuilder,
    plugin_manager: PluginManager | None = None,
) -> AgentRuntime:
    config = prepared.config
    core_tool_runtime = ToolRuntime(
        prepared.tool_registry,
        prepared.sessions,
        policies=prepared.policies,
        middlewares=prepared.tool_middlewares,
        timeout_seconds=config.tool_timeout_seconds,
        max_output_chars=config.max_tool_output_chars,
    )
    tool_runtime = ToolRuntime(
        activation_set.tools,
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
        prompt=activation_set.prompt,
        provider=config.provider,
        model=config.model,
        temperature=config.temperature,
        max_output_tokens=config.max_output_tokens,
        plugins=activation_set.identities,
        activation_set=activation_set,
        compatibility_tools_source=core_tool_runtime,
        compatibility_prompt_source=prepared.prompt,
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
        plugins=activation_set.identities,
        plugin_manager=plugin_manager,
        services=prepared.services,
        plugin_builder=plugin_builder,
        assembly_llms=prepared.llms,
        assembly_policies=prepared.policies,
        assembly_middlewares=prepared.tool_middlewares,
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
    service_bindings: Sequence[ScopedServiceBinding] = (),
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
        service_bindings=service_bindings,
    )
    from traceh.plugins.manager import PluginGenerationBuilder

    builder = PluginGenerationBuilder(
        tools=prepared.tool_registry,
        prompt=prepared.prompt,
        services=prepared.services,
        service_bindings=prepared.service_bindings,
    )
    return _finish_default_runtime(
        prepared,
        activation_set=builder.empty(),
        plugin_builder=builder,
    )


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
    service_bindings: Sequence[ScopedServiceBinding] = (),
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
        service_bindings=service_bindings,
    )
    from traceh.plugins.manager import PluginGenerationBuilder

    builder = PluginGenerationBuilder(
        tools=prepared.tool_registry,
        prompt=prepared.prompt,
        services=prepared.services,
        service_bindings=prepared.service_bindings,
        discovery=plugin_discovery,
        plugin_configs=plugin_configs,
    )
    activation_set: PluginActivationSet | None = None
    try:
        if enabled_plugins:
            activation_set = await builder.prepare(tuple(enabled_plugins))
        else:
            activation_set = builder.empty()
        return _finish_default_runtime(
            prepared,
            activation_set=activation_set,
            plugin_builder=builder,
        )
    except BaseException as error:
        # Activation succeeded but assembly did not: the candidate is live and
        # the Composition Generation did not receive it, so roll it back now.
        if activation_set is not None:
            try:
                await activation_set.dispose()
            except asyncio.CancelledError:
                raise
            except BaseException as cleanup_error:
                if isinstance(error, asyncio.CancelledError):
                    raise cleanup_error from None
                raise ExceptionGroup(
                    "plugin composition candidate cleanup failed",
                    (error, cleanup_error),
                ) from None
        raise
