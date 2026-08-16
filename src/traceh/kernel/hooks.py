"""Typed hook dispatcher with explicit delivery semantics.

v0.3 uses notification hooks for observability. Behavioral extension points such as
prompt sections, model providers and tool policies remain explicit services so they can
be snapshotted and reconstructed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Generic, TypeVar, cast

from traceh.kernel.lifespan import CallbackRegistration

T = TypeVar("T")
R = TypeVar("R")


class HookMode(str, Enum):
    NOTIFY = "notify"
    TRANSFORM = "transform"


@dataclass(frozen=True, slots=True)
class HookKey(Generic[T]):
    name: str
    mode: HookMode


NotifyHandler = Callable[[Any], Awaitable[None]]
TransformHandler = Callable[[Any], Awaitable[Any]]


class HookDispatcher:
    def __init__(self) -> None:
        self._handlers: dict[HookKey[Any], list[Callable[..., Awaitable[Any]]]] = {}
        self._lock = asyncio.Lock()

    async def register(
        self,
        key: HookKey[T],
        handler: Callable[..., Awaitable[Any]],
    ) -> CallbackRegistration:
        async with self._lock:
            handlers = self._handlers.setdefault(key, [])
            handlers.append(handler)

        async def cleanup() -> None:
            async with self._lock:
                current = self._handlers.get(key)
                if current and handler in current:
                    current.remove(handler)
                if current == []:
                    self._handlers.pop(key, None)

        return CallbackRegistration(cleanup)

    async def notify(self, key: HookKey[T], value: T) -> tuple[BaseException, ...]:
        if key.mode is not HookMode.NOTIFY:
            raise TypeError(f"hook {key.name} is not a NOTIFY hook")
        handlers = tuple(self._handlers.get(key, ()))
        if not handlers:
            return ()
        outcomes = await asyncio.gather(
            *(cast(NotifyHandler, handler)(value) for handler in handlers),
            return_exceptions=True,
        )
        return tuple(item for item in outcomes if isinstance(item, BaseException))

    async def transform(self, key: HookKey[T], value: T) -> T:
        if key.mode is not HookMode.TRANSFORM:
            raise TypeError(f"hook {key.name} is not a TRANSFORM hook")
        current: Any = value
        for handler in tuple(self._handlers.get(key, ())):
            current = await cast(TransformHandler, handler)(current)
        return cast(T, current)


TURN_STARTED = HookKey[dict[str, object]]("agent.turn_started", HookMode.NOTIFY)
TURN_FINISHED = HookKey[dict[str, object]]("agent.turn_finished", HookMode.NOTIFY)
STEP_FINISHED = HookKey[dict[str, object]]("agent.step_finished", HookMode.NOTIFY)
