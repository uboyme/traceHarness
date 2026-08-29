from __future__ import annotations

import pytest

from traceh.api.events import PendingEvent
from traceh.session.event_store import ConcurrencyConflict, InMemoryEventStore
from traceh.session.sqlite import SqliteEventStore


@pytest.mark.asyncio
async def test_in_memory_store_enforces_expected_sequence() -> None:
    store = InMemoryEventStore()
    first = await store.append(
        "session:one",
        expected_seq=0,
        events=(PendingEvent("session/created", {"value": 1}),),
    )
    assert first[0].seq == 1

    with pytest.raises(ConcurrencyConflict):
        await store.append(
            "session:one",
            expected_seq=0,
            events=(PendingEvent("turn/start", {"turn_id": "t1"}),),
        )


@pytest.mark.asyncio
async def test_sqlite_store_round_trips_and_lists_sorted_prefixes(tmp_path) -> None:
    store = SqliteEventStore(tmp_path)
    try:
        for stream in ("workflow:z", "session:one", "workflow:a"):
            await store.append(
                stream,
                expected_seq=0,
                events=(PendingEvent("session/created", {"stream": stream}),),
            )

        events = await store.read("session:one")
        assert [event.type for event in events] == ["session/created"]
        assert events[0].data == {"stream": "session:one"}
        assert await store.list_streams() == (
            "session:one",
            "workflow:a",
            "workflow:z",
        )
        assert await store.list_streams(prefix="workflow:") == (
            "workflow:a",
            "workflow:z",
        )
    finally:
        await store.aclose()
