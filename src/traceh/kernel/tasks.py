"""Owned background tasks with quiescent shutdown."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any


class OwnedTaskSet:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False

    def spawn(self, coroutine: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
        if self._closed:
            raise RuntimeError("task owner is closed")
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    @property
    def active_count(self) -> int:
        return sum(not task.done() for task in self._tasks)

    async def cancel_and_wait(self) -> None:
        self._closed = True
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
