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

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import IO


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


def _read_from_start(handle: IO[bytes]) -> bytes:
    handle.seek(0)
    return handle.read()
