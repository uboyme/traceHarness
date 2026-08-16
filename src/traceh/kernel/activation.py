"""Transactional activation scope used by future plugin loading."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from traceh.kernel.lifespan import CallbackRegistration, Lifespan
from traceh.kernel.tasks import OwnedTaskSet


class Activation:
    """Collect reversible effects before atomically publishing a composition.

    v0.3 exposes this primitive without automatic entry-point discovery. A future
    PluginManager can run setup inside an Activation, publish only after setup succeeds,
    and call ``dispose`` to drain owned work and reverse registrations.
    """

    def __init__(self, plugin_id: str) -> None:
        self.plugin_id = plugin_id
        self.lifespan = Lifespan()
        self.tasks = OwnedTaskSet()
        self._published = False
        self._disposed = False

    @property
    def published(self) -> bool:
        return self._published

    def own(self, registration: CallbackRegistration) -> CallbackRegistration:
        return self.lifespan.add(registration)

    def add_cleanup(self, callback: Callable[[], Awaitable[None]]) -> CallbackRegistration:
        return self.lifespan.add_cleanup(callback)

    def publish(self) -> None:
        if self._disposed:
            raise RuntimeError("cannot publish a disposed activation")
        self._published = True

    async def rollback(self) -> None:
        await self.dispose()

    async def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        await self.tasks.cancel_and_wait()
        await self.lifespan.close()
