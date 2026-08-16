"""Cross-process advisory file locking for local event streams.

The JSONL event store needs a mutual exclusion primitive that is honoured by
*independent operating system processes*, not only by tasks inside one Python
interpreter. An ``asyncio.Lock`` guards a single event loop and a ``.lock`` file
merely existing guards nothing at all, so both platforms must reach a real
kernel-level lock:

* POSIX uses ``fcntl.flock`` on the lock file descriptor.
* Windows uses ``msvcrt.locking``, which maps to the Win32 ``LockFile`` API and
  locks a byte range of the file identified by the descriptor.

Both mechanisms are released automatically when the descriptor is closed, which
is what makes crashes recoverable: a process that dies while holding a stream
lock leaves no stale lock behind.
"""

from __future__ import annotations

import errno
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

try:  # pragma: no cover - platform dependent import
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:  # pragma: no cover - platform dependent import
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]


class FileLockTimeout(TimeoutError):
    """Raised when an exclusive lock cannot be acquired before the deadline."""


class FileLockUnavailable(RuntimeError):
    """Raised when the platform exposes no cross-process locking primitive."""


class FileLockCancelled(RuntimeError):
    """Raised inside a worker thread when its lock wait is abandoned.

    The caller that set the cancel token is responsible for translating this
    into whatever its own cancellation looks like; nothing was locked and the
    protected resource was never touched.
    """


# Windows byte-range locks apply to an explicit region. One byte at offset 0 is
# enough for mutual exclusion, and locking a region past end-of-file is legal,
# so an empty (or freshly created) lock file locks fine.
_LOCK_OFFSET = 0
_LOCK_LENGTH = 1

_MIN_POLL_SECONDS = 0.001
_MAX_POLL_SECONDS = 0.025

# Contention errors reported by ``msvcrt.locking`` when the region is held.
_WINDOWS_BUSY_ERRNOS = frozenset(
    code
    for code in (
        getattr(errno, "EACCES", None),
        getattr(errno, "EDEADLOCK", None),
        getattr(errno, "EDEADLK", None),
    )
    if code is not None
)


def locking_backend() -> str:
    """Name of the OS primitive used on this platform."""

    if fcntl is not None:
        return "fcntl"
    if msvcrt is not None:
        return "msvcrt"
    return "none"


@contextmanager
def exclusive_file_lock(
    path: Path,
    *,
    timeout: float | None = None,
    cancel_event: threading.Event | None = None,
    waiting_event: threading.Event | None = None,
) -> Iterator[None]:
    """Hold an exclusive cross-process lock on ``path`` for the block body.

    ``timeout`` of ``None`` waits indefinitely; a numeric timeout raises
    :class:`FileLockTimeout` once the deadline passes.

    ``cancel_event`` makes the *wait* abortable: setting it while the lock is
    still held elsewhere raises :class:`FileLockCancelled` instead of waiting
    further, and the protected resource is never touched. Because a blocking
    ``flock`` cannot be interrupted, passing a cancel token switches POSIX from
    kernel blocking to the same interruptible polling loop Windows uses; an
    unbounded wait stays unbounded, it just becomes abortable.

    ``waiting_event`` is set immediately before the first acquisition attempt,
    so a caller on another thread can tell that this thread really reached the
    lock rather than assuming it from timing.

    The lock is always released, including on exceptions and on cancellation
    that unwinds through this context manager.
    """

    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o644)
    try:
        _acquire(descriptor, path, timeout, cancel_event, waiting_event)
        try:
            yield
        finally:
            _release(descriptor)
    finally:
        # Closing also drops any lock still held, which is the backstop for a
        # failed explicit release.
        os.close(descriptor)


def _acquire(
    descriptor: int,
    path: Path,
    timeout: float | None,
    cancel_event: threading.Event | None,
    waiting_event: threading.Event | None,
) -> None:
    if fcntl is not None:
        if timeout is None and cancel_event is None:
            if waiting_event is not None:
                waiting_event.set()
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            return
        _poll_until_locked(
            lambda: fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB),
            _is_posix_busy,
            path,
            timeout,
            cancel_event,
            waiting_event,
        )
        return
    if msvcrt is not None:
        _poll_until_locked(
            lambda: _windows_try_lock(descriptor),
            _is_windows_busy,
            path,
            timeout,
            cancel_event,
            waiting_event,
        )
        return
    raise FileLockUnavailable(
        "no cross-process file locking primitive is available on this platform"
    )


def _release(descriptor: int) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        elif msvcrt is not None:
            os.lseek(descriptor, _LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, _LOCK_LENGTH)
    except OSError:
        # The descriptor is closed by the caller immediately after this, and
        # both backends drop locks on close, so an unlock failure must not mask
        # the original error from the critical section.
        pass


def _windows_try_lock(descriptor: int) -> None:
    # ``msvcrt.locking`` locks ``_LOCK_LENGTH`` bytes starting at the current
    # file position, so the position is set explicitly on every attempt.
    os.lseek(descriptor, _LOCK_OFFSET, os.SEEK_SET)
    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, _LOCK_LENGTH)


def _is_posix_busy(error: OSError) -> bool:
    return isinstance(error, BlockingIOError) or error.errno in (errno.EACCES, errno.EAGAIN)


def _is_windows_busy(error: OSError) -> bool:
    return error.errno in _WINDOWS_BUSY_ERRNOS


def _poll_until_locked(
    attempt: Callable[[], None],
    is_busy: Callable[[OSError], bool],
    path: Path,
    timeout: float | None,
    cancel_event: threading.Event | None,
    waiting_event: threading.Event | None,
) -> None:
    deadline = None if timeout is None else time.monotonic() + max(timeout, 0.0)
    delay = _MIN_POLL_SECONDS
    if waiting_event is not None:
        waiting_event.set()
    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise FileLockCancelled(f"lock wait on {path} was cancelled")
        try:
            attempt()
            return
        except OSError as error:
            if not is_busy(error):
                raise
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FileLockTimeout(
                    f"could not acquire exclusive lock on {path} within {timeout} seconds"
                )
            delay = min(delay, remaining)
        if cancel_event is not None:
            # Sleeping on the token instead of the clock makes cancellation
            # take effect immediately rather than at the next poll.
            if cancel_event.wait(delay):
                raise FileLockCancelled(f"lock wait on {path} was cancelled")
        else:
            time.sleep(delay)
        delay = min(delay * 2, _MAX_POLL_SECONDS)
