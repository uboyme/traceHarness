"""What a failed or cancelled append actually did.

`EventStore` documents a commit-point boundary: a cancellation landing inside
the critical section raises `CancelledError` to the caller while the event is
already durable, and there is no automatic retry. "I was cancelled" therefore
does not mean "nothing was written", and the only honest answer is to look.

Both control-plane transactions - Agent creation and Inbox acceptance - need
exactly that answer, and they must not develop two readings of it. A second
copy of this convergence protocol would be a second definition of when a caller
may be released and of what "not committed" is allowed to claim, which is the
same class of drift the identity module exists to prevent.

The seam is deliberately narrow. This module answers *the question* - did our
event land, and can we even tell - while each transaction keeps its own error
mapping, because which domain error a failure becomes is a property of that
transaction, not of the re-read. Nothing here knows about Agents, Inboxes,
streams or payload shapes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from traceh.api.events import EventEnvelope
from traceh.concurrency import await_worker_convergence


async def committed_after_failure(
    read_events: Callable[[], Awaitable[tuple[EventEnvelope, ...]]],
    matches: Callable[[EventEnvelope], bool],
) -> bool | None:
    """Whether an event satisfying ``matches`` is in the stream.

    Returns:
        ``True``  - the event is provably there;
        ``False`` - it is provably not there;
        ``None``  - **unknown**; the re-read could not answer.

    The third state is not pedantry. Collapsing unknown into ``False`` makes the
    strongest possible claim from the weakest possible evidence, at the exact
    moment the store is already misbehaving, and a caller acting on it would
    write a second copy of something that had already committed.

    The read runs in its own Task and is converged through
    `await_worker_convergence()`, so a second and third cancellation cannot
    release the caller early and no read is left in flight when this returns.

    ``Exception`` is caught, never ``BaseException``: `SystemExit` and
    `KeyboardInterrupt` are not answers about the stream and must reach the
    caller unchanged.
    """

    read_task = asyncio.create_task(read_events())
    try:
        events = await asyncio.shield(read_task)
    except asyncio.CancelledError:
        await await_worker_convergence(read_task)
        if read_task.cancelled():
            return None
        try:
            events = read_task.result()
        except Exception:
            return None
    except Exception:
        return None
    try:
        for event in events:
            if matches(event):
                return True
    except Exception:
        # ``matches`` reads payload the store handed back, and reading
        # caller-influenced data can itself fail. An unreadable answer is
        # unknown, not "absent".
        return None
    return False


__all__ = ["committed_after_failure"]
