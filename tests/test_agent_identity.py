"""Durable Agent identity is a fact in the log, not an object in a process.

Every test here works the boundary v0.6 Stage A exists to establish: an Agent
exists because ``agent/created`` is in the control-plane stream, and for no
other reason. Nothing in this file asserts that a returned object is not
another object - the assertions are that a *fresh* projector, reader and store
instance, holding nothing from the process that wrote the events, recovers the
same identity, and that broken or contradictory history is reported rather than
repaired.

Concurrency and cancellation use explicit gates. There is no ``sleep()`` used to
guess timing: a store stub lights an `asyncio.Event` when it has genuinely
entered the append, and the test only then cancels.
"""

from __future__ import annotations

import asyncio
import gc
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from traceh.agents import (  # noqa: F401
    AGENT_CREATED,
    AGENT_DIRECTORY_STREAM,
    AgentCreationError,
    AgentDirectory,
    AgentDirectoryConflictError,
    AgentDirectoryProtocolError,
    AgentDirectoryReader,
    AgentIdentityConflictError,
    AgentIdentityError,
    AgentOwnerNotFoundError,
    AgentRegistrar,
    AgentRequestConflictError,
    AgentSessionConflictError,
    agent_created_data,
    is_agent_identifier,
    parse_agent_created,  # noqa: F811
    validate_agent_directory_events,
)
from traceh.agents.identity import (
    AGENT_CREATED_KEYS,
    MAX_BUDGET_VALUE,
    MAX_METADATA_DEPTH,
)
from traceh.api.agents import AgentRecord, AgentSpec, Budget
from traceh.api.events import EventEnvelope, PendingEvent
from traceh.session.event_store import (
    ConcurrencyConflict,
    Durability,
    EventStore,
    InMemoryEventStore,
)
from traceh.session.jsonl import JsonlEventStore

SPEC = AgentSpec(preset="coder", workspace_id="workspace-1")


def spec(**overrides) -> AgentSpec:
    fields = {
        "preset": "coder",
        "workspace_id": "workspace-1",
        **overrides,
    }
    return AgentSpec(**fields)


async def read_directory(store: EventStore) -> AgentDirectory:
    """Rebuild through objects that never saw the write path."""

    return await AgentDirectoryReader(store).load()


async def raw_append(store: EventStore, data: dict, *, event_type: str = AGENT_CREATED) -> None:
    """Append a payload directly, bypassing every registrar check.

    This is how the tests produce histories the writer would never create:
    corrupt data, contradictions, and payloads that assert facts about other
    Agents.
    """

    head = await store.head(AGENT_DIRECTORY_STREAM)
    await store.append(
        AGENT_DIRECTORY_STREAM,
        expected_seq=head,
        events=(PendingEvent(type=event_type, data=data),),
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
    """An `EventStore` whose append and read can be stopped at an exact point.

    ``commit_first`` decides which side of the store's documented commit-point
    boundary the test is exercising: ``False`` blocks before anything is
    written, ``True`` writes and *then* blocks, which is the may-have-committed
    case a caller cannot distinguish by looking at the exception alone.
    """

    def __init__(self, inner: EventStore, *, commit_first: bool = False) -> None:
        self.inner = inner
        self.commit_first = commit_first
        self.append_entered = asyncio.Event()
        self.append_release = asyncio.Event()
        self.read_entered = asyncio.Event()
        self.read_release = asyncio.Event()
        self.gate_reads = False
        self.stale_reads = False
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
        if self.stale_reads:
            # Snapshot first, block second, return the snapshot: the caller's
            # view is genuinely out of date by the time it acts on it, which is
            # what a second writer in another process produces.
            snapshot = await self.inner.read(stream_id, from_seq=from_seq)
            self.read_entered.set()
            await self.read_release.wait()
            return snapshot
        if self.gate_reads:
            self.read_entered.set()
            await self.read_release.wait()
        return await self.inner.read(stream_id, from_seq=from_seq)

    async def head(self, stream_id):
        return await self.inner.head(stream_id)

    async def list_streams(self, *, prefix=None):
        return await self.inner.list_streams(prefix=prefix)


class YieldingStore:
    """A store whose reads and appends genuinely suspend.

    `InMemoryEventStore` never awaits anything, so two tasks driving it can
    never interleave: the first runs to completion before the second starts. A
    concurrency test built on it would pass even against a registrar with no
    linearization at all - which is exactly what reverse verification caught.

    One ``asyncio.sleep(0)`` is a deterministic yield point, because asyncio's
    ready queue is FIFO. It is not a guess about how long anything takes.
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


# 1. Ordinary creation, then rebuilt by instances that share nothing with it.


async def test_created_agent_is_rebuilt_from_a_fresh_projector(tmp_path):
    store = JsonlEventStore(tmp_path / "data")
    record = await AgentRegistrar(store).create_agent(
        spec(forked_from_session_id="session-origin", capability_grants=("read",)),
        request_id="request-1",
        agent_id="agent-1",
        session_id="session-1",
    )

    # A second store object over the same directory: no shared Python state
    # with the writer at all, only the files.
    reopened = JsonlEventStore(tmp_path / "data")
    directory = await read_directory(reopened)

    assert [item.agent_id for item in directory] == ["agent-1"]
    rebuilt = directory.get("agent-1")
    assert rebuilt == record
    assert rebuilt.session_id == "session-1"
    assert rebuilt.forked_from_session_id == "session-origin"
    assert rebuilt.capability_grants == ("read",)
    # Equal, deliberately not identical: every lookup returns a detached copy
    # so a caller cannot write through the directory (see the detach test).
    assert directory.for_session("session-1") == rebuilt
    assert directory.for_request("request-1") == rebuilt
    assert directory.head_seq == 1


async def test_lookup_by_session_id_finds_the_owning_agent():
    store = InMemoryEventStore()
    registrar = AgentRegistrar(store)
    await registrar.create_agent(SPEC, request_id="r1", agent_id="a1", session_id="s1")
    await registrar.create_agent(SPEC, request_id="r2", agent_id="a2", session_id="s2")

    directory = await read_directory(store)
    assert directory.for_session("s2").agent_id == "a2"
    assert directory.for_session("unknown") is None
    assert len(directory) == 2


async def test_creation_writes_exactly_one_event_on_its_own_stream():
    store = InMemoryEventStore()
    await AgentRegistrar(store).create_agent(SPEC, request_id="r1")

    assert await store.list_streams() == (AGENT_DIRECTORY_STREAM,)
    events = await store.read(AGENT_DIRECTORY_STREAM)
    assert [event.type for event in events] == [AGENT_CREATED]


# 2. Identity does not come from, and does not depend on, a live Activation.


async def test_identity_survives_an_activation_that_is_built_and_dropped():
    """Stand in for an Activation with a plain object, then destroy it.

    An `AgentRuntime` is exactly this from the directory's point of view: a
    process-local object. If identity depended on one, dropping it would change
    the answer.
    """

    store = InMemoryEventStore()
    registrar = AgentRegistrar(store)
    record = await registrar.create_agent(SPEC, request_id="r1", agent_id="a1", session_id="s1")

    class Activation:
        def __init__(self, agent_id: str) -> None:
            self.agent_id = agent_id
            self.running = True

        def stop(self) -> None:
            self.running = False

    activation = Activation(record.agent_id)
    activation.stop()
    del activation
    gc.collect()

    # A different registrar object, a different reader, a different directory.
    assert (await AgentRegistrar(store).directory()).get("a1") == record

    restarted = Activation(record.agent_id)
    assert restarted.running
    assert (await read_directory(store)).get("a1") == record


async def test_directory_holds_no_reference_to_the_registrar_that_wrote_it():
    store = InMemoryEventStore()
    registrar = AgentRegistrar(store)
    record = await registrar.create_agent(SPEC, request_id="r1", agent_id="a1")
    del registrar
    gc.collect()

    assert (await read_directory(store)).get("a1") == record


# 3. agent_id conflicts.


async def test_a_second_agent_cannot_take_an_existing_agent_id():
    store = InMemoryEventStore()
    registrar = AgentRegistrar(store)
    await registrar.create_agent(SPEC, request_id="r1", agent_id="a1", session_id="s1")

    with pytest.raises(AgentIdentityConflictError) as error:
        await registrar.create_agent(SPEC, request_id="r2", agent_id="a1", session_id="s2")

    assert error.value.code == "agent-id-taken"
    assert len(await store.read(AGENT_DIRECTORY_STREAM)) == 1


async def test_replay_fails_closed_on_a_duplicated_agent_id():
    """Last write must not win: this is a log, not a mutable registry."""

    store = InMemoryEventStore()
    await AgentRegistrar(store).create_agent(SPEC, request_id="r1", agent_id="a1", session_id="s1")
    await raw_append(
        store,
        agent_created_data(agent_id="a1", session_id="s9", request_id="r9", spec=spec()),
    )

    with pytest.raises(AgentDirectoryProtocolError) as error:
        await read_directory(store)
    assert error.value.code == "agent-id-duplicate"
    assert error.value.seq == 2
    assert [issue.code for issue in validate_agent_directory_events(
        await store.read(AGENT_DIRECTORY_STREAM)
    )] == ["agent-id-duplicate"]


# 4. session_id conflicts.


async def test_two_agents_cannot_own_one_session():
    store = InMemoryEventStore()
    registrar = AgentRegistrar(store)
    await registrar.create_agent(SPEC, request_id="r1", agent_id="a1", session_id="s1")

    with pytest.raises(AgentSessionConflictError) as error:
        await registrar.create_agent(SPEC, request_id="r2", agent_id="a2", session_id="s1")

    assert error.value.code == "agent-session-taken"
    directory = await read_directory(store)
    assert len(directory) == 1
    assert directory.for_session("s1").agent_id == "a1"


async def test_replay_fails_closed_when_history_gives_one_session_two_agents():
    store = InMemoryEventStore()
    await AgentRegistrar(store).create_agent(SPEC, request_id="r1", agent_id="a1", session_id="s1")
    await raw_append(
        store,
        agent_created_data(agent_id="a2", session_id="s1", request_id="r2", spec=spec()),
    )

    with pytest.raises(AgentDirectoryProtocolError) as error:
        await read_directory(store)
    assert error.value.code == "agent-session-duplicate"


async def test_one_agent_cannot_become_a_different_session_on_replay():
    """An externally injected payload cannot rebind an existing identity."""

    store = InMemoryEventStore()
    record = await AgentRegistrar(store).create_agent(
        SPEC, request_id="r1", agent_id="a1", session_id="s1"
    )
    await raw_append(
        store,
        agent_created_data(agent_id="a1", session_id="s-other", request_id="r2", spec=spec()),
    )

    with pytest.raises(AgentDirectoryProtocolError):
        await read_directory(store)

    # And the first fact was never altered by the attempt.
    events = await store.read(AGENT_DIRECTORY_STREAM)
    assert events[0].data["session_id"] == "s1"
    assert AgentDirectory.rebuild(events[:1]).get("a1") == record


# 5. Lineage, ownership and communication are separate relations.


async def test_lineage_and_ownership_are_independent_fields():
    store = InMemoryEventStore()
    registrar = AgentRegistrar(store)
    parent = await registrar.create_agent(SPEC, request_id="r1", agent_id="parent", session_id="s1")

    # Forked from the parent's Session but owned by nobody.
    orphan_lineage = await registrar.create_agent(
        spec(forked_from_session_id=parent.session_id),
        request_id="r2",
        agent_id="lineage-only",
        session_id="s2",
    )
    # Owned by the parent but with no shared history.
    owned_only = await registrar.create_agent(
        spec(owner_agent_id="parent"),
        request_id="r3",
        agent_id="owned-only",
        session_id="s3",
    )

    assert orphan_lineage.forked_from_session_id == "s1"
    assert orphan_lineage.owner_agent_id is None
    assert owned_only.owner_agent_id == "parent"
    assert owned_only.forked_from_session_id is None

    directory = await read_directory(store)
    assert [item.agent_id for item in directory.children_of("parent")] == ["owned-only"]
    # Lineage does not create ownership, so the forked Agent is not a child.
    assert directory.children_of("lineage-only") == ()


async def test_creation_records_no_communication_relation():
    """Stage A must not let a message source hide inside a creation fact."""

    store = InMemoryEventStore()
    await AgentRegistrar(store).create_agent(
        spec(owner_agent_id=None), request_id="r1", agent_id="a1"
    )
    payload = (await store.read(AGENT_DIRECTORY_STREAM))[0].data

    assert set(payload) == {
        "agent_id",
        "session_id",
        "request_id",
        "preset",
        "workspace_id",
        "owner_agent_id",
        "forked_from_session_id",
        "capability_grants",
        "budget",
        "metadata",
    }
    assert not any(
        key in payload for key in ("inbox", "messages", "source", "reply_to", "wakeup")
    )


async def test_owner_must_already_exist_and_cannot_be_itself():
    store = InMemoryEventStore()
    registrar = AgentRegistrar(store)

    with pytest.raises(AgentOwnerNotFoundError):
        await registrar.create_agent(
            spec(owner_agent_id="ghost"), request_id="r1", agent_id="a1"
        )
    with pytest.raises(AgentOwnerNotFoundError):
        await registrar.create_agent(
            spec(owner_agent_id="a1"), request_id="r2", agent_id="a1"
        )
    assert await store.read(AGENT_DIRECTORY_STREAM) == ()


async def test_replay_rejects_self_ownership_and_dangling_owners():
    store = InMemoryEventStore()
    await raw_append(
        store,
        agent_created_data(
            agent_id="a1", session_id="s1", request_id="r1", spec=spec(owner_agent_id="a1")
        ),
    )
    with pytest.raises(AgentDirectoryProtocolError) as error:
        await read_directory(store)
    assert error.value.code == "agent-owner-self"

    other = InMemoryEventStore()
    await raw_append(
        other,
        agent_created_data(
            agent_id="a1", session_id="s1", request_id="r1", spec=spec(owner_agent_id="later")
        ),
    )
    with pytest.raises(AgentDirectoryProtocolError) as error:
        await read_directory(other)
    assert error.value.code == "agent-owner-unknown"


# 6. Malformed input and malformed history both fail closed.


UNUSABLE_IDENTIFIERS = [
    True, False, 0, 1, 3.5, "", "   ", "\t", " padded", "padded ",
    "a\nb", "a\x1bb", "a\u2028b", "a\u202eb",
    "x" * 257, ["a"], {"a": 1},
]


@pytest.mark.parametrize("value", [*UNUSABLE_IDENTIFIERS, None])
async def test_an_unusable_request_id_is_rejected_before_anything_is_written(value):
    store = InMemoryEventStore()

    assert not is_agent_identifier(value)
    with pytest.raises(AgentIdentityError) as error:
        await AgentRegistrar(store).create_agent(SPEC, request_id=value)
    assert error.value.field == "request_id"
    assert await store.read(AGENT_DIRECTORY_STREAM) == ()


@pytest.mark.parametrize("value", UNUSABLE_IDENTIFIERS)
async def test_unusable_agent_and_session_ids_are_rejected(value):
    """``None`` is excluded: it means "assign one", not "this bad value"."""

    store = InMemoryEventStore()
    registrar = AgentRegistrar(store)

    assert not is_agent_identifier(value)
    with pytest.raises(AgentIdentityError) as error:
        await registrar.create_agent(SPEC, request_id="r1", agent_id=value)
    assert error.value.field == "agent_id"
    with pytest.raises(AgentIdentityError) as error:
        await registrar.create_agent(SPEC, request_id="r1", session_id=value)
    assert error.value.field == "session_id"
    assert await store.read(AGENT_DIRECTORY_STREAM) == ()


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"preset": ""}, "preset"),
        ({"workspace_id": " padded"}, "workspace_id"),
        ({"owner_agent_id": "a\nb"}, "owner_agent_id"),
        ({"forked_from_session_id": 7}, "forked_from_session_id"),
        ({"capability_grants": ("read", "read")}, "capability_grants"),
        ({"capability_grants": ("",)}, "capability_grants"),
    ],
)
async def test_an_unusable_specification_is_rejected_before_anything_is_written(
    overrides, field
):
    store = InMemoryEventStore()

    with pytest.raises(AgentIdentityError) as error:
        await AgentRegistrar(store).create_agent(spec(**overrides), request_id="r1")
    assert error.value.field == field
    assert await store.read(AGENT_DIRECTORY_STREAM) == ()


async def test_identity_errors_never_echo_the_rejected_value():
    """The usual way to break this setting is to paste a token into it."""

    store = InMemoryEventStore()
    secret = "sk-proj-FAKE-FIXTURE-NOT-A-REAL-KEY"
    with pytest.raises(AgentIdentityError) as error:
        await AgentRegistrar(store).create_agent(SPEC, request_id=f"{secret}\n")

    message = str(error.value)
    assert secret not in message
    assert "FAKE" not in message
    assert str(len(secret)) not in message
    assert message == "agent request_id is not a usable identity"


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda data: data.__setitem__("agent_id", None), "agent-identity-invalid"),
        (lambda data: data.__setitem__("agent_id", 7), "agent-identity-invalid"),
        (lambda data: data.__setitem__("session_id", "  "), "agent-identity-invalid"),
        (lambda data: data.__setitem__("request_id", True), "agent-identity-invalid"),
        (lambda data: data.__setitem__("preset", ""), "agent-identity-invalid"),
        (lambda data: data.pop("workspace_id"), "agent-payload-keys-unexpected"),
        (lambda data: data.pop("owner_agent_id"), "agent-payload-keys-unexpected"),
        (lambda data: data.pop("forked_from_session_id"), "agent-payload-keys-unexpected"),
        (lambda data: data.__setitem__("owner_agent_id", 1), "agent-identity-invalid"),
        (lambda data: data.__setitem__("capability_grants", "read"), "agent-grants-invalid"),
        (lambda data: data.__setitem__("capability_grants", ["a", "a"]), "agent-grants-invalid"),
        (lambda data: data.__setitem__("capability_grants", [None]), "agent-grants-invalid"),
        (lambda data: data.__setitem__("budget", None), "agent-budget-invalid"),
        (lambda data: data.__setitem__("budget", {}), "agent-budget-invalid"),
        (lambda data: data["budget"].__setitem__("max_steps", True), "agent-budget-invalid"),
        (lambda data: data["budget"].__setitem__("max_steps", -1), "agent-budget-invalid"),
        (lambda data: data["budget"].__setitem__("max_steps", "5"), "agent-budget-invalid"),
        (lambda data: data["budget"].__setitem__("extra", 1), "agent-budget-invalid"),
        (lambda data: data.__setitem__("metadata", []), "agent-metadata-invalid"),
    ],
)
async def test_malformed_creation_facts_fail_closed(mutate, code):
    store = InMemoryEventStore()
    data = agent_created_data(agent_id="a1", session_id="s1", request_id="r1", spec=spec())
    mutate(data)
    await raw_append(store, data)

    with pytest.raises(AgentDirectoryProtocolError) as error:
        await read_directory(store)
    assert error.value.code == code
    assert error.value.seq == 1
    # The message is fixed repository text, never the offending payload.
    assert str(error.value).startswith("an agent creation fact")
    assert "unlimited" not in str(error.value)


async def test_a_malformed_record_is_never_silently_dropped():
    """A directory that skipped the bad record would describe a set that never existed."""

    store = InMemoryEventStore()
    await AgentRegistrar(store).create_agent(SPEC, request_id="r1", agent_id="a1", session_id="s1")
    broken = agent_created_data(agent_id="a2", session_id="s2", request_id="r2", spec=spec())
    broken["budget"] = "unlimited"
    await raw_append(store, broken)
    await raw_append(
        store,
        agent_created_data(agent_id="a3", session_id="s3", request_id="r3", spec=spec()),
    )

    events = await store.read(AGENT_DIRECTORY_STREAM)
    with pytest.raises(AgentDirectoryProtocolError):
        AgentDirectory.rebuild(events)
    # The scan reports the one bad record rather than quietly returning the
    # two good ones around it.
    issues = validate_agent_directory_events(events)
    assert [(issue.code, issue.seq) for issue in issues] == [("agent-budget-invalid", 2)]


async def test_a_broken_directory_blocks_new_creations_instead_of_writing_into_it():
    """Fail closed on the write path too: appending onto history we cannot read
    would build a second Agent set on top of an unreadable one."""

    store = InMemoryEventStore()
    broken = agent_created_data(agent_id="a1", session_id="s1", request_id="r1", spec=spec())
    broken["budget"] = "unlimited"
    await raw_append(store, broken)

    with pytest.raises(AgentDirectoryProtocolError) as error:
        await AgentRegistrar(store).create_agent(SPEC, request_id="r2", agent_id="a2")
    assert error.value.code == "agent-budget-invalid"
    assert len(await store.read(AGENT_DIRECTORY_STREAM)) == 1


async def test_an_unknown_event_type_on_the_directory_stream_fails_closed():
    store = InMemoryEventStore()
    await raw_append(store, {"agent_id": "a1"}, event_type="agent/inbox-message")

    with pytest.raises(AgentDirectoryProtocolError) as error:
        await read_directory(store)
    assert error.value.code == "agent-event-type-unknown"


async def test_a_reused_request_id_in_history_fails_closed():
    store = InMemoryEventStore()
    await AgentRegistrar(store).create_agent(SPEC, request_id="r1", agent_id="a1", session_id="s1")
    await raw_append(
        store,
        agent_created_data(agent_id="a2", session_id="s2", request_id="r1", spec=spec()),
    )

    with pytest.raises(AgentDirectoryProtocolError) as error:
        await read_directory(store)
    assert error.value.code == "agent-request-duplicate"


async def test_an_empty_stream_is_an_empty_directory_not_an_error():
    directory = await read_directory(InMemoryEventStore())
    assert len(directory) == 0
    assert directory.records == ()
    assert directory.head_seq == 0
    assert directory.get("anything") is None


# 7. Concurrency: creation linearizes.


async def test_two_concurrent_creations_of_one_identity_produce_one_agent():
    # A store that really suspends, so the two tasks genuinely interleave.
    inner = InMemoryEventStore()
    store = YieldingStore(inner)
    registrar = AgentRegistrar(store)
    start = asyncio.Event()

    async def create(request_id: str):
        await start.wait()
        return await registrar.create_agent(
            SPEC, request_id=request_id, agent_id="a1", session_id="s1"
        )

    first = asyncio.create_task(create("r1"))
    second = asyncio.create_task(create("r2"))
    start.set()
    results = await asyncio.gather(first, second, return_exceptions=True)

    winners = [item for item in results if isinstance(item, AgentRecord)]
    losers = [item for item in results if isinstance(item, AgentIdentityConflictError)]
    assert len(winners) == 1
    # The loser is turned away by the identity it asked for, not by a lost
    # compare-and-swap: creation serialized rather than racing to the append.
    assert len(losers) == 1
    assert len(await inner.read(AGENT_DIRECTORY_STREAM)) == 1
    assert len(await read_directory(inner)) == 1


async def test_two_concurrent_creations_contending_for_one_session_id():
    inner = InMemoryEventStore()
    store = YieldingStore(inner)
    registrar = AgentRegistrar(store)
    start = asyncio.Event()

    async def create(agent_id: str, request_id: str):
        await start.wait()
        return await registrar.create_agent(
            SPEC, request_id=request_id, agent_id=agent_id, session_id="shared-session"
        )

    tasks = [
        asyncio.create_task(create("a1", "r1")),
        asyncio.create_task(create("a2", "r2")),
    ]
    start.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert len([item for item in results if isinstance(item, AgentRecord)]) == 1
    assert len([item for item in results if isinstance(item, AgentSessionConflictError)]) == 1
    directory = await read_directory(inner)
    assert len(directory) == 1
    assert directory.for_session("shared-session") is not None


async def test_concurrent_creations_of_distinct_identities_all_commit():
    inner = InMemoryEventStore()
    store = YieldingStore(inner)
    registrar = AgentRegistrar(store)
    start = asyncio.Event()

    async def create(index: int):
        await start.wait()
        return await registrar.create_agent(
            SPEC, request_id=f"r{index}", agent_id=f"a{index}", session_id=f"s{index}"
        )

    tasks = [asyncio.create_task(create(index)) for index in range(8)]
    start.set()
    records = await asyncio.gather(*tasks)

    directory = await read_directory(inner)
    assert len(directory) == 8
    assert {item.agent_id for item in records} == {f"a{index}" for index in range(8)}
    # Eight interleaved creations, eight dense sequence numbers and no lost
    # compare-and-swap: they linearized instead of racing.
    assert sorted(item.created_seq for item in records) == list(range(1, 9))


async def test_a_creation_built_on_a_stale_read_is_rejected_and_writes_nothing():
    """Another writer advanced the stream after our read of it.

    The in-process lock cannot see that writer, so the append must carry the
    sequence the *directory read* returned. Re-reading the head at append time
    instead would silently accept a decision made against history that no
    longer exists - the conflict checks above would have been run against the
    wrong Agent set.
    """

    inner = InMemoryEventStore()
    gated = GatedStore(inner)
    gated.stale_reads = True
    gated.append_release.set()
    registrar = AgentRegistrar(gated)

    creation = asyncio.create_task(
        registrar.create_agent(SPEC, request_id="r1", agent_id="a1", session_id="s1")
    )
    await gated.read_entered.wait()
    # A different process commits while our view of the directory is frozen.
    await raw_append(
        inner,
        agent_created_data(
            agent_id="other", session_id="s-other", request_id="r-other", spec=spec()
        ),
    )
    gated.read_release.set()

    with pytest.raises(AgentDirectoryConflictError) as error:
        await creation
    assert error.value.code == "agent-directory-changed"

    directory = await read_directory(inner)
    assert [item.agent_id for item in directory] == ["other"]
    # Retrying the same request id is safe and now succeeds against fresh history.
    record = await AgentRegistrar(inner).create_agent(
        SPEC, request_id="r1", agent_id="a1", session_id="s1"
    )
    assert record.agent_id == "a1"
    assert len(await read_directory(inner)) == 2


# 8-9. Cancellation, convergence and the may-have-committed boundary.


async def test_cancelling_before_the_append_writes_nothing(loop_reports):
    inner = InMemoryEventStore()
    gated = GatedStore(inner)
    gated.gate_reads = True
    registrar = AgentRegistrar(gated)

    creation = asyncio.create_task(registrar.create_agent(SPEC, request_id="r1", agent_id="a1"))
    await gated.read_entered.wait()
    creation.cancel()
    gated.read_release.set()

    with pytest.raises(asyncio.CancelledError):
        await creation

    assert gated.append_calls == 0
    assert await inner.read(AGENT_DIRECTORY_STREAM) == ()
    await settle()
    assert never_retrieved(loop_reports) == []


async def test_cancellation_inside_a_committed_append_is_reported_as_cancellation(loop_reports):
    """The store's commit-point boundary, honestly surfaced.

    The event is durable and the caller is still told it was cancelled: a
    cancellation is never converted into a quiet success. The identity is
    recoverable by ``request_id``, which is what makes that honest rather than
    merely strict.
    """

    inner = InMemoryEventStore()
    gated = GatedStore(inner, commit_first=True)
    registrar = AgentRegistrar(gated)

    creation = asyncio.create_task(
        registrar.create_agent(SPEC, request_id="r1", agent_id="a1", session_id="s1")
    )
    await gated.append_entered.wait()
    creation.cancel()
    gated.append_failure = asyncio.CancelledError()
    gated.append_release.set()

    with pytest.raises(asyncio.CancelledError):
        await creation

    # The append did commit, and the caller can reconcile it by request id.
    directory = await AgentRegistrar(inner).directory()
    recovered = directory.for_request("r1")
    assert recovered is not None
    assert recovered.agent_id == "a1"
    assert len(directory) == 1
    await settle()
    assert never_retrieved(loop_reports) == []


async def test_a_retry_after_a_may_have_committed_cancellation_returns_one_agent():
    inner = InMemoryEventStore()
    gated = GatedStore(inner, commit_first=True)

    creation = asyncio.create_task(
        AgentRegistrar(gated).create_agent(
            SPEC, request_id="stable-request", agent_id="a1", session_id="s1"
        )
    )
    await gated.append_entered.wait()
    creation.cancel()
    gated.append_failure = asyncio.CancelledError()
    gated.append_release.set()
    with pytest.raises(asyncio.CancelledError):
        await creation

    retried = await AgentRegistrar(inner).create_agent(
        SPEC, request_id="stable-request", agent_id="a1", session_id="s1"
    )

    assert retried.agent_id == "a1"
    assert len(await inner.read(AGENT_DIRECTORY_STREAM)) == 1
    assert len(await read_directory(inner)) == 1


async def test_repeated_cancellation_cannot_release_the_caller_early(loop_reports):
    """Cancelling again is a statement of intent, not an escape hatch."""

    inner = InMemoryEventStore()
    gated = GatedStore(inner, commit_first=True)
    registrar = AgentRegistrar(gated)

    creation = asyncio.create_task(
        registrar.create_agent(SPEC, request_id="r1", agent_id="a1", session_id="s1")
    )
    await gated.append_entered.wait()

    # Hold the reconciliation read open, then cancel three more times.
    gated.gate_reads = True
    creation.cancel()
    gated.append_failure = asyncio.CancelledError()
    gated.append_release.set()
    await gated.read_entered.wait()

    for _ in range(3):
        creation.cancel()
        for _ in range(3):
            await asyncio.sleep(0)
        assert not creation.done(), "repeated cancellation released the caller early"

    gated.read_release.set()
    with pytest.raises(asyncio.CancelledError):
        await creation

    await settle()
    assert never_retrieved(loop_reports) == []


async def test_a_failed_append_reports_whether_it_committed(loop_reports):
    inner = InMemoryEventStore()
    gated = GatedStore(inner)
    gated.append_failure = RuntimeError("store exploded")
    gated.append_release.set()

    with pytest.raises(AgentCreationError) as error:
        await AgentRegistrar(gated).create_agent(SPEC, request_id="r1", agent_id="a1")
    assert error.value.committed is False
    assert error.value.code == "agent-creation-failed"
    assert await inner.read(AGENT_DIRECTORY_STREAM) == ()

    committed = GatedStore(inner, commit_first=True)
    committed.append_failure = RuntimeError("store exploded after writing")
    committed.append_release.set()
    with pytest.raises(AgentCreationError) as error:
        await AgentRegistrar(committed).create_agent(SPEC, request_id="r2", agent_id="a2")
    assert error.value.committed is True
    assert "recorded but the call failed" in str(error.value)
    await settle()
    assert never_retrieved(loop_reports) == []


async def test_a_concurrency_conflict_that_did_commit_is_not_reported_as_a_lost_cas():
    """`AgentDirectoryConflictError` promises nothing was written."""

    inner = InMemoryEventStore()
    gated = GatedStore(inner, commit_first=True)
    gated.append_failure = ConcurrencyConflict("late conflict")
    gated.append_release.set()

    with pytest.raises(AgentCreationError) as error:
        await AgentRegistrar(gated).create_agent(SPEC, request_id="r1", agent_id="a1")
    assert error.value.committed is True


# 10. No duplicate facts, no stray tasks, no unretrieved exceptions.


async def test_a_repeated_request_id_returns_the_same_agent_without_a_second_append():
    store = InMemoryEventStore()
    registrar = AgentRegistrar(store)
    first = await registrar.create_agent(SPEC, request_id="r1", agent_id="a1", session_id="s1")
    second = await registrar.create_agent(SPEC, request_id="r1", agent_id="a1", session_id="s1")
    third = await AgentRegistrar(store).create_agent(SPEC, request_id="r1")

    assert first == second == third
    assert len(await store.read(AGENT_DIRECTORY_STREAM)) == 1


async def test_a_request_id_reused_for_a_different_identity_is_rejected():
    store = InMemoryEventStore()
    registrar = AgentRegistrar(store)
    await registrar.create_agent(SPEC, request_id="r1", agent_id="a1", session_id="s1")

    with pytest.raises(AgentRequestConflictError):
        await registrar.create_agent(SPEC, request_id="r1", agent_id="a2", session_id="s2")
    with pytest.raises(AgentRequestConflictError):
        await registrar.create_agent(SPEC, request_id="r1", agent_id="a1", session_id="s2")
    with pytest.raises(AgentRequestConflictError):
        await registrar.create_agent(
            spec(preset="reviewer"), request_id="r1", agent_id="a1", session_id="s1"
        )
    assert len(await store.read(AGENT_DIRECTORY_STREAM)) == 1


async def test_creation_leaves_no_task_behind(loop_reports):
    store = InMemoryEventStore()
    registrar = AgentRegistrar(store)
    before = {task for task in asyncio.all_tasks() if task is not asyncio.current_task()}

    await registrar.create_agent(SPEC, request_id="r1", agent_id="a1")
    with pytest.raises(AgentIdentityConflictError):
        await registrar.create_agent(SPEC, request_id="r2", agent_id="a1")

    await settle()
    after = {task for task in asyncio.all_tasks() if task is not asyncio.current_task()}
    assert after == before
    assert never_retrieved(loop_reports) == []


async def test_a_cancelled_creation_leaves_no_reconciliation_task_behind(loop_reports):
    inner = InMemoryEventStore()
    gated = GatedStore(inner, commit_first=True)
    before = {task for task in asyncio.all_tasks() if task is not asyncio.current_task()}

    creation = asyncio.create_task(
        AgentRegistrar(gated).create_agent(SPEC, request_id="r1", agent_id="a1")
    )
    await gated.append_entered.wait()
    creation.cancel()
    gated.append_failure = asyncio.CancelledError()
    gated.append_release.set()
    with pytest.raises(asyncio.CancelledError):
        await creation

    await settle()
    after = {task for task in asyncio.all_tasks() if task is not asyncio.current_task()}
    assert after == before
    assert never_retrieved(loop_reports) == []


async def test_the_returned_record_equals_the_replayed_record_exactly():
    """One reader, so an in-memory answer cannot be more forgiving than replay."""

    store = InMemoryEventStore()
    record = await AgentRegistrar(store).create_agent(
        spec(
            owner_agent_id=None,
            forked_from_session_id="origin",
            capability_grants=("read", "write"),
            budget=Budget(max_tokens=11, max_steps=2, max_tool_calls=3, max_wall_seconds=4.5,
                          max_children=1, max_depth=1, max_processes=1),
            metadata={"note": "example", "nested": {"k": [1, 2]}},
        ),
        request_id="r1",
        agent_id="a1",
        session_id="s1",
    )

    replayed = (await read_directory(store)).get("a1")
    assert replayed == record
    assert replayed.budget.max_wall_seconds == 4.5
    assert replayed.metadata == {"note": "example", "nested": {"k": [1, 2]}}


async def test_editing_a_returned_record_payload_cannot_rewrite_history():
    store = InMemoryEventStore()
    record = await AgentRegistrar(store).create_agent(
        spec(metadata={"nested": {"value": "original"}}),
        request_id="r1",
        agent_id="a1",
    )
    record.metadata["nested"]["value"] = "tampered"

    replayed = (await read_directory(store)).get("a1")
    assert replayed.metadata == {"nested": {"value": "original"}}


# 11. Review findings: the writer must not be able to append a fact replay
# rejects, an unknown outcome must not be reported as a known one, interpreter
# signals must survive, and a shared projector must not hand out writable state.


class FailingReadStore:
    """Commits the append, then makes every later read fail.

    This is the shape that made the old code lie: the event is durable, the
    append still raises, and the reconciling read cannot answer.
    """

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
        if self.reads_fail:
            raise OSError("reconciliation read failed")
        return await self.inner.read(stream_id, from_seq=from_seq)

    async def head(self, stream_id):
        return await self.inner.head(stream_id)

    async def list_streams(self, *, prefix=None):
        return await self.inner.list_streams(prefix=prefix)


@pytest.mark.parametrize(
    "budget",
    [
        Budget(max_steps=True),
        Budget(max_tokens=-1),
        Budget(max_processes=False),
        Budget(max_wall_seconds=-0.5),
        Budget(max_wall_seconds=float("nan")),
        Budget(max_wall_seconds=float("inf")),
        Budget(max_wall_seconds=float("-inf")),
        Budget(max_wall_seconds="abc"),
        Budget(max_wall_seconds=None),
        Budget(max_steps="5"),
    ],
)
async def test_a_budget_replay_would_reject_is_never_appended(budget):
    """The writer must not be able to commit a fact the reader refuses.

    A rule applied more loosely on the write path is not a weaker check: it is
    a way to append a record that can never be read back. One such event used
    to brick the whole directory for that store - every later rebuild *and*
    every later creation failed forever.
    """

    store = InMemoryEventStore()

    with pytest.raises(AgentIdentityError) as error:
        await AgentRegistrar(store).create_agent(spec(budget=budget), request_id="r1")

    assert error.value.code == "agent-budget-invalid"
    assert error.value.field == "budget"
    assert await store.read(AGENT_DIRECTORY_STREAM) == ()
    # The directory is still usable, and a good creation still succeeds.
    assert len(await read_directory(store)) == 0
    assert (await AgentRegistrar(store).create_agent(SPEC, request_id="r2")).request_id == "r2"


async def test_every_accepted_budget_survives_a_round_trip():
    """Write-side acceptance and read-side acceptance are the same rule."""

    store = InMemoryEventStore()
    budget = Budget(
        max_tokens=0,
        max_steps=7,
        max_tool_calls=1,
        max_wall_seconds=0.0,
        max_children=2,
        max_depth=3,
        max_processes=4,
    )
    record = await AgentRegistrar(store).create_agent(
        spec(budget=budget), request_id="r1", agent_id="a1"
    )

    assert record.budget == budget
    assert (await read_directory(store)).get("a1").budget == budget


async def test_a_failed_reconciliation_read_reports_unknown_not_uncommitted():
    """Could-not-find-out must never be reported as it-is-not-there."""

    inner = InMemoryEventStore()
    store = FailingReadStore(inner, RuntimeError("append blew up after committing"))

    with pytest.raises(AgentCreationError) as error:
        await AgentRegistrar(store).create_agent(SPEC, request_id="r1", agent_id="a1")

    assert error.value.committed is None
    assert "unknown" in str(error.value)
    # The claim matters because the event really is durable underneath.
    assert len(await inner.read(AGENT_DIRECTORY_STREAM)) == 1
    assert (await read_directory(inner)).for_request("r1").agent_id == "a1"


async def test_an_unknown_outcome_is_not_reported_as_a_lost_compare_and_swap():
    """AgentDirectoryConflictError promises nothing was written, so it needs proof."""

    inner = InMemoryEventStore()
    store = FailingReadStore(inner, ConcurrencyConflict("conflict after commit"))

    with pytest.raises(AgentCreationError) as error:
        await AgentRegistrar(store).create_agent(SPEC, request_id="r1")

    assert not isinstance(error.value, AgentDirectoryConflictError)
    assert error.value.committed is None


@pytest.mark.parametrize("interrupt", [SystemExit(3), KeyboardInterrupt()])
async def test_interpreter_signals_are_not_rewritten_into_creation_failures(interrupt):
    """Only CancelledError needs convergence; the rest must stay themselves.

    Turning a `SystemExit` into an `AgentCreationError` would make a shutdown
    look like a storage problem and swallow the interrupt entirely.
    """

    store = FailingReadStore(InMemoryEventStore(), interrupt)

    with pytest.raises(type(interrupt)) as error:
        await AgentRegistrar(store).create_agent(SPEC, request_id="r1")

    assert error.value is interrupt


async def test_an_unsupported_schema_version_fails_closed():
    store = InMemoryEventStore()
    await store.append(
        AGENT_DIRECTORY_STREAM,
        expected_seq=0,
        events=(
            PendingEvent(
                type=AGENT_CREATED,
                data=agent_created_data(
                    agent_id="a1", session_id="s1", request_id="r1", spec=spec()
                ),
                schema_version=999,
            ),
        ),
    )

    with pytest.raises(AgentDirectoryProtocolError) as error:
        await read_directory(store)
    assert error.value.code == "agent-schema-version-unsupported"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data.__setitem__("future_authority", "yes"),
        lambda data: data.__setitem__("inbox", []),
        lambda data: data.pop("metadata"),
    ],
)
async def test_a_payload_whose_keys_are_not_exactly_the_protocol_fails_closed(mutate):
    """An extra key means the writer knows something this reader does not.

    Reading the remaining fields as a complete v1 identity would silently drop
    whatever the newer writer added - including, one day, a field that changes
    what the others mean.
    """

    store = InMemoryEventStore()
    data = agent_created_data(agent_id="a1", session_id="s1", request_id="r1", spec=spec())
    mutate(data)
    await raw_append(store, data)

    with pytest.raises(AgentDirectoryProtocolError) as error:
        await read_directory(store)
    assert error.value.code == "agent-payload-keys-unexpected"


async def test_an_identity_fact_on_a_session_stream_is_not_an_identity_fact():
    """Otherwise one Agent's execution history could assert who exists."""

    store = InMemoryEventStore()
    await store.append(
        "session:s1",
        expected_seq=0,
        events=(
            PendingEvent(
                type=AGENT_CREATED,
                data=agent_created_data(
                    agent_id="a1", session_id="s1", request_id="r1", spec=spec()
                ),
            ),
        ),
    )

    with pytest.raises(AgentDirectoryProtocolError) as error:
        AgentDirectory.rebuild(await store.read("session:s1"))
    assert error.value.code == "agent-stream-unexpected"
    # The real directory stream is untouched and still empty.
    assert len(await read_directory(store)) == 0


async def test_writing_through_a_returned_record_cannot_change_the_directory():
    """A shared projector must not acquire a mutable second version of the truth.

    `AgentRecord` is frozen, but ``metadata`` is an ordinary nested graph, so
    the freeze alone is not enough: returning the retained object would let one
    caller change what every later query on that directory answers.
    """

    store = InMemoryEventStore()
    await AgentRegistrar(store).create_agent(
        spec(metadata={"nested": {"value": "original"}, "items": [{"k": 1}]}),
        request_id="r1",
        agent_id="a1",
        session_id="s1",
    )
    directory = await read_directory(store)

    directory.get("a1").metadata["nested"]["value"] = "tampered"
    directory.for_session("s1").metadata["items"][0]["k"] = 99
    directory.for_request("r1").metadata["added"] = True
    directory.records[0].metadata.clear()
    for record in directory:
        record.metadata["loop"] = "written"

    expected = {"nested": {"value": "original"}, "items": [{"k": 1}]}
    assert directory.get("a1").metadata == expected
    assert directory.for_session("s1").metadata == expected
    assert directory.for_request("r1").metadata == expected
    assert directory.records[0].metadata == expected
    assert (await read_directory(store)).get("a1").metadata == expected


async def test_child_records_are_detached_too():
    store = InMemoryEventStore()
    registrar = AgentRegistrar(store)
    await registrar.create_agent(SPEC, request_id="r1", agent_id="parent", session_id="s1")
    await registrar.create_agent(
        spec(owner_agent_id="parent", metadata={"n": {"v": "original"}}),
        request_id="r2",
        agent_id="child",
        session_id="s2",
    )
    directory = await read_directory(store)

    directory.children_of("parent")[0].metadata["n"]["v"] = "tampered"

    assert directory.children_of("parent")[0].metadata == {"n": {"v": "original"}}


# 12. Review findings, round two: the ownership boundary has two entrances.
# Copying on the way out does not help if the projector already kept a
# reference on the way in, and validating before the first ``await`` is not the
# same as validating before the append.


class SuspendingReadStore:
    """Suspends inside the first directory read and runs a callback there.

    That is the only window the caller has: `create_agent` validates, then
    awaits the directory read. Anything it still reads from the caller after
    this point can be changed underneath it.
    """

    def __init__(self, inner: EventStore, during_first_read) -> None:
        self.inner = inner
        self.during_first_read = during_first_read
        self.read_count = 0

    async def append(self, stream_id, *, expected_seq, events, durability=Durability.SYNC):
        return await self.inner.append(
            stream_id, expected_seq=expected_seq, events=events, durability=durability
        )

    async def read(self, stream_id, *, from_seq=1):
        self.read_count += 1
        if self.read_count == 1:
            await asyncio.sleep(0)
            self.during_first_read()
        return await self.inner.read(stream_id, from_seq=from_seq)

    async def head(self, stream_id):
        return await self.inner.head(stream_id)

    async def list_streams(self, *, prefix=None):
        return await self.inner.list_streams(prefix=prefix)


async def test_the_directory_does_not_share_the_input_events_payload():
    """Detaching on the way out cannot repair a reference kept on the way in.

    ``rebuild()`` is handed envelopes the caller still holds. If parsing keeps
    ``event.data["metadata"]`` itself, the retained record is already poisoned,
    and every later query answers with whatever the caller did to those events.
    """

    store = InMemoryEventStore()
    await AgentRegistrar(store).create_agent(
        spec(metadata={"nested": {"value": "original"}, "items": [{"k": 1}]}),
        request_id="r1",
        agent_id="a1",
    )
    events = await store.read(AGENT_DIRECTORY_STREAM)
    directory = AgentDirectory.rebuild(events)

    events[0].data["metadata"]["nested"]["value"] = "changed"
    events[0].data["metadata"]["items"][0]["k"] = 99
    events[0].data["metadata"]["added"] = True

    assert directory.get("a1").metadata == {"nested": {"value": "original"}, "items": [{"k": 1}]}


async def test_a_record_returned_by_the_registrar_does_not_share_the_appended_event():
    store = InMemoryEventStore()
    record = await AgentRegistrar(store).create_agent(
        spec(metadata={"nested": {"value": "original"}}), request_id="r1", agent_id="a1"
    )

    record.metadata["nested"]["value"] = "tampered"

    assert (await read_directory(store)).get("a1").metadata == {"nested": {"value": "original"}}


async def test_metadata_mutated_during_the_first_await_does_not_reach_the_log():
    """The request is frozen before the transaction suspends, or it is not frozen.

    `AgentSpec` is frozen, but ``metadata`` is an ordinary nested graph, so a
    caller can keep editing it while `create_agent` is suspended in the
    directory read. A shallow copy taken later persists the edit.
    """

    live = {"nested": {"value": "original"}, "items": [1]}
    inner = InMemoryEventStore()

    def mutate() -> None:
        live["nested"]["value"] = "mutated-during-first-await"
        live["items"].append(2)
        live["added"] = True

    store = SuspendingReadStore(inner, mutate)
    record = await AgentRegistrar(store).create_agent(
        spec(metadata=live), request_id="r1", agent_id="a1"
    )

    expected = {"nested": {"value": "original"}, "items": [1]}
    assert store.read_count >= 1
    assert live["nested"]["value"] == "mutated-during-first-await"
    assert (await inner.read(AGENT_DIRECTORY_STREAM))[0].data["metadata"] == expected
    assert record.metadata == expected
    assert (await read_directory(inner)).get("a1").metadata == expected


async def test_a_conflict_check_cannot_be_decided_by_a_mutated_request():
    """The owner check must read the frozen snapshot, not the live spec."""

    live_metadata = {"note": "original"}
    inner = InMemoryEventStore()
    registrar = AgentRegistrar(inner)
    await registrar.create_agent(SPEC, request_id="r0", agent_id="owner", session_id="s0")

    store = SuspendingReadStore(inner, lambda: live_metadata.__setitem__("note", "changed"))
    record = await AgentRegistrar(store).create_agent(
        spec(owner_agent_id="owner", metadata=live_metadata),
        request_id="r1",
        agent_id="child",
        session_id="s1",
    )

    assert record.owner_agent_id == "owner"
    assert record.metadata == {"note": "original"}


@pytest.mark.parametrize(
    "metadata",
    [
        {"unsupported": {1, 2, 3}},
        {"nested": {"deep": {"worse": {4, 5}}}},
        {"in_a_list": [1, {"bad": {6}}]},
        {"raw_bytes": b"abc"},
        {"object": object()},
        {1: "non-string key"},
        "not a mapping",
    ],
)
async def test_metadata_the_store_cannot_encode_is_rejected_before_the_append(metadata):
    """Otherwise a caller mistake surfaces as a storage failure mid-transaction."""

    store = InMemoryEventStore()

    with pytest.raises(AgentIdentityError) as error:
        await AgentRegistrar(store).create_agent(spec(metadata=metadata), request_id="r1")

    assert error.value.code == "agent-metadata-invalid"
    assert error.value.field == "metadata"
    assert await store.read(AGENT_DIRECTORY_STREAM) == ()


@pytest.mark.parametrize("field", ["max_wall_seconds", "max_steps", "max_tokens"])
async def test_an_enormous_budget_number_stays_inside_the_error_protocol(field):
    """``10**10000`` is an ``int``, is not negative, and used to raise a bare
    ``OverflowError`` out of ``float()``/``math.isfinite()`` - escaping the
    fixed ``agent-budget-invalid`` outcome on both paths at once."""

    huge = 10**10000
    store = InMemoryEventStore()

    with pytest.raises(AgentIdentityError) as error:
        await AgentRegistrar(store).create_agent(
            spec(budget=Budget(**{field: huge})), request_id="r1"
        )
    assert error.value.code == "agent-budget-invalid"
    assert await store.read(AGENT_DIRECTORY_STREAM) == ()

    # And the same value already persisted by some other writer replays as a
    # stable protocol error, not an OverflowError.
    data = agent_created_data(agent_id="a1", session_id="s1", request_id="r1", spec=spec())
    data["budget"][field] = huge
    await raw_append(store, data)
    with pytest.raises(AgentDirectoryProtocolError) as replay_error:
        await read_directory(store)
    assert replay_error.value.code == "agent-budget-invalid"


async def test_budget_values_are_bounded_at_the_json_safe_integer():
    store = InMemoryEventStore()
    at_limit = Budget(max_steps=MAX_BUDGET_VALUE, max_wall_seconds=float(2**52))
    record = await AgentRegistrar(store).create_agent(
        spec(budget=at_limit), request_id="r1", agent_id="a1"
    )
    assert record.budget.max_steps == MAX_BUDGET_VALUE
    assert (await read_directory(store)).get("a1").budget == record.budget

    with pytest.raises(AgentIdentityError):
        await AgentRegistrar(InMemoryEventStore()).create_agent(
            spec(budget=Budget(max_steps=MAX_BUDGET_VALUE + 1)), request_id="r2"
        )


async def test_an_unpinned_retry_still_returns_the_same_agent():
    """The frozen-payload comparison must not reject a legitimate retry.

    A retry that did not pin ``agent_id``/``session_id`` gets fresh generated
    ids; those are expected to differ and are not part of what identifies the
    request.
    """

    store = InMemoryEventStore()
    first = await AgentRegistrar(store).create_agent(
        spec(capability_grants=("read",)), request_id="r1", agent_id="a1", session_id="s1"
    )
    retry = await AgentRegistrar(store).create_agent(
        spec(capability_grants=("read",)), request_id="r1"
    )

    assert retry == first
    assert len(await store.read(AGENT_DIRECTORY_STREAM)) == 1

    with pytest.raises(AgentRequestConflictError):
        await AgentRegistrar(store).create_agent(
            spec(capability_grants=("write",)), request_id="r1"
        )


# 13. Review finding, round three: a metadata graph that cannot be walked.
#
# ``to_json_value()`` recurses, so a self-referential or very deep graph used to
# raise a bare ``RecursionError`` - out of both entrances - at whatever depth
# this interpreter happened to run out of stack. The bound below makes the
# rejection a property of the protocol instead of a property of
# ``sys.getrecursionlimit()``.


def cyclic_mapping() -> dict:
    graph: dict = {"self": None}
    graph["self"] = graph
    return graph


def cyclic_sequence() -> dict:
    items: list = [1]
    items.append(items)
    return {"items": items}


def nested(depth: int) -> dict:
    root: dict = {}
    current = root
    for _ in range(depth):
        current["n"] = {}
        current = current["n"]
    return root


def envelope_with_metadata(metadata: object, *, seq: int = 1) -> EventEnvelope:
    """Build an envelope directly, bypassing `EventEnvelope.materialize`.

    ``materialize()`` would encode the payload and hit the same recursion, so a
    cyclic graph cannot be produced through the normal write path at all. The
    projector still has to be robust against whatever it is handed.
    """

    data = agent_created_data(agent_id="a1", session_id="s1", request_id="r1", spec=spec())
    data["metadata"] = metadata
    return EventEnvelope(
        event_id=uuid4(),
        stream_id=AGENT_DIRECTORY_STREAM,
        seq=seq,
        type=AGENT_CREATED,
        schema_version=1,
        data=data,
        occurred_at=datetime.now(UTC),
    )


@pytest.mark.parametrize(
    "metadata",
    [
        cyclic_mapping(),
        cyclic_sequence(),
        {"buried": {"deeper": cyclic_mapping()}},
        nested(MAX_METADATA_DEPTH + 1),
        nested(60_000),
    ],
)
async def test_an_unwalkable_metadata_graph_is_rejected_before_the_append(metadata):
    store = InMemoryEventStore()

    with pytest.raises(AgentIdentityError) as error:
        await AgentRegistrar(store).create_agent(spec(metadata=metadata), request_id="r1")

    assert error.value.code == "agent-metadata-invalid"
    assert error.value.field == "metadata"
    assert await store.read(AGENT_DIRECTORY_STREAM) == ()


@pytest.mark.parametrize(
    "metadata",
    [cyclic_mapping(), cyclic_sequence(), nested(MAX_METADATA_DEPTH + 1), nested(300)],
)
async def test_an_unwalkable_metadata_graph_fails_closed_on_replay(metadata):
    with pytest.raises(AgentDirectoryProtocolError) as error:
        AgentDirectory.rebuild((envelope_with_metadata(metadata),))
    assert error.value.code == "agent-metadata-invalid"

    issues = validate_agent_directory_events((envelope_with_metadata(metadata),))
    assert [issue.code for issue in issues] == ["agent-metadata-invalid"]


async def test_a_deep_but_walkable_metadata_graph_still_round_trips():
    """The bound must reject the unbounded case without rejecting ordinary data."""

    store = InMemoryEventStore()
    metadata = nested(MAX_METADATA_DEPTH - 4)
    record = await AgentRegistrar(store).create_agent(
        spec(metadata=metadata), request_id="r1", agent_id="a1"
    )

    assert record.metadata == metadata
    assert (await read_directory(store)).get("a1").metadata == metadata


@pytest.mark.parametrize(
    "metadata",
    [
        {"unsupported": {1, 2, 3}},
        cyclic_mapping(),
        nested(MAX_METADATA_DEPTH + 1),
        {1: "non-string key"},
        "not a mapping",
        None,
    ],
)
def test_the_public_payload_helper_refuses_metadata_instead_of_emptying_it(metadata):
    """``or {}`` conflated "rejected" with "legitimately empty".

    `agent_created_data` is exported, so a caller reaching it directly with
    metadata this protocol cannot carry previously got a payload with the
    metadata silently dropped rather than an error.
    """

    with pytest.raises(AgentIdentityError) as error:
        agent_created_data(
            agent_id="a1", session_id="s1", request_id="r1", spec=spec(metadata=metadata)
        )
    assert error.value.code == "agent-metadata-invalid"
    assert error.value.field == "metadata"


def test_the_public_payload_helper_still_accepts_an_empty_mapping():
    data = agent_created_data(
        agent_id="a1", session_id="s1", request_id="r1", spec=spec(metadata={})
    )
    assert data["metadata"] == {}
    assert set(data) == AGENT_CREATED_KEYS


# 14. Review finding, round four: merely *looking* at metadata can fail.
#
# Metadata is caller-supplied, so traversal itself is untrusted code. A ``dict``
# subclass whose ``values()`` or ``__iter__`` raises breaks the bounded walk
# while remaining perfectly encodable - so a pre-check performed outside the
# normalization boundary leaked a bare exception and bypassed the one stable
# outcome, on both entrances.


class RaisingValues(dict):
    """Encodable, but not walkable.

    ``to_json_value()`` reads a mapping through ``items()``, while the bounded
    walk reads it through ``values()``. Overriding only the latter separates
    "can be encoded" from "can be traversed", which is exactly the gap.
    """

    def values(self):
        raise ValueError("values() is hostile")


class RaisingItems(dict):
    def items(self):
        raise RuntimeError("items() is hostile")


class RaisingIteration(dict):
    def __iter__(self):
        raise LookupError("__iter__ is hostile")


class InterruptingValues(dict):
    def values(self):
        raise KeyboardInterrupt()


class ExitingValues(dict):
    def values(self):
        raise SystemExit(7)


# Which access path each one breaks is not interchangeable, and pretending
# otherwise would make the tests assert something untrue. ``values()`` is read
# by the bounded walk and ``items()`` by the encoder, so those two are hostile
# wherever they sit. ``__iter__`` is only read by the top-level key scan, so it
# is hostile only in that position - a nested one is harmless and is asserted
# as such by the ordinary-subclass test below.
NESTED_HOSTILE = [
    RaisingValues({"k": "v"}),
    RaisingItems({"k": "v"}),
]
HOSTILE_CONTAINERS = [*NESTED_HOSTILE, RaisingIteration({"k": "v"})]


@pytest.mark.parametrize("metadata", HOSTILE_CONTAINERS)
async def test_a_container_that_raises_while_being_walked_is_a_metadata_error(metadata):
    store = InMemoryEventStore()

    with pytest.raises(AgentIdentityError) as error:
        await AgentRegistrar(store).create_agent(spec(metadata=metadata), request_id="r1")

    assert error.value.code == "agent-metadata-invalid"
    assert error.value.field == "metadata"
    assert await store.read(AGENT_DIRECTORY_STREAM) == ()


@pytest.mark.parametrize("hostile", NESTED_HOSTILE)
async def test_a_nested_container_that_raises_while_being_walked_is_also_caught(hostile):
    store = InMemoryEventStore()

    with pytest.raises(AgentIdentityError) as error:
        await AgentRegistrar(store).create_agent(
            spec(metadata={"outer": [{"inner": hostile}]}), request_id="r1"
        )

    assert error.value.code == "agent-metadata-invalid"
    assert await store.read(AGENT_DIRECTORY_STREAM) == ()


@pytest.mark.parametrize("metadata", HOSTILE_CONTAINERS)
async def test_a_container_that_raises_while_being_walked_fails_closed_on_replay(metadata):
    with pytest.raises(AgentDirectoryProtocolError) as error:
        AgentDirectory.rebuild((envelope_with_metadata(metadata),))
    assert error.value.code == "agent-metadata-invalid"

    issues = validate_agent_directory_events((envelope_with_metadata(metadata),))
    assert [issue.code for issue in issues] == ["agent-metadata-invalid"]


@pytest.mark.parametrize("metadata", HOSTILE_CONTAINERS)
def test_the_public_payload_helper_also_refuses_an_unwalkable_container(metadata):
    with pytest.raises(AgentIdentityError) as error:
        agent_created_data(
            agent_id="a1", session_id="s1", request_id="r1", spec=spec(metadata=metadata)
        )
    assert error.value.code == "agent-metadata-invalid"


@pytest.mark.parametrize(
    ("container", "interrupt"),
    [(InterruptingValues, KeyboardInterrupt), (ExitingValues, SystemExit)],
)
async def test_an_interrupt_raised_while_walking_metadata_is_not_a_metadata_error(
    container, interrupt
):
    """The fix must catch `Exception`, never `BaseException`.

    An interrupt raised while traversing is not a verdict about the metadata.
    Swallowing it into ``agent-metadata-invalid`` would report a shutdown as a
    caller mistake and lose the interrupt entirely - the same rule the creation
    transaction already follows around its append.
    """

    store = InMemoryEventStore()

    with pytest.raises(interrupt):
        await AgentRegistrar(store).create_agent(
            spec(metadata=container({"k": "v"})), request_id="r1"
        )
    assert await store.read(AGENT_DIRECTORY_STREAM) == ()


@pytest.mark.parametrize(
    ("container", "interrupt"),
    [(InterruptingValues, KeyboardInterrupt), (ExitingValues, SystemExit)],
)
def test_an_interrupt_raised_while_walking_metadata_survives_replay_and_the_helper(
    container, interrupt
):
    with pytest.raises(interrupt):
        AgentDirectory.rebuild((envelope_with_metadata(container({"k": "v"})),))

    with pytest.raises(interrupt):
        agent_created_data(
            agent_id="a1",
            session_id="s1",
            request_id="r1",
            spec=spec(metadata=container({"k": "v"})),
        )


async def test_an_ordinary_mapping_subclass_is_still_accepted():
    """The boundary rejects unwalkable containers, not every subclass."""

    class Ordinary(dict):
        pass

    store = InMemoryEventStore()
    record = await AgentRegistrar(store).create_agent(
        spec(metadata=Ordinary({"nested": Ordinary({"value": "kept"})})),
        request_id="r1",
        agent_id="a1",
    )

    assert record.metadata == {"nested": {"value": "kept"}}
    assert (await read_directory(store)).get("a1").metadata == {"nested": {"value": "kept"}}


# 15. Review findings, round five: reconciliation must recognise *our* event,
# and reading untrusted payload is itself untrusted work.


class LosingRaceStore:
    """Lets another writer commit under the same id, then fails our append."""

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
    ("agent_id", "session_id", "overrides"),
    [
        ("agent-b", "session-b", {}),
        ("agent-a", "session-b", {}),
        ("agent-b", "session-a", {}),
        ("agent-b", "session-b", {"preset": "reviewer"}),
        ("agent-b", "session-b", {"capability_grants": ("write",)}),
    ],
)
async def test_another_writers_agent_is_never_reported_as_ours(agent_id, session_id, overrides):
    """``committed`` answers "did *our* event land", not "is that id present".

    Two independent registrars racing on one ``request_id`` create different
    Agents. Matching on the id alone told the loser its Agent was recorded when
    what actually landed was somebody else's.
    """

    inner = InMemoryEventStore()

    async def other_writer():
        await AgentRegistrar(inner).create_agent(
            spec(**overrides), request_id="shared", agent_id=agent_id, session_id=session_id
        )

    store = LosingRaceStore(inner, other_writer)

    with pytest.raises(AgentCreationError) as error:
        await AgentRegistrar(store).create_agent(
            SPEC, request_id="shared", agent_id="agent-a", session_id="session-a"
        )

    assert error.value.committed is False
    durable = AgentDirectory.rebuild(await inner.read(AGENT_DIRECTORY_STREAM))
    assert durable.get(agent_id) is not None
    assert len(durable) == 1


async def test_our_own_committed_agent_is_still_reported_as_committed():
    """The stricter match must not turn every may-have-committed into False."""

    inner = InMemoryEventStore()

    with pytest.raises(AgentCreationError) as error:
        await AgentRegistrar(CommitThenFailStore(inner)).create_agent(
            spec(metadata={"note": "mine"}),
            request_id="r1",
            agent_id="a1",
            session_id="s1",
        )

    assert error.value.committed is True
    assert (await read_directory(inner)).get("a1") is not None


async def test_a_malformed_unrelated_event_does_not_make_the_answer_unknown():
    class CommitThenPoisonStore:
        def __init__(self, inner: EventStore) -> None:
            self.inner = inner
            self.poisoned = False

        async def append(self, stream_id, *, expected_seq, events, durability=Durability.SYNC):
            await self.inner.append(
                stream_id, expected_seq=expected_seq, events=events, durability=durability
            )
            if not self.poisoned:
                self.poisoned = True
                broken = agent_created_data(
                    agent_id="a2", session_id="s2", request_id="r2", spec=spec()
                )
                broken["budget"] = "unlimited"
                await raw_append(self.inner, broken)
            raise RuntimeError("failed after committing")

        async def read(self, stream_id, *, from_seq=1):
            return await self.inner.read(stream_id, from_seq=from_seq)

        async def head(self, stream_id):
            return await self.inner.head(stream_id)

        async def list_streams(self, *, prefix=None):
            return await self.inner.list_streams(prefix=prefix)

    inner = InMemoryEventStore()
    with pytest.raises(AgentCreationError) as error:
        await AgentRegistrar(CommitThenPoisonStore(inner)).create_agent(
            SPEC, request_id="r1", agent_id="a1", session_id="s1"
        )
    assert error.value.committed is True


class RaisingIteration(dict):
    """Encodable, but ``set(data)`` cannot walk it."""

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


# Only the access paths ``parse_agent_created`` actually uses.
HOSTILE_DIRECTORY_PAYLOADS = [RaisingIteration, RaisingGet, RaisingGetItem]


def poisoned_directory_event(container) -> EventEnvelope:
    data = agent_created_data(agent_id="a1", session_id="s1", request_id="r1", spec=spec())
    return EventEnvelope(
        event_id=uuid4(),
        stream_id=AGENT_DIRECTORY_STREAM,
        seq=1,
        type=AGENT_CREATED,
        schema_version=1,
        data=container(data),
        occurred_at=datetime.now(UTC),
    )


@pytest.mark.parametrize("container", HOSTILE_DIRECTORY_PAYLOADS)
def test_a_directory_payload_that_raises_while_being_read_is_a_protocol_error(container):
    """`parse_agent_created` calls ``set(data)``, ``data.get()`` and
    ``data[key]`` on a container the store handed back, all of which a hostile
    subclass can break - outside the protocol error boundary that used to end at
    the metadata walk."""

    with pytest.raises(AgentDirectoryProtocolError) as error:
        AgentDirectory.rebuild((poisoned_directory_event(container),))
    assert error.value.code == "agent-payload-invalid"


@pytest.mark.parametrize("container", HOSTILE_DIRECTORY_PAYLOADS)
def test_the_directory_validator_still_returns_issues_for_a_hostile_payload(container):
    issues = validate_agent_directory_events((poisoned_directory_event(container),))
    assert [issue.code for issue in issues] == ["agent-payload-invalid"]


@pytest.mark.parametrize(
    ("container", "interrupt"),
    [(InterruptingIteration, KeyboardInterrupt), (ExitingIteration, SystemExit)],
)
def test_an_interrupt_while_reading_a_directory_payload_is_not_a_protocol_error(
    container, interrupt
):
    with pytest.raises(interrupt):
        AgentDirectory.rebuild((poisoned_directory_event(container),))
    with pytest.raises(interrupt):
        validate_agent_directory_events((poisoned_directory_event(container),))


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
        ({"flag": 1}, {"flag": True}),
        ({"flag": 0}, {"flag": False}),
        ({"n": 1}, {"n": 1.0}),
        ({"l": [1]}, {"l": [True]}),
        ({"d": {"x": 0}}, {"d": {"x": False}}),
        ({"deep": {"a": [{"b": 1}]}}, {"deep": {"a": [{"b": True}]}}),
    ],
)
async def test_metadata_that_is_only_python_equal_is_not_our_fact(mine, theirs):
    """Python equality is not JSON identity.

    ``True == 1``, ``1 == 1.0`` and ``[True] == [1]`` are all true in Python
    while being different facts in a log, so a plain ``==`` on metadata let a
    racing writer's Agent be reported as ours.
    """

    inner = InMemoryEventStore()

    async def other_writer():
        await AgentRegistrar(inner).create_agent(
            spec(metadata=theirs), request_id="shared", agent_id="a1", session_id="s1"
        )

    with pytest.raises(AgentCreationError) as error:
        await AgentRegistrar(LosingRaceStore(inner, other_writer)).create_agent(
            spec(metadata=mine), request_id="shared", agent_id="a1", session_id="s1"
        )

    assert error.value.committed is False
    assert (await read_directory(inner)).get("a1").metadata == theirs


@pytest.mark.parametrize(
    "metadata",
    [{"flag": 1}, {"flag": True}, {"n": 1.0}, {"l": [1, True, 1.0]}, {"deep": {"a": [{"b": 0}]}}],
)
async def test_our_own_metadata_still_matches_exactly(metadata):
    """The stricter comparison must not reject our own committed fact."""

    inner = InMemoryEventStore()

    with pytest.raises(AgentCreationError) as error:
        await AgentRegistrar(CommitThenFailStore(inner)).create_agent(
            spec(metadata=metadata), request_id="r1", agent_id="a1", session_id="s1"
        )

    assert error.value.committed is True


@pytest.mark.parametrize("field", ["type", "stream_id", "schema_version"])
def test_a_hostile_envelope_protocol_field_is_a_directory_protocol_error(field):
    """The exception boundary must cover the whole event, not only its payload."""

    data = agent_created_data(agent_id="a1", session_id="s1", request_id="r1", spec=spec())
    event = replace_envelope_field(
        EventEnvelope(
            event_id=uuid4(),
            stream_id=AGENT_DIRECTORY_STREAM,
            seq=1,
            type=AGENT_CREATED,
            schema_version=1,
            data=data,
            occurred_at=datetime.now(UTC),
        ),
        field,
        HostileComparison("whatever"),
    )

    with pytest.raises(AgentDirectoryProtocolError) as error:
        AgentDirectory.rebuild((event,))
    assert error.value.code == "agent-payload-invalid"

    issues = validate_agent_directory_events((event,))
    assert [issue.code for issue in issues] == ["agent-payload-invalid"]


@pytest.mark.parametrize(
    ("container", "interrupt"),
    [(InterruptingComparison, KeyboardInterrupt), (ExitingComparison, SystemExit)],
)
@pytest.mark.parametrize("field", ["type", "stream_id", "schema_version"])
def test_an_interrupt_from_a_directory_envelope_field_is_not_a_protocol_error(
    field, container, interrupt
):
    data = agent_created_data(agent_id="a1", session_id="s1", request_id="r1", spec=spec())
    event = replace_envelope_field(
        EventEnvelope(
            event_id=uuid4(),
            stream_id=AGENT_DIRECTORY_STREAM,
            seq=1,
            type=AGENT_CREATED,
            schema_version=1,
            data=data,
            occurred_at=datetime.now(UTC),
        ),
        field,
        container("whatever"),
    )

    with pytest.raises(interrupt):
        AgentDirectory.rebuild((event,))
    with pytest.raises(interrupt):
        validate_agent_directory_events((event,))


def _directory_envelope(**overrides) -> EventEnvelope:
    data = agent_created_data(agent_id="a1", session_id="s1", request_id="r1", spec=spec())
    fields = {
        "event_id": uuid4(),
        "stream_id": AGENT_DIRECTORY_STREAM,
        "seq": 1,
        "type": AGENT_CREATED,
        "schema_version": 1,
        "data": data,
        "occurred_at": datetime.now(UTC),
    }
    fields.update(overrides)
    return EventEnvelope(**fields)


@pytest.mark.parametrize("field", ["type", "stream_id", "schema_version"])
def test_the_directory_parser_itself_converts_a_hostile_envelope_field(field):
    """Pinned directly on the parser, not only through ``rebuild``.

    ``_scan`` has its own net, so a leak inside the parser is invisible from
    the projector - but `parse_agent_created` is public and is also what commit
    reconciliation calls, so its boundary has to hold on its own.
    """

    event = replace_envelope_field(_directory_envelope(), field, HostileComparison("whatever"))
    with pytest.raises(AgentDirectoryProtocolError) as error:
        parse_agent_created(event)
    assert error.value.code == "agent-payload-invalid"


@pytest.mark.parametrize(
    ("container", "interrupt"),
    [(InterruptingComparison, KeyboardInterrupt), (ExitingComparison, SystemExit)],
)
def test_the_directory_parser_still_propagates_an_interrupt(container, interrupt):
    event = replace_envelope_field(_directory_envelope(), "type", container("whatever"))
    with pytest.raises(interrupt):
        parse_agent_created(event)


@pytest.mark.parametrize("field", ["type", "stream_id", "schema_version"])
async def test_directory_reconciliation_survives_a_hostile_neighbouring_event(field):
    """A hostile event next to ours must not make our own answer unknown."""

    inner = InMemoryEventStore()

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
            # Injected only into the *reconciliation* read; injecting into the
            # initial rebuild would fail the transaction closed before the
            # append, which tests a different thing.
            if not self.committed:
                return real
            hostile = replace_envelope_field(
                _directory_envelope(seq=99), field, HostileComparison("whatever")
            )
            return (hostile, *real)

        async def head(self, stream_id):
            return await self.inner.head(stream_id)

        async def list_streams(self, *, prefix=None):
            return await self.inner.list_streams(prefix=prefix)

    with pytest.raises(AgentCreationError) as error:
        await AgentRegistrar(CommitThenPoisonEnvelope(inner)).create_agent(
            SPEC, request_id="r1", agent_id="a1", session_id="s1"
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
        if not self.committed:
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


async def test_a_committed_agent_that_cannot_be_compared_is_unknown_not_uncommitted():
    """Through the real registrar, not the helper."""

    inner = InMemoryEventStore()

    with pytest.raises(AgentCreationError) as error:
        await AgentRegistrar(UnreadableAfterCommit(inner, UnreadableItems)).create_agent(
            SPEC, request_id="r1", agent_id="a1", session_id="s1"
        )

    assert error.value.committed is None
    assert "unknown" in str(error.value)
    assert len(await inner.read(AGENT_DIRECTORY_STREAM)) == 1


@pytest.mark.parametrize(
    ("container", "interrupt"),
    [(InterruptingItems, KeyboardInterrupt), (ExitingItems, SystemExit)],
)
async def test_an_interrupt_during_directory_comparison_still_propagates(container, interrupt):
    inner = InMemoryEventStore()

    with pytest.raises(interrupt):
        await AgentRegistrar(UnreadableAfterCommit(inner, container)).create_agent(
            SPEC, request_id="r1", agent_id="a1", session_id="s1"
        )


async def test_a_readable_directory_still_answers_true_and_false_definitively():
    """The unknown state must not swallow the two knowable ones."""

    inner = InMemoryEventStore()

    with pytest.raises(AgentCreationError) as committed:
        await AgentRegistrar(CommitThenFailStore(inner)).create_agent(
            SPEC, request_id="r1", agent_id="a1", session_id="s1"
        )
    assert committed.value.committed is True

    gated = GatedStore(InMemoryEventStore())
    gated.append_failure = RuntimeError("never committed")
    gated.append_release.set()
    with pytest.raises(AgentCreationError) as missing:
        await AgentRegistrar(gated).create_agent(
            SPEC, request_id="r2", agent_id="a2", session_id="s2"
        )
    assert missing.value.committed is False
