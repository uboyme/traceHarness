"""v0.7-E recovery boundary, cancellation, close and dependency guards."""

from __future__ import annotations

import ast
import asyncio
import dataclasses
from pathlib import Path
from typing import get_type_hints

import pytest
from workflow_fixtures import RecordingResolver, build_assembly, definition

import traceh.plugins.manager as plugin_manager_module
import traceh.runtime.agent_loop as agent_loop_module
import traceh.runtime.agent_runtime as agent_runtime_module
import traceh.supervision.supervisor as supervisor_module
import traceh.supervision.tools as tools_module
import traceh.workflow.service as workflow_service_module
from traceh.api import workflow as workflow_api
from traceh.api.agents import AgentSupervisor
from traceh.api.workflow import (
    AgentTaskNode,
    JoinNode,
    MapNode,
    NodeStatus,
    WorkflowBindingResolver,
    WorkflowStatus,
)
from traceh.session.event_store import InMemoryEventStore
from traceh.workflow import (
    WorkflowRecoveryError,
    WorkflowService,
    WorkflowServices,
    WorkflowStateError,
    workflow_stream_id,
)

WORKFLOW_ROOT = Path(workflow_service_module.__file__).parent
EXECUTION_OWNERS = (
    agent_loop_module,
    agent_runtime_module,
    supervisor_module,
    plugin_manager_module,
)


def _sources(root: Path) -> tuple[tuple[Path, str], ...]:
    return tuple(
        (source, source.read_text(encoding="utf-8"))
        for source in sorted(root.glob("*.py"))
    )


def _imports(module) -> set[str]:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            result.add(node.module)
    return result


# ------------------------------------------------------------------ boundaries


def test_the_workflow_uses_narrow_public_seams() -> None:
    hints = get_type_hints(WorkflowService.__init__)
    assert hints["resolver"] is WorkflowBindingResolver
    assert get_type_hints(WorkflowServices)["supervisor"] is AgentSupervisor
    for name in (
        "AgentTaskNode",
        "MapNode",
        "JoinNode",
        "VerificationNode",
        "ApprovalNode",
        "NodeOutcome",
        "WorkflowDefinition",
        "WorkflowRun",
    ):
        value = getattr(workflow_api, name)
        assert dataclasses.is_dataclass(value), name
        assert value.__dataclass_params__.frozen, name
        assert getattr(value, "__slots__", None) is not None, name


def test_no_existing_owner_learns_about_the_workflow() -> None:
    for module in (*EXECUTION_OWNERS, tools_module):
        assert not any(
            name == "traceh.workflow" or name.startswith("traceh.workflow.")
            for name in _imports(module)
        ), module.__name__


def test_the_workflow_never_imports_private_execution_internals() -> None:
    forbidden = {
        "traceh.runtime.agent_loop",
        "traceh.runtime.agent_runtime",
        "traceh.runtime.composition_runtime",
        "traceh.supervision.supervisor",
        "traceh.plugins.manager",
        "traceh.evolution",
    }
    # `traceh.supervision.execution` also holds `durable_log_identity`, the one
    # shared store-identity resolver every peer domain (budgets, artifacts,
    # promotion) already uses. That single symbol is allowed; nothing else in
    # that module is.
    allowed_execution_symbols = {"durable_log_identity"}
    for source, text in _sources(WORKFLOW_ROOT):
        tree = ast.parse(text)
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert forbidden.isdisjoint(imported), source.name
        assert not any(name.startswith("traceh.cli") for name in imported), source.name
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "traceh.supervision.execution"
            ):
                names = {alias.name for alias in node.names}
                assert names <= allowed_execution_symbols, (source.name, names)


def test_the_workflow_touches_no_private_supervisor_state() -> None:
    """Reaching into an Activation table would be a second scheduler."""

    forbidden = (
        "_activations",
        "_pending_create",
        "_workers",
        "_inbox",
        "_delivery",
        "_activation",
    )
    for source, text in _sources(WORKFLOW_ROOT):
        for name in forbidden:
            assert f".{name}" not in text, (source.name, name)


def test_no_cli_or_later_stage_capability_arrived_early() -> None:
    package = Path(agent_runtime_module.__file__).parent.parent
    for source, text in _sources(package / "cli"):
        assert "traceh.workflow" not in text, source.name
        assert "WorkflowService" not in text, source.name
    assert not (package / "workflow" / "cli.py").exists()
    toolset = Path(tools_module.__file__).read_text(encoding="utf-8")
    for forbidden in ("workflow", "approve", "promote", "capture_patch"):
        assert forbidden not in toolset, forbidden


def test_no_test_fixture_identity_leaks_into_production_code() -> None:
    forbidden = (
        "coder-spec",
        "coder-message",
        "main-target",
        "demo-workflow",
        "trusted-source",
        "pytest",
        "tmp_path",
    )
    for source, text in _sources(WORKFLOW_ROOT):
        literals = {
            node.value
            for node in ast.walk(ast.parse(text))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        for value in forbidden:
            assert not any(value in literal for literal in literals), (
                source.name,
                value,
            )


def test_the_workflow_stream_is_the_only_new_fact_source() -> None:
    streams: set[str] = set()
    for _, text in _sources(WORKFLOW_ROOT):
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.startswith("workflow:"):
                    streams.add(node.value)
    assert streams == {"workflow:"} or not streams


# -------------------------------------------------------------------- recovery


async def test_a_run_stopped_at_the_approval_barrier_can_be_continued(
    tmp_path: Path,
) -> None:
    """The one allowed continuation point: a clean human barrier.

    A completely fresh service object, sharing only the durable store, must be
    able to pick the run up again.
    """

    from traceh.api.workflow import ApprovalNode, VerificationNode
    from traceh.promotion import expected_approval_digest

    assembly = build_assembly(tmp_path)
    nodes = definition(
        AgentTaskNode("build", (), "coder-spec", "coder-message", True),
        VerificationNode("review", ("build",), "build", "main-target"),
        ApprovalNode("sign", ("review",), "review"),
    )
    run = await assembly.workflow.start("run-1", nodes)
    assert run.status is WorkflowStatus.AWAITING_APPROVAL
    await assembly.workflow.aclose()

    ledger = await assembly.promotion.ledger()
    report = ledger.review(run.outcome("review").review_id)
    await assembly.promotion.approve(
        review_id=report.review_id,
        approval_digest=expected_approval_digest(report),
        approver_id="release-manager",
        operation_id="approve-1",
    )

    successor = WorkflowService(
        assembly.store,
        WorkflowServices(
            supervisor=assembly.supervisor,
            capture=assembly.capture,
            promotion=assembly.promotion,
        ),
        assembly.resolver,
    )
    resumed = await successor.resume("run-1", nodes)
    assert resumed.status is WorkflowStatus.COMPLETED
    await successor.aclose()
    await assembly.promotion.aclose()
    await assembly.capture.aclose()
    await assembly.supervisor.aclose()


async def test_a_run_interrupted_mid_node_refuses_to_continue(
    tmp_path: Path,
) -> None:
    """A started node with no terminal fact could have left anything behind."""

    from traceh.api.events import PendingEvent
    from traceh.api.workflow import WorkflowNodeKind
    from traceh.workflow.events import (
        WORKFLOW_NODE_STARTED,
        WORKFLOW_RUN_STARTED,
        WORKFLOW_SCHEMA_VERSION,
        node_started_data,
        run_started_data,
    )
    from traceh.workflow.models import workflow_definition_hash

    assembly = build_assembly(tmp_path)
    nodes = definition(AgentTaskNode("build", (), "coder-spec", "coder-message", False))
    stream = workflow_stream_id("run-1")
    await assembly.store.append(
        stream,
        expected_seq=0,
        events=(
            PendingEvent(
                type=WORKFLOW_RUN_STARTED,
                data=run_started_data(
                    run_id="run-1",
                    definition_id="demo-workflow",
                    definition_hash=workflow_definition_hash(nodes),
                ),
                schema_version=WORKFLOW_SCHEMA_VERSION,
            ),
            PendingEvent(
                type=WORKFLOW_NODE_STARTED,
                data=node_started_data(
                    node_id="build", kind=WorkflowNodeKind.AGENT_TASK, map_key=None
                ),
                schema_version=WORKFLOW_SCHEMA_VERSION,
            ),
        ),
    )
    with pytest.raises(WorkflowRecoveryError) as raised:
        await assembly.workflow.resume("run-1", nodes)
    assert raised.value.code == "workflow-node-still-running"
    assert raised.value.node_id == "build"
    await assembly.aclose()


async def test_a_changed_definition_cannot_take_over_a_run(tmp_path: Path) -> None:
    assembly = build_assembly(tmp_path)
    original = definition(
        AgentTaskNode("build", (), "coder-spec", "coder-message", False)
    )
    await assembly.workflow.start("run-1", original)

    changed = definition(
        AgentTaskNode("build", (), "coder-spec", "coder-message", True)
    )
    with pytest.raises(WorkflowStateError) as raised:
        await assembly.workflow.resume("run-1", changed)
    assert raised.value.code == "workflow-definition-changed"
    await assembly.aclose()


async def test_resuming_a_run_that_never_started_is_refused(tmp_path: Path) -> None:
    assembly = build_assembly(tmp_path)
    nodes = definition(AgentTaskNode("build", (), "coder-spec", "coder-message", False))
    with pytest.raises(WorkflowStateError) as raised:
        await assembly.workflow.resume("never", nodes)
    assert raised.value.code == "workflow-run-unknown"
    await assembly.aclose()


# ---------------------------------------------------- cancellation and close


class _GatedResolver(RecordingResolver):
    """Hold one node inside the resolver so a test can cancel deterministically."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def message_content(self, binding_id, *, run_id, node_id, map_key):
        self.entered.set()
        await self.release.wait()
        return await super().message_content(
            binding_id, run_id=run_id, node_id=node_id, map_key=map_key
        )


async def test_repeated_cancellation_waits_for_the_owned_run(tmp_path: Path) -> None:
    resolver = _GatedResolver()
    assembly = build_assembly(tmp_path, resolver=resolver)
    nodes = definition(AgentTaskNode("build", (), "coder-spec", "coder-message", False))

    running = asyncio.create_task(assembly.workflow.start("run-1", nodes))
    await resolver.entered.wait()
    running.cancel()
    running.cancel()
    running.cancel()
    await asyncio.sleep(0)
    assert not running.done()

    resolver.release.set()
    with pytest.raises(asyncio.CancelledError):
        await running

    # The owned run converged, so the node has a durable terminal either way.
    projection = await assembly.workflow.state("run-1", nodes)
    assert projection.outcome("build").status is NodeStatus.COMPLETED
    await assembly.aclose()


async def test_close_converges_in_flight_work_and_admits_no_more(
    tmp_path: Path,
) -> None:
    resolver = _GatedResolver()
    assembly = build_assembly(tmp_path, resolver=resolver)
    nodes = definition(AgentTaskNode("build", (), "coder-spec", "coder-message", False))

    running = asyncio.create_task(assembly.workflow.start("run-1", nodes))
    await resolver.entered.wait()
    closing = asyncio.create_task(assembly.workflow.aclose())
    await asyncio.sleep(0)
    assert not closing.done()

    resolver.release.set()
    await running
    await closing

    from traceh.workflow import WorkflowServiceClosedError

    with pytest.raises(WorkflowServiceClosedError):
        await assembly.workflow.start("run-2", nodes)
    await assembly.promotion.aclose()
    await assembly.capture.aclose()
    await assembly.supervisor.aclose()


async def test_every_independent_node_failure_is_reported(tmp_path: Path) -> None:
    """Two siblings failing with the *same* exception object are two failures."""

    shared = RuntimeError("both nodes hit this one object")

    class _FailingResolver(RecordingResolver):
        async def message_content(self, binding_id, *, run_id, node_id, map_key):
            raise shared

    resolver = _FailingResolver()
    resolver.keys["fan"] = ("alpha", "beta")
    assembly = build_assembly(tmp_path, resolver=resolver)
    nodes = definition(
        MapNode("fan", (), "fan", "coder-spec", "coder-message", 8, False),
        JoinNode("gather", ("fan",)),
    )
    with pytest.raises(BaseExceptionGroup) as raised:
        await assembly.workflow.start("run-1", nodes)
    assert len(raised.value.exceptions) == 2
    assert all(item is shared for item in raised.value.exceptions)

    projection = await assembly.workflow.state("run-1", nodes)
    failed = [
        outcome for outcome in projection.outcomes
        if outcome.status is NodeStatus.FAILED
    ]
    assert len(failed) == 2
    # The Join after a failed fan-out never starts.
    assert projection.outcome("gather") is None
    await assembly.aclose()


async def test_an_append_that_may_have_committed_is_not_assumed_absent(
    tmp_path: Path,
) -> None:
    class _CommitThenFailStore:
        def __init__(self) -> None:
            self.inner = InMemoryEventStore()
            self.fired = False

        async def append(self, stream_id, *, expected_seq, events, durability=None):
            result = (
                await self.inner.append(
                    stream_id, expected_seq=expected_seq, events=events
                )
                if durability is None
                else await self.inner.append(
                    stream_id,
                    expected_seq=expected_seq,
                    events=events,
                    durability=durability,
                )
            )
            if (
                stream_id.startswith("workflow:")
                and events[0].type == "workflow/node-completed"
                and not self.fired
            ):
                self.fired = True
                raise RuntimeError("append failed after committing")
            return result

        async def read(self, stream_id, *, from_seq=1):
            return await self.inner.read(stream_id, from_seq=from_seq)

        async def head(self, stream_id):
            return await self.inner.head(stream_id)

        async def list_streams(self, *, prefix=None):
            return await self.inner.list_streams(prefix=prefix)

    # The failing store is the assembly's one durable log, not a wrapper beside
    # it, so this exercises the real append path.
    assembly = build_assembly(tmp_path, store=_CommitThenFailStore())
    nodes = definition(AgentTaskNode("build", (), "coder-spec", "coder-message", False))
    # The store already recorded the fact, so the run must recognise it rather
    # than write a second one.
    run = await assembly.workflow.start("run-1", nodes)
    assert run.status is WorkflowStatus.COMPLETED
    assert assembly.store.fired is True
    events = await assembly.store.read(workflow_stream_id("run-1"))
    assert [event.type for event in events].count("workflow/node-completed") == 1
    await assembly.aclose()


async def test_an_approval_bound_to_a_different_artifact_fails_closed(
    tmp_path: Path,
) -> None:
    """Finding *an* approval is not enough; it must cover this exact Artifact."""

    from traceh.api.events import EventEnvelope, PendingEvent
    from traceh.api.workflow import ApprovalNode, VerificationNode, WorkflowNodeKind
    from traceh.promotion import expected_approval_digest
    from traceh.workflow import WorkflowProjection
    from traceh.workflow.errors import WorkflowNodeFailedError
    from traceh.workflow.events import (
        WORKFLOW_NODE_COMPLETED,
        WORKFLOW_NODE_STARTED,
        WORKFLOW_RUN_STARTED,
        WORKFLOW_SCHEMA_VERSION,
        node_completed_data,
        node_started_data,
        run_started_data,
    )
    from traceh.workflow.execution import NodeExecutor
    from traceh.workflow.models import workflow_definition_hash

    assembly = build_assembly(tmp_path)
    nodes = definition(
        AgentTaskNode("build", (), "coder-spec", "coder-message", True),
        VerificationNode("review", ("build",), "build", "main-target"),
        ApprovalNode("sign", ("review",), "review"),
    )
    run = await assembly.workflow.start("run-1", nodes)
    review_id = run.outcome("review").review_id
    ledger = await assembly.promotion.ledger()
    report = ledger.review(review_id)
    await assembly.promotion.approve(
        review_id=report.review_id,
        approval_digest=expected_approval_digest(report),
        approver_id="release-manager",
        operation_id="approve-1",
    )

    # A projection whose verification outcome names a different Artifact than
    # the durable Review actually covers.
    other = "run-2"
    stream = workflow_stream_id(other)

    def envelope(seq, event_type, data):
        return EventEnvelope.materialize(
            stream,
            seq,
            PendingEvent(
                type=event_type, data=data, schema_version=WORKFLOW_SCHEMA_VERSION
            ),
        )

    forged = WorkflowProjection.rebuild(
        other,
        (
            envelope(
                1,
                WORKFLOW_RUN_STARTED,
                run_started_data(
                    run_id=other,
                    definition_id="demo-workflow",
                    definition_hash=workflow_definition_hash(nodes),
                ),
            ),
            envelope(
                2,
                WORKFLOW_NODE_STARTED,
                node_started_data(
                    node_id="review",
                    kind=WorkflowNodeKind.VERIFICATION,
                    map_key=None,
                ),
            ),
            envelope(
                3,
                WORKFLOW_NODE_COMPLETED,
                node_completed_data(
                    node_id="review",
                    kind=WorkflowNodeKind.VERIFICATION,
                    map_key=None,
                    artifact_id="patch-" + "9" * 64,
                    review_id=review_id,
                ),
            ),
        ),
    )
    executor = NodeExecutor(
        run_id=other,
        services=WorkflowServices(
            supervisor=assembly.supervisor,
            capture=assembly.capture,
            promotion=assembly.promotion,
        ),
        resolver=assembly.resolver,
    )
    approval_node = next(node for node in nodes.nodes if node.node_id == "sign")
    with pytest.raises(WorkflowNodeFailedError) as raised:
        await executor.execute(approval_node, None, forged)
    assert raised.value.code == "workflow-approval-stale"
    await assembly.aclose()


# ------------------------------------------- one durable log, complete facts


async def test_services_writing_to_a_second_store_are_refused(
    tmp_path: Path,
) -> None:
    """Two logs cannot check each other, so they are never composed."""

    from traceh.workflow import WorkflowInputError

    assembly = build_assembly(tmp_path)
    with pytest.raises(WorkflowInputError) as raised:
        WorkflowService(
            InMemoryEventStore(),
            WorkflowServices(
                supervisor=assembly.supervisor,
                capture=assembly.capture,
                promotion=assembly.promotion,
            ),
            assembly.resolver,
        )
    assert raised.value.code == "workflow-event-store-mismatch"
    await assembly.aclose()


async def test_a_publishing_decorator_is_still_the_same_durable_log(
    tmp_path: Path,
) -> None:
    """The check resolves the one known transparent decorator, by type."""

    from traceh.session.event_feed import PublishingEventStore, SessionEventFeed

    assembly = build_assembly(tmp_path)
    service = WorkflowService(
        PublishingEventStore(assembly.store, SessionEventFeed()),
        WorkflowServices(supervisor=assembly.supervisor),
        assembly.resolver,
    )
    await service.aclose()
    await assembly.aclose()


async def test_a_foreign_message_on_this_identity_is_not_adopted(
    tmp_path: Path,
) -> None:
    """Someone else's durable message is not this node's work."""

    from traceh.api.agents import AgentMessage, AgentSpec, MessageTarget
    from traceh.workflow.models import agent_identity

    assembly = build_assembly(tmp_path)
    nodes = definition(AgentTaskNode("build", (), "coder-spec", "coder-message", False))
    agent_id, session_id, request_id, message_id = agent_identity("run-1", "build")

    # Occupy the derived identity with a different author's content.
    await assembly.supervisor.create(
        AgentSpec(preset="coder", workspace_id="workflow-intent"),
        request_id=request_id,
        agent_id=agent_id,
        session_id=session_id,
    )
    await assembly.supervisor.send(
        agent_id,
        AgentMessage(
            message_id=message_id,
            content="different durable content",
            source="other-writer",
        ),
        target=MessageTarget.NEW_TURN,
        wakeup=False,
    )
    await assembly.supervisor.dispose(agent_id)

    with pytest.raises(BaseExceptionGroup) as raised:
        await assembly.workflow.start("run-1", nodes)
    codes = {getattr(item, "code", None) for item in raised.value.exceptions}
    assert "workflow-message-conflict" in codes

    state = await assembly.workflow.state("run-1", nodes)
    assert state.outcome("build").status is NodeStatus.FAILED
    await assembly.aclose()


async def test_a_foreign_agent_on_this_identity_is_not_adopted(
    tmp_path: Path,
) -> None:
    from traceh.api.agents import AgentSpec
    from traceh.workflow.models import agent_identity

    assembly = build_assembly(tmp_path)
    nodes = definition(AgentTaskNode("build", (), "coder-spec", "coder-message", False))
    agent_id, session_id, request_id, _ = agent_identity("run-1", "build")

    # Same identity, a different durable create fact (another preset).
    await assembly.supervisor.create(
        AgentSpec(preset="reviewer", workspace_id="workflow-intent"),
        request_id=request_id,
        agent_id=agent_id,
        session_id=session_id,
    )
    await assembly.supervisor.dispose(agent_id)

    with pytest.raises(BaseExceptionGroup) as raised:
        await assembly.workflow.start("run-1", nodes)
    codes = {getattr(item, "code", None) for item in raised.value.exceptions}
    assert "workflow-agent-identity-conflict" in codes
    await assembly.aclose()


async def test_a_failed_run_records_its_terminal_before_reporting(
    tmp_path: Path,
) -> None:
    """A failed run must be durably over, not merely un-continuable."""

    class _FailingResolver(RecordingResolver):
        async def message_content(self, binding_id, *, run_id, node_id, map_key):
            raise RuntimeError("the host binding is broken")

    assembly = build_assembly(tmp_path, resolver=_FailingResolver())
    nodes = definition(AgentTaskNode("build", (), "coder-spec", "coder-message", False))
    with pytest.raises(BaseExceptionGroup):
        await assembly.workflow.start("run-1", nodes)

    state = await assembly.workflow.state("run-1", nodes)
    assert state.status is WorkflowStatus.FAILED
    events = [
        event.type for event in await assembly.store.read(workflow_stream_id("run-1"))
    ]
    assert events[-1] == "workflow/run-finished"

    # A later resume finds a finished run and cannot invent a continuation.
    again = await assembly.workflow.resume("run-1", nodes)
    assert again.status is WorkflowStatus.FAILED
    await assembly.aclose()


async def test_a_read_also_refuses_a_definition_the_run_never_agreed_to(
    tmp_path: Path,
) -> None:
    """state() interprets a run; it must not interpret it as something else."""

    assembly = build_assembly(tmp_path)
    original = definition(
        AgentTaskNode("build", (), "coder-spec", "coder-message", False)
    )
    await assembly.workflow.start("run-1", original)

    changed = definition(
        AgentTaskNode("build", (), "coder-spec", "coder-message", True)
    )
    with pytest.raises(WorkflowStateError) as raised:
        await assembly.workflow.state("run-1", changed)
    assert raised.value.code == "workflow-definition-changed"

    # A run that does not exist yet is readable with any definition.
    empty = await assembly.workflow.state("never", changed)
    assert empty.status is WorkflowStatus.RUNNING
    assert empty.head_seq == 0
    await assembly.aclose()


async def test_an_agent_with_different_grants_is_not_adopted(tmp_path: Path) -> None:
    """Capability grants decide what the adopted Agent may do."""

    from traceh.api.agents import AgentSpec
    from traceh.workflow.models import agent_identity

    assembly = build_assembly(tmp_path)
    nodes = definition(AgentTaskNode("build", (), "coder-spec", "coder-message", False))
    agent_id, session_id, request_id, _ = agent_identity("run-1", "build")

    await assembly.supervisor.create(
        AgentSpec(
            preset="coder",
            workspace_id="workflow-intent",
            capability_grants=("host-admin",),
        ),
        request_id=request_id,
        agent_id=agent_id,
        session_id=session_id,
    )
    await assembly.supervisor.dispose(agent_id)

    with pytest.raises(BaseExceptionGroup) as raised:
        await assembly.workflow.start("run-1", nodes)
    codes = {getattr(item, "code", None) for item in raised.value.exceptions}
    assert "workflow-agent-identity-conflict" in codes
    await assembly.aclose()


async def test_a_message_accepted_without_the_required_wakeup_is_not_adopted(
    tmp_path: Path,
) -> None:
    """Delivery semantics are part of the operation, not decoration."""

    from traceh.api.agents import AgentMessage, AgentSpec, MessageTarget
    from traceh.workflow.models import agent_identity

    assembly = build_assembly(tmp_path)
    nodes = definition(AgentTaskNode("build", (), "coder-spec", "coder-message", False))
    agent_id, session_id, request_id, message_id = agent_identity("run-1", "build")

    await assembly.supervisor.create(
        AgentSpec(preset="coder", workspace_id="workflow-intent"),
        request_id=request_id,
        agent_id=agent_id,
        session_id=session_id,
    )
    # Byte-identical text, but delivered without the wake-up this node requires.
    await assembly.supervisor.send(
        agent_id,
        AgentMessage(
            message_id=message_id,
            content="do the work",
            source="workflow:run-1",
        ),
        target=MessageTarget.NEW_TURN,
        wakeup=False,
    )
    await assembly.supervisor.dispose(agent_id)

    with pytest.raises(BaseExceptionGroup) as raised:
        await assembly.workflow.start("run-1", nodes)
    codes = {getattr(item, "code", None) for item in raised.value.exceptions}
    assert "workflow-message-conflict" in codes
    await assembly.aclose()


async def test_a_failed_terminal_write_never_hides_the_node_root_cause(
    tmp_path: Path,
) -> None:
    """Bookkeeping failure must not erase why the nodes failed."""

    root = RuntimeError("node root failure")

    class _FailingResolver(RecordingResolver):
        async def message_content(self, binding_id, *, run_id, node_id, map_key):
            raise root

    class _TerminalRefusingStore:
        def __init__(self) -> None:
            self.inner = InMemoryEventStore()

        async def append(self, stream_id, *, expected_seq, events, durability=None):
            if events[0].type == "workflow/run-finished":
                raise OSError("run terminal unavailable")
            kwargs = {} if durability is None else {"durability": durability}
            return await self.inner.append(
                stream_id, expected_seq=expected_seq, events=events, **kwargs
            )

        async def read(self, stream_id, *, from_seq=1):
            return await self.inner.read(stream_id, from_seq=from_seq)

        async def head(self, stream_id):
            return await self.inner.head(stream_id)

        async def list_streams(self, *, prefix=None):
            return await self.inner.list_streams(prefix=prefix)

    assembly = build_assembly(
        tmp_path, resolver=_FailingResolver(), store=_TerminalRefusingStore()
    )
    nodes = definition(AgentTaskNode("build", (), "coder-spec", "coder-message", False))
    with pytest.raises(BaseException) as raised:
        await assembly.workflow.start("run-1", nodes)

    def chain(error, seen=None):
        seen = set() if seen is None else seen
        if error is None or id(error) in seen:
            return
        seen.add(id(error))
        yield error
        yield from chain(error.__cause__, seen)
        yield from chain(error.__context__, seen)
        for nested in getattr(error, "exceptions", ()):
            yield from chain(nested, seen)

    found = list(chain(raised.value))
    assert any(item is root for item in found), found
    assert any(isinstance(item, OSError) for item in found), found
    await assembly.aclose()
