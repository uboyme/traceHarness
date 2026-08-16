from __future__ import annotations

import json

import pytest

from traceh.api.events import PendingEvent
from traceh.session.event_store import ConcurrencyConflict, InMemoryEventStore
from traceh.session.jsonl import JsonlEventStore


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
async def test_jsonl_store_round_trips_and_repairs_partial_tail(tmp_path) -> None:
    store = JsonlEventStore(tmp_path)
    await store.append(
        "session:one",
        expected_seq=0,
        events=(PendingEvent("session/created", {"name": "demo"}),),
    )
    stream_path = next(tmp_path.glob("*.jsonl"))
    with stream_path.open("ab") as handle:
        handle.write(b'{"partial":')

    events = await store.read("session:one")
    assert [event.type for event in events] == ["session/created"]
    lines = stream_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["seq"] == 1


@pytest.mark.asyncio
async def test_jsonl_head_repairs_tail_before_next_append(tmp_path) -> None:
    store = JsonlEventStore(tmp_path)
    await store.append(
        "session:head",
        expected_seq=0,
        events=(PendingEvent("session/created", {"value": 1}),),
    )
    stream_path = next(tmp_path.glob("*.jsonl"))
    with stream_path.open("ab") as handle:
        handle.write(b'{"event_id":"partial')

    assert await store.head("session:head") == 1
    appended = await store.append(
        "session:head",
        expected_seq=1,
        events=(PendingEvent("turn/start", {"turn_id": "t"}),),
    )
    assert appended[0].seq == 2
    assert [event.seq for event in await store.read("session:head")] == [1, 2]
