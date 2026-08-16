"""Service registry protocols shared by the runtime and future plugins."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

T = TypeVar("T")
AsyncCleanup = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ServiceKey(Generic[T]):
    name: str
    api_major: int = 1

    def __str__(self) -> str:
        return f"{self.name}@{self.api_major}"


class Registration(Protocol):
    async def dispose(self) -> None:
        """Reverse one registration. Must be idempotent."""
