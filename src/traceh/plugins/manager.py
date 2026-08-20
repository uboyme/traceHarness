"""Transactional startup-time plugin loading and activation.

Activation is a transaction with four ordered phases:

1. **private staged setup** - each enabled plugin runs ``setup`` against private
   staged registries that no runtime, turn or step can observe;
2. **full conflict check** - every staged tool, prompt section, service,
   provider, policy and middleware is compared against what the host already
   registered, and explicitly selected providers/verifiers must exist;
3. **health check** - only now may a plugin's ``health_check`` run;
4. **atomic publish** - contributions enter the candidate's existing registries,
   the complete activation set transfers to one Composition Generation, and every
   activation is marked published.

The ordering of 2 before 3 is deliberate. A plugin whose tool collides with a
built-in is going to be rejected no matter what its health check says, so running
that health check first only grants known-doomed third-party code an extra chance
to execute, take time, or touch the network. Conflicts are decided from data the
manager already holds; nothing is gained by asking the plugin first.

Any failure in any phase unwinds every activation in reverse order. Cancellation
is *not* a failure: see :meth:`PluginManager.activate`.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import re
import threading
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from heapq import heappop, heappush
from types import MappingProxyType
from typing import Any, cast

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from traceh.api.llm import LlmProvider
from traceh.api.plugins import (
    CORE_PLUGIN_IDENTITY,
    Plugin,
    PluginDependency,
    PluginIdentity,
    PluginManifest,
)
from traceh.api.prompts import PromptSection
from traceh.api.services import Registration, ServiceKey
from traceh.api.tools import Tool
from traceh.concurrency import await_worker_convergence
from traceh.kernel.activation import Activation
from traceh.kernel.composition_overlays import (
    CompositionOverlayConflictError,
    CompositionOverlayPlan,
    ResolvedCompositionOverlay,
    ScopedPolicyBinding,
    ScopedPromptBinding,
    ScopedToolBinding,
)
from traceh.kernel.lifespan import CallbackRegistration
from traceh.kernel.registry import ServiceConflictError, ServiceRegistry, ServiceView
from traceh.kernel.scope import Scope, ScopeChain, ScopedServiceBinding
from traceh.llm.registry import LlmRegistry
from traceh.plugins.discovery import DiscoveredPlugin, PluginDiscovery
from traceh.plugins.errors import (
    PluginActivationError,
    PluginDisposeError,
    PluginFailure,
    PluginValidationError,
)
from traceh.plugins.selection import is_plugin_id
from traceh.runtime.prompt import PromptAssembler
from traceh.runtime.verification import CompletionVerifier
from traceh.tools.middleware import ToolMiddleware
from traceh.tools.policy import ToolPolicy
from traceh.tools.registry import ToolRegistry
from traceh.version import __version__

TRACEH_PLUGIN_API_VERSION = __version__
"""Version a manifest's ``requires_traceh`` range is evaluated against.

Same value as the distribution and the package: see :mod:`traceh.version`.
"""

_CAPABILITY_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._:-]{0,126}[A-Za-z0-9])?\Z")
_CONTRIBUTION_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}\Z")
# v0.4 activates application scope only; the other names are recognised so a
# manifest can declare future intent without becoming unparseable.
_KNOWN_SCOPES = frozenset({"application", "workspace", "preset", "agent"})
_MISSING = object()


async def _await_cleanup_task(task: asyncio.Task[None]) -> None:
    """Wait through cancellation without hiding cleanup's terminal failure."""

    try:
        await asyncio.shield(task)
    except asyncio.CancelledError as cancellation:
        await await_worker_convergence(task)
        if not task.cancelled():
            failure = task.exception()
            if failure is not None:
                raise failure from None
        raise cancellation


@dataclass(frozen=True, slots=True)
class PluginNotice:
    """A non-fatal observation, such as an absent optional dependency."""

    code: str
    plugin_id: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "plugin_id": self.plugin_id, "message": self.message}


@dataclass(frozen=True, slots=True)
class PluginStatus:
    plugin_id: str
    state: str
    distribution_name: str | None
    distribution_version: str | None
    entry_value: str
    manifest: PluginManifest | None = None
    failure: PluginFailure | None = None

    def to_dict(self) -> dict[str, object]:
        manifest: dict[str, object] | None = None
        if self.manifest is not None:
            manifest = {
                "plugin_id": self.manifest.plugin_id,
                "version": self.manifest.version,
                "requires_traceh": self.manifest.requires_traceh,
                "requires_plugins": [
                    {"plugin_id": item.plugin_id, "version_spec": item.version_spec}
                    for item in self.manifest.requires_plugins
                ],
                "optional_plugins": [
                    {"plugin_id": item.plugin_id, "version_spec": item.version_spec}
                    for item in self.manifest.optional_plugins
                ],
                "allowed_scopes": list(self.manifest.allowed_scopes),
                "trust_mode": self.manifest.trust_mode,
                "provides": list(self.manifest.provides),
            }
        return {
            "plugin_id": self.plugin_id,
            "state": self.state,
            "distribution": {
                "name": self.distribution_name,
                "version": self.distribution_version,
            },
            "entry_value": self.entry_value,
            "manifest": manifest,
            "failure": self.failure.to_dict() if self.failure else None,
        }


@dataclass(slots=True)
class _LoadedPlugin:
    record: DiscoveredPlugin
    plugin: Plugin
    manifest: PluginManifest


@dataclass(slots=True)
class _ToolContribution:
    name: str
    tool: Tool


@dataclass(slots=True)
class _PromptContribution:
    section: PromptSection


@dataclass(slots=True)
class _ServiceContribution:
    key: ServiceKey[Any]
    value: Any
    replace: bool


@dataclass(slots=True)
class _ProviderContribution:
    name: str
    provider: LlmProvider


@dataclass(slots=True)
class _PolicyContribution:
    name: str
    policy: ToolPolicy


@dataclass(slots=True)
class _MiddlewareContribution:
    name: str
    middleware: ToolMiddleware


@dataclass(slots=True)
class _VerifierContribution:
    name: str
    verifier: CompletionVerifier


class PluginActivationSet:
    """One candidate plugin activation and its generation-owned registries.

    The set is deliberately separate from :class:`PluginManager`.  A manager
    is a builder/discovery object; once a candidate is prepared, this object is
    the only owner of the plugin Activations, their candidate registries and
    their cleanup.  ``claim_generation`` is the synchronous hand-off point
    used by the Composition Runtime.  A set can be claimed by one Generation
    exactly once and cannot be accepted by another Runtime.
    """

    def __init__(
        self,
        *,
        tools: ToolRegistry,
        prompt: PromptAssembler,
        services: ServiceRegistry,
        llms: LlmRegistry | None = None,
        scope_chain: ScopeChain | None = None,
        policies: tuple[ToolPolicy, ...] = (),
        middlewares: tuple[ToolMiddleware, ...] = (),
        verifier: CompletionVerifier | None = None,
        identities: tuple[PluginIdentity, ...],
        activation_order: tuple[str, ...],
        activations: tuple[Activation, ...],
        statuses: tuple[PluginStatus, ...] = (),
        notices: tuple[PluginNotice, ...] = (),
    ) -> None:
        self.tools = tools
        self.prompt = prompt
        self.llms = llms
        self.policies = tuple(policies)
        self.middlewares = tuple(middlewares)
        self.verifier = verifier
        self.identities = tuple(identities)
        # The manager's contribution records are intentionally discarded when
        # ownership transfers to this set.  Preserve an immutable receipt of
        # the candidate at that exact boundary so a public caller may await
        # before constructing a Generation without turning live capability
        # objects into a second source of identity truth.
        self._tools_identity = tools
        self._prompt_identity = prompt
        self._llms_identity = llms
        self._plugin_identities = self.identities
        self._tool_identities = self._capture_registry_identities(tools)
        self._provider_identities = (
            self._capture_registry_identities(llms) if llms is not None else None
        )
        self._prompt_sections = prompt.sections()
        self._policy_identities = self._capture_named_identities(self.policies)
        self._middleware_identities = self._capture_named_identities(self.middlewares)
        self._verifier_identity = verifier
        self._application_services = services.view()
        self._scope_chain = scope_chain or ScopeChain.build(services)
        if not self._scope_chain.has_application_registry(services):
            raise ValueError(
                "plugin ActivationSet scope must use its application service registry"
            )
        self._scope = self._scope_chain.effective
        self._services = self._scope.services
        self.activation_order = tuple(activation_order)
        self.statuses = tuple(statuses)
        self.notices = tuple(notices)
        self._activations = tuple(activations)
        self._claim_lock = threading.Lock()
        self._claimed_by: object | None = None
        self._state = "candidate"
        self._dispose_task: asyncio.Task[None] | None = None

    @staticmethod
    def _capture_registry_identities(
        registry: ToolRegistry | LlmRegistry,
    ) -> tuple[tuple[str, object], ...]:
        identities = tuple((name, registry.require(name)) for name in registry.names())
        if any(getattr(value, "name", None) != name for name, value in identities):
            raise ValueError(
                "plugin ActivationSet capability identity changed before transfer"
            )
        return identities

    @staticmethod
    def _capture_named_identities(
        values: Sequence[object],
    ) -> tuple[tuple[str, object], ...]:
        identities = tuple((getattr(value, "name", None), value) for value in values)
        if any(not isinstance(name, str) for name, _ in identities):
            raise ValueError(
                "plugin ActivationSet capability identity changed before transfer"
            )
        return cast(tuple[tuple[str, object], ...], identities)

    @staticmethod
    def _same_objects(
        current: Sequence[object],
        expected: tuple[tuple[str, object], ...],
    ) -> bool:
        return len(current) == len(expected) and all(
            value is expected_value and getattr(value, "name", None) == expected_name
            for value, (expected_name, expected_value) in zip(
                current,
                expected,
                strict=True,
            )
        )

    @staticmethod
    def _same_registry(
        registry: ToolRegistry | LlmRegistry,
        expected: tuple[tuple[str, object], ...],
    ) -> bool:
        expected_names = tuple(name for name, _ in expected)
        if registry.names() != expected_names:
            return False
        return all(
            registry.get(name) is value and getattr(value, "name", None) == name
            for name, value in expected
        )

    def _verify_generation_capabilities(
        self,
        *,
        tools: ToolRegistry,
        prompt: PromptAssembler,
        policies: Sequence[ToolPolicy],
        middlewares: Sequence[ToolMiddleware],
        verifier: CompletionVerifier | None,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Validate the public candidate again at the Generation hand-off.

        ``prepare_activation_set()`` is public and returning from it is an
        asynchronous ownership boundary.  An owned task may run before the
        caller constructs a Generation, so the candidate must prove that its
        registries and executable identities still equal the receipt captured
        above.  The returned names are the authoritative metadata used by the
        frozen Generation projection.
        """

        valid = (
            self.tools is self._tools_identity
            and tools is self._tools_identity
            and self.prompt is self._prompt_identity
            and prompt is self._prompt_identity
            and prompt.sections() == self._prompt_sections
            and self.llms is self._llms_identity
            and tuple(self.identities) == self._plugin_identities
            and self._same_registry(tools, self._tool_identities)
            and self._same_objects(tuple(self.policies), self._policy_identities)
            and self._same_objects(tuple(policies), self._policy_identities)
            and self._same_objects(
                tuple(self.middlewares), self._middleware_identities
            )
            and self._same_objects(tuple(middlewares), self._middleware_identities)
            and self.verifier is self._verifier_identity
            and verifier is self._verifier_identity
        )
        if self._provider_identities is not None:
            valid = (
                valid
                and self.llms is not None
                and self._same_registry(self.llms, self._provider_identities)
            )
        if not valid:
            raise ValueError(
                "plugin ActivationSet capability identity changed after preparation"
            )
        return (
            tuple(name for name, _ in self._policy_identities),
            tuple(name for name, _ in self._middleware_identities),
        )

    @classmethod
    def empty(
        cls,
        *,
        tools: ToolRegistry,
        prompt: PromptAssembler,
        services: ServiceRegistry,
        llms: LlmRegistry | None = None,
        scope_chain: ScopeChain | None = None,
        policies: tuple[ToolPolicy, ...] = (),
        middlewares: tuple[ToolMiddleware, ...] = (),
        verifier: CompletionVerifier | None = None,
    ) -> PluginActivationSet:
        return cls(
            tools=tools,
            prompt=prompt,
            services=services,
            llms=llms,
            scope_chain=scope_chain,
            policies=policies,
            middlewares=middlewares,
            verifier=verifier,
            identities=(CORE_PLUGIN_IDENTITY,),
            activation_order=(),
            activations=(),
        )

    @property
    def claimed_by(self) -> object | None:
        with self._claim_lock:
            return self._claimed_by

    @property
    def application_services(self) -> ServiceView:
        return self._application_services

    @property
    def scope_chain(self) -> ScopeChain:
        return self._scope_chain

    @property
    def scope(self) -> Scope:
        return self._scope

    @property
    def services(self) -> ServiceView:
        return self._services

    @property
    def state(self) -> str:
        with self._claim_lock:
            return self._state

    @property
    def activations(self) -> tuple[Activation, ...]:
        return self._activations

    @property
    def disposed(self) -> bool:
        task = self._dispose_task
        return task is not None and task.done()

    def _ensure_claimable(self, generation_owner: object) -> None:
        with self._claim_lock:
            if self._claimed_by is not None:
                raise ValueError("plugin ActivationSet is already bound to a Generation")
            if self._dispose_task is not None or self._state != "candidate":
                raise ValueError("plugin ActivationSet is no longer a candidate")
            del generation_owner

    def _claim_for_generation(self, generation_owner: object) -> None:
        with self._claim_lock:
            if self._claimed_by is not None:
                raise ValueError("plugin ActivationSet is already bound to a Generation")
            if self._dispose_task is not None or self._state != "candidate":
                raise ValueError("plugin ActivationSet is no longer a candidate")
            self._claimed_by = generation_owner
            self._state = "owned"

    def _unclaim_for_generation(self, generation_owner: object) -> None:
        with self._claim_lock:
            if self._claimed_by is generation_owner and self._state == "owned":
                self._claimed_by = None
                self._state = "candidate"

    async def _dispose_for_generation(self, generation_owner: object) -> None:
        """Dispose only through the Generation that claimed this set."""

        with self._claim_lock:
            if self._claimed_by is not generation_owner:
                raise ValueError("plugin ActivationSet is not owned by this Generation")
            if self._dispose_task is None:
                self._dispose_task = asyncio.create_task(
                    self._dispose_body(),
                    name="traceh-plugin-activation-set-dispose",
                )
            task = self._dispose_task
        await _await_cleanup_task(task)

    async def _dispose_body(self) -> None:
        failures: list[PluginFailure] = []
        cancellation: asyncio.CancelledError | None = None
        # Activation order is dependency order, so reverse order is the only
        # safe order. Stop every plugin task before releasing any plugin
        # registration: a task in one Activation may still use a Service owned
        # by another Activation in the same set.
        for activation in reversed(self._activations):
            try:
                await activation._cancel_owned_tasks()
            except asyncio.CancelledError as error:
                if cancellation is None:
                    cancellation = error
            except BaseException:
                failures.append(
                    PluginFailure(
                        "plugin-cleanup-failed",
                        "dispose",
                        "Plugin Activation cleanup failed",
                        activation.plugin_id,
                    )
                )
        for activation in reversed(self._activations):
            try:
                await activation._close_registrations()
            except asyncio.CancelledError as error:
                if cancellation is None:
                    cancellation = error
            except BaseException:
                # Do not retain the exception or traceback.  The structured
                # failure is bounded by the number of Activations and contains
                # only repository-authored text.
                failures.append(
                    PluginFailure(
                        "plugin-cleanup-failed",
                        "dispose",
                        "Plugin Activation cleanup failed",
                        activation.plugin_id,
                    )
                )
        with self._claim_lock:
            self._state = "cleanup_failed" if failures else "disposed"
        if failures:
            raise PluginDisposeError(tuple(failures))
        if cancellation is not None:
            raise cancellation

    async def dispose(self) -> None:
        """Converge reverse cleanup exactly once, absorbing repeat cancellation."""

        with self._claim_lock:
            if self._claimed_by is not None:
                raise ValueError(
                    "a Generation-owned ActivationSet must be disposed by its Generation"
                )
            if self._dispose_task is None:
                self._dispose_task = asyncio.create_task(
                    self._dispose_body(),
                    name="traceh-plugin-activation-set-dispose",
                )
            task = self._dispose_task
        await _await_cleanup_task(task)


class PluginGenerationBuilder:
    """Prepare independent plugin candidates for Composition Generations."""

    def __init__(
        self,
        *,
        tools: ToolRegistry,
        prompt: PromptAssembler,
        services: ServiceRegistry,
        llms: LlmRegistry | None = None,
        service_bindings: Sequence[ScopedServiceBinding] = (),
        policies: Sequence[ToolPolicy] = (),
        middlewares: Sequence[ToolMiddleware] = (),
        verifier: CompletionVerifier | None = None,
        verifier_name: str | None = None,
        provider_name: str | None = None,
        tool_bindings: Sequence[ScopedToolBinding] = (),
        prompt_bindings: Sequence[ScopedPromptBinding] = (),
        policy_bindings: Sequence[ScopedPolicyBinding] = (),
        discovery: PluginDiscovery | None = None,
        plugin_configs: Mapping[str, Mapping[str, object]] | None = None,
    ) -> None:
        self.tools = tools
        self.prompt = prompt
        self.services = services
        self.llms = llms
        self.service_bindings = tuple(service_bindings)
        self.policies = tuple(policies)
        self.middlewares = tuple(middlewares)
        self.verifier = verifier
        self.verifier_name = verifier_name
        self.provider_name = provider_name
        if verifier is not None and verifier_name is not None:
            raise ValueError(
                "a direct verifier and a named plugin verifier are mutually exclusive"
            )
        self.composition_overlays = CompositionOverlayPlan(
            tuple(tool_bindings),
            tuple(prompt_bindings),
            tuple(policy_bindings),
        )
        self.discovery = discovery
        self.plugin_configs = plugin_configs

    def _candidate_application(
        self,
    ) -> tuple[
        ResolvedCompositionOverlay,
        ServiceRegistry,
        ScopeChain,
        CompositionOverlayPlan,
    ]:
        services = self.services.fork(parent=None)
        scope_chain = ScopeChain.build(services, self.service_bindings)
        application = self.composition_overlays.application_only().resolve(
            self.tools,
            self.prompt,
            self.policies,
        )
        children = self.composition_overlays.children_only()
        # Reject invalid core/programmatic layering before importing or running
        # any plugin code. A later plugin may add a new application target, so
        # the same plan is checked again inside PluginManager before health.
        children.resolve(application.tools, application.prompt, application.policies)
        return application, services, scope_chain, children

    def empty(self) -> PluginActivationSet:
        application, services, scope_chain, children = self._candidate_application()
        effective = children.resolve(
            application.tools,
            application.prompt,
            application.policies,
        )
        return PluginActivationSet.empty(
            tools=effective.tools,
            prompt=effective.prompt,
            services=services,
            llms=self.llms,
            scope_chain=scope_chain,
            policies=effective.policies,
            middlewares=self.middlewares,
            verifier=self._empty_verifier(),
        )

    def _empty_verifier(self) -> CompletionVerifier | None:
        if self.verifier_name is not None:
            raise PluginValidationError(
                (
                    PluginFailure(
                        "verifier-not-provided",
                        "conflict",
                        "The explicitly selected verifier is not provided",
                    ),
                )
            )
        return self.verifier

    async def prepare(
        self,
        enabled_plugin_ids: Sequence[str],
        *,
        discovery: PluginDiscovery | None = None,
        plugin_configs: Mapping[str, Mapping[str, object]] | None = None,
    ) -> PluginActivationSet:
        application, services, scope_chain, children = self._candidate_application()
        manager = PluginManager(
            tools=application.tools,
            prompt=application.prompt,
            services=services,
            llms=self.llms.fork() if self.llms is not None else None,
            scope_chain=scope_chain,
            policies=application.policies,
            middlewares=self.middlewares,
            verifier=self.verifier,
            verifier_name=self.verifier_name,
            provider_name=self.provider_name,
            composition_overlays=children,
            discovery=discovery if discovery is not None else self.discovery,
            plugin_configs=(
                plugin_configs if plugin_configs is not None else self.plugin_configs
            ),
        )
        await manager.activate(tuple(enabled_plugin_ids))
        try:
            return manager._transfer_activation_set()
        except BaseException as error:
            # Activation ownership has not transferred until construction of
            # PluginActivationSet succeeds.  A receipt or scope validation
            # failure therefore remains this temporary manager's rollback
            # responsibility; the caller has no candidate it could dispose.
            try:
                await manager.dispose()
            except asyncio.CancelledError:
                # dispose() has already converged its shared cleanup Task before
                # re-raising cancellation.  Cancellation of the caller outranks
                # the synchronous transfer failure once cleanup is complete.
                raise
            except BaseException as cleanup_error:
                if isinstance(error, asyncio.CancelledError):
                    raise cleanup_error from None
                # The built-in derives ExceptionGroup when every child is an
                # Exception, while still preserving synchronous BaseException
                # interruptions such as KeyboardInterrupt or SystemExit.
                raise BaseExceptionGroup(
                    "plugin activation transfer cleanup failed",
                    (error, cleanup_error),
                ) from None
            raise


class _PluginContext:
    """Concrete :class:`~traceh.api.plugins.PluginContext` bound to one Activation.

    Registrations land in *staged* registries, so nothing a plugin does during
    setup is visible to the runtime until the manager publishes. Every register
    call also hands a reversing registration to the Activation, which is what
    makes rollback complete rather than best-effort.
    """

    def __init__(
        self,
        *,
        plugin_id: str,
        activation: Activation,
        staged_tools: ToolRegistry,
        staged_prompt: PromptAssembler,
        staged_services: ServiceRegistry,
        staged_llms: LlmRegistry,
        staged_policies: dict[str, ToolPolicy],
        staged_middlewares: dict[str, ToolMiddleware],
        staged_verifiers: dict[str, CompletionVerifier],
        base_services: ServiceRegistry,
        config: Mapping[str, object],
    ) -> None:
        self.plugin_id = plugin_id
        self.activation = activation
        self._staged_tools = staged_tools
        self._staged_prompt = staged_prompt
        self._staged_services = staged_services
        self._staged_llms = staged_llms
        self._staged_policies = staged_policies
        self._staged_middlewares = staged_middlewares
        self._staged_verifiers = staged_verifiers
        self._base_services = base_services
        self._config = MappingProxyType(copy.deepcopy(dict(config)))
        self._task_index = 0
        self.tools: list[_ToolContribution] = []
        self.prompts: list[_PromptContribution] = []
        self.services: list[_ServiceContribution] = []
        self.providers: list[_ProviderContribution] = []
        self.policies: list[_PolicyContribution] = []
        self.middlewares: list[_MiddlewareContribution] = []
        self.verifiers: list[_VerifierContribution] = []
        self._contributions_open = True

    def _ensure_contributions_open(self) -> None:
        if not self._contributions_open:
            raise RuntimeError("plugin contributions are closed after setup")

    def _close_contributions(self) -> None:
        """Freeze the staged Composition before conflict and health phases."""

        self._contributions_open = False

    @staticmethod
    def _capability_name(value: object, *, kind: str) -> str:
        name = getattr(value, "name", None)
        if not isinstance(name, str) or not _CONTRIBUTION_PATTERN.fullmatch(name):
            raise ValueError(f"plugin {kind} name is invalid")
        return name

    def register_provider(self, provider: LlmProvider) -> Registration:
        self._ensure_contributions_open()
        name = self._capability_name(provider, kind="provider")
        if not callable(getattr(provider, "complete", None)):
            raise TypeError("plugin provider complete must be callable")
        contribution = _ProviderContribution(name, provider)
        staged = self._staged_llms.register(provider)
        self.providers.append(contribution)

        async def cleanup() -> None:
            await staged.dispose()
            if contribution in self.providers:
                self.providers.remove(contribution)

        return self.activation.own(CallbackRegistration(cleanup))

    def register_policy(self, policy: ToolPolicy) -> Registration:
        self._ensure_contributions_open()
        name = self._capability_name(policy, kind="policy")
        if not callable(getattr(policy, "check", None)):
            raise TypeError("plugin policy check must be callable")
        if name in self._staged_policies:
            raise RuntimeError(f"plugin policy already registered: {name}")
        contribution = _PolicyContribution(name, policy)
        self._staged_policies[name] = policy
        self.policies.append(contribution)

        async def cleanup() -> None:
            if self._staged_policies.get(name) is policy:
                self._staged_policies.pop(name, None)
            if contribution in self.policies:
                self.policies.remove(contribution)

        return self.activation.own(CallbackRegistration(cleanup))

    def register_middleware(self, middleware: ToolMiddleware) -> Registration:
        self._ensure_contributions_open()
        name = self._capability_name(middleware, kind="middleware")
        if not callable(getattr(middleware, "invoke", None)):
            raise TypeError("plugin middleware invoke must be callable")
        if name in self._staged_middlewares:
            raise RuntimeError(f"plugin middleware already registered: {name}")
        contribution = _MiddlewareContribution(name, middleware)
        self._staged_middlewares[name] = middleware
        self.middlewares.append(contribution)

        async def cleanup() -> None:
            if self._staged_middlewares.get(name) is middleware:
                self._staged_middlewares.pop(name, None)
            if contribution in self.middlewares:
                self.middlewares.remove(contribution)

        return self.activation.own(CallbackRegistration(cleanup))

    def register_verifier(
        self,
        name: str,
        verifier: CompletionVerifier,
    ) -> Registration:
        self._ensure_contributions_open()
        if not isinstance(name, str) or not _CONTRIBUTION_PATTERN.fullmatch(name):
            raise ValueError("plugin verifier name is invalid")
        if not callable(getattr(verifier, "verify", None)):
            raise TypeError("plugin verifier verify must be callable")
        if name in self._staged_verifiers:
            raise RuntimeError(f"plugin verifier already registered: {name}")
        contribution = _VerifierContribution(name, verifier)
        self._staged_verifiers[name] = verifier
        self.verifiers.append(contribution)

        async def cleanup() -> None:
            if self._staged_verifiers.get(name) is verifier:
                self._staged_verifiers.pop(name, None)
            if contribution in self.verifiers:
                self.verifiers.remove(contribution)

        return self.activation.own(CallbackRegistration(cleanup))

    def register_tool(self, tool: Tool) -> Registration:
        self._ensure_contributions_open()
        name = getattr(tool, "name", None)
        if not isinstance(name, str) or not _CONTRIBUTION_PATTERN.fullmatch(name):
            raise ValueError("plugin tool name is invalid")
        if not isinstance(getattr(tool, "description", None), str):
            raise TypeError("plugin tool description must be a string")
        if not isinstance(getattr(tool, "input_schema", None), dict):
            raise TypeError("plugin tool input_schema must be an object")
        if not callable(getattr(tool, "execute", None)):
            raise TypeError("plugin tool execute must be callable")
        contribution = _ToolContribution(name, tool)
        staged = self._staged_tools.register(tool)
        self.tools.append(contribution)

        async def cleanup() -> None:
            await staged.dispose()
            if contribution in self.tools:
                self.tools.remove(contribution)

        return self.activation.own(CallbackRegistration(cleanup))

    def register_prompt(self, section: PromptSection) -> Registration:
        self._ensure_contributions_open()
        if not isinstance(section, PromptSection):
            raise TypeError("plugin prompt must be a PromptSection")
        if not _CONTRIBUTION_PATTERN.fullmatch(section.section_id):
            raise ValueError("plugin prompt section id is invalid")
        if not isinstance(section.content, str):
            raise TypeError("plugin prompt content must be a string")
        if not isinstance(section.priority, int) or isinstance(section.priority, bool):
            raise TypeError("plugin prompt priority must be an integer")
        contribution = _PromptContribution(section)
        staged = self._staged_prompt.register(section)
        self.prompts.append(contribution)

        async def cleanup() -> None:
            await staged.dispose()
            if contribution in self.prompts:
                self.prompts.remove(contribution)

        return self.activation.own(CallbackRegistration(cleanup))

    def require(self, key: ServiceKey[Any]) -> Any:
        staged = self._staged_services.get(key)
        if staged is not None:
            return staged
        return self._base_services.require(key)

    async def provide(
        self,
        key: ServiceKey[Any],
        service: Any,
        *,
        replace: bool = False,
    ) -> Registration:
        self._ensure_contributions_open()
        if not isinstance(key, ServiceKey):
            raise TypeError("plugin service key must be a ServiceKey")
        if not isinstance(key.name, str) or not _CAPABILITY_PATTERN.fullmatch(key.name):
            raise ValueError("plugin service key name is invalid")
        if (
            not isinstance(key.api_major, int)
            or isinstance(key.api_major, bool)
            or key.api_major < 1
        ):
            raise ValueError("plugin service api_major must be a positive integer")
        if service is None:
            raise ValueError("plugin service value cannot be None")
        contribution = _ServiceContribution(key, service, replace)
        staged = await self._staged_services.provide(key, service, replace=replace)
        self.services.append(contribution)

        async def cleanup() -> None:
            await staged.dispose()
            if contribution in self.services:
                self.services.remove(contribution)

        return self.activation.own(CallbackRegistration(cleanup))

    def add_cleanup(self, callback):
        if not callable(callback):
            raise TypeError("plugin cleanup must be callable")
        return self.activation.add_cleanup(callback)

    def spawn_owned(self, coroutine, *, name: str):
        if not inspect.iscoroutine(coroutine):
            raise TypeError("spawn_owned requires a coroutine object")
        self._task_index += 1
        # A plugin-supplied task name is untrusted text that would surface in
        # diagnostics and tracebacks. Keep the identity useful and derive it from
        # values this repository controls instead.
        safe_name = f"traceh-plugin-{self.plugin_id}-task-{self._task_index}"
        del name
        return self.activation.tasks.spawn(coroutine, name=safe_name)

    def get_config(self, key: str, default: object = _MISSING) -> object:
        if not isinstance(key, str) or not key:
            raise ValueError("plugin configuration key must be a non-empty string")
        if key in self._config:
            return copy.deepcopy(self._config[key])
        if default is _MISSING:
            raise KeyError(key)
        return copy.deepcopy(default)

    def config_snapshot(self) -> Mapping[str, object]:
        return MappingProxyType(copy.deepcopy(dict(self._config)))


def _manifest_failure(code: str, plugin_id: str | None, message: str) -> PluginFailure:
    return PluginFailure(code, "manifest", message, plugin_id)


def _validate_dependency(
    dependency: object,
    *,
    owner: str,
    kind: str,
) -> tuple[PluginFailure, ...]:
    if not isinstance(dependency, PluginDependency):
        return (
            _manifest_failure("invalid-dependency", owner, f"{kind} entry has an invalid type"),
        )
    failures: list[PluginFailure] = []
    if not isinstance(dependency.plugin_id, str) or not is_plugin_id(dependency.plugin_id):
        failures.append(
            _manifest_failure(
                "invalid-dependency-id", owner, f"{kind} contains an invalid plugin id"
            )
        )
    if not isinstance(dependency.version_spec, str):
        failures.append(
            _manifest_failure(
                "invalid-dependency-version", owner, f"{kind} version must be a string"
            )
        )
    else:
        try:
            SpecifierSet(dependency.version_spec)
        except InvalidSpecifier:
            failures.append(
                _manifest_failure(
                    "invalid-dependency-version",
                    owner,
                    f"{kind} contains an invalid PEP 440 version specifier",
                )
            )
    return tuple(failures)


def validate_manifest(manifest: object, *, expected_id: str) -> tuple[PluginFailure, ...]:
    """Validate every manifest field, returning all failures rather than the first.

    Reporting the full set matters for ``traceh plugins doctor``: an author fixing
    a manifest should see every problem in one run instead of rediscovering them
    one reinstall at a time.
    """

    if not isinstance(manifest, PluginManifest):
        return (
            _manifest_failure(
                "manifest-type-invalid",
                expected_id,
                "Plugin manifest must be a PluginManifest value",
            ),
        )
    plugin_id = manifest.plugin_id if isinstance(manifest.plugin_id, str) else expected_id
    failures: list[PluginFailure] = []
    if not isinstance(manifest.plugin_id, str) or not is_plugin_id(manifest.plugin_id):
        failures.append(
            _manifest_failure("plugin-id-invalid", expected_id, "Manifest plugin_id is invalid")
        )
    elif manifest.plugin_id != expected_id:
        # The entry-point name is what the operator enabled and what discovery
        # reported. A manifest claiming a different id would make the two
        # disagree about which plugin is running.
        failures.append(
            _manifest_failure(
                "plugin-id-mismatch",
                expected_id,
                "Manifest plugin_id does not match the entry-point name",
            )
        )
    elif manifest.plugin_id == CORE_PLUGIN_IDENTITY.plugin_id:
        failures.append(
            _manifest_failure(
                "plugin-id-reserved",
                expected_id,
                "The traceh.core plugin id is reserved by the harness",
            )
        )
    if not isinstance(manifest.version, str):
        failures.append(
            _manifest_failure(
                "plugin-version-invalid", plugin_id, "Plugin version must be a string"
            )
        )
    else:
        try:
            Version(manifest.version)
        except InvalidVersion:
            failures.append(
                _manifest_failure(
                    "plugin-version-invalid", plugin_id, "Plugin version is not valid PEP 440"
                )
            )
    if not isinstance(manifest.requires_traceh, str):
        failures.append(
            _manifest_failure(
                "traceh-api-requirement-invalid", plugin_id, "requires_traceh must be a string"
            )
        )
    else:
        try:
            requirement = SpecifierSet(manifest.requires_traceh)
        except InvalidSpecifier:
            failures.append(
                _manifest_failure(
                    "traceh-api-requirement-invalid",
                    plugin_id,
                    "requires_traceh is not a valid PEP 440 specifier",
                )
            )
        else:
            if Version(TRACEH_PLUGIN_API_VERSION) not in requirement:
                failures.append(
                    _manifest_failure(
                        "traceh-api-incompatible",
                        plugin_id,
                        "Plugin does not support this TraceHarness plugin API version",
                    )
                )

    for attribute, label in (
        (manifest.requires_plugins, "requires_plugins"),
        (manifest.optional_plugins, "optional_plugins"),
    ):
        if not isinstance(attribute, tuple):
            failures.append(
                _manifest_failure("dependency-list-invalid", plugin_id, f"{label} must be a tuple")
            )
            continue
        seen: set[str] = set()
        for dependency in attribute:
            failures.extend(_validate_dependency(dependency, owner=plugin_id, kind=label))
            if isinstance(dependency, PluginDependency) and isinstance(dependency.plugin_id, str):
                if dependency.plugin_id in seen:
                    failures.append(
                        _manifest_failure(
                            "duplicate-dependency",
                            plugin_id,
                            f"{label} contains the same plugin more than once",
                        )
                    )
                seen.add(dependency.plugin_id)

    if isinstance(manifest.requires_plugins, tuple) and isinstance(
        manifest.optional_plugins, tuple
    ):
        required = {
            item.plugin_id
            for item in manifest.requires_plugins
            if isinstance(item, PluginDependency)
        }
        optional = {
            item.plugin_id
            for item in manifest.optional_plugins
            if isinstance(item, PluginDependency)
        }
        if required & optional:
            failures.append(
                _manifest_failure(
                    "dependency-kind-conflict",
                    plugin_id,
                    "A plugin cannot be both required and optional",
                )
            )

    if not isinstance(manifest.allowed_scopes, tuple) or not manifest.allowed_scopes:
        failures.append(
            _manifest_failure(
                "allowed-scopes-invalid", plugin_id, "allowed_scopes must be a non-empty tuple"
            )
        )
    else:
        if any(
            not isinstance(scope, str) or scope not in _KNOWN_SCOPES
            for scope in manifest.allowed_scopes
        ):
            failures.append(
                _manifest_failure(
                    "allowed-scopes-invalid", plugin_id, "allowed_scopes contains an unknown scope"
                )
            )
        if len(set(manifest.allowed_scopes)) != len(manifest.allowed_scopes):
            failures.append(
                _manifest_failure(
                    "allowed-scopes-duplicate", plugin_id, "allowed_scopes contains duplicates"
                )
            )
        if "application" not in manifest.allowed_scopes:
            failures.append(
                _manifest_failure(
                    "application-scope-not-allowed",
                    plugin_id,
                    "v0.4 can activate only plugins that allow application scope",
                )
            )

    if not isinstance(manifest.trust_mode, str) or manifest.trust_mode not in {
        "trusted",
        "isolated",
    }:
        failures.append(
            _manifest_failure("trust-mode-invalid", plugin_id, "trust_mode is not supported")
        )
    elif manifest.trust_mode == "isolated":
        # Rejected explicitly rather than downgraded to trusted. Treating a
        # request for isolation as permission to run in-process would silently
        # give the plugin more privilege than it asked for.
        failures.append(
            _manifest_failure(
                "isolated-mode-unsupported",
                plugin_id,
                "trust_mode=isolated is not implemented in v0.4",
            )
        )

    if not isinstance(manifest.provides, tuple):
        failures.append(
            _manifest_failure("provides-invalid", plugin_id, "provides must be a tuple")
        )
    else:
        seen_provides: set[str] = set()
        for capability in manifest.provides:
            if not isinstance(capability, str) or not _CAPABILITY_PATTERN.fullmatch(capability):
                failures.append(
                    _manifest_failure(
                        "provides-invalid", plugin_id, "provides contains an invalid capability id"
                    )
                )
            elif capability in seen_provides:
                failures.append(
                    _manifest_failure(
                        "provides-duplicate", plugin_id, "provides contains a duplicate capability"
                    )
                )
            else:
                seen_provides.add(capability)
    return tuple(failures)


class PluginManager:
    """Discover and transact explicitly enabled plugins for one registry set.

    The manager remains the v0.4 transaction engine and is also used by
    :class:`PluginGenerationBuilder` against private registry forks.  In the
    Stage B default path it transfers successful Activation ownership to a
    :class:`PluginActivationSet`; it does not retain a second cleanup owner.
    ``AgentLoop`` has no knowledge of either class: plugin tools, prompt
    sections and services still use the core mainlines, so there is no parallel
    plugin tool runtime or plugin agent loop.
    """

    def __init__(
        self,
        *,
        tools: ToolRegistry,
        prompt: PromptAssembler,
        services: ServiceRegistry | None = None,
        llms: LlmRegistry | None = None,
        scope_chain: ScopeChain | None = None,
        policies: Sequence[ToolPolicy] = (),
        middlewares: Sequence[ToolMiddleware] = (),
        verifier: CompletionVerifier | None = None,
        verifier_name: str | None = None,
        provider_name: str | None = None,
        composition_overlays: CompositionOverlayPlan | None = None,
        discovery: PluginDiscovery | None = None,
        plugin_configs: Mapping[str, Mapping[str, object]] | None = None,
    ) -> None:
        self.tools = tools
        self.prompt = prompt
        self.services = services or ServiceRegistry()
        self.llms = llms or LlmRegistry()
        self._scope_chain = scope_chain
        self._application_policies = tuple(policies)
        self._application_middlewares = tuple(middlewares)
        self._application_verifier = verifier
        self._selected_verifier_name = verifier_name
        self._selected_provider_name = provider_name
        if verifier is not None and verifier_name is not None:
            raise ValueError(
                "a direct verifier and a named plugin verifier are mutually exclusive"
            )
        self._composition_overlays = composition_overlays or CompositionOverlayPlan()
        self.discovery = discovery or PluginDiscovery()
        self._configs = {
            plugin_id: copy.deepcopy(dict(config))
            for plugin_id, config in (plugin_configs or {}).items()
        }
        self._activations: list[Activation] = []
        self._contexts: dict[str, _PluginContext] = {}
        self._order: tuple[str, ...] = ()
        self._identities: tuple[PluginIdentity, ...] = (CORE_PLUGIN_IDENTITY,)
        self._statuses: dict[str, PluginStatus] = {}
        self._notices: list[PluginNotice] = []
        self._active = False
        self._dispose_task: asyncio.Task[None] | None = None
        self._effective_composition: ResolvedCompositionOverlay | None = None

    @property
    def identities(self) -> tuple[PluginIdentity, ...]:
        return self._identities

    @property
    def enabled_plugin_ids(self) -> tuple[str, ...]:
        return tuple(
            identity.plugin_id
            for identity in self._identities
            if identity.plugin_id != CORE_PLUGIN_IDENTITY.plugin_id
        )

    @property
    def activation_order(self) -> tuple[str, ...]:
        return self._order

    @property
    def statuses(self) -> tuple[PluginStatus, ...]:
        return tuple(self._statuses[key] for key in sorted(self._statuses))

    @property
    def notices(self) -> tuple[PluginNotice, ...]:
        return tuple(self._notices)

    def _status(self, record: DiscoveredPlugin, state: str, *, manifest=None, failure=None) -> None:
        self._statuses[record.entry_name] = PluginStatus(
            plugin_id=record.entry_name,
            state=state,
            distribution_name=record.distribution_name,
            distribution_version=record.distribution_version,
            entry_value=record.entry_value,
            manifest=manifest,
            failure=failure,
        )

    def _fail(self, failures: Sequence[PluginFailure]) -> None:
        for failure in failures:
            if failure.plugin_id and failure.plugin_id in self._statuses:
                current = self._statuses[failure.plugin_id]
                self._statuses[failure.plugin_id] = PluginStatus(
                    plugin_id=current.plugin_id,
                    state="failed",
                    distribution_name=current.distribution_name,
                    distribution_version=current.distribution_version,
                    entry_value=current.entry_value,
                    manifest=current.manifest,
                    failure=failure,
                )

    def _load(
        self, records: Mapping[str, DiscoveredPlugin], enabled: tuple[str, ...]
    ) -> dict[str, _LoadedPlugin]:
        failures: list[PluginFailure] = []
        loaded: dict[str, _LoadedPlugin] = {}
        for plugin_id in sorted(enabled):
            record = records.get(plugin_id)
            if record is None:
                failures.append(
                    PluginFailure(
                        "plugin-not-discovered",
                        "discovery",
                        "Enabled plugin was not found in installed entry points",
                        plugin_id,
                    )
                )
                continue
            self._status(record, "discovered")
            if record.issues:
                failures.extend(
                    PluginFailure(issue.code, "metadata", issue.message, plugin_id)
                    for issue in record.issues
                )
                continue
            try:
                candidate = record.entry_point.load()
            except Exception:
                # The import error's own text is discarded: it can quote plugin
                # source, paths or credentials. The failure code is what a caller
                # branches on anyway.
                failures.append(
                    PluginFailure(
                        "plugin-load-failed",
                        "load",
                        "Plugin entry point could not be imported",
                        plugin_id,
                    )
                )
                continue
            try:
                if inspect.isclass(candidate):
                    candidate = candidate()
                elif not hasattr(candidate, "manifest") and callable(candidate):
                    candidate = candidate()
            except Exception:
                failures.append(
                    PluginFailure(
                        "plugin-factory-failed",
                        "load",
                        "Plugin factory could not create the plugin object",
                        plugin_id,
                    )
                )
                continue
            manifest = getattr(candidate, "manifest", None)
            manifest_failures = validate_manifest(manifest, expected_id=plugin_id)
            if not callable(getattr(candidate, "setup", None)):
                manifest_failures = (
                    *manifest_failures,
                    PluginFailure(
                        "plugin-setup-missing",
                        "load",
                        "Plugin object does not provide setup",
                        plugin_id,
                    ),
                )
            if manifest_failures:
                failures.extend(manifest_failures)
                self._status(
                    record,
                    "loaded",
                    manifest=manifest if isinstance(manifest, PluginManifest) else None,
                )
                continue
            typed_manifest = cast(PluginManifest, manifest)
            typed_plugin = cast(Plugin, candidate)
            loaded[plugin_id] = _LoadedPlugin(record, typed_plugin, typed_manifest)
            self._status(record, "loaded", manifest=typed_manifest)

        if failures:
            ordered = tuple(
                sorted(failures, key=lambda item: (item.plugin_id or "", item.code, item.message))
            )
            self._fail(ordered)
            raise PluginValidationError(ordered)
        return loaded

    def _dependency_order(self, loaded: Mapping[str, _LoadedPlugin]) -> tuple[str, ...]:
        failures: list[PluginFailure] = []
        dependencies: dict[str, set[str]] = {plugin_id: set() for plugin_id in loaded}
        core_version = Version(CORE_PLUGIN_IDENTITY.version)
        for plugin_id in sorted(loaded):
            manifest = loaded[plugin_id].manifest
            for dependency in manifest.requires_plugins:
                specifier = SpecifierSet(dependency.version_spec)
                if dependency.plugin_id == CORE_PLUGIN_IDENTITY.plugin_id:
                    if core_version not in specifier:
                        failures.append(
                            PluginFailure(
                                "plugin-version-incompatible",
                                "dependency",
                                "Required core plugin version is incompatible",
                                plugin_id,
                            )
                        )
                    continue
                target = loaded.get(dependency.plugin_id)
                if target is None:
                    # Installed is not enabled. A required dependency that the
                    # operator did not enable must fail rather than be enabled
                    # implicitly on the plugin's say-so.
                    failures.append(
                        PluginFailure(
                            "required-plugin-missing",
                            "dependency",
                            "A required plugin is not explicitly enabled",
                            plugin_id,
                        )
                    )
                    continue
                if Version(target.manifest.version) not in specifier:
                    failures.append(
                        PluginFailure(
                            "plugin-version-incompatible",
                            "dependency",
                            "An enabled required plugin has an incompatible version",
                            plugin_id,
                        )
                    )
                dependencies[plugin_id].add(dependency.plugin_id)

            for dependency in manifest.optional_plugins:
                specifier = SpecifierSet(dependency.version_spec)
                if dependency.plugin_id == CORE_PLUGIN_IDENTITY.plugin_id:
                    if core_version not in specifier:
                        failures.append(
                            PluginFailure(
                                "optional-plugin-version-incompatible",
                                "dependency",
                                "An enabled optional core dependency has an incompatible version",
                                plugin_id,
                            )
                        )
                    continue
                target = loaded.get(dependency.plugin_id)
                if target is None:
                    self._notices.append(
                        PluginNotice(
                            "optional-plugin-missing",
                            plugin_id,
                            "Optional plugin is not enabled; activation continues",
                        )
                    )
                    continue
                if Version(target.manifest.version) not in specifier:
                    # An optional dependency that *is* enabled but at a version
                    # the plugin declared incompatible is a real conflict, not an
                    # absence to shrug at.
                    failures.append(
                        PluginFailure(
                            "optional-plugin-version-incompatible",
                            "dependency",
                            "An enabled optional plugin has an incompatible version",
                            plugin_id,
                        )
                    )
                dependencies[plugin_id].add(dependency.plugin_id)

        providers: dict[str, str] = {}
        for plugin_id in sorted(loaded):
            for capability in sorted(loaded[plugin_id].manifest.provides):
                previous = providers.get(capability)
                if previous is not None:
                    failures.append(
                        PluginFailure(
                            "provides-conflict",
                            "dependency",
                            "More than one enabled plugin declares the same capability",
                            plugin_id,
                        )
                    )
                else:
                    providers[capability] = plugin_id

        if failures:
            ordered = tuple(sorted(failures, key=lambda item: (item.plugin_id or "", item.code)))
            self._fail(ordered)
            raise PluginValidationError(ordered)

        # Deterministic topological order: the heap makes ties resolve by plugin
        # id, so the same set of plugins always sets up in the same sequence and
        # a reproduction is actually reproducible.
        dependents: dict[str, set[str]] = defaultdict(set)
        indegree = {plugin_id: len(items) for plugin_id, items in dependencies.items()}
        for plugin_id, items in dependencies.items():
            for dependency in items:
                dependents[dependency].add(plugin_id)
        heap: list[str] = []
        for plugin_id, count in indegree.items():
            if count == 0:
                heappush(heap, plugin_id)
        order: list[str] = []
        while heap:
            plugin_id = heappop(heap)
            order.append(plugin_id)
            for dependent in sorted(dependents.get(plugin_id, ())):
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    heappush(heap, dependent)
        if len(order) != len(loaded):
            remaining = sorted(plugin_id for plugin_id, count in indegree.items() if count)
            cycle_failures = tuple(
                PluginFailure(
                    "plugin-dependency-cycle",
                    "dependency",
                    "Plugin dependency graph contains a cycle",
                    plugin_id,
                )
                for plugin_id in remaining
            )
            self._fail(cycle_failures)
            raise PluginValidationError(cycle_failures)
        return tuple(order)

    def _publish_conflicts(self, order: Sequence[str]) -> tuple[PluginFailure, ...]:
        """Compare every staged contribution against what the core already owns.

        Plugin-versus-plugin collisions cannot reach this point: all plugins share
        one set of staged registries, so the second registration of a name already
        raised during that plugin's setup.
        """

        base_tool_names = set(self.tools.names())
        base_prompt_ids = set(self.prompt.section_ids())
        base_provider_names = set(self.llms.names())
        base_policy_names = {policy.name for policy in self._application_policies}
        base_middleware_names = {
            middleware.name for middleware in self._application_middlewares
        }
        conflicts: list[PluginFailure] = []
        for plugin_id in order:
            context = self._contexts[plugin_id]
            for tool_contribution in context.tools:
                if tool_contribution.name in base_tool_names:
                    conflicts.append(
                        PluginFailure(
                            "tool-publish-conflict",
                            "conflict",
                            "Plugin tool conflicts with an existing tool",
                            plugin_id,
                        )
                    )
            for prompt_contribution in context.prompts:
                if prompt_contribution.section.section_id in base_prompt_ids:
                    conflicts.append(
                        PluginFailure(
                            "prompt-publish-conflict",
                            "conflict",
                            "Plugin prompt conflicts with an existing prompt section",
                            plugin_id,
                        )
                    )
            for provider_contribution in context.providers:
                if provider_contribution.name in base_provider_names:
                    conflicts.append(
                        PluginFailure(
                            "provider-publish-conflict",
                            "conflict",
                            "Plugin provider conflicts with an existing provider",
                            plugin_id,
                        )
                    )
            for policy_contribution in context.policies:
                if policy_contribution.name in base_policy_names:
                    conflicts.append(
                        PluginFailure(
                            "policy-publish-conflict",
                            "conflict",
                            "Plugin policy conflicts with an existing policy",
                            plugin_id,
                        )
                    )
            for middleware_contribution in context.middlewares:
                if middleware_contribution.name in base_middleware_names:
                    conflicts.append(
                        PluginFailure(
                            "middleware-publish-conflict",
                            "conflict",
                            "Plugin middleware conflicts with an existing middleware",
                            plugin_id,
                        )
                    )
            for service_contribution in context.services:
                existing = self.services.resolve(service_contribution.key)
                if existing is not None and not service_contribution.replace:
                    conflicts.append(
                        PluginFailure(
                            "service-publish-conflict",
                            "conflict",
                            "Plugin service conflicts with an existing service",
                            plugin_id,
                        )
                    )
                elif existing is None and service_contribution.replace:
                    named = self.services.resolve_name(service_contribution.key.name)
                    if (
                        named is not None
                        and named.key.api_major != service_contribution.key.api_major
                    ):
                        conflicts.append(
                            PluginFailure(
                                "service-override-api-major-mismatch",
                                "conflict",
                                "Plugin service API major does not match existing service",
                                plugin_id,
                            )
                        )
        return tuple(sorted(conflicts, key=lambda item: (item.plugin_id or "", item.code)))

    def _contribution_identity_failures(
        self,
        order: Sequence[str],
    ) -> tuple[PluginFailure, ...]:
        """Reject capability identity drift after its setup registration.

        Plugin capability objects remain executable delegates, but their
        registration names are transaction facts. Conflict checks, overlay
        attribution and publication must therefore never re-read a mutable
        ``name`` after registration. The repeated checks also close every
        await boundary between setup and final candidate publication.
        """

        failures: list[PluginFailure] = []
        for plugin_id in order:
            context = self._contexts[plugin_id]
            contributions = (
                *((item.name, item.tool) for item in context.tools),
                *((item.name, item.provider) for item in context.providers),
                *((item.name, item.policy) for item in context.policies),
                *((item.name, item.middleware) for item in context.middlewares),
            )
            if any(
                getattr(capability, "name", None) != name
                for name, capability in contributions
            ):
                failures.append(
                    PluginFailure(
                        "plugin-contribution-identity-changed",
                        "conflict",
                        "Plugin contribution identity changed after registration",
                        plugin_id,
                    )
                )
        return tuple(failures)

    def _require_stable_contribution_identities(
        self,
        order: Sequence[str],
    ) -> None:
        failures = self._contribution_identity_failures(order)
        if failures:
            raise PluginValidationError(failures)

    def _candidate_policies(self, order: Sequence[str]) -> tuple[ToolPolicy, ...]:
        return (
            *self._application_policies,
            *(
                contribution.policy
                for plugin_id in order
                for contribution in self._contexts[plugin_id].policies
            ),
        )

    def _candidate_middlewares(
        self,
        order: Sequence[str],
    ) -> tuple[ToolMiddleware, ...]:
        return (
            *self._application_middlewares,
            *(
                contribution.middleware
                for plugin_id in order
                for contribution in self._contexts[plugin_id].middlewares
            ),
        )

    def _candidate_verifier(
        self,
        order: Sequence[str],
    ) -> CompletionVerifier | None:
        if self._selected_verifier_name is None:
            return self._application_verifier
        for plugin_id in order:
            for contribution in self._contexts[plugin_id].verifiers:
                if contribution.name == self._selected_verifier_name:
                    return contribution.verifier
        raise PluginValidationError(
            (
                PluginFailure(
                    "verifier-not-provided",
                    "conflict",
                    "The explicitly selected verifier is not provided",
                ),
            )
        )

    def _validate_selected_provider(self, order: Sequence[str]) -> None:
        selected = self._selected_provider_name
        if selected is None or self.llms.get(selected) is not None:
            return
        if any(
            contribution.name == selected
            for plugin_id in order
            for contribution in self._contexts[plugin_id].providers
        ):
            return
        raise PluginValidationError(
            (
                PluginFailure(
                    "provider-not-provided",
                    "conflict",
                    "The explicitly selected provider is not provided",
                ),
            )
        )

    def _resolve_effective_composition(
        self,
        order: Sequence[str],
        *,
        include_staged: bool,
    ) -> ResolvedCompositionOverlay:
        """Resolve child overlays against the candidate application layer.

        Phase 2 uses a private projection of staged contributions so a child
        overlay that forgot ``replace=True`` fails before plugin health code is
        allowed to run. Phase 4 calls the same resolver after real publication;
        the returned registries become the only Generation-visible composition.
        """

        tools = self.tools.fork()
        prompt = self.prompt.fork()
        if include_staged:
            for plugin_id in order:
                context = self._contexts[plugin_id]
                for contribution in context.tools:
                    tools.register(contribution.tool)
                for contribution in context.prompts:
                    prompt.register(contribution.section)
        return self._composition_overlays.resolve(
            tools,
            prompt,
            self._candidate_policies(order),
        )

    def _overlay_failure(
        self,
        error: CompositionOverlayConflictError,
        order: Sequence[str],
    ) -> PluginFailure:
        responsible: str | None = None
        if error.existing_scope == "application":
            for plugin_id in order:
                context = self._contexts[plugin_id]
                if error.capability == "tool" and any(
                    item.name == error.name for item in context.tools
                ):
                    responsible = plugin_id
                    break
                if error.capability == "prompt" and any(
                    item.section.section_id == error.name for item in context.prompts
                ):
                    responsible = plugin_id
                    break
                if error.capability == "policy" and any(
                    item.name == error.name for item in context.policies
                ):
                    responsible = plugin_id
                    break
        return PluginFailure(
            error.code,
            "conflict",
            "Scoped composition overlay is invalid",
            responsible,
        )

    def _transfer_activation_set(self) -> PluginActivationSet:
        """Transfer successful activation ownership to a candidate set."""

        if not self._active:
            raise RuntimeError("plugin activation is not complete")
        if self._effective_composition is None:
            raise RuntimeError("plugin composition was not resolved")
        activation_set = PluginActivationSet(
            tools=self._effective_composition.tools,
            prompt=self._effective_composition.prompt,
            services=self.services,
            llms=self.llms,
            scope_chain=self._scope_chain,
            policies=self._effective_composition.policies,
            middlewares=self._candidate_middlewares(self._order),
            verifier=self._candidate_verifier(self._order),
            identities=self._identities,
            activation_order=self._order,
            activations=tuple(self._activations),
            statuses=self.statuses,
            notices=self.notices,
        )
        # The temporary manager is now only a builder record.  It must not
        # retain a second cleanup owner for the same Activation objects.
        self._activations.clear()
        self._contexts.clear()
        self._active = False
        return activation_set

    async def prepare_activation_set(
        self,
        enabled_plugin_ids: Sequence[str],
    ) -> PluginActivationSet:
        """Build a private candidate without touching this manager's registries."""

        builder = PluginGenerationBuilder(
            tools=self.tools,
            prompt=self.prompt,
            services=self.services,
            llms=self.llms,
            policies=self._application_policies,
            middlewares=self._application_middlewares,
            verifier=self._application_verifier,
            verifier_name=self._selected_verifier_name,
            provider_name=self._selected_provider_name,
            service_bindings=(
                self._scope_chain.child_bindings
                if self._scope_chain is not None
                else ()
            ),
            tool_bindings=self._composition_overlays.tool_bindings,
            prompt_bindings=self._composition_overlays.prompt_bindings,
            policy_bindings=self._composition_overlays.policy_bindings,
            discovery=self.discovery,
            plugin_configs=self._configs,
        )
        return await builder.prepare(enabled_plugin_ids)

    # Short alias for assembly code and callers that prefer the noun used by
    # the Stage B design documents.
    prepare = prepare_activation_set

    async def _rollback(self) -> tuple[tuple[PluginFailure, ...], asyncio.CancelledError | None]:
        """Unwind every activation in reverse order, whatever happens.

        Cancellation arriving *during* rollback is absorbed rather than obeyed:
        ``Activation.dispose()`` has already converged that plugin's owned tasks
        and registrations before re-raising, so the correct response is to record
        the cancellation and keep unwinding the remaining activations. Letting it
        propagate here would strand every activation that had not been reached.
        """

        failures: list[PluginFailure] = []
        cancellation: asyncio.CancelledError | None = None
        for activation in reversed(self._activations):
            try:
                await activation.rollback()
            except asyncio.CancelledError as cancelled:
                if cancellation is None:
                    cancellation = cancelled
            except BaseException:
                failures.append(
                    PluginFailure(
                        "plugin-rollback-failed",
                        "rollback",
                        "Plugin rollback reported one or more cleanup failures",
                        activation.plugin_id,
                    )
                )
        return tuple(failures), cancellation

    async def activate(self, enabled_plugin_ids: Sequence[str]) -> tuple[PluginIdentity, ...]:
        """Run the four-phase activation transaction.

        Cancellation is not a plugin failure. If the caller is cancelled at any
        point - during setup, during a health check, during publish, or during the
        rollback that follows - every activation is still unwound in reverse
        order and owned tasks and cleanups converge. Successful rollback re-raises
        the original ``CancelledError``; a real rollback cleanup failure is
        reported as a bounded ``PluginDisposeError`` instead of being hidden by
        cancellation. A user pressing Ctrl+C never sees their interrupt reported
        as a broken plugin configuration.
        """

        if self._active or self._activations:
            raise RuntimeError("plugin manager activation may run only once")
        enabled = tuple(enabled_plugin_ids)
        if len(set(enabled)) != len(enabled) or any(
            not isinstance(item, str) or not is_plugin_id(item) for item in enabled
        ):
            raise PluginValidationError(
                (
                    PluginFailure(
                        "enabled-plugin-list-invalid",
                        "selection",
                        "Enabled plugin ids must be valid and unique",
                    ),
                )
            )
        if not enabled:
            self._validate_selected_provider(())
            self._candidate_verifier(())
            self._effective_composition = self._resolve_effective_composition(
                (),
                include_staged=False,
            )
            self._active = True
            return self._identities

        discovered = self.discovery.discover()
        records: dict[str, DiscoveredPlugin] = {}
        for record in discovered:
            if record.entry_name not in records:
                records[record.entry_name] = record
        loaded = self._load(records, enabled)
        order = self._dependency_order(loaded)

        staged_tools = ToolRegistry()
        staged_prompt = PromptAssembler()
        staged_services = ServiceRegistry()
        staged_llms = LlmRegistry()
        staged_policies: dict[str, ToolPolicy] = {}
        staged_middlewares: dict[str, ToolMiddleware] = {}
        staged_verifiers: dict[str, CompletionVerifier] = {}
        setup_failure: PluginFailure | None = None
        try:
            # Phase 1: private staged setup.
            for plugin_id in order:
                activation = Activation(plugin_id)
                self._activations.append(activation)
                context = _PluginContext(
                    plugin_id=plugin_id,
                    activation=activation,
                    staged_tools=staged_tools,
                    staged_prompt=staged_prompt,
                    staged_services=staged_services,
                    staged_llms=staged_llms,
                    staged_policies=staged_policies,
                    staged_middlewares=staged_middlewares,
                    staged_verifiers=staged_verifiers,
                    base_services=self.services,
                    config=self._configs.get(plugin_id, {}),
                )
                self._contexts[plugin_id] = context
                try:
                    result = loaded[plugin_id].plugin.setup(
                        context,
                        dict(context.config_snapshot()),
                    )
                    if not inspect.isawaitable(result):
                        raise TypeError("plugin setup must return an awaitable")
                    await result
                except asyncio.CancelledError:
                    # Not a setup failure. Leave setup_failure unset so the
                    # handler below cannot mistake an interrupt for bad config.
                    raise
                except BaseException:
                    setup_failure = PluginFailure(
                        "plugin-setup-failed",
                        "setup",
                        "Plugin setup failed",
                        plugin_id,
                    )
                    raise

            # setup() is the only contribution phase. Freeze every context
            # before conflict checks and health so third-party health code
            # cannot mutate the candidate after it has been validated.
            for plugin_id in order:
                self._contexts[plugin_id]._close_contributions()

            self._require_stable_contribution_identities(order)

            # Phase 2: full conflict check, before any health check runs.
            conflicts = self._publish_conflicts(order)
            if conflicts:
                self._fail(conflicts)
                raise PluginValidationError(conflicts)
            self._validate_selected_provider(order)
            self._candidate_verifier(order)
            try:
                self._resolve_effective_composition(order, include_staged=True)
            except CompositionOverlayConflictError as error:
                failure = self._overlay_failure(error, order)
                self._fail((failure,))
                raise PluginValidationError((failure,)) from None
            # Phase 3: health checks, now that publication is otherwise possible.
            for plugin_id in order:
                health = getattr(loaded[plugin_id].plugin, "health_check", None)
                if health is None:
                    continue
                try:
                    try:
                        parameter_count = len(inspect.signature(health).parameters)
                    except (TypeError, ValueError):
                        parameter_count = 1
                    result = health() if parameter_count == 0 else health(self._contexts[plugin_id])
                    if inspect.isawaitable(result):
                        result = await result
                    if result is False:
                        raise RuntimeError("health check returned false")
                except asyncio.CancelledError:
                    raise
                except BaseException:
                    setup_failure = PluginFailure(
                        "plugin-health-check-failed",
                        "health",
                        "Plugin health check failed",
                        plugin_id,
                    )
                    raise
                self._require_stable_contribution_identities(order)

            # Phase 4: atomic publish. No runtime or turn can observe these
            # registries until this method returns.
            for plugin_id in order:
                activation = next(
                    item for item in self._activations if item.plugin_id == plugin_id
                )
                context = self._contexts[plugin_id]
                for tool_contribution in context.tools:
                    activation.own(self.tools.register(tool_contribution.tool))
                for prompt_contribution in context.prompts:
                    activation.own(self.prompt.register(prompt_contribution.section))
                for provider_contribution in context.providers:
                    activation.own(self.llms.register(provider_contribution.provider))
                for service_contribution in context.services:
                    try:
                        registration = await self.services.provide(
                            service_contribution.key,
                            service_contribution.value,
                            replace=service_contribution.replace,
                        )
                    except ServiceConflictError as error:
                        code = (
                            "service-publish-conflict"
                            if error.code == "service-already-bound"
                            and not service_contribution.replace
                            else error.code
                        )
                        raise PluginValidationError(
                            (
                                PluginFailure(
                                    code,
                                    "publish",
                                    "Plugin service could not be published",
                                    plugin_id,
                                ),
                            )
                        ) from None
                    activation.own(registration)
            # Service publication awaits. An owned plugin task may run at any
            # such boundary, so validate capability identities once more before
            # the synchronous final composition resolution and ownership handoff.
            self._require_stable_contribution_identities(order)
            if self._scope_chain is not None:
                try:
                    self._scope_chain.validate_overrides()
                except ServiceConflictError as error:
                    raise PluginValidationError(
                        (
                            PluginFailure(
                                error.code,
                                "publish",
                                "Service scope composition is invalid",
                            ),
                        )
                    ) from None
            try:
                self._effective_composition = self._resolve_effective_composition(
                    order,
                    include_staged=False,
                )
            except CompositionOverlayConflictError as error:
                failure = self._overlay_failure(error, order)
                raise PluginValidationError((failure,)) from None
            for activation in self._activations:
                activation.publish()

        except asyncio.CancelledError as cancellation:
            rollback_failures, _rollback_cancellation = await self._rollback()
            self._activations.clear()
            self._contexts.clear()
            if rollback_failures:
                # Cancellation caused rollback, but it cannot certify that the
                # rollback succeeded.  A real cleanup failure is the terminal
                # result; only repository-authored structured text escapes.
                self._fail(rollback_failures)
                raise PluginDisposeError(tuple(rollback_failures)) from None
            # Pure cancellation retains the original cancellation object after
            # every contribution, owned task and cleanup has converged.
            raise cancellation
        except BaseException as error:
            rollback_failures, cancellation = await self._rollback()
            self._activations.clear()
            self._contexts.clear()
            if cancellation is not None:
                # A cancellation that arrived while unwinding a genuine failure
                # still outranks it: the caller is going away. `from None`
                # because the setup failure is not the cause of the interrupt.
                raise cancellation from None
            if isinstance(error, PluginValidationError):
                failures = (*error.failures, *rollback_failures)
            else:
                primary = setup_failure or PluginFailure(
                    "plugin-publish-failed",
                    "publish",
                    "Plugin activation could not be published",
                )
                failures = (primary, *rollback_failures)
            self._fail(failures)
            raise PluginActivationError(tuple(failures)) from None

        self._order = order
        self._identities = (
            CORE_PLUGIN_IDENTITY,
            *(PluginIdentity(plugin_id, loaded[plugin_id].manifest.version) for plugin_id in order),
        )
        self._active = True
        for plugin_id in order:
            current = self._statuses[plugin_id]
            self._statuses[plugin_id] = PluginStatus(
                plugin_id=current.plugin_id,
                state="active",
                distribution_name=current.distribution_name,
                distribution_version=current.distribution_version,
                entry_value=current.entry_value,
                manifest=current.manifest,
            )
        return self._identities

    async def _dispose(self) -> None:
        failures: list[PluginFailure] = []
        raw_errors: list[BaseException] = []
        # Reverse activation order, so a plugin is torn down before anything it
        # depended on.
        for activation in reversed(self._activations):
            try:
                await activation.dispose()
            except BaseException as error:
                # One plugin's failing cleanup must not skip the others.
                raw_errors.append(error)
                failures.append(
                    PluginFailure(
                        "plugin-cleanup-failed",
                        "dispose",
                        "Plugin cleanup reported one or more failures",
                        activation.plugin_id,
                    )
                )
        self._active = False
        if failures:
            structured = PluginDisposeError(tuple(failures))
            structured.cleanup_errors = tuple(raw_errors)  # type: ignore[attr-defined]
            raise structured

    async def dispose(self) -> None:
        """Tear every activation down exactly once, converging on repeat cancellation."""

        if self._dispose_task is None:
            self._dispose_task = asyncio.create_task(
                self._dispose(),
                name="traceh-plugin-manager-dispose",
            )
        task = self._dispose_task
        await _await_cleanup_task(task)


__all__ = [
    "TRACEH_PLUGIN_API_VERSION",
    "PluginActivationSet",
    "PluginGenerationBuilder",
    "PluginManager",
    "PluginNotice",
    "PluginStatus",
    "validate_manifest",
]
