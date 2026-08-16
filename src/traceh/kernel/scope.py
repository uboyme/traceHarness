"""Hierarchical capability scopes."""

from __future__ import annotations

from typing import Any, TypeVar, cast

from traceh.api.services import ServiceKey
from traceh.kernel.registry import ServiceConflictError, ServiceNotFoundError

T = TypeVar("T")


class Scope:
    """A small hierarchical registry.

    v0.3 keeps scopes immutable after runtime assembly. Future plugin generations can
    publish a new Scope without mutating a Step already holding the old one.
    """

    def __init__(self, *, name: str, parent: "Scope | None" = None) -> None:
        self.name = name
        self.parent = parent
        self._values: dict[ServiceKey[Any], Any] = {}

    def provide(self, key: ServiceKey[T], value: T, *, replace: bool = False) -> None:
        if key in self._values and not replace:
            raise ServiceConflictError(f"service already exists in scope {self.name}: {key}")
        self._values[key] = value

    def get(self, key: ServiceKey[T]) -> T | None:
        if key in self._values:
            return cast(T, self._values[key])
        if self.parent is not None:
            return self.parent.get(key)
        return None

    def require(self, key: ServiceKey[T]) -> T:
        value = self.get(key)
        if value is None:
            raise ServiceNotFoundError(f"service not found from scope {self.name}: {key}")
        return value

    def flatten(self) -> dict[str, object]:
        result = self.parent.flatten() if self.parent else {}
        result.update({str(key): value for key, value in self._values.items()})
        return result
