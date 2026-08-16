"""Reversible registrations and deterministic cleanup."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


class CallbackRegistration:
    """An idempotent async disposer."""

    def __init__(self, cleanup: Callable[[], Awaitable[None]]) -> None:
        self._cleanup = cleanup
        self._lock = asyncio.Lock()
        self._disposed = False

    @property
    def disposed(self) -> bool:
        return self._disposed

    async def dispose(self) -> None:
        async with self._lock:
            if self._disposed:
                return
            self._disposed = True
            await self._cleanup()


class Lifespan:
    """Own registrations/tasks and release them in reverse order."""

    def __init__(self) -> None:
        self._registrations: list[CallbackRegistration] = []
        self._lock = asyncio.Lock()
        self._closed = False

    def add(self, registration: CallbackRegistration) -> CallbackRegistration:
        if self._closed:
            raise RuntimeError("lifespan is closed")
        self._registrations.append(registration)
        return registration

    def add_cleanup(self, cleanup: Callable[[], Awaitable[None]]) -> CallbackRegistration:
        return self.add(CallbackRegistration(cleanup))

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            errors: list[BaseException] = []
            while self._registrations:
                registration = self._registrations.pop()
                try:
                    await registration.dispose()
                except BaseException as error:  # cleanup must continue
                    errors.append(error)
            if errors:
                raise ExceptionGroup("one or more cleanup operations failed", errors)
