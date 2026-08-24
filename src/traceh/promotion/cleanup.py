"""How an owned scratch removal reports what actually happened.

Promotion has two scratch lifetimes - the integration clone and the verifier's
working space - and they must not develop two readings of the same question:
*when several things fail at once, which one does the caller learn about?*

Three facts can be true simultaneously: the work itself already failed, the
removal failed, and the caller was cancelled. Each one is real, so none may be
dropped. The rule is:

* the caller always sees its own `CancelledError` at the top when it cancelled;
* whatever else happened is attached as the cause;
* when both the work and the removal failed, the cause carries **both**.

Converging a cleanup task is deliberately not the same as reading it. The
removal keeps running after a cancellation, so its real outcome only exists once
the task is done - retrieving it and then discarding it would lose a failure that
happened strictly after the caller walked away.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from traceh.concurrency import await_worker_convergence


async def release_scratch(
    root: Path,
    primary: BaseException | None,
    *,
    remove: Callable[[Path], None],
    task_name: str,
    alone_error: Callable[[], BaseException],
    group_message: str,
) -> None:
    """Remove ``root`` in an owned task and raise every failure that occurred.

    ``alone_error`` builds the domain error used when the removal is the only
    thing that went wrong, so each caller keeps its own error vocabulary while
    sharing one composition rule.
    """

    cleanup = asyncio.create_task(
        asyncio.to_thread(remove, root), name=task_name
    )
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError as cancellation:
        # Repeated cancellation still waits for this same task.
        await await_worker_convergence(cleanup)
        cause = _combine(
            _informative(primary), _task_failure(cleanup), group_message
        )
        if cause is not None:
            raise cancellation from cause
        raise
    except BaseException as cleanup_error:
        if primary is None:
            raise alone_error() from cleanup_error
        if isinstance(primary, asyncio.CancelledError):
            raise primary from cleanup_error
        raise BaseExceptionGroup(group_message, (primary, cleanup_error)) from None


def _task_failure(cleanup: asyncio.Task[None]) -> BaseException | None:
    if cleanup.cancelled():
        return None
    return cleanup.exception()


def _informative(error: BaseException | None) -> BaseException | None:
    """A cancellation is not extra information about why something failed."""

    if error is None or isinstance(error, asyncio.CancelledError):
        return None
    return error


def _combine(
    primary: BaseException | None,
    cleanup_error: BaseException | None,
    group_message: str,
) -> BaseException | None:
    if primary is not None and cleanup_error is not None:
        return BaseExceptionGroup(group_message, (primary, cleanup_error))
    return primary if primary is not None else cleanup_error


__all__ = ["release_scratch"]
