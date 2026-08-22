"""The delivery lifecycle is what stops a message being executed twice.

Every test here works one boundary: a message is claimed exactly when its
``agent/message-claimed`` event is in the Agent's delivery stream, and a claim
has ended exactly when a terminal event referencing it is there too. Nothing
asserts anything about *what happened inside* a Turn - that is the Session
Event Log's business, and this stream deliberately cannot express it.

Because this projector is what a worker consults before running a model, it
fails closed harder than a display projection would: an event it could not
validate is never read as "no claim", because that reading is the one that runs
the same message again.
"""

from __future__ import annotations

import asyncio
import gc
from dataclasses import replace

import pytest
from supervision_fixtures import (
    InMemoryEventStore,
    delivery_envelope,
    message,
    raw_delivery_append,
    read_delivery,
    register_agent,
    replace_envelope_field,
)

from traceh.agents import AgentInboxReader, AgentInboxService
from traceh.api.agents import MessageTarget
from traceh.session.event_store import ConcurrencyConflict, Durability, EventStore
from traceh.supervision import (
    AgentDeliveryLog,
    AgentDeliveryService,
    DeliveryAppendError,
    DeliveryConflictError,
    DeliveryInputError,
    DeliveryProtocolError,
    agent_delivery_stream,
    cancelled_data,
    claimed_data,
    completed_data,
    failed_data,
    parse_delivery_event,
    validate_agent_delivery_events,
)
from traceh.supervision.delivery_identity import (
    AGENT_MESSAGE_CANCELLED,
    AGENT_MESSAGE_CLAIMED,
    AGENT_MESSAGE_COMPLETED,
    AGENT_MESSAGE_FAILED,
    MAX_REASON_CHARS,
)

AGENT = "agent-1"
SESSION = "session-1"


async def accept(store, *, message_id="m1", agent_id=AGENT, target=MessageTarget.NEW_TURN):
    return await AgentInboxService(store).accept(
        agent_id, message(message_id), target=target, wakeup=False
    )


async def prepared(*, messages=("m1",)):
    """A store with one Agent and some accepted messages."""

    store = InMemoryEventStore()
    await register_agent(store, agent_id=AGENT, session_id=SESSION)
    for message_id in messages:
        await accept(store, message_id=message_id)
    return store


def claim_payload(*, message_id="m1", accepted_seq=1, claim_id="c1", **overrides):
    fields = {
        "agent_id": AGENT,
        "message_id": message_id,
        "accepted_seq": accepted_seq,
        "claim_id": claim_id,
        "activation_id": "act-1",
        "session_id": SESSION,
    }
    fields.update(overrides)
    return claimed_data(**fields)


@pytest.fixture
async def loop_reports():
    loop = asyncio.get_running_loop()
    reports: list[dict] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reports.append(context))
    yield reports
    loop.set_exception_handler(previous)


# 1. The happy path and the three terminal states.


async def test_accepted_claimed_completed_round_trips():
    store = await prepared()
    inbox = await AgentInboxReader(store).load(AGENT)
    service = AgentDeliveryService(store)
    log = await service.delivery_log(AGENT, inbox)

    claim = await service.claim(
        agent_id=AGENT,
        accepted=inbox.get("m1"),
        claim_id="c1",
        activation_id="act-1",
        session_id=SESSION,
        inbox=inbox,
        delivery=log,
    )
    await service.complete(agent_id=AGENT, claim=claim, turn_id="turn-1", reason="completed")

    rebuilt = await read_delivery(store, AGENT)
    assert [item.message_id for item in rebuilt.claims] == ["m1"]
    assert rebuilt.claims[0].accepted_seq == 1
    assert rebuilt.claims[0].activation_id == "act-1"
    assert rebuilt.claims[0].session_id == SESSION
    outcome = rebuilt.outcome_for_message("m1")
    assert (outcome.state, outcome.code, outcome.turn_id) == ("completed", "completed", "turn-1")
    assert rebuilt.is_claimed("m1")
    assert not rebuilt.has_open_claim()
    assert rebuilt.next_unclaimed(await AgentInboxReader(store).load(AGENT)) is None


@pytest.mark.parametrize(
    ("event_type", "builder", "state", "code"),
    [
        (AGENT_MESSAGE_FAILED, "fail", "failed", "turn-failed"),
        (AGENT_MESSAGE_CANCELLED, "cancel", "cancelled", "turn-cancelled"),
    ],
)
async def test_failed_and_cancelled_are_terminal(event_type, builder, state, code):
    store = await prepared()
    inbox = await AgentInboxReader(store).load(AGENT)
    service = AgentDeliveryService(store)
    claim = await service.claim(
        agent_id=AGENT,
        accepted=inbox.get("m1"),
        claim_id="c1",
        activation_id="act-1",
        session_id=SESSION,
        inbox=inbox,
        delivery=await service.delivery_log(AGENT, inbox),
    )
    if builder == "fail":
        await service.fail(agent_id=AGENT, claim=claim, error_code=code)
    else:
        await service.cancel(agent_id=AGENT, claim=claim, reason=code)

    outcome = (await read_delivery(store, AGENT)).outcome_for_message("m1")
    assert (outcome.state, outcome.code, outcome.turn_id) == (state, code, None)


async def test_fifo_next_unclaimed_never_skips_ahead():
    store = await prepared(messages=("m1", "m2", "m3"))
    inbox = await AgentInboxReader(store).load(AGENT)
    log = await read_delivery(store, AGENT)
    assert log.next_unclaimed(inbox).message.message_id == "m1"

    await raw_delivery_append(
        store, AGENT, claim_payload(message_id="m1"), event_type=AGENT_MESSAGE_CLAIMED
    )
    log = await read_delivery(store, AGENT)
    assert log.next_unclaimed(inbox) is None
    assert log.has_open_claim()

    await raw_delivery_append(
        store,
        AGENT,
        completed_data(
            agent_id=AGENT,
            message_id="m1",
            claim_id="c1",
            turn_id="turn-1",
            reason="completed",
        ),
        event_type=AGENT_MESSAGE_COMPLETED,
    )
    log = await read_delivery(store, AGENT)
    assert log.next_unclaimed(inbox).message.message_id == "m2"


async def test_replay_rejects_a_claim_that_skipped_the_fifo_head():
    store = await prepared(messages=("m1", "m2"))
    await raw_delivery_append(
        store,
        AGENT,
        claim_payload(message_id="m2", accepted_seq=2),
        event_type=AGENT_MESSAGE_CLAIMED,
    )

    inbox = await AgentInboxReader(store).load(AGENT)
    with pytest.raises(DeliveryProtocolError) as error:
        await AgentDeliveryService(store).delivery_log(AGENT, inbox)

    assert error.value.code == "delivery-claim-not-next"


async def test_delivery_events_live_only_on_their_own_stream():
    store = await prepared()
    inbox = await AgentInboxReader(store).load(AGENT)
    service = AgentDeliveryService(store)
    await service.claim(
        agent_id=AGENT,
        accepted=inbox.get("m1"),
        claim_id="c1",
        activation_id="act-1",
        session_id=SESSION,
        inbox=inbox,
        delivery=await service.delivery_log(AGENT, inbox),
    )

    streams = set(await store.list_streams())
    assert agent_delivery_stream(AGENT) in streams
    # Stage B's Inbox stream still holds nothing but acceptances: its projector
    # rejects any other type, and that contract is deliberately preserved.
    inbox_events = await store.read(f"agent-inbox:{AGENT}")
    assert {event.type for event in inbox_events} == {"agent/message-accepted"}


async def test_claim_rejects_fabricated_acceptance_without_writing():
    store = await prepared()
    inbox = await AgentInboxReader(store).load(AGENT)
    service = AgentDeliveryService(store)
    fabricated = replace(
        inbox.get("m1"),
        message=replace(inbox.get("m1").message, content="different content"),
    )

    with pytest.raises(DeliveryProtocolError) as error:
        await service.claim(
            agent_id=AGENT,
            accepted=fabricated,
            claim_id="c1",
            activation_id="act-1",
            session_id=SESSION,
            inbox=inbox,
            delivery=await service.delivery_log(AGENT, inbox),
        )

    assert error.value.code == "delivery-message-unknown"
    assert await store.read(agent_delivery_stream(AGENT)) == ()


async def test_claim_rejects_cross_agent_views_without_writing():
    store = await prepared()
    await register_agent(
        store, agent_id="agent-2", session_id="session-2", request_id="request-2"
    )
    await accept(store, agent_id="agent-2", message_id="other-message")
    inbox = await AgentInboxReader(store).load(AGENT)
    other_inbox = await AgentInboxReader(store).load("agent-2")
    service = AgentDeliveryService(store)

    with pytest.raises(DeliveryProtocolError) as error:
        await service.claim(
            agent_id=AGENT,
            accepted=inbox.get("m1"),
            claim_id="c1",
            activation_id="act-1",
            session_id=SESSION,
            inbox=other_inbox,
            delivery=await service.delivery_log("agent-2", other_inbox),
        )

    assert error.value.code == "delivery-inbox-agent-mismatch"
    assert await store.read(agent_delivery_stream(AGENT)) == ()


async def test_terminal_rejects_a_foreign_claim_without_writing():
    store = await prepared()
    inbox = await AgentInboxReader(store).load(AGENT)
    service = AgentDeliveryService(store)
    claim = await service.claim(
        agent_id=AGENT,
        accepted=inbox.get("m1"),
        claim_id="c1",
        activation_id="act-1",
        session_id=SESSION,
        inbox=inbox,
        delivery=await service.delivery_log(AGENT, inbox),
    )
    head = await store.head(agent_delivery_stream(AGENT))

    with pytest.raises(DeliveryProtocolError) as error:
        await service.complete(
            agent_id=AGENT,
            claim=replace(claim, claim_id="not-the-durable-claim"),
            turn_id="turn-1",
            reason="completed",
        )

    assert error.value.code == "delivery-claim-unknown"
    assert await store.head(agent_delivery_stream(AGENT)) == head


# 2. Protocol counter-examples.


@pytest.mark.parametrize(
    ("event_type", "data", "code"),
    [
        (AGENT_MESSAGE_CLAIMED, {"agent_id": AGENT}, "delivery-payload-keys-unexpected"),
        ("agent/message-taken", None, "delivery-event-type-unknown"),
    ],
)
async def test_unknown_types_and_key_sets_fail_closed(event_type, data, code):
    store = await prepared()
    payload = claim_payload() if data is None else data
    await raw_delivery_append(store, AGENT, payload, event_type=event_type)

    with pytest.raises(DeliveryProtocolError) as error:
        await read_delivery(store, AGENT)
    assert error.value.code == code


async def test_an_extra_payload_key_fails_closed():
    store = await prepared()
    payload = claim_payload()
    payload["future_authority"] = "yes"
    await raw_delivery_append(store, AGENT, payload, event_type=AGENT_MESSAGE_CLAIMED)

    with pytest.raises(DeliveryProtocolError) as error:
        await read_delivery(store, AGENT)
    assert error.value.code == "delivery-payload-keys-unexpected"


async def test_an_unsupported_schema_version_fails_closed():
    store = await prepared()
    await raw_delivery_append(
        store, AGENT, claim_payload(), event_type=AGENT_MESSAGE_CLAIMED, schema_version=99
    )

    with pytest.raises(DeliveryProtocolError) as error:
        await read_delivery(store, AGENT)
    assert error.value.code == "delivery-schema-version-unsupported"


async def test_a_delivery_fact_on_another_agents_stream_fails_closed():
    store = await prepared()
    await register_agent(store, agent_id="agent-2", session_id="session-2", request_id="request-2")
    await raw_delivery_append(
        store,
        "agent-2",
        claim_payload(),
        event_type=AGENT_MESSAGE_CLAIMED,
    )

    inbox = await AgentInboxReader(store).load("agent-2")
    events = await store.read(agent_delivery_stream("agent-2"))
    with pytest.raises(DeliveryProtocolError) as error:
        AgentDeliveryLog.rebuild(events, "agent-2", inbox)
    assert error.value.code == "delivery-stream-unexpected"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("agent_id", None, "delivery-identity-invalid"),
        ("message_id", 7, "delivery-identity-invalid"),
        ("claim_id", "", "delivery-identity-invalid"),
        ("activation_id", " padded", "delivery-identity-invalid"),
        ("session_id", True, "delivery-identity-invalid"),
        ("accepted_seq", 0, "delivery-accepted-seq-invalid"),
        ("accepted_seq", True, "delivery-accepted-seq-invalid"),
        ("accepted_seq", "1", "delivery-accepted-seq-invalid"),
        ("accepted_seq", 1.0, "delivery-accepted-seq-invalid"),
    ],
)
async def test_malformed_claim_fields_fail_closed(field, value, code):
    store = await prepared()
    payload = claim_payload()
    payload[field] = value
    await raw_delivery_append(store, AGENT, payload, event_type=AGENT_MESSAGE_CLAIMED)

    with pytest.raises(DeliveryProtocolError) as error:
        await read_delivery(store, AGENT)
    assert error.value.code == code


@pytest.mark.parametrize(
    "reason",
    [None, 7, "", "   ", "a\nb", "a\x1bb", "x" * (MAX_REASON_CHARS + 1)],
    ids=["none", "int", "empty", "blank", "newline", "escape", "oversized"],
)
async def test_malformed_reasons_fail_closed(reason):
    store = await prepared()
    await raw_delivery_append(
        store, AGENT, claim_payload(), event_type=AGENT_MESSAGE_CLAIMED
    )
    payload = completed_data(
        agent_id=AGENT, message_id="m1", claim_id="c1", turn_id="turn-1", reason="completed"
    )
    payload["reason"] = reason
    await raw_delivery_append(store, AGENT, payload, event_type=AGENT_MESSAGE_COMPLETED)

    with pytest.raises(DeliveryProtocolError) as error:
        await read_delivery(store, AGENT)
    assert error.value.code == "delivery-reason-invalid"


async def test_a_claim_on_a_message_this_agent_never_accepted_fails_closed():
    store = await prepared()
    await raw_delivery_append(
        store, AGENT, claim_payload(message_id="never-sent"), event_type=AGENT_MESSAGE_CLAIMED
    )

    with pytest.raises(DeliveryProtocolError) as error:
        await read_delivery(store, AGENT)
    assert error.value.code == "delivery-message-unknown"


async def test_a_claim_that_disagrees_with_the_accepted_position_fails_closed():
    """``accepted_seq`` pins the claim to one exact place in the Inbox."""

    store = await prepared(messages=("m1", "m2"))
    await raw_delivery_append(
        store,
        AGENT,
        claim_payload(message_id="m1", accepted_seq=2),
        event_type=AGENT_MESSAGE_CLAIMED,
    )

    with pytest.raises(DeliveryProtocolError) as error:
        await read_delivery(store, AGENT)
    assert error.value.code == "delivery-accepted-seq-mismatch"


async def test_a_message_cannot_be_claimed_twice():
    store = await prepared()
    await raw_delivery_append(store, AGENT, claim_payload(), event_type=AGENT_MESSAGE_CLAIMED)
    await raw_delivery_append(
        store, AGENT, claim_payload(claim_id="c2"), event_type=AGENT_MESSAGE_CLAIMED
    )

    with pytest.raises(DeliveryProtocolError) as error:
        await read_delivery(store, AGENT)
    assert error.value.code == "delivery-claim-duplicate"


async def test_two_claims_cannot_share_a_claim_id():
    store = await prepared(messages=("m1", "m2"))
    await raw_delivery_append(store, AGENT, claim_payload(), event_type=AGENT_MESSAGE_CLAIMED)
    await raw_delivery_append(
        store,
        AGENT,
        claim_payload(message_id="m2", accepted_seq=2, claim_id="c1"),
        event_type=AGENT_MESSAGE_CLAIMED,
    )

    with pytest.raises(DeliveryProtocolError) as error:
        await read_delivery(store, AGENT)
    assert error.value.code == "delivery-claim-id-duplicate"


@pytest.mark.parametrize(
    ("event_type", "builder"),
    [
        (AGENT_MESSAGE_COMPLETED, "completed"),
        (AGENT_MESSAGE_FAILED, "failed"),
        (AGENT_MESSAGE_CANCELLED, "cancelled"),
    ],
)
async def test_a_terminal_without_a_claim_fails_closed(event_type, builder):
    store = await prepared()
    payload = _terminal_payload(builder, claim_id="missing")
    await raw_delivery_append(store, AGENT, payload, event_type=event_type)

    with pytest.raises(DeliveryProtocolError) as error:
        await read_delivery(store, AGENT)
    assert error.value.code == "delivery-claim-unknown"


def _terminal_payload(builder, *, claim_id="c1", message_id="m1"):
    if builder == "completed":
        return completed_data(
            agent_id=AGENT,
            message_id=message_id,
            claim_id=claim_id,
            turn_id="turn-1",
            reason="completed",
        )
    if builder == "failed":
        return failed_data(
            agent_id=AGENT, message_id=message_id, claim_id=claim_id, error_code="turn-failed"
        )
    return cancelled_data(
        agent_id=AGENT, message_id=message_id, claim_id=claim_id, reason="turn-cancelled"
    )


@pytest.mark.parametrize(
    ("first", "first_type", "second", "second_type"),
    [
        ("completed", AGENT_MESSAGE_COMPLETED, "completed", AGENT_MESSAGE_COMPLETED),
        ("completed", AGENT_MESSAGE_COMPLETED, "failed", AGENT_MESSAGE_FAILED),
        ("failed", AGENT_MESSAGE_FAILED, "cancelled", AGENT_MESSAGE_CANCELLED),
        ("cancelled", AGENT_MESSAGE_CANCELLED, "completed", AGENT_MESSAGE_COMPLETED),
    ],
)
async def test_a_claim_reaches_a_terminal_state_exactly_once(
    first, first_type, second, second_type
):
    """completed, failed and cancelled are mutually exclusive."""

    store = await prepared()
    await raw_delivery_append(store, AGENT, claim_payload(), event_type=AGENT_MESSAGE_CLAIMED)
    await raw_delivery_append(store, AGENT, _terminal_payload(first), event_type=first_type)
    await raw_delivery_append(store, AGENT, _terminal_payload(second), event_type=second_type)

    with pytest.raises(DeliveryProtocolError) as error:
        await read_delivery(store, AGENT)
    assert error.value.code == "delivery-claim-already-terminal"


async def test_a_terminal_that_disagrees_with_its_claim_fails_closed():
    store = await prepared(messages=("m1", "m2"))
    await raw_delivery_append(store, AGENT, claim_payload(), event_type=AGENT_MESSAGE_CLAIMED)
    await raw_delivery_append(
        store,
        AGENT,
        _terminal_payload("completed", message_id="m2"),
        event_type=AGENT_MESSAGE_COMPLETED,
    )

    with pytest.raises(DeliveryProtocolError) as error:
        await read_delivery(store, AGENT)
    assert error.value.code == "delivery-claim-message-mismatch"


async def test_the_validator_reports_issues_without_raising():
    store = await prepared()
    await raw_delivery_append(
        store, AGENT, claim_payload(message_id="never-sent"), event_type=AGENT_MESSAGE_CLAIMED
    )
    inbox = await AgentInboxReader(store).load(AGENT)
    events = await store.read(agent_delivery_stream(AGENT))

    issues = validate_agent_delivery_events(events, AGENT, inbox)
    assert [(issue.code, issue.seq) for issue in issues] == [("delivery-message-unknown", 1)]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("agent_id", ""),
        ("message_id", None),
        ("claim_id", 5),
        ("activation_id", "a\nb"),
        ("session_id", " padded"),
        ("accepted_seq", 0),
    ],
)
def test_the_public_claim_builder_refuses_instead_of_repairing(field, value):
    with pytest.raises(DeliveryInputError) as error:
        claim_payload(**{field: value})
    assert error.value.field == field


def test_the_public_terminal_builders_refuse_unusable_reasons():
    for builder in (completed_data, failed_data, cancelled_data):
        kwargs = {"agent_id": AGENT, "message_id": "m1", "claim_id": "c1"}
        if builder is completed_data:
            kwargs |= {"turn_id": "turn-1", "reason": "a\nb"}
        elif builder is failed_data:
            kwargs |= {"error_code": "a\nb"}
        else:
            kwargs |= {"reason": "a\nb"}
        with pytest.raises(DeliveryInputError):
            builder(**kwargs)


# 3. Hostile access: reading an event is itself untrusted work.


class RaisingIteration(dict):
    def __iter__(self):
        raise ValueError("__iter__ is hostile")


class RaisingGet(dict):
    def get(self, *args, **kwargs):
        raise RuntimeError("get() is hostile")


class HostileComparison(str):
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


# Only the access paths the delivery parser actually uses. ``set(data)`` reads
# ``__iter__`` and the field readers use ``get``; ``__getitem__`` is *not* on
# this path, and listing it would assert something untrue.
HOSTILE_PAYLOADS = [RaisingIteration, RaisingGet]


def hostile_value(field: str, container):
    """A poisoned envelope field the parser will actually compare.

    ``event.type`` is tested for membership in a ``frozenset``, so a value whose
    hash matches nothing is simply an unknown type - a correct answer, but not
    the one under test. Reusing the real strings makes each comparison reach
    ``__eq__``/``__ne__``, which is what must not leak.
    """

    if field == "type":
        return container(AGENT_MESSAGE_CLAIMED)
    if field == "stream_id":
        return container(agent_delivery_stream(AGENT))
    return container("1")


@pytest.mark.parametrize("container", HOSTILE_PAYLOADS)
async def test_a_payload_that_raises_while_being_read_is_a_protocol_error(container):
    store = await prepared()
    inbox = await AgentInboxReader(store).load(AGENT)
    event = delivery_envelope(container(claim_payload()), event_type=AGENT_MESSAGE_CLAIMED)

    with pytest.raises(DeliveryProtocolError) as error:
        AgentDeliveryLog.rebuild((event,), AGENT, inbox)
    assert error.value.code == "delivery-payload-invalid"
    assert [issue.code for issue in validate_agent_delivery_events((event,), AGENT, inbox)] == [
        "delivery-payload-invalid"
    ]


@pytest.mark.parametrize("field", ["type", "stream_id", "schema_version"])
async def test_a_hostile_envelope_protocol_field_is_a_protocol_error(field):
    """`EventEnvelope` is a public DTO, so its protocol fields are untrusted too."""

    store = await prepared()
    inbox = await AgentInboxReader(store).load(AGENT)
    event = replace_envelope_field(
        delivery_envelope(claim_payload(), event_type=AGENT_MESSAGE_CLAIMED),
        field,
        hostile_value(field, HostileComparison),
    )

    with pytest.raises(DeliveryProtocolError) as error:
        AgentDeliveryLog.rebuild((event,), AGENT, inbox)
    assert error.value.code == "delivery-payload-invalid"
    assert [issue.code for issue in validate_agent_delivery_events((event,), AGENT, inbox)] == [
        "delivery-payload-invalid"
    ]


@pytest.mark.parametrize("field", ["type", "stream_id", "schema_version"])
def test_the_parser_itself_converts_a_hostile_envelope_field(field):
    """Pinned on the parser directly: ``_scan`` has its own net, and the parser
    is public and is also what commit reconciliation calls."""

    event = replace_envelope_field(
        delivery_envelope(claim_payload(), event_type=AGENT_MESSAGE_CLAIMED),
        field,
        hostile_value(field, HostileComparison),
    )
    with pytest.raises(DeliveryProtocolError) as error:
        parse_delivery_event(event)
    assert error.value.code == "delivery-payload-invalid"


@pytest.mark.parametrize(
    ("container", "interrupt"),
    [(InterruptingComparison, KeyboardInterrupt), (ExitingComparison, SystemExit)],
)
@pytest.mark.parametrize("field", ["type", "stream_id", "schema_version"])
async def test_an_interrupt_from_an_envelope_field_is_not_a_protocol_error(
    field, container, interrupt
):
    store = await prepared()
    inbox = await AgentInboxReader(store).load(AGENT)
    event = replace_envelope_field(
        delivery_envelope(claim_payload(), event_type=AGENT_MESSAGE_CLAIMED),
        field,
        hostile_value(field, container),
    )

    with pytest.raises(interrupt):
        AgentDeliveryLog.rebuild((event,), AGENT, inbox)
    with pytest.raises(interrupt):
        validate_agent_delivery_events((event,), AGENT, inbox)
    with pytest.raises(interrupt):
        parse_delivery_event(event)


async def test_an_ordinary_dict_subclass_payload_is_still_read():
    class Ordinary(dict):
        pass

    store = await prepared()
    inbox = await AgentInboxReader(store).load(AGENT)
    event = delivery_envelope(Ordinary(claim_payload()), event_type=AGENT_MESSAGE_CLAIMED)
    assert AgentDeliveryLog.rebuild((event,), AGENT, inbox).is_claimed("m1")


# 4. Commit reconciliation: True, False and unknown.


class LosingRaceStore:
    """Lets another writer commit under the same claim id, then fails our append."""

    def __init__(self, inner: EventStore, other_writer) -> None:
        self.inner = inner
        self.other_writer = other_writer
        self.raced = False

    async def append(self, stream_id, *, expected_seq, events, durability=Durability.SYNC):
        if not self.raced and stream_id.startswith("agent-delivery:"):
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
    def __init__(self, inner: EventStore, *, error=None) -> None:
        self.inner = inner
        self.error = error if error is not None else RuntimeError("failed after committing")

    async def append(self, stream_id, *, expected_seq, events, durability=Durability.SYNC):
        result = await self.inner.append(
            stream_id, expected_seq=expected_seq, events=events, durability=durability
        )
        if stream_id.startswith("agent-delivery:"):
            raise self.error
        return result

    async def read(self, stream_id, *, from_seq=1):
        return await self.inner.read(stream_id, from_seq=from_seq)

    async def head(self, stream_id):
        return await self.inner.head(stream_id)

    async def list_streams(self, *, prefix=None):
        return await self.inner.list_streams(prefix=prefix)


class UnreadableItems(dict):
    def items(self):
        raise ValueError("items() is hostile")


class CommitThenUnreadableStore(CommitThenFailStore):
    async def read(self, stream_id, *, from_seq=1):
        real = await self.inner.read(stream_id, from_seq=from_seq)
        if not stream_id.startswith("agent-delivery:"):
            return real
        return tuple(
            replace_envelope_field(event, "data", UnreadableItems(event.data)) for event in real
        )


async def _claim_through(store, inner):
    inbox = await AgentInboxReader(inner).load(AGENT)
    service = AgentDeliveryService(store)
    return await service.claim(
        agent_id=AGENT,
        accepted=inbox.get("m1"),
        claim_id="c1",
        activation_id="act-1",
        session_id=SESSION,
        inbox=inbox,
        delivery=await AgentDeliveryService(inner).delivery_log(AGENT, inbox),
    )


async def test_losing_the_claim_race_is_a_conflict_and_writes_nothing():
    inner = await prepared()

    async def other_writer():
        await raw_delivery_append(
            inner, AGENT, claim_payload(claim_id="other"), event_type=AGENT_MESSAGE_CLAIMED
        )

    store = LosingRaceStore(inner, other_writer)
    inbox = await AgentInboxReader(inner).load(AGENT)
    service = AgentDeliveryService(store)
    with pytest.raises(DeliveryAppendError) as error:
        await service.claim(
            agent_id=AGENT,
            accepted=inbox.get("m1"),
            claim_id="c1",
            activation_id="act-1",
            session_id=SESSION,
            inbox=inbox,
            delivery=await AgentDeliveryService(inner).delivery_log(AGENT, inbox),
        )
    # Another writer's claim is not ours: the answer is "not committed", not
    # "committed" just because *a* claim with that message id now exists.
    assert error.value.committed is False
    assert (await read_delivery(inner, AGENT)).claim_for("m1").claim_id == "other"


async def test_a_stale_read_loses_the_compare_and_swap():
    """The claim carries the head the decision was made from."""

    inner = await prepared(messages=("m1", "m2"))
    inbox = await AgentInboxReader(inner).load(AGENT)
    stale = await AgentDeliveryService(inner).delivery_log(AGENT, inbox)
    await raw_delivery_append(
        inner, AGENT, claim_payload(message_id="m1", accepted_seq=1, claim_id="other"),
        event_type=AGENT_MESSAGE_CLAIMED,
    )

    with pytest.raises(DeliveryConflictError):
        await AgentDeliveryService(inner).claim(
            agent_id=AGENT,
            accepted=inbox.get("m1"),
            claim_id="c1",
            activation_id="act-1",
            session_id=SESSION,
            inbox=inbox,
            delivery=stale,
        )
    assert len(await read_delivery(inner, AGENT)) == 1


async def test_our_own_committed_claim_reports_committed_true():
    inner = await prepared()
    with pytest.raises(DeliveryAppendError) as error:
        await _claim_through(CommitThenFailStore(inner), inner)
    assert error.value.committed is True
    assert (await read_delivery(inner, AGENT)).claim_for("m1").claim_id == "c1"


async def test_a_comparison_that_cannot_be_made_is_unknown_not_uncommitted():
    """A claim we cannot prove is a claim we must not act on."""

    inner = await prepared()
    with pytest.raises(DeliveryAppendError) as error:
        await _claim_through(CommitThenUnreadableStore(inner), inner)
    assert error.value.committed is None
    assert "unknown" in str(error.value)
    assert len(await inner.read(agent_delivery_stream(AGENT))) == 1


@pytest.mark.parametrize("interrupt", [SystemExit(3), KeyboardInterrupt()])
async def test_interpreter_signals_are_not_rewritten(interrupt):
    inner = await prepared()
    with pytest.raises(type(interrupt)) as error:
        await _claim_through(CommitThenFailStore(inner, error=interrupt), inner)
    assert error.value is interrupt


async def test_a_concurrency_conflict_that_did_commit_is_not_a_lost_cas():
    inner = await prepared()
    store = CommitThenFailStore(inner, error=ConcurrencyConflict("late"))
    with pytest.raises(DeliveryAppendError) as error:
        await _claim_through(store, inner)
    assert not isinstance(error.value, DeliveryConflictError)
    assert error.value.committed is True


async def test_claims_and_outcomes_are_immutable_scalars():
    """The projector hands out its retained objects, which is only safe while
    every field is an immutable scalar."""

    store = await prepared()
    inbox = await AgentInboxReader(store).load(AGENT)
    service = AgentDeliveryService(store)
    claim = await service.claim(
        agent_id=AGENT,
        accepted=inbox.get("m1"),
        claim_id="c1",
        activation_id="act-1",
        session_id=SESSION,
        inbox=inbox,
        delivery=await service.delivery_log(AGENT, inbox),
    )
    await service.complete(agent_id=AGENT, claim=claim, turn_id="turn-1", reason="completed")
    log = await read_delivery(store, AGENT)

    import dataclasses

    immutable = (str, bool, int, float, type(None))
    for record in (log.claims[0], log.outcome_for_message("m1")):
        for field in dataclasses.fields(record):
            assert isinstance(getattr(record, field.name), immutable), field.name
        assert type(record).__dataclass_params__.frozen

    gc.collect()
    assert (await read_delivery(store, AGENT)).outcome_for_message("m1").state == "completed"
