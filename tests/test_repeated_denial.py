from __future__ import annotations

import asyncio

import pytest

from traceh.api.llm import ModelResponse, ToolCall
from traceh.cli.main import _repeated_denial_policy, build_parser
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.runtime.repeated_denial import RepeatedDenialPolicy, repeated_denial_state
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.event_store import InMemoryEventStore
from traceh.session.recovery import RecoveryService
from traceh.session.sqlite import SqliteEventStore
from traceh.tools.policy import DecisionKind, ToolDecision


class Calls:
    name = "scripted"

    def __init__(self, actions=None, *, gate_at=None):
        self.actions = actions
        self.requests = []
        self.gate_at = gate_at
        self.entered = asyncio.Event()
        self.settled = asyncio.Event()

    async def complete(self, request):
        self.requests.append(request)
        n = len(self.requests)
        if n == self.gate_at:
            self.entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.settled.set()
        paths = ["alpha.txt"] if self.actions is None else self.actions[n - 1]
        if paths is None:
            return ModelResponse(content="done")
        return ModelResponse(
            tool_calls=tuple(
                ToolCall(f"call-{n}-{i}", "read_file", {"path": path})
                for i, path in enumerate(paths)
            )
        )


class Decisions:
    name = "explicit-test-policy"

    def __init__(self, outcomes=None):
        self.outcomes = outcomes
        self.calls = 0

    async def check(self, call, tool, context):
        self.calls += 1
        kind, reason = (
            (DecisionKind.DENY, "access unavailable")
            if self.outcomes is None
            else self.outcomes[self.calls - 1]
        )
        return ToolDecision(kind, reason, self.name)


def make_runtime(tmp_path, provider, policy, *, guard=RepeatedDenialPolicy(), store=None):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    for path in ("alpha.txt", "beta.txt"):
        (workspace / path).write_text("original evidence", encoding="utf-8")
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data", max_steps=8, repeated_denial_policy=guard),
        provider=provider,
        policies=(policy,),
        event_store=store or InMemoryEventStore(),
    )
    return runtime, workspace


@pytest.mark.asyncio
async def test_repeated_denial_warns_then_stops_without_effects(tmp_path):
    provider, policy = Calls(), Decisions()
    runtime, workspace = make_runtime(tmp_path, provider, policy)
    try:
        session = await runtime.create_session(workspace)
        result = await runtime.run_existing(session, "Read the requested evidence")
        assert (result.reason, result.steps) == ("stalled_repeated_denial", 3)
        assert policy.calls == 3  # Every denial underwent a fresh permission check.
        assert await runtime.sessions.read_effects(session) == ()
        assert any(
            "Do not repeat unchanged calls" in (m.content or "")
            for m in provider.requests[2].messages
        )
        assert "Read the requested evidence" in provider.requests[2].messages[-1].content
        assert not await runtime.check_invariants(session)
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, session)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["arguments", "allowed", "reason", "disabled", "batch"])
async def test_counterexamples_and_batch_counting(tmp_path, mode):
    deny = (DecisionKind.DENY, "access unavailable")
    allow = (DecisionKind.ALLOW, "permission granted")
    guard = RepeatedDenialPolicy()
    actions = None
    outcomes = None
    expected = ("stalled_repeated_denial", 3)
    if mode == "arguments":
        actions = [["alpha.txt"], ["beta.txt"]] * 3 + [None]
        expected = ("completed", 7)
    elif mode == "allowed":
        actions = [["alpha.txt"]] * 4 + [None]
        outcomes = [deny, deny, allow, allow]
        expected = ("completed", 5)
    elif mode == "reason":
        outcomes = [deny, deny] + [(DecisionKind.DENY, "different decision")] * 3
        expected = ("stalled_repeated_denial", 5)
    elif mode == "disabled":
        guard = None
        expected = ("max_steps_exceeded", 8)
    elif mode == "batch":
        actions = [["alpha.txt", "beta.txt"], ["beta.txt", "alpha.txt"], ["alpha.txt", "beta.txt"]]
    provider, policy = Calls(actions), Decisions(outcomes)
    runtime, workspace = make_runtime(tmp_path, provider, policy, guard=guard)
    try:
        session = await runtime.create_session(workspace)
        result = await runtime.run_existing(session, "Read evidence")
        assert (result.reason, result.steps) == expected
        effects = await runtime.sessions.read_effects(session)
        if mode == "allowed":
            assert sum(e.type == "effect/outcome" for e in effects) == 2
            events = await runtime.sessions.read_session(session)
            assert (
                sum(
                    e.type == "tool/result"
                    and e.data.get("status") == "succeeded"
                    and "original evidence" in e.data.get("content", "")
                    for e in events
                )
                == 2
            )
        else:
            assert not effects
        if mode == "batch":
            assert policy.calls == 6
        assert not await runtime.check_invariants(session)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_cancellation_converges_and_does_not_leak_streak_to_next_turn(tmp_path):
    provider, policy = Calls(gate_at=3), Decisions()
    store = SqliteEventStore(tmp_path / "events")
    runtime, workspace = make_runtime(tmp_path, provider, policy, store=store)
    try:
        session = await runtime.create_session(workspace)
        task = asyncio.create_task(runtime.run_existing(session, "Read evidence"))
        await asyncio.wait_for(provider.entered.wait(), 10)
        task.cancel()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert provider.settled.is_set()
        await RecoveryService(runtime.sessions).recover(session)
        await RecoveryService(runtime.sessions).recover(session)
        result = await runtime.run_existing(session, "Try this new turn")
        assert (result.reason, result.steps) == ("stalled_repeated_denial", 3)
        assert not await runtime.check_invariants(session)
        events = await runtime.sessions.read_session(session)
        start = next(e for e in events if e.type == "turn/start")
        ends = [
            e for e in events if e.type == "step/end" and e.data.get("reason") == "model_response"
        ]
        prefix = tuple(e for e in events if e.seq <= ends[1].seq)
        state = repeated_denial_state(prefix, turn_id=start.data["turn_id"])
        assert state is not None and state.count == 2
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, session)
    finally:
        await runtime.dispose()
        await store.aclose()
    reopened = SqliteEventStore(tmp_path / "events")
    from traceh.session.service import SessionService

    try:
        reread = await SessionService(reopened).read_session(session)
        assert (
            repeated_denial_state(
                tuple(e for e in reread if e.seq <= ends[1].seq), turn_id=start.data["turn_id"]
            )
            == state
        )
    finally:
        await reopened.aclose()


@pytest.mark.parametrize("warning,stop", [(1, 3), (2, 2), (True, 3), (2, 2.5)])
def test_invalid_thresholds(warning, stop):
    with pytest.raises(ValueError):
        RepeatedDenialPolicy(warning, stop)


def test_cli_configures_or_disables_guard():
    parser = build_parser()
    assert _repeated_denial_policy(parser.parse_args(["chat"])) == RepeatedDenialPolicy()
    assert _repeated_denial_policy(
        parser.parse_args(
            [
                "chat",
                "--denial-warn-after",
                "3",
                "--denial-stop-after",
                "5",
            ]
        )
    ) == RepeatedDenialPolicy(3, 5)
    assert (
        _repeated_denial_policy(parser.parse_args(["chat", "--disable-repeated-denial-check"]))
        is None
    )
    with pytest.raises(ValueError):
        _repeated_denial_policy(
            parser.parse_args(
                [
                    "chat",
                    "--disable-repeated-denial-check",
                    "--denial-warn-after",
                    "3",
                ]
            )
        )


@pytest.mark.asyncio
async def test_budget_continuation_preserves_stall_and_settles_steps(tmp_path):
    from traceh.agents import AgentRegistrar
    from traceh.api.agents import AgentSpec
    from traceh.api.budgets import BudgetLimits
    from traceh.budgets import BudgetEnforcement, BudgetLedgerService
    from traceh.runtime.continuation import DefaultContinuationRuntime

    store = InMemoryEventStore()
    service = BudgetLedgerService(store)
    enforcement = BudgetEnforcement(
        service,
        agent_id="guard-agent",
        session_id="guard-session",
        continuation=DefaultContinuationRuntime(),
    )
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data", max_steps=8),
        provider=Calls(),
        policies=(Decisions(),),
        event_store=store,
        continuation=enforcement.continuation,
        llm_runtime=enforcement.llm_runtime,
        tool_admission_gate=enforcement.tool_admission_gate,
    )
    try:
        await runtime.create_session(tmp_path, session_id="guard-session")
        await AgentRegistrar(store).create_agent(
            AgentSpec(preset="managed", workspace_id="guard-workspace"),
            request_id="create",
            agent_id="guard-agent",
            session_id="guard-session",
        )
        await service.grant_root(
            operation_id="grant",
            agent_id="guard-agent",
            limits=BudgetLimits(
                max_steps=8,
                max_tokens=None,
                max_tool_calls=None,
                max_wall_milliseconds=None,
                max_children=None,
                max_depth=None,
                max_processes=None,
            ),
        )
        result = await runtime.run_existing("guard-session", "Read evidence")
        assert (result.reason, result.steps) == ("stalled_repeated_denial", 3)
        account = (await service.ledger()).account("guard-agent")
        assert account.charged.steps == 3
        assert not await runtime.check_invariants("guard-session")
    finally:
        await runtime.dispose()
