"""Typed, hierarchical service registry."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, TypeVar, cast

from traceh.api.services import ServiceKey
from traceh.kernel.lifespan import CallbackRegistration

T = TypeVar("T")
_MISSING = object()


class ServiceNotFoundError(LookupError):
    """Raised when no scope in a service chain provides the requested key."""


class ServiceConflictError(RuntimeError):
    """Base class for deterministic service-registration conflicts."""

    def __init__(
        self,
        code: str,
        *,
        key: ServiceKey[Any],
        scope: str,
        existing_scope: str,
    ) -> None:
        self.code = code
        self.key = key
        self.scope = scope
        self.existing_scope = existing_scope
        super().__init__(
            f"{code}: cannot bind {key} in {scope}; existing binding is in "
            f"{existing_scope}"
        )


class ServiceOverrideRequiredError(ServiceConflictError):
    """A nearer scope attempted to shadow an ancestor without ``replace=True``."""


class ServiceApiMajorMismatchError(ServiceConflictError):
    """An explicit override named a different API major than its target."""


@dataclass(frozen=True, slots=True)
class ServiceResolution:
    """One resolved value together with the scope that supplied it."""

    key: ServiceKey[Any]
    value: Any
    scope: str


class ServiceView:
    """Read-only service resolution surface exposed by a published scope."""

    __slots__ = ("_registry",)

    def __init__(self, registry: ServiceRegistry) -> None:
        self._registry = registry

    def resolve(self, key: ServiceKey[T]) -> ServiceResolution | None:
        return self._registry.resolve(key)

    def get(self, key: ServiceKey[T]) -> T | None:
        return self._registry.get(key)

    def require(self, key: ServiceKey[T]) -> T:
        return self._registry.require(key)

    def snapshot(self) -> dict[str, object]:
        return self._registry.snapshot()


class ServiceRegistry:
    """A local registry with an optional, read-through parent.

    Registrations always belong to exactly one layer. Reads prefer the nearest
    layer, while shadowing an ancestor is rejected unless the caller explicitly
    passes ``replace=True``. A replacement must target the same
    :class:`ServiceKey`, including its API major.
    """

    def __init__(
        self,
        *,
        scope: str = "application",
        parent: ServiceRegistry | None = None,
    ) -> None:
        if not isinstance(scope, str) or not scope:
            raise ValueError("service registry scope must be a non-empty string")
        self.scope = scope
        self.parent = parent
        self._values: dict[ServiceKey[Any], Any] = {}
        self._lock = asyncio.Lock()
        self._view = ServiceView(self)

    def view(self) -> ServiceView:
        return self._view

    def resolve(self, key: ServiceKey[T]) -> ServiceResolution | None:
        if key in self._values:
            return ServiceResolution(key, self._values[key], self.scope)
        if self.parent is not None:
            return self.parent.resolve(key)
        return None

    def get(self, key: ServiceKey[T]) -> T | None:
        resolution = self.resolve(key)
        return cast(T | None, resolution.value if resolution is not None else None)

    def require(self, key: ServiceKey[T]) -> T:
        resolution = self.resolve(key)
        if resolution is None:
            raise ServiceNotFoundError(f"service not found: {key}")
        return cast(T, resolution.value)

    def _same_name_resolution(self, name: str) -> ServiceResolution | None:
        for key, value in self._values.items():
            if key.name == name:
                return ServiceResolution(key, value, self.scope)
        if self.parent is not None:
            return self.parent._same_name_resolution(name)
        return None

    def resolve_name(self, name: str) -> ServiceResolution | None:
        """Resolve the nearest binding with ``name``, regardless of API major."""

        if not isinstance(name, str) or not name:
            raise ValueError("service name must be a non-empty string")
        return self._same_name_resolution(name)

    def _validate_binding(self, key: ServiceKey[Any], *, replace: bool) -> object:
        if not isinstance(key, ServiceKey):
            raise TypeError("service key must be a ServiceKey")
        if not isinstance(replace, bool):
            raise TypeError("service replace must be a bool")
        local = self._values.get(key, _MISSING)
        if local is not _MISSING:
            if not replace:
                raise ServiceConflictError(
                    "service-already-bound",
                    key=key,
                    scope=self.scope,
                    existing_scope=self.scope,
                )
            return local

        inherited = self.parent.resolve(key) if self.parent is not None else None
        if inherited is not None:
            if not replace:
                raise ServiceOverrideRequiredError(
                    "service-override-requires-replace",
                    key=key,
                    scope=self.scope,
                    existing_scope=inherited.scope,
                )
            return _MISSING

        named = self._same_name_resolution(key.name)
        if replace and named is not None and named.key.api_major != key.api_major:
            raise ServiceApiMajorMismatchError(
                "service-override-api-major-mismatch",
                key=key,
                scope=self.scope,
                existing_scope=named.scope,
            )
        return _MISSING

    def bind(
        self,
        key: ServiceKey[T],
        value: T,
        *,
        replace: bool = False,
    ) -> None:
        """Bind during single-threaded assembly before a registry is published."""

        if value is None:
            raise ValueError("service value cannot be None")
        self._validate_binding(key, replace=replace)
        self._values[key] = value

    async def provide(
        self,
        key: ServiceKey[T],
        value: T,
        *,
        replace: bool = False,
    ) -> CallbackRegistration:
        if value is None:
            raise ValueError("service value cannot be None")
        async with self._lock:
            previous = self._validate_binding(key, replace=replace)
            self._values[key] = value

        async def cleanup() -> None:
            async with self._lock:
                current = self._values.get(key, _MISSING)
                if current is value:
                    if previous is _MISSING:
                        self._values.pop(key, None)
                    else:
                        self._values[key] = previous

        return CallbackRegistration(cleanup)

    def local_snapshot(self) -> dict[ServiceKey[Any], Any]:
        """Return a detached mapping of bindings owned by this layer only."""

        return dict(self._values)

    def snapshot(self) -> dict[str, object]:
        values = self.parent.snapshot() if self.parent is not None else {}
        values.update({str(key): value for key, value in self._values.items()})
        return values

    def fork(self, *, parent: ServiceRegistry | None | object = _MISSING) -> ServiceRegistry:
        """Return an independent registry with the same local bindings.

        With no explicit ``parent``, the complete parent chain is forked too.
        Passing a parent reuses that already-forked ancestor when assembling a
        new scope chain.
        """

        if parent is _MISSING:
            actual_parent = self.parent.fork() if self.parent is not None else None
        elif parent is None or isinstance(parent, ServiceRegistry):
            actual_parent = parent
        else:
            raise TypeError("parent must be a ServiceRegistry or None")
        forked = ServiceRegistry(scope=self.scope, parent=actual_parent)
        forked._values = dict(self._values)
        return forked
