"""Owning child processes and their output.

Shared by every place that starts a subprocess - `CommandVerifier` and
`ShellTool` today - so the rules below are about processes in general, not about
one caller's result type.

Two rules hold everywhere a subprocess is started:

* a cancelled `await` only unwinds the Python coroutine, so the child has to be
  shut down explicitly before cancellation continues outwards;
* on paths whose contract retains captured output - normal completion, and a
  timeout owned by the component that started the child - that output is
  evidence, so shutting the child down must not lose it.

Which paths those are is the caller's contract, not this module's. A component
that owns its own timeout converges the child and then reads the capture. Plain
cancellation does not read it. Neither does runtime budget preemption: when
`ToolRuntime`'s budget expires it cancels the tool, the tool takes its
cancellation path, and the result reported is the runtime's generic timeout
rather than anything the child printed.

Both are solved by giving the output one owner. The child writes straight into
temporary files owned by this process instead of into pipes, so there is exactly
one place the bytes live, nothing has to be re-read, and no pipe transport can be
left dangling for the event loop to complain about at shutdown.

What this module does *not* promise is retention. It converges the child; whether
the captured bytes are read at all is the caller's decision. On cancellation the
caller converges the child and re-raises, without reading the capture and without
writing anything to the event log, and the temporary files are dropped with it.

Only an event the caller appends to the Session or Effect log is a persisted
fact - `verification/result` for the verifier, `effect/outcome` and `tool/result`
for a tool call - and which of them appears depends on the path taken. Bytes
sitting in these temporary files are not yet a fact.

Every wait here absorbs cancellation aimed at it. Abandoning a shutdown halfway
is exactly the bug these helpers exist to prevent, so an impatient second or
third Ctrl+C cannot turn a child into an orphan.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import IO

DEFAULT_GRACE_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class CapturedOutput:
    """The single owner of one child's stdout and stderr.

    The child writes into these files directly, so whatever it flushed has
    already been captured and is visible to this process before any timeout or
    cancellation is noticed. That is capture, not durability: the bytes live in
    a temporary file for the lifetime of this call, and only become a persisted
    fact once they are written to the event log. Reading is a plain file read:
    it cannot block on a pipe a grandchild is still holding, and it can be
    repeated without losing anything.
    """

    stdout: IO[bytes]
    stderr: IO[bytes]

    def read(self) -> tuple[bytes, bytes]:
        return _read_from_start(self.stdout), _read_from_start(self.stderr)


@contextmanager
def capture_output() -> Iterator[CapturedOutput]:
    """Provide temporary files to hand to `create_subprocess_exec`."""

    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        yield CapturedOutput(stdout=stdout, stderr=stderr)


async def converge_process(
    process: asyncio.subprocess.Process,
    *,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
) -> bool:
    """Stop `process` and do not return until it is gone.

    Terminate first so the child can exit on its own terms, wait a bounded
    grace period, then kill and wait for good. Returns True when at least one
    cancellation was absorbed while doing so, which lets the caller re-raise it
    once the child is actually dead.

    Only the direct child is managed. A grandchild it spawned may keep running
    and may keep writing into the captured files; that is out of scope and does
    not hold this function up.
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


def _read_from_start(handle: IO[bytes]) -> bytes:
    handle.seek(0)
    return handle.read()


def _request_stop(action: Callable[[], None]) -> None:
    try:
        action()
    except ProcessLookupError:
        # It exited between the check and the signal; nothing left to stop.
        pass


async def _await_exit(
    process: asyncio.subprocess.Process,
    limit_seconds: float | None,
) -> tuple[bool, bool]:
    """Wait for exit, returning (exited, absorbed_a_cancellation).

    `exited` is False when `limit_seconds` elapsed first. Cancellation aimed at
    this coroutine is absorbed and the same wait is resumed, so repeated
    cancellation cannot cut the wait short.
    """

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
        # Cancelling the waiter never affects the child, only our interest in it.
        waiter.cancel()
