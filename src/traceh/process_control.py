"""Cancellation-safe ownership of direct child processes."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

DEFAULT_GRACE_SECONDS = 2.0


async def converge_process(
    process: asyncio.subprocess.Process,
    *,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
) -> bool:
    """Stop ``process`` and do not return until the direct child is gone.

    Terminate first, wait a bounded grace period, then kill and wait for good.
    Cancellation aimed at convergence is absorbed; the return value tells the
    caller whether at least one such cancellation occurred.
    """

    interrupted = False
    if process.returncode is None:
        _request_stop(process.terminate)
        exited, interrupted = await _await_exit(process, grace_seconds)
        if not exited:
            _request_stop(process.kill)
            _, interrupted_after_kill = await _await_exit(process, None)
            interrupted = interrupted or interrupted_after_kill
    return interrupted


def _request_stop(action: Callable[[], None]) -> None:
    try:
        action()
    except ProcessLookupError:
        pass


async def _await_exit(
    process: asyncio.subprocess.Process,
    limit_seconds: float | None,
) -> tuple[bool, bool]:
    waiter = asyncio.ensure_future(process.wait())
    deadline = None if limit_seconds is None else time.monotonic() + limit_seconds
    interrupted = False
    try:
        while not waiter.done():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False, interrupted
            try:
                await asyncio.wait_for(asyncio.shield(waiter), remaining)
            except TimeoutError:
                return False, interrupted
            except asyncio.CancelledError:
                interrupted = True
                continue
        return True, interrupted
    finally:
        waiter.cancel()


__all__ = ["DEFAULT_GRACE_SECONDS", "converge_process"]
