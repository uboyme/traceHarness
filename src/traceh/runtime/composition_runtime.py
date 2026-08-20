"""Generation-backed, step-scoped composition leases.

The runtime owns one current generation at a time.  A lease increments that
generation's reference count at the publication boundary and keeps the exact
generation record until its context exits.  Publication never mutates a
generation: it retires the old record and installs a new one atomically.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from dataclasses import InitVar, dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from traceh.api.llm import LlmProvider, ToolSchema
from traceh.api.plugins import CORE_PLUGIN_IDENTITY, PluginIdentity
from traceh.api.prompts import PromptSection
from traceh.concurrency import await_worker_convergence
from traceh.kernel.composition import CompositionSnapshot, RuntimeComposition
from traceh.kernel.registry import ServiceView
from traceh.kernel.scope import Scope
from traceh.llm.registry import LlmRegistry
from traceh.plugins.errors import PluginDisposeError
from traceh.runtime.prompt import PromptAssembler
from traceh.runtime.verification import CompletionVerifier
from traceh.session.service import SessionService
from traceh.tools.registry import ToolRegistry
from traceh.tools.runtime import ToolRuntime

CleanupCallback = Callable[[], Awaitable[None]]
_RESOURCE_BINDING_ATTRIBUTE = "_composition_resource_binding"
_RESOURCE_BINDING_MISSING = object()


@dataclass(slots=True)
class _GenerationPublicationState:
    claimed_by: object | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def owner(self) -> object | None:
        with self._lock:
            return self.claimed_by

    def claim(
        self,
        owner: object,
        resource_owner: CompositionResourceOwner | None,
        activation_set: object | None = None,
    ) -> None:
        """Atomically claim the generation and its optional cleanup owner.

        The claim is synchronous and never awaits.  Keeping the two claims
        under the generation's private lock prevents one runtime from
        observing a half-claimed generation while another runtime is
        publishing it.
        """

        with self._lock:
            if self.claimed_by is not None:
                raise ValueError("generation is already bound to a runtime")
            if resource_owner is not None:
                resource_owner._ensure_claimable(owner)
            if activation_set is not None:
                ensure_claimable = getattr(activation_set, "_ensure_claimable", None)
                if not callable(ensure_claimable):
                    raise TypeError("generation activation set has no ownership contract")
                ensure_claimable(owner)
            resource_claimed = False
            activation_claimed = False
            try:
                if resource_owner is not None:
                    resource_owner._claim(owner)
                    resource_claimed = True
                if activation_set is not None:
                    activation_set._claim_for_generation(owner)
                    activation_claimed = True
                self.claimed_by = owner
            except BaseException:
                if activation_claimed:
                    activation_set._unclaim_for_generation(owner)
                if resource_claimed:
                    resource_owner._unclaim(owner)
                raise


@dataclass(slots=True, weakref_slot=True)
class _CompositionResourceBinding:
    """Local resource lineage propagated by the composition assembly.

    This object is deliberately attached to the assembled capability objects
    and is never looked up through a process-wide identity catalog.  A
    frozen/re-wrapped capability carries the same binding; a new raw
    capability assembly gets a new binding.
    """

    owner: CompositionResourceOwner | None = None
    used: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def commit(
        self,
        components: tuple[object, ...],
        owner: CompositionResourceOwner | None,
    ) -> None:
        """Commit one assembled binding after every fallible freeze operation.

        A Generation constructor is a transaction: it must not mark raw
        capabilities as used until provider lookup and every frozen projection
        has succeeded.  Cleanup-bearing assemblies additionally require every
        raw component without an existing binding to have writable binding
        storage.  A slotted third-party object cannot prove that it is the
        same resource when it is placed in a new container, so accepting it
        would make cleanup ownership unsound; such assemblies are rejected.
        """

        with self._lock:
            previous_owner = self.owner
            previous_used = self.used
            if owner is not None and self.owner is not None and self.owner is not owner:
                raise ValueError(
                    "resource capabilities already belong to another cleanup owner"
                )
            if owner is not None and self.used:
                raise ValueError(
                    "cleanup ownership must be created with fresh capabilities"
                )
            missing = tuple(
                component
                for component in components
                if _binding_for(component) is None
            )
            if owner is not None:
                unsupported = tuple(
                    component
                    for component in missing
                    if not _binding_storage_available(component)
                )
                if unsupported:
                    raise ValueError(
                        "cleanup ownership requires binding-capable raw capabilities"
                    )

            previous_states = tuple(
                (component, _binding_attribute_state(component))
                for component in missing
            )
            try:
                if owner is not None:
                    self.owner = owner
                for component in missing:
                    _set_binding(
                        component,
                        self,
                        require_storage=owner is not None,
                    )
                self.used = True
            except BaseException:
                rollback_error: BaseException | None = None
                for component, previous_state in reversed(previous_states):
                    try:
                        _restore_binding_state(component, previous_state)
                    except BaseException as restore_error:
                        if rollback_error is None:
                            rollback_error = restore_error
                self.owner = previous_owner
                self.used = previous_used
                if rollback_error is not None:
                    raise RuntimeError(
                        "composition resource binding rollback failed"
                    ) from rollback_error
                raise


class CompositionResourceOwner:
    """One-shot lifecycle ownership for a Generation's external resources.

    A cleanup callback is intentionally not accepted directly by
    ``CompositionGeneration``.  The controlled runtime factory creates this
    handle and a candidate must carry the same explicit handle through every
    freeze or wrapper operation.  The handle can be claimed by one runtime
    only, so cleanup cannot be attached to the same resource lineage twice.
    """

    __slots__ = ("_cleanup", "_claimed_by", "_lock")

    def __init__(self, cleanup: CleanupCallback) -> None:
        if not callable(cleanup):
            raise TypeError("composition resource cleanup must be callable")
        self._cleanup = cleanup
        self._claimed_by: object | None = None
        self._lock = threading.Lock()

    @property
    def cleanup(self) -> CleanupCallback:
        return self._cleanup

    @property
    def claimed_by(self) -> object | None:
        with self._lock:
            return self._claimed_by

    def _claim(self, runtime_owner: object) -> None:
        with self._lock:
            if self._claimed_by is not None:
                raise ValueError("composition resource owner is already bound to a runtime")
            self._claimed_by = runtime_owner

    def _ensure_claimable(self, runtime_owner: object) -> None:
        del runtime_owner
        with self._lock:
            if self._claimed_by is not None:
                raise ValueError("composition resource owner is already bound to a runtime")

    def _unclaim(self, runtime_owner: object) -> None:
        with self._lock:
            if self._claimed_by is runtime_owner:
                self._claimed_by = None


def _composition_components(
    llms: object,
    tools: object,
    prompt: object,
    verifier: object | None = None,
) -> tuple[object, ...]:
    """Return the explicitly assembled capability components.

    This is an assembly boundary, not an object-graph traversal.  It only
    visits the registries, policies, middleware and entries that form the
    Composition contract.  Wrapper classes propagate the binding directly;
    there is no global ``id()``/weak-reference catalog to retain or reuse.
    """

    components: list[object] = [llms, tools, prompt]
    registry = getattr(tools, "registry", None)
    if registry is not None:
        components.append(registry)
        for name in registry.names():
            components.append(registry.require(name))
    for policy in getattr(tools, "policies", ()):
        components.append(policy)
    for middleware in getattr(tools, "middlewares", ()):
        components.append(middleware)
    for name in llms.names():
        components.append(llms.require(name))
    if verifier is not None:
        components.append(verifier)
    return tuple(components)


def _binding_dict(component: object) -> dict[str, object] | None:
    try:
        namespace = object.__getattribute__(component, "__dict__")
    except AttributeError:
        return None
    return namespace if isinstance(namespace, dict) else None


def _binding_slot_available(component: object) -> bool:
    for component_type in type(component).__mro__:
        slots = component_type.__dict__.get("__slots__", ())
        if isinstance(slots, str):
            slots = (slots,)
        if _RESOURCE_BINDING_ATTRIBUTE in slots:
            return True
    return False


def _binding_attribute_state(component: object) -> object:
    namespace = _binding_dict(component)
    if namespace is not None and _RESOURCE_BINDING_ATTRIBUTE in namespace:
        return namespace[_RESOURCE_BINDING_ATTRIBUTE]
    if _binding_slot_available(component):
        try:
            return object.__getattribute__(component, _RESOURCE_BINDING_ATTRIBUTE)
        except AttributeError:
            pass
    return _RESOURCE_BINDING_MISSING


def _binding_for(component: object) -> _CompositionResourceBinding | None:
    binding = _binding_attribute_state(component)
    if binding is _RESOURCE_BINDING_MISSING or binding is None:
        return None
    if not isinstance(binding, _CompositionResourceBinding):
        raise ValueError("composition capability has an invalid resource binding")
    return binding


def _binding_storage_available(component: object) -> bool:
    """Whether ``component`` can retain an explicit binding marker."""

    return _binding_dict(component) is not None or _binding_slot_available(component)


def _write_binding_attribute(component: object, value: object) -> None:
    namespace = _binding_dict(component)
    if namespace is not None:
        namespace[_RESOURCE_BINDING_ATTRIBUTE] = value
        return
    if _binding_slot_available(component):
        object.__setattr__(component, _RESOURCE_BINDING_ATTRIBUTE, value)
        return
    raise AttributeError("composition capability has no resource binding storage")


def _delete_binding_attribute(component: object) -> None:
    namespace = _binding_dict(component)
    if namespace is not None:
        namespace.pop(_RESOURCE_BINDING_ATTRIBUTE, None)
        return
    if _binding_slot_available(component):
        try:
            object.__delattr__(component, _RESOURCE_BINDING_ATTRIBUTE)
        except AttributeError:
            pass


def _restore_binding_state(component: object, previous_state: object) -> None:
    if previous_state is _RESOURCE_BINDING_MISSING:
        _delete_binding_attribute(component)
    else:
        _write_binding_attribute(component, previous_state)
    if _binding_attribute_state(component) is not previous_state:
        raise RuntimeError("composition capability binding state was not restored")


def _set_binding(
    component: object,
    binding: _CompositionResourceBinding,
    *,
    require_storage: bool = False,
) -> None:
    current = _binding_for(component)
    if current is not None and current is not binding:
        raise ValueError(
            "composition components belong to different resource lineages"
        )
    if current is binding:
        return
    if not _binding_storage_available(component):
        if require_storage:
            raise ValueError(
                "cleanup ownership requires binding-capable raw capabilities"
            )
        return
    try:
        _write_binding_attribute(component, binding)
    except (AttributeError, TypeError) as error:
        if require_storage:
            raise ValueError(
                "cleanup ownership requires binding-capable raw capabilities"
            ) from error
        return
    if _binding_for(component) is not binding:
        if require_storage:
            raise ValueError(
                "cleanup ownership requires binding-capable raw capabilities"
            )
        _restore_binding_state(component, _RESOURCE_BINDING_MISSING)


def _binding_for_components(
    components: tuple[object, ...],
) -> _CompositionResourceBinding | None:
    binding: _CompositionResourceBinding | None = None
    for component in components:
        candidate = _binding_for(component)
        if candidate is None:
            continue
        if binding is not None and binding is not candidate:
            raise ValueError(
                "composition components belong to different resource lineages"
            )
        binding = candidate
    return binding


def _safe_error_type(error: BaseException) -> str:
    """Return a terminal-safe, deterministic exception type label."""

    allowed = frozenset(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    )
    sanitized = "".join(char if char in allowed else "_" for char in type(error).__name__)
    return sanitized[:128] or "UnknownError"


class _FrozenPromptAssembler:
    """Immutable prompt source owned by one CompositionGeneration."""

    __slots__ = (
        "_sections",
        "_publication_state",
        "_composition_resource_binding",
    )

    def __init__(
        self,
        sections: tuple[PromptSection, ...],
        *,
        publication_state: _GenerationPublicationState | None = None,
        resource_binding: _CompositionResourceBinding | None = None,
    ) -> None:
        self._sections = tuple(
            sorted(sections, key=lambda item: (item.priority, item.section_id))
        )
        self._publication_state = publication_state
        self._composition_resource_binding = resource_binding

    def section_ids(self) -> tuple[str, ...]:
        return tuple(sorted(section.section_id for section in self._sections))

    def sections(self) -> tuple[PromptSection, ...]:
        return self._sections

    def assemble(self, *, workspace: str) -> str:
        runtime_section = PromptSection(
            "traceh.runtime.workspace",
            f"Workspace root: {workspace}\n"
            "All file and process operations must stay in this workspace.",
            50,
        )
        sections = sorted(
            (*self._sections, runtime_section),
            key=lambda item: (item.priority, item.section_id),
        )
        return "\n\n".join(
            f"## {section.section_id}\n{section.content.strip()}" for section in sections
        )


class _FrozenLlmRegistry:
    """Read-only provider lookup captured at generation construction."""

    __slots__ = (
        "_providers",
        "_publication_state",
        "_composition_resource_binding",
    )

    def __init__(
        self,
        providers: dict[str, LlmProvider],
        *,
        publication_state: _GenerationPublicationState | None = None,
        resource_binding: _CompositionResourceBinding | None = None,
    ) -> None:
        self._providers = MappingProxyType(dict(providers))
        self._publication_state = publication_state
        self._composition_resource_binding = resource_binding

    @classmethod
    def from_registry(
        cls,
        registry: LlmRegistry,
        *,
        publication_state: _GenerationPublicationState | None = None,
        resource_binding: _CompositionResourceBinding | None = None,
    ) -> _FrozenLlmRegistry:
        return cls(
            {
                name: _FrozenProvider.freeze(
                    registry.require(name),
                    name=name,
                    resource_binding=resource_binding,
                )
                for name in registry.names()
            },
            publication_state=publication_state,
            resource_binding=resource_binding,
        )

    def get(self, name: str) -> LlmProvider | None:
        return self._providers.get(name)

    def require(self, name: str) -> LlmProvider:
        provider = self.get(name)
        if provider is None:
            raise LookupError(f"unknown LLM provider: {name}")
        return provider

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))


class _FrozenProvider:
    """Stable provider identity with execution delegated to its source."""

    __slots__ = ("_delegate", "_name", "_composition_resource_binding")

    def __init__(
        self,
        provider: LlmProvider,
        *,
        name: str | None = None,
        resource_binding: _CompositionResourceBinding | None = None,
    ) -> None:
        object.__setattr__(self, "_delegate", provider)
        object.__setattr__(self, "_name", provider.name if name is None else name)
        object.__setattr__(self, "_composition_resource_binding", resource_binding)

    @classmethod
    def freeze(
        cls,
        provider: LlmProvider,
        *,
        name: str | None = None,
        resource_binding: _CompositionResourceBinding | None = None,
    ) -> _FrozenProvider:
        if isinstance(provider, _FrozenProvider):
            if name is not None and provider.name != name:
                raise ValueError("frozen provider identity does not match registry key")
            return provider
        return cls(provider, name=name, resource_binding=resource_binding)

    @property
    def name(self) -> str:
        return self._name

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("frozen composition provider")

    def __delattr__(self, name: str) -> None:
        del name
        raise AttributeError("frozen composition provider")

    async def complete(self, request):
        return await self._delegate.complete(request)


class _FrozenPolicy:
    """Stable policy metadata with checks delegated to the source policy."""

    __slots__ = ("_delegate", "_name", "_composition_resource_binding")

    def __init__(
        self,
        policy: object,
        *,
        name: str | None = None,
        resource_binding: _CompositionResourceBinding | None = None,
    ) -> None:
        object.__setattr__(self, "_delegate", policy)
        object.__setattr__(self, "_name", policy.name if name is None else name)
        object.__setattr__(self, "_composition_resource_binding", resource_binding)

    @classmethod
    def freeze(
        cls,
        policy: object,
        *,
        name: str | None = None,
        resource_binding: _CompositionResourceBinding | None = None,
    ) -> _FrozenPolicy:
        if isinstance(policy, _FrozenPolicy):
            if name is not None and policy.name != name:
                raise ValueError("frozen policy identity does not match candidate")
            return policy
        return cls(policy, name=name, resource_binding=resource_binding)

    @property
    def name(self) -> str:
        return self._name

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("frozen composition policy")

    def __delattr__(self, name: str) -> None:
        del name
        raise AttributeError("frozen composition policy")

    async def check(self, call, tool, context):
        return await self._delegate.check(call, tool, context)


class _FrozenMiddleware:
    """Stable middleware metadata with invocation delegated to the source."""

    __slots__ = ("_delegate", "_name", "_composition_resource_binding")

    def __init__(
        self,
        middleware: object,
        *,
        name: str | None = None,
        resource_binding: _CompositionResourceBinding | None = None,
    ) -> None:
        object.__setattr__(self, "_delegate", middleware)
        object.__setattr__(
            self,
            "_name",
            middleware.name if name is None else name,
        )
        object.__setattr__(self, "_composition_resource_binding", resource_binding)

    @classmethod
    def freeze(
        cls,
        middleware: object,
        *,
        name: str | None = None,
        resource_binding: _CompositionResourceBinding | None = None,
    ) -> _FrozenMiddleware:
        if isinstance(middleware, _FrozenMiddleware):
            if name is not None and middleware.name != name:
                raise ValueError("frozen middleware identity does not match candidate")
            return middleware
        return cls(middleware, name=name, resource_binding=resource_binding)

    @property
    def name(self) -> str:
        return self._name

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("frozen composition middleware")

    def __delattr__(self, name: str) -> None:
        del name
        raise AttributeError("frozen composition middleware")

    async def invoke(self, invocation, call_next):
        return await self._delegate.invoke(invocation, call_next)


class _CompatibilityPromptAssembler(_FrozenPromptAssembler):
    """Mutable v0.4 inspection view, never exposed by a Generation."""

    def sync_from(self, source: PromptAssembler) -> None:
        self._sections = tuple(
            sorted(source.sections(), key=lambda item: (item.priority, item.section_id))
        )

    def clear(self) -> None:
        self._sections = ()


class _CompatibilityLlmRegistry(_FrozenLlmRegistry):
    """Mutable v0.4 provider view, never exposed by a Generation."""

    @classmethod
    def from_registry(
        cls,
        registry: LlmRegistry,
        *,
        publication_state: _GenerationPublicationState | None = None,
        resource_binding: _CompositionResourceBinding | None = None,
    ) -> _CompatibilityLlmRegistry:
        # The compatibility surface intentionally preserves the v0.4 provider
        # object type.  Generation leases use _FrozenProvider; inspection and
        # legacy callers continue to see the startup provider instance.
        return cls(
            {name: registry.require(name) for name in registry.names()},
            publication_state=publication_state,
            resource_binding=resource_binding,
        )

    @classmethod
    def from_frozen_registry(
        cls,
        registry: _FrozenLlmRegistry,
        *,
        publication_state: _GenerationPublicationState | None = None,
        resource_binding: _CompositionResourceBinding | None = None,
    ) -> _CompatibilityLlmRegistry:
        """Preserve v0.4 provider types without re-reading a raw registry."""

        providers: dict[str, LlmProvider] = {}
        for name in registry.names():
            provider = registry.require(name)
            if not isinstance(provider, _FrozenProvider):
                raise RuntimeError("frozen LLM registry contains an invalid provider")
            providers[name] = provider._delegate
        return cls(
            providers,
            publication_state=publication_state,
            resource_binding=resource_binding,
        )

    def clear(self) -> None:
        self._providers = MappingProxyType({})


class _FrozenJsonDict(dict):
    """A dict-shaped JSON value whose mutation methods all fail."""

    def __init__(self, values: dict[str, object]) -> None:
        dict.__init__(self, values)

    def __setitem__(self, key, value) -> None:
        del key, value
        raise TypeError("frozen composition value")

    def __delitem__(self, key) -> None:
        del key
        raise TypeError("frozen composition value")

    def clear(self) -> None:
        raise TypeError("frozen composition value")

    def pop(self, key, default=None):
        del key, default
        raise TypeError("frozen composition value")

    def popitem(self):
        raise TypeError("frozen composition value")

    def setdefault(self, key, default=None):
        del key, default
        raise TypeError("frozen composition value")

    def update(self, *args, **kwargs) -> None:
        del args, kwargs
        raise TypeError("frozen composition value")

    def __ior__(self, value):
        del value
        raise TypeError("frozen composition value")


class _FrozenJsonList(list):
    """A list-shaped JSON value whose mutation methods all fail."""

    def __init__(self, values: list[object]) -> None:
        list.__init__(self, values)

    def __setitem__(self, key, value) -> None:
        del key, value
        raise TypeError("frozen composition value")

    def __delitem__(self, key) -> None:
        del key
        raise TypeError("frozen composition value")

    def append(self, value) -> None:
        del value
        raise TypeError("frozen composition value")

    def clear(self) -> None:
        raise TypeError("frozen composition value")

    def extend(self, values) -> None:
        del values
        raise TypeError("frozen composition value")

    def insert(self, index, value) -> None:
        del index, value
        raise TypeError("frozen composition value")

    def pop(self, index=-1):
        del index
        raise TypeError("frozen composition value")

    def remove(self, value) -> None:
        del value
        raise TypeError("frozen composition value")

    def reverse(self) -> None:
        raise TypeError("frozen composition value")

    def sort(self, *args, **kwargs) -> None:
        del args, kwargs
        raise TypeError("frozen composition value")

    def __iadd__(self, value):
        del value
        raise TypeError("frozen composition value")

    def __imul__(self, value):
        del value
        raise TypeError("frozen composition value")


def _freeze_json(value: object) -> object:
    if isinstance(value, dict):
        return _FrozenJsonDict(
            {str(key): _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return _FrozenJsonList([_freeze_json(item) for item in value])
    return value


class _FrozenTool:
    """Stable tool metadata with execution delegated to the captured tool."""

    __slots__ = (
        "_delegate",
        "_name",
        "_description",
        "_input_schema",
        "_effect_kind",
        "_composition_resource_binding",
    )

    def __init__(
        self,
        tool: object,
        *,
        name: str | None = None,
        resource_binding: _CompositionResourceBinding | None = None,
    ) -> None:
        object.__setattr__(self, "_delegate", tool)
        object.__setattr__(self, "_name", tool.name if name is None else name)
        object.__setattr__(self, "_description", tool.description)
        object.__setattr__(self, "_input_schema", _freeze_json(tool.input_schema))
        object.__setattr__(self, "_effect_kind", tool.effect_kind)
        object.__setattr__(self, "_composition_resource_binding", resource_binding)

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def input_schema(self) -> dict[str, object]:
        return self._input_schema

    @property
    def effect_kind(self) -> object:
        return self._effect_kind

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("frozen composition tool")

    def __delattr__(self, name: str) -> None:
        del name
        raise AttributeError("frozen composition tool")

    @classmethod
    def freeze(
        cls,
        tool: object,
        *,
        name: str | None = None,
        resource_binding: _CompositionResourceBinding | None = None,
    ) -> _FrozenTool:
        """Return one flat adapter even when a source is already frozen."""

        if isinstance(tool, _FrozenTool):
            if name is not None and tool.name != name:
                raise ValueError("frozen tool identity does not match registry key")
            return tool
        return cls(
            tool,
            name=name,
            resource_binding=resource_binding,
        )

    async def execute(self, arguments, context):
        return await self._delegate.execute(arguments, context)


class _FrozenToolRegistry:
    """Read-only tool lookup and schema projection captured per generation."""

    __slots__ = ("_tools", "_schemas", "_composition_resource_binding")

    def __init__(
        self,
        tools: dict[str, object],
        schemas: tuple[ToolSchema, ...],
        *,
        resource_binding: _CompositionResourceBinding | None = None,
    ) -> None:
        self._tools = MappingProxyType(dict(tools))
        self._schemas = schemas
        self._composition_resource_binding = resource_binding

    @classmethod
    def from_registry(
        cls,
        registry: ToolRegistry,
        *,
        resource_binding: _CompositionResourceBinding | None = None,
    ) -> _FrozenToolRegistry:
        tools = {
            name: _FrozenTool.freeze(
                registry.require(name),
                name=name,
                resource_binding=resource_binding,
            )
            for name in registry.names()
        }
        schemas = tuple(
            ToolSchema(name, tools[name].description, tools[name].input_schema)
            for name in sorted(tools)
        )
        return cls(
            tools,
            schemas,
            resource_binding=resource_binding,
        )

    def get(self, name: str) -> object | None:
        return self._tools.get(name)

    def require(self, name: str) -> object:
        tool = self.get(name)
        if tool is None:
            raise LookupError(f"unknown tool: {name}")
        return tool

    def schemas(self) -> tuple[ToolSchema, ...]:
        return tuple(self._schemas)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))


class _FrozenToolRuntime:
    """Read-only ToolRuntime projection backed by a frozen registry."""

    __slots__ = (
        "_delegate",
        "_registry",
        "_publication_state",
        "_composition_resource_binding",
    )

    def __init__(
        self,
        source: ToolRuntime,
        *,
        registry_type: type[_FrozenToolRegistry] = _FrozenToolRegistry,
        policy_names: tuple[str, ...] | None = None,
        middleware_names: tuple[str, ...] | None = None,
        publication_state: _GenerationPublicationState | None = None,
        resource_binding: _CompositionResourceBinding | None = None,
    ) -> None:
        registry = registry_type.from_registry(
            source.registry,
            resource_binding=resource_binding,
        )
        self._registry = registry
        self._publication_state = publication_state
        self._composition_resource_binding = resource_binding
        if policy_names is None:
            policy_names = tuple(policy.name for policy in source.policies)
        if middleware_names is None:
            middleware_names = tuple(
                middleware.name for middleware in source.middlewares
            )
        if len(policy_names) != len(source.policies):
            raise ValueError("frozen policy identity count does not match candidate")
        if len(middleware_names) != len(source.middlewares):
            raise ValueError("frozen middleware identity count does not match candidate")
        frozen_policies = tuple(
            _FrozenPolicy.freeze(
                policy,
                name=name,
                resource_binding=resource_binding,
            )
            for policy, name in zip(source.policies, policy_names, strict=True)
        )
        frozen_middlewares = tuple(
            _FrozenMiddleware.freeze(
                middleware,
                name=name,
                resource_binding=resource_binding,
            )
            for middleware, name in zip(
                source.middlewares,
                middleware_names,
                strict=True,
            )
        )
        self._delegate = ToolRuntime(
            registry,
            source.sessions,
            policies=frozen_policies,
            middlewares=frozen_middlewares,
            timeout_seconds=source.timeout_seconds,
            max_output_chars=source.max_output_chars,
        )

    @property
    def registry(self) -> _FrozenToolRegistry:
        return self._registry

    @property
    def sessions(self):
        return self._delegate.sessions

    @property
    def policies(self):
        return self._delegate.policies

    @property
    def middlewares(self):
        return self._delegate.middlewares

    @property
    def timeout_seconds(self) -> float:
        return self._delegate.timeout_seconds

    @property
    def max_output_chars(self) -> int:
        return self._delegate.max_output_chars

    async def execute_batch(self, *args, **kwargs):
        return await self._delegate.execute_batch(*args, **kwargs)


class _CompatibilityToolRegistry(_FrozenToolRegistry):
    """Mutable v0.4 tool view, never exposed by a Generation."""

    def sync_from(self, source: ToolRegistry) -> None:
        refreshed = type(self).from_registry(
            source,
            resource_binding=self._composition_resource_binding,
        )
        self._tools = refreshed._tools
        self._schemas = refreshed._schemas

    def clear(self) -> None:
        self._tools = MappingProxyType({})
        self._schemas = ()


class _CompatibilityToolRuntime(_FrozenToolRuntime):
    """Mutable v0.4 tool view, never exposed by a Generation."""

    def __init__(
        self,
        source: ToolRuntime,
        *,
        publication_state: _GenerationPublicationState | None = None,
        resource_binding: _CompositionResourceBinding | None = None,
    ) -> None:
        super().__init__(
            source,
            registry_type=_CompatibilityToolRegistry,
            publication_state=publication_state,
            resource_binding=resource_binding,
        )

    def sync_from(self, source: ToolRuntime) -> None:
        self._registry.sync_from(source.registry)

    def clear(self) -> None:
        self._registry.clear()


@dataclass(frozen=True, slots=True)
class CompositionGeneration:
    """The immutable runtime capabilities belonging to one generation.

    ``generation_id`` is deliberately not stored here.  It is assigned by the
    runtime for lifecycle bookkeeping and never enters a model request or a
    persisted Composition Snapshot.  The provider object is resolved once at
    construction so a lease cannot observe a different Provider after it has
    been acquired.  External cleanup is carried by an explicit one-shot
    ``CompositionResourceOwner`` handle rather than by a naked callback.
    """

    llms: LlmRegistry
    tools: ToolRuntime
    prompt: PromptAssembler
    provider: str
    model: str
    temperature: float | None = None
    max_output_tokens: int | None = None
    plugins: tuple[PluginIdentity, ...] = (CORE_PLUGIN_IDENTITY,)
    verifier: CompletionVerifier | None = field(default=None, compare=False, repr=False)
    resource_owner: CompositionResourceOwner | None = field(
        default=None, compare=False, repr=False
    )
    # Stage B ownership is explicit at the ActivationSet boundary.  Its
    # registries contain borrowed core entries plus generation-owned plugin
    # registrations; the set, rather than a capability-wide cleanup owner,
    # closes the latter.  Keeping this separate lets generations share core
    # Provider/Tool/Session resources without pretending that a plugin set
    # owns them.
    activation_set: Any | None = field(default=None, compare=False, repr=False)
    # Compatibility input only: InitVar ensures the callback is validated
    # against the handle but is never stored on the Generation itself.
    cleanup: InitVar[CleanupCallback | None] = None
    _provider: LlmProvider = field(init=False, compare=False, repr=False)
    _tool_schemas: tuple[ToolSchema, ...] = field(init=False, compare=False, repr=False)
    _policy_names: tuple[str, ...] = field(init=False, compare=False, repr=False)
    _middleware_names: tuple[str, ...] = field(init=False, compare=False, repr=False)
    _prompt_sections: tuple[PromptSection, ...] = field(
        init=False, compare=False, repr=False
    )
    _scope: Scope | None = field(init=False, compare=False, repr=False)
    _services: ServiceView | None = field(init=False, compare=False, repr=False)
    _publication_state: _GenerationPublicationState = field(
        init=False, compare=False, repr=False
    )
    _resource_binding: _CompositionResourceBinding = field(
        init=False, compare=False, repr=False
    )

    def __post_init__(self, cleanup: CleanupCallback | None) -> None:
        activation_policy_names: tuple[str, ...] | None = None
        activation_middleware_names: tuple[str, ...] | None = None
        if cleanup is not None:
            if self.resource_owner is None:
                raise ValueError(
                    "generation cleanup requires an explicit CompositionResourceOwner"
                )
            if self.resource_owner.cleanup is not cleanup:
                raise ValueError(
                    "generation cleanup must be bound to its resource owner"
                )

        if self.activation_set is not None and self.resource_owner is not None:
            raise ValueError(
                "a Generation cannot combine an ActivationSet with a resource owner"
            )

        if self.activation_set is not None and not callable(
            getattr(self.activation_set, "dispose", None)
        ):
            raise TypeError("generation activation_set must provide dispose()")
        if self.activation_set is not None:
            activation_identities = getattr(self.activation_set, "identities", None)
            if activation_identities is not None and tuple(self.plugins) != tuple(
                activation_identities
            ):
                raise ValueError(
                    "generation plugin identities must match its ActivationSet"
                )
            activation_tools = getattr(self.activation_set, "tools", _RESOURCE_BINDING_MISSING)
            activation_prompt = getattr(
                self.activation_set, "prompt", _RESOURCE_BINDING_MISSING
            )
            if activation_tools is not _RESOURCE_BINDING_MISSING and (
                self.tools.registry is not activation_tools
            ):
                raise ValueError(
                    "generation ToolRuntime must use its ActivationSet registry"
                )
            if (
                activation_prompt is not _RESOURCE_BINDING_MISSING
                and self.prompt is not activation_prompt
            ):
                raise ValueError(
                    "generation PromptAssembler must use its ActivationSet registry"
                )
            activation_policies = getattr(
                self.activation_set,
                "policies",
                _RESOURCE_BINDING_MISSING,
            )
            if activation_policies is not _RESOURCE_BINDING_MISSING:
                runtime_policies = tuple(self.tools.policies)
                candidate_policies = tuple(activation_policies)
                if len(runtime_policies) != len(candidate_policies) or any(
                    runtime_policy is not candidate_policy
                    for runtime_policy, candidate_policy in zip(
                        runtime_policies,
                        candidate_policies,
                        strict=True,
                    )
                ):
                    # Policy is executable admission behavior. Equality is not
                    # an ownership or identity boundary: a caller-controlled
                    # __eq__ could claim that two behaviorally different
                    # policies are the same candidate capability.
                    raise ValueError(
                        "generation ToolRuntime must use its ActivationSet policies"
                    )
            activation_middlewares = getattr(
                self.activation_set,
                "middlewares",
                _RESOURCE_BINDING_MISSING,
            )
            if activation_middlewares is not _RESOURCE_BINDING_MISSING:
                runtime_middlewares = tuple(self.tools.middlewares)
                candidate_middlewares = tuple(activation_middlewares)
                if len(runtime_middlewares) != len(candidate_middlewares) or any(
                    runtime_middleware is not candidate_middleware
                    for runtime_middleware, candidate_middleware in zip(
                        runtime_middlewares,
                        candidate_middlewares,
                        strict=True,
                    )
                ):
                    raise ValueError(
                        "generation ToolRuntime must use its ActivationSet middleware"
                    )
            activation_verifier = getattr(
                self.activation_set,
                "verifier",
                _RESOURCE_BINDING_MISSING,
            )
            if (
                activation_verifier is not _RESOURCE_BINDING_MISSING
                and self.verifier is not activation_verifier
            ):
                raise ValueError(
                    "generation verifier must use its ActivationSet verifier"
                )
            activation_llms = getattr(
                self.activation_set,
                "llms",
                _RESOURCE_BINDING_MISSING,
            )
            if (
                activation_llms is not _RESOURCE_BINDING_MISSING
                and activation_llms is not None
            ):
                activation_provider = activation_llms.get(self.provider)
                runtime_provider = self.llms.get(self.provider)
                if (
                    activation_provider is None
                    or runtime_provider is not activation_provider
                ):
                    raise ValueError(
                        "generation provider must use its ActivationSet provider"
                    )
            activation_scope = getattr(
                self.activation_set, "scope", _RESOURCE_BINDING_MISSING
            )
            activation_services = getattr(
                self.activation_set, "services", _RESOURCE_BINDING_MISSING
            )
            if (
                activation_scope is _RESOURCE_BINDING_MISSING
                and activation_services is _RESOURCE_BINDING_MISSING
            ):
                # D0's replaceable ActivationSet ownership protocol remains
                # valid. Scope is an optional D1 capability, not a new
                # requirement imposed on every custom ActivationSet.
                activation_scope = None
                activation_services = None
            elif not isinstance(activation_scope, Scope) or not isinstance(
                activation_services, ServiceView
            ):
                raise TypeError(
                    "generation ActivationSet scope and services must be provided together"
                )
            elif activation_scope.services is not activation_services:
                raise ValueError(
                    "generation ActivationSet service view must belong to its scope"
                )
            verify_capabilities = getattr(
                self.activation_set,
                "_verify_generation_capabilities",
                None,
            )
            if callable(verify_capabilities):
                (
                    activation_policy_names,
                    activation_middleware_names,
                ) = verify_capabilities(
                    tools=self.tools.registry,
                    prompt=self.prompt,
                    policies=tuple(self.tools.policies),
                    middlewares=tuple(self.tools.middlewares),
                    verifier=self.verifier,
                )
        else:
            activation_scope = None
            activation_services = None

        # An ActivationSet is the Stage B ownership boundary.  Its candidate
        # registries may intentionally contain borrowed core objects, so do
        # not infer a capability-wide cleanup lineage from the object graph.
        # Stage A's explicit CompositionResourceOwner path retains its strict
        # one-shot capability binding rules for callers that use it directly.
        if self.activation_set is not None and self.resource_owner is None:
            components: tuple[object, ...] = ()
            resource_binding: _CompositionResourceBinding | None = None
        else:
            components = _composition_components(
                self.llms,
                self.tools,
                self.prompt,
                self.verifier,
            )
            resource_binding = _binding_for_components(components)
            if resource_binding is None:
                resource_binding = _CompositionResourceBinding()
            elif (
                resource_binding.owner is not None
                and self.resource_owner is not None
                and resource_binding.owner is not self.resource_owner
            ):
                raise ValueError(
                    "resource capabilities already belong to another cleanup owner"
                )
            elif resource_binding.used and (
                self.resource_owner is not None or resource_binding.owner is not None
            ):
                raise ValueError(
                    "cleanup ownership cannot be derived from existing capabilities"
                )
        publication_state = _GenerationPublicationState()
        frozen_llms = _FrozenLlmRegistry.from_registry(
            self.llms,
            publication_state=publication_state,
            resource_binding=resource_binding,
        )
        frozen_tools = _FrozenToolRuntime(
            self.tools,
            policy_names=activation_policy_names,
            middleware_names=activation_middleware_names,
            publication_state=publication_state,
            resource_binding=resource_binding,
        )
        frozen_prompt = _FrozenPromptAssembler(
            self.prompt.sections(),
            publication_state=publication_state,
            resource_binding=resource_binding,
        )
        provider_instance = frozen_llms.require(self.provider)
        tool_schemas = frozen_tools.registry.schemas()
        prompt_sections = frozen_prompt.sections()
        policy_names = tuple(policy.name for policy in frozen_tools.policies)
        middleware_names = tuple(
            middleware.name for middleware in frozen_tools.middlewares
        )
        # Commit ownership only after every fallible provider lookup and
        # projection has completed.  A bad provider name must leave the raw
        # capabilities and the explicit Owner retryable.
        if components:
            resource_binding.commit(components, self.resource_owner)
        object.__setattr__(self, "llms", frozen_llms)
        object.__setattr__(self, "tools", frozen_tools)
        object.__setattr__(self, "prompt", frozen_prompt)
        object.__setattr__(self, "plugins", tuple(self.plugins))
        if (
            self.resource_owner is None
            and resource_binding is not None
            and resource_binding.owner is not None
        ):
            object.__setattr__(self, "resource_owner", resource_binding.owner)
        object.__setattr__(self, "_publication_state", publication_state)
        object.__setattr__(self, "_resource_binding", resource_binding)
        object.__setattr__(self, "_provider", provider_instance)
        object.__setattr__(self, "_tool_schemas", tool_schemas)
        object.__setattr__(self, "_prompt_sections", prompt_sections)
        object.__setattr__(self, "_policy_names", policy_names)
        object.__setattr__(self, "_middleware_names", middleware_names)
        object.__setattr__(self, "_scope", activation_scope)
        object.__setattr__(self, "_services", activation_services)

    def snapshot(self, *, workspace: Path) -> CompositionSnapshot:
        """Build the model-visible content from this generation only."""

        system_prompt = _FrozenPromptAssembler(self._prompt_sections).assemble(
            workspace=str(workspace)
        )
        return RuntimeComposition(
            provider=self.provider,
            model=self.model,
            system_prompt=system_prompt,
            tools=tuple(
                ToolSchema(
                    schema.name,
                    schema.description,
                    schema.input_schema,
                )
                for schema in self._tool_schemas
            ),
            plugins=self.plugins,
            policies=self._policy_names,
            tool_middlewares=self._middleware_names,
            temperature=self.temperature,
            max_output_tokens=self.max_output_tokens,
        ).snapshot()

    @property
    def provider_instance(self) -> LlmProvider:
        return self._provider

    @property
    def scope(self) -> Scope | None:
        return self._scope

    @property
    def services(self) -> ServiceView | None:
        return self._services

    def _claim_for_runtime(self, owner: object) -> None:
        self._publication_state.claim(owner, self.resource_owner, self.activation_set)

    async def _cleanup_owned_resources(self) -> None:
        errors: list[BaseException] = []
        if self.resource_owner is not None:
            try:
                await self.resource_owner.cleanup()
            except BaseException as error:
                errors.append(error)
        if self.activation_set is not None:
            try:
                dispose_for_generation = getattr(
                    self.activation_set, "_dispose_for_generation", None
                )
                if callable(dispose_for_generation):
                    await dispose_for_generation(self._publication_state.owner)
                else:
                    await self.activation_set.dispose()
            except BaseException as error:
                # ActivationSet has already attempted every plugin in reverse
                # order and exposes only a bounded structured error.
                errors.append(error)
        if errors:
            if len(errors) == 1:
                raise errors[0]
            raise ExceptionGroup("generation-owned cleanup failed", errors)


@dataclass(frozen=True, slots=True)
class ActiveComposition:
    snapshot: CompositionSnapshot
    provider: LlmProvider
    tools: ToolRuntime
    generation_id: int = 0
    scope: Scope | None = None
    services: ServiceView | None = None
    verifier: CompletionVerifier | None = None


@dataclass(frozen=True, slots=True)
class GenerationCleanupFailure:
    """A safe, deterministic description of one generation cleanup failure."""

    generation_id: int
    error_type: str

    def to_dict(self) -> dict[str, int | str]:
        return {
            "generation_id": self.generation_id,
            "error_type": self.error_type,
        }


class CompositionDrainError(PluginDisposeError):
    """Raised after every retired generation has attempted cleanup."""

    def __init__(self, failures: tuple[GenerationCleanupFailure, ...]) -> None:
        self.failures = failures
        details = ", ".join(
            f"generation {item.generation_id}: {item.error_type}" for item in failures
        )
        # Keep the Stage A structured GenerationCleanupFailure payload while
        # remaining compatible with v0.4 callers that treated startup plugin
        # teardown failures as PluginDisposeError.
        RuntimeError.__init__(self, f"composition generation cleanup failed: {details}")


class CompositionRuntime(Protocol):
    def lease(
        self,
        *,
        workspace: Path,
        session_id: str,
        turn_id: str,
        step_id: str,
    ) -> CompositionLease:
        ...


class CompositionLease(Protocol):
    async def __aenter__(self) -> ActiveComposition:
        ...

    async def __aexit__(self, exc_type, exc_value, traceback) -> bool | None:
        ...


@dataclass(slots=True)
class _GenerationRecord:
    generation_id: int
    generation: CompositionGeneration
    leases: int = 0
    state: str = "current"
    cleanup_task: asyncio.Task[None] | None = None
    cleanup_failure: GenerationCleanupFailure | None = None


class GenerationCompositionRuntime:
    """Manage the current generation, Step leases, retirement and drain."""

    def __init__(
        self,
        *,
        llms: LlmRegistry,
        tools: ToolRuntime,
        prompt: PromptAssembler,
        provider: str,
        model: str,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        plugins: tuple[PluginIdentity, ...] = (CORE_PLUGIN_IDENTITY,),
        cleanup: CleanupCallback | None = None,
        resource_owner: CompositionResourceOwner | None = None,
        activation_set: Any | None = None,
        verifier: CompletionVerifier | None = None,
        compatibility_tools_source: ToolRuntime | None = None,
        compatibility_prompt_source: PromptAssembler | None = None,
        defer_external_cleanup: bool = False,
    ) -> None:
        self._lock = asyncio.Lock()
        self._changed = asyncio.Event()
        self._next_generation_id = 1
        self._owner_token = object()
        self._session_service: SessionService = tools.sessions
        self._startup_plugins = tuple(plugins)
        self._startup_provider = provider
        self._startup_model = model
        self._startup_temperature = temperature
        self._startup_max_output_tokens = max_output_tokens
        if cleanup is not None:
            if resource_owner is not None and resource_owner.cleanup is not cleanup:
                raise ValueError(
                    "runtime cleanup must be bound to its resource owner"
                )
            resource_owner = resource_owner or CompositionResourceOwner(cleanup)
        initial = CompositionGeneration(
            llms=llms,
            tools=tools,
            prompt=prompt,
            provider=provider,
            model=model,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            plugins=plugins,
            resource_owner=resource_owner,
            activation_set=activation_set,
            verifier=verifier,
            cleanup=cleanup,
        )
        compatibility_llms = _CompatibilityLlmRegistry.from_frozen_registry(
            initial.llms,
            publication_state=initial._publication_state,
            resource_binding=initial._resource_binding,
        )
        compatibility_tools = _CompatibilityToolRuntime(
            initial.tools,
            publication_state=initial._publication_state,
            resource_binding=initial._resource_binding,
        )
        compatibility_prompt = _CompatibilityPromptAssembler(
            initial.prompt.sections(),
            publication_state=initial._publication_state,
            resource_binding=initial._resource_binding,
        )
        # Everything above this point may still project data.  Claim the
        # one-shot owner only after the complete initial runtime view exists;
        # no caller-controlled Provider, Tool or Prompt method runs afterward.
        self._validate_and_claim_generation(initial)
        self._records: dict[int, _GenerationRecord] = {
            1: _GenerationRecord(1, initial)
        }
        self._current: _GenerationRecord | None = self._records[1]
        self._compatibility_llms = compatibility_llms
        self._compatibility_tools = compatibility_tools
        self._compatibility_prompt = compatibility_prompt
        self._source_tools: ToolRuntime | None = compatibility_tools_source or tools
        self._source_prompt: PromptAssembler | None = compatibility_prompt_source or prompt
        self._defer_external_cleanup = defer_external_cleanup
        self._cleanup_failure: GenerationCleanupFailure | None = None
        self._poisoned = False
        self._drain_task: asyncio.Task[None] | None = None
        self._dispose_task: asyncio.Task[None] | None = None
        self._disposed = False

    @property
    def current_generation_id(self) -> int | None:
        return self._current.generation_id if self._current is not None else None

    @property
    def current_generation(self) -> CompositionGeneration:
        if self._current is None:
            raise RuntimeError("composition runtime is disposed")
        return self._current.generation

    # These read-only projections keep the v0.4 assembly inspection surface
    # usable while the lease path itself reads only from a bound generation.
    @property
    def llms(self) -> LlmRegistry:
        return self._compatibility_llms

    @property
    def tools(self) -> ToolRuntime:
        return self._compatibility_tools

    @property
    def prompt(self) -> PromptAssembler:
        return self._compatibility_prompt

    @property
    def provider(self) -> str:
        return (
            self._current.generation.provider
            if self._current is not None
            else self._startup_provider
        )

    @property
    def model(self) -> str:
        return self._current.generation.model if self._current is not None else self._startup_model

    @property
    def temperature(self) -> float | None:
        return (
            self._current.generation.temperature
            if self._current is not None
            else self._startup_temperature
        )

    @property
    def max_output_tokens(self) -> int | None:
        return (
            self._current.generation.max_output_tokens
            if self._current is not None
            else self._startup_max_output_tokens
        )

    @property
    def plugins(self) -> tuple[PluginIdentity, ...]:
        return (
            self._current.generation.plugins
            if self._current is not None
            else self._startup_plugins
        )

    @property
    def current_activation_set(self) -> object | None:
        return self._current.generation.activation_set if self._current is not None else None

    @property
    def generation_states(self) -> tuple[tuple[int, str, int], ...]:
        """Return lifecycle state for deterministic diagnostics and tests."""

        return tuple(
            (record.generation_id, record.state, record.leases)
            for record in sorted(self._records.values(), key=lambda item: item.generation_id)
        )

    @property
    def cleanup_failures(self) -> tuple[GenerationCleanupFailure, ...]:
        return (self._cleanup_failure,) if self._cleanup_failure is not None else ()

    @property
    def poisoned(self) -> bool:
        """Whether a generation cleanup failure closed publication admission."""

        return self._poisoned

    def lease(
        self,
        *,
        workspace: Path,
        session_id: str,
        turn_id: str,
        step_id: str,
    ) -> CompositionLease:
        del session_id, turn_id, step_id
        return _GenerationLease(self, workspace=workspace)

    async def publish(self, generation: CompositionGeneration) -> int:
        """Install ``generation`` at one lock-protected linearization point."""

        if not isinstance(generation, CompositionGeneration):
            raise TypeError("published value must be a CompositionGeneration")
        # Build every compatibility projection before the publication lock can
        # claim the candidate.  The projection is legacy inspection state only;
        # preparing it here closes the last fallible window in which a claimed
        # candidate could otherwise fail after the old current was retired.
        compatibility_views = self._compatibility_views_for(generation)
        async with self._lock:
            if self._disposed or self._current is None:
                raise RuntimeError("composition runtime is disposed")
            if self._poisoned:
                raise RuntimeError(
                    "composition runtime is poisoned by generation cleanup failure"
                )
            if generation is self._current.generation:
                raise ValueError(
                    "the current generation cannot be published again"
                )
            self._validate_and_claim_generation(generation)
            previous = self._current
            previous.state = "retired"
            self._schedule_cleanup_locked(previous)
            self._next_generation_id += 1
            generation_id = self._next_generation_id
            current = _GenerationRecord(generation_id, generation)
            self._records[generation_id] = current
            self._current = current
            self._install_compatibility_views(compatibility_views)
            self._changed.set()
            return generation_id

    def _validate_and_claim_generation(
        self,
        generation: CompositionGeneration,
    ) -> None:
        if generation.tools.sessions is not self._session_service:
            raise ValueError(
                "published generation must use the runtime session service"
            )
        if (
            tuple(generation.plugins) != self._startup_plugins
            and generation.activation_set is None
        ):
            raise ValueError(
                "published generation plugin identities require a Generation-owned ActivationSet"
            )
        generation._claim_for_runtime(self._owner_token)

    async def _acquire(self, lease: _GenerationLease) -> _GenerationRecord:
        async with self._lock:
            if self._disposed or self._current is None:
                raise RuntimeError("composition runtime is disposed")
            record = self._current
            record.leases += 1
            lease._record = record
            return record

    async def _release(self, record: _GenerationRecord) -> None:
        async with self._lock:
            if record.leases < 1:
                raise RuntimeError("composition lease released more than once")
            record.leases -= 1
            if record.state == "retired" and record.leases == 0:
                self._schedule_cleanup_locked(record)
            self._changed.set()

    def _schedule_cleanup_locked(self, record: _GenerationRecord) -> None:
        if record.state != "retired" or record.leases != 0 or record.cleanup_task is not None:
            return
        record.state = "cleaning"
        record.cleanup_task = asyncio.create_task(
            self._cleanup_generation(record),
            name=f"traceh-composition-cleanup-{record.generation_id}",
        )

    async def _cleanup_generation(self, record: _GenerationRecord) -> None:
        failure: GenerationCleanupFailure | None = None
        try:
            await record.generation._cleanup_owned_resources()
        except BaseException as error:
            failure = GenerationCleanupFailure(record.generation_id, _safe_error_type(error))
        async with self._lock:
            record.cleanup_failure = failure
            record.state = "cleanup_failed" if failure is not None else "cleaned"
            if failure is not None:
                self._poisoned = True
                if (
                    self._cleanup_failure is None
                    or failure.generation_id < self._cleanup_failure.generation_id
                ):
                    self._cleanup_failure = failure
            self._records.pop(record.generation_id, None)
            self._changed.set()

    def _ensure_drain_task_locked(self) -> asyncio.Task[None]:
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(
                self._drain_body(),
                name="traceh-composition-drain",
            )
        return self._drain_task

    async def _drain_body(self) -> None:
        while True:
            async with self._lock:
                retired = [
                    record for record in self._records.values() if record.state != "current"
                ]
                for record in retired:
                    if record.state == "retired" and record.leases == 0:
                        self._schedule_cleanup_locked(record)
                pending = [
                    record
                    for record in retired
                    if record.leases != 0
                    or record.cleanup_task is None
                    or not record.cleanup_task.done()
                ]
                if not pending:
                    failures = (
                        (self._cleanup_failure,)
                        if self._cleanup_failure is not None
                        else ()
                    )
                    if failures:
                        raise CompositionDrainError(failures)
                    return
                self._changed.clear()
            await self._changed.wait()

    async def drain(self) -> None:
        """Wait for all retired generations and report all cleanup failures."""

        async with self._lock:
            task = self._ensure_drain_task_locked()
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await await_worker_convergence(task)
            raise

    async def dispose(self) -> None:
        """Retire the current generation and converge every generation once."""

        async with self._lock:
            if self._dispose_task is None:
                self._disposed = True
                if self._current is not None:
                    current = self._current
                    current.state = "retired"
                    self._schedule_cleanup_locked(current)
                    self._current = None
                self._changed.set()
                self._dispose_task = self._ensure_drain_task_locked()
            task = self._dispose_task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await await_worker_convergence(task)
            self._clear_compatibility_resources()
            raise
        except BaseException:
            self._clear_compatibility_resources()
            raise
        else:
            self._clear_compatibility_resources()

    def _clear_compatibility_resources(self) -> None:
        """Drop generation-owned compatibility references after drain."""

        self._compatibility_llms.clear()
        if self._defer_external_cleanup:
            self._compatibility_tools.clear()
            self._compatibility_prompt.clear()
        else:
            if self._source_tools is not None:
                self._compatibility_tools.sync_from(self._source_tools)
            if self._source_prompt is not None:
                self._compatibility_prompt.sync_from(self._source_prompt)
            self._source_tools = None
            self._source_prompt = None

    def _refresh_compatibility_views(self, generation: CompositionGeneration) -> None:
        """Refresh the mutable v0.4 inspection projection from one Generation.

        The projection is never used by a Lease and is never the source of a
        Snapshot.  Replacing it on publish keeps legacy inspection callers
        aligned with the current Generation without exposing mutation methods
        on the Generation-owned frozen objects.
        """

        self._install_compatibility_views(self._compatibility_views_for(generation))

    def _compatibility_views_for(
        self,
        generation: CompositionGeneration,
    ) -> tuple[LlmRegistry, ToolRuntime, PromptAssembler]:
        return (
            _CompatibilityLlmRegistry.from_frozen_registry(
                generation.llms,
                publication_state=generation._publication_state,
                resource_binding=generation._resource_binding,
            ),
            _CompatibilityToolRuntime(
                generation.tools,
                publication_state=generation._publication_state,
                resource_binding=generation._resource_binding,
            ),
            _CompatibilityPromptAssembler(
                generation.prompt.sections(),
                publication_state=generation._publication_state,
                resource_binding=generation._resource_binding,
            ),
        )

    def _install_compatibility_views(
        self,
        views: tuple[LlmRegistry, ToolRuntime, PromptAssembler],
    ) -> None:
        self._compatibility_llms, self._compatibility_tools, self._compatibility_prompt = views

    def finalize_external_cleanup(self) -> None:
        """Refresh a legacy compatibility view after external cleanup.

        Stage B's default path has no external plugin owner: its ActivationSet
        is drained by the Generation.  This hook remains only for custom v0.4
        assemblies that still pass a legacy PluginManager separately.
        """

        if self._source_tools is not None:
            self._compatibility_tools.sync_from(self._source_tools)
        if self._source_prompt is not None:
            self._compatibility_prompt.sync_from(self._source_prompt)
        self._source_tools = None
        self._source_prompt = None


class _GenerationLease:
    def __init__(self, runtime: GenerationCompositionRuntime, *, workspace: Path) -> None:
        self._runtime = runtime
        self._workspace = workspace
        self._record: _GenerationRecord | None = None
        self._release_task: asyncio.Task[None] | None = None
        self._released = False
        self._entered = False

    async def __aenter__(self) -> ActiveComposition:
        if self._entered:
            raise RuntimeError("composition lease is single-use")
        self._entered = True
        try:
            record = await self._runtime._acquire(self)
        except asyncio.CancelledError:
            if self._record is not None:
                await self._release_once()
            raise
        try:
            snapshot = record.generation.snapshot(workspace=self._workspace)
        except BaseException:
            await self._release_once()
            raise
        return ActiveComposition(
            snapshot=snapshot,
            provider=record.generation.provider_instance,
            tools=record.generation.tools,
            generation_id=record.generation_id,
            scope=record.generation.scope,
            services=record.generation.services,
            verifier=record.generation.verifier,
        )

    async def _release_once(self, *, suppress_cancellation: bool = False) -> None:
        if not self._entered:
            raise RuntimeError("composition lease was not entered")
        if self._released or self._record is None:
            return

        if self._release_task is None:
            record = self._record
            self._release_task = asyncio.create_task(
                self._runtime._release(record),
                name=f"traceh-composition-lease-release-{record.generation_id}",
            )
        task = self._release_task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await await_worker_convergence(task)
            await task
            self._released = True
            if not suppress_cancellation:
                raise
        else:
            self._released = True

    async def __aexit__(self, exc_type, exc_value, traceback) -> bool | None:
        del exc_value, traceback
        await self._release_once(suppress_cancellation=exc_type is asyncio.CancelledError)
        return None


# Keep the v0.4 name importable for callers that assembled a custom runtime
# directly.  The alias is the generation-backed implementation, not a second
# lifecycle path.
StaticCompositionRuntime = GenerationCompositionRuntime
Generation = CompositionGeneration
Lease = _GenerationLease


__all__ = [
    "ActiveComposition",
    "CompositionDrainError",
    "CompositionGeneration",
    "CompositionLease",
    "CompositionResourceOwner",
    "CompositionRuntime",
    "Generation",
    "GenerationCleanupFailure",
    "GenerationCompositionRuntime",
    "Lease",
    "StaticCompositionRuntime",
]
