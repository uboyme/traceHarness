"""Budget negotiation through the existing ledger and real Agent execution loop."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from supervision_fixtures import SPEC, RuntimeFactory
from test_budget_ledger import AppendBarrierStore, create_agent, granted_root, limits
from test_investigation_tools import WORK, BoundPolicy, context

from traceh.api.budgets import BudgetAmounts, ChildBudgetGrant
from traceh.api.llm import ModelResponse, ToolCall, Usage, UsageQuality
from traceh.budgets import (
    BudgetAccountClosedError,
    BudgetEnforcement,
    BudgetExhaustedError,
    BudgetInputError,
    BudgetLedgerConflictError,
    BudgetLedgerService,
    BudgetOperationConflictError,
    BudgetWriteError,
)
from traceh.budgets.events import BUDGET_CHILD_TOKEN_DECIDED
from traceh.budgets.supervision import BudgetedAgentSupervisor
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.session.event_store import InMemoryEventStore
from traceh.session.sqlite import SqliteEventStore
from traceh.supervision import AgentRuntimeExecution, AgentToolBindingError, ProcessAgentSupervisor
from traceh.supervision.delegation import InvestigationToolset
from traceh.supervision.investigation_budget import (
    DECIDE_BUDGET,
    INVESTIGATOR_BUDGET_TOOLS,
    REQUEST_BUDGET,
    InvestigationBudgetContinuation,
    InvestigatorBudgetTool,
)
from traceh.tools.builtins import ReadFileTool

pytestmark = pytest.mark.asyncio


async def child_account(store, service, child="child", *, initial=20, ceiling=80, retained=30):
    await service.reserve_child(
        operation_id=f"reserve-{child}",
        reservation_id=f"r-{child}",
        parent_agent_id="root",
        child_agent_id=child,
        creation_request_id=f"create-{child}",
        child_limits=limits(
            max_tokens=ceiling,
            max_steps=0,
            max_tool_calls=0,
            max_wall_milliseconds=0,
            max_children=0,
            max_depth=0,
            max_processes=0,
        ),
        initial_tokens=initial,
        retained_tokens=retained,
    )
    await create_agent(store, child, owner_agent_id="root")
    await service.commit_reservation(operation_id=f"commit-{child}", reservation_id=f"r-{child}")


async def decide(service, *, child="child", request="ask", tokens=30, operation="decide"):
    return await service.decide_child_tokens(
        operation_id=operation,
        request_id=request,
        parent_agent_id="root",
        child_agent_id=child,
        tokens=tokens,
        reason="bounded remaining work",
    )


async def test_partial_grant_conserves_capacity_usage_and_replays(tmp_path):
    path = tmp_path / "events.sqlite"
    store = SqliteEventStore(path)
    service = await granted_root(store)
    await child_account(store, service)
    await service.admit_usage(
        operation_id="used", agent_id="child", amounts=BudgetAmounts(tokens=5)
    )
    original = (await service.ledger()).account("child")
    decision = await decide(service)
    assert await decide(service) == decision
    ledger = await service.ledger()
    assert ledger.available("root").max_tokens == 50
    assert ledger.available("child").max_tokens == 45
    assert ledger.token_ceiling("child") == 80
    assert ledger.account("child").charged == original.charged
    assert ledger.account("child").limits.max_steps == original.limits.max_steps
    with pytest.raises(BudgetOperationConflictError):
        await decide(service, tokens=20)
    with pytest.raises(BudgetInputError):
        await decide(service, operation="different-id")
    await store.aclose()
    reopened = SqliteEventStore(path)
    try:
        replay = await BudgetLedgerService(reopened).ledger()
        assert replay.token_decision("ask") == decision
        assert replay.available("root").max_tokens == 50
        assert replay.available("child").max_tokens == 45
    finally:
        await reopened.aclose()


@pytest.mark.parametrize("initial", [0, -1, True, 81, "20"])
async def test_invalid_initial_grant_never_appends(initial):
    store = InMemoryEventStore()
    service = await granted_root(store)
    before = (await service.ledger()).head_seq
    with pytest.raises(BudgetInputError):
        await child_account(store, service, initial=initial)
    assert (await service.ledger()).head_seq == before


async def test_ceiling_full_grant_decline_and_closed_accounts():
    store = InMemoryEventStore()
    service = await granted_root(store)
    await child_account(store, service, initial=None, retained=0)
    with pytest.raises(BudgetInputError):
        await decide(service, tokens=1)
    declined = await decide(service, tokens=0)
    assert declined.tokens == 0
    assert (await service.ledger()).available("root").max_tokens == 20
    await service.close_account(operation_id="close", agent_id="child")
    with pytest.raises(BudgetAccountClosedError):
        await decide(service, request="new", operation="new", tokens=1)


async def test_pending_parent_reservation_and_retained_allowance_cannot_be_spent():
    store = InMemoryEventStore()
    service = await granted_root(store)
    await child_account(store, service)
    await service.reserve_usage(
        operation_id="hold",
        reservation_id="hold",
        agent_id="root",
        amounts=BudgetAmounts(tokens=40),
    )
    with pytest.raises(BudgetExhaustedError):
        await decide(service, tokens=20)
    await service.release_usage(operation_id="release", reservation_id="hold")
    await decide(service, tokens=20)
    with pytest.raises(BudgetExhaustedError):
        await decide(service, request="next", operation="next", tokens=40)
    assert (await service.ledger()).available("root").max_tokens == 60


async def test_two_writers_cannot_allocate_the_same_parent_reserve():
    inner = InMemoryEventStore()
    store = AppendBarrierStore(inner)
    service = await granted_root(store)
    await child_account(store, service, "a")
    await child_account(store, service, "b")
    store.enabled = True
    tasks = [
        asyncio.create_task(
            decide(BudgetLedgerService(store), child=child, request=child, operation=child)
        )
        for child in ("a", "b")
    ]
    await store.both_entered.wait()
    store.release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert sum(isinstance(r, BudgetLedgerConflictError) for r in results) == 1
    loser = ("a", "b")[next(i for i, r in enumerate(results) if isinstance(r, Exception))]
    with pytest.raises(BudgetExhaustedError):
        await decide(BudgetLedgerService(inner), child=loser, request=loser, operation=loser)
    assert (await BudgetLedgerService(inner).ledger()).available("root").max_tokens == 30


@pytest.mark.parametrize("cancel", [False, True])
async def test_committed_append_failure_never_double_allocates(cancel):
    class AfterCommit:
        def __init__(self):
            self.inner = InMemoryEventStore()
            self.fail = True

        def __getattr__(self, name):
            return getattr(self.inner, name)

        async def append(self, *args, **kwargs):
            result = await self.inner.append(*args, **kwargs)
            if self.fail and kwargs["events"][0].type == BUDGET_CHILD_TOKEN_DECIDED:
                self.fail = False
                raise asyncio.CancelledError() if cancel else OSError("post-commit fixture")
            return result

    store = AfterCommit()
    service = await granted_root(store)
    await child_account(store, service)
    with pytest.raises(asyncio.CancelledError if cancel else BudgetWriteError) as caught:
        await decide(service)
    if not cancel:
        assert caught.value.committed is True
    await decide(service)
    assert (await service.ledger()).available("root").max_tokens == 50


class NegotiationFactory(RuntimeFactory):
    def __init__(self, store, root, provider):
        super().__init__(store, root, provider=provider)
        self.budgets = BudgetLedgerService(store)
        self.policy = None
        self.before_yield = None

    async def provision(self, spec, *, agent_id, session_id):
        if spec.owner_agent_id is None:
            return await super().provision(spec, agent_id=agent_id, session_id=session_id)
        continuation = InvestigationBudgetContinuation(self.store, agent_id, session_id)
        if self.before_yield is not None:
            original = continuation
            entered, release = self.before_yield

            class YieldGate:
                async def decide(self, **kwargs):
                    result = await original.decide(**kwargs)
                    if getattr(result, "reason", None) == "investigation_budget_requested":
                        entered.set()
                        await release.wait()
                    return result

            continuation = YieldGate()
        enforcement = BudgetEnforcement(
            self.budgets, agent_id=agent_id, session_id=session_id, continuation=continuation
        )
        tools = [
            ReadFileTool(),
            *(
                InvestigatorBudgetTool(
                    name=name,
                    budgets=self.budgets,
                    child_id=agent_id,
                    session_id=session_id,
                    policy=self.policy,
                )
                for name in INVESTIGATOR_BUDGET_TOOLS
            ),
        ]
        runtime = build_default_runtime(
            RuntimeConfig(
                data_dir=self.root / "data",
                provider="scripted",
                model="fixture",
                max_output_tokens=8,
                max_steps=8,
            ),
            provider=self.provider,
            event_store=self.store,
            include_default_tools=False,
            additional_tools=tuple(tools),
            continuation=enforcement.continuation,
            llm_runtime=enforcement.llm_runtime,
            tool_admission_gate=enforcement.tool_admission_gate,
        )
        workspace = self._workspace(spec)
        (workspace / "notes.txt").write_text("verified fixture evidence\n", encoding="utf-8")
        await runtime.create_session(workspace, session_id=session_id)
        return enforcement.wrap(
            AgentRuntimeExecution(runtime, session_id), max_turn_wall_milliseconds=1000
        )


def response(text, *calls):
    return ModelResponse(
        content=text, tool_calls=tuple(calls), usage=Usage(1, 1, UsageQuality.EXACT)
    )


async def negotiation(tmp_path, *, store=None, provider=None):
    store = InMemoryEventStore() if store is None else store
    provider = provider or ScriptedLlmProvider(
        (
            response("Read evidence", ToolCall("read", "read_file", {"path": "notes.txt"})),
            response(
                "",
                ToolCall(
                    "ask",
                    REQUEST_BUDGET,
                    {
                        "tokens": 30,
                        "progress": "notes.txt:1 contains verified fixture evidence",
                        "remaining_work": "explain the evidence and its limits",
                    },
                ),
            ),
            response("Completed after explicit further work"),
        )
    )
    factory = NegotiationFactory(store, tmp_path, provider)

    class Grants:
        def grant_for_child(self, *, parent, child):
            return ChildBudgetGrant(
                limits(
                    max_tokens=80,
                    max_steps=8,
                    max_tool_calls=8,
                    max_wall_milliseconds=5000,
                    max_children=0,
                    max_depth=0,
                    max_processes=0,
                ),
                30,
                20,
            )

    supervisor = BudgetedAgentSupervisor(
        ProcessAgentSupervisor(store=store, factory=factory),
        factory.budgets,
        child_budget_policy=Grants(),
    )
    owner = await supervisor.create(SPEC, request_id="owner")
    await factory.budgets.grant_root(operation_id="root", agent_id=owner.agent_id, limits=limits())
    factory.policy = BoundPolicy(owner.agent_id)
    tools = {
        t.name: t
        for t in InvestigationToolset(
            supervisor=supervisor,
            owner_agent_id=owner.agent_id,
            event_store=store,
            policy=factory.policy,
            budgets=factory.budgets,
        ).tools
    }
    return supervisor, factory, owner, tools


@pytest.mark.parametrize("grant", [0, 30])
async def test_actual_request_yields_without_extra_llm_then_parent_decides(tmp_path, grant):
    supervisor, factory, owner, tools = await negotiation(tmp_path)
    ctx = context(tmp_path, owner.session_id)
    try:
        work = await tools["delegate_investigation"].execute(WORK, ctx)
        report = await tools["collect_investigation"].execute({**work.data, "wait_seconds": 5}, ctx)
        assert report.data["reason"] == "investigation_budget_requested"
        assert report.data["status"] == "completed"  # turn completed, not investigation success
        request = report.data["budget_requests"][0]
        assert request["progress"].startswith("notes.txt:1")
        assert request["evidence_ref"].startswith("session:")
        assert report.data["budget"]["charged"]["tokens"] == 4  # exactly two model responses
        args = {
            "agent_id": work.data["agent_id"],
            "request_id": request["request_id"],
            "tokens": grant,
            "reason": "useful work" if grant else "enough evidence",
        }
        decided = await tools[DECIDE_BUDGET].execute(args, ctx)
        assert (
            await tools[DECIDE_BUDGET].execute(args, replace(ctx, tool_call_id="retry")) == decided
        )
        assert decided.data["status"] == ("granted" if grant else "declined")
        assert (await factory.budgets.ledger()).account(work.data["agent_id"]).charged.tokens == 4
        if grant:
            followup = await tools["followup_investigation"].execute(
                {**WORK, "agent_id": work.data["agent_id"]}, replace(ctx, tool_call_id="continue")
            )
            final = await tools["collect_investigation"].execute(
                {**followup.data, "wait_seconds": 5}, ctx
            )
            assert final.data["reason"] == "completed"
            assert final.data["budget"]["charged"]["tokens"] == 6
        await tools["stop_investigation"].execute({"agent_id": work.data["agent_id"]}, ctx)
        stopped = await tools["collect_investigation"].execute(
            {**work.data, "wait_seconds": 0}, ctx
        )
        assert stopped.data["budget_requests"][0]["decision"]["tokens"] == grant
        with pytest.raises(BudgetAccountClosedError):
            await tools["followup_investigation"].execute(
                {**WORK, "agent_id": work.data["agent_id"]}, ctx
            )
    finally:
        await supervisor.aclose()


async def test_request_cannot_be_decided_before_yield_or_after_cancel(tmp_path):
    supervisor, factory, owner, tools = await negotiation(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    factory.before_yield = entered, release
    ctx = context(tmp_path, owner.session_id)
    try:
        work = await tools["delegate_investigation"].execute(WORK, ctx)
        await asyncio.wait_for(entered.wait(), 5)
        from traceh.agents import AgentDirectoryReader
        from traceh.supervision.errors import AgentMessageNotSettledError
        from traceh.supervision.investigation_budget import read_budget_requests

        child = (await AgentDirectoryReader(factory.store).load()).get(work.data["agent_id"])
        request = (await read_budget_requests(factory.store, child))[0]
        args = {
            "agent_id": child.agent_id,
            "request_id": request["request_id"],
            "tokens": 20,
            "reason": "finish analysis",
        }
        with pytest.raises(AgentMessageNotSettledError):
            await tools[DECIDE_BUDGET].execute(args, ctx)
        await supervisor.interrupt(child.agent_id)
        partial = await tools["collect_investigation"].execute(
            {**work.data, "wait_seconds": 5}, ctx
        )
        assert partial.data["status"] == "cancelled"
        assert len(partial.data["budget_requests"]) == 1
        with pytest.raises(AgentToolBindingError):
            await tools[DECIDE_BUDGET].execute(args, ctx)
        assert (await factory.budgets.ledger()).token_decision(request["request_id"]) is None
    finally:
        release.set()
        await supervisor.aclose()


async def test_foreign_owner_and_stale_request_cannot_allocate(tmp_path):
    supervisor, factory, owner, tools = await negotiation(tmp_path)
    ctx = context(tmp_path, owner.session_id)
    try:
        work = await tools["delegate_investigation"].execute(WORK, ctx)
        report = await tools["collect_investigation"].execute({**work.data, "wait_seconds": 5}, ctx)
        request = report.data["budget_requests"][0]
        args = {
            "agent_id": work.data["agent_id"],
            "request_id": request["request_id"],
            "tokens": 20,
            "reason": "finish analysis",
        }
        with pytest.raises(AgentToolBindingError):
            await tools[DECIDE_BUDGET].execute(args, replace(ctx, session_id="foreign"))
        with pytest.raises(ValueError):
            await tools[DECIDE_BUDGET].execute({**args, "tokens": 31}, ctx)
        next_work = await tools["followup_investigation"].execute(
            {**WORK, "agent_id": work.data["agent_id"]}, replace(ctx, tool_call_id="narrow")
        )
        await supervisor.wait_message(next_work.data["agent_id"], next_work.data["message_id"])
        with pytest.raises(AgentToolBindingError):
            await tools[DECIDE_BUDGET].execute(args, ctx)
        assert (await factory.budgets.ledger()).token_decision(request["request_id"]) is None
    finally:
        await supervisor.aclose()


async def test_cancelled_request_turn_cannot_be_approved_and_no_report_is_invented(tmp_path):
    class Gate:
        name = "scripted"

        def __init__(self):
            self.entered, self.release = asyncio.Event(), asyncio.Event()

        async def complete(self, request):
            self.entered.set()
            await self.release.wait()
            return response("unused")

    provider = Gate()
    supervisor, factory, owner, tools = await negotiation(tmp_path, provider=provider)
    ctx = context(tmp_path, owner.session_id)
    try:
        work = await tools["delegate_investigation"].execute(WORK, ctx)
        await provider.entered.wait()
        await supervisor.interrupt(work.data["agent_id"])
        report = await tools["collect_investigation"].execute({**work.data, "wait_seconds": 5}, ctx)
        assert report.data["status"] == "cancelled"
        assert report.data["budget_requests"] == []
        with pytest.raises(AgentToolBindingError):
            await tools[DECIDE_BUDGET].execute(
                {
                    "agent_id": work.data["agent_id"],
                    "request_id": "invented",
                    "tokens": 1,
                    "reason": "not evidence",
                },
                ctx,
            )
    finally:
        provider.release.set()
        await supervisor.aclose()
