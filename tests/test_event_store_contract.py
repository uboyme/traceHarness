"""Event ownership contract shared by every `EventStore` implementation.

These tests pin down who owns an event's JSON graph. `EventEnvelope` is a
frozen dataclass, but that only stops its fields from being rebound - the
nested dicts and lists inside `data` stay mutable. So "the store cannot be
rewritten through an event it handed out" is a contract that has to be tested
by really mutating a nested structure and reading the store again, not by
asserting that two objects are merely not identical.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from traceh.api.events import EventEnvelope, PendingEvent, detach_event
from traceh.session.event_store import ConcurrencyConflict, EventStore, InMemoryEventStore
from traceh.session.jsonl import JsonlEventStore

STREAM = "session:contract"


def payload() -> dict:
    """A payload with every shape that can hide a shared reference."""

    return {
        "top": "original",
        "nested": {"value": "original", "deeper": {"value": "original"}},
        "items": ["original", {"value": "original"}],
    }


def assert_pristine(data: dict) -> None:
    """Fail unless ``data`` still holds exactly the values `payload()` wrote."""

    assert data == payload()


@pytest.fixture(params=["in_memory", "jsonl"])
def store(request: pytest.FixtureRequest, tmp_path) -> EventStore:
    if request.param == "in_memory":
        return InMemoryEventStore()
    return JsonlEventStore(tmp_path)


async def append_one(store: EventStore, data: dict, *, expected_seq: int = 0):
    appended = await store.append(
        STREAM,
        expected_seq=expected_seq,
        events=(PendingEvent("contract/event", data),),
    )
    return appended[0]


async def test_mutating_pending_input_does_not_change_stored_event(store: EventStore) -> None:
    """The caller keeps owning the PendingEvent it built; the store does not read it again."""

    data = payload()
    await append_one(store, data)

    data["top"] = "mutated"
    data["nested"]["value"] = "mutated"
    data["nested"]["deeper"]["value"] = "mutated"
    data["items"].append("appended")
    data["items"][1]["value"] = "mutated"

    stored = await store.read(STREAM)
    assert_pristine(stored[0].data)


async def test_mutating_append_result_does_not_change_store(store: EventStore) -> None:
    """An append result is the caller's copy, not a handle into stored history."""

    appended = await append_one(store, payload())

    appended.data["top"] = "mutated"
    appended.data["nested"]["value"] = "mutated"
    appended.data["nested"]["deeper"]["value"] = "mutated"
    appended.data["items"].append("appended")
    appended.data["items"][1]["value"] = "mutated"

    stored = await store.read(STREAM)
    assert_pristine(stored[0].data)


async def test_mutating_read_result_does_not_change_next_read(store: EventStore) -> None:
    """Reading is not a way to acquire write access to the log."""

    await append_one(store, payload())

    first = await store.read(STREAM)
    first[0].data["top"] = "mutated"
    first[0].data["nested"]["deeper"]["value"] = "mutated"
    first[0].data["items"][1]["value"] = "mutated"
    first[0].data["items"].append("appended")

    second = await store.read(STREAM)
    assert_pristine(second[0].data)


async def test_two_reads_do_not_share_mutable_event_data(store: EventStore) -> None:
    """Two readers must not be able to observe each other's edits."""

    await append_one(store, payload())

    first = await store.read(STREAM)
    second = await store.read(STREAM)

    first[0].data["nested"]["deeper"]["value"] = "mutated"
    first[0].data["items"][1]["value"] = "mutated"

    # The already-materialised second read is unaffected...
    assert_pristine(second[0].data)
    # ...and so is a fresh one.
    third = await store.read(STREAM)
    assert_pristine(third[0].data)


async def test_multiple_events_do_not_share_nested_input(store: EventStore) -> None:
    """Reusing one nested object across PendingEvents must not couple the events."""

    shared_nested = {"value": "original", "deeper": {"value": "original"}}
    shared_items = ["original", {"value": "original"}]
    first_input = {"top": "original", "nested": shared_nested, "items": shared_items}
    second_input = {"top": "original", "nested": shared_nested, "items": shared_items}

    appended = await store.append(
        STREAM,
        expected_seq=0,
        events=(
            PendingEvent("contract/event", first_input),
            PendingEvent("contract/event", second_input),
        ),
    )

    appended[0].data["nested"]["deeper"]["value"] = "mutated"
    appended[0].data["items"][1]["value"] = "mutated"

    assert_pristine(appended[1].data)
    stored = await store.read(STREAM)
    assert_pristine(stored[0].data)
    assert_pristine(stored[1].data)


async def test_detach_preserves_event_metadata(store: EventStore) -> None:
    """Detaching copies the payload only; identity and provenance survive unchanged."""

    event_id = uuid4()
    causation_id = uuid4()
    correlation_id = uuid4()
    occurred_at = datetime(2024, 5, 17, 8, 30, 45, 123456, tzinfo=UTC)
    pending = PendingEvent(
        "contract/event",
        payload(),
        schema_version=7,
        event_id=event_id,
        causation_id=causation_id,
        correlation_id=correlation_id,
        actor_id="actor-1",
        composition_revision="rev-abc",
        occurred_at=occurred_at,
    )

    original = EventEnvelope.materialize(STREAM, 1, pending)
    detached = detach_event(original)

    assert detached == original
    assert detached.data is not original.data
    for field, expected in (
        ("event_id", event_id),
        ("stream_id", STREAM),
        ("seq", 1),
        ("type", "contract/event"),
        ("schema_version", 7),
        ("occurred_at", occurred_at),
        ("causation_id", causation_id),
        ("correlation_id", correlation_id),
        ("actor_id", "actor-1"),
        ("composition_revision", "rev-abc"),
    ):
        assert getattr(detached, field) == expected

    # The same metadata must survive a real store round trip, where detaching
    # sits on the actual append/read path.
    await store.append(STREAM, expected_seq=0, events=(pending,))
    stored = (await store.read(STREAM))[0]
    assert stored.event_id == event_id
    assert stored.stream_id == STREAM
    assert stored.seq == 1
    assert stored.type == "contract/event"
    assert stored.schema_version == 7
    assert stored.occurred_at == occurred_at
    assert stored.causation_id == causation_id
    assert stored.correlation_id == correlation_id
    assert stored.actor_id == "actor-1"
    assert stored.composition_revision == "rev-abc"
    assert_pristine(stored.data)


async def test_store_failures_and_expected_seq_semantics_are_unchanged(store: EventStore) -> None:
    """Detachment must not disturb the append contract that guards the log."""

    assert await store.head(STREAM) == 0
    assert await store.read(STREAM) == ()

    await append_one(store, payload())
    assert await store.head(STREAM) == 1

    with pytest.raises(ConcurrencyConflict):
        await append_one(store, payload())

    # A rejected append leaves the stream exactly as it was.
    assert await store.head(STREAM) == 1
    assert len(await store.read(STREAM)) == 1

    second = await append_one(store, payload(), expected_seq=1)
    assert second.seq == 2
    assert await store.head(STREAM) == 2
    assert [event.seq for event in await store.read(STREAM)] == [1, 2]
    assert [event.seq for event in await store.read(STREAM, from_seq=2)] == [2]


async def test_to_dict_returns_a_detached_json_graph() -> None:
    """`to_dict()` output belongs to the caller; editing it cannot rewrite the event."""

    event = EventEnvelope.materialize(STREAM, 1, PendingEvent("contract/event", payload()))

    raw = event.to_dict()
    assert isinstance(raw["data"], dict)
    raw["data"]["top"] = "mutated"
    raw["data"]["nested"]["deeper"]["value"] = "mutated"
    raw["data"]["items"][1]["value"] = "mutated"
    raw["data"]["items"].append("appended")

    assert_pristine(event.data)


async def test_from_dict_detaches_the_input_json_graph() -> None:
    """A decoded event must not stay wired to the dict it was decoded from."""

    raw = {
        "event_id": str(uuid4()),
        "stream_id": STREAM,
        "seq": 1,
        "type": "contract/event",
        "schema_version": 1,
        "data": payload(),
        "occurred_at": "2024-05-17T08:30:45.123456+00:00",
    }

    event = EventEnvelope.from_dict(raw)

    raw["data"]["top"] = "mutated"
    raw["data"]["nested"]["deeper"]["value"] = "mutated"
    raw["data"]["items"][1]["value"] = "mutated"
    raw["data"]["items"].append("appended")

    assert_pristine(event.data)


async def test_from_dict_still_rejects_a_non_object_payload() -> None:
    """Detaching must not turn a malformed payload into a quietly coerced one."""

    raw = {
        "event_id": str(uuid4()),
        "stream_id": STREAM,
        "seq": 1,
        "type": "contract/event",
        "schema_version": 1,
        "data": ["not", "an", "object"],
        "occurred_at": "2024-05-17T08:30:45.123456+00:00",
    }

    with pytest.raises(ValueError, match="event.data must be an object"):
        EventEnvelope.from_dict(raw)


async def test_in_memory_and_jsonl_follow_the_same_detachment_contract(tmp_path) -> None:
    """The two stores must be indistinguishable to a caller that mutates its events.

    The other tests are parametrised, so each store is checked on its own. This
    one runs both side by side and compares the observed history, so a store
    that silently develops its own ownership rules shows up as a difference
    rather than as a differently-worded failure.
    """

    stores: dict[str, EventStore] = {
        "in_memory": InMemoryEventStore(),
        "jsonl": JsonlEventStore(tmp_path),
    }
    observed: dict[str, list[dict]] = {}

    for name, candidate in stores.items():
        source = payload()
        appended = await append_one(candidate, source)

        source["nested"]["deeper"]["value"] = "mutated-input"
        appended.data["items"][1]["value"] = "mutated-append-result"

        first = await candidate.read(STREAM)
        first[0].data["top"] = "mutated-read-result"

        second = await candidate.read(STREAM)
        observed[name] = [event.data for event in second]

    assert observed["in_memory"] == observed["jsonl"]
    assert observed["in_memory"] == [payload()]


async def test_detach_rejects_unsupported_values() -> None:
    """Detaching is not a general deep copy: unsupported values are refused, not copied.

    "Unsupported" is narrower than "not a `JsonValue`" - see
    `test_detach_normalizes_supported_framework_values` for what is converted.
    """

    event = EventEnvelope.materialize(STREAM, 1, PendingEvent("contract/event", {"ok": 1}))
    # Bypass the frozen field to plant a value the encoder has no rule for.
    object.__setattr__(event, "data", {"bad": {object()}})

    with pytest.raises(TypeError, match="not JSON serializable"):
        detach_event(event)


async def test_detach_normalizes_supported_framework_values() -> None:
    """Framework types the encoder supports are converted, not rejected.

    Detaching reuses `to_json_value()`, whose reach is wider than `JsonValue`:
    a `Path` or a `tuple` is not a `JsonValue`, yet neither is an error - they
    become a string and a list. Pinning this keeps the contract from being
    described as "anything outside `JsonValue` raises", which is not what the
    encoder does and would make `tuple` payloads look forbidden.
    """

    event = EventEnvelope.materialize(STREAM, 1, PendingEvent("contract/event", {"ok": 1}))
    object.__setattr__(
        event,
        "data",
        {
            "path": Path("sub") / "file.txt",
            "pair": (1, 2),
            "nested": {"inner_pair": ("a", "b")},
            "items": [Path("other.txt")],
        },
    )

    detached = detach_event(event)

    assert detached.data == {
        "path": str(Path("sub") / "file.txt"),
        "pair": [1, 2],
        "nested": {"inner_pair": ["a", "b"]},
        "items": [str(Path("other.txt"))],
    }
    # Converted, not merely equal: a tuple would have compared unequal to a
    # list, but assert the type so a future "copy tuples as tuples" cannot pass.
    assert isinstance(detached.data["pair"], list)
    assert isinstance(detached.data["path"], str)


async def test_detached_scalars_are_not_wrapped() -> None:
    """Immutable scalars need no copy and must not be rewritten on the way through."""

    data = {"none": None, "flag": True, "count": 3, "ratio": 1.5, "text": "x"}
    event = EventEnvelope.materialize(STREAM, 1, PendingEvent("contract/event", data))

    detached = detach_event(event)
    assert detached.data == data
    for key, value in data.items():
        assert type(detached.data[key]) is type(value)


async def test_event_id_round_trips_as_uuid_not_text(store: EventStore) -> None:
    """Detachment must not degrade typed metadata into its JSON spelling."""

    appended = await append_one(store, payload())
    assert isinstance(appended.event_id, UUID)

    stored = (await store.read(STREAM))[0]
    assert isinstance(stored.event_id, UUID)
    assert stored.event_id == appended.event_id
    assert isinstance(stored.occurred_at, datetime)
