"""Crash-tolerant JSONL event store."""

from __future__ import annotations

import asyncio
import json
import os
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote, unquote

from traceh.api.events import EventEnvelope, PendingEvent
from traceh.session.event_store import ConcurrencyConflict, Durability

try:  # pragma: no cover - Windows fallback is intentionally simple
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


class CorruptEventStream(RuntimeError):
    pass


class JsonlEventStore:
    def __init__(self, root: Path, *, repair_partial_tail: bool = True) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.repair_partial_tail = repair_partial_tail
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

    def _head_locked_sync(self, stream_id: str) -> int:
        lock_path = self._lock_path(stream_id)
        lock_path.touch(exist_ok=True)
        with lock_path.open("rb") as lock_handle:
            if fcntl is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                return self._head_unlocked(stream_id)
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

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

    def _read_locked_sync(self, stream_id: str) -> tuple[EventEnvelope, ...]:
        lock_path = self._lock_path(stream_id)
        lock_path.touch(exist_ok=True)
        with lock_path.open("rb") as lock_handle:
            if fcntl is not None:
                # Exclusive because a partial-tail repair may truncate the stream.
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                return self._read_unlocked(stream_id)
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def _append_sync(
        self,
        stream_id: str,
        expected_seq: int,
        pending_events: tuple[PendingEvent, ...],
        durability: Durability,
    ) -> tuple[EventEnvelope, ...]:
        lock_path = self._lock_path(stream_id)
        lock_path.touch(exist_ok=True)
        with lock_path.open("rb") as lock_handle:
            if fcntl is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                current_seq = self._head_unlocked(stream_id)
                if current_seq != expected_seq:
                    raise ConcurrencyConflict(
                        f"stream {stream_id!r} expected seq {expected_seq}, current seq is {current_seq}"
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
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

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
            return await asyncio.to_thread(
                self._append_sync,
                stream_id,
                expected_seq,
                events,
                durability,
            )

    async def read(
        self,
        stream_id: str,
        *,
        from_seq: int = 1,
    ) -> tuple[EventEnvelope, ...]:
        async with self._locks[stream_id]:
            events = await asyncio.to_thread(self._read_locked_sync, stream_id)
        return tuple(event for event in events if event.seq >= from_seq)

    async def head(self, stream_id: str) -> int:
        async with self._locks[stream_id]:
            return await asyncio.to_thread(self._head_locked_sync, stream_id)

    async def list_streams(self, *, prefix: str | None = None) -> tuple[str, ...]:
        def scan() -> tuple[str, ...]:
            result = []
            for path in self.root.glob("*.jsonl"):
                stream_id = unquote(path.stem)
                if prefix is None or stream_id.startswith(prefix):
                    result.append(stream_id)
            return tuple(sorted(result))

        return await asyncio.to_thread(scan)
