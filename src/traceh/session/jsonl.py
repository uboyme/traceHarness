"""Crash-tolerant JSONL event store."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections import defaultdict
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TypeVar
from urllib.parse import quote, unquote

from traceh.api.events import EventEnvelope, PendingEvent
from traceh.concurrency import await_worker_convergence
from traceh.session.event_store import ConcurrencyConflict, Durability
from traceh.session.file_lock import FileLockCancelled, exclusive_file_lock

_T = TypeVar("_T")


class CorruptEventStream(RuntimeError):
    pass


@dataclass(frozen=True)
class _StreamLockSignals:
    """Cross-thread signals for one locked store operation.

    ``waiting`` is set by the worker thread when it starts trying to take the
    OS lock, ``cancel`` asks that worker to abandon the wait, and ``finished``
    is set once the worker has fully converged. ``_run_locked()`` guarantees
    ``finished`` is set before ``CancelledError`` reaches the caller, which is
    what makes "no detached background work" observable rather than assumed.
    """

    waiting: threading.Event = field(default_factory=threading.Event)
    cancel: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)


class JsonlEventStore:
    """Append-only JSONL store guarded by a real cross-process stream lock.

    Every critical section (head lookup, partial-tail repair, ``expected_seq``
    check, append and flush) runs while holding an exclusive OS lock on the
    stream's ``.lock`` file, so independent processes serialize instead of
    racing. The in-process ``asyncio.Lock`` remains as a cheap fast path for
    tasks inside one interpreter.

    The blocking work runs on a worker thread, so cancellation is handled
    explicitly by ``_run_locked()``: cancelling ``append``/``read``/``head``
    never leaves a detached thread waiting for the lock or writing to the
    stream.
    """

    def __init__(
        self,
        root: Path,
        *,
        repair_partial_tail: bool = True,
        lock_timeout: float | None = None,
    ) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.repair_partial_tail = repair_partial_tail
        self.lock_timeout = lock_timeout
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _path(self, stream_id: str) -> Path:
        return self.root / f"{quote(stream_id, safe='')}.jsonl"

    def _lock_path(self, stream_id: str) -> Path:
        return self.root / f"{quote(stream_id, safe='')}.lock"

    @staticmethod
    def _previous_newline(handle, before: int) -> int:
        position = before
        chunk_size = 8192
        while position > 0:
            start = max(0, position - chunk_size)
            handle.seek(start)
            chunk = handle.read(position - start)
            index = chunk.rfind(b"\n")
            if index >= 0:
                return start + index
            position = start
        return -1

    def _head_unlocked(self, stream_id: str) -> int:
        path = self._path(stream_id)
        if not path.exists():
            return 0
        with path.open("r+b") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if size == 0:
                return 0
            handle.seek(size - 1)
            if handle.read(1) != b"\n":
                previous = self._previous_newline(handle, size)
                truncate_at = previous + 1 if previous >= 0 else 0
                if not self.repair_partial_tail:
                    raise CorruptEventStream(f"partial JSONL tail in {path}")
                handle.truncate(truncate_at)
                size = truncate_at
                if size == 0:
                    return 0

            previous = self._previous_newline(handle, size - 1)
            start = previous + 1
            handle.seek(start)
            line = handle.read(size - 1 - start)
            try:
                raw = json.loads(line.decode("utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("event line must be a JSON object")
                event = EventEnvelope.from_dict(raw)
            except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                raise CorruptEventStream(f"invalid last event in {path}: {error}") from error
            if event.stream_id != stream_id:
                raise CorruptEventStream(
                    f"event stream mismatch in {path}: {event.stream_id!r} != {stream_id!r}"
                )
            return event.seq

    def _stream_lock(
        self, stream_id: str, signals: _StreamLockSignals
    ) -> AbstractContextManager[None]:
        return exclusive_file_lock(
            self._lock_path(stream_id),
            timeout=self.lock_timeout,
            cancel_event=signals.cancel,
            waiting_event=signals.waiting,
        )

    def _read_unlocked(self, stream_id: str) -> tuple[EventEnvelope, ...]:
        path = self._path(stream_id)
        if not path.exists():
            return ()

        events: list[EventEnvelope] = []
        last_good_offset = 0
        with path.open("rb") as handle:
            while True:
                start = handle.tell()
                line = handle.readline()
                if not line:
                    break
                if not line.strip():
                    last_good_offset = handle.tell()
                    continue
                try:
                    raw = json.loads(line.decode("utf-8"))
                    if not isinstance(raw, dict):
                        raise ValueError("event line must be a JSON object")
                    event = EventEnvelope.from_dict(raw)
                except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                    is_partial_tail = handle.tell() == path.stat().st_size and not line.endswith(b"\n")
                    if is_partial_tail and self.repair_partial_tail:
                        with path.open("r+b") as repair:
                            repair.truncate(last_good_offset)
                        break
                    raise CorruptEventStream(
                        f"invalid event at byte {start} in {path}: {error}"
                    ) from error
                if event.stream_id != stream_id:
                    raise CorruptEventStream(
                        f"event stream mismatch in {path}: {event.stream_id!r} != {stream_id!r}"
                    )
                expected = len(events) + 1
                if event.seq != expected:
                    raise CorruptEventStream(
                        f"non-contiguous seq in {path}: expected {expected}, got {event.seq}"
                    )
                events.append(event)
                last_good_offset = handle.tell()
        return tuple(events)

    def _append_unlocked(
        self,
        stream_id: str,
        expected_seq: int,
        pending_events: tuple[PendingEvent, ...],
        durability: Durability,
    ) -> tuple[EventEnvelope, ...]:
        current_seq = self._head_unlocked(stream_id)
        if current_seq != expected_seq:
            raise ConcurrencyConflict(
                f"stream {stream_id!r} expected seq {expected_seq}, "
                f"current seq is {current_seq}"
            )
        materialized = tuple(
            EventEnvelope.materialize(stream_id, current_seq + index, event)
            for index, event in enumerate(pending_events, start=1)
        )
        path = self._path(stream_id)
        with path.open("ab") as output:
            for event in materialized:
                line = json.dumps(
                    event.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                output.write(line + b"\n")
            output.flush()
            if durability is Durability.SYNC:
                os.fsync(output.fileno())
        return materialized

    def _new_lock_signals(self) -> _StreamLockSignals:
        return _StreamLockSignals()

    def _call_locked(
        self,
        stream_id: str,
        work: Callable[[], _T],
        signals: _StreamLockSignals,
    ) -> _T:
        """Run ``work`` under the stream lock; executed on a worker thread."""

        try:
            with self._stream_lock(stream_id, signals):
                if signals.cancel.is_set():
                    # Cancelled while waiting; the critical section never began.
                    raise FileLockCancelled(f"stream {stream_id!r} operation was cancelled")
                return work()
        finally:
            # Runs after the lock has been released, so an observer that sees
            # ``finished`` knows the stream is free and untouched from here on.
            signals.finished.set()

    async def _run_locked(self, stream_id: str, work: Callable[[], _T]) -> _T:
        """Await ``work`` under the stream lock with real cancellation support.

        A thread cannot be killed, so cancellation is cooperative in two stages:
        the lock wait is abandoned through the cancel token, and the coroutine
        then waits for the worker to converge before letting ``CancelledError``
        reach the caller. No detached thread can therefore still be waiting for,
        or writing to, the stream after ``append``/``read``/``head`` returns.
        Convergence survives repeated cancellation - a second, third or tenth
        ``cancel()`` cannot release the caller while the worker is still alive.

        If cancellation arrives once the uninterruptible critical section is
        already running, that section completes atomically: the caller still
        sees ``CancelledError``, but the file has stopped changing before it
        resumes.
        """

        signals = self._new_lock_signals()
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(None, self._call_locked, stream_id, work, signals)
        try:
            # Shielded so that cancelling this coroutine does not detach the
            # worker; convergence is handled explicitly below.
            return await asyncio.shield(future)
        except asyncio.CancelledError as cancellation:
            signals.cancel.set()
            await await_worker_convergence(future)
            raise cancellation

    async def append(
        self,
        stream_id: str,
        *,
        expected_seq: int,
        events: tuple[PendingEvent, ...],
        durability: Durability = Durability.SYNC,
    ) -> tuple[EventEnvelope, ...]:
        if not events:
            return ()
        async with self._locks[stream_id]:
            # The whole critical section - head lookup, partial-tail repair,
            # expected_seq check, write and flush - runs under one exclusive lock.
            return await self._run_locked(
                stream_id,
                partial(self._append_unlocked, stream_id, expected_seq, events, durability),
            )

    async def read(
        self,
        stream_id: str,
        *,
        from_seq: int = 1,
    ) -> tuple[EventEnvelope, ...]:
        async with self._locks[stream_id]:
            # Exclusive because a partial-tail repair may truncate the stream.
            events = await self._run_locked(
                stream_id, partial(self._read_unlocked, stream_id)
            )
        return tuple(event for event in events if event.seq >= from_seq)

    async def head(self, stream_id: str) -> int:
        async with self._locks[stream_id]:
            # Exclusive because the head lookup may repair a partial tail.
            return await self._run_locked(stream_id, partial(self._head_unlocked, stream_id))

    async def list_streams(self, *, prefix: str | None = None) -> tuple[str, ...]:
        def scan() -> tuple[str, ...]:
            result = []
            for path in self.root.glob("*.jsonl"):
                stream_id = unquote(path.stem)
                if prefix is None or stream_id.startswith(prefix):
                    result.append(stream_id)
            return tuple(sorted(result))

        return await asyncio.to_thread(scan)
