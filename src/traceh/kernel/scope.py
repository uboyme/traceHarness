"""Application-to-Agent service scope composition."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar

from traceh.api.services import ServiceKey
from traceh.kernel.registry import (
    ServiceApiMajorMismatchError,
    ServiceOverrideRequiredError,
    ServiceRegistry,
    ServiceView,
)

T = TypeVar("T")


class ScopeKind(StrEnum):
    APPLICATION = "application"
    WORKSPACE = "workspace"
    PRESET = "preset"
    AGENT = "agent"


_SCOPE_ORDER = (
    ScopeKind.APPLICATION,
    ScopeKind.WORKSPACE,
    ScopeKind.PRESET,
    ScopeKind.AGENT,
)


@dataclass(frozen=True, slots=True)
class ScopedServiceBinding:
    """One borrowed assembly-time value targeted at an explicit scope layer.

    The assembler retains lifecycle ownership of ``value``. Scope publication
    controls lookup visibility; it does not imply cleanup ownership.
    """

    scope: ScopeKind
    key: ServiceKey[Any]
    value: Any
    replace: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.scope, ScopeKind):
            raise TypeError("scoped service binding scope must be a ScopeKind")
        if not isinstance(self.key, ServiceKey):
            raise TypeError("scoped service binding key must be a ServiceKey")
        if self.value is None:
            raise ValueError("scoped service binding value cannot be None")
        if not isinstance(self.replace, bool):
            raise TypeError("scoped service binding replace must be a bool")


class Scope:
    """One named layer whose service lookup reads through its parent."""

    __slots__ = (
        "_kind",
        "_name",
        "_parent",
        "_registry",
        "_sealed",
        "_services",
    )

    def __init__(
        self,
        *,
        name: str,
        parent: Scope | None = None,
        kind: ScopeKind | None = None,
        services: ServiceRegistry | None = None,
    ) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError("scope name must be a non-empty string")
        expected_index = 0 if parent is None else _SCOPE_ORDER.index(parent.kind) + 1
        if expected_index >= len(_SCOPE_ORDER):
            raise ValueError("agent scope cannot have a child scope")
        actual_kind = kind or _SCOPE_ORDER[expected_index]
        if actual_kind is not _SCOPE_ORDER[expected_index]:
            raise ValueError(
                "scope hierarchy must be application -> workspace -> preset -> agent"
            )
        expected_parent = parent._registry if parent is not None else None
        if services is None:
            services = ServiceRegistry(scope=actual_kind.value, parent=expected_parent)
        elif services.parent is not expected_parent:
            raise ValueError("scope service registry must reference its parent scope registry")
        self._name = name
        self._kind = actual_kind
        self._parent = parent
        self._registry = services
        self._services: ServiceView = services.view()
        self._sealed = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def kind(self) -> ScopeKind:
        return self._kind

    @property
    def parent(self) -> Scope | None:
        return self._parent

    @property
    def services(self) -> ServiceView:
        return self._services

    def provide(self, key: ServiceKey[T], value: T, *, replace: bool = False) -> None:
        if self._sealed:
            raise RuntimeError("published scope is read-only")
        self._registry.bind(key, value, replace=replace)

    def _seal(self) -> None:
        self._sealed = True

    def get(self, key: ServiceKey[T]) -> T | None:
        return self._registry.get(key)

    def require(self, key: ServiceKey[T]) -> T:
        return self._registry.require(key)

    def flatten(self) -> dict[str, object]:
        return self._registry.snapshot()


@dataclass(frozen=True, slots=True)
class ScopeChain:
    """The four service layers that form one effective Agent scope."""

    application: Scope
    workspace: Scope
    preset: Scope
    agent: Scope
    bindings: tuple[ScopedServiceBinding, ...] = ()

    @classmethod
    def _assemble(
        cls,
        application_services: ServiceRegistry,
        bindings: tuple[ScopedServiceBinding, ...],
    ) -> ScopeChain:
        application = Scope(
            name=ScopeKind.APPLICATION.value,
            kind=ScopeKind.APPLICATION,
            services=application_services,
        )
        workspace = Scope(name=ScopeKind.WORKSPACE.value, parent=application)
        preset = Scope(name=ScopeKind.PRESET.value, parent=workspace)
        agent = Scope(name=ScopeKind.AGENT.value, parent=preset)
        chain = cls(application, workspace, preset, agent, tuple(bindings))
        by_kind = {
            ScopeKind.APPLICATION: application,
            ScopeKind.WORKSPACE: workspace,
            ScopeKind.PRESET: preset,
            ScopeKind.AGENT: agent,
        }
        ordered_bindings = sorted(
            enumerate(bindings),
            key=lambda item: (_SCOPE_ORDER.index(item[1].scope), item[0]),
        )
        for _, binding in ordered_bindings:
            by_kind[binding.scope].provide(
                binding.key,
                binding.value,
                replace=binding.replace,
            )
        for scope in (application, workspace, preset, agent):
            scope._seal()
        return chain

    @classmethod
    def build(
        cls,
        application_services: ServiceRegistry,
        bindings: tuple[ScopedServiceBinding, ...] = (),
    ) -> ScopeChain:
        """Build all four layers without partially mutating the application owner.

        Binding validation is first run against an isolated fork.  Only a
        complete, valid assembly is replayed into the caller-owned Application
        registry.  The operation is synchronous, so no other coroutine can
        interleave between preflight and commit.
        """

        if application_services.parent is not None:
            raise ValueError("application service registry cannot have a parent")
        frozen_bindings = tuple(bindings)
        cls._assemble(application_services.fork(parent=None), frozen_bindings)
        return cls._assemble(application_services, frozen_bindings)

    def validate_overrides(self) -> None:
        """Recheck child intent after application plugins finish publishing."""

        by_kind = {
            ScopeKind.APPLICATION: self.application,
            ScopeKind.WORKSPACE: self.workspace,
            ScopeKind.PRESET: self.preset,
            ScopeKind.AGENT: self.agent,
        }
        for binding in self.bindings:
            if binding.scope is ScopeKind.APPLICATION:
                continue
            target = by_kind[binding.scope]
            parent = target.parent
            if parent is None:
                raise RuntimeError("non-application scope has no parent")
            inherited = parent._registry.resolve(binding.key)
            if inherited is not None and not binding.replace:
                raise ServiceOverrideRequiredError(
                    "service-override-requires-replace",
                    key=binding.key,
                    scope=binding.scope.value,
                    existing_scope=inherited.scope,
                )
            named = parent._registry.resolve_name(binding.key.name)
            if (
                binding.replace
                and inherited is None
                and named is not None
                and named.key.api_major != binding.key.api_major
            ):
                raise ServiceApiMajorMismatchError(
                    "service-override-api-major-mismatch",
                    key=binding.key,
                    scope=binding.scope.value,
                    existing_scope=named.scope,
                )

    def has_application_registry(self, registry: ServiceRegistry) -> bool:
        """Return whether ``registry`` is this chain's application owner."""

        return self.application._registry is registry

    @property
    def child_bindings(self) -> tuple[ScopedServiceBinding, ...]:
        """Return the reusable Workspace/Preset/Agent assembly blueprint."""

        return tuple(
            binding
            for binding in self.bindings
            if binding.scope is not ScopeKind.APPLICATION
        )

    @property
    def effective(self) -> Scope:
        return self.agent
