"""Contract for the in-process event feed.

The feed is an observation channel bolted onto a durable log, so its tests are
mostly about what it must *not* do: publish something that was never persisted,
reorder a stream, hand two subscribers the same mutable payload, or let a slow
reader reach back into the runtime.
"""

from __future__ import annotations

import asyncio

import pytest

from traceh.api.events import EventEnvelope, PendingEvent
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.session.event_feed import EventFeed, PublishingEventStore, SessionEventFeed
from traceh.session.event_store import (
    ConcurrencyConflict,
    Durability,
    EventStore,
    InMemoryEventStore,
)
from traceh.session.sqlite import SqliteEventStore

STREAM = "session:feed"
OTHER_STREAM = "session:other"


@pytest.fixture(params=["in_memory", "sqlite"])
async def store(request: pytest.FixtureRequest, tmp_path):
    inner: EventStore = (
        InMemoryEventStore() if request.param == "in_memory" else SqliteEventStore(tmp_path)
    )
    yield PublishingEventStore(inner, SessionEventFeed())
    if isinstance(inner, SqliteEventStore):
        await inner.aclose()


def payload() -> dict:
    return {"top": "original", "nested": {"value": "original"}, "items": [{"value": "original"}]}


async def append(
    store: PublishingEventStore,
    *,
    expected_seq: int,
    count: int = 1,
    stream_id: str = STREAM,
) -> tuple[EventEnvelope, ...]:
    return await store.append(
        stream_id,
        expected_seq=expected_seq,
        events=tuple(PendingEvent("feed/event", payload()) for _ in range(count)),
    )


async def collect(subscription, *, limit: int) -> list[EventEnvelope]:
    """Take exactly ``limit`` events, failing fast instead of hanging forever."""

    received: list[EventEnvelope] = []

    async def pump() -> None:
        async for event in subscription:
            received.append(event)
            if len(received) >= limit:
                return

    await asyncio.wait_for(pump(), timeout=5)
    return received


async def test_events_are_published_only_after_a_successful_append(
    store: PublishingEventStore,
) -> None:
    subscription = store.feed.subscribe(STREAM)

    await append(store, expected_seq=0)

    received = await collect(subscription, limit=1)
    assert [event.seq for event in received] == [1]
    assert received[0].type == "feed/event"
    subscription.close()


async def test_a_rejected_append_publishes_nothing(store: PublishingEventStore) -> None:
    subscription = store.feed.subscribe(STREAM)
    await append(store, expected_seq=0)
    await collect(subscription, limit=1)

    with pytest.raises(ConcurrencyConflict):
        await append(store, expected_seq=0)

    # Nothing further is queued: closing then draining must yield no events.
    subscription.close()
    assert [event async for event in subscription] == []
    # The rejected append changed nothing about the stream either.
    assert await store.head(STREAM) == 1


async def test_one_batch_publishes_in_sequence_order(store: PublishingEventStore) -> None:
    subscription = store.feed.subscribe(STREAM)

    await append(store, expected_seq=0, count=4)

    received = await collect(subscription, limit=4)
    assert [event.seq for event in received] == [1, 2, 3, 4]
    subscription.close()


async def test_concurrent_appends_never_publish_out_of_order(
    store: PublishingEventStore,
) -> None:
    """Racing writers must not be able to invert the stream a reader sees.

    Each writer reads the head and appends, so they contend for real; whichever
    loses retries. The published order must still equal the persisted order.
    """

    subscription = store.feed.subscribe(STREAM)
    total = 12

    async def writer() -> None:
        for _ in range(total // 3):
            while True:
                head = await store.head(STREAM)
                try:
                    await append(store, expected_seq=head)
                except ConcurrencyConflict:
                    continue
                break

    await asyncio.gather(writer(), writer(), writer())

    received = await collect(subscription, limit=total)
    published = [event.seq for event in received]
    assert published == sorted(published), f"feed published out of order: {published}"
    assert published == list(range(1, total + 1))
    stored = [event.seq for event in await store.read(STREAM)]
    assert published == stored
    subscription.close()


async def test_two_subscribers_both_receive_the_same_events(
    store: PublishingEventStore,
) -> None:
    first = store.feed.subscribe(STREAM)
    second = store.feed.subscribe(STREAM)

    await append(store, expected_seq=0, count=2)

    assert [e.seq for e in await collect(first, limit=2)] == [1, 2]
    assert [e.seq for e in await collect(second, limit=2)] == [1, 2]
    first.close()
    second.close()


async def test_two_subscribers_do_not_share_mutable_payloads(
    store: PublishingEventStore,
) -> None:
    """Fan-out is where the store's ownership contract would otherwise end."""

    first = store.feed.subscribe(STREAM)
    second = store.feed.subscribe(STREAM)
    await append(store, expected_seq=0)

    a = (await collect(first, limit=1))[0]
    b = (await collect(second, limit=1))[0]

    a.data["top"] = "mutated"
    a.data["nested"]["value"] = "mutated"
    a.data["items"][0]["value"] = "mutated"

    assert b.data == payload()
    first.close()
    second.close()


async def test_subscriber_mutation_does_not_reach_the_store(
    store: PublishingEventStore,
) -> None:
    subscription = store.feed.subscribe(STREAM)
    await append(store, expected_seq=0)

    event = (await collect(subscription, limit=1))[0]
    event.data["nested"]["value"] = "mutated"
    event.data["items"][0]["value"] = "mutated"

    assert (await store.read(STREAM))[0].data == payload()
    subscription.close()


async def test_a_later_subscriber_is_unaffected_by_an_earlier_one(
    store: PublishingEventStore,
) -> None:
    first = store.feed.subscribe(STREAM)
    await append(store, expected_seq=0)
    mutated = (await collect(first, limit=1))[0]
    mutated.data["nested"]["value"] = "mutated"
    first.close()

    later = store.feed.subscribe(STREAM)
    await append(store, expected_seq=1)
    assert (await collect(later, limit=1))[0].data == payload()
    later.close()


async def test_closing_a_subscription_stops_delivery(store: PublishingEventStore) -> None:
    subscription = store.feed.subscribe(STREAM)
    await append(store, expected_seq=0)
    await collect(subscription, limit=1)

    subscription.close()
    assert store.feed.subscriber_count(STREAM) == 0

    await append(store, expected_seq=1)
    received: list[EventEnvelope] = []
    async for event in subscription:
        received.append(event)
    assert received == []


async def test_closing_twice_is_safe(store: PublishingEventStore) -> None:
    subscription = store.feed.subscribe(STREAM)
    subscription.close()
    subscription.close()
    assert subscription.closed
    assert store.feed.subscriber_count(STREAM) == 0


async def test_queued_events_survive_close_and_can_still_be_drained(
    store: PublishingEventStore,
) -> None:
    """Close-then-drain is what lets chat promise "timeline before answer"."""

    subscription = store.feed.subscribe(STREAM)
    await append(store, expected_seq=0, count=3)

    subscription.close()
    drained = [event.seq async for event in subscription]
    assert drained == [1, 2, 3]


async def test_draining_a_finished_subscription_twice_does_not_deadlock(
    store: PublishingEventStore,
) -> None:
    """Defensive double-draining must be harmless, not a hang.

    There is one end marker, so a second pass would otherwise wait forever for a
    marker the first pass already consumed.
    """

    subscription = store.feed.subscribe(STREAM)
    await append(store, expected_seq=0)
    subscription.close()

    assert [event.seq for event in [e async for e in subscription]] == [1]
    second = await asyncio.wait_for(
        _drain(subscription),
        timeout=5,
    )
    assert second == []


async def _drain(subscription) -> list[EventEnvelope]:
    return [event async for event in subscription]


async def test_a_slow_subscriber_does_not_block_appends(
    store: PublishingEventStore,
) -> None:
    """A reader that never reads must not stall the writer."""

    stalled = store.feed.subscribe(STREAM)  # never consumed

    async def write_many() -> None:
        for seq in range(20):
            await append(store, expected_seq=seq)

    await asyncio.wait_for(write_many(), timeout=5)
    assert await store.head(STREAM) == 20

    # The events really were queued for the stalled reader, not dropped.
    stalled.close()
    assert len([event async for event in stalled]) == 20


async def test_streams_are_strictly_isolated(store: PublishingEventStore) -> None:
    watched = store.feed.subscribe(STREAM)

    await append(store, expected_seq=0, stream_id=OTHER_STREAM, count=3)
    await append(store, expected_seq=0, stream_id=STREAM)

    watched.close()
    received = [event async for event in watched]
    assert [event.stream_id for event in received] == [STREAM]
    assert [event.seq for event in received] == [1]


async def test_effect_stream_events_do_not_leak_into_a_session_subscription(
    store: PublishingEventStore,
) -> None:
    session = store.feed.subscribe("session:s1")

    await append(store, expected_seq=0, stream_id="effects:s1", count=2)
    await append(store, expected_seq=0, stream_id="session:s1")

    session.close()
    assert [event.stream_id for event in [e async for e in session]] == ["session:s1"]


async def test_a_broken_subscriber_cannot_break_a_durable_append(
    store: PublishingEventStore,
) -> None:
    """A consumer that raises does so in its own task, never inside append."""

    subscription = store.feed.subscribe(STREAM)

    async def angry_consumer() -> None:
        async for _event in subscription:
            raise RuntimeError("subscriber exploded")

    consumer = asyncio.create_task(angry_consumer())
    await append(store, expected_seq=0)
    with pytest.raises(RuntimeError, match="subscriber exploded"):
        await asyncio.wait_for(consumer, timeout=5)

    # The append succeeded and the store keeps working afterwards.
    assert await store.head(STREAM) == 1
    await append(store, expected_seq=1)
    assert [event.seq for event in await store.read(STREAM)] == [1, 2]


async def test_publishing_adds_no_events_and_no_new_types(
    store: PublishingEventStore,
) -> None:
    subscription = store.feed.subscribe(STREAM)
    await append(store, expected_seq=0, count=2)
    await collect(subscription, limit=2)
    subscription.close()

    stored = await store.read(STREAM)
    assert [event.type for event in stored] == ["feed/event", "feed/event"]
    assert await store.head(STREAM) == 2


async def test_subscribing_replays_no_history(store: PublishingEventStore) -> None:
    await append(store, expected_seq=0, count=3)

    late = store.feed.subscribe(STREAM)
    late.close()
    assert [event async for event in late] == []

    # History remains available the documented way.
    assert len(await store.read(STREAM)) == 3


async def test_the_consumer_interface_cannot_publish(store: PublishingEventStore) -> None:
    """An observer must not be able to announce an event the store never took.

    A public ``publish`` would let any holder of the feed inject a forged
    envelope that subscribers could not distinguish from a real one, which would
    make "only what the store accepted is published" a convention rather than a
    property of the API.
    """

    feed: EventFeed = store.feed

    assert not hasattr(feed, "publish")
    assert isinstance(store.feed, SessionEventFeed)
    # The read-only surface is exactly subscribe + diagnostics.
    assert callable(feed.subscribe)
    assert callable(feed.subscriber_count)


async def test_a_forged_event_cannot_be_injected_through_the_feed(
    store: PublishingEventStore,
) -> None:
    """Only the store's own append path reaches a subscriber."""

    subscription = store.feed.subscribe(STREAM)
    forged = EventEnvelope.materialize(
        STREAM, 999, PendingEvent("feed/forged", {"injected": True})
    )

    # Nothing on the consumer interface accepts an envelope.
    publishers = [
        name
        for name in dir(store.feed)
        if not name.startswith("_") and "publish" in name.lower()
    ]
    assert publishers == [], f"public publication surface exists: {publishers}"

    # A real append still works and is the only thing that arrives.
    await append(store, expected_seq=0)
    received = await collect(subscription, limit=1)
    assert [event.type for event in received] == ["feed/event"]
    assert forged.type not in [event.type for event in received]
    subscription.close()

    # The forged envelope never entered the log either.
    assert [event.type for event in await store.read(STREAM)] == ["feed/event"]


async def test_durability_is_passed_through_unchanged() -> None:
    """The feed passes the one current SYNC durability value through unchanged.

    Publishing means "the store accepted this for the durability its caller
    asked for", not "this is fsynced". A spy records what the inner store
    actually received.
    """

    class SpyStore:
        def __init__(self) -> None:
            self.inner = InMemoryEventStore()
            self.seen: list[Durability] = []

        async def append(self, stream_id, *, expected_seq, events, durability=Durability.SYNC):
            self.seen.append(durability)
            return await self.inner.append(
                stream_id, expected_seq=expected_seq, events=events, durability=durability
            )

        async def read(self, stream_id, *, from_seq=1):
            return await self.inner.read(stream_id, from_seq=from_seq)

        async def head(self, stream_id):
            return await self.inner.head(stream_id)

        async def list_streams(self, *, prefix=None):
            return await self.inner.list_streams(prefix=prefix)

    spy = SpyStore()
    store = PublishingEventStore(spy, SessionEventFeed())
    subscription = store.feed.subscribe(STREAM)

    await store.append(
        STREAM,
        expected_seq=0,
        events=(PendingEvent("feed/event", payload()),),
        durability=Durability.SYNC,
    )
    await store.append(
        STREAM,
        expected_seq=1,
        events=(PendingEvent("feed/event", payload()),),
        durability=Durability.SYNC,
    )

    assert spy.seen == [Durability.SYNC, Durability.SYNC]
    # Publication reports acceptance; it adds no second durability mode.
    assert [event.seq for event in await collect(subscription, limit=2)] == [1, 2]
    subscription.close()


async def test_the_wrapper_delegates_the_full_store_surface(
    store: PublishingEventStore,
) -> None:
    assert await store.head(STREAM) == 0
    assert await store.read(STREAM) == ()

    await append(store, expected_seq=0, count=2)
    await append(store, expected_seq=0, stream_id=OTHER_STREAM)

    assert await store.head(STREAM) == 2
    assert [event.seq for event in await store.read(STREAM, from_seq=2)] == [2]
    assert set(await store.list_streams(prefix="session:")) == {STREAM, OTHER_STREAM}


# -- runtime wiring ------------------------------------------------------


def build_runtime(tmp_path):
    return build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / ".traceh", provider="scripted", model="m"),
        event_store=InMemoryEventStore(),
    )


def test_the_runtime_feed_is_the_one_its_store_publishes_to(tmp_path) -> None:
    """A runtime must not expose a feed that is wired to nothing.

    Defaulting the feed would hand callers a subscribable object that stays
    silent forever - an interface that exists while the capability does not.
    """

    runtime = build_runtime(tmp_path)
    published_to = runtime.sessions.store.feed
    assert runtime.events is published_to


def test_agent_runtime_requires_a_feed() -> None:
    """The connection is mandatory, so it cannot be forgotten into silence."""

    import inspect

    from traceh.runtime.agent_runtime import AgentRuntime

    parameter = inspect.signature(AgentRuntime.__init__).parameters["events"]
    assert parameter.default is inspect.Parameter.empty


async def test_writing_through_the_runtime_reaches_a_runtime_subscriber(tmp_path) -> None:
    """The end-to-end proof that the exposed feed is live."""

    runtime = build_runtime(tmp_path)
    session_id = await runtime.create_session(tmp_path)
    stream = runtime.sessions.session_stream(session_id)

    subscription = runtime.events.subscribe(stream)
    await runtime.sessions.append_session(session_id, "feed/probe", {"hello": "world"})

    received = await collect(subscription, limit=1)
    assert [event.type for event in received] == ["feed/probe"]
    subscription.close()


async def test_the_runtime_feed_is_not_a_publisher(tmp_path) -> None:
    runtime = build_runtime(tmp_path)
    assert not hasattr(runtime.events, "publish")
