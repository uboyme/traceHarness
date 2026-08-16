"""Typed service registry."""

from __future__ import annotations

import asyncio
from typing import Any, TypeVar, cast

from traceh.api.services import ServiceKey
from traceh.kernel.lifespan import CallbackRegistration

T = TypeVar("T")


class ServiceNotFoundError(LookupError):
    pass


class ServiceConflictError(RuntimeError):
    pass


class ServiceRegistry:
    def __init__(self) -> None:
        self._values: dict[ServiceKey[Any], Any] = {}
        self._lock = asyncio.Lock()

    def get(self, key: ServiceKey[T]) -> T | None:
        value = self._values.get(key)
        return cast(T | None, value)

    def require(self, key: ServiceKey[T]) -> T:
        value = self.get(key)
        if value is None:
            raise ServiceNotFoundError(f"service not found: {key}")
        return value

    async def provide(
        self,
        key: ServiceKey[T],
        value: T,
        *,
        replace: bool = False,
    ) -> CallbackRegistration:
        async with self._lock:
            previous = self._values.get(key)
            if previous is not None and not replace:
                raise ServiceConflictError(f"service already registered: {key}")
            self._values[key] = value

        async def cleanup() -> None:
            async with self._lock:
                current = self._values.get(key)
                if current is value:
                    if previous is None:
                        self._values.pop(key, None)
                    else:
                        self._values[key] = previous

        return CallbackRegistration(cleanup)

    def snapshot(self) -> dict[str, object]:
        return {str(key): value for key, value in self._values.items()}
