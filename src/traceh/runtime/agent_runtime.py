"""Public runtime facade and default composition factories."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from traceh.api.events import EventEnvelope
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
from traceh.plugins.errors import PluginDisposeError
from traceh.runtime.agent_loop import AgentLoop, TurnResult
from traceh.runtime.composition_runtime import (
    CompositionGeneration,
    GenerationCompositionRuntime,
)
from traceh.runtime.continuation import ContinuationRuntime
from traceh.runtime.prompt import PromptAssembler, default_coding_prompt
from traceh.runtime.request_builder import RequestBuilder
from traceh.runtime.verification import CommandVerifier, CompletionVerifier
from traceh.session.compaction import CompactionService
from traceh.session.event_feed import EventFeed, PublishingEventStore, SessionEventFeed
from traceh.session.event_store import ConcurrencyConflict, EventStore
from traceh.session.invariants import CoreInvariantChecker
from traceh.session.jsonl import JsonlEventStore
from traceh.session.plugin_identity import (
    MIGRATION_EVENT_TYPE,
    PLUGIN_METADATA_KEY,
    PersistedPluginComposition,
    PluginIdentityProtocolError,
    comparable_plugin_identities,
    external_plugin_identities,
    find_migration_event,
    migration_event_data,
    rebuild_plugin_identity,
)
from traceh.session.projections import StateProjector
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


class AgentAlreadyRunningError(RuntimeError):
    pass


class SessionPluginMismatchError(RuntimeError):
    """A persisted session was created under a different plugin composition.

    Continuing anyway would let a turn run against tools and prompt sections the
    earlier turns never had - or, worse, silently drop a plugin whose tool results
    are already part of the session's history.
    """


class SessionPluginMigrationError(RuntimeError):
    """A user-requested Session migration could not reach a safe outcome."""


@dataclass(frozen=True, slots=True)
class PluginCompositionReplacement:
    """Non-secret result of an internal plugin composition replacement."""

    generation_id: int
    plugins: tuple[PluginIdentity, ...]
    migration_id: str | None = None

    @property
    def enabled_plugin_ids(self) -> tuple[str, ...]:
        return tuple(
            identity.plugin_id
            for identity in self.plugins
            if identity.plugin_id != CORE_PLUGIN_IDENTITY.plugin_id
        )


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
        self._plugin_builder = plugin_builder
        self._assembly_llms = assembly_llms
        self._assembly_policies = tuple(assembly_policies)
        self._assembly_middlewares = tuple(assembly_middlewares)
        self._active: dict[str, asyncio.Task[TurnResult]] = {}
        self._lock = asyncio.Lock()
        # Turn admission and Session migration share this gate.  A migration
        # holds it across candidate preparation, the authorization CAS and
        # Generation publication; a Turn holds it through identity verification
        # and active-turn registration.
        self._composition_gate = asyncio.Lock()
        self._composition_replacement_lock = asyncio.Lock()
        self._turn_admission_task_lock = asyncio.Lock()
        self._turn_admission_tasks: set[asyncio.Task[object]] = set()
        # Replacement workers own candidate setup and rollback.  Keeping the
        # workers here makes them part of Runtime shutdown rather than leaving
        # them attached only to whichever caller requested a replacement.
        self._replacement_task_lock = asyncio.Lock()
        self._replacement_tasks: set[asyncio.Task[PluginCompositionReplacement]] = set()
        self._disposed = False
        self._dispose_started = asyncio.Event()
        self._dispose_task: asyncio.Task[None] | None = None

    @property
    def plugins(self) -> tuple[PluginIdentity, ...]:
        composition_plugins = getattr(self.loop.compositions, "plugins", None)
        if composition_plugins is None:
            return self._initial_plugins
        return tuple(composition_plugins)

    @property
    def services(self) -> ServiceRegistry:
        activation_set = getattr(self.loop.compositions, "current_activation_set", None)
        if activation_set is not None:
            candidate_services = getattr(activation_set, "services", None)
            if isinstance(candidate_services, ServiceRegistry):
                return candidate_services
        return self._services_base

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

    async def _persisted_plugin_composition(
        self,
        session_id: str,
    ) -> tuple[tuple[EventEnvelope, ...], PersistedPluginComposition]:
        events = await self.sessions.read_session(session_id)
        if not events or events[0].type != "session/created":
            await self.sessions.ensure_session(session_id)
            return events, PersistedPluginComposition((), 0)
        try:
            facts = rebuild_plugin_identity(events)
        except PluginIdentityProtocolError as error:
            raise SessionPluginMismatchError(str(error)) from None
        return events, facts

    @staticmethod
    def _assert_persisted_session_idle(events: tuple[EventEnvelope, ...]) -> None:
        """Reject migration while durable Turn/Step lifecycle is still open."""

        projection = StateProjector().project(events)
        if projection.open_turn_id is not None or projection.open_step_id is not None:
            raise AgentAlreadyRunningError("session has an open durable Turn or Step")

    def _assert_session_plugin_composition(
        self,
        persisted: PersistedPluginComposition,
    ) -> None:
        active = self.external_plugin_identities
        if comparable_plugin_identities(persisted.plugins) != comparable_plugin_identities(
            active
        ):
            required = (
                ", ".join(f"{item.plugin_id}=={item.version}" for item in persisted.plugins)
                or "none"
            )
            current = ", ".join(f"{item.plugin_id}=={item.version}" for item in active) or "none"
            raise SessionPluginMismatchError(
                "session plugin composition does not match the active runtime; "
                f"session requires [{required}], current runtime has [{current}]"
            )

    async def persisted_external_plugin_ids(self, session_id: str) -> tuple[str, ...]:
        """Return the Session's durable plugin identity for safe resume output."""

        _, persisted = await self._persisted_plugin_composition(session_id)
        return tuple(identity.plugin_id for identity in persisted.plugins)

    async def _reread_session_after_cancellation(
        self,
        session_id: str,
    ) -> tuple[EventEnvelope, ...]:
        """Read a may-have-committed append without a second cancel escape."""

        read_task = asyncio.create_task(self.sessions.read_session(session_id))
        try:
            return await asyncio.shield(read_task)
        except asyncio.CancelledError:
            await await_worker_convergence(read_task)
            if read_task.cancelled():
                raise
            return read_task.result()

    async def verify_session_plugins(self, session_id: str) -> None:
        """Refuse a Session whose durable identity differs from the current Generation."""

        _, persisted = await self._persisted_plugin_composition(session_id)
        self._assert_session_plugin_composition(persisted)

    async def _prepare_plugin_generation(
        self,
        enabled_plugin_ids: Sequence[str],
        *,
        plugin_configs: Mapping[str, Mapping[str, object]] | None = None,
        plugin_discovery: PluginDiscovery | None = None,
    ) -> tuple[PluginActivationSet, CompositionGeneration]:
        """Prepare a complete candidate while the caller owns its rollback."""

        if self._disposed:
            raise RuntimeError("runtime is disposed")
        builder = self._plugin_builder
        if builder is None or not callable(getattr(builder, "prepare", None)):
            raise RuntimeError("this runtime has no plugin composition builder")
        if self._assembly_llms is None:
            raise RuntimeError("this runtime has no core LLM registry for replacement")
        activation_set = await builder.prepare(
            tuple(enabled_plugin_ids),
            discovery=plugin_discovery,
            plugin_configs=plugin_configs,
        )
        try:
            if self._disposed:
                raise RuntimeError("runtime is disposed")
            current = self.loop.compositions.current_generation
            candidate_tools = ToolRuntime(
                activation_set.tools,
                self.sessions,
                policies=self._assembly_policies,
                middlewares=self._assembly_middlewares,
                timeout_seconds=self.config.tool_timeout_seconds,
                max_output_chars=self.config.max_tool_output_chars,
            )
            generation = CompositionGeneration(
                llms=self._assembly_llms,
                tools=candidate_tools,
                prompt=activation_set.prompt,
                provider=current.provider,
                model=current.model,
                temperature=current.temperature,
                max_output_tokens=current.max_output_tokens,
                plugins=activation_set.identities,
                activation_set=activation_set,
            )
        except BaseException as error:
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
        return activation_set, generation

    @staticmethod
    def _candidate_ownership_transferred(activation_set: PluginActivationSet) -> bool:
        """Return whether publish already handed cleanup to a Generation."""

        return getattr(activation_set, "claimed_by", None) is not None

    async def _replace_plugin_composition(
        self,
        enabled_plugin_ids: Sequence[str],
        *,
        plugin_configs: Mapping[str, Mapping[str, object]] | None = None,
        plugin_discovery: PluginDiscovery | None = None,
    ) -> PluginCompositionReplacement:
        """Prepare and publish one plugin candidate through the Stage B path."""

        async with self._composition_replacement_lock:
            activation_set, generation = await self._prepare_plugin_generation(
                enabled_plugin_ids,
                plugin_configs=plugin_configs,
                plugin_discovery=plugin_discovery,
            )
            try:
                generation_id = await self.loop.compositions.publish(generation)
            except BaseException as error:
                if not self._candidate_ownership_transferred(activation_set):
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
            return PluginCompositionReplacement(
                generation_id=generation_id,
                plugins=tuple(activation_set.identities),
            )

    async def replace_plugin_composition(
        self,
        enabled_plugin_ids: Sequence[str],
        *,
        plugin_configs: Mapping[str, Mapping[str, object]] | None = None,
        plugin_discovery: PluginDiscovery | None = None,
    ) -> PluginCompositionReplacement:
        """Prepare and publish one replacement owned by this Runtime.

        The current Task is registered as Runtime-owned work before candidate
        setup starts.  Cancellation therefore enters the same rollback path,
        and Runtime disposal can cancel and wait for every in-flight
        replacement before Composition Drain begins.
        """

        if self._disposed:
            raise RuntimeError("runtime is disposed")
        replacement_task = asyncio.current_task()
        if replacement_task is None:
            raise RuntimeError("plugin replacement requires an asyncio Task")
        async with self._replacement_task_lock:
            if self._disposed:
                raise RuntimeError("runtime is disposed")
            self._replacement_tasks.add(replacement_task)
        try:
            async with self._composition_gate:
                return await self._replace_plugin_composition(
                    enabled_plugin_ids,
                    plugin_configs=plugin_configs,
                    plugin_discovery=plugin_discovery,
                )
        finally:
            async with self._replacement_task_lock:
                self._replacement_tasks.discard(replacement_task)

    async def _publish_migration_candidate(
        self,
        activation_set: PluginActivationSet,
        generation: CompositionGeneration,
    ) -> PluginCompositionReplacement:
        """Publish a prepared candidate while the caller retains rollback ownership."""

        generation_id = await self.loop.compositions.publish(generation)
        return PluginCompositionReplacement(
            generation_id=generation_id,
            plugins=tuple(activation_set.identities),
        )

    async def migrate_session_plugin_composition(
        self,
        session_id: str,
        enabled_plugin_ids: Sequence[str],
        *,
        plugin_configs: Mapping[str, Mapping[str, object]] | None = None,
        plugin_discovery: PluginDiscovery | None = None,
    ) -> PluginCompositionReplacement:
        """Authorize and publish one explicit Session plugin composition.

        The composition gate makes Session admission and migration one
        application-wide linearization domain.  The authorization is appended
        with the latest stream head as a CAS value only after the candidate is
        fully healthy.  If an append cancellation may have committed, the
        Session is reread by the stable migration id before rollback or publish
        is chosen.
        """

        migration_task = asyncio.current_task()
        if migration_task is None:
            raise RuntimeError("plugin migration requires an asyncio Task")
        async with self._replacement_task_lock:
            if self._disposed:
                raise RuntimeError("runtime is disposed")
            self._replacement_tasks.add(migration_task)
        try:
            async with self._composition_gate:
                if self._disposed:
                    raise RuntimeError("runtime is disposed")
                if getattr(self.loop.compositions, "poisoned", False):
                    raise RuntimeError(
                        "composition runtime is poisoned by generation cleanup failure"
                    )
                events, persisted = await self._persisted_plugin_composition(session_id)
                self._assert_persisted_session_idle(events)
                self._assert_session_plugin_composition(persisted)
                async with self._lock:
                    if any(not task.done() for task in self._active.values()):
                        raise AgentAlreadyRunningError("runtime has an active Turn")

                activation_set, generation = await self._prepare_plugin_generation(
                    tuple(enabled_plugin_ids),
                    plugin_configs=plugin_configs,
                    plugin_discovery=plugin_discovery,
                )
                candidate_owned = True
                migration_id = str(uuid4())
                try:
                    events, persisted = await self._persisted_plugin_composition(session_id)
                    self._assert_persisted_session_idle(events)
                    if comparable_plugin_identities(persisted.plugins) != (
                        comparable_plugin_identities(self.external_plugin_identities)
                    ):
                        raise SessionPluginMigrationError(
                            "session identity changed while plugin composition was preparing"
                        )
                    target = external_plugin_identities(tuple(activation_set.identities))
                    same_identity = comparable_plugin_identities(persisted.plugins) == (
                        comparable_plugin_identities(target)
                    )
                    if same_identity:
                        result = await self._publish_migration_candidate(
                            activation_set, generation
                        )
                        candidate_owned = False
                        return result

                    if not events:
                        raise SessionPluginMigrationError("session has no durable head")
                    expected_head_seq = events[-1].seq
                    payload = migration_event_data(
                        migration_id=migration_id,
                        source_seq=persisted.source_seq,
                        from_plugins=persisted.plugins,
                        to_plugins=target,
                    )
                    append_error: BaseException | None = None
                    try:
                        await self.sessions.append_session(
                            session_id,
                            MIGRATION_EVENT_TYPE,
                            payload,
                            expected_seq=expected_head_seq,
                        )
                    except BaseException as error:
                        append_error = error

                    if append_error is not None:
                        latest = await self._reread_session_after_cancellation(session_id)
                        committed = find_migration_event(latest, migration_id) is not None
                        if not committed:
                            if isinstance(append_error, asyncio.CancelledError):
                                raise append_error
                            if isinstance(append_error, ConcurrencyConflict):
                                raise SessionPluginMigrationError(
                                    "session changed before migration authorization "
                                    "could be recorded"
                                ) from append_error
                            raise SessionPluginMigrationError(
                                "migration authorization could not be recorded"
                            ) from append_error
                        # The append may have crossed the durability boundary.
                        # It is now a fact, so converge to the authorized target
                        # instead of pretending the operation never happened.
                        try:
                            result = await self._publish_migration_candidate(
                                activation_set, generation
                            )
                        except BaseException as publish_error:
                            raise SessionPluginMigrationError(
                                "migration authorization was recorded but composition "
                                "publish failed"
                            ) from publish_error
                        candidate_owned = False
                        if isinstance(append_error, asyncio.CancelledError):
                            raise append_error
                        raise SessionPluginMigrationError(
                            "migration authorization was recorded after an append failure"
                        ) from append_error

                    try:
                        result = await self._publish_migration_candidate(
                            activation_set, generation
                        )
                    except BaseException as publish_error:
                        # The durable Session now requires the target.  The old
                        # Generation remains in memory but verify_session_plugins
                        # will fail closed until a matching target is published.
                        raise SessionPluginMigrationError(
                            "migration authorization was recorded but composition publish failed"
                        ) from publish_error
                    candidate_owned = False
                    return PluginCompositionReplacement(
                        generation_id=result.generation_id,
                        plugins=result.plugins,
                        migration_id=migration_id,
                    )
                finally:
                    if candidate_owned and not self._candidate_ownership_transferred(
                        activation_set
                    ):
                        await activation_set.dispose()
        except ConcurrencyConflict as error:
            raise SessionPluginMigrationError(
                "session changed before migration authorization could be recorded"
            ) from error
        finally:
            async with self._replacement_task_lock:
                self._replacement_tasks.discard(migration_task)

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
        admission_task = asyncio.current_task()
        if admission_task is None:
            raise RuntimeError("turn admission requires an asyncio Task")
        async with self._turn_admission_task_lock:
            if self._disposed:
                raise RuntimeError("runtime is disposed")
            self._turn_admission_tasks.add(admission_task)
        task_handle: asyncio.Task[TurnResult] | None = None
        try:
            async with self._composition_gate:
                if self._disposed:
                    raise RuntimeError("runtime is disposed")
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
            async with self._turn_admission_task_lock:
                self._turn_admission_tasks.discard(admission_task)

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
        # Candidate setup/health/publish is Runtime-owned work too.  It must
        # converge (including candidate rollback) before any Generation can be
        # drained, otherwise dispose could return while plugin setup still owns
        # tasks or registrations.
        async with self._replacement_task_lock:
            replacement_tasks = tuple(self._replacement_tasks)
        for replacement_task in replacement_tasks:
            replacement_task.cancel()
        for replacement_task in replacement_tasks:
            await await_worker_convergence(replacement_task)
            if not replacement_task.cancelled():
                replacement_error = replacement_task.exception()
                # Replacement setup/validation errors belong to that API's
                # caller.  A rollback cleanup failure also belongs to Runtime
                # shutdown because shutdown initiated the cancellation and
                # must not report success while plugin resources may remain.
                if isinstance(replacement_error, PluginDisposeError):
                    shutdown_errors.append(replacement_error)
        # Disposal has already closed the admission boundary, so no new task
        # can enter this set.  Clear completed entries even if cancellation
        # interrupted a caller's bookkeeping ``finally`` block.
        async with self._replacement_task_lock:
            for replacement_task in replacement_tasks:
                self._replacement_tasks.discard(replacement_task)
        # A caller may have started Turn admission before dispose closed the
        # boundary but still be waiting for the composition gate.  It is not in
        # ``_active`` until identity verification and registration finish, so
        # wait for these admission tasks explicitly before Drain as well.
        async with self._turn_admission_task_lock:
            admission_tasks = tuple(self._turn_admission_tasks)
        for admission_task in admission_tasks:
            await await_worker_convergence(admission_task)
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
    from traceh.plugins.manager import PluginGenerationBuilder

    builder = PluginGenerationBuilder(
        tools=prepared.tool_registry,
        prompt=prepared.prompt,
        services=prepared.services,
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
    from traceh.plugins.manager import PluginGenerationBuilder

    builder = PluginGenerationBuilder(
        tools=prepared.tool_registry,
        prompt=prepared.prompt,
        services=prepared.services,
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
