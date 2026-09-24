"""One main and one on-demand concurrent child through the real loop owners.

Dispatch, waiting, budget and cancellation keep their existing owners: the
Supervisor creates/sends and disposes, the collect tool owns the bounded wait,
and the continuation refuses delivery on an uncollected child report.
"""

import asyncio
import json

import pytest
from collaboration_fixtures import flat_main_work
from supervision_fixtures import SPEC, GatedProvider, RuntimeFactory, settle
from test_investigation_tools import WORK, BoundPolicy, context
from test_product_f3_e2e import _response

from traceh.agents import AgentDirectoryReader, AgentInboxReader
from traceh.api.agents import AgentSpec
from traceh.api.llm import ModelResponse, ToolCall
from traceh.api.workspaces import WorkspaceAccess, WorkspaceProvisioningRequest
from traceh.artifacts import LocalArtifactCas, PatchCaptureService
from traceh.artifacts.catalog import PatchArtifactCatalogReader
from traceh.budgets.service import BudgetLedgerService
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.product.collaboration import (
    CollaborationChildDeliveryEmpty,
    CollaborationChildIncomplete,
    CollaborationContinuation,
    CollaborationExecutionStopped,
    CollaborationPolicy,
)
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.session.event_store import InMemoryEventStore
from traceh.session.service import SessionService
from traceh.supervision import (
    AgentRunReportReader,
    AgentRuntimeExecution,
    ProcessAgentSupervisor,
)
from traceh.supervision.authority import AgentToolAuthority, AgentToolBindingError
from traceh.supervision.delegation import COLLECT_INVESTIGATION, InvestigationToolset
from traceh.supervision.errors import AgentMessageNotSettledError
from traceh.supervision.structured_collaboration import (
    DISPATCH_AND_CONTINUE,
    SUBMIT_COLLABORATION,
    CollaborationPlanTool,
    correctable_plan_result,
    investigator_role,
    patch_author_role,
)
from traceh.supervision.writable_collaboration import (
    COLLECT_CHILD_PATCH,
    ChildPatchCollectTool,
    WritableControl,
)
from traceh.supervision.writable_work import WritableBinding
from traceh.workspaces import (
    LocalGitWorkspaceProvider,
    WorkspaceManagedAgentSupervisor,
    WorkspaceService,
)

ASSIGNMENT = {
    "assignment_id": "explicit-test-investigation",
    "role": "investigator",
    **{k: v for k, v in WORK.items() if k != "main_work"},
}
PLAN_ARGUMENTS = {
    **flat_main_work(WORK["main_work"]),
    "children": [ASSIGNMENT],
    "handoff": DISPATCH_AND_CONTINUE,
}


class FailingChild:
    """A child whose Turn ends in a terminal failure rather than a delivery."""

    name = "scripted"

    async def complete(self, request):
        del request
        raise RuntimeError("child-cannot-continue")


class ConcurrentMain:
    """Scripted main agent: dispatch, keep working, then collect on demand."""

    name = "concurrent-main"

    def __init__(self, mode, *, supervisor, child_gate, parked):
        self.mode = mode
        self.supervisor = supervisor
        self.child_gate = child_gate
        self.parked = parked
        self.calls = 0
        self.dispatch = None
        self.handle = None
        self.receipt = None
        self.child_running = False
        self.collected = []
        self.execute_views = []
        self.unsettled_while_working = False
        self.calls_after_terminal = 0

    async def complete(self, request):
        self.calls += 1
        names = {t.name for t in request.tools}
        if SUBMIT_COLLABORATION in names:
            arguments = dict(PLAN_ARGUMENTS)
            if self.mode == "invalid-handoff":
                arguments["handoff"] = "background"
            return _response("", ToolCall(f"plan-{self.calls}", SUBMIT_COLLABORATION, arguments))
        self.execute_views.append(tuple(sorted(names)))
        if self.dispatch is None:
            self.dispatch = json.loads(
                next(m.content for m in request.messages if m.name == SUBMIT_COLLABORATION)
            )
            self.receipt = (self.dispatch["outcome"], self.dispatch["children"][0]["status"])
            self.handle = {
                key: self.dispatch["children"][0][key] for key in ("agent_id", "message_id")
            }
            # The child is really executing while this main request is served.
            self.child_running = self.child_gate.entered.is_set()
            try:
                await self.supervisor.report(*self.handle.values())
            except AgentMessageNotSettledError:
                self.unsettled_while_working = True
            if self.mode == "cancel":
                self.parked.set()
                await asyncio.Event().wait()
            return _response("", ToolCall("own-work", "list_files", {"path": "."}))
        collections = [m for m in request.messages if m.name == COLLECT_INVESTIGATION]
        if collections:
            self.collected = [json.loads(m.content) for m in collections]
        if self.mode == "uncollected":
            return _response("Delivering without the dispatched child report.")
        if not collections:
            return _response(
                "",
                ToolCall(
                    "collect-poll",
                    COLLECT_INVESTIGATION,
                    {**self.handle, "wait_seconds": 0},
                ),
            )
        if self.collected[-1]["status"] == "pending":
            if self.mode == "pending-only":
                return _response("Delivering on a pending child report.")
            self.child_gate.release.set()
            return _response(
                "",
                ToolCall(
                    "collect-report",
                    COLLECT_INVESTIGATION,
                    {**self.handle, "wait_seconds": 5},
                ),
            )
        if self.mode == "keeps-working-after-terminal":
            # Deliberately eager: it wants to keep investigating after the
            # terminal report. If the host lets it, the stop came too late.
            self.calls_after_terminal += 1
            return _response("", ToolCall(f"more-{self.calls}", "list_files", {"path": "."}))
        return _response("Used the collected child report and finished the retained work.")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    ["parallel", "uncollected", "pending-only", "invalid-handoff", "cancel", "empty-delivery"],
)
async def test_dispatch_and_continue_requires_a_collected_child_report(tmp_path, mode):
    store = InMemoryEventStore()
    child_gate = GatedProvider(
        # A child that finishes its Turn cleanly and says nothing. The lifecycle
        # is genuinely "completed"; the delivery is not.
        ScriptedLlmProvider((ModelResponse(content=""),), repeat_last=True)
        if mode == "empty-delivery"
        else None
    )
    factory = RuntimeFactory(store, tmp_path, provider=child_gate)
    supervisor = ProcessAgentSupervisor(store=store, factory=factory)
    owner = await supervisor.create(SPEC, request_id="owner")
    toolset = InvestigationToolset(
        supervisor=supervisor,
        owner_agent_id=owner.agent_id,
        event_store=store,
        policy=BoundPolicy(owner.agent_id),
    )
    parked = asyncio.Event()
    provider = ConcurrentMain(mode, supervisor=supervisor, child_gate=child_gate, parked=parked)
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "main", provider=provider.name, model="model", max_steps=12
        ),
        provider=provider,
        event_store=store,
        step_view=CollaborationPolicy(),
        continuation=CollaborationContinuation(store, owner.session_id),
        # The host assembles the delegation controls next to the plan tool, as the
        # multi Product profile does; the frozen Step view decides what is visible.
        additional_tools=(
            *toolset.tools,
            CollaborationPlanTool((investigator_role(toolset.control),)),
        ),
    )
    running = asyncio.create_task(runtime.run_existing(owner.session_id, "Investigate the target"))
    try:
        if mode == "parallel":
            await running
        elif mode == "cancel":
            await asyncio.wait_for(parked.wait(), 5)
            running.cancel()
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
        elif mode == "invalid-handoff":
            with pytest.raises(CollaborationExecutionStopped):
                await running
        elif mode == "empty-delivery":
            # Stopped on the first empty report, not carried into a delivery and
            # not retried: the main Agent cannot fix what the child did not say.
            with pytest.raises(CollaborationChildDeliveryEmpty) as refused:
                await running
            assert "collaboration-child-delivery-empty" in str(refused.value)
        else:
            with pytest.raises(CollaborationChildIncomplete) as refused:
                await running
            assert "collaboration-child-report-not-collected" in str(refused.value)

        children = [
            r
            for r in (await AgentDirectoryReader(store).load()).records
            if r.owner_agent_id == owner.agent_id
        ]
        assert len(children) == (0 if mode == "invalid-handoff" else 1)
        if mode == "invalid-handoff":
            results = [
                e.data
                for e in await SessionService(store).read_session(owner.session_id)
                if e.type == "tool/result" and e.data["tool_name"] == SUBMIT_COLLABORATION
            ]
            # An unknown mode is refused before dispatch and stays correctable.
            assert results and all(correctable_plan_result(r) for r in results)
            return
        assert provider.receipt == ("dispatched", "accepted")
        assert provider.child_running
        assert provider.unsettled_while_working
        # Every request after the accepted dispatch keeps the single bounded
        # collect surface and hides every other delegation control.
        assert provider.execute_views
        for names in provider.execute_views:
            assert COLLECT_INVESTIGATION in names
            assert SUBMIT_COLLABORATION not in names
            assert "delegate_investigation" not in names
        if mode == "parallel":
            assert [row["status"] for row in provider.collected] == ["pending", "completed"]
            assert provider.collected[-1]["statement"] == "answer 0"
            await _assert_collect_follows_dispatched_roles(store, owner.session_id)
        child = children[0]
        # The owned tree, not the returning dispatch call, converges the child. A
        # still-gated child is cancelled child-first; it never escapes the owner.
        await supervisor.dispose(owner.agent_id)
        accepted = next(iter(await AgentInboxReader(store).load(child.agent_id)))
        report = await AgentRunReportReader(store).load(
            child.agent_id, accepted.message.message_id
        )
        assert report.status == (
            "completed" if mode in ("parallel", "empty-delivery") else "cancelled"
        )
        assert not await runtime.check_invariants(owner.session_id)
    finally:
        child_gate.release.set()
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        await runtime.dispose()
        await supervisor.aclose()


async def _assert_collect_follows_dispatched_roles(store, session_id):
    """The view exposes the collect control of the roles the plan actually used."""
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class _Named:
        name: str

    @dataclass(frozen=True)
    class _Composition:
        tools: tuple
        system_prompt: str = "explicit test prompt"

    events = await SessionService(store).read_session(session_id)
    turn_id = next(e.data["turn_id"] for e in events if e.type == "step/start")
    execute = next(
        e
        for e in events
        if e.type == "request/view" and e.data["label"] == "collaboration-execute"
    )
    granted = (
        "apply_patch",
        COLLECT_CHILD_PATCH,
        COLLECT_INVESTIGATION,
        "delegate_investigation",
        SUBMIT_COLLABORATION,
    )
    view = await CollaborationPolicy()._select(
        [e for e in events if e.seq <= execute.seq],
        _Composition(tuple(_Named(name) for name in granted)),
        turn_id=turn_id,
        step_id=execute.data["step_id"],
    )
    # This plan dispatched an investigator, so only that collect control appears
    # even though the writable one is also granted.
    assert view.tool_names == ("apply_patch", COLLECT_INVESTIGATION)
    assert COLLECT_INVESTIGATION in view.system_prompt
    assert COLLECT_CHILD_PATCH not in view.system_prompt


class ManagedRuntimeFactory(RuntimeFactory):
    """Run each Agent in its real managed worktree, as the Product factory does."""

    def __init__(self, store, root, workspaces, *, provider=None):
        super().__init__(store, root, provider=provider)
        self.workspaces = workspaces

    async def provision(self, spec, *, agent_id, session_id):
        del agent_id
        handle = await self.workspaces.resolve_for_creation(spec.workspace_id)
        self.provisions += 1
        runtime = self._runtime()
        created = await runtime.create_session(handle.root, session_id=session_id)
        execution = AgentRuntimeExecution(runtime, created)
        self.executions.append(execution)
        return execution


class WritablePolicy:
    """Explicit test binding; production uses ProductWritablePolicy."""

    def __init__(self, owner_id, revision):
        self.owner_id = owner_id
        self.revision = revision

    def prepare(self, owner):
        if owner.agent_id != self.owner_id:
            raise AgentToolBindingError("unbound parent")
        return WritableBinding(
            AgentSpec(
                preset="patch-author",
                workspace_id="child-workspace",
                owner_agent_id=owner.agent_id,
            ),
            "explicit-test-task",
            "trusted-source",
            self.revision,
            "b" * 64,
            "c" * 64,
        )

    async def validate_child(self, owner, child):
        binding = self.prepare(owner)
        if child.preset != binding.spec.preset or child.owner_agent_id != owner.agent_id:
            raise AgentToolBindingError("unbound child")
        return binding


class WritableWorkspacePolicy:
    def workspace_for_agent(self, spec):
        del spec
        return WorkspaceProvisioningRequest(
            source_id="trusted-source", revision="main", access=WorkspaceAccess.WRITABLE
        )


def _source_repository(root):
    from promotion_fixtures import build_source_repository

    source, _ = build_source_repository(root)
    return source


@pytest.mark.asyncio
async def test_concurrent_patch_author_is_collected_on_demand(tmp_path):
    """The main polls, keeps working, and only then captures the real Patch."""
    store = InMemoryEventStore()
    source = _source_repository(tmp_path / "source")
    workspaces = WorkspaceService(
        store,
        LocalGitWorkspaceProvider(
            managed_root=tmp_path / "managed", sources={"trusted-source": source}
        ),
    )
    child_gate = GatedProvider()
    factory = ManagedRuntimeFactory(store, tmp_path, workspaces, provider=child_gate)
    supervisor = WorkspaceManagedAgentSupervisor(
        ProcessAgentSupervisor(store=store, factory=factory),
        workspaces,
        workspace_policy=WritableWorkspacePolicy(),
    )
    cas = LocalArtifactCas(tmp_path / "cas")
    capture = PatchCaptureService(supervisor, workspaces, cas, limits=_capture_limits())
    budgets = BudgetLedgerService(store)
    main = await supervisor.create(
        AgentSpec(preset="main", workspace_id="main-workspace"),
        request_id="main-create",
        agent_id="main-agent",
        session_id="main-session",
    )
    revision = (await workspaces.resolve_for_agent(main.agent_id)).base_revision
    control = WritableControl(
        supervisor=supervisor,
        authority=AgentToolAuthority(
            directory_reader=AgentDirectoryReader(store), owner_agent_id=main.agent_id
        ),
        policy=WritablePolicy(main.agent_id, revision),
        capture=capture,
        workspaces=workspaces,
        budgets=budgets,
    )
    collect = ChildPatchCollectTool(control)
    ctx = context(tmp_path, main.session_id)
    assignment = {
        **{k: v for k, v in ASSIGNMENT.items() if k not in ("assignment_id", "role")},
        "paths": ["candidate.py"],
        "main_work": WORK["main_work"],
    }
    try:
        dispatched = (await control.delegate(assignment, ctx)).data
        handle = {key: dispatched[key] for key in ("agent_id", "message_id")}
        await asyncio.wait_for(child_gate.entered.wait(), 5)

        pending = await collect.execute({**handle, "wait_seconds": 0}, ctx)
        assert pending.data["status"] == "pending" and pending.data["artifact"] is None
        assert not tuple(await PatchArtifactCatalogReader(store).load())

        # A cancelled wait is the caller's waiter only; the child keeps running.
        waiting = asyncio.create_task(collect.execute({**handle, "wait_seconds": 5}, ctx))
        await settle()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        child_workspace = await workspaces.resolve_for_agent(handle["agent_id"])
        (child_workspace.root / "candidate.py").write_text("VALUE = 1\n", encoding="utf-8")
        child_gate.release.set()

        report = await collect.execute({**handle, "wait_seconds": 5}, ctx)
        assert report.data["status"] == "completed" and report.data["reason"] == "completed"
        artifact = report.data["artifact"]
        assert artifact["changed_paths"] == ["candidate.py"]
        assert artifact["base_revision"] == revision
        assert artifact["agent_id"] == handle["agent_id"]
        stored = await capture.reader.load(artifact["artifact_id"])
        assert b"VALUE = 1" in stored.content

        repeated = await collect.execute({**handle, "wait_seconds": 0}, ctx)
        assert repeated.data == report.data
        assert len(tuple(await PatchArtifactCatalogReader(store).load())) == 1

        with pytest.raises(AgentToolBindingError):
            await collect.execute({**handle, "wait_seconds": 0}, context(tmp_path, "foreign"))
        for invalid in (-1, 31, "5"):
            with pytest.raises(ValueError):
                await collect.execute({**handle, "wait_seconds": invalid}, ctx)
    finally:
        child_gate.release.set()
        await capture.aclose()
        await supervisor.aclose()


class WritableMain:
    """Dispatch concurrently, work, collect, read the Patch, then integrate it."""

    name = "writable-main"

    def __init__(self, child_gate):
        self.child_gate = child_gate
        self.dispatch = None
        self.collected = []
        self.integrated = None

    async def complete(self, request):
        names = {t.name for t in request.tools}
        if SUBMIT_COLLABORATION in names:
            return _response(
                "",
                ToolCall(
                    "plan",
                    SUBMIT_COLLABORATION,
                    {
                        **flat_main_work(WORK["main_work"]),
                        "children": [{**ASSIGNMENT, "role": "patch_author",
                                      "paths": ["candidate.py"]}],
                        "handoff": DISPATCH_AND_CONTINUE,
                    },
                ),
            )
        self.dispatch = json.loads(
            next(m.content for m in request.messages if m.name == SUBMIT_COLLABORATION)
        )
        handle = {key: self.dispatch["children"][0][key] for key in ("agent_id", "message_id")}
        calls = {m.tool_call_id for m in request.messages}
        if "own-work" not in calls:
            return _response(
                "",
                ToolCall(
                    "own-work",
                    "apply_patch",
                    {"path": "added.txt", "old_text": "", "new_text": "added\n", "create": True},
                ),
            )
        self.collected = [
            json.loads(m.content) for m in request.messages if m.name == COLLECT_CHILD_PATCH
        ]
        if not self.collected:
            return _response(
                "", ToolCall("collect-poll", COLLECT_CHILD_PATCH, {**handle, "wait_seconds": 0})
            )
        if self.collected[-1]["status"] != "completed":
            self.child_gate.release.set()
            return _response(
                "", ToolCall("collect-report", COLLECT_CHILD_PATCH, {**handle, "wait_seconds": 5})
            )
        artifact_id = self.collected[-1]["artifact"]["artifact_id"]
        pages = [m for m in request.messages if m.name == "read_child_patch"]
        if not pages:
            return _response(
                "", ToolCall("read-patch", "read_child_patch", {"artifact_id": artifact_id})
            )
        page = json.loads(pages[-1].content)
        if "integrate" not in calls:
            return _response(
                "",
                ToolCall(
                    "integrate",
                    "integrate_child_patch",
                    {
                        "artifact_id": artifact_id,
                        "request_digest": page["request_digest"],
                        "read_tool_call_id": "read-patch",
                    },
                ),
            )
        self.integrated = json.loads(
            next(m.content for m in request.messages if m.name == "integrate_child_patch")
        )
        return _response("Collected, read and integrated the concurrent child Patch.")


class WritableChild:
    """Scripted patch author: change only the assigned path, then report."""

    name = "scripted"  # The managed factory configures this provider id.

    def __init__(self, gate):
        self.gate = gate

    async def complete(self, request):
        if not self.gate.release.is_set():
            self.gate.entered.set()
            await self.gate.release.wait()
        if not any(m.role == "tool" for m in request.messages):
            return _response(
                "",
                ToolCall(
                    "child-write",
                    "apply_patch",
                    {
                        "path": "candidate.py",
                        "old_text": "",
                        "new_text": "VALUE = 1\n",
                        "create": True,
                    },
                ),
            )
        return _response("Changed the assigned file; no functional tests were run.")


@pytest.mark.asyncio
async def test_concurrent_writable_handoff_supplies_the_completion_receipt(tmp_path):
    """The collected handoff is the completion evidence, with a real applied receipt."""
    from traceh.supervision.patch_integration import (
        PATCH_INTEGRATION_TOOLS,
        PatchIntegrationControl,
        PatchIntegrationTool,
    )

    store = InMemoryEventStore()
    source = _source_repository(tmp_path / "source")
    workspaces = WorkspaceService(
        store,
        LocalGitWorkspaceProvider(
            managed_root=tmp_path / "managed", sources={"trusted-source": source}
        ),
    )
    gate = GatedProvider()
    child = WritableChild(gate)
    factory = ManagedRuntimeFactory(store, tmp_path, workspaces, provider=child)
    supervisor = WorkspaceManagedAgentSupervisor(
        ProcessAgentSupervisor(store=store, factory=factory),
        workspaces,
        workspace_policy=WritableWorkspacePolicy(),
    )
    capture = PatchCaptureService(
        supervisor, workspaces, LocalArtifactCas(tmp_path / "cas"), limits=_capture_limits()
    )
    main = await supervisor.create(
        AgentSpec(preset="main", workspace_id="main-workspace"),
        request_id="main-create",
        agent_id="main-agent",
        session_id="main-session",
    )
    main_workspace = await workspaces.resolve_for_agent(main.agent_id)
    control = WritableControl(
        supervisor=supervisor,
        authority=AgentToolAuthority(
            directory_reader=AgentDirectoryReader(store), owner_agent_id=main.agent_id
        ),
        policy=WritablePolicy(main.agent_id, main_workspace.base_revision),
        capture=capture,
        workspaces=workspaces,
        budgets=BudgetLedgerService(store),
    )
    integration = PatchIntegrationControl(control, capture.limits)
    provider = WritableMain(gate)
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "main-runtime",
            provider=provider.name,
            model="model",
            max_steps=12,
        ),
        provider=provider,
        event_store=store,
        step_view=CollaborationPolicy(),
        continuation=CollaborationContinuation(store, main.session_id),
        additional_tools=(
            CollaborationPlanTool((patch_author_role(control),)),
            ChildPatchCollectTool(control),
            *(PatchIntegrationTool(integration, name) for name in PATCH_INTEGRATION_TOOLS),
        ),
    )
    try:
        await runtime.run_existing(main.session_id, "Add the accepted file and use the child work")
        assert provider.dispatch["outcome"] == "dispatched"
        assert [row["status"] for row in provider.collected] == ["pending", "completed"]
        artifact = provider.collected[-1]["artifact"]
        assert artifact["changed_paths"] == ["candidate.py"]
        assert provider.integrated["outcome"] == "applied"
        # The child change really reached the main worktree, next to its own work.
        assert (main_workspace.root / "candidate.py").read_text() == "VALUE = 1\n"
        assert (main_workspace.root / "added.txt").read_text() == "added\n"

        events = await SessionService(store).read_session(main.session_id)
        turn_id = next(e.data["turn_id"] for e in events if e.type == "turn/start")
        receipt = await integration.completion_receipt(
            session_id=main.session_id, turn_id=turn_id, workspace=main_workspace.root
        )
        assert [row["artifact_id"] for row in receipt] == [artifact["artifact_id"]]
        assert [row["tool_call_id"] for row in receipt] == ["integrate"]
        assert receipt[0]["assignment_id"] == ASSIGNMENT["assignment_id"]
        with pytest.raises(AgentToolBindingError, match="handoff-missing"):
            await integration.completion_receipt(
                session_id=main.session_id, turn_id="foreign-turn", workspace=main_workspace.root
            )
        assert not await runtime.check_invariants(main.session_id)
    finally:
        gate.release.set()
        await runtime.dispose()
        await capture.aclose()
        await supervisor.aclose()


def _capture_limits():
    from traceh.api.artifacts import PatchCaptureLimits

    return PatchCaptureLimits(
        max_changed_paths=16,
        max_path_bytes=512,
        max_file_bytes=1024 * 1024,
        max_total_file_bytes=4 * 1024 * 1024,
        max_patch_bytes=4 * 1024 * 1024,
    )


@pytest.mark.asyncio
async def test_waiting_handoff_stays_the_default_shape(tmp_path):
    """An omitted handoff keeps today's one-call report delivery."""
    store = InMemoryEventStore()
    factory = RuntimeFactory(store, tmp_path)
    supervisor = ProcessAgentSupervisor(store=store, factory=factory)
    owner = await supervisor.create(SPEC, request_id="owner")
    toolset = InvestigationToolset(
        supervisor=supervisor,
        owner_agent_id=owner.agent_id,
        event_store=store,
        policy=BoundPolicy(owner.agent_id),
    )

    class SerialMain:
        name = "serial-main"

        def __init__(self):
            self.handoff = None
            self.views = []

        async def complete(self, request):
            names = {t.name for t in request.tools}
            if SUBMIT_COLLABORATION in names:
                return _response(
                    "",
                    ToolCall(
                        "plan",
                        SUBMIT_COLLABORATION,
                        {k: v for k, v in PLAN_ARGUMENTS.items() if k != "handoff"},
                    ),
                )
            self.views.append(tuple(sorted(names)))
            if self.handoff is None:
                self.handoff = json.loads(
                    next(m.content for m in request.messages if m.name == SUBMIT_COLLABORATION)
                )
            return _response("Used the returned child report.")

    provider = SerialMain()
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "main", provider=provider.name, model="model", max_steps=8
        ),
        provider=provider,
        event_store=store,
        step_view=CollaborationPolicy(),
        continuation=CollaborationContinuation(store, owner.session_id),
        additional_tools=(
            *toolset.tools,
            CollaborationPlanTool((investigator_role(toolset.control),)),
        ),
    )
    try:
        await runtime.run_existing(owner.session_id, "Investigate the target")
        assert provider.handoff["outcome"] == "completed"
        assert provider.handoff["children"][0]["status"] == "completed"
        assert provider.views and all(
            COLLECT_INVESTIGATION not in names and SUBMIT_COLLABORATION not in names
            for names in provider.views
        )
        assert not await runtime.check_invariants(owner.session_id)
    finally:
        await runtime.dispose()
        await supervisor.aclose()


@pytest.mark.asyncio
async def test_a_terminal_child_failure_stops_the_run_at_that_collect(tmp_path) -> None:
    """The verdict is known the moment the report lands, so spending stops there.

    Holding this check until the main Agent announces completion made it a
    final-delivery gate. In a real trial the main Agent had a ``failed`` report
    at 04:55:17 and then spent 17 further model calls and 562,727 tokens over
    18.52 minutes before being refused. This scripted main is deliberately
    eager - it asks for more work after the terminal report - so the assertion
    is about the host refusing, not about the model stopping politely.
    """

    store = InMemoryEventStore()
    # A child that fails its Turn rather than delivering.
    child_gate = GatedProvider(FailingChild())
    factory = RuntimeFactory(store, tmp_path, provider=child_gate)
    supervisor = ProcessAgentSupervisor(store=store, factory=factory)
    owner = await supervisor.create(SPEC, request_id="owner")
    toolset = InvestigationToolset(
        supervisor=supervisor,
        owner_agent_id=owner.agent_id,
        event_store=store,
        policy=BoundPolicy(owner.agent_id),
    )
    parked = asyncio.Event()
    provider = ConcurrentMain(
        "keeps-working-after-terminal",
        supervisor=supervisor,
        child_gate=child_gate,
        parked=parked,
    )
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "main", provider=provider.name, model="model", max_steps=12
        ),
        provider=provider,
        event_store=store,
        step_view=CollaborationPolicy(),
        continuation=CollaborationContinuation(store, owner.session_id),
        additional_tools=(
            *toolset.tools,
            CollaborationPlanTool((investigator_role(toolset.control),)),
        ),
    )
    running = asyncio.create_task(runtime.run_existing(owner.session_id, "Investigate the target"))
    try:
        child_gate.release.set()
        with pytest.raises(CollaborationChildIncomplete) as refused:
            await running
        assert "collaboration-child-terminal-failure" in str(refused.value)
        # The whole point: the eager main never got another request in.
        assert provider.calls_after_terminal == 0
    finally:
        child_gate.release.set()
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        await runtime.dispose()
        await supervisor.aclose()
