"""Plugin composition replacement and Session migration control plane.

This module owns the coordination state that sits between the public
``AgentRuntime`` facade and the Generation-backed Composition Runtime.  It does
not run Turns and it does not alter ``AgentLoop``: its job is to serialize
plugin candidates, durable Session identity changes, Turn admission checks,
and shutdown convergence around the existing Composition publication path.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from traceh.api.events import EventEnvelope
from traceh.api.plugins import CORE_PLUGIN_IDENTITY, PluginIdentity
from traceh.concurrency import await_worker_convergence
from traceh.llm.registry import LlmRegistry
from traceh.plugins.errors import PluginDisposeError
from traceh.runtime.composition_runtime import CompositionGeneration
from traceh.session.event_store import ConcurrencyConflict
from traceh.session.plugin_identity import (
    MIGRATION_EVENT_TYPE,
    PersistedPluginComposition,
    PluginIdentityProtocolError,
    comparable_plugin_identities,
    external_plugin_identities,
    find_migration_event,
    migration_event_data,
    rebuild_plugin_identity,
)
from traceh.session.projections import StateProjector
from traceh.session.service import SessionService
from traceh.tools.middleware import ToolMiddleware
from traceh.tools.policy import ToolPolicy
from traceh.tools.runtime import ToolRuntime

if TYPE_CHECKING:
    from traceh.plugins.discovery import PluginDiscovery
    from traceh.plugins.manager import PluginActivationSet


class _GenerationPublisher(Protocol):
    """The narrow Generation surface needed by the control plane."""

    @property
    def current_generation(self) -> CompositionGeneration: ...

    async def publish(self, generation: CompositionGeneration) -> int: ...


class AgentAlreadyRunningError(RuntimeError):
    pass


class SessionPluginMismatchError(RuntimeError):
    """A persisted Session and the current Generation have different plugins.

    Continuing would run later Turns with tools and prompt sections that the
    durable Session history did not authorize, or silently remove a plugin
    whose results are already part of that history.
    """


class SessionPluginMigrationError(RuntimeError):
    """A user-requested Session migration could not reach a safe outcome."""


@dataclass(frozen=True, slots=True)
class PluginCompositionReplacement:
    """Non-secret result of one plugin composition replacement."""

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


class PluginCompositionCoordinator:
    """Coordinate plugin Generations without taking over Turn execution.

    The coordinator owns one linearization gate, candidate replacement tasks,
    and Turn-admission tasks that have crossed into plugin identity checking.
    Runtime liveness and active-Turn state remain owned by ``AgentRuntime`` and
    are exposed here only through narrow callbacks; Session identity remains a
    durable Event Log projection.
    """

    def __init__(
        self,
        *,
        sessions: SessionService,
        compositions: _GenerationPublisher,
        plugin_builder: object | None,
        llms: LlmRegistry | None,
        policies: tuple[ToolPolicy, ...],
        middlewares: tuple[ToolMiddleware, ...],
        tool_timeout_seconds: float,
        max_tool_output_chars: int,
        current_external_plugins: Callable[[], tuple[PluginIdentity, ...]],
        runtime_is_disposed: Callable[[], bool],
        runtime_has_active_turn: Callable[[], Awaitable[bool]],
    ) -> None:
        self._sessions = sessions
        self._compositions = compositions
        self._plugin_builder = plugin_builder
        self._llms = llms
        self._policies = tuple(policies)
        self._middlewares = tuple(middlewares)
        self._tool_timeout_seconds = tool_timeout_seconds
        self._max_tool_output_chars = max_tool_output_chars
        self._current_external_plugins = current_external_plugins
        self._runtime_is_disposed = runtime_is_disposed
        self._runtime_has_active_turn = runtime_has_active_turn
        self._gate = asyncio.Lock()
        self._replacement_lock = asyncio.Lock()
        self._replacement_task_lock = asyncio.Lock()
        self._replacement_tasks: set[asyncio.Task[PluginCompositionReplacement]] = set()
        self._admission_task_lock = asyncio.Lock()
        self._admission_tasks: set[asyncio.Task[object]] = set()

    @property
    def enabled_plugin_ids(self) -> tuple[str, ...]:
        return tuple(identity.plugin_id for identity in self._current_external_plugins())

    async def _persisted_plugin_composition(
        self,
        session_id: str,
    ) -> tuple[tuple[EventEnvelope, ...], PersistedPluginComposition]:
        events = await self._sessions.read_session(session_id)
        if not events or events[0].type != "session/created":
            await self._sessions.ensure_session(session_id)
            return events, PersistedPluginComposition((), 0)
        try:
            facts = rebuild_plugin_identity(events)
        except PluginIdentityProtocolError as error:
            raise SessionPluginMismatchError(str(error)) from None
        return events, facts

    @staticmethod
    def _assert_persisted_session_idle(events: tuple[EventEnvelope, ...]) -> None:
        projection = StateProjector().project(events)
        if projection.open_turn_id is not None or projection.open_step_id is not None:
            raise AgentAlreadyRunningError("session has an open durable Turn or Step")

    def _assert_session_plugin_composition(
        self,
        persisted: PersistedPluginComposition,
    ) -> None:
        active = self._current_external_plugins()
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

    async def verify_session_plugins(self, session_id: str) -> None:
        """Refuse a Session whose durable identity differs from the current Generation."""

        _, persisted = await self._persisted_plugin_composition(session_id)
        self._assert_session_plugin_composition(persisted)

    @asynccontextmanager
    async def turn_admission(self) -> AsyncIterator[None]:
        """Hold the shared composition gate through final Turn registration.

        The caller must perform its own active-Turn registration before leaving
        this context.  That keeps the gate's ownership here while the active
        task table and final dispose check remain in ``AgentRuntime``.
        """

        admission_task = asyncio.current_task()
        if admission_task is None:
            raise RuntimeError("turn admission requires an asyncio Task")
        async with self._admission_task_lock:
            if self._runtime_is_disposed():
                raise RuntimeError("runtime is disposed")
            self._admission_tasks.add(admission_task)
        try:
            async with self._gate:
                if self._runtime_is_disposed():
                    raise RuntimeError("runtime is disposed")
                yield
        finally:
            async with self._admission_task_lock:
                self._admission_tasks.discard(admission_task)

    async def _reread_session_after_cancellation(
        self,
        session_id: str,
    ) -> tuple[EventEnvelope, ...]:
        """Read a may-have-committed append without a second cancel escape."""

        read_task = asyncio.create_task(self._sessions.read_session(session_id))
        try:
            return await asyncio.shield(read_task)
        except asyncio.CancelledError:
            await await_worker_convergence(read_task)
            if read_task.cancelled():
                raise
            return read_task.result()

    async def _prepare_generation(
        self,
        enabled_plugin_ids: Sequence[str],
        *,
        plugin_configs: Mapping[str, Mapping[str, object]] | None = None,
        plugin_discovery: PluginDiscovery | None = None,
    ) -> tuple[PluginActivationSet, CompositionGeneration]:
        if self._runtime_is_disposed():
            raise RuntimeError("runtime is disposed")
        builder = self._plugin_builder
        prepare = getattr(builder, "prepare", None)
        if not callable(prepare):
            raise RuntimeError("this runtime has no plugin composition builder")
        llms = self._llms
        if llms is None:
            raise RuntimeError("this runtime has no core LLM registry for replacement")
        activation_set = await prepare(
            tuple(enabled_plugin_ids),
            discovery=plugin_discovery,
            plugin_configs=plugin_configs,
        )
        try:
            if self._runtime_is_disposed():
                raise RuntimeError("runtime is disposed")
            current = self._compositions.current_generation
            candidate_tools = ToolRuntime(
                activation_set.tools,
                self._sessions,
                policies=tuple(
                    getattr(activation_set, "policies", self._policies)
                ),
                middlewares=self._middlewares,
                timeout_seconds=self._tool_timeout_seconds,
                max_output_chars=self._max_tool_output_chars,
            )
            generation = CompositionGeneration(
                llms=llms,
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
        return getattr(activation_set, "claimed_by", None) is not None

    async def _replace(
        self,
        enabled_plugin_ids: Sequence[str],
        *,
        plugin_configs: Mapping[str, Mapping[str, object]] | None = None,
        plugin_discovery: PluginDiscovery | None = None,
    ) -> PluginCompositionReplacement:
        async with self._replacement_lock:
            activation_set, generation = await self._prepare_generation(
                enabled_plugin_ids,
                plugin_configs=plugin_configs,
                plugin_discovery=plugin_discovery,
            )
            try:
                generation_id = await self._compositions.publish(generation)
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

    async def _register_replacement_task(
        self,
    ) -> asyncio.Task[PluginCompositionReplacement]:
        replacement_task = asyncio.current_task()
        if replacement_task is None:
            raise RuntimeError("plugin replacement requires an asyncio Task")
        async with self._replacement_task_lock:
            if self._runtime_is_disposed():
                raise RuntimeError("runtime is disposed")
            self._replacement_tasks.add(replacement_task)
        return replacement_task

    async def _discard_replacement_task(
        self,
        replacement_task: asyncio.Task[PluginCompositionReplacement],
    ) -> None:
        async with self._replacement_task_lock:
            self._replacement_tasks.discard(replacement_task)

    async def replace_plugin_composition(
        self,
        enabled_plugin_ids: Sequence[str],
        *,
        plugin_configs: Mapping[str, Mapping[str, object]] | None = None,
        plugin_discovery: PluginDiscovery | None = None,
    ) -> PluginCompositionReplacement:
        replacement_task = await self._register_replacement_task()
        try:
            async with self._gate:
                return await self._replace(
                    enabled_plugin_ids,
                    plugin_configs=plugin_configs,
                    plugin_discovery=plugin_discovery,
                )
        finally:
            await self._discard_replacement_task(replacement_task)

    async def _publish_candidate(
        self,
        activation_set: PluginActivationSet,
        generation: CompositionGeneration,
    ) -> PluginCompositionReplacement:
        generation_id = await self._compositions.publish(generation)
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
        migration_task = await self._register_replacement_task()
        try:
            async with self._gate:
                if self._runtime_is_disposed():
                    raise RuntimeError("runtime is disposed")
                if getattr(self._compositions, "poisoned", False):
                    raise RuntimeError(
                        "composition runtime is poisoned by generation cleanup failure"
                    )
                events, persisted = await self._persisted_plugin_composition(session_id)
                self._assert_persisted_session_idle(events)
                self._assert_session_plugin_composition(persisted)
                if await self._runtime_has_active_turn():
                    raise AgentAlreadyRunningError("runtime has an active Turn")

                activation_set, generation = await self._prepare_generation(
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
                        comparable_plugin_identities(self._current_external_plugins())
                    ):
                        raise SessionPluginMigrationError(
                            "session identity changed while plugin composition was preparing"
                        )
                    target = external_plugin_identities(tuple(activation_set.identities))
                    same_identity = comparable_plugin_identities(persisted.plugins) == (
                        comparable_plugin_identities(target)
                    )
                    if same_identity:
                        result = await self._publish_candidate(activation_set, generation)
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
                        await self._sessions.append_session(
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
                        try:
                            result = await self._publish_candidate(activation_set, generation)
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
                        result = await self._publish_candidate(activation_set, generation)
                    except BaseException as publish_error:
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
            await self._discard_replacement_task(migration_task)

    async def shutdown_inflight(self) -> tuple[BaseException, ...]:
        """Cancel and converge candidate work, then pending Turn admissions."""

        shutdown_errors: list[BaseException] = []
        async with self._replacement_task_lock:
            replacement_tasks = tuple(self._replacement_tasks)
        for replacement_task in replacement_tasks:
            replacement_task.cancel()
        for replacement_task in replacement_tasks:
            await await_worker_convergence(replacement_task)
            if not replacement_task.cancelled():
                replacement_error = replacement_task.exception()
                if isinstance(replacement_error, PluginDisposeError):
                    shutdown_errors.append(replacement_error)
        async with self._replacement_task_lock:
            for replacement_task in replacement_tasks:
                self._replacement_tasks.discard(replacement_task)

        async with self._admission_task_lock:
            admission_tasks = tuple(self._admission_tasks)
        for admission_task in admission_tasks:
            await await_worker_convergence(admission_task)
        return tuple(shutdown_errors)
