"""Append-only event store protocol and in-memory implementation."""

from __future__ import annotations

import asyncio
from enum import Enum
from typing import Protocol

from traceh.api.events import EventEnvelope, PendingEvent, detach_event


class Durability(str, Enum):
    SYNC = "sync"
    BATCHED = "batched"


class ConcurrencyConflict(RuntimeError):
    pass


class EventStore(Protocol):
    """Append-only event storage.

    Every backend owes callers the same event-ownership contract, because a
    replaceable store must not change what its callers may safely do with an
    event. `EventEnvelope` is frozen but its ``data`` graph is not, so this
    cannot be left to the type system:

    - ``append()`` and ``read()`` return **caller-owned** envelopes;
    - mutating a returned envelope's nested payload must not change the history
      the store has saved;
    - nor may it change the result of any other ``read()``, before or after;
    - this holds regardless of how the backend stores events.

    ``head()`` returns a sequence number and owns nothing. `detach_event()` is
    the shared way to satisfy the contract when a backend hands out objects it
    also keeps.
    """

    async def append(
        self,
        stream_id: str,
        *,
        expected_seq: int,
        events: tuple[PendingEvent, ...],
        durability: Durability = Durability.SYNC,
    ) -> tuple[EventEnvelope, ...]:
        """Append ``events``, returning the persisted, caller-owned envelopes.

        Raises `ConcurrencyConflict` when ``expected_seq`` does not match the
        current stream head. Editing either the passed `PendingEvent` payloads
        or the returned envelopes leaves stored history unchanged.
        """
        ...

    async def read(
        self,
        stream_id: str,
        *,
        from_seq: int = 1,
    ) -> tuple[EventEnvelope, ...]:
        """Read events from ``from_seq``, as caller-owned envelopes.

        Editing a returned envelope must not affect stored history or any other
        read's result, so two reads never share a mutable payload.
        """
        ...

    async def head(self, stream_id: str) -> int:
        ...

    async def list_streams(self, *, prefix: str | None = None) -> tuple[str, ...]:
        ...


class InMemoryEventStore:
    """In-memory `EventStore`.

    It meets the ownership contract documented on `EventStore` by detaching
    explicitly, because it is the case where the danger is real: history lives
    in ``_streams`` as the very objects it returns. Without `detach_event()` an
    event handed to a caller is a live handle into stored history, and a caller
    editing its own payload silently rewrites the past. A JSONL store reaches
    the same contract differently - its history is a file, so reads reconstruct
    envelopes rather than expose them.
    """

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
            # `materialize()` already detached the payloads from the caller's
            # PendingEvents, but those objects are now stored history: hand
            # back copies instead of references into the stream.
            return tuple(detach_event(event) for event in materialized)

    async def read(
        self,
        stream_id: str,
        *,
        from_seq: int = 1,
    ) -> tuple[EventEnvelope, ...]:
        return tuple(
            detach_event(event)
            for event in self._streams.get(stream_id, ())
            if event.seq >= from_seq
        )

    async def head(self, stream_id: str) -> int:
        stream = self._streams.get(stream_id, ())
        return stream[-1].seq if stream else 0

    async def list_streams(self, *, prefix: str | None = None) -> tuple[str, ...]:
        streams = sorted(self._streams)
        if prefix is not None:
            streams = [stream for stream in streams if stream.startswith(prefix)]
        return tuple(streams)
