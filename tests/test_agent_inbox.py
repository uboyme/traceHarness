"""An Agent's Inbox is an accepted-message history, not a queue in memory.

Every test here works the boundary v0.6 Stage B establishes: a message is
accepted exactly when its ``agent/message-accepted`` event is in that Agent's
Inbox stream, and the FIFO order is stream order. Nothing asserts delivery,
claiming, execution or completion - Stage B has no Supervisor and the protocol
deliberately cannot express any of them.

Concurrency and cancellation use explicit gates and a store stub that suspends
at an exact point. There is no ``sleep()`` used to guess timing.
"""

from __future__ import annotations

import asyncio
import dataclasses
import gc
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from traceh.agents import (
    AGENT_MESSAGE_ACCEPTED,
    AgentInbox,
    AgentInboxConflictError,
    AgentInboxProtocolError,
    AgentInboxReader,
    AgentInboxService,
    AgentMessageAcceptError,
    AgentMessageConflictError,
    AgentMessageError,
    AgentRegistrar,
    AgentUnknownError,
    agent_inbox_stream,
    is_message_content,
    message_accepted_data,
    parse_message_accepted,  # noqa: F811
    validate_agent_inbox_events,
)
from traceh.agents.inbox_identity import (
    AGENT_MESSAGE_ACCEPTED_KEYS,
    MAX_MESSAGE_CONTENT_CHARS,
)
from traceh.api.agents import AcceptedMessage, AgentMessage, AgentSpec, MessageTarget
from traceh.api.events import EventEnvelope, PendingEvent
from traceh.session.event_store import (
    ConcurrencyConflict,
    Durability,
    EventStore,
    InMemoryEventStore,
)
from traceh.session.jsonl import JsonlEventStore

SPEC = AgentSpec(preset="coder", workspace_id="workspace-1")


def message(message_id: str = "m1", **overrides) -> AgentMessage:
    fields = {"message_id": message_id, "content": "hello", "source": "user", **overrides}
    return AgentMessage(**fields)


async def register(store: EventStore, *agent_ids: str) -> None:
    registrar = AgentRegistrar(store)
    for index, agent_id in enumerate(agent_ids):
        await registrar.create_agent(
            SPEC,
            request_id=f"req-{agent_id}-{index}",
            agent_id=agent_id,
            session_id=f"s-{agent_id}",
        )


async def read_inbox(store: EventStore, agent_id: str) -> AgentInbox:
    """Rebuild through objects that never saw the write path."""

    return await AgentInboxReader(store).load(agent_id)


async def raw_append(
    store: EventStore,
    agent_id: str,
    data: dict,
    *,
    event_type: str = AGENT_MESSAGE_ACCEPTED,
    schema_version: int = 1,
    stream_id: str | None = None,
) -> None:
    """Append directly, bypassing every service check."""

    stream = stream_id if stream_id is not None else agent_inbox_stream(agent_id)
    head = await store.head(stream)
    await store.append(
        stream,
        expected_seq=head,
        events=(PendingEvent(type=event_type, data=data, schema_version=schema_version),),
    )


_MESSAGE_FIELDS = {"message_id", "content", "source", "correlation_id", "causation_id"}


def accepted_payload(agent_id: str = "a1", **overrides) -> dict:
    fields = {key: value for key, value in overrides.items() if key in _MESSAGE_FIELDS}
    return message_accepted_data(
        agent_id=agent_id,
        message=message(**fields),
        target=overrides.get("target", MessageTarget.NEW_TURN),
        wakeup=overrides.get("wakeup", False),
    )


def envelope(data: dict, *, agent_id: str = "a1", seq: int = 1, **overrides) -> EventEnvelope:
    """Build an envelope directly, bypassing the store's own encoding."""

    return EventEnvelope(
        event_id=uuid4(),
        stream_id=overrides.get("stream_id", agent_inbox_stream(agent_id)),
        seq=seq,
        type=overrides.get("type", AGENT_MESSAGE_ACCEPTED),
        schema_version=overrides.get("schema_version", 1),
        data=data,
        occurred_at=datetime.now(UTC),
    )


@pytest.fixture
async def loop_reports():
    loop = asyncio.get_running_loop()
    reports: list[dict] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reports.append(context))
    yield reports
    loop.set_exception_handler(previous)


async def settle(times: int = 5) -> None:
    for _ in range(times):
        await asyncio.sleep(0)
    gc.collect()
    for _ in range(times):
        await asyncio.sleep(0)


def never_retrieved(reports: list[dict]) -> list[dict]:
    return [item for item in reports if "never retrieved" in str(item.get("message", ""))]


class GatedStore:
    """An `EventStore` whose append and read stop at an exact point.

    ``commit_first`` selects which side of the store's commit-point boundary is
    being exercised: ``False`` blocks before anything is written, ``True``
    writes and *then* blocks - the may-have-committed case a caller cannot tell
    apart from the exception alone.
    """

    def __init__(self, inner: EventStore, *, commit_first: bool = False) -> None:
        self.inner = inner
        self.commit_first = commit_first
        self.append_entered = asyncio.Event()
        self.append_release = asyncio.Event()
        self.read_entered = asyncio.Event()
        self.read_release = asyncio.Event()
        self.stale_reads = False
        self.gate_reads = False
        self.append_calls = 0
        self.append_failure: BaseException | None = None

    async def append(self, stream_id, *, expected_seq, events, durability=Durability.SYNC):
        self.append_calls += 1
        result = None
        if self.commit_first:
            result = await self.inner.append(
                stream_id, expected_seq=expected_seq, events=events, durability=durability
            )
        self.append_entered.set()
        await self.append_release.wait()
        if self.append_failure is not None:
            raise self.append_failure
        if result is not None:
            return result
        return await self.inner.append(
            stream_id, expected_seq=expected_seq, events=events, durability=durability
        )

    async def read(self, stream_id, *, from_seq=1):
        gate = stream_id.startswith("agent-inbox:")
        if self.stale_reads and gate:
            snapshot = await self.inner.read(stream_id, from_seq=from_seq)
            self.read_entered.set()
            await self.read_release.wait()
            return snapshot
        if self.gate_reads and gate:
            self.read_entered.set()
            await self.read_release.wait()
        return await self.inner.read(stream_id, from_seq=from_seq)

    async def head(self, stream_id):
        return await self.inner.head(stream_id)

    async def list_streams(self, *, prefix=None):
        return await self.inner.list_streams(prefix=prefix)


class YieldingStore:
    """A store whose reads and appends genuinely suspend.

    `InMemoryEventStore` never awaits, so two tasks driving it can never
    interleave and a concurrency test built on it would pass against a service
    with no linearization at all. One ``asyncio.sleep(0)`` is a deterministic
    yield point - asyncio's ready queue is FIFO - not a timing guess.
    """

    def __init__(self, inner: EventStore) -> None:
        self.inner = inner

    async def append(self, stream_id, *, expected_seq, events, durability=Durability.SYNC):
        await asyncio.sleep(0)
        return await self.inner.append(
            stream_id, expected_seq=expected_seq, events=events, durability=durability
        )

    async def read(self, stream_id, *, from_seq=1):
        await asyncio.sleep(0)
        return await self.inner.read(stream_id, from_seq=from_seq)

    async def head(self, stream_id):
        await asyncio.sleep(0)
        return await self.inner.head(stream_id)

    async def list_streams(self, *, prefix=None):
        return await self.inner.list_streams(prefix=prefix)


class FailingReadStore:
    """Commits the append, then makes every later Inbox read fail."""

    def __init__(self, inner: EventStore, error: BaseException) -> None:
        self.inner = inner
        self.error = error
        self.reads_fail = False

    async def append(self, stream_id, *, expected_seq, events, durability=Durability.SYNC):
        await self.inner.append(
            stream_id, expected_seq=expected_seq, events=events, durability=durability
        )
        self.reads_fail = True
        raise self.error

    async def read(self, stream_id, *, from_seq=1):
        if self.reads_fail and stream_id.startswith("agent-inbox:"):
            raise OSError("reconciliation read failed")
        return await self.inner.read(stream_id, from_seq=from_seq)

    async def head(self, stream_id):
        return await self.inner.head(stream_id)

    async def list_streams(self, *, prefix=None):
        return await self.inner.list_streams(prefix=prefix)


# 1. Ordinary acceptance and rebuild.


async def test_an_accepted_message_is_rebuilt_from_a_fresh_projector(tmp_path):
    store = JsonlEventStore(tmp_path / "data")
    await register(store, "a1")
    receipt = await AgentInboxService(store).accept(
        "a1",
        message(content="line one\nline two", correlation_id="c1", causation_id="k1"),
        target=MessageTarget.NEXT_STEP,
        wakeup=True,
    )

    # A second store object over the same directory: only the files are shared.
    reopened = JsonlEventStore(tmp_path / "data")
    inbox = await read_inbox(reopened, "a1")

    assert len(inbox) == 1
    accepted = inbox.get("m1")
    assert accepted.agent_id == "a1"
    assert accepted.message.content == "line one\nline two"
    assert accepted.message.correlation_id == "c1"
    assert accepted.message.causation_id == "k1"
    assert accepted.target is MessageTarget.NEXT_STEP
    assert accepted.wakeup is True
    assert accepted.accepted_seq == receipt.accepted_seq == 1
    assert accepted.receipt() == receipt
    assert inbox.head_seq == 1


async def test_messages_keep_strict_fifo_order():
    store = InMemoryEventStore()
    await register(store, "a1")
    service = AgentInboxService(store)

    for index in range(6):
        await service.accept(
            "a1", message(f"m{index}", content=f"body {index}"),
            target=MessageTarget.NEW_TURN, wakeup=False,
        )

    inbox = await read_inbox(store, "a1")
    assert [item.message.message_id for item in inbox] == [f"m{index}" for index in range(6)]
    assert [item.accepted_seq for item in inbox] == [1, 2, 3, 4, 5, 6]
    assert [item.message.content for item in inbox.messages] == [f"body {i}" for i in range(6)]


async def test_two_agents_have_completely_isolated_inboxes():
    store = InMemoryEventStore()
    await register(store, "a1", "a2")
    service = AgentInboxService(store)

    await service.accept("a1", message("m1"), target=MessageTarget.NEW_TURN, wakeup=False)
    await service.accept("a2", message("m2"), target=MessageTarget.NEW_TURN, wakeup=False)
    await service.accept("a1", message("m3"), target=MessageTarget.NEW_TURN, wakeup=False)

    first = await read_inbox(store, "a1")
    second = await read_inbox(store, "a2")
    assert [item.message.message_id for item in first] == ["m1", "m3"]
    assert [item.message.message_id for item in second] == ["m2"]
    # Each Agent's sequence numbers start at 1: they are separate streams, so
    # one Agent's traffic never advances another's.
    assert [item.accepted_seq for item in first] == [1, 2]
    assert [item.accepted_seq for item in second] == [1]
    assert first.get("m2") is None


async def test_acceptance_writes_only_to_the_target_inbox_stream():
    store = InMemoryEventStore()
    await register(store, "a1")
    await AgentInboxService(store).accept(
        "a1", message(), target=MessageTarget.NEW_TURN, wakeup=False
    )

    assert set(await store.list_streams()) == {"agents:directory", "agent-inbox:a1"}
    events = await store.read(agent_inbox_stream("a1"))
    assert [event.type for event in events] == [AGENT_MESSAGE_ACCEPTED]
    assert set(events[0].data) == AGENT_MESSAGE_ACCEPTED_KEYS
    # The Session Stream is untouched: an Inbox fact is not a Session fact.
    assert await store.read("session:s-a1") == ()


async def test_an_empty_inbox_is_not_an_error():
    store = InMemoryEventStore()
    await register(store, "a1")
    inbox = await AgentInboxService(store).inbox("a1")
    assert len(inbox) == 0
    assert inbox.messages == ()
    assert inbox.head_seq == 0
    assert inbox.get("anything") is None
    assert inbox.agent_id == "a1"


# 2. Identity and idempotency.


async def test_a_message_for_an_unknown_agent_is_refused_with_no_inbox_event():
    store = InMemoryEventStore()
    await register(store, "a1")

    with pytest.raises(AgentUnknownError) as error:
        await AgentInboxService(store).accept(
            "ghost", message(), target=MessageTarget.NEW_TURN, wakeup=False
        )

    assert error.value.code == "agent-unknown"
    assert await store.read(agent_inbox_stream("ghost")) == ()
    assert await store.list_streams(prefix="agent-inbox:") == ()


async def test_the_same_message_id_and_payload_returns_the_original_receipt():
    store = InMemoryEventStore()
    await register(store, "a1")
    service = AgentInboxService(store)

    first = await service.accept(
        "a1", message(content="body"), target=MessageTarget.NEXT_STEP, wakeup=True
    )
    again = await service.accept(
        "a1", message(content="body"), target=MessageTarget.NEXT_STEP, wakeup=True
    )
    # A different service object over the same store answers identically.
    third = await AgentInboxService(store).accept(
        "a1", message(content="body"), target=MessageTarget.NEXT_STEP, wakeup=True
    )

    assert first == again == third
    assert len(await store.read(agent_inbox_stream("a1"))) == 1


@pytest.mark.parametrize(
    ("changed", "target", "wakeup"),
    [
        ({"content": "different"}, MessageTarget.NEW_TURN, False),
        ({"source": "other"}, MessageTarget.NEW_TURN, False),
        ({"correlation_id": "c9"}, MessageTarget.NEW_TURN, False),
        ({"causation_id": "k9"}, MessageTarget.NEW_TURN, False),
        ({}, MessageTarget.NEXT_STEP, False),
        ({}, MessageTarget.NEW_TURN, True),
    ],
)
async def test_the_same_message_id_with_a_different_message_is_refused(changed, target, wakeup):
    store = InMemoryEventStore()
    await register(store, "a1")
    service = AgentInboxService(store)
    await service.accept("a1", message(), target=MessageTarget.NEW_TURN, wakeup=False)

    with pytest.raises(AgentMessageConflictError) as error:
        await service.accept("a1", message(**changed), target=target, wakeup=wakeup)

    assert error.value.code == "inbox-message-reused"
    assert len(await store.read(agent_inbox_stream("a1"))) == 1


async def test_a_duplicated_message_id_in_history_fails_closed():
    """Last write does not win: this is a log, not a mutable queue."""

    store = InMemoryEventStore()
    await register(store, "a1")
    await AgentInboxService(store).accept(
        "a1", message(), target=MessageTarget.NEW_TURN, wakeup=False
    )
    await raw_append(store, "a1", accepted_payload(content="second body"))

    events = await store.read(agent_inbox_stream("a1"))
    with pytest.raises(AgentInboxProtocolError) as error:
        AgentInbox.rebuild(events, "a1")
    assert error.value.code == "inbox-message-id-duplicate"
    assert error.value.seq == 2
    assert [issue.code for issue in validate_agent_inbox_events(events, "a1")] == [
        "inbox-message-id-duplicate"
    ]


async def test_one_agents_event_moved_into_another_inbox_fails_closed():
    """An acceptance is only a fact on its own Agent's Inbox."""

    store = InMemoryEventStore()
    await register(store, "a1", "a2")
    # A payload naming a1, appended onto a2's stream.
    await raw_append(store, "a2", accepted_payload(agent_id="a1"))

    events = await store.read(agent_inbox_stream("a2"))
    with pytest.raises(AgentInboxProtocolError) as error:
        AgentInbox.rebuild(events, "a2")
    assert error.value.code == "inbox-stream-unexpected"
    # And rebuilding it as a1's Inbox does not rescue it either.
    with pytest.raises(AgentInboxProtocolError):
        AgentInbox.rebuild(events, "a1")


async def test_a_broken_inbox_blocks_new_acceptances_instead_of_writing_into_it():
    store = InMemoryEventStore()
    await register(store, "a1")
    broken = accepted_payload()
    broken["wakeup"] = "yes"
    await raw_append(store, "a1", broken)

    with pytest.raises(AgentInboxProtocolError) as error:
        await AgentInboxService(store).accept(
            "a1", message("m2"), target=MessageTarget.NEW_TURN, wakeup=False
        )
    assert error.value.code == "inbox-wakeup-invalid"
    assert len(await store.read(agent_inbox_stream("a1"))) == 1


# 3. Protocol counter-examples.


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("message_id", None), ("message_id", 1), ("message_id", True), ("message_id", ""),
        ("message_id", " padded"), ("message_id", "a\nb"), ("message_id", "x" * 257),
        ("source", None), ("source", 7), ("source", "  "), ("source", "a\x1bb"),
        ("correlation_id", 5), ("correlation_id", ""), ("correlation_id", "trailing "),
        ("causation_id", False), ("causation_id", "a b"),
    ],
)
async def test_unusable_message_identifiers_are_refused_before_any_append(field, value):
    store = InMemoryEventStore()
    await register(store, "a1")

    with pytest.raises(AgentMessageError) as error:
        await AgentInboxService(store).accept(
            "a1", message(**{field: value}), target=MessageTarget.NEW_TURN, wakeup=False
        )

    assert error.value.field == field
    assert error.value.code == "inbox-identity-invalid"
    assert await store.read(agent_inbox_stream("a1")) == ()


@pytest.mark.parametrize(
    "content",
    [None, 1, True, b"bytes", ["list"], {"dict": 1}, "oversized",
     "lone surrogate \ud800 here"],
    ids=["none", "int", "bool", "bytes", "list", "dict", "oversized", "lone-surrogate"],
)
async def test_unusable_message_content_is_refused_before_any_append(content):
    if content == "oversized":
        # Built here rather than in the parameter list: a megabyte of ``x`` in a
        # test id makes every failure report unreadable.
        content = "x" * (MAX_MESSAGE_CONTENT_CHARS + 1)
    store = InMemoryEventStore()
    await register(store, "a1")

    assert not is_message_content(content)
    with pytest.raises(AgentMessageError) as error:
        await AgentInboxService(store).accept(
            "a1", message(content=content), target=MessageTarget.NEW_TURN, wakeup=False
        )

    assert error.value.code == "inbox-content-invalid"
    assert await store.read(agent_inbox_stream("a1")) == ()


@pytest.mark.parametrize(
    "content",
    ["", "plain", "line one\nline two", "tab\there", "中文内容", "emoji 🙂", "x" * 10_000,
     "carriage\r\nreturn"],
)
async def test_ordinary_multiline_content_is_accepted_and_round_trips(content):
    """Content is prose, not an identifier: the single-line terminal rules that
    govern an ``agent_id`` must not be applied to it."""

    store = InMemoryEventStore()
    await register(store, "a1")

    assert is_message_content(content)
    await AgentInboxService(store).accept(
        "a1", message(content=content), target=MessageTarget.NEW_TURN, wakeup=False
    )
    assert (await read_inbox(store, "a1")).get("m1").message.content == content


@pytest.mark.parametrize("target", [None, "new_turn", 0, True, "unknown", MessageTarget])
async def test_a_target_that_is_not_a_message_target_is_refused(target):
    store = InMemoryEventStore()
    await register(store, "a1")

    with pytest.raises(AgentMessageError) as error:
        await AgentInboxService(store).accept("a1", message(), target=target, wakeup=False)

    assert error.value.code == "inbox-target-invalid"
    assert await store.read(agent_inbox_stream("a1")) == ()


@pytest.mark.parametrize("wakeup", [None, 0, 1, "", "false", "true", [], object()])
async def test_a_wakeup_that_is_not_a_bool_is_refused(wakeup):
    """Truthiness is not consent. This flag will later decide whether an
    Activation is started, which is not a place to guess."""

    store = InMemoryEventStore()
    await register(store, "a1")

    with pytest.raises(AgentMessageError) as error:
        await AgentInboxService(store).accept(
            "a1", message(), target=MessageTarget.NEW_TURN, wakeup=wakeup
        )

    assert error.value.code == "inbox-wakeup-invalid"
    assert await store.read(agent_inbox_stream("a1")) == ()


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda d: d.__setitem__("agent_id", None), "inbox-identity-invalid"),
        (lambda d: d.__setitem__("message_id", 5), "inbox-identity-invalid"),
        (lambda d: d.__setitem__("source", ""), "inbox-identity-invalid"),
        (lambda d: d.__setitem__("content", None), "inbox-content-invalid"),
        (lambda d: d.__setitem__("content", 7), "inbox-content-invalid"),
        (lambda d: d.__setitem__("target", "unknown"), "inbox-target-invalid"),
        (lambda d: d.__setitem__("target", None), "inbox-target-invalid"),
        (lambda d: d.__setitem__("wakeup", 1), "inbox-wakeup-invalid"),
        (lambda d: d.__setitem__("wakeup", "true"), "inbox-wakeup-invalid"),
        (lambda d: d.__setitem__("correlation_id", 3), "inbox-identity-invalid"),
        (lambda d: d.pop("causation_id"), "inbox-payload-keys-unexpected"),
        (lambda d: d.pop("wakeup"), "inbox-payload-keys-unexpected"),
        (lambda d: d.__setitem__("extra", "surprise"), "inbox-payload-keys-unexpected"),
        (lambda d: d.__setitem__("claimed", True), "inbox-payload-keys-unexpected"),
    ],
)
async def test_a_malformed_acceptance_fact_fails_closed(mutate, code):
    store = InMemoryEventStore()
    await register(store, "a1")
    data = accepted_payload()
    mutate(data)
    await raw_append(store, "a1", data)

    with pytest.raises(AgentInboxProtocolError) as error:
        await read_inbox(store, "a1")
    assert error.value.code == code
    assert error.value.seq == 1


async def test_an_unsupported_schema_version_fails_closed():
    store = InMemoryEventStore()
    await register(store, "a1")
    await raw_append(store, "a1", accepted_payload(), schema_version=99)

    with pytest.raises(AgentInboxProtocolError) as error:
        await read_inbox(store, "a1")
    assert error.value.code == "inbox-schema-version-unsupported"


async def test_an_unknown_event_type_on_an_inbox_stream_fails_closed():
    store = InMemoryEventStore()
    await register(store, "a1")
    await raw_append(store, "a1", {"message_id": "m1"}, event_type="agent/message-claimed")

    with pytest.raises(AgentInboxProtocolError) as error:
        await read_inbox(store, "a1")
    assert error.value.code == "inbox-event-type-unknown"


async def test_an_acceptance_on_the_directory_stream_is_not_an_acceptance():
    store = InMemoryEventStore()
    await register(store, "a1")
    events = (envelope(accepted_payload(), stream_id="agents:directory"),)

    with pytest.raises(AgentInboxProtocolError) as error:
        AgentInbox.rebuild(events, "a1")
    assert error.value.code == "inbox-stream-unexpected"


async def test_a_broken_record_is_never_silently_dropped():
    store = InMemoryEventStore()
    await register(store, "a1")
    service = AgentInboxService(store)
    await service.accept("a1", message("m1"), target=MessageTarget.NEW_TURN, wakeup=False)
    broken = accepted_payload(message_id="m2")
    broken["target"] = "nope"
    await raw_append(store, "a1", broken)
    await raw_append(store, "a1", accepted_payload(message_id="m3"))

    events = await store.read(agent_inbox_stream("a1"))
    with pytest.raises(AgentInboxProtocolError):
        AgentInbox.rebuild(events, "a1")
    assert [(i.code, i.seq) for i in validate_agent_inbox_events(events, "a1")] == [
        ("inbox-target-invalid", 2)
    ]


@pytest.mark.parametrize("agent_id", [None, 1, True, "", "  ", " padded", "a\nb", "x" * 257])
async def test_an_unusable_agent_id_is_refused_before_anything_is_read(agent_id):
    store = InMemoryEventStore()

    with pytest.raises(AgentMessageError) as error:
        await AgentInboxService(store).accept(
            agent_id, message(), target=MessageTarget.NEW_TURN, wakeup=False
        )
    assert error.value.field == "agent_id"
    assert await store.list_streams() == ()


async def test_errors_never_echo_message_content_or_source():
    """The most likely thing a caller pastes wrongly is the message itself."""

    store = InMemoryEventStore()
    await register(store, "a1")
    secret = "sk-proj-FAKE-FIXTURE-NOT-A-REAL-KEY"

    with pytest.raises(AgentMessageError) as error:
        await AgentInboxService(store).accept(
            "a1",
            message(content=f"{secret}\ud800"),
            target=MessageTarget.NEW_TURN,
            wakeup=False,
        )
    assert secret not in str(error.value)
    assert "FAKE" not in str(error.value)
    assert str(error.value) == "agent message content is not usable"

    with pytest.raises(AgentMessageError) as source_error:
        await AgentInboxService(store).accept(
            "a1", message(source=f"{secret}\n"), target=MessageTarget.NEW_TURN, wakeup=False
        )
    assert secret not in str(source_error.value)
    assert str(source_error.value) == "agent message source is not usable"


async def test_the_public_payload_helper_refuses_instead_of_repairing():
    with pytest.raises(AgentMessageError):
        message_accepted_data(
            agent_id="a1", message=message(content=None),
            target=MessageTarget.NEW_TURN, wakeup=False,
        )
    with pytest.raises(AgentMessageError):
        message_accepted_data(
            agent_id="a1", message=message(), target="new_turn", wakeup=False,
        )
    with pytest.raises(AgentMessageError):
        message_accepted_data(
            agent_id="a1", message="not a message",
            target=MessageTarget.NEW_TURN, wakeup=False,
        )


# 4. Concurrency, cancellation and the commit-point boundary.


async def test_two_concurrent_acceptances_of_one_message_id_produce_one_event():
    inner = InMemoryEventStore()
    await register(inner, "a1")
    store = YieldingStore(inner)
    service = AgentInboxService(store)
    start = asyncio.Event()

    async def send():
        await start.wait()
        return await service.accept(
            "a1", message(content="body"), target=MessageTarget.NEW_TURN, wakeup=False
        )

    tasks = [asyncio.create_task(send()) for _ in range(2)]
    start.set()
    results = await asyncio.gather(*tasks)

    assert results[0] == results[1]
    assert len(await inner.read(agent_inbox_stream("a1"))) == 1
    assert len(await read_inbox(inner, "a1")) == 1


async def test_concurrent_acceptances_of_distinct_messages_all_commit_in_order():
    inner = InMemoryEventStore()
    await register(inner, "a1")
    store = YieldingStore(inner)
    service = AgentInboxService(store)
    start = asyncio.Event()

    async def send(index: int):
        await start.wait()
        return await service.accept(
            "a1", message(f"m{index}"), target=MessageTarget.NEW_TURN, wakeup=False
        )

    tasks = [asyncio.create_task(send(index)) for index in range(8)]
    start.set()
    receipts = await asyncio.gather(*tasks)

    inbox = await read_inbox(inner, "a1")
    assert len(inbox) == 8
    # Dense, unique sequence numbers: the appends linearized rather than racing.
    assert sorted(item.accepted_seq for item in receipts) == list(range(1, 9))
    assert [item.accepted_seq for item in inbox] == list(range(1, 9))


async def test_messages_to_different_agents_do_not_serialize_against_each_other():
    inner = InMemoryEventStore()
    await register(inner, "a1", "a2", "a3")
    store = YieldingStore(inner)
    service = AgentInboxService(store)
    start = asyncio.Event()

    async def send(agent_id: str, index: int):
        await start.wait()
        return await service.accept(
            agent_id, message(f"m{index}"), target=MessageTarget.NEW_TURN, wakeup=False
        )

    tasks = [
        asyncio.create_task(send(agent, index))
        for index, agent in enumerate(["a1", "a2", "a3", "a1", "a2", "a3"])
    ]
    start.set()
    await asyncio.gather(*tasks)

    for agent_id in ("a1", "a2", "a3"):
        inbox = await read_inbox(inner, agent_id)
        assert len(inbox) == 2
        assert [item.accepted_seq for item in inbox] == [1, 2]


async def test_an_acceptance_built_on_a_stale_read_is_rejected_and_writes_nothing():
    """Another sender advanced the Inbox after our read of it.

    The per-Agent lock cannot see a writer in another process, so the append
    must carry the sequence the *Inbox read* returned. Re-reading the head at
    append time would accept a decision whose idempotency check ran against a
    history that no longer exists.
    """

    inner = InMemoryEventStore()
    await register(inner, "a1")
    gated = GatedStore(inner)
    gated.stale_reads = True
    gated.append_release.set()

    acceptance = asyncio.create_task(
        AgentInboxService(gated).accept(
            "a1", message("m1"), target=MessageTarget.NEW_TURN, wakeup=False
        )
    )
    await gated.read_entered.wait()
    await raw_append(inner, "a1", accepted_payload(message_id="other"))
    gated.read_release.set()

    with pytest.raises(AgentInboxConflictError) as error:
        await acceptance
    assert error.value.code == "inbox-changed"

    inbox = await read_inbox(inner, "a1")
    assert [item.message.message_id for item in inbox] == ["other"]
    # Retrying the same message id is safe and now succeeds.
    retry = await AgentInboxService(inner).accept(
        "a1", message("m1"), target=MessageTarget.NEW_TURN, wakeup=False
    )
    assert retry.accepted_seq == 2


async def test_cancelling_before_the_append_writes_nothing(loop_reports):
    inner = InMemoryEventStore()
    await register(inner, "a1")
    gated = GatedStore(inner)
    gated.gate_reads = True

    acceptance = asyncio.create_task(
        AgentInboxService(gated).accept(
            "a1", message(), target=MessageTarget.NEW_TURN, wakeup=False
        )
    )
    await gated.read_entered.wait()
    acceptance.cancel()
    gated.read_release.set()

    with pytest.raises(asyncio.CancelledError):
        await acceptance

    assert gated.append_calls == 0
    assert await inner.read(agent_inbox_stream("a1")) == ()
    await settle()
    assert never_retrieved(loop_reports) == []


async def test_cancellation_inside_a_committed_append_is_reported_as_cancellation(loop_reports):
    inner = InMemoryEventStore()
    await register(inner, "a1")
    gated = GatedStore(inner, commit_first=True)

    acceptance = asyncio.create_task(
        AgentInboxService(gated).accept(
            "a1", message(), target=MessageTarget.NEW_TURN, wakeup=False
        )
    )
    await gated.append_entered.wait()
    acceptance.cancel()
    gated.append_failure = asyncio.CancelledError()
    gated.append_release.set()

    with pytest.raises(asyncio.CancelledError):
        await acceptance

    # The append did commit, and the caller can reconcile it by message id.
    inbox = await AgentInboxService(inner).inbox("a1")
    assert inbox.get("m1") is not None
    assert len(inbox) == 1
    await settle()
    assert never_retrieved(loop_reports) == []


async def test_a_retry_after_a_may_have_committed_cancellation_accepts_once():
    inner = InMemoryEventStore()
    await register(inner, "a1")
    gated = GatedStore(inner, commit_first=True)

    acceptance = asyncio.create_task(
        AgentInboxService(gated).accept(
            "a1", message("stable"), target=MessageTarget.NEW_TURN, wakeup=False
        )
    )
    await gated.append_entered.wait()
    acceptance.cancel()
    gated.append_failure = asyncio.CancelledError()
    gated.append_release.set()
    with pytest.raises(asyncio.CancelledError):
        await acceptance

    retry = await AgentInboxService(inner).accept(
        "a1", message("stable"), target=MessageTarget.NEW_TURN, wakeup=False
    )

    assert retry.accepted_seq == 1
    assert len(await inner.read(agent_inbox_stream("a1"))) == 1


async def test_repeated_cancellation_cannot_release_the_caller_early(loop_reports):
    inner = InMemoryEventStore()
    await register(inner, "a1")
    gated = GatedStore(inner, commit_first=True)

    acceptance = asyncio.create_task(
        AgentInboxService(gated).accept(
            "a1", message(), target=MessageTarget.NEW_TURN, wakeup=False
        )
    )
    await gated.append_entered.wait()

    gated.gate_reads = True
    acceptance.cancel()
    gated.append_failure = asyncio.CancelledError()
    gated.append_release.set()
    await gated.read_entered.wait()

    for _ in range(3):
        acceptance.cancel()
        for _ in range(3):
            await asyncio.sleep(0)
        assert not acceptance.done(), "repeated cancellation released the caller early"

    gated.read_release.set()
    with pytest.raises(asyncio.CancelledError):
        await acceptance

    await settle()
    assert never_retrieved(loop_reports) == []


async def test_a_failed_reconciliation_read_reports_unknown_not_uncommitted():
    inner = InMemoryEventStore()
    await register(inner, "a1")
    store = FailingReadStore(inner, RuntimeError("append blew up after committing"))

    with pytest.raises(AgentMessageAcceptError) as error:
        await AgentInboxService(store).accept(
            "a1", message(), target=MessageTarget.NEW_TURN, wakeup=False
        )

    assert error.value.committed is None
    assert "unknown" in str(error.value)
    assert len(await inner.read(agent_inbox_stream("a1"))) == 1


async def test_an_unknown_outcome_is_not_reported_as_a_lost_compare_and_swap():
    inner = InMemoryEventStore()
    await register(inner, "a1")
    store = FailingReadStore(inner, ConcurrencyConflict("conflict after commit"))

    with pytest.raises(AgentMessageAcceptError) as error:
        await AgentInboxService(store).accept(
            "a1", message(), target=MessageTarget.NEW_TURN, wakeup=False
        )

    assert not isinstance(error.value, AgentInboxConflictError)
    assert error.value.committed is None


async def test_a_failed_append_reports_whether_it_committed(loop_reports):
    inner = InMemoryEventStore()
    await register(inner, "a1")
    gated = GatedStore(inner)
    gated.append_failure = RuntimeError("store exploded")
    gated.append_release.set()

    with pytest.raises(AgentMessageAcceptError) as error:
        await AgentInboxService(gated).accept(
            "a1", message(), target=MessageTarget.NEW_TURN, wakeup=False
        )
    assert error.value.committed is False
    assert await inner.read(agent_inbox_stream("a1")) == ()

    committed = GatedStore(inner, commit_first=True)
    committed.append_failure = RuntimeError("store exploded after writing")
    committed.append_release.set()
    with pytest.raises(AgentMessageAcceptError) as second:
        await AgentInboxService(committed).accept(
            "a1", message("m2"), target=MessageTarget.NEW_TURN, wakeup=False
        )
    assert second.value.committed is True
    await settle()
    assert never_retrieved(loop_reports) == []


@pytest.mark.parametrize("interrupt", [SystemExit(3), KeyboardInterrupt()])
async def test_interpreter_signals_are_not_rewritten_into_acceptance_failures(interrupt):
    inner = InMemoryEventStore()
    await register(inner, "a1")
    store = FailingReadStore(inner, interrupt)

    with pytest.raises(type(interrupt)) as error:
        await AgentInboxService(store).accept(
            "a1", message(), target=MessageTarget.NEW_TURN, wakeup=False
        )
    assert error.value is interrupt


async def test_acceptance_leaves_no_task_behind(loop_reports):
    inner = InMemoryEventStore()
    await register(inner, "a1")
    service = AgentInboxService(inner)
    before = {task for task in asyncio.all_tasks() if task is not asyncio.current_task()}

    await service.accept("a1", message(), target=MessageTarget.NEW_TURN, wakeup=False)
    with pytest.raises(AgentMessageConflictError):
        await service.accept(
            "a1", message(content="other"), target=MessageTarget.NEW_TURN, wakeup=False
        )

    await settle()
    after = {task for task in asyncio.all_tasks() if task is not asyncio.current_task()}
    assert after == before
    assert never_retrieved(loop_reports) == []


async def test_a_cancelled_acceptance_leaves_no_reconciliation_task_behind(loop_reports):
    inner = InMemoryEventStore()
    await register(inner, "a1")
    gated = GatedStore(inner, commit_first=True)
    before = {task for task in asyncio.all_tasks() if task is not asyncio.current_task()}

    acceptance = asyncio.create_task(
        AgentInboxService(gated).accept(
            "a1", message(), target=MessageTarget.NEW_TURN, wakeup=False
        )
    )
    await gated.append_entered.wait()
    acceptance.cancel()
    gated.append_failure = asyncio.CancelledError()
    gated.append_release.set()
    with pytest.raises(asyncio.CancelledError):
        await acceptance

    await settle()
    after = {task for task in asyncio.all_tasks() if task is not asyncio.current_task()}
    assert after == before
    assert never_retrieved(loop_reports) == []


# 5. Ownership of the returned facts.


async def test_the_returned_receipt_equals_the_replayed_receipt_exactly():
    """One reader, so an in-memory answer cannot differ from replay."""

    store = InMemoryEventStore()
    await register(store, "a1")
    receipt = await AgentInboxService(store).accept(
        "a1",
        message(content="body", correlation_id="c1", causation_id="k1"),
        target=MessageTarget.NEXT_STEP,
        wakeup=True,
    )

    replayed = (await read_inbox(store, "a1")).get("m1")
    assert replayed.receipt() == receipt
    assert replayed.target is MessageTarget.NEXT_STEP
    assert replayed.wakeup is True


def test_every_accepted_message_field_is_immutable():
    """The Inbox hands out its retained objects, which is only safe while every
    field is an immutable scalar.

    This is the boundary a future mutable `ContentBlock` would cross. If someone
    adds one, this test fails and the projector has to start detaching before it
    can hand the same object to two callers.
    """

    immutable = (str, bool, int, float, type(None))
    for field in dataclasses.fields(AgentMessage):
        value = getattr(message(correlation_id="c", causation_id="k"), field.name)
        assert isinstance(value, immutable), field.name

    accepted = AcceptedMessage(
        agent_id="a1",
        message=message(),
        target=MessageTarget.NEW_TURN,
        wakeup=False,
        accepted_seq=1,
    )
    for field in dataclasses.fields(AcceptedMessage):
        value = getattr(accepted, field.name)
        assert isinstance(value, (*immutable, AgentMessage, MessageTarget)), field.name

    assert AgentMessage.__dataclass_params__.frozen
    assert AcceptedMessage.__dataclass_params__.frozen


async def test_the_inbox_holds_no_runtime_task_or_handle():
    store = InMemoryEventStore()
    await register(store, "a1")
    service = AgentInboxService(store)
    await service.accept("a1", message(), target=MessageTarget.NEW_TURN, wakeup=False)
    receipt_before = (await read_inbox(store, "a1")).get("m1")

    del service
    gc.collect()

    assert (await read_inbox(store, "a1")).get("m1") == receipt_before


# 6. Review findings: reconciliation must recognise *our* event, and reading
# untrusted payload is itself untrusted work.


class LosingRaceStore:
    """Lets another writer commit under the same id, then fails our append.

    This is the shape that made reconciliation lie: the id we are looking for
    really is in the stream, but the fact behind it is somebody else's.
    """

    def __init__(self, inner: EventStore, other_writer) -> None:
        self.inner = inner
        self.other_writer = other_writer
        self.raced = False

    async def append(self, stream_id, *, expected_seq, events, durability=Durability.SYNC):
        if not self.raced:
            self.raced = True
            await self.other_writer()
            raise RuntimeError("our append failed after the other writer won")
        return await self.inner.append(
            stream_id, expected_seq=expected_seq, events=events, durability=durability
        )

    async def read(self, stream_id, *, from_seq=1):
        return await self.inner.read(stream_id, from_seq=from_seq)

    async def head(self, stream_id):
        return await self.inner.head(stream_id)

    async def list_streams(self, *, prefix=None):
        return await self.inner.list_streams(prefix=prefix)


class CommitThenFailStore:
    """Commits our append, then fails - a genuine may-have-committed."""

    def __init__(self, inner: EventStore) -> None:
        self.inner = inner

    async def append(self, stream_id, *, expected_seq, events, durability=Durability.SYNC):
        await self.inner.append(
            stream_id, expected_seq=expected_seq, events=events, durability=durability
        )
        raise RuntimeError("failed after committing")

    async def read(self, stream_id, *, from_seq=1):
        return await self.inner.read(stream_id, from_seq=from_seq)

    async def head(self, stream_id):
        return await self.inner.head(stream_id)

    async def list_streams(self, *, prefix=None):
        return await self.inner.list_streams(prefix=prefix)


@pytest.mark.parametrize(
    ("changed", "target", "wakeup"),
    [
        ({"content": "payload-B"}, MessageTarget.NEW_TURN, False),
        ({"source": "someone-else"}, MessageTarget.NEW_TURN, False),
        ({}, MessageTarget.NEXT_STEP, False),
        ({}, MessageTarget.NEW_TURN, True),
    ],
)
async def test_another_writers_message_is_never_reported_as_ours(changed, target, wakeup):
    """``committed`` answers "did *our* event land", not "is that id present".

    Two independent services racing on one ``message_id`` write different
    messages. Matching on the id alone told the loser its message was recorded
    when what actually landed was the winner's.
    """

    inner = InMemoryEventStore()
    await register(inner, "a1")

    async def other_writer():
        await AgentInboxService(inner).accept(
            "a1", message(**changed), target=target, wakeup=wakeup
        )

    store = LosingRaceStore(inner, other_writer)

    with pytest.raises(AgentMessageAcceptError) as error:
        await AgentInboxService(store).accept(
            "a1", message(content="payload-A"), target=MessageTarget.NEW_TURN, wakeup=False
        )

    assert error.value.committed is False
    # And the stream really does hold the other writer's message, not ours.
    durable = (await read_inbox(inner, "a1")).get("m1")
    assert durable.message.content != "payload-A" or durable.target is not MessageTarget.NEW_TURN \
        or durable.wakeup is not False or durable.message.source != "user"


async def test_our_own_committed_message_is_still_reported_as_committed():
    """The stricter match must not turn every may-have-committed into False."""

    inner = InMemoryEventStore()
    await register(inner, "a1")

    with pytest.raises(AgentMessageAcceptError) as error:
        await AgentInboxService(CommitThenFailStore(inner)).accept(
            "a1", message(content="mine"), target=MessageTarget.NEXT_STEP, wakeup=True
        )

    assert error.value.committed is True
    assert (await read_inbox(inner, "a1")).get("m1").message.content == "mine"


class CommitThenPoisonStore:
    """Commits our append, then another writer adds a malformed event.

    Reachable in practice: a second process can append between our Inbox read
    and our reconciliation read. The malformed event cannot be ours, so it must
    not derail the answer about whether ours landed.
    """

    def __init__(self, inner: EventStore) -> None:
        self.inner = inner
        self.poisoned = False

    async def append(self, stream_id, *, expected_seq, events, durability=Durability.SYNC):
        await self.inner.append(
            stream_id, expected_seq=expected_seq, events=events, durability=durability
        )
        if not self.poisoned:
            self.poisoned = True
            broken = accepted_payload(message_id="unrelated")
            broken["wakeup"] = "not-a-bool"
            await raw_append(self.inner, "a1", broken)
        raise RuntimeError("failed after committing")

    async def read(self, stream_id, *, from_seq=1):
        return await self.inner.read(stream_id, from_seq=from_seq)

    async def head(self, stream_id):
        return await self.inner.head(stream_id)

    async def list_streams(self, *, prefix=None):
        return await self.inner.list_streams(prefix=prefix)


async def test_a_malformed_unrelated_event_does_not_make_the_answer_unknown():
    """It cannot be ours, so it says nothing about whether ours landed."""

    inner = InMemoryEventStore()
    await register(inner, "a1")

    with pytest.raises(AgentMessageAcceptError) as error:
        await AgentInboxService(CommitThenPoisonStore(inner)).accept(
            "a1", message(content="mine"), target=MessageTarget.NEW_TURN, wakeup=False
        )
    assert error.value.committed is True
    # The Inbox itself is now unreadable, which is a separate, honest failure.
    with pytest.raises(AgentInboxProtocolError):
        await read_inbox(inner, "a1")


class RaisingIteration(dict):
    """Encodable through ``items()``, but ``set(data)`` cannot walk it."""

    def __iter__(self):
        raise ValueError("__iter__ is hostile")


class RaisingGet(dict):
    def get(self, *args, **kwargs):
        raise RuntimeError("get() is hostile")


class RaisingGetItem(dict):
    def __getitem__(self, key):
        raise ArithmeticError("__getitem__ is hostile")


class InterruptingIteration(dict):
    def __iter__(self):
        raise KeyboardInterrupt()


class ExitingIteration(dict):
    def __iter__(self):
        raise SystemExit(7)


# Only the access paths the parse actually uses. ``set(data)`` reads
# ``__iter__``, the field readers use ``get`` and ``__getitem__``. ``items()``
# is *not* on this path, and listing it here would assert something untrue -
# the same mistake caught once already in the Stage A suite.
HOSTILE_PAYLOADS = [RaisingIteration, RaisingGet, RaisingGetItem]


def poisoned_event(container) -> EventEnvelope:
    """A real acceptance envelope whose payload container fights back."""

    return envelope(container(accepted_payload()))


@pytest.mark.parametrize("container", HOSTILE_PAYLOADS)
def test_a_payload_that_raises_while_being_read_is_a_protocol_error(container):
    """Reading untrusted payload is itself untrusted work.

    `parse_message_accepted` calls ``set(data)``, ``data.get()`` and
    ``data[key]`` on a container the store handed back. A ``dict`` subclass
    that raises from any of those broke the parse *outside* the protocol error
    boundary and leaked a bare exception.
    """

    with pytest.raises(AgentInboxProtocolError) as error:
        AgentInbox.rebuild((poisoned_event(container),), "a1")
    assert error.value.code == "inbox-payload-invalid"


@pytest.mark.parametrize("container", HOSTILE_PAYLOADS)
def test_the_validator_still_returns_issues_for_a_hostile_payload(container):
    """`validate_agent_inbox_events` promises issues, not exceptions."""

    issues = validate_agent_inbox_events((poisoned_event(container),), "a1")
    assert [issue.code for issue in issues] == ["inbox-payload-invalid"]


@pytest.mark.parametrize(
    ("container", "interrupt"),
    [(InterruptingIteration, KeyboardInterrupt), (ExitingIteration, SystemExit)],
)
def test_an_interrupt_while_reading_a_payload_is_not_a_protocol_error(container, interrupt):
    """The boundary catches `Exception`, never `BaseException`."""

    with pytest.raises(interrupt):
        AgentInbox.rebuild((poisoned_event(container),), "a1")
    with pytest.raises(interrupt):
        validate_agent_inbox_events((poisoned_event(container),), "a1")


def test_an_ordinary_dict_subclass_payload_is_still_read():
    """The boundary rejects unreadable payloads, not every subclass."""

    class Ordinary(dict):
        pass

    inbox = AgentInbox.rebuild((envelope(Ordinary(accepted_payload())),), "a1")
    assert inbox.get("m1").message.content == "hello"


# ROUND SIX: Python equality is not JSON identity, and envelope protocol fields
# are as untrusted as the payload.


class HostileComparison(str):
    """A legal ``str`` whose comparison raises.

    `EventEnvelope` is a public DTO that anything may construct - these tests
    construct it directly - so ``event.type``, ``event.stream_id`` and
    ``event.schema_version`` are no more trusted than ``event.data``.
    """

    def __eq__(self, other):
        raise ValueError("hostile __eq__")

    def __ne__(self, other):
        raise ValueError("hostile __ne__")

    def __hash__(self):
        return str.__hash__(self)


class InterruptingComparison(str):
    def __ne__(self, other):
        raise KeyboardInterrupt()

    def __eq__(self, other):
        raise KeyboardInterrupt()

    def __hash__(self):
        return str.__hash__(self)


class ExitingComparison(str):
    def __ne__(self, other):
        raise SystemExit(7)

    def __eq__(self, other):
        raise SystemExit(7)

    def __hash__(self):
        return str.__hash__(self)


def replace_envelope_field(event: EventEnvelope, field: str, value) -> EventEnvelope:
    fields = {name: getattr(event, name) for name in event.__slots__}
    fields[field] = value
    return EventEnvelope(**fields)


@pytest.mark.parametrize(
    ("mine", "theirs"),
    [
        ("plain", "plain "),
        ("1", "1.0"),
    ],
)
async def test_a_racing_message_differing_only_by_content_is_not_ours(mine, theirs):
    inner = InMemoryEventStore()
    await register(inner, "a1")

    async def other_writer():
        await AgentInboxService(inner).accept(
            "a1", message(content=theirs), target=MessageTarget.NEW_TURN, wakeup=False
        )

    with pytest.raises(AgentMessageAcceptError) as error:
        await AgentInboxService(LosingRaceStore(inner, other_writer)).accept(
            "a1", message(content=mine), target=MessageTarget.NEW_TURN, wakeup=False
        )
    assert error.value.committed is False


@pytest.mark.parametrize("field", ["type", "stream_id", "schema_version"])
def test_a_hostile_envelope_protocol_field_is_a_protocol_error(field):
    """The exception boundary must cover the whole event, not only its payload.

    ``event.type`` is compared before anything else is read, so a ``str``
    subclass with a raising ``__ne__`` used to leak a bare exception out of all
    four public replay entry points.
    """

    event = replace_envelope_field(
        envelope(accepted_payload()), field, HostileComparison("whatever")
    )

    with pytest.raises(AgentInboxProtocolError) as error:
        AgentInbox.rebuild((event,), "a1")
    assert error.value.code == "inbox-payload-invalid"

    issues = validate_agent_inbox_events((event,), "a1")
    assert [issue.code for issue in issues] == ["inbox-payload-invalid"]


@pytest.mark.parametrize(
    ("container", "interrupt"),
    [(InterruptingComparison, KeyboardInterrupt), (ExitingComparison, SystemExit)],
)
@pytest.mark.parametrize("field", ["type", "stream_id", "schema_version"])
def test_an_interrupt_from_an_envelope_field_is_not_a_protocol_error(
    field, container, interrupt
):
    event = replace_envelope_field(envelope(accepted_payload()), field, container("whatever"))

    with pytest.raises(interrupt):
        AgentInbox.rebuild((event,), "a1")
    with pytest.raises(interrupt):
        validate_agent_inbox_events((event,), "a1")


def test_an_ordinary_str_subclass_envelope_field_is_still_read():
    class Ordinary(str):
        pass

    event = replace_envelope_field(
        envelope(accepted_payload()), "type", Ordinary(AGENT_MESSAGE_ACCEPTED)
    )
    assert AgentInbox.rebuild((event,), "a1").get("m1") is not None


@pytest.mark.parametrize("field", ["type", "stream_id", "schema_version"])
def test_the_parser_itself_converts_a_hostile_envelope_field(field):
    """Pinned directly on the parser, not only through ``rebuild``.

    ``_scan`` has its own net, so a leak inside the parser is invisible from
    the projector - but `parse_message_accepted` is public and is also what
    commit reconciliation calls, so its boundary has to hold on its own.
    """

    event = replace_envelope_field(
        envelope(accepted_payload()), field, HostileComparison("whatever")
    )
    with pytest.raises(AgentInboxProtocolError) as error:
        parse_message_accepted(event)
    assert error.value.code == "inbox-payload-invalid"


@pytest.mark.parametrize(
    ("container", "interrupt"),
    [(InterruptingComparison, KeyboardInterrupt), (ExitingComparison, SystemExit)],
)
def test_the_parser_still_propagates_an_interrupt_from_an_envelope_field(container, interrupt):
    event = replace_envelope_field(
        envelope(accepted_payload()), "type", container("whatever")
    )
    with pytest.raises(interrupt):
        parse_message_accepted(event)


@pytest.mark.parametrize("field", ["type", "stream_id", "schema_version"])
async def test_reconciliation_is_unaffected_by_a_hostile_neighbouring_event(field):
    """A hostile event next to ours must not make our own answer unknown."""

    inner = InMemoryEventStore()
    await register(inner, "a1")

    class CommitThenPoisonEnvelope:
        def __init__(self, inner: EventStore) -> None:
            self.inner = inner
            self.committed = False

        async def append(self, stream_id, *, expected_seq, events, durability=Durability.SYNC):
            await self.inner.append(
                stream_id, expected_seq=expected_seq, events=events, durability=durability
            )
            self.committed = True
            raise RuntimeError("failed after committing")

        async def read(self, stream_id, *, from_seq=1):
            real = await self.inner.read(stream_id, from_seq=from_seq)
            # Injected only into the *reconciliation* read. Injecting into the
            # initial rebuild would fail the transaction closed before the
            # append, which tests a different thing.
            if not self.committed or not stream_id.startswith("agent-inbox:"):
                return real
            hostile = replace_envelope_field(
                envelope(accepted_payload(message_id="other"), seq=99),
                field,
                HostileComparison("whatever"),
            )
            return (hostile, *real)

        async def head(self, stream_id):
            return await self.inner.head(stream_id)

        async def list_streams(self, *, prefix=None):
            return await self.inner.list_streams(prefix=prefix)

    with pytest.raises(AgentMessageAcceptError) as error:
        await AgentInboxService(CommitThenPoisonEnvelope(inner)).accept(
            "a1", message(content="mine"), target=MessageTarget.NEW_TURN, wakeup=False
        )
    assert error.value.committed is True


# ROUND SEVEN: a comparison that could not be made is not a negative answer.


class UnreadableAfterCommit:
    """Commits our event, then hands back a payload canonical encoding cannot read.

    The container parses fine - ``__iter__``, ``get`` and ``__getitem__`` all
    work - so the event passes the protocol gate. Only ``items()``, which
    ``to_json_value()`` needs, raises. That separates "this is not a valid fact"
    from "this could not be compared", and only the first justifies ``False``.
    """

    def __init__(self, inner: EventStore, container) -> None:
        self.inner = inner
        self.container = container
        self.committed = False

    async def append(self, stream_id, *, expected_seq, events, durability=Durability.SYNC):
        await self.inner.append(
            stream_id, expected_seq=expected_seq, events=events, durability=durability
        )
        self.committed = True
        raise RuntimeError("failed after committing")

    async def read(self, stream_id, *, from_seq=1):
        real = await self.inner.read(stream_id, from_seq=from_seq)
        if not self.committed or not stream_id.startswith("agent-inbox:"):
            return real
        return tuple(
            replace_envelope_field(event, "data", self.container(event.data)) for event in real
        )

    async def head(self, stream_id):
        return await self.inner.head(stream_id)

    async def list_streams(self, *, prefix=None):
        return await self.inner.list_streams(prefix=prefix)


class UnreadableItems(dict):
    def items(self):
        raise ValueError("items() is hostile")


class InterruptingItems(dict):
    def items(self):
        raise KeyboardInterrupt()


class ExitingItems(dict):
    def items(self):
        raise SystemExit(7)


async def test_a_committed_message_that_cannot_be_compared_is_unknown_not_uncommitted():
    """Through the real service, not the helper.

    The event really is in the stream; only the comparison failed. Reporting
    ``False`` would say "provably not committed" at the exact moment the
    evidence is weakest, and a caller acting on it would accept the message a
    second time.
    """

    inner = InMemoryEventStore()
    await register(inner, "a1")

    with pytest.raises(AgentMessageAcceptError) as error:
        await AgentInboxService(UnreadableAfterCommit(inner, UnreadableItems)).accept(
            "a1", message(), target=MessageTarget.NEW_TURN, wakeup=False
        )

    assert error.value.committed is None
    assert "unknown" in str(error.value)
    assert len(await inner.read(agent_inbox_stream("a1"))) == 1


@pytest.mark.parametrize(
    ("container", "interrupt"),
    [(InterruptingItems, KeyboardInterrupt), (ExitingItems, SystemExit)],
)
async def test_an_interrupt_during_comparison_still_propagates(container, interrupt):
    inner = InMemoryEventStore()
    await register(inner, "a1")

    with pytest.raises(interrupt):
        await AgentInboxService(UnreadableAfterCommit(inner, container)).accept(
            "a1", message(), target=MessageTarget.NEW_TURN, wakeup=False
        )


async def test_a_readable_stream_still_answers_true_and_false_definitively():
    """The unknown state must not swallow the two knowable ones."""

    inner = InMemoryEventStore()
    await register(inner, "a1")

    with pytest.raises(AgentMessageAcceptError) as committed:
        await AgentInboxService(CommitThenFailStore(inner)).accept(
            "a1", message("m1"), target=MessageTarget.NEW_TURN, wakeup=False
        )
    assert committed.value.committed is True

    gated = GatedStore(inner)
    gated.append_failure = RuntimeError("never committed")
    gated.append_release.set()
    with pytest.raises(AgentMessageAcceptError) as missing:
        await AgentInboxService(gated).accept(
            "a1", message("m2"), target=MessageTarget.NEW_TURN, wakeup=False
        )
    assert missing.value.committed is False
