"""Transactional plugin activation scope."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from traceh.concurrency import await_worker_convergence
from traceh.kernel.lifespan import CallbackRegistration, Lifespan
from traceh.kernel.tasks import OwnedTaskSet


class Activation:
    """Own every reversible side effect produced by one plugin's setup.

    A plugin never registers anything directly. It registers through a context
    that hands the resulting :class:`CallbackRegistration` to its Activation, so
    "undo everything this plugin did" is a single call rather than a checklist
    that each contribution type has to remember to join.

    ``dispose`` converges: owned background tasks are cancelled and awaited
    before registrations are released, and a caller cancelled mid-teardown waits
    for the same teardown rather than escaping early. That is what makes rollback
    safe to run from a cancellation path.
    """

    def __init__(self, plugin_id: str) -> None:
        self.plugin_id = plugin_id
        self.lifespan = Lifespan()
        self.tasks = OwnedTaskSet()
        self._published = False
        self._dispose_task: asyncio.Task[None] | None = None

    @property
    def published(self) -> bool:
        return self._published

    @property
    def disposed(self) -> bool:
        return self._dispose_task is not None and self._dispose_task.done()

    def own(self, registration: CallbackRegistration) -> CallbackRegistration:
        return self.lifespan.add(registration)

    def add_cleanup(self, callback: Callable[[], Awaitable[None]]) -> CallbackRegistration:
        return self.lifespan.add_cleanup(callback)

    def publish(self) -> None:
        if self._dispose_task is not None:
            raise RuntimeError("cannot publish a disposing activation")
        self._published = True

    async def rollback(self) -> None:
        await self.dispose()

    async def _dispose(self) -> None:
        errors: list[BaseException] = []
        # Tasks first: a background task may still be using something a
        # registration is about to tear down.
        try:
            await self.tasks.cancel_and_wait()
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            errors.append(error)
        # A failing task set must not skip registration cleanup, and one failing
        # registration must not skip the rest - Lifespan.close() already
        # guarantees the latter and reports the collected failures.
        try:
            await self.lifespan.close()
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            errors.append(error)
        if errors:
            raise ExceptionGroup(
                f"activation cleanup failed for {self.plugin_id}",
                errors,
            )

    async def dispose(self) -> None:
        if self._dispose_task is None:
            self._dispose_task = asyncio.create_task(
                self._dispose(),
                name=f"traceh-plugin-dispose-{self.plugin_id}",
            )
        task = self._dispose_task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await await_worker_convergence(task)
            raise
