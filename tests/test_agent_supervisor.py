"""The Supervisor turns durable facts into work, and must never invent any.

Every test here works one of four boundaries:

* **one Activation per Agent** - concurrent resumes build one runtime, not two;
* **durable claim before execution** - no model call happens until the claim is
  provably in the log, because a claim that is not durable is a claim another
  worker cannot see;
* **FIFO** - messages run in accepted order and none is skipped;
* **convergence** - dispose and repeated cancellation leave no Task, no
  unretrieved exception and no half-recorded delivery.

Timing is never guessed. Gates and `asyncio.Event` make each scenario
deterministic; the only ``sleep(0)`` calls deliver a cancellation that has
already been requested, and say so.
"""

from __future__ import annotations

import asyncio
import gc
import inspect

import pytest
from supervision_fixtures import (
    SPEC,
    GatedProvider,
    InMemoryEventStore,
    RecordingProvider,
    RuntimeFactory,
    StubExecution,
    destroyed_pending,
    message,
    never_retrieved,
    read_delivery,
    register_agent,
    settle,
)

from traceh.agents import (
    AgentIdentityConflictError,
    AgentInboxReader,
    AgentInboxService,
    AgentRequestConflictError,
    AgentUnknownError,
)
from traceh.api.agents import AgentSpec, AgentSupervisor, MessageTarget
from traceh.api.turns import TurnInput
from traceh.session.event_store import Durability, EventStore
from traceh.supervision import (
    ActivationFaultedError,
    AgentDeliveryService,
    AgentNotActiveError,
    AgentNotFoundError,
    AgentRuntimeExecution,
    ExecutionSessionMismatchError,
    ExecutionStoreMismatchError,
    MessageWakeError,
    ProcessAgentSupervisor,
    SupervisorDisposedError,
    UnsupportedMessageTargetError,
    agent_delivery_stream,
)


@pytest.fixture
async def loop_reports():
    loop = asyncio.get_running_loop()
    reports: list[dict] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reports.append(context))
    yield reports
    loop.set_exception_handler(previous)


@pytest.fixture
def world(tmp_path):
    store = InMemoryEventStore()
    factory = RuntimeFactory(store, tmp_path)
    supervisor = ProcessAgentSupervisor(store=store, factory=factory)
    return store, factory, supervisor


async def send(supervisor, agent_id, message_id, *, wakeup=True, content=None):
    return await supervisor.send(
        agent_id,
        message(message_id, content=content if content is not None else f"task {message_id}"),
        target=MessageTarget.NEW_TURN,
        wakeup=wakeup,
    )


# ---------------------------------------------------------------- happy path


async def test_an_accepted_message_becomes_a_claim_a_turn_and_a_completion(world):
    store, factory, supervisor = world
    handle = await supervisor.create(SPEC, request_id="request-1")

    await send(supervisor, handle.agent_id, "m1")
    await supervisor.wait_idle(handle.agent_id)

    log = await read_delivery(store, handle.agent_id)
    claim = log.claim_for("m1")
    assert claim is not None
    assert claim.session_id == handle.session_id
    assert claim.activation_id == handle.activation_id
    outcome = log.outcome_for_message("m1")
    assert outcome.state == "completed"

    # The completion points at a Turn that really exists in the Session.
    session_events = await store.read(f"session:{handle.session_id}")
    turn_ids = {
        event.data.get("turn_id") for event in session_events if event.type == "turn/start"
    }
    assert outcome.turn_id in turn_ids
    await supervisor.aclose()


async def test_the_control_plane_message_identity_reaches_the_session(world):
    """The whole point of `TurnInput`: the Turn is addressable afterwards."""

    store, factory, supervisor = world
    handle = await supervisor.create(SPEC, request_id="request-1")
    await supervisor.send(
        handle.agent_id,
        message("m-control", content="line one\nline two", source="reviewer"),
        target=MessageTarget.NEW_TURN,
        wakeup=True,
    )
    await supervisor.wait_idle(handle.agent_id)

    events = await store.read(f"session:{handle.session_id}")
    accepted = next(event for event in events if event.type == "inbox/accepted")
    claimed = next(event for event in events if event.type == "inbox/claimed")
    started = next(event for event in events if event.type == "turn/start")
    assert accepted.data["message_id"] == "m-control"
    assert claimed.data["message_id"] == "m-control"
    assert started.data["message_id"] == "m-control"
    assert accepted.data["source"] == "reviewer"
    assert accepted.data["content"] == "line one\nline two"

    log = await read_delivery(store, handle.agent_id)
    assert log.outcome_for_message("m-control").turn_id == started.data["turn_id"]
    await supervisor.aclose()


async def test_a_plain_string_task_keeps_its_previous_behaviour(tmp_path):
    """`run_existing(session_id, "text")` must be untouched by Stage C."""

    store = InMemoryEventStore()
    factory = RuntimeFactory(store, tmp_path)
    runtime = factory._runtime()
    session_id = await runtime.create_session(factory._workspace(SPEC))
    result = await runtime.run_existing(session_id, "plain task")

    events = await store.read(f"session:{session_id}")
    accepted = next(event for event in events if event.type == "inbox/accepted")
    started = next(event for event in events if event.type == "turn/start")
    assert accepted.data["source"] == "user"
    assert accepted.data["content"] == "plain task"
    # The id is freshly generated, exactly as before Stage C.
    assert accepted.data["message_id"] != "plain task"
    assert accepted.data["message_id"] == started.data["message_id"]
    assert result.turn_id == started.data["turn_id"]
    await runtime.dispose()


async def test_turn_input_round_trips_through_the_runtime(tmp_path):
    store = InMemoryEventStore()
    factory = RuntimeFactory(store, tmp_path)
    runtime = factory._runtime()
    session_id = await runtime.create_session(factory._workspace(SPEC))
    await runtime.run_existing(
        session_id, TurnInput(content="body", message_id="chosen", source="evaluator")
    )

    events = await store.read(f"session:{session_id}")
    accepted = next(event for event in events if event.type == "inbox/accepted")
    assert (accepted.data["message_id"], accepted.data["source"]) == ("chosen", "evaluator")
    await runtime.dispose()


# ------------------------------------------------------------------- FIFO


async def test_messages_run_in_strict_accepted_order(world):
    store, factory, supervisor = world
    handle = await supervisor.create(SPEC, request_id="request-1")
    for index in range(5):
        await send(supervisor, handle.agent_id, f"m{index}")
    await supervisor.wait_idle(handle.agent_id)

    log = await read_delivery(store, handle.agent_id)
    assert [claim.message_id for claim in log.claims] == [f"m{i}" for i in range(5)]
    assert [claim.accepted_seq for claim in log.claims] == [1, 2, 3, 4, 5]
    assert all(log.outcome_for_claim(c.claim_id).state == "completed" for c in log.claims)
    await supervisor.aclose()


async def test_two_supervisors_claim_and_run_each_message_once(tmp_path):
    """Two Supervisors on one store are two workers competing for one Inbox."""

    store = InMemoryEventStore()
    first_factory = RuntimeFactory(store, tmp_path / "a")
    second_factory = RuntimeFactory(store, tmp_path / "b")
    first = ProcessAgentSupervisor(store=store, factory=first_factory)
    handle = await first.create(SPEC, request_id="request-1")
    second = ProcessAgentSupervisor(store=store, factory=second_factory)

    for index in range(4):
        await send(first, handle.agent_id, f"m{index}", wakeup=False)
    await second.resume(handle.session_id)
    await first.send(
        handle.agent_id, message("m-wake"), target=MessageTarget.NEW_TURN, wakeup=True
    )
    await first.wait_idle(handle.agent_id)
    await second.wait_idle(handle.agent_id)

    log = await read_delivery(store, handle.agent_id)
    claimed = [claim.message_id for claim in log.claims]
    assert sorted(claimed) == sorted({f"m{i}" for i in range(4)} | {"m-wake"})
    # Every message claimed exactly once, by exactly one of the two.
    assert len(claimed) == len(set(claimed))
    assert all(log.outcome_for_claim(c.claim_id) is not None for c in log.claims)
    await first.aclose()
    await second.aclose()


# ------------------------------------------------------- claim before running


class ClaimGateStore:
    """Holds the claim append open, so a test can look before it lands."""

    def __init__(self, inner: EventStore) -> None:
        self.inner = inner
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.fail_with: BaseException | None = None
        self.swap_reads = False

    async def append(self, stream_id, *, expected_seq, events, durability=Durability.SYNC):
        if stream_id.startswith("agent-delivery:") and events[0].type == "agent/message-claimed":
            if self.fail_with is not None:
                if self.swap_reads:
                    await self.inner.append(
                        stream_id,
                        expected_seq=expected_seq,
                        events=events,
                        durability=durability,
                    )
                self.entered.set()
                raise self.fail_with
            self.entered.set()
            await self.release.wait()
        return await self.inner.append(
            stream_id, expected_seq=expected_seq, events=events, durability=durability
        )

    async def read(self, stream_id, *, from_seq=1):
        return await self.inner.read(stream_id, from_seq=from_seq)

    async def head(self, stream_id):
        return await self.inner.head(stream_id)

    async def list_streams(self, *, prefix=None):
        return await self.inner.list_streams(prefix=prefix)


async def test_no_model_is_called_before_the_claim_is_durable(tmp_path):
    inner = InMemoryEventStore()
    gate = ClaimGateStore(inner)
    provider = RecordingProvider()
    # The factory builds on the *same* store object the Supervisor holds;
    # anything else is refused by the execution-store identity check, which is
    # a different test.
    factory = RuntimeFactory(gate, tmp_path, provider=provider)
    supervisor = ProcessAgentSupervisor(store=gate, factory=factory)
    handle = await supervisor.create(SPEC, request_id="request-1")

    await send(supervisor, handle.agent_id, "m1")
    await gate.entered.wait()
    # The claim append is suspended: nothing is in the delivery stream yet, and
    # nothing may have reached the model.
    assert await gate.inner.read(agent_delivery_stream(handle.agent_id)) == ()
    assert provider.calls == 0

    gate.release.set()
    await supervisor.wait_idle(handle.agent_id)
    assert provider.calls >= 1
    await supervisor.aclose()


class UnreadableItems(dict):
    def items(self):
        raise ValueError("items() is hostile")


class UnknownClaimStore(ClaimGateStore):
    """Commits the claim, then makes the outcome unprovable."""

    async def read(self, stream_id, *, from_seq=1):
        real = await self.inner.read(stream_id, from_seq=from_seq)
        if not self.swap_reads or not stream_id.startswith("agent-delivery:"):
            return real
        from supervision_fixtures import replace_envelope_field

        return tuple(
            replace_envelope_field(event, "data", UnreadableItems(event.data)) for event in real
        )


async def test_an_unknown_claim_never_runs_a_turn_and_faults_the_activation(tmp_path):
    """A claim we cannot prove is a claim we must not act on."""

    inner = InMemoryEventStore()
    gate = UnknownClaimStore(inner)
    provider = RecordingProvider()
    factory = RuntimeFactory(gate, tmp_path, provider=provider)
    supervisor = ProcessAgentSupervisor(store=gate, factory=factory)
    handle = await supervisor.create(SPEC, request_id="request-1")

    gate.fail_with = RuntimeError("append blew up after committing")
    gate.swap_reads = True
    await send(supervisor, handle.agent_id, "m1")

    with pytest.raises(ActivationFaultedError) as error:
        await supervisor.wait_idle(handle.agent_id)
    assert error.value.fault_code == "claim-not-durable"
    assert provider.calls == 0
    await supervisor.aclose()


async def test_a_lost_claim_race_is_not_a_fault(tmp_path):
    """Losing a claim is ordinary: someone else is running that message."""

    store = InMemoryEventStore()
    factory = RuntimeFactory(store, tmp_path)
    supervisor = ProcessAgentSupervisor(store=store, factory=factory)
    handle = await supervisor.create(SPEC, request_id="request-1")
    await send(supervisor, handle.agent_id, "m1", wakeup=False)

    # Another worker claims and finishes it first.
    other = ProcessAgentSupervisor(store=store, factory=RuntimeFactory(store, tmp_path / "b"))
    await other.resume(handle.session_id)
    await other.wait_idle(handle.agent_id)

    await supervisor.send(
        handle.agent_id, message("m2"), target=MessageTarget.NEW_TURN, wakeup=True
    )
    await supervisor.wait_idle(handle.agent_id)

    log = await read_delivery(store, handle.agent_id)
    assert {claim.message_id for claim in log.claims} == {"m1", "m2"}
    await supervisor.aclose()
    await other.aclose()


async def test_an_open_claim_blocks_every_later_fifo_message(tmp_path):
    store = InMemoryEventStore()
    provider = RecordingProvider()
    factory = RuntimeFactory(store, tmp_path, provider=provider)
    supervisor = ProcessAgentSupervisor(store=store, factory=factory)
    handle = await supervisor.create(SPEC, request_id="request-1")
    await send(supervisor, handle.agent_id, "m1", wakeup=False)
    inbox = await AgentInboxReader(store).load(handle.agent_id)
    delivery_service = AgentDeliveryService(store)
    await delivery_service.claim(
        agent_id=handle.agent_id,
        accepted=inbox.get("m1"),
        claim_id="external-open-claim",
        activation_id="external-activation",
        session_id=handle.session_id,
        inbox=inbox,
        delivery=await delivery_service.delivery_log(handle.agent_id, inbox),
    )

    await send(supervisor, handle.agent_id, "m2", wakeup=True)
    await supervisor.wait_idle(handle.agent_id)

    log = await read_delivery(store, handle.agent_id)
    assert log.claim_for("m1") is not None
    assert log.claim_for("m2") is None
    assert provider.calls == 0
    await supervisor.aclose()


# ------------------------------------------------------------------ wakeup


async def test_wakeup_false_accepts_without_starting_anything(world):
    store, factory, supervisor = world
    handle = await supervisor.create(SPEC, request_id="request-1")
    provisions_before = factory.provisions

    receipt = await send(supervisor, handle.agent_id, "m1", wakeup=False)
    await settle()

    assert receipt.message_id == "m1"
    assert (await AgentInboxReader(store).load(handle.agent_id)).get("m1") is not None
    # Durably accepted and deliberately not run.
    assert await store.read(agent_delivery_stream(handle.agent_id)) == ()
    assert factory.provisions == provisions_before
    await supervisor.aclose()


async def test_wakeup_false_then_resume_drains_the_deferred_message(world):
    store, factory, supervisor = world
    handle = await supervisor.create(SPEC, request_id="request-1")
    await send(supervisor, handle.agent_id, "m1", wakeup=False)
    await supervisor.dispose(handle.agent_id)

    await supervisor.resume(handle.session_id)
    await supervisor.wait_idle(handle.agent_id)

    log = await read_delivery(store, handle.agent_id)
    assert log.outcome_for_message("m1").state == "completed"
    await supervisor.aclose()


async def test_wakeup_creates_an_activation_when_none_exists(world):
    store, factory, supervisor = world
    handle = await supervisor.create(SPEC, request_id="request-1")
    await supervisor.dispose(handle.agent_id)
    activations_before = factory.activations

    await send(supervisor, handle.agent_id, "m1", wakeup=True)
    await supervisor.wait_idle(handle.agent_id)

    assert factory.activations == activations_before + 1
    assert (await read_delivery(store, handle.agent_id)).outcome_for_message("m1") is not None
    await supervisor.aclose()


async def test_a_wake_arriving_while_the_worker_finishes_is_not_lost(world):
    """The window between "drain finished" and "now idle" must not swallow it."""

    store, factory, supervisor = world
    handle = await supervisor.create(SPEC, request_id="request-1")

    for index in range(12):
        # Sent without waiting, so each wake lands at an arbitrary point in the
        # worker's cycle - including the moment it is about to park.
        await send(supervisor, handle.agent_id, f"m{index}")
    await supervisor.wait_idle(handle.agent_id)

    log = await read_delivery(store, handle.agent_id)
    assert len(log.claims) == 12
    assert all(log.outcome_for_claim(c.claim_id) is not None for c in log.claims)
    await supervisor.aclose()


async def test_next_step_is_refused_before_anything_is_accepted(world):
    store, factory, supervisor = world
    handle = await supervisor.create(SPEC, request_id="request-1")

    with pytest.raises(UnsupportedMessageTargetError) as error:
        await supervisor.send(
            handle.agent_id, message("m1"), target=MessageTarget.NEXT_STEP, wakeup=True
        )

    assert error.value.code == "message-target-unsupported"
    assert len(await AgentInboxReader(store).load(handle.agent_id)) == 0
    assert await store.read(agent_delivery_stream(handle.agent_id)) == ()
    await supervisor.aclose()


async def test_a_next_step_written_directly_is_failed_rather_than_skipped(world):
    """Stage B still accepts it; the Supervisor records an honest failure."""

    store, factory, supervisor = world
    handle = await supervisor.create(SPEC, request_id="request-1")
    await AgentInboxService(store).accept(
        handle.agent_id, message("m-next"), target=MessageTarget.NEXT_STEP, wakeup=False
    )
    await send(supervisor, handle.agent_id, "m-after")
    await supervisor.wait_idle(handle.agent_id)

    log = await read_delivery(store, handle.agent_id)
    assert log.outcome_for_message("m-next").state == "failed"
    assert log.outcome_for_message("m-next").code == "unsupported-target"
    # FIFO was not broken: the later message still ran, after it.
    assert [claim.message_id for claim in log.claims] == ["m-next", "m-after"]
    assert log.outcome_for_message("m-after").state == "completed"
    await supervisor.aclose()


async def test_a_wake_failure_still_reports_the_durable_acceptance(world):
    store, factory, supervisor = world
    handle = await supervisor.create(SPEC, request_id="request-1")
    await supervisor.dispose(handle.agent_id)
    factory.activate_error = RuntimeError("factory is down")

    with pytest.raises(MessageWakeError) as error:
        await send(supervisor, handle.agent_id, "m1", wakeup=True)

    # The message really was accepted; pretending otherwise would invite a
    # retry that appends it a second time under a new id.
    assert error.value.receipt.message_id == "m1"
    assert (await AgentInboxReader(store).load(handle.agent_id)).get("m1") is not None
    await supervisor.aclose()


# ------------------------------------------------------- single Activation


async def test_concurrent_resume_builds_exactly_one_activation(world):
    store, factory, supervisor = world
    handle = await supervisor.create(SPEC, request_id="request-1")
    await supervisor.dispose(handle.agent_id)
    activations_before = factory.activations

    gate = asyncio.Event()
    factory.activate_gate = gate
    resumes = [
        asyncio.create_task(supervisor.resume(handle.session_id)) for _ in range(6)
    ]
    await settle()
    gate.set()
    handles = await asyncio.gather(*resumes)

    assert factory.activations == activations_before + 1
    assert len({item.activation_id for item in handles}) == 1
    await supervisor.aclose()


async def test_one_agent_never_has_two_live_runtimes(world):
    store, factory, supervisor = world
    handle = await supervisor.create(SPEC, request_id="request-1")
    before = factory.calls

    for _ in range(4):
        again = await supervisor.resume(handle.session_id)
        assert again.activation_id == handle.activation_id
    assert factory.calls == before
    await supervisor.aclose()


async def test_dispose_then_resume_builds_a_new_activation(world):
    store, factory, supervisor = world
    first = await supervisor.create(SPEC, request_id="request-1")
    await supervisor.dispose(first.agent_id)
    second = await supervisor.resume(first.session_id)

    assert second.activation_id != first.activation_id
    assert second.agent_id == first.agent_id
    # The old execution really was released.
    assert factory.executions[0]._disposed
    await supervisor.aclose()


async def test_dispose_converges_an_inflight_resume_before_return(world):
    store, factory, supervisor = world
    handle = await supervisor.create(SPEC, request_id="request-1")
    await supervisor.dispose(handle.agent_id)
    factory.activate_gate = asyncio.Event()
    resume = asyncio.create_task(supervisor.resume(handle.session_id))
    await factory.activate_entered.wait()

    await supervisor.dispose(handle.agent_id)

    assert resume.done(), "dispose returned while activation construction survived"
    with pytest.raises(asyncio.CancelledError):
        await resume
    with pytest.raises(AgentNotActiveError):
        await supervisor.wait_idle(handle.agent_id)
    await supervisor.aclose()


async def test_no_turn_slips_into_a_disposing_activation(world):
    store, factory, supervisor = world
    handle = await supervisor.create(SPEC, request_id="request-1")
    await supervisor.dispose(handle.agent_id)

    # After disposal there is no Activation to wake, and ``wait_idle`` says so
    # rather than pretending the Agent is quietly up to date.
    with pytest.raises(AgentNotActiveError):
        await supervisor.wait_idle(handle.agent_id)
    with pytest.raises(AgentNotActiveError):
        await supervisor.interrupt(handle.agent_id, "nope")
    await supervisor.aclose()


class ForeignStoreFactory(RuntimeFactory):
    async def activate(self, record):
        self.activations += 1
        from traceh.supervision import AgentRuntimeExecution

        other = InMemoryEventStore()
        runtime = self._runtime.__func__(self)  # type: ignore[attr-defined]
        del runtime
        from supervision_fixtures import scripted

        from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime

        foreign = build_default_runtime(
            RuntimeConfig(data_dir=self.root / "other", provider="scripted", model="m"),
            provider=scripted(),
            event_store=other,
        )
        return AgentRuntimeExecution(foreign, record.session_id)


async def test_an_execution_on_another_store_is_refused(tmp_path):
    store = InMemoryEventStore()
    good = RuntimeFactory(store, tmp_path)
    supervisor = ProcessAgentSupervisor(store=store, factory=good)
    handle = await supervisor.create(SPEC, request_id="request-1")
    await supervisor.dispose(handle.agent_id)

    foreign = ForeignStoreFactory(store, tmp_path)
    strict = ProcessAgentSupervisor(store=store, factory=foreign)
    with pytest.raises(ExecutionStoreMismatchError):
        await strict.resume(handle.session_id)
    await supervisor.aclose()
    await strict.aclose()


class WrongSessionFactory(RuntimeFactory):
    async def activate(self, record):
        self.activations += 1
        return StubExecution(self.store, "some-other-session")


class FailingCleanupExecution(StubExecution):
    async def dispose(self) -> None:
        self.disposals += 1
        raise RuntimeError("candidate cleanup failed")


class WrongSessionFailingCleanupFactory(RuntimeFactory):
    def __init__(self, store, root) -> None:
        super().__init__(store, root)
        self.candidate: FailingCleanupExecution | None = None

    async def activate(self, record):
        self.activations += 1
        self.candidate = FailingCleanupExecution(self.store, "some-other-session")
        return self.candidate


async def test_an_execution_bound_to_another_session_is_refused(tmp_path):
    store = InMemoryEventStore()
    supervisor = ProcessAgentSupervisor(store=store, factory=RuntimeFactory(store, tmp_path))
    handle = await supervisor.create(SPEC, request_id="request-1")
    await supervisor.dispose(handle.agent_id)

    strict = ProcessAgentSupervisor(store=store, factory=WrongSessionFactory(store, tmp_path))
    with pytest.raises(ExecutionSessionMismatchError):
        await strict.resume(handle.session_id)
    await supervisor.aclose()
    await strict.aclose()


async def test_activation_failure_preserves_candidate_cleanup_failure(tmp_path):
    store = InMemoryEventStore()
    owner = ProcessAgentSupervisor(
        store=store, factory=RuntimeFactory(store, tmp_path / "owner")
    )
    handle = await owner.create(SPEC, request_id="request-1")
    await owner.dispose(handle.agent_id)
    factory = WrongSessionFailingCleanupFactory(store, tmp_path / "strict")
    strict = ProcessAgentSupervisor(store=store, factory=factory)

    with pytest.raises(BaseExceptionGroup) as grouped:
        await strict.resume(handle.session_id)

    assert {type(error) for error in grouped.value.exceptions} == {
        ExecutionSessionMismatchError,
        RuntimeError,
    }
    assert factory.candidate is not None
    assert factory.candidate.disposals == 1
    await owner.aclose()
    await strict.aclose()


# ------------------------------------------------------------ create/resume


async def test_a_repeated_request_id_returns_the_same_agent(world):
    store, factory, supervisor = world
    first = await supervisor.create(SPEC, request_id="request-1")
    second = await supervisor.create(SPEC, request_id="request-1")

    assert (first.agent_id, first.session_id) == (second.agent_id, second.session_id)
    assert factory.provisions == 1
    await supervisor.aclose()


async def test_a_repeated_request_id_reconciles_the_complete_request(world):
    store, factory, supervisor = world
    await supervisor.create(SPEC, request_id="request-1")

    with pytest.raises(AgentRequestConflictError):
        await supervisor.create(
            AgentSpec(preset="different-preset", workspace_id=SPEC.workspace_id),
            request_id="request-1",
        )

    assert factory.provisions == 1
    assert len(await supervisor.registrar.directory()) == 1
    await supervisor.aclose()


async def test_inflight_create_rejects_a_different_request_before_joining(world):
    store, factory, supervisor = world
    gate = asyncio.Event()
    factory.provision_gate = gate
    first = asyncio.create_task(supervisor.create(SPEC, request_id="request-1"))
    await factory.provision_entered.wait()

    with pytest.raises(AgentRequestConflictError):
        await supervisor.create(
            AgentSpec(preset="different-preset", workspace_id=SPEC.workspace_id),
            request_id="request-1",
        )

    gate.set()
    handle = await first
    assert handle.agent_id
    assert factory.provisions == 1
    await supervisor.aclose()


async def test_concurrent_creates_with_one_request_id_create_one_agent(world):
    store, factory, supervisor = world
    creates = [
        asyncio.create_task(supervisor.create(SPEC, request_id="request-1")) for _ in range(5)
    ]
    handles = await asyncio.gather(*creates)

    assert len({item.agent_id for item in handles}) == 1
    directory = await supervisor.registrar.directory()
    assert len(directory) == 1
    assert factory.provisions == 1
    await supervisor.aclose()


async def test_aclose_converges_an_inflight_create_before_return(world):
    store, factory, supervisor = world
    factory.provision_gate = asyncio.Event()
    creation = asyncio.create_task(
        supervisor.create(SPEC, request_id="request-1", agent_id="pending-agent")
    )
    await factory.provision_entered.wait()

    await supervisor.aclose()

    assert creation.done(), "aclose returned while provisioning survived"
    with pytest.raises(asyncio.CancelledError):
        await creation
    assert len(await supervisor.registrar.directory()) == 0


class CancellationResistantProvisionFactory(RuntimeFactory):
    def __init__(self, store, root) -> None:
        super().__init__(store, root)
        self.release = asyncio.Event()
        self.cancellation_entered = asyncio.Event()
        self.cancellations = 0

    async def provision(self, spec, *, agent_id, session_id):
        self.provision_entered.set()
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancellations += 1
                self.cancellation_entered.set()
        return await super().provision(spec, agent_id=agent_id, session_id=session_id)


async def test_aclose_absorbs_repeated_cancellation_until_create_cleanup(tmp_path):
    store = InMemoryEventStore()
    factory = CancellationResistantProvisionFactory(store, tmp_path)
    supervisor = ProcessAgentSupervisor(store=store, factory=factory)
    creation = asyncio.create_task(
        supervisor.create(SPEC, request_id="request-1", agent_id="pending-agent")
    )
    await factory.provision_entered.wait()
    closing = asyncio.create_task(supervisor.aclose())
    await factory.cancellation_entered.wait()
    try:
        for _ in range(3):
            closing.cancel()
            await asyncio.sleep(0)
            assert not closing.done()
    finally:
        factory.release.set()

    with pytest.raises(asyncio.CancelledError):
        await closing
    with pytest.raises(SupervisorDisposedError):
        await creation
    await supervisor.aclose()
    assert factory.cancellations >= 1
    assert all(execution._disposed for execution in factory.executions)


async def test_a_failed_identity_append_releases_the_candidate_runtime(world):
    """Session first, identity second - and the loser is cleaned up."""

    store, factory, supervisor = world
    await register_agent(store, agent_id="taken", session_id="s-taken", request_id="other")

    with pytest.raises(AgentIdentityConflictError):
        await supervisor.create(SPEC, request_id="request-1", agent_id="taken")

    assert factory.executions
    assert factory.executions[-1]._disposed
    # No Activation was installed for a record that does not exist.
    with pytest.raises(AgentNotActiveError):
        await supervisor.wait_idle("taken")
    await supervisor.aclose()


async def test_a_failed_provision_does_not_pollute_the_registry(world):
    store, factory, supervisor = world
    factory.provision_error = RuntimeError("no workspace")

    with pytest.raises(RuntimeError):
        await supervisor.create(SPEC, request_id="request-1")

    assert len(await supervisor.registrar.directory()) == 0
    factory.provision_error = None
    handle = await supervisor.create(SPEC, request_id="request-2")
    assert handle.agent_id
    await supervisor.aclose()


async def test_resume_of_an_unknown_session_or_agent_fails_clearly(world):
    store, factory, supervisor = world
    with pytest.raises(AgentNotFoundError):
        await supervisor.resume("no-such-session")

    # A record whose Session was never created is refused too.
    await register_agent(store, agent_id="ghost", session_id="ghost-session", request_id="r-g")
    with pytest.raises(AgentNotFoundError):
        await supervisor.resume("ghost-session")
    await supervisor.aclose()


async def test_sending_to_an_unknown_agent_writes_nothing(world):
    store, factory, supervisor = world
    with pytest.raises(AgentUnknownError):
        await send(supervisor, "no-such-agent", "m1")
    assert await store.list_streams(prefix="agent-inbox:") == ()
    await supervisor.aclose()


# -------------------------------------------------- failure and cancellation


class FailingExecution(StubExecution):
    async def run_turn(self, turn_input):
        self.turns += 1
        raise RuntimeError("provider exploded with a secret: sk-FAKE-NOT-REAL")


class FailingFactory(RuntimeFactory):
    async def activate(self, record):
        self.activations += 1
        return FailingExecution(self.store, record.session_id)


class FailingInboxReadStore(InMemoryEventStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_inbox_reads = False

    async def read(self, stream_id, *, from_seq=1):
        if self.fail_inbox_reads and stream_id.startswith("agent-inbox:"):
            raise RuntimeError("untrusted storage failure")
        return await super().read(stream_id, from_seq=from_seq)


async def test_worker_failure_is_reported_and_future_wakes_fail(tmp_path):
    store = FailingInboxReadStore()
    factory = RuntimeFactory(store, tmp_path)
    supervisor = ProcessAgentSupervisor(store=store, factory=factory)
    handle = await supervisor.create(SPEC, request_id="request-1")
    await send(supervisor, handle.agent_id, "m1", wakeup=False)
    store.fail_inbox_reads = True

    await supervisor.resume(handle.session_id)
    with pytest.raises(ActivationFaultedError) as error:
        await supervisor.wait_idle(handle.agent_id)
    assert error.value.fault_code == "worker-failed"
    with pytest.raises(ActivationFaultedError):
        await supervisor.resume(handle.session_id)
    await supervisor.aclose()


async def test_a_failing_turn_records_a_stable_code_and_no_exception_text(tmp_path):
    store = InMemoryEventStore()
    supervisor = ProcessAgentSupervisor(store=store, factory=RuntimeFactory(store, tmp_path))
    handle = await supervisor.create(SPEC, request_id="request-1")
    await supervisor.dispose(handle.agent_id)

    failing = ProcessAgentSupervisor(store=store, factory=FailingFactory(store, tmp_path))
    await failing.resume(handle.session_id)
    await send(failing, handle.agent_id, "m1")
    await failing.wait_idle(handle.agent_id)

    log = await read_delivery(store, handle.agent_id)
    outcome = log.outcome_for_message("m1")
    assert (outcome.state, outcome.code) == ("failed", "turn-failed")
    raw = await store.read(agent_delivery_stream(handle.agent_id))
    blob = repr([event.data for event in raw])
    assert "sk-FAKE" not in blob
    assert "Traceback" not in blob
    await supervisor.aclose()
    await failing.aclose()


async def test_interrupt_cancels_only_the_current_turn(tmp_path, loop_reports):
    store = InMemoryEventStore()
    gated = GatedProvider()
    factory = RuntimeFactory(store, tmp_path, provider=gated)
    supervisor = ProcessAgentSupervisor(store=store, factory=factory)
    handle = await supervisor.create(SPEC, request_id="request-1")

    await send(supervisor, handle.agent_id, "m1")
    await gated.entered.wait()
    assert await supervisor.interrupt(handle.agent_id, "operator stopped it")

    # The Activation survives: a second message still runs.
    gated.release.set()
    await send(supervisor, handle.agent_id, "m2")
    await supervisor.wait_idle(handle.agent_id)

    log = await read_delivery(store, handle.agent_id)
    assert log.outcome_for_message("m1").state == "cancelled"
    assert log.outcome_for_message("m2").state == "completed"
    await settle()
    assert never_retrieved(loop_reports) == []
    await supervisor.aclose()


async def test_interrupt_when_idle_is_a_no_op(world):
    store, factory, supervisor = world
    handle = await supervisor.create(SPEC, request_id="request-1")
    assert await supervisor.interrupt(handle.agent_id, "nothing running") is False
    assert await supervisor.interrupt(handle.agent_id, "still nothing") is False
    await supervisor.aclose()


@pytest.mark.parametrize("reason", [None, 7, "", "   ", "a\nb", "a\x1bb", "x" * 500])
async def test_an_unusable_interrupt_reason_is_refused(world, reason):
    store, factory, supervisor = world
    handle = await supervisor.create(SPEC, request_id="request-1")
    from traceh.supervision import DeliveryInputError

    with pytest.raises(DeliveryInputError) as error:
        await supervisor.interrupt(handle.agent_id, reason)
    assert error.value.field == "reason"
    await supervisor.aclose()


async def test_dispose_while_a_turn_is_running_converges(tmp_path, loop_reports):
    store = InMemoryEventStore()
    gated = GatedProvider()
    factory = RuntimeFactory(store, tmp_path, provider=gated)
    supervisor = ProcessAgentSupervisor(store=store, factory=factory)
    # Captured before anything is started, so the comparison covers the worker
    # Task itself rather than treating it as pre-existing.
    before = {task for task in asyncio.all_tasks() if task is not asyncio.current_task()}
    handle = await supervisor.create(SPEC, request_id="request-1")

    await send(supervisor, handle.agent_id, "m1")
    await gated.entered.wait()
    gated.release.set()
    await supervisor.dispose(handle.agent_id)

    await settle()
    after = {task for task in asyncio.all_tasks() if task is not asyncio.current_task()}
    assert after == before
    assert never_retrieved(loop_reports) == []
    assert destroyed_pending(loop_reports) == []
    assert factory.executions[0]._disposed
    await supervisor.aclose()



async def test_repeated_cancellation_cannot_release_dispose_early(tmp_path, loop_reports):
    store = InMemoryEventStore()
    gated = GatedProvider()
    factory = RuntimeFactory(store, tmp_path, provider=gated)
    supervisor = ProcessAgentSupervisor(store=store, factory=factory)
    handle = await supervisor.create(SPEC, request_id="request-1")

    await send(supervisor, handle.agent_id, "m1")
    await gated.entered.wait()

    disposal = asyncio.create_task(supervisor.dispose(handle.agent_id))
    await settle()
    for _ in range(3):
        disposal.cancel()
        # One ``sleep(0)`` per cancel only delivers the request that was just
        # made; it is not a guess about how long anything takes.
        for _ in range(3):
            await asyncio.sleep(0)
        assert not disposal.done(), "repeated cancellation released dispose early"

    gated.release.set()
    with pytest.raises(asyncio.CancelledError):
        await disposal

    # The shutdown itself still finished, because it belongs to its own Task.
    await supervisor.dispose(handle.agent_id)
    assert factory.executions[0]._disposed
    await settle()
    assert never_retrieved(loop_reports) == []
    await supervisor.aclose()


async def test_dispose_is_idempotent_and_runs_the_runtime_shutdown_once(world):
    store, factory, supervisor = world
    handle = await supervisor.create(SPEC, request_id="request-1")

    await supervisor.dispose(handle.agent_id)
    await supervisor.dispose(handle.agent_id)
    await supervisor.dispose(handle.agent_id)

    execution = factory.executions[0]
    assert execution._disposed
    # The adapter guards re-entry, so the runtime's own shutdown ran once.
    assert execution._runtime._disposed
    await supervisor.aclose()


async def test_dispose_keeps_every_durable_fact(world):
    store, factory, supervisor = world
    handle = await supervisor.create(SPEC, request_id="request-1")
    await send(supervisor, handle.agent_id, "m1")
    await supervisor.wait_idle(handle.agent_id)
    await supervisor.dispose(handle.agent_id)

    assert (await supervisor.registrar.directory()).get(handle.agent_id) is not None
    assert len(await AgentInboxReader(store).load(handle.agent_id)) == 1
    assert len(await read_delivery(store, handle.agent_id)) == 1
    await supervisor.aclose()


async def test_dispose_while_the_worker_is_claiming_converges(tmp_path, loop_reports):
    inner = InMemoryEventStore()
    gate = ClaimGateStore(inner)
    factory = RuntimeFactory(gate, tmp_path)
    supervisor = ProcessAgentSupervisor(store=gate, factory=factory)
    # Captured before anything is started, so the comparison covers the worker
    # Task itself rather than treating it as pre-existing.
    before = {task for task in asyncio.all_tasks() if task is not asyncio.current_task()}
    handle = await supervisor.create(SPEC, request_id="request-1")

    await send(supervisor, handle.agent_id, "m1")
    await gate.entered.wait()
    disposal = asyncio.create_task(supervisor.dispose(handle.agent_id))
    await settle()
    gate.release.set()
    await disposal

    await settle()
    after = {task for task in asyncio.all_tasks() if task is not asyncio.current_task()}
    assert after == before
    assert never_retrieved(loop_reports) == []
    assert destroyed_pending(loop_reports) == []
    await supervisor.aclose()


async def test_wait_idle_does_not_wait_for_an_unscheduled_message(world):
    """``wakeup=False`` was never scheduled, so idle is the honest answer."""

    store, factory, supervisor = world
    handle = await supervisor.create(SPEC, request_id="request-1")
    await send(supervisor, handle.agent_id, "m1", wakeup=False)

    await asyncio.wait_for(supervisor.wait_idle(handle.agent_id), timeout=5)
    assert await store.read(agent_delivery_stream(handle.agent_id)) == ()
    await supervisor.aclose()


async def test_cancelling_wait_idle_does_not_cancel_the_agent(tmp_path):
    store = InMemoryEventStore()
    gated = GatedProvider()
    factory = RuntimeFactory(store, tmp_path, provider=gated)
    supervisor = ProcessAgentSupervisor(store=store, factory=factory)
    handle = await supervisor.create(SPEC, request_id="request-1")

    await send(supervisor, handle.agent_id, "m1")
    await gated.entered.wait()
    waiter = asyncio.create_task(supervisor.wait_idle(handle.agent_id))
    await settle()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    gated.release.set()
    await supervisor.wait_idle(handle.agent_id)
    assert (await read_delivery(store, handle.agent_id)).outcome_for_message("m1").state == (
        "completed"
    )
    await supervisor.aclose()


async def test_no_agent_record_is_deleted_and_gc_changes_nothing(world):
    store, factory, supervisor = world
    handle = await supervisor.create(SPEC, request_id="request-1")
    await supervisor.aclose()
    gc.collect()

    directory = await supervisor.registrar.directory()
    assert directory.get(handle.agent_id).session_id == handle.session_id


async def test_the_supervisor_never_touches_agent_runtime_internals(world):
    """A structural check: `AgentRuntime` gains no Supervisor state."""

    store, factory, supervisor = world
    await supervisor.create(SPEC, request_id="request-1")
    runtime = factory.executions[0]._runtime

    for forbidden in ("supervisor", "inbox", "deliveries", "activation", "claims"):
        assert not hasattr(runtime, forbidden), forbidden
    await supervisor.aclose()


def test_agent_loop_does_not_import_the_control_plane():
    import traceh.runtime.agent_loop as loop_module
    import traceh.runtime.agent_runtime as runtime_module

    for module in (loop_module, runtime_module):
        source = module.__file__
        text = open(source, encoding="utf-8").read()
        assert "traceh.agents" not in text, module.__name__
        assert "traceh.supervision" not in text, module.__name__


def test_supervised_handle_satisfies_the_public_protocol():
    from traceh.api.agents import AgentHandle
    from traceh.supervision import SupervisedAgentHandle

    handle = SupervisedAgentHandle(agent_id="a", session_id="s", activation_id="x")
    assert isinstance(handle.agent_id, str)
    assert isinstance(handle.session_id, str)
    assert set(AgentHandle.__annotations__) <= {"agent_id", "session_id"}


def test_process_supervisor_satisfies_the_public_protocol(world):
    store, factory, supervisor = world
    assert isinstance(supervisor, AgentSupervisor)
    for method_name in (
        "create",
        "resume",
        "send",
        "interrupt",
        "wait_idle",
        "dispose",
        "aclose",
    ):
        protocol = inspect.signature(getattr(AgentSupervisor, method_name))
        concrete = inspect.signature(getattr(ProcessAgentSupervisor, method_name))
        assert [
            (item.name, item.kind, item.default)
            for item in protocol.parameters.values()
        ] == [
            (item.name, item.kind, item.default)
            for item in concrete.parameters.values()
        ]


class FailingDisposeRuntime:
    def __init__(self) -> None:
        self.calls = 0

    async def dispose(self) -> None:
        self.calls += 1
        raise RuntimeError("cleanup failed")


async def test_runtime_execution_replays_one_failed_dispose():
    runtime = FailingDisposeRuntime()
    execution = AgentRuntimeExecution(runtime, "session-1")

    for _ in range(2):
        with pytest.raises(RuntimeError, match="cleanup failed"):
            await execution.dispose()

    assert runtime.calls == 1


class GatedDisposeRuntime:
    def __init__(self) -> None:
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def dispose(self) -> None:
        self.calls += 1
        self.entered.set()
        await self.release.wait()


async def test_runtime_execution_dispose_absorbs_repeated_cancellation():
    runtime = GatedDisposeRuntime()
    execution = AgentRuntimeExecution(runtime, "session-1")
    disposing = asyncio.create_task(execution.dispose())
    await runtime.entered.wait()

    for _ in range(3):
        disposing.cancel()
        await asyncio.sleep(0)
        assert not disposing.done()

    runtime.release.set()
    with pytest.raises(asyncio.CancelledError):
        await disposing
    await execution.dispose()
    assert runtime.calls == 1


def test_agent_spec_workspace_is_never_treated_as_a_local_path():
    """No example name or host path may leak into the control plane."""

    import traceh.supervision.supervisor as module

    text = open(module.__file__, encoding="utf-8").read()
    for forbidden in ("Path(", "coder", "reviewer", "python-quality", "/tmp", "C:\\\\"):
        assert forbidden not in text, forbidden
    assert isinstance(AgentSpec(preset="p", workspace_id="w").workspace_id, str)
