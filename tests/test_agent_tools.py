"""Stage E: model-visible subagent tools stay inside Supervisor contracts."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from supervision_fixtures import (
    CHILD_POLICY,
    SPEC,
    GatedProvider,
    RuntimeFactory,
    replace_envelope_field,
    scripted,
)

from traceh.agents import AGENT_DIRECTORY_STREAM
from traceh.api.agents import AgentMessage, AgentSpec, MessageTarget
from traceh.api.llm import ModelResponse, ToolCall
from traceh.api.tools import ToolExecutionContext
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.session.event_store import InMemoryEventStore
from traceh.session.service import SessionService
from traceh.supervision import (
    AgentMessageNotFoundError,
    AgentMessageNotSettledError,
    AgentNotActiveError,
    AgentRunEvidenceError,
    AgentRunReportReader,
    AgentRuntimeExecution,
    AgentToolAuthorizationError,
    AgentToolBindingError,
    DeliveryInputError,
    ProcessAgentSupervisor,
    SupervisorToolset,
)


class SequencedGatedProvider:
    """Holds successive messages at independent deterministic gates."""

    name = "scripted"

    def __init__(self, count: int = 2) -> None:
        self.entered = tuple(asyncio.Event() for _ in range(count))
        self.release = tuple(asyncio.Event() for _ in range(count))
        self.calls = 0

    async def complete(self, request):
        index = self.calls
        self.calls += 1
        self.entered[index].set()
        await self.release[index].wait()
        return ModelResponse(content=f"sequenced answer {index}")


class RetryGatedSupervisor(ProcessAgentSupervisor):
    """Lets a test cancel an idempotent retry after it entered create()."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.create_calls = 0
        self.retry_entered = asyncio.Event()
        self.retry_release = asyncio.Event()

    async def create(self, spec, *, request_id, agent_id=None, session_id=None):
        self.create_calls += 1
        if self.create_calls == 3:  # owner, first child, then child retry
            self.retry_entered.set()
            await self.retry_release.wait()
        return await super().create(
            spec,
            request_id=request_id,
            agent_id=agent_id,
            session_id=session_id,
        )


class CreateJoinObservingSupervisor(ProcessAgentSupervisor):
    """Signals after two callers registered on one shared create Task."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.joined = 0
        self.two_joined = asyncio.Event()

    def reset_create_observation(self) -> None:
        self.joined = 0
        self.two_joined = asyncio.Event()

    async def _await_shared(self, task):
        self.joined += 1
        if self.joined == 2:
            self.two_joined.set()
        return await super()._await_shared(task)


class CompensationGateSupervisor(ProcessAgentSupervisor):
    """Holds one cancelled-create disposal while a stable retry arrives."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.gate_next_dispose = False
        self.compensation_entered = asyncio.Event()
        self.compensation_release = asyncio.Event()
        self.close_body_returned = asyncio.Event()

    async def _compensate_unreturned_create(self, request_id, pending, agent_id):
        if self.gate_next_dispose:
            self.gate_next_dispose = False
            self.compensation_entered.set()
            await self.compensation_release.wait()
        await super()._compensate_unreturned_create(request_id, pending, agent_id)

    async def wait_close_resources_done(self) -> None:
        await self._close_resources_done.wait()

    async def _close(self) -> None:
        try:
            await super()._close()
        finally:
            self.close_body_returned.set()


class StaleDirectoryReadStore(InMemoryEventStore):
    """Returns one earlier valid Directory snapshot after a later write."""

    def __init__(self) -> None:
        super().__init__()
        self.capture_next_directory_read = False
        self.snapshot_captured = asyncio.Event()
        self.snapshot_release = asyncio.Event()

    async def read(self, stream_id, *, from_seq=1):
        events = await super().read(stream_id, from_seq=from_seq)
        if (
            stream_id == AGENT_DIRECTORY_STREAM
            and self.capture_next_directory_read
        ):
            self.capture_next_directory_read = False
            self.snapshot_captured.set()
            await self.snapshot_release.wait()
        return events


class SharedResultGateSupervisor(ProcessAgentSupervisor):
    """Holds one completed shared-create result before delivery accounting."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.gate_next_result = False
        self.result_entered = asyncio.Event()
        self.result_release = asyncio.Event()

    async def _await_shared(self, task):
        result = await super()._await_shared(task)
        if self.gate_next_result:
            self.gate_next_result = False
            self.result_entered.set()
            await self.result_release.wait()
        return result


class CreateCallSnapshot(dict):
    """Signals when close snapshots the operation-level create registry."""

    def __init__(self) -> None:
        super().__init__()
        self.taken = asyncio.Event()

    def items(self):
        self.taken.set()
        return super().items()


class CreateCompletionGateSupervisor(ProcessAgentSupervisor):
    """Holds the post-return create receipt before registry removal."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.completion_entered = asyncio.Event()
        self.completion_release = asyncio.Event()

    async def _complete_create_call(self, call_token, state) -> None:
        self.completion_entered.set()
        await self.completion_release.wait()
        await super()._complete_create_call(call_token, state)


class PollingGateSupervisor(ProcessAgentSupervisor):
    """Holds the durable re-read after a local message waiter is registered."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.poll_entered = asyncio.Event()
        self.poll_release = asyncio.Event()

    async def _wait_for_message_progress(self, waiter):
        self.poll_entered.set()
        await self.poll_release.wait()
        await super()._wait_for_message_progress(waiter)


class HostileSessionSeqStore(InMemoryEventStore):
    """Returns one public Session envelope with an unorderable seq."""

    def __init__(self) -> None:
        super().__init__()
        self.poison_session_reads = False

    async def read(self, stream_id, *, from_seq=1):
        events = await super().read(stream_id, from_seq=from_seq)
        if not self.poison_session_reads or not stream_id.startswith("session:"):
            return events
        return tuple(
            replace_envelope_field(event, "seq", object())
            if event.type == "assistant/message"
            else event
            for event in events
        )


class CleanupFailingExecution:
    """A child execution whose owned cleanup converges with one failure."""

    def __init__(self, store, session_id, entered, release) -> None:
        self._store = store
        self._session_id = session_id
        self._entered = entered
        self._release = release
        self.dispose_calls = 0

    @property
    def session_id(self):
        return self._session_id

    @property
    def event_store(self):
        return self._store

    async def run_turn(self, turn_input):  # pragma: no cover - child gets no message
        raise AssertionError("the cleanup fixture child must not run a Turn")

    async def cancel_turn(self, *, reason):
        return False

    async def dispose(self):
        self.dispose_calls += 1
        self._entered.set()
        await self._release.wait()
        raise RuntimeError("fixture child cleanup failed")


class SpawnCleanupFailureFactory:
    """Runs a real parent Tool call and gives its child a failing cleanup."""

    def __init__(self, store, root: Path) -> None:
        self.store = store
        self.root = root
        self.supervisor: ProcessAgentSupervisor | None = None
        self.child_provision_entered = asyncio.Event()
        self.child_provision_release = asyncio.Event()
        self.cleanup_entered = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.child_execution: CleanupFailingExecution | None = None

    def _parent_runtime(self, agent_id: str):
        assert self.supervisor is not None
        provider = ScriptedLlmProvider(
            (
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "spawn-with-failing-cleanup",
                            "spawn_agent",
                            {
                                "preset": "cleanup-child-preset",
                                "workspace_id": "cleanup-child-workspace",
                            },
                        ),
                    )
                ),
            )
        )
        tools = SupervisorToolset(
            supervisor=self.supervisor,
            owner_agent_id=agent_id,
            event_store=self.store,
            provisioning_policy=CHILD_POLICY,
        ).tools
        return build_default_runtime(
            RuntimeConfig(
                data_dir=self.root / "data",
                provider="scripted",
                model="cleanup-parent-model",
            ),
            provider=provider,
            event_store=self.store,
            additional_tools=tools,
        )

    async def provision(self, spec, *, agent_id, session_id):
        workspace = self.root / spec.workspace_id
        workspace.mkdir(parents=True, exist_ok=True)
        if spec.preset == "cleanup-parent-preset":
            runtime = self._parent_runtime(agent_id)
            created = await runtime.create_session(workspace, session_id=session_id)
            return AgentRuntimeExecution(runtime, created)

        created = await SessionService(self.store).create_session(
            workspace, session_id=session_id
        )
        self.child_provision_entered.set()
        await self.child_provision_release.wait()
        self.child_execution = CleanupFailingExecution(
            self.store,
            created,
            self.cleanup_entered,
            self.cleanup_release,
        )
        return self.child_execution

    async def activate(self, record):  # pragma: no cover - not used by this scenario
        raise AssertionError("the cleanup fixture must not reactivate an Agent")


def failure_leaves(error: BaseException) -> tuple[BaseException, ...]:
    if isinstance(error, BaseExceptionGroup):
        return tuple(
            leaf
            for nested in error.exceptions
            for leaf in failure_leaves(nested)
        )
    return (error,)


def context(session_id: str, *, call_id: str = "tool-call") -> ToolExecutionContext:
    return ToolExecutionContext(
        session_id=session_id,
        turn_id="parent-turn",
        step_id="parent-step",
        tool_call_id=call_id,
        workspace=Path.cwd(),
        data_dir=Path.cwd(),
    )


def by_name(toolset: SupervisorToolset, name: str):
    return next(tool for tool in toolset.tools if tool.name == name)


@pytest.fixture
async def tool_world(tmp_path):
    store = InMemoryEventStore()
    factory = RuntimeFactory(store, tmp_path)
    supervisor = ProcessAgentSupervisor(store=store, factory=factory)
    owner = await supervisor.create(SPEC, request_id="owner-request")
    toolset = SupervisorToolset(
        supervisor=supervisor,
        owner_agent_id=owner.agent_id,
        event_store=store,
        provisioning_policy=CHILD_POLICY,
    )
    yield store, factory, supervisor, owner, toolset
    await supervisor.aclose()


async def test_toolset_exposes_exactly_the_stage_e_surface(tool_world):
    _store, _factory, _supervisor, _owner, toolset = tool_world
    assert tuple(tool.name for tool in toolset.tools) == (
        "spawn_agent",
        "send_agent_message",
        "wait_agent",
        "stop_agent",
        "collect_agent_artifact",
    )
    spawn = by_name(toolset, "spawn_agent")
    assert set(spawn.input_schema["properties"]) == {"preset", "workspace_id"}
    assert "owner_agent_id" not in spawn.input_schema["properties"]


async def test_tools_reject_a_runtime_on_a_different_durable_store(tool_world):
    _store, _factory, supervisor, owner, _toolset = tool_world
    with pytest.raises(AgentToolBindingError):
        SupervisorToolset(
            supervisor=supervisor,
            owner_agent_id=owner.agent_id,
            event_store=InMemoryEventStore(),
            provisioning_policy=CHILD_POLICY,
        )


async def test_spawn_binds_owner_and_is_idempotent_per_tool_call(tool_world):
    _store, _factory, supervisor, owner, toolset = tool_world
    spawn = by_name(toolset, "spawn_agent")
    execution_context = context(owner.session_id)
    first = await spawn.execute(
        {"preset": "analysis-preset", "workspace_id": "child-workspace"},
        execution_context,
    )
    second = await spawn.execute(
        {"preset": "analysis-preset", "workspace_id": "child-workspace"},
        execution_context,
    )

    assert first.data == second.data
    record = (await supervisor.registrar.directory()).get(str(first.data["agent_id"]))
    assert record is not None
    assert record.owner_agent_id == owner.agent_id
    assert record.session_id == first.data["session_id"]
    assert record.session_id != owner.session_id


async def test_bound_tools_reject_a_different_caller_session_without_writing(tool_world):
    _store, _factory, supervisor, owner, toolset = tool_world
    before = len((await supervisor.registrar.directory()).records)
    with pytest.raises(AgentToolBindingError):
        await by_name(toolset, "spawn_agent").execute(
            {"preset": "analysis-preset", "workspace_id": "child-workspace"},
            context("different-session"),
        )
    assert len((await supervisor.registrar.directory()).records) == before
    assert (await supervisor.registrar.directory()).get(owner.agent_id) is not None


async def test_send_wait_and_collect_join_the_durable_child_run(tool_world):
    store, _factory, _supervisor, owner, toolset = tool_world
    child = await by_name(toolset, "spawn_agent").execute(
        {"preset": "analysis-preset", "workspace_id": "child-workspace"},
        context(owner.session_id, call_id="spawn-child"),
    )
    child_id = str(child.data["agent_id"])
    sent = await by_name(toolset, "send_agent_message").execute(
        {"agent_id": child_id, "content": "Inspect the requested material."},
        context(owner.session_id, call_id="send-child"),
    )
    message_id = str(sent.data["message_id"])
    waited = await by_name(toolset, "wait_agent").execute(
        {"agent_id": child_id, "message_id": message_id},
        context(owner.session_id, call_id="wait-child"),
    )
    collected = await by_name(toolset, "collect_agent_artifact").execute(
        {"agent_id": child_id, "message_id": message_id},
        context(owner.session_id, call_id="collect-child"),
    )

    assert waited.data["status"] == "completed"
    assert waited.data["turn_id"]
    assert collected.content.startswith("answer ")
    assert collected.data["artifact_refs"] == []
    assert len(collected.data["evidence_refs"]) == 3
    assert collected.evidence == tuple(collected.data["evidence_refs"])
    replayed = await AgentRunReportReader(store).load(child_id, message_id)
    assert replayed.final_text == collected.content
    assert replayed.evidence_refs == collected.evidence


async def test_collect_refuses_an_accepted_but_unsettled_message(tmp_path):
    store = InMemoryEventStore()
    provider = GatedProvider()
    factory = RuntimeFactory(store, tmp_path, provider=provider)
    supervisor = ProcessAgentSupervisor(store=store, factory=factory)
    owner = await supervisor.create(SPEC, request_id="owner-request")
    tools = SupervisorToolset(
        supervisor=supervisor,
        owner_agent_id=owner.agent_id,
        event_store=store,
        provisioning_policy=CHILD_POLICY,
    )
    child = await by_name(tools, "spawn_agent").execute(
        {"preset": "analysis-preset", "workspace_id": "child-workspace"},
        context(owner.session_id, call_id="spawn-child"),
    )
    child_id = str(child.data["agent_id"])
    sent = await by_name(tools, "send_agent_message").execute(
        {"agent_id": child_id, "content": "Wait for the gate."},
        context(owner.session_id, call_id="send-child"),
    )
    await provider.entered.wait()

    with pytest.raises(AgentMessageNotSettledError):
        await by_name(tools, "collect_agent_artifact").execute(
            {"agent_id": child_id, "message_id": sent.data["message_id"]},
            context(owner.session_id, call_id="collect-child"),
        )
    provider.release.set()
    await supervisor.wait_idle(child_id)
    await supervisor.aclose()


async def test_cancelling_wait_does_not_cancel_the_child_turn(tmp_path):
    store = InMemoryEventStore()
    provider = GatedProvider()
    factory = RuntimeFactory(store, tmp_path, provider=provider)
    supervisor = ProcessAgentSupervisor(store=store, factory=factory)
    owner = await supervisor.create(SPEC, request_id="owner-request")
    tools = SupervisorToolset(
        supervisor=supervisor,
        owner_agent_id=owner.agent_id,
        event_store=store,
        provisioning_policy=CHILD_POLICY,
    )
    child = await by_name(tools, "spawn_agent").execute(
        {"preset": "analysis-preset", "workspace_id": "child-workspace"},
        context(owner.session_id, call_id="spawn-child"),
    )
    child_id = str(child.data["agent_id"])
    sent = await by_name(tools, "send_agent_message").execute(
        {"agent_id": child_id, "content": "Finish after release."},
        context(owner.session_id, call_id="send-child"),
    )
    await provider.entered.wait()
    waiting = asyncio.create_task(
        by_name(tools, "wait_agent").execute(
            {"agent_id": child_id, "message_id": sent.data["message_id"]},
            context(owner.session_id, call_id="wait-child"),
        )
    )
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    provider.release.set()
    report = await supervisor.wait_message(child_id, str(sent.data["message_id"]))
    assert report.status == "completed"
    await supervisor.aclose()


async def test_wait_for_one_message_is_not_blocked_by_a_later_message(tmp_path):
    store = InMemoryEventStore()
    provider = SequencedGatedProvider()
    factory = RuntimeFactory(store, tmp_path, provider=provider)
    supervisor = ProcessAgentSupervisor(store=store, factory=factory)
    child = await supervisor.create(SPEC, request_id="child-request")
    first = await supervisor.send(
        child.agent_id,
        AgentMessage("first-message", "first", "operator"),
        target=MessageTarget.NEW_TURN,
        wakeup=True,
    )
    await provider.entered[0].wait()
    await supervisor.send(
        child.agent_id,
        AgentMessage("second-message", "second", "operator"),
        target=MessageTarget.NEW_TURN,
        wakeup=True,
    )
    waiting = asyncio.create_task(
        supervisor.wait_message(child.agent_id, first.message_id)
    )

    provider.release[0].set()
    await provider.entered[1].wait()
    assert (await supervisor.report(child.agent_id, first.message_id)).status == "completed"
    report = await asyncio.wait_for(waiting, timeout=1)
    assert report.message_id == first.message_id
    assert report.status == "completed"

    provider.release[1].set()
    await supervisor.wait_idle(child.agent_id)
    await supervisor.aclose()


async def test_wait_observes_a_terminal_written_by_another_supervisor(tmp_path):
    store = InMemoryEventStore()
    first = PollingGateSupervisor(
        store=store,
        factory=RuntimeFactory(store, tmp_path / "first"),
    )
    child = await first.create(SPEC, request_id="child-request")
    await first.wait_idle(child.agent_id)
    receipt = await first.send(
        child.agent_id,
        AgentMessage("shared-message", "run elsewhere", "operator"),
        target=MessageTarget.NEW_TURN,
        wakeup=False,
    )
    waiting = asyncio.create_task(
        first.wait_message(child.agent_id, receipt.message_id)
    )
    await first.poll_entered.wait()

    provider = GatedProvider()
    second = ProcessAgentSupervisor(
        store=store,
        factory=RuntimeFactory(store, tmp_path / "second", provider=provider),
    )
    await second.resume(child.session_id)
    await provider.entered.wait()
    provider.release.set()
    await second.wait_idle(child.agent_id)

    durable = await first.report(child.agent_id, receipt.message_id)
    assert durable.status == "completed"
    assert not waiting.done(), "the local Activation did not write this terminal fact"
    first.poll_release.set()
    observed = await asyncio.wait_for(waiting, timeout=1)
    assert observed == durable

    await first.aclose()
    await second.aclose()


async def test_interrupted_child_has_a_durable_cancelled_report(tmp_path):
    store = InMemoryEventStore()
    provider = GatedProvider()
    factory = RuntimeFactory(store, tmp_path, provider=provider)
    supervisor = ProcessAgentSupervisor(store=store, factory=factory)
    owner = await supervisor.create(SPEC, request_id="owner-request")
    tools = SupervisorToolset(
        supervisor=supervisor,
        owner_agent_id=owner.agent_id,
        event_store=store,
        provisioning_policy=CHILD_POLICY,
    )
    child = await by_name(tools, "spawn_agent").execute(
        {"preset": "analysis-preset", "workspace_id": "child-workspace"},
        context(owner.session_id, call_id="spawn-child"),
    )
    child_id = str(child.data["agent_id"])
    sent = await by_name(tools, "send_agent_message").execute(
        {"agent_id": child_id, "content": "Remain active until interrupted."},
        context(owner.session_id, call_id="send-child"),
    )
    await provider.entered.wait()
    assert await supervisor.interrupt(child_id, "operator-stop") is True
    await supervisor.wait_idle(child_id)

    report = await supervisor.report(child_id, str(sent.data["message_id"]))
    assert (report.status, report.reason, report.final_text, report.turn_id) == (
        "cancelled",
        "turn-cancelled",
        "",
        None,
    )
    provider.release.set()
    await supervisor.aclose()


async def test_wait_message_uses_durable_terminal_after_activation_disposal(tool_world):
    _store, _factory, supervisor, owner, toolset = tool_world
    child = await by_name(toolset, "spawn_agent").execute(
        {"preset": "analysis-preset", "workspace_id": "child-workspace"},
        context(owner.session_id, call_id="spawn-child"),
    )
    child_id = str(child.data["agent_id"])
    sent = await by_name(toolset, "send_agent_message").execute(
        {"agent_id": child_id, "content": "Finish before disposal."},
        context(owner.session_id, call_id="send-child"),
    )
    await supervisor.wait_idle(child_id)
    await supervisor.dispose(child_id)

    report = await supervisor.wait_message(child_id, str(sent.data["message_id"]))
    assert report.status == "completed"


async def test_owner_cannot_operate_self_ancestor_or_sibling(tool_world):
    _store, _factory, supervisor, owner, toolset = tool_world
    sibling = await supervisor.create(
        AgentSpec(preset="peer-preset", workspace_id="peer-workspace"),
        request_id="sibling-request",
    )
    for target in (owner.agent_id, sibling.agent_id):
        with pytest.raises(AgentToolAuthorizationError):
            await by_name(toolset, "stop_agent").execute(
                {"agent_id": target},
                context(owner.session_id, call_id=f"stop-{target}"),
            )


async def test_owner_may_stop_a_grandchild_subtree(tool_world):
    store, _factory, supervisor, owner, owner_tools = tool_world
    child = await by_name(owner_tools, "spawn_agent").execute(
        {"preset": "middle-preset", "workspace_id": "middle-workspace"},
        context(owner.session_id, call_id="spawn-middle"),
    )
    child_id = str(child.data["agent_id"])
    child_tools = SupervisorToolset(
        supervisor=supervisor,
        owner_agent_id=child_id,
        event_store=store,
        provisioning_policy=CHILD_POLICY,
    )
    grandchild = await by_name(child_tools, "spawn_agent").execute(
        {"preset": "leaf-preset", "workspace_id": "leaf-workspace"},
        context(str(child.data["session_id"]), call_id="spawn-leaf"),
    )
    grandchild_id = str(grandchild.data["agent_id"])

    await by_name(owner_tools, "stop_agent").execute(
        {"agent_id": child_id},
        context(owner.session_id, call_id="stop-middle"),
    )
    with pytest.raises(AgentNotActiveError):
        await supervisor.wait_idle(child_id)
    with pytest.raises(AgentNotActiveError):
        await supervisor.wait_idle(grandchild_id)


async def test_cancelled_spawn_converges_and_disposes_a_committed_child(tool_world):
    _store, factory, supervisor, owner, toolset = tool_world
    factory.provision_gate = asyncio.Event()
    factory.provision_entered = asyncio.Event()
    spawning = asyncio.create_task(
        by_name(toolset, "spawn_agent").execute(
            {"preset": "analysis-preset", "workspace_id": "child-workspace"},
            context(owner.session_id, call_id="cancelled-spawn"),
        )
    )
    await factory.provision_entered.wait()
    spawning.cancel()
    await asyncio.sleep(0)
    assert not spawning.done()
    factory.provision_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await spawning

    # The concrete request id is an implementation detail. What matters is
    # that any child identity which committed is no longer active.
    children = (await supervisor.registrar.directory()).children_of(owner.agent_id)
    assert len(children) <= 1
    for child in children:
        with pytest.raises(AgentNotActiveError):
            await supervisor.wait_idle(child.agent_id)


async def test_cancelling_an_idempotent_spawn_retry_keeps_the_delivered_child(tmp_path):
    store = InMemoryEventStore()
    factory = RuntimeFactory(store, tmp_path)
    supervisor = RetryGatedSupervisor(store=store, factory=factory)
    owner = await supervisor.create(SPEC, request_id="owner-request")
    tools = SupervisorToolset(
        supervisor=supervisor,
        owner_agent_id=owner.agent_id,
        event_store=store,
        provisioning_policy=CHILD_POLICY,
    )
    spawn = by_name(tools, "spawn_agent")
    execution_context = context(owner.session_id, call_id="stable-spawn")
    first = await spawn.execute(
        {"preset": "analysis-preset", "workspace_id": "child-workspace"},
        execution_context,
    )

    retry = asyncio.create_task(
        spawn.execute(
            {"preset": "analysis-preset", "workspace_id": "child-workspace"},
            execution_context,
        )
    )
    await supervisor.retry_entered.wait()
    retry.cancel()
    with pytest.raises(asyncio.CancelledError):
        await retry
    supervisor.retry_release.set()

    await supervisor.wait_idle(str(first.data["agent_id"]))
    assert (await supervisor.registrar.directory()).get(
        str(first.data["agent_id"])
    ) is not None
    await supervisor.aclose()


async def test_cancelling_one_concurrent_first_spawn_keeps_the_delivered_child(
    tmp_path,
):
    store = InMemoryEventStore()
    factory = RuntimeFactory(store, tmp_path)
    supervisor = CreateJoinObservingSupervisor(store=store, factory=factory)
    owner = await supervisor.create(SPEC, request_id="owner-request")
    supervisor.reset_create_observation()
    factory.provision_gate = asyncio.Event()
    factory.provision_entered = asyncio.Event()
    tools = SupervisorToolset(
        supervisor=supervisor,
        owner_agent_id=owner.agent_id,
        event_store=store,
        provisioning_policy=CHILD_POLICY,
    )
    spawn = by_name(tools, "spawn_agent")
    execution_context = context(owner.session_id, call_id="concurrent-spawn")
    arguments = {"preset": "analysis-preset", "workspace_id": "child-workspace"}
    successful = asyncio.create_task(spawn.execute(arguments, execution_context))
    cancelled = asyncio.create_task(spawn.execute(arguments, execution_context))
    await factory.provision_entered.wait()
    await supervisor.two_joined.wait()

    cancelled.cancel()
    # Deliver the already-requested cancellation while both callers still own
    # waiter receipts for the same shared create Task.
    await asyncio.sleep(0)
    factory.provision_gate.set()
    delivered = await successful
    with pytest.raises(asyncio.CancelledError):
        await cancelled

    child_id = str(delivered.data["agent_id"])
    await supervisor.wait_idle(child_id)
    assert (await supervisor.registrar.directory()).get(child_id) is not None
    await supervisor.aclose()


async def test_stale_pre_admission_snapshot_cannot_own_spawn_compensation(tmp_path):
    store = StaleDirectoryReadStore()
    factory = RuntimeFactory(store, tmp_path)
    supervisor = SharedResultGateSupervisor(store=store, factory=factory)
    owner = await supervisor.create(SPEC, request_id="owner-request")
    tools = SupervisorToolset(
        supervisor=supervisor,
        owner_agent_id=owner.agent_id,
        event_store=store,
        provisioning_policy=CHILD_POLICY,
    )
    spawn = by_name(tools, "spawn_agent")
    execution_context = context(owner.session_id, call_id="stale-read-spawn")
    arguments = {"preset": "analysis-preset", "workspace_id": "child-workspace"}

    store.capture_next_directory_read = True
    stale = asyncio.create_task(spawn.execute(arguments, execution_context))
    await store.snapshot_captured.wait()

    delivered = await spawn.execute(arguments, execution_context)
    supervisor.gate_next_result = True
    store.snapshot_release.set()
    await supervisor.result_entered.wait()
    stale.cancel()
    supervisor.result_release.set()
    with pytest.raises(asyncio.CancelledError):
        await stale

    child_id = str(delivered.data["agent_id"])
    await supervisor.wait_idle(child_id)
    assert (await supervisor.registrar.directory()).get(child_id) is not None
    await supervisor.aclose()


async def test_resume_retains_activation_before_cancelled_create_compensation(tmp_path):
    store = InMemoryEventStore()
    factory = RuntimeFactory(store, tmp_path)
    supervisor = SharedResultGateSupervisor(store=store, factory=factory)
    owner = await supervisor.create(SPEC, request_id="owner-request")
    child_session_id = "resumed-child-session"
    child_spec = AgentSpec(
        preset="child-preset",
        workspace_id="child-workspace",
        owner_agent_id=owner.agent_id,
    )

    supervisor.gate_next_result = True
    creating = asyncio.create_task(
        supervisor.create(
            child_spec,
            request_id="resumed-child-request",
            session_id=child_session_id,
        )
    )
    await supervisor.result_entered.wait()

    resumed = await supervisor.resume(child_session_id)
    creating.cancel()
    supervisor.result_release.set()
    with pytest.raises(asyncio.CancelledError):
        await creating

    await supervisor.wait_idle(resumed.agent_id)
    assert (await supervisor.registrar.directory()).get(resumed.agent_id) is not None
    await supervisor.aclose()


async def test_wakeup_retains_activation_before_cancelled_create_compensation(tmp_path):
    store = InMemoryEventStore()
    factory = RuntimeFactory(store, tmp_path)
    supervisor = SharedResultGateSupervisor(store=store, factory=factory)
    owner = await supervisor.create(SPEC, request_id="owner-request")
    child_agent_id = "woken-child-agent"
    child_spec = AgentSpec(
        preset="child-preset",
        workspace_id="child-workspace",
        owner_agent_id=owner.agent_id,
    )

    supervisor.gate_next_result = True
    creating = asyncio.create_task(
        supervisor.create(
            child_spec,
            request_id="woken-child-request",
            agent_id=child_agent_id,
            session_id="woken-child-session",
        )
    )
    await supervisor.result_entered.wait()

    receipt = await supervisor.send(
        child_agent_id,
        AgentMessage("woken-child-message", "inspect the task", owner.agent_id),
        target=MessageTarget.NEW_TURN,
        wakeup=True,
    )
    creating.cancel()
    supervisor.result_release.set()
    with pytest.raises(asyncio.CancelledError):
        await creating

    report = await supervisor.wait_message(child_agent_id, receipt.message_id)
    assert report.status == "completed"
    await supervisor.aclose()


async def test_spawn_retry_waits_for_selected_compensation_before_delivery(tmp_path):
    store = InMemoryEventStore()
    factory = RuntimeFactory(store, tmp_path)
    supervisor = CompensationGateSupervisor(store=store, factory=factory)
    owner = await supervisor.create(SPEC, request_id="owner-request")
    factory.provision_gate = asyncio.Event()
    factory.provision_entered = asyncio.Event()
    supervisor.gate_next_dispose = True
    tools = SupervisorToolset(
        supervisor=supervisor,
        owner_agent_id=owner.agent_id,
        event_store=store,
        provisioning_policy=CHILD_POLICY,
    )
    spawn = by_name(tools, "spawn_agent")
    execution_context = context(owner.session_id, call_id="compensated-spawn")
    arguments = {"preset": "analysis-preset", "workspace_id": "child-workspace"}

    cancelled = asyncio.create_task(spawn.execute(arguments, execution_context))
    await factory.provision_entered.wait()
    cancelled.cancel()
    await asyncio.sleep(0)
    factory.provision_gate.set()
    await supervisor.compensation_entered.wait()

    # The retry arrives after compensation ownership was selected but before
    # disposal began. It must wait outside lifecycle admission, then rebuild
    # the durable Agent instead of receiving the soon-to-be-disposed handle.
    retry = asyncio.create_task(spawn.execute(arguments, execution_context))
    await asyncio.sleep(0)
    supervisor.compensation_release.set()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    delivered = await retry

    child_id = str(delivered.data["agent_id"])
    await supervisor.wait_idle(child_id)
    assert (await supervisor.registrar.directory()).get(child_id) is not None
    await supervisor.aclose()


async def test_aclose_waits_for_post_admission_create_compensation(tmp_path):
    store = InMemoryEventStore()
    factory = RuntimeFactory(store, tmp_path)
    supervisor = CompensationGateSupervisor(store=store, factory=factory)
    owner = await supervisor.create(SPEC, request_id="owner-request")
    factory.provision_gate = asyncio.Event()
    factory.provision_entered = asyncio.Event()
    supervisor.gate_next_dispose = True
    child_spec = AgentSpec(
        preset="child-preset",
        workspace_id="child-workspace",
        owner_agent_id=owner.agent_id,
    )

    creating = asyncio.create_task(
        supervisor.create(child_spec, request_id="closing-child-request")
    )
    await factory.provision_entered.wait()
    creating.cancel()
    await asyncio.sleep(0)
    factory.provision_gate.set()
    await supervisor.compensation_entered.wait()

    closing = asyncio.create_task(supervisor.aclose())
    await supervisor.wait_close_resources_done()
    assert not creating.done()
    assert not supervisor.close_body_returned.is_set()
    assert not closing.done()

    supervisor.compensation_release.set()
    with pytest.raises(asyncio.CancelledError):
        await creating
    await closing


async def test_aclose_joins_create_operation_not_unrelated_caller_work(tmp_path):
    store = InMemoryEventStore()
    factory = RuntimeFactory(store, tmp_path)
    supervisor = SharedResultGateSupervisor(store=store, factory=factory)
    owner = await supervisor.create(SPEC, request_id="owner-request")
    calls = CreateCallSnapshot()
    supervisor._create_calls = calls
    create_returned = asyncio.Event()
    caller_release = asyncio.Event()
    child_spec = AgentSpec(
        preset="child-preset",
        workspace_id="child-workspace",
        owner_agent_id=owner.agent_id,
    )

    async def caller():
        handle = await supervisor.create(
            child_spec,
            request_id="operation-receipt-request",
        )
        create_returned.set()
        await caller_release.wait()
        return handle

    supervisor.gate_next_result = True
    caller_task = asyncio.create_task(caller())
    await supervisor.result_entered.wait()
    closing = asyncio.create_task(supervisor.aclose())
    await calls.taken.wait()
    supervisor.result_release.set()

    await create_returned.wait()
    await asyncio.wait_for(closing, timeout=1.0)
    assert not caller_task.done()

    caller_release.set()
    await caller_task


async def test_create_registration_survives_until_return_receipt_is_published(
    tmp_path,
):
    store = InMemoryEventStore()
    factory = RuntimeFactory(store, tmp_path)
    supervisor = CreateCompletionGateSupervisor(store=store, factory=factory)
    calls = CreateCallSnapshot()
    supervisor._create_calls = calls
    method_returned = asyncio.Event()

    async def caller():
        handle = await supervisor.create(SPEC, request_id="return-receipt-request")
        method_returned.set()
        return handle

    creating = asyncio.create_task(caller())
    await supervisor.completion_entered.wait()
    try:
        assert method_returned.is_set()
        assert creating.done()
    except BaseException:
        # A reverse-verification failure must not strand fixture cleanup.
        supervisor.completion_release.set()
        await asyncio.gather(creating, return_exceptions=True)
        raise

    handle = await creating
    closing = asyncio.create_task(supervisor.aclose())
    await calls.taken.wait()
    await supervisor._close_resources_done.wait()
    assert not closing.done()

    supervisor.completion_release.set()
    await closing
    with pytest.raises(AgentNotActiveError):
        await supervisor.wait_idle(handle.agent_id)


async def test_early_create_failure_ends_caller_cancellation_permission(tmp_path):
    store = InMemoryEventStore()
    factory = RuntimeFactory(store, tmp_path)
    supervisor = CreateCompletionGateSupervisor(store=store, factory=factory)
    calls = CreateCallSnapshot()
    supervisor._create_calls = calls
    create_returned = asyncio.Event()
    caller_release = asyncio.Event()

    async def caller():
        with pytest.raises(DeliveryInputError):
            await supervisor.create(SPEC, request_id="")
        create_returned.set()
        await caller_release.wait()

    caller_task = asyncio.create_task(caller())
    await supervisor.completion_entered.wait()
    assert create_returned.is_set()

    closing = asyncio.create_task(supervisor.aclose())
    try:
        await calls.taken.wait()
        await supervisor._close_resources_done.wait()
        assert not caller_task.done()
        assert not closing.done()

        supervisor.completion_release.set()
        await closing
        assert not caller_task.done()
    finally:
        supervisor.completion_release.set()
        caller_release.set()
        await asyncio.gather(caller_task, closing, return_exceptions=True)


@pytest.mark.parametrize("repeat_cancel", [False, True])
async def test_spawn_cleanup_failure_preserves_parent_cancellation_and_terminal(
    tmp_path, repeat_cancel
):
    store = InMemoryEventStore()
    factory = SpawnCleanupFailureFactory(store, tmp_path)
    supervisor = ProcessAgentSupervisor(store=store, factory=factory)
    factory.supervisor = supervisor
    parent = await supervisor.create(
        AgentSpec(
            preset="cleanup-parent-preset",
            workspace_id="cleanup-parent-workspace",
        ),
        request_id="cleanup-parent-request",
    )
    receipt = await supervisor.send(
        parent.agent_id,
        AgentMessage("cleanup-parent-message", "create the child", "operator"),
        target=MessageTarget.NEW_TURN,
        wakeup=True,
    )
    await factory.child_provision_entered.wait()

    first_interrupt = asyncio.create_task(
        supervisor.interrupt(parent.agent_id, "cancel-spawn")
    )
    # Deliver the cancellation already requested above so the shielded create
    # converges and reaches the deterministic child cleanup gate.
    await asyncio.sleep(0)
    factory.child_provision_release.set()
    await factory.cleanup_entered.wait()
    second_interrupt = None
    if repeat_cancel:
        second_interrupt = asyncio.create_task(
            supervisor.interrupt(parent.agent_id, "cancel-spawn-again")
        )
        await asyncio.sleep(0)
    factory.cleanup_release.set()

    assert await first_interrupt is True
    if second_interrupt is not None:
        assert await second_interrupt is True
    report = await asyncio.wait_for(
        supervisor.wait_message(parent.agent_id, receipt.message_id), timeout=1
    )
    assert (report.status, report.reason) == ("cancelled", "turn-cancelled")
    assert factory.child_execution is not None
    assert factory.child_execution.dispose_calls == 1

    with pytest.raises(BaseExceptionGroup) as closing:
        await supervisor.aclose()
    assert any(
        isinstance(error, RuntimeError)
        and str(error) == "fixture child cleanup failed"
        for error in failure_leaves(closing.value)
    )


async def test_report_rejects_a_duplicate_durable_turn_end(tool_world):
    store, _factory, supervisor, owner, toolset = tool_world
    child = await by_name(toolset, "spawn_agent").execute(
        {"preset": "analysis-preset", "workspace_id": "child-workspace"},
        context(owner.session_id, call_id="spawn-child"),
    )
    child_id = str(child.data["agent_id"])
    sent = await by_name(toolset, "send_agent_message").execute(
        {"agent_id": child_id, "content": "Produce a result."},
        context(owner.session_id, call_id="send-child"),
    )
    report = await supervisor.wait_message(child_id, str(sent.data["message_id"]))
    await SessionService(store).append_session(
        report.session_id,
        "turn/end",
        {"turn_id": report.turn_id, "reason": report.reason},
    )
    with pytest.raises(AgentRunEvidenceError):
        await supervisor.report(child_id, str(sent.data["message_id"]))


async def test_report_normalizes_a_hostile_session_sequence(tmp_path):
    store = HostileSessionSeqStore()
    factory = RuntimeFactory(store, tmp_path)
    supervisor = ProcessAgentSupervisor(store=store, factory=factory)
    child = await supervisor.create(SPEC, request_id="child-request")
    receipt = await supervisor.send(
        child.agent_id,
        AgentMessage("child-message", "produce a result", "operator"),
        target=MessageTarget.NEW_TURN,
        wakeup=True,
    )
    await supervisor.wait_idle(child.agent_id)
    store.poison_session_reads = True

    with pytest.raises(AgentRunEvidenceError) as raised:
        await supervisor.report(child.agent_id, receipt.message_id)
    assert raised.value.code == "run-event-invalid"

    store.poison_session_reads = False
    await supervisor.aclose()


def test_message_report_errors_have_distinct_stable_codes():
    assert AgentMessageNotFoundError.code == "agent-message-not-found"
    assert AgentMessageNotSettledError.code == "agent-message-not-settled"


class ToolAwareFactory:
    """Host policy that injects the bound Stage E tools into every Agent."""

    def __init__(self, store, root: Path) -> None:
        self.store = store
        self.root = root
        self.supervisor: ProcessAgentSupervisor | None = None

    def _runtime(self, spec: AgentSpec, agent_id: str):
        assert self.supervisor is not None
        if spec.preset == "orchestration-preset":
            provider = ScriptedLlmProvider(
                (
                    ModelResponse(
                        tool_calls=(
                            ToolCall(
                                "spawn-from-model",
                                "spawn_agent",
                                {
                                    "preset": "worker-preset",
                                    "workspace_id": "worker-workspace",
                                },
                            ),
                        )
                    ),
                    ModelResponse(content="orchestration complete"),
                )
            )
        else:
            provider = scripted()
        tools = SupervisorToolset(
            supervisor=self.supervisor,
            owner_agent_id=agent_id,
            event_store=self.store,
            provisioning_policy=CHILD_POLICY,
        ).tools
        return build_default_runtime(
            RuntimeConfig(
                data_dir=self.root / "data",
                provider="scripted",
                model="tool-aware-model",
            ),
            provider=provider,
            event_store=self.store,
            additional_tools=tools,
        )

    async def provision(self, spec, *, agent_id, session_id):
        runtime = self._runtime(spec, agent_id)
        workspace = self.root / spec.workspace_id
        workspace.mkdir(parents=True, exist_ok=True)
        created = await runtime.create_session(workspace, session_id=session_id)
        return AgentRuntimeExecution(runtime, created)

    async def activate(self, record):
        spec = AgentSpec(
            preset=record.preset,
            workspace_id=record.workspace_id,
            owner_agent_id=record.owner_agent_id,
        )
        return AgentRuntimeExecution(self._runtime(spec, record.agent_id), record.session_id)


async def test_spawn_agent_runs_through_real_agent_loop_and_tool_runtime(tmp_path):
    store = InMemoryEventStore()
    factory = ToolAwareFactory(store, tmp_path)
    supervisor = ProcessAgentSupervisor(store=store, factory=factory)
    factory.supervisor = supervisor
    parent = await supervisor.create(
        AgentSpec(preset="orchestration-preset", workspace_id="parent-workspace"),
        request_id="parent-request",
    )
    await supervisor.send(
        parent.agent_id,
        AgentMessage("parent-message", "Create one worker.", "operator"),
        target=MessageTarget.NEW_TURN,
        wakeup=True,
    )
    await supervisor.wait_idle(parent.agent_id)

    children = (await supervisor.registrar.directory()).children_of(parent.agent_id)
    assert len(children) == 1
    assert children[0].session_id != parent.session_id
    events = await store.read(f"session:{parent.session_id}")
    result = next(
        event
        for event in events
        if event.type == "tool/result" and event.data.get("tool_name") == "spawn_agent"
    )
    assert result.data["status"] == "succeeded"
    await supervisor.aclose()
