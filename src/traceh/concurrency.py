"""Convergence helpers for work that cannot be cancelled once it started.

Threads cannot be killed and a blocking library call cannot be interrupted, so
anything handed to an executor has to be waited for rather than abandoned.
Otherwise the caller is told the operation is over while the worker keeps
writing files or holding a socket.
"""

from __future__ import annotations

import asyncio


async def await_worker_convergence[T](future: asyncio.Future[T]) -> None:
    """Wait for `future`'s worker to finish, however often we are cancelled.

    Cancelling again while the worker is still running must not release the
    caller early, so every additional ``CancelledError`` is absorbed and the
    *same* future is awaited again. The worker's own outcome is retrieved here
    so it can never surface later as a never-retrieved future exception.
    """

    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            # Repeated cancellation: keep waiting for this worker.
            continue
        except BaseException:
            # The worker itself failed; it has converged either way.
            break
    if future.done() and not future.cancelled():
        future.exception()
