"""Append-only event store protocol and in-memory implementation."""

from __future__ import annotations

import asyncio
from enum import Enum
from typing import Protocol

from traceh.api.events import EventEnvelope, PendingEvent


class Durability(str, Enum):
    SYNC = "sync"
    BATCHED = "batched"


class ConcurrencyConflict(RuntimeError):
    pass


class EventStore(Protocol):
    async def append(
        self,
        stream_id: str,
        *,
        expected_seq: int,
        events: tuple[PendingEvent, ...],
        durability: Durability = Durability.SYNC,
    ) -> tuple[EventEnvelope, ...]:
        ...

    async def read(
        self,
        stream_id: str,
        *,
        from_seq: int = 1,
    ) -> tuple[EventEnvelope, ...]:
        ...

    async def head(self, stream_id: str) -> int:
        ...

    async def list_streams(self, *, prefix: str | None = None) -> tuple[str, ...]:
        ...


class InMemoryEventStore:
    def __init__(self) -> None:
        self._streams: dict[str, list[EventEnvelope]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, stream_id: str) -> asyncio.Lock:
        return self._locks.setdefault(stream_id, asyncio.Lock())

    async def append(
        self,
        stream_id: str,
        *,
        expected_seq: int,
        events: tuple[PendingEvent, ...],
        durability: Durability = Durability.SYNC,
    ) -> tuple[EventEnvelope, ...]:
        del durability
        async with self._lock(stream_id):
            stream = self._streams.setdefault(stream_id, [])
            current = stream[-1].seq if stream else 0
            if current != expected_seq:
                raise ConcurrencyConflict(
                    f"stream {stream_id!r} expected seq {expected_seq}, current seq is {current}"
                )
            materialized = tuple(
                EventEnvelope.materialize(stream_id, current + index, event)
                for index, event in enumerate(events, start=1)
            )
            stream.extend(materialized)
            return materialized

    async def read(
        self,
        stream_id: str,
        *,
        from_seq: int = 1,
    ) -> tuple[EventEnvelope, ...]:
        return tuple(event for event in self._streams.get(stream_id, ()) if event.seq >= from_seq)

    async def head(self, stream_id: str) -> int:
        stream = self._streams.get(stream_id, ())
        return stream[-1].seq if stream else 0

    async def list_streams(self, *, prefix: str | None = None) -> tuple[str, ...]:
        streams = sorted(self._streams)
        if prefix is not None:
            streams = [stream for stream in streams if stream.startswith(prefix)]
        return tuple(streams)
