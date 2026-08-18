"""Owned background tasks with quiescent shutdown."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from traceh.concurrency import await_worker_convergence


class OwnedTaskSet:
    """Own the lifetime of background tasks, and retrieve their outcomes.

    **Scope.** This is *lifecycle ownership*, not a supervisor. It guarantees
    that owned tasks are cancelled and awaited at shutdown, and that whatever
    they raised is retrieved rather than left for the garbage collector. It
    deliberately does **not** restart tasks, escalate a background failure into a
    Runtime fault, log anything, or fail an activation because a task died.
    Turning a plugin's background crash into a harness-level error is a policy
    decision v0.4 has not made, and inventing it here would silently change what
    "the runtime failed" means.

    **Why outcomes need an owner at all.** A task that raises before shutdown
    completes on its own. The done callback removes it from the set, so
    ``cancel_and_wait()`` never sees it and never retrieves its exception - and
    asyncio then reports "Task exception was never retrieved" from the garbage
    collector, at an unrelated moment, attributed to nothing in particular. The
    callback therefore retrieves the outcome at the instant the task finishes.

    **Retrieving is all it does.** The exception is deliberately *not* retained.
    An earlier revision kept every failure in a list, which was an unbounded,
    permanently-growing store of exception objects - each one holding its
    traceback, and through it every frame's local variables - that no mainline
    code ever read. Keeping untrusted plugin exceptions alive for nobody is a
    memory leak and a disclosure surface, not observability. Real observability
    needs a consumer first; until v0.4 has one, ownership stops at retrieval.

    Shutdown reuses the project's existing rule for work that cannot simply be
    abandoned (see :func:`traceh.concurrency.await_worker_convergence`): the
    cancellation of all owned tasks runs in *one* task, and a caller cancelled
    while waiting for it keeps waiting for that same task. Repeat cancellation -
    a user pressing Ctrl+C a second and third time - is therefore not an escape
    hatch that leaves plugin background work running after the caller has been
    told teardown is over.
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    def spawn(self, coroutine: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
        if self._closed:
            # Close the coroutine explicitly; otherwise the rejected coroutine is
            # garbage collected un-awaited and emits a RuntimeWarning that points
            # at this line instead of at the caller's real mistake.
            coroutine.close()
            raise RuntimeError("task owner is closed")
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._retire)
        return task

    def _retire(self, task: asyncio.Task[Any]) -> None:
        """Drop a finished task, retrieving its outcome so asyncio cannot report it.

        The return value of ``task.exception()`` is intentionally discarded: the
        call itself is what marks the exception retrieved. Nothing is stored.
        """

        self._tasks.discard(task)
        if task.cancelled():
            # Cancellation is the expected shutdown outcome, not a failure, and
            # `task.exception()` would raise CancelledError if asked here.
            return
        task.exception()

    @property
    def active_count(self) -> int:
        return sum(not task.done() for task in self._tasks)

    async def _cancel_all(self) -> None:
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            # return_exceptions keeps one failing task from hiding the others and
            # retrieves every outcome, so none can resurface as a never-retrieved
            # task exception after teardown. Tasks that already finished were
            # retrieved by `_retire` at the moment they finished; this covers the
            # ones still running when shutdown began.
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def cancel_and_wait(self) -> None:
        self._closed = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._cancel_all(),
                name="traceh-owned-task-convergence",
            )
        close_task = self._close_task
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            # The caller may be cancelled repeatedly, but owned work must reach
            # quiescence before that cancellation is allowed to escape.
            await await_worker_convergence(close_task)
            raise
