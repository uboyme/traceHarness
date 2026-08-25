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


def informative_failure(error: BaseException | None) -> BaseException | None:
    """Drop a cancellation, which explains nothing about *why* work failed.

    A caller that cancelled already knows it cancelled. Keeping that in a cause
    chain only pads the report with a fact the caller supplied itself.
    """

    if error is None or isinstance(error, asyncio.CancelledError):
        return None
    return error


def combine_failures(
    primary: BaseException | None,
    secondary: BaseException | None,
    message: str,
) -> BaseException | None:
    """Keep every independent failure that really happened.

    Convergence points routinely have more than one true answer - the work
    failed *and* its cleanup failed - and choosing one silently deletes the
    other. Written once here so each convergence point cannot grow its own
    slightly different rule.
    """

    if primary is not None and secondary is not None:
        return BaseExceptionGroup(message, (primary, secondary))
    return primary if primary is not None else secondary
