"""In-process observation channel for events an `EventStore` has accepted.

The event log stays the only source of truth. This module adds a way to *watch*
it as it grows, so a user interface can react during a turn instead of after it,
and it is deliberately weaker than the log in every respect:

* **Adds no persisted fact.** Nothing here is written, replayed or recovered
  from, and no new event type is introduced.
* **No extra crash durability.** An event reaches the feed once the inner
  ``append()`` returned normally *for the `Durability` the caller asked for*.
  The feed neither upgrades `Durability.BATCHED` to `SYNC` nor adds any
  guarantee of its own: whether a published event survives an OS crash is
  decided entirely by the store's own contract. A crash between the append
  returning and the publish loses the notification, and leaves the event exactly
  as durable as its requested `Durability` made it.
* **Not history.** Subscribing never replays the past. A subscriber sees events
  published after it subscribed; anything earlier comes from
  ``EventStore.read()`` as before.
* **Not state.** No projection or cache is kept here. Subscribers that need
  state derive it themselves.
* **Not cross-process.** Another process writing the same JSONL file publishes
  nothing into this feed. Visibility is limited to one interpreter.

Because of all that, `Recovery`, `Inspector`, invariants and the runtime keep
reading the store and never consult the feed.

The publishing boundary is an `EventStore` decorator rather than a hook inside
`SessionService`, for two reasons. It is backend-agnostic, so it behaves
identically over `InMemoryEventStore` and `JsonlEventStore`; and it sits at the
one place where "the store accepted this event" becomes true, so announcing
something the store never accepted is not expressible.

Publishing is deliberately *not* part of the surface consumers get. `EventFeed`
is the read-only interface handed to observers; only `PublishingEventStore`, in
this module, can feed it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Protocol

from traceh.api.events import EventEnvelope, PendingEvent, detach_event
from traceh.session.event_store import Durability, EventStore

#: Queue sentinel marking a closed subscription. Queued events published before
#: the close are still delivered ahead of it, which is what lets a caller close
#: a subscription and then drain it without racing the writer.
_END = object()


class EventFeed(Protocol):
    """The read-only side of an event feed: what an observer is allowed to do.

    Consumers - the CLI timeline today - receive this, not `SessionEventFeed`.
    The distinction is the point: a surface with a public ``publish`` would let
    any holder inject an envelope the store never accepted, and a subscriber
    could not tell it apart from a real one. Keeping publication out of the
    consumer interface makes "only what the store accepted is published" a
    property of the API shape rather than a rule observers must respect.

    An underscore is not a sandbox; it is a stated boundary of authority.
    """

    def subscribe(self, stream_id: str) -> EventSubscription:
        """Watch one stream from now on. No history is replayed."""
        ...

    def subscriber_count(self, stream_id: str) -> int:
        """How many open subscriptions a stream has. Diagnostics only."""
        ...


class EventSubscription:
    """One reader's view of one stream.

    Events arrive through an unbounded queue, so a subscriber that consumes
    slowly delays nobody: publishing never awaits a consumer, and an append is
    never held up by a renderer. The cost of that choice is stated rather than
    hidden - see `SessionEventFeed`.
    """

    def __init__(self, feed: SessionEventFeed, stream_id: str) -> None:
        self._feed = feed
        self._stream_id = stream_id
        self._queue: asyncio.Queue[object] = asyncio.Queue()
        self._closed = False
        self._exhausted = False

    @property
    def stream_id(self) -> str:
        return self._stream_id

    @property
    def closed(self) -> bool:
        return self._closed

    def _offer(self, event: EventEnvelope) -> None:
        """Hand one event to this subscriber. Never blocks, never raises."""

        if self._closed:
            return
        self._queue.put_nowait(event)

    def close(self) -> None:
        """Stop receiving. Already-queued events remain deliverable.

        Idempotent, and safe from any exit path - normal return, exception,
        cancellation - which is what keeps a chat session from leaking
        subscriptions.
        """

        if self._closed:
            return
        self._closed = True
        self._feed._unsubscribe(self)
        self._queue.put_nowait(_END)

    async def __aiter__(self) -> AsyncIterator[EventEnvelope]:
        """Yield events until the subscription is closed and drained.

        Iterating an already finished subscription yields nothing instead of
        waiting forever. There is exactly one end marker, so without this a
        second pass would await a marker that the first pass consumed - a
        deadlock produced by ordinary defensive code such as draining twice.
        """

        if self._exhausted:
            return
        while True:
            item = await self._queue.get()
            if item is _END:
                self._exhausted = True
                return
            assert isinstance(item, EventEnvelope)
            yield item

    async def __aenter__(self) -> EventSubscription:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        self.close()


class SessionEventFeed:
    """Fans store-accepted events out to in-process subscribers, per stream.

    Implements `EventFeed` for consumers and adds the private publication side
    used only by `PublishingEventStore`. Hand `EventFeed` to observers; hand this
    class only to the store that owns it.

    Ownership: every subscriber receives its **own** `detach_event()` copy. The
    store's ownership contract only covers what `append()`/`read()` hand back;
    fanning one envelope out to several readers would give them a shared mutable
    payload, so the copy happens once per subscriber rather than once per
    publish.

    Unbounded queues are a deliberate, documented trade:

    * no backpressure is applied to the runtime - a subscriber can never slow
      down or fail a store append;
    * an abandoned or permanently slow subscriber grows in memory, bounded only
      by how many events the session produces;
    * the chat lifecycle closes its subscription on every exit path, so the
      shipped consumer does not leak;
    * a future bounded queue must define explicit overflow semantics - dropping
      events silently would make the timeline lie about what happened.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[EventSubscription]] = {}

    def subscribe(self, stream_id: str) -> EventSubscription:
        """Watch one stream from now on. No history is replayed."""

        subscription = EventSubscription(self, stream_id)
        self._subscribers.setdefault(stream_id, []).append(subscription)
        return subscription

    def _unsubscribe(self, subscription: EventSubscription) -> None:
        readers = self._subscribers.get(subscription.stream_id)
        if not readers:
            return
        if subscription in readers:
            readers.remove(subscription)
        if not readers:
            del self._subscribers[subscription.stream_id]

    def subscriber_count(self, stream_id: str) -> int:
        """How many open subscriptions a stream has. For tests and diagnostics."""

        return len(self._subscribers.get(stream_id, ()))

    def _publish(self, events: tuple[EventEnvelope, ...]) -> None:
        """Deliver store-accepted events, in the order given.

        Private on purpose: see `EventFeed`. `PublishingEventStore` is the only
        supported caller, and it is what guarantees the two preconditions this
        method cannot check for itself - that the append succeeded, and that the
        events arrive in sequence order.
        """

        for event in events:
            readers = self._subscribers.get(event.stream_id)
            if not readers:
                continue
            # Snapshot: a subscriber closing during the fan-out must not
            # reshape the list being iterated.
            for subscription in tuple(readers):
                subscription._offer(detach_event(event))


class PublishingEventStore:
    """An `EventStore` that also announces what the inner store just accepted.

    Delegates everything, adds nothing to the persisted record, and introduces
    no new event types. Two properties matter and both come from holding one
    per-stream lock across "append then publish":

    **Accepted before visible.** Publishing happens only after the wrapped store
    returns normally, so a `ConcurrencyConflict`, a serialization failure or any
    other append error publishes exactly nothing. Cancellation likewise publishes
    nothing, including on the may-have-committed path where the event did land -
    the feed is allowed to miss events, and the log is not.

    This is *acceptance*, not extra durability. ``durability`` is passed through
    unchanged, so a `Durability.BATCHED` append is announced once the store
    returns from a flushed-but-not-fsynced write, exactly as its caller asked
    for; how such an event fares in an OS crash is the store's contract, not the
    feed's. The feed never upgrades `BATCHED` to `SYNC` and never makes an event
    more durable than the writer requested.

    **Sequence order, not completion order.** Two appends racing on one stream
    would otherwise be free to publish in either order: the store serializes the
    writes, but the callers resume independently, so the writer of seq 10 could
    be descheduled and publish after the writer of seq 11. Holding the lock
    across both halves makes "published in seq order" structural rather than a
    hopeful consequence of how callers happen to be scheduled today. Different
    streams take different locks and never wait on each other.

    Publishing itself only puts objects on unbounded queues - it never awaits -
    so the lock is released promptly and no subscriber can extend it.
    """

    def __init__(self, inner: EventStore, feed: SessionEventFeed) -> None:
        self.inner = inner
        self.feed = feed
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
        async with self._lock(stream_id):
            appended = await self.inner.append(
                stream_id,
                expected_seq=expected_seq,
                events=events,
                durability=durability,
            )
            # Reached only when the append succeeded, and still inside the lock,
            # so no later append for this stream can publish ahead of it.
            self.feed._publish(appended)
            return appended

    async def read(
        self,
        stream_id: str,
        *,
        from_seq: int = 1,
    ) -> tuple[EventEnvelope, ...]:
        return await self.inner.read(stream_id, from_seq=from_seq)

    async def head(self, stream_id: str) -> int:
        return await self.inner.head(stream_id)

    async def list_streams(self, *, prefix: str | None = None) -> tuple[str, ...]:
        return await self.inner.list_streams(prefix=prefix)
