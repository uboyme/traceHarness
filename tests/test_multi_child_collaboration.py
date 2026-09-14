"""One plan, several direct assistants, through the real loop and owners.

Identity, budget, waiting and cancellation keep their existing owners. These
cases prove the batch is not hard-coded to one or two assistants, that one
assignment's report never settles another, and that a partial dispatch converges.
"""

import asyncio
import json

import pytest
from supervision_fixtures import SPEC, GatedProvider, RuntimeFactory
from test_concurrent_collaboration import (
    ManagedRuntimeFactory,
    WritablePolicy,
    WritableWorkspacePolicy,
    _capture_limits,
    _source_repository,
)
from test_investigation_tools import WORK, BoundPolicy, context
from test_product_f3_e2e import _response

from traceh.agents import AgentDirectoryReader, AgentInboxReader
from traceh.api.agents import AgentSpec
from traceh.api.budgets import BudgetLimits
from traceh.api.llm import ToolCall
from traceh.artifacts import LocalArtifactCas, PatchCaptureService
from traceh.artifacts.catalog import PatchArtifactCatalogReader
from traceh.budgets.service import BudgetLedgerService
from traceh.product.collaboration import (
    CollaborationChildIncomplete,
    CollaborationContinuation,
    CollaborationPolicy,
    dispatched_roles,
)
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.session.event_store import InMemoryEventStore
from traceh.session.service import SessionService
from traceh.supervision import AgentRunReportReader, ProcessAgentSupervisor
from traceh.supervision.authority import AgentToolAuthority, AgentToolBindingError
from traceh.supervision.delegation import COLLECT_INVESTIGATION, InvestigationToolset
from traceh.supervision.errors import AgentMessageNotSettledError
from traceh.supervision.investigation_work import read_investigation_work
from traceh.supervision.structured_collaboration import (
    AWAIT_REPORT,
    CHILD_REPORT_WAIT_SECONDS,
    DISPATCH_AND_CONTINUE,
    SUBMIT_COLLABORATION,
    CollaborationPlanInputInvalid,
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
from traceh.workspaces import (
    LocalGitWorkspaceProvider,
    WorkspaceManagedAgentSupervisor,
    WorkspaceService,
)

CHILD_TEXT = {k: v for k, v in WORK.items() if k != "main_work"}


def readonly(assignment_id):
    return {"assignment_id": assignment_id, "role": "investigator", **CHILD_TEXT}


def writable(assignment_id, paths):
    return {"assignment_id": assignment_id, "role": "patch_author", "paths": paths, **CHILD_TEXT}


def plan(children, handoff=DISPATCH_AND_CONTINUE):
    return {"main_work": WORK["main_work"], "children": children, "handoff": handoff}


class GatedChildFactory(RuntimeFactory):
    """Hand each owned child its own gated provider, in creation order."""

    def __init__(self, store, root, gates):
        super().__init__(store, root)
        self.gates = list(gates)
        self.assigned = []

    async def provision(self, spec, *, agent_id, session_id):
        if spec.owner_agent_id is not None and self.gates:
            gate = self.gates[min(len(self.assigned), len(self.gates) - 1)]
            self.assigned.append(agent_id)
            self.provider = gate
        return await super().provision(spec, agent_id=agent_id, session_id=session_id)


class BatchMain:
    """Dispatch a batch, work while it runs, then collect in the given order."""

    name = "batch-main"

    def __init__(self, children, *, gates, collect_order=None, skip=(), handoff=None,
                 supervisor=None, collect_repeats=1):
        self.children = children
        self.gates = gates
        self.supervisor = supervisor
        self.unsettled_while_working = 0
        self.collect_order = collect_order
        self.skip = set(skip)
        self.handoff = handoff
        self.dispatch = None
        self.collected = []
        self.views = []
        self.running_while_working = None
        self.collect_repeats = collect_repeats
        self.collect_calls = 0

    def _handles(self):
        rows = {row["assignment_id"]: row for row in self.dispatch["children"]}
        order = self.collect_order or list(rows)
        return [rows[key] for key in order if key not in self.skip]

    async def complete(self, request):
        names = {t.name for t in request.tools}
        if SUBMIT_COLLABORATION in names:
            arguments = plan(self.children)
            if self.handoff is not None:
                arguments["handoff"] = self.handoff
            return _response("", ToolCall("plan", SUBMIT_COLLABORATION, arguments))
        self.views.append(tuple(sorted(names)))
        if self.dispatch is None:
            self.dispatch = json.loads(
                next(m.content for m in request.messages if m.name == SUBMIT_COLLABORATION)
            )
            # Deterministic overlap: every assistant is parked inside its own
            # model call while this main request is being served.
            await asyncio.wait_for(
                asyncio.gather(*(gate.entered.wait() for gate in self.gates)), 5
            )
            self.running_while_working = sum(gate.entered.is_set() for gate in self.gates)
            if self.supervisor is not None:
                for row in self.dispatch["children"]:
                    try:
                        await self.supervisor.report(row["agent_id"], row["message_id"])
                    except AgentMessageNotSettledError:
                        self.unsettled_while_working += 1
            return _response("", ToolCall("own-work", "list_files", {"path": "."}))
        done = {
            (m.name, m.tool_call_id): json.loads(m.content)
            for m in request.messages
            if m.name in {COLLECT_INVESTIGATION, COLLECT_CHILD_PATCH}
        }
        self.collected = list(done.values())
        pending = [
            row
            for row in self._handles()
            if sum(
                value.get("agent_id") == row["agent_id"] and value.get("status") == "completed"
                for value in done.values()
            )
            < self.collect_repeats
        ]
        if not pending:
            return _response("Used every collected report and finished the retained work.")
        row = pending[0]
        self.collect_calls += 1
        for gate in self.gates:
            gate.release.set()
        tool = COLLECT_CHILD_PATCH if row["role"] == "patch_author" else COLLECT_INVESTIGATION
        arguments = {"agent_id": row["agent_id"], "message_id": row["message_id"]}
        return _response(
            "",
            ToolCall(
                f"collect-{row['assignment_id']}-{len(done)}",
                tool,
                {**arguments, "wait_seconds": 5},
            ),
        )


def _readonly_runtime(
    tmp_path,
    store,
    supervisor,
    owner,
    provider,
    *,
    max_steps=16,
    policy=None,
    budgets=None,
    in_call_wait_seconds=None,
):
    toolset = InvestigationToolset(
        supervisor=supervisor,
        owner_agent_id=owner.agent_id,
        event_store=store,
        policy=policy or BoundPolicy(owner.agent_id),
        **({} if budgets is None else {"budgets": budgets}),
    )
    held = {} if in_call_wait_seconds is None else {"in_call_wait_seconds": in_call_wait_seconds}
    return build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "main",
            provider=provider.name,
            model="model",
            max_steps=max_steps,
        ),
        provider=provider,
        event_store=store,
        step_view=CollaborationPolicy(),
        continuation=CollaborationContinuation(store, owner.session_id),
        additional_tools=(
            *toolset.tools,
            CollaborationPlanTool((investigator_role(toolset.control),), **held),
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("count,order,skip", [(2, None, ()), (3, None, ()), (2, ["b", "a"], ())])
async def test_batch_runs_concurrently_and_each_report_settles_only_itself(
    tmp_path, count, order, skip
):
    store = InMemoryEventStore()
    gates = [GatedProvider() for _ in range(count)]
    names = ["a", "b", "c"][:count]

    supervisor = ProcessAgentSupervisor(
        store=store, factory=GatedChildFactory(store, tmp_path, gates)
    )
    owner = await supervisor.create(SPEC, request_id="owner")
    provider = BatchMain(
        [readonly(n) for n in names],
        gates=gates,
        collect_order=order,
        skip=skip,
        supervisor=supervisor,
    )
    runtime = _readonly_runtime(tmp_path, store, supervisor, owner, provider)
    try:
        await runtime.run_existing(owner.session_id, "Investigate the target")
        assert provider.dispatch["outcome"] == "dispatched"
        assert [row["assignment_id"] for row in provider.dispatch["children"]] == names
        identities = {row["agent_id"] for row in provider.dispatch["children"]}
        assert len(identities) == count
        # Every assistant was really running while the main did its own work.
        assert provider.running_while_working == count
        assert provider.unsettled_while_working == count
        children = [
            r
            for r in (await AgentDirectoryReader(store).load()).records
            if r.owner_agent_id == owner.agent_id
        ]
        assert len(children) == count
        keys = set()
        for child in children:
            accepted = next(iter(await AgentInboxReader(store).load(child.agent_id)))
            work = read_investigation_work(accepted.message.content)
            keys.add(work["assignment_id"])
            report = await AgentRunReportReader(store).load(
                child.agent_id, accepted.message.message_id
            )
            assert report.status == "completed"
        assert keys == set(names)
        assert {row["agent_id"] for row in provider.collected} == identities
        assert not await runtime.check_invariants(owner.session_id)
    finally:
        for gate in gates:
            gate.release.set()
        await runtime.dispose()
        await supervisor.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("repeats", [1, 2])
async def test_one_uncollected_assignment_refuses_delivery(tmp_path, repeats):
    """Collecting "a" - even twice - never settles the uncollected "b"."""
    store = InMemoryEventStore()
    gates = [GatedProvider(), GatedProvider()]

    supervisor = ProcessAgentSupervisor(
        store=store, factory=GatedChildFactory(store, tmp_path, gates)
    )
    owner = await supervisor.create(SPEC, request_id="owner")
    # "b" is dispatched and completes, but the main only ever collects "a".
    provider = BatchMain(
        [readonly("a"), readonly("b")], gates=gates, skip=["b"], collect_repeats=repeats
    )
    runtime = _readonly_runtime(tmp_path, store, supervisor, owner, provider)
    try:
        with pytest.raises(CollaborationChildIncomplete) as refused:
            await runtime.run_existing(owner.session_id, "Investigate the target")
        assert "collaboration-child-report-not-collected" in str(refused.value)
        collected = {
            row["agent_id"] for row in provider.collected if row.get("status") == "completed"
        }
        # Repeating one collection never covers the sibling it did not collect.
        assert len(collected) == 1
        assert provider.collect_calls == repeats
    finally:
        for gate in gates:
            gate.release.set()
        await runtime.dispose()
        await supervisor.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "children,expected",
    [
        ([], "collaboration-plan-assignment-count-invalid"),
        ([readonly("a"), readonly("a")], "collaboration-assignment-id-duplicate"),
        ([readonly("a"), {**readonly("b"), "role": "patch_author"}], "role-unauthorized"),
        ([{**readonly("a"), "assignment_id": " "}], "collaboration-assignment-id-invalid"),
        ([{**readonly("a"), "extra": "x"}], "collaboration-assignment-fields-invalid"),
    ],
)
async def test_invalid_batches_are_refused_before_any_dispatch(tmp_path, children, expected):
    store = InMemoryEventStore()
    supervisor = ProcessAgentSupervisor(store=store, factory=RuntimeFactory(store, tmp_path))
    owner = await supervisor.create(SPEC, request_id="owner")
    toolset = InvestigationToolset(
        supervisor=supervisor,
        owner_agent_id=owner.agent_id,
        event_store=store,
        policy=BoundPolicy(owner.agent_id),
    )
    tool = CollaborationPlanTool((investigator_role(toolset.control),))
    try:
        with pytest.raises(ValueError, match=expected):
            tool.validate(plan(children))
        assert not (await AgentDirectoryReader(store).load()).children_of(owner.agent_id)
    finally:
        await supervisor.aclose()


@pytest.mark.asyncio
async def test_overlapping_writable_paths_are_refused_before_any_dispatch(tmp_path):
    store = InMemoryEventStore()
    supervisor = ProcessAgentSupervisor(store=store, factory=RuntimeFactory(store, tmp_path))
    owner = await supervisor.create(SPEC, request_id="owner")
    toolset = InvestigationToolset(
        supervisor=supervisor,
        owner_agent_id=owner.agent_id,
        event_store=store,
        policy=BoundPolicy(owner.agent_id),
    )
    tool = CollaborationPlanTool(
        (investigator_role(toolset.control), patch_author_role(object()))
    )
    try:
        tool.validate(plan([writable("a", ["one.py"]), writable("b", ["two.py"])]))
        with pytest.raises(ValueError, match="paths-overlap"):
            tool.validate(plan([writable("a", ["one.py"]), writable("b", ["one.py"])]))
        with pytest.raises(ValueError, match="fields-invalid"):
            tool.validate(plan([{**readonly("a"), "paths": ["one.py"]}]))
    finally:
        await supervisor.aclose()


@pytest.mark.asyncio
async def test_count_over_the_host_authorization_is_correctable(tmp_path):
    store = InMemoryEventStore()
    supervisor = ProcessAgentSupervisor(store=store, factory=RuntimeFactory(store, tmp_path))
    owner = await supervisor.create(SPEC, request_id="owner")
    budgets = BudgetLedgerService(store)
    await budgets.grant_root(
        operation_id="explicit-test-root",
        agent_id=owner.agent_id,
        limits=BudgetLimits(
            max_tokens=10_000,
            max_steps=20,
            max_tool_calls=20,
            max_wall_milliseconds=60_000,
            max_children=1,
            max_depth=1,
            max_processes=0,
        ),
    )
    toolset = InvestigationToolset(
        supervisor=supervisor,
        owner_agent_id=owner.agent_id,
        event_store=store,
        policy=BoundPolicy(owner.agent_id),
        budgets=budgets,
    )
    tool = CollaborationPlanTool((investigator_role(toolset.control),))
    try:
        record = (await AgentDirectoryReader(store).load()).get(owner.agent_id)
        with pytest.raises(CollaborationPlanInputInvalid, match="authorization has 1 left"):
            await tool._require_capacity([readonly("a"), readonly("b")], record)
        await tool._require_capacity([readonly("a")], record)
    finally:
        await supervisor.aclose()


@pytest.mark.asyncio
async def test_a_batch_over_the_declared_grant_is_correctable_per_dimension(tmp_path):
    """The real failure mode: the batch fits max_children but not the clock."""
    from traceh.api.budgets import ChildBudgetGrant

    store = InMemoryEventStore()
    supervisor = ProcessAgentSupervisor(store=store, factory=RuntimeFactory(store, tmp_path))
    owner = await supervisor.create(SPEC, request_id="owner")
    budgets = BudgetLedgerService(store)
    await budgets.grant_root(
        operation_id="explicit-test-root",
        agent_id=owner.agent_id,
        limits=BudgetLimits(
            max_tokens=200_000,
            max_steps=40,
            max_tool_calls=40,
            max_wall_milliseconds=400_000,
            max_children=2,
            max_depth=1,
            max_processes=0,
        ),
    )

    class GrantingPolicy(BoundPolicy):
        def planned_grant(self, record):
            return ChildBudgetGrant(
                BudgetLimits(
                    max_tokens=10_000,
                    max_steps=8,
                    max_tool_calls=8,
                    # Two of these exceed the parent's remaining clock.
                    max_wall_milliseconds=300_000,
                    max_children=0,
                    max_depth=0,
                    max_processes=0,
                ),
                0,
                None,
            )

    toolset = InvestigationToolset(
        supervisor=supervisor,
        owner_agent_id=owner.agent_id,
        event_store=store,
        policy=GrantingPolicy(owner.agent_id),
        budgets=budgets,
    )
    tool = CollaborationPlanTool((investigator_role(toolset.control),))
    record = (await AgentDirectoryReader(store).load()).get(owner.agent_id)
    try:
        with pytest.raises(CollaborationPlanInputInvalid, match="max_wall_milliseconds=600000"):
            await tool._require_capacity([readonly("a"), readonly("b")], record)
        # One assistant still fits, so the answer is "ask for fewer", not "stop".
        await tool._require_capacity([readonly("a")], record)
    finally:
        await supervisor.aclose()


def test_a_wrong_typed_plan_argument_is_refused_with_its_declared_shape():
    """A rejection the caller cannot act on is retried, not corrected.

    A real round submitted five plans and was refused five times with only
    "must be object, got str". The keys are in the schema the caller already
    holds, so naming them turns the refusal into a one-attempt repair.
    """

    from traceh.tools.schema import ToolArgumentError, validate_arguments

    schema = CollaborationPlanTool((investigator_role(object()),)).input_schema
    with pytest.raises(ToolArgumentError) as refused:
        validate_arguments({"main_work": "a string", "children": []}, schema)

    message = str(refused.value)
    assert "must be object, got str" in message
    for key in ("goal", "deliverable", "uses_child_report"):
        assert key in message
    # The shape is only added where the schema declares one.
    with pytest.raises(ToolArgumentError) as plain:
        validate_arguments({"main_work": {}, "children": "not a list"}, schema)
    assert "must be array, got str" in str(plain.value)
    assert "an object with keys" not in str(plain.value)


def _grant(wall_milliseconds):
    from traceh.api.budgets import ChildBudgetGrant

    return ChildBudgetGrant(
        BudgetLimits(
            max_tokens=10_000,
            max_steps=8,
            max_tool_calls=8,
            max_wall_milliseconds=wall_milliseconds,
            max_children=0,
            max_depth=0,
            max_processes=0,
        ),
        0,
        None,
    )


def test_the_in_call_wait_follows_the_authorized_assistant_clock():
    """The wait is derived from what the host granted, not from a fixed number."""
    tool = CollaborationPlanTool((investigator_role(object()),), in_call_wait_seconds=60.0)
    # Inside the ceiling: wait exactly as long as an assistant may run.
    assert tool._require_report_wait(20_000) == 20.0
    # No declared grant: the fallback still fits inside what this call may hold.
    assert tool._require_report_wait(None) == 55.0
    # A host that declares no ceiling of its own keeps the fallback bound.
    assert CollaborationPlanTool(
        (investigator_role(object()),)
    )._require_report_wait(None) == CHILD_REPORT_WAIT_SECONDS - 5
    with pytest.raises(CollaborationPlanInputInvalid, match=DISPATCH_AND_CONTINUE):
        tool._require_report_wait(240_000)


@pytest.mark.asyncio
async def test_an_unholdable_await_report_plan_is_corrected_before_any_assistant_exists(tmp_path):
    """The host authorized more assistant clock than one plan call may hold.

    Waiting anyway would cut down assistants that are still inside their budget,
    so the plan is refused while it is still correctable and the model is told
    which handoff mode can deliver the authorization.
    """

    store = InMemoryEventStore()
    gates = [GatedProvider()]
    supervisor = ProcessAgentSupervisor(
        store=store, factory=GatedChildFactory(store, tmp_path, gates)
    )
    owner = await supervisor.create(SPEC, request_id="owner")
    budgets = BudgetLedgerService(store)
    await budgets.grant_root(
        operation_id="explicit-test-root",
        agent_id=owner.agent_id,
        limits=BudgetLimits(
            max_tokens=200_000,
            max_steps=40,
            max_tool_calls=40,
            max_wall_milliseconds=900_000,
            max_children=2,
            max_depth=1,
            max_processes=0,
        ),
    )

    class SlowAssistantPolicy(BoundPolicy):
        def planned_grant(self, record):
            # Authorized to run far longer than one tool call may hold.
            return _grant(240_000)

    class Correcting:
        name = "await-report-main"

        def __init__(self):
            self.modes = []
            self.refusal = None

        def _dispatch(self, request):
            for message in request.messages:
                if message.name != SUBMIT_COLLABORATION:
                    continue
                try:
                    payload = json.loads(message.content)
                except json.JSONDecodeError:
                    continue  # the refused plan came back as tool text
                if type(payload) is dict and payload.get("outcome") == "dispatched":
                    return payload
            return None

        async def complete(self, request):
            names = {t.name for t in request.tools}
            if SUBMIT_COLLABORATION not in names:
                if any(m.name == COLLECT_INVESTIGATION for m in request.messages):
                    return _response("Used the collected report and finished.")
                row = self._dispatch(request)["children"][0]
                for gate in gates:
                    gate.release.set()
                return _response(
                    "",
                    ToolCall(
                        "collect-a",
                        COLLECT_INVESTIGATION,
                        {
                            "agent_id": row["agent_id"],
                            "message_id": row["message_id"],
                            "wait_seconds": 5,
                        },
                    ),
                )
            # A refused plan comes back as tool text, not as a plan payload.
            prior = [m.content for m in request.messages if m.name == SUBMIT_COLLABORATION]
            if prior:
                self.refusal = prior[-1]
            mode = AWAIT_REPORT if not prior else DISPATCH_AND_CONTINUE
            self.modes.append(mode)
            return _response(
                "",
                ToolCall(
                    f"plan-{len(self.modes)}",
                    SUBMIT_COLLABORATION,
                    plan([readonly("a")], handoff=mode),
                ),
            )

    provider = Correcting()
    runtime = _readonly_runtime(
        tmp_path,
        store,
        supervisor,
        owner,
        provider,
        max_steps=6,
        policy=SlowAssistantPolicy(owner.agent_id),
        budgets=budgets,
        in_call_wait_seconds=60.0,
    )
    try:
        await runtime.run_existing(owner.session_id, "Investigate the target")
        assert provider.modes == [AWAIT_REPORT, DISPATCH_AND_CONTINUE]
        # The model was told which mode can deliver the host's authorization.
        assert DISPATCH_AND_CONTINUE in provider.refusal
        assert COLLECT_INVESTIGATION in provider.refusal
        results = [
            e.data
            for e in await SessionService(store).read_session(owner.session_id)
            if e.type == "tool/result" and e.data["tool_name"] == SUBMIT_COLLABORATION
        ]
        assert len(results) == 2
        assert correctable_plan_result(results[0])
        assert results[1]["status"] == "succeeded"
        # The refused plan asked for one assistant and created none: the only
        # child in the directory came from the corrected plan.
        children = (await AgentDirectoryReader(store).load()).children_of(owner.agent_id)
        assert len(children) == 1
    finally:
        for gate in gates:
            gate.release.set()
        await runtime.dispose()
        await supervisor.aclose()


@pytest.mark.asyncio
async def test_a_failed_second_dispatch_converges_the_first_and_skips_the_third(tmp_path):
    """Partial dispatch is not a transaction, and nothing is left running."""
    store = InMemoryEventStore()
    child_gate = GatedProvider()
    sends = []

    class FailingSend(ProcessAgentSupervisor):
        async def send(self, *args, **kwargs):
            sends.append(args[0])
            if len(sends) == 2:
                raise RuntimeError("explicit test send failure")
            return await super().send(*args, **kwargs)

    supervisor = FailingSend(
        store=store, factory=RuntimeFactory(store, tmp_path, provider=child_gate)
    )
    owner = await supervisor.create(SPEC, request_id="owner")

    class Planner:
        name = "batch-failure-main"

        async def complete(self, request):
            if SUBMIT_COLLABORATION in {t.name for t in request.tools}:
                return _response(
                    "",
                    ToolCall(
                        "plan",
                        SUBMIT_COLLABORATION,
                        plan([readonly("a"), readonly("b"), readonly("c")]),
                    ),
                )
            return _response("unreachable")

    runtime = _readonly_runtime(tmp_path, store, supervisor, owner, Planner(), max_steps=4)
    try:
        with pytest.raises(BaseException):  # noqa: B017 - the loop wraps provider faults
            await runtime.run_existing(owner.session_id, "Investigate the target")
        results = [
            e.data
            for e in await SessionService(store).read_session(owner.session_id)
            if e.type == "tool/result" and e.data["tool_name"] == SUBMIT_COLLABORATION
        ]
        assert results and results[-1]["status"] == "failed"
        assert not correctable_plan_result(results[-1])
        children = (await AgentDirectoryReader(store).load()).children_of(owner.agent_id)
        # The first assignment was sent, the second's send failed after its Agent
        # was created, and the third was never reached.
        assert len(sends) == 2 and len(children) == 2
        first = next(iter(await AgentInboxReader(store).load(sends[0])))
        report = await AgentRunReportReader(store).load(sends[0], first.message.message_id)
        assert report.status == "cancelled"
    finally:
        child_gate.release.set()
        await runtime.dispose()
        await supervisor.aclose()


@pytest.mark.asyncio
async def test_two_writable_assistants_deliver_independent_patches(tmp_path):
    """Each assistant captures its own Patch; identity and paths stay separate."""
    store = InMemoryEventStore()
    source = _source_repository(tmp_path / "source")
    workspaces = WorkspaceService(
        store,
        LocalGitWorkspaceProvider(
            managed_root=tmp_path / "managed", sources={"trusted-source": source}
        ),
    )
    gate = GatedProvider()
    factory = ManagedRuntimeFactory(store, tmp_path, workspaces, provider=gate)
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
    revision = (await workspaces.resolve_for_agent(main.agent_id)).base_revision
    control = WritableControl(
        supervisor=supervisor,
        authority=AgentToolAuthority(
            directory_reader=AgentDirectoryReader(store), owner_agent_id=main.agent_id
        ),
        policy=WritablePolicy(main.agent_id, revision),
        capture=capture,
        workspaces=workspaces,
        budgets=BudgetLedgerService(store),
    )
    collect = ChildPatchCollectTool(control)
    ctx = context(tmp_path, main.session_id)
    files = {"a": "first.py", "b": "second.py"}
    try:
        dispatched = {}
        for key, path in files.items():
            receipt = await control.delegate(
                {**CHILD_TEXT, "paths": [path], "main_work": WORK["main_work"]},
                ctx,
                assignment_id=key,
            )
            dispatched[key] = receipt.data
        assert dispatched["a"]["agent_id"] != dispatched["b"]["agent_id"]
        await asyncio.wait_for(gate.entered.wait(), 5)
        for key, path in files.items():
            workspace = await workspaces.resolve_for_agent(dispatched[key]["agent_id"])
            (workspace.root / path).write_text(f"VALUE = '{key}'\n", encoding="utf-8")
        gate.release.set()
        artifacts = {}
        for key in files:
            handle = {k: dispatched[key][k] for k in ("agent_id", "message_id")}
            report = await collect.execute({**handle, "wait_seconds": 5}, ctx)
            assert report.data["status"] == "completed"
            artifacts[key] = report.data["artifact"]
        assert artifacts["a"]["changed_paths"] == ["first.py"]
        assert artifacts["b"]["changed_paths"] == ["second.py"]
        assert artifacts["a"]["artifact_id"] != artifacts["b"]["artifact_id"]
        assert len(tuple(await PatchArtifactCatalogReader(store).load())) == 2
        # One assistant's collection never returns another's identity.
        crossed = {
            "agent_id": dispatched["a"]["agent_id"],
            "message_id": dispatched["b"]["message_id"],
        }
        with pytest.raises(AgentToolBindingError):
            await collect.execute({**crossed, "wait_seconds": 0}, ctx)
    finally:
        gate.release.set()
        await capture.aclose()
        await supervisor.aclose()


@pytest.mark.asyncio
async def test_dispatched_roles_drive_the_visible_collect_controls(tmp_path):
    store = InMemoryEventStore()
    gates = [GatedProvider(), GatedProvider()]

    supervisor = ProcessAgentSupervisor(
        store=store, factory=GatedChildFactory(store, tmp_path, gates)
    )
    owner = await supervisor.create(SPEC, request_id="owner")
    provider = BatchMain([readonly("a"), readonly("b")], gates=gates)
    runtime = _readonly_runtime(tmp_path, store, supervisor, owner, provider)
    try:
        await runtime.run_existing(owner.session_id, "Investigate the target")
        events = await SessionService(store).read_session(owner.session_id)
        turn_id = next(e.data["turn_id"] for e in events if e.type == "turn/start")
        assert dispatched_roles(events, turn_id) == frozenset({"investigator"})
        for names in provider.views:
            assert COLLECT_INVESTIGATION in names
            assert COLLECT_CHILD_PATCH not in names
            assert SUBMIT_COLLABORATION not in names
            assert "delegate_investigation" not in names
    finally:
        for gate in gates:
            gate.release.set()
        await runtime.dispose()
        await supervisor.aclose()


@pytest.mark.asyncio
async def test_handoff_rows_survive_a_multi_assignment_plan_receipt(tmp_path):
    """Every child yields one complete diagnostic row, one per assignment.

    The plan receipt now carries a list, and reading it must not disturb the
    rows being built: a collapsed row set breaks the original diagnostics.
    """
    from traceh.evaluation.evaluators.product_handoffs import (
        collaboration_diagnostics,
        investigations,
    )

    store = InMemoryEventStore()
    gates = [GatedProvider(), GatedProvider()]
    supervisor = ProcessAgentSupervisor(
        store=store, factory=GatedChildFactory(store, tmp_path, gates)
    )
    owner = await supervisor.create(SPEC, request_id="owner")
    # Distinct work per assignment, so the duplicate-work diagnostic stays zero.
    second = {**readonly("b"), "goal": "Inspect the other specified source instead."}
    provider = BatchMain([readonly("a"), second], gates=gates, supervisor=supervisor)
    runtime = _readonly_runtime(tmp_path, store, supervisor, owner, provider)
    running = asyncio.create_task(runtime.run_existing(owner.session_id, "Investigate the target"))
    try:
        await running
        rows = await investigations(store, owner.agent_id, BoundPolicy(owner.agent_id).revision)
        assert len(rows) == 2
        assert {row["assignment_id"] for row in rows} == {"a", "b"}
        assert all(row["role"] == "investigator" for row in rows)
        assert all(row["work_digest"] and row["report_status"] == "completed" for row in rows)
        # Each row must show its own delivered report, not a sibling's.
        assert all(row["parent_report_dispatches"] for row in rows)
        diagnostics = collaboration_diagnostics(
            [await SessionService(store).read_session(owner.session_id)], rows
        )
        assert diagnostics["available_reports"] == 2
        assert diagnostics["reports_dispatched_to_parent"] == 2
        assert diagnostics["identical_work_extra_deliveries"] == 0
    finally:
        for gate in gates:
            gate.release.set()
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        await runtime.dispose()
        await supervisor.aclose()


@pytest.mark.asyncio
async def test_a_batch_over_the_live_process_slots_is_correctable(tmp_path):
    """A live-Agent ceiling must be answerable before dispatch, not at create."""
    from traceh.budgets import ProcessSlotAuthority

    store = InMemoryEventStore()
    supervisor = ProcessAgentSupervisor(store=store, factory=RuntimeFactory(store, tmp_path))
    owner = await supervisor.create(SPEC, request_id="owner")
    budgets = BudgetLedgerService(store)
    await budgets.grant_root(
        operation_id="explicit-test-root",
        agent_id=owner.agent_id,
        limits=BudgetLimits(
            max_tokens=200_000,
            max_steps=40,
            max_tool_calls=40,
            max_wall_milliseconds=400_000,
            max_children=4,
            max_depth=1,
            # Only two assistants may be live under this owner at once.
            max_processes=2,
        ),
    )
    toolset = InvestigationToolset(
        supervisor=supervisor,
        owner_agent_id=owner.agent_id,
        event_store=store,
        policy=BoundPolicy(owner.agent_id),
        budgets=budgets,
    )
    slots = ProcessSlotAuthority(budgets)
    tool = CollaborationPlanTool((investigator_role(toolset.control),), slots=slots)
    record = (await AgentDirectoryReader(store).load()).get(owner.agent_id)
    try:
        # The Budget ledger alone would allow four: only the live-slot ceiling refuses.
        await tool._require_capacity([readonly("a"), readonly("b")], record)
        with pytest.raises(CollaborationPlanInputInvalid, match="max_processes"):
            await tool._require_capacity(
                [readonly("a"), readonly("b"), readonly("c")], record
            )
        # An assistant already running consumes one of the two slots.
        lease = await slots.acquire_new(agent_id="held-child", owner_agent_id=owner.agent_id)
        try:
            assert await slots.held(owner.agent_id) == 1
            with pytest.raises(CollaborationPlanInputInvalid, match="allows 1 more"):
                await tool._require_capacity([readonly("a"), readonly("b")], record)
            await tool._require_capacity([readonly("a")], record)
        finally:
            await lease.release()
    finally:
        await supervisor.aclose()
