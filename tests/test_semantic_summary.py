"""D uses the ordinary Runtime, Session permit, accounting and recovery path."""

import asyncio
import json
from dataclasses import replace

import pytest

pytest.importorskip("tiktoken")

from traceh.api.llm import ModelResponse, Usage, UsageQuality
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.llm.token_meter import TokenBudgetPolicy
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.compaction import CompactionPolicy
from traceh.session.semantic_summary import SUMMARY_RESPONSE
from traceh.session.sqlite import SqliteEventStore


def summary_text(request):
    source = json.loads(request.messages[0].content)["source"]
    return json.dumps(
        {
            "goal": "Continue the offline parser work.",
            "constraints": ["Do not use the network."],
            "verified_progress": [],
            "reported_progress": ["An implementation was proposed."],
            "decisions": [],
            "open_questions": ["Validation is still pending."],
            "evidence": [
                {"source_seq": source[0]["source_seq"], "note": "Original user constraints."}
            ],
        }
    )


class SummaryProvider:
    name = "scripted"

    def __init__(self, *, change=None):
        self.requests = []
        self.change = change

    async def complete(self, request):
        self.requests.append(request)
        if "summary_input_seq" in request.metadata:
            response = ModelResponse(
                content=summary_text(request), usage=Usage(41, 12, UsageQuality.EXACT)
            )
            return self.change(response) if self.change else response
        return ModelResponse(
            content="Current question answered.", usage=Usage(31, 7, UsageQuality.EXACT)
        )


def config(root, **changes):
    return RuntimeConfig(
        data_dir=root,
        semantic_summary=True,
        token_budget=TokenBudgetPolicy("cl100k_base", 100_000, 2048, 1024, 1),
        compaction=CompactionPolicy(True, 10_000_000, 3000, 1),
        **changes,
    )


async def history(root):
    store = SqliteEventStore(root / "events")
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=root),
        event_store=store,
        provider=ScriptedLlmProvider(
            (ModelResponse(content="Proposed; untested."),), repeat_last=True
        ),
    )
    try:
        sid = await runtime.create_session(root)
        await runtime.run_existing(
            sid, "Work on an offline parser. Do not use the network. " + "Earlier detail. " * 450
        )
        await runtime.run_existing(sid, "Keep this recent question intact.")
        return sid, await runtime.sessions.read_session(sid)
    finally:
        await runtime.dispose()
        await store.aclose()


async def test_semantic_step_uses_normal_attempts_then_current_question_and_replays(tmp_path):
    from test_history_runtime import policy as history_policy

    sid, before = await history(tmp_path)
    provider = SummaryProvider()
    store = SqliteEventStore(tmp_path / "events")
    runtime = build_default_runtime(
        config(tmp_path, context_input=history_policy()), provider=provider, event_store=store
    )
    try:
        result = await runtime.run_existing(sid, "What should we verify next?")
        assert result.final_text == "Current question answered."
        assert len(provider.requests) == 2
        assert not provider.requests[0].tools
        assert provider.requests[0].metadata.get("summary_input_seq")
        assert not provider.requests[1].metadata.get("summary_input_seq")
        assert any(
            m.content == "What should we verify next?" for m in provider.requests[1].messages
        )
        assert any(
            m.content == "Keep this recent question intact." for m in provider.requests[1].messages
        )
        events = await runtime.sessions.read_session(sid)
        assert events[: len(before)] == before
        replacements = [e for e in events if e.type == "surface/replace"]
        assert len(replacements) == 1 and replacements[0].data["method"] == "semantic"
        response = next(e for e in events if e.type == SUMMARY_RESPONSE)
        assert replacements[0].causation_id == response.event_id
        assert "Validation is still pending" in replacements[0].data["summary"]
        assert result.usage.input_tokens == 72 and result.usage.output_tokens == 19
        assert not await runtime.check_invariants(sid)
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, sid)
        from traceh.tui.context_inspection import ContextInspectionReader
        from traceh.tui.presentation import compaction_notice_text

        view = await ContextInspectionReader(runtime.sessions).load(sid)
        assert view.request.actual_input_tokens == 31
        assert "模型语义摘要" in compaction_notice_text(replacements[0])
    finally:
        await runtime.dispose()
        await store.aclose()


@pytest.mark.parametrize("kind", ["empty", "json", "length", "tool", "source", "too-long"])
async def test_bad_model_summary_preserves_history_and_never_executes_tools(tmp_path, kind):
    from traceh.api.llm import ToolCall

    sid, before = await history(tmp_path)

    def change(response):
        if kind == "empty":
            return replace(response, content="")
        if kind == "json":
            return replace(response, content="not json")
        if kind == "length":
            return replace(response, finish_reason="length")
        if kind == "tool":
            return replace(
                response,
                tool_calls=(ToolCall("forbidden", "shell", {"command": "echo forbidden"}),),
            )
        value = json.loads(response.content)
        if kind == "source":
            value["evidence"][0]["source_seq"] = 999999
        else:
            value["goal"] = "too much " * 3000
        return replace(response, content=json.dumps(value))

    provider = SummaryProvider(change=change)
    store = SqliteEventStore(tmp_path / "events")
    runtime = build_default_runtime(config(tmp_path), provider=provider, event_store=store)
    try:
        result = await runtime.run_existing(sid, "A current question.")
        assert result.final_text == "Current question answered."
        events = await runtime.sessions.read_session(sid)
        assert events[: len(before)] == before
        assert not any(e.type in {"surface/replace", "tool/call"} for e in events)
        assert any(e.type == "surface/compaction-failed" for e in events)
        assert len(provider.requests) == 2
        assert not await runtime.check_invariants(sid)
    finally:
        await runtime.dispose()
        await store.aclose()


async def test_double_cancel_summary_provider_converges_without_replacement(tmp_path):
    sid, _ = await history(tmp_path)
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Provider(SummaryProvider):
        async def complete(self, request):
            self.requests.append(request)
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
                raise

    provider = Provider()
    store = SqliteEventStore(tmp_path / "events")
    runtime = build_default_runtime(config(tmp_path), provider=provider, event_store=store)
    task = None
    try:
        task = asyncio.create_task(runtime.run_existing(sid, "A pending question."))
        await asyncio.wait_for(entered.wait(), 10)
        task.cancel()
        await asyncio.wait_for(cancelled.wait(), 10)
        task.cancel()
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        events = await runtime.sessions.read_session(sid)
        assert len(provider.requests) == 1
        assert not any(e.type == "surface/replace" for e in events)
        assert not await runtime.check_invariants(sid)
    finally:
        release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await runtime.dispose()
        await store.aclose()


@pytest.mark.parametrize("capacity", [0, 10000])
async def test_summary_obeys_the_same_budget_ledger_before_dispatch(tmp_path, capacity):
    from test_budget_enforcement import limits

    from traceh.agents import AgentRegistrar
    from traceh.api.agents import AgentSpec
    from traceh.budgets import BudgetedLlmRuntime, BudgetExhaustedError, BudgetLedgerService

    sid, _ = await history(tmp_path)
    store = SqliteEventStore(tmp_path / "events")
    await AgentRegistrar(store).create_agent(
        AgentSpec(preset="managed", workspace_id="fixture-workspace"),
        request_id="fixture-create",
        agent_id="summary-owner",
        session_id=sid,
    )
    budget = BudgetLedgerService(store)
    await budget.grant_root(
        operation_id="fixture-grant", agent_id="summary-owner", limits=limits(max_tokens=capacity)
    )
    provider = SummaryProvider()
    runtime = build_default_runtime(
        config(tmp_path),
        provider=provider,
        event_store=store,
        llm_runtime=BudgetedLlmRuntime(budget, agent_id="summary-owner", session_id=sid),
    )
    try:
        if capacity == 0:
            with pytest.raises(BudgetExhaustedError):
                await runtime.run_existing(sid, "Continue within the budget.")
            assert not provider.requests
        else:
            await runtime.run_existing(sid, "Continue within the budget.")
            assert len(provider.requests) == 2
        account = (await budget.ledger()).account("summary-owner")
        assert account.charged.tokens == (91 if capacity else 0)
        assert account.reserved.tokens == 0
        assert not await runtime.check_invariants(sid)
    finally:
        await runtime.dispose()
        await store.aclose()


@pytest.mark.parametrize("failure", ["before", "after", "race", "cancel"])
async def test_semantic_commit_faults_reconcile_without_repeating_model(
    tmp_path, monkeypatch, failure
):
    sid, _ = await history(tmp_path)
    provider = SummaryProvider()
    store = SqliteEventStore(tmp_path / "events")
    runtime = build_default_runtime(config(tmp_path), provider=provider, event_store=store)
    original = runtime.sessions.append_session
    entered, release = asyncio.Event(), asyncio.Event()
    injected = False

    async def append(sid, kind, data, **kwargs):
        nonlocal injected
        if kind == "surface/replace" and data["method"] == "semantic" and not injected:
            injected = True
            if failure == "before":
                raise OSError("before commit")
            if failure == "race":
                await original(sid, "fixture/race", {})
            event = await original(sid, kind, data, **kwargs)
            entered.set()
            if failure == "cancel":
                await release.wait()
            elif failure == "after":
                raise OSError("after commit")
            return event
        return await original(sid, kind, data, **kwargs)

    monkeypatch.setattr(runtime.sessions, "append_session", append)
    task = None
    try:
        task = asyncio.create_task(runtime.run_existing(sid, "A current question."))
        if failure == "cancel":
            await asyncio.wait_for(entered.wait(), 10)
            task.cancel()
            task.cancel()
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
        events = await runtime.sessions.read_session(sid)
        assert sum(e.type == "surface/replace" for e in events) == (0 if failure == "before" else 1)
        assert len([r for r in provider.requests if "summary_input_seq" in r.metadata]) == 1
        assert not await runtime.check_invariants(sid)
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, sid)
    finally:
        release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await runtime.dispose()
        await store.aclose()


async def test_forged_semantic_replacement_and_wrong_response_cause_are_rejected(tmp_path):
    sid, _ = await history(tmp_path)
    store = SqliteEventStore(tmp_path / "events")
    runtime = build_default_runtime(config(tmp_path), provider=SummaryProvider(), event_store=store)
    try:
        await runtime.run_existing(sid, "Continue.")
        events = await runtime.sessions.read_session(sid)
        replacement = next(e for e in events if e.type == "surface/replace")
        from traceh.session.semantic_summary import validate_summary_events

        validate_summary_events(events)
        for forged in (
            replace(replacement, causation_id=None),
            replace(replacement, data={**replacement.data, "summary": "Invented completion."}),
        ):
            with pytest.raises(ValueError, match="semantic-replacement"):
                validate_summary_events(
                    tuple(forged if e.seq == replacement.seq else e for e in events)
                )
    finally:
        await runtime.dispose()
        await store.aclose()


async def test_explicit_history_selection_keeps_first_step_priority(tmp_path):
    from test_history_runtime import page_request, policy

    from traceh.api.turns import TurnInput

    sid, _ = await history(tmp_path)
    store = SqliteEventStore(tmp_path / "events")
    provider = SummaryProvider()
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path, context_input=policy()),
        provider=provider,
        event_store=store,
    )
    try:
        events = await runtime.sessions.read_session(sid)
        await runtime.compaction.replace_through(
            sid, through_seq=events[-1].seq, summary="Earlier parser discussion."
        )
        await runtime.run_existing(sid, "Show available history.")
        selected = page_request(provider.requests[-1])
    finally:
        await runtime.dispose()
    provider = SummaryProvider()
    runtime = build_default_runtime(
        config(tmp_path, context_input=policy()), provider=provider, event_store=store
    )
    try:
        result = await runtime.run_existing(
            sid,
            TurnInput(
                content="Read the selected evidence.",
                message_id="fixture-selection",
                source="explicit-host",
                history_requests=(selected,),
            ),
        )
        assert result.steps == 1 and len(provider.requests) == 1
        assert "summary_input_seq" not in provider.requests[0].metadata
        events = await runtime.sessions.read_session(sid)
        context = next(
            e for e in events if e.type == "context/input" and e.data["turn_id"] == result.turn_id
        )
        assert any(
            b["tier"] == "chunk" and "offline parser" in b["body"] for b in context.data["blocks"]
        )
        assert not await runtime.check_invariants(sid)
    finally:
        await runtime.dispose()
        await store.aclose()


@pytest.mark.parametrize("cut_kind", ["model/attempt-start", SUMMARY_RESPONSE, "surface/replace"])
async def test_crash_prefix_recovers_summary_without_calling_model_or_inventing_usage(
    tmp_path, cut_kind
):
    from dataclasses import fields

    from traceh.api.events import PendingEvent
    from traceh.session.event_store import InMemoryEventStore
    from traceh.session.invariants import CoreInvariantChecker
    from traceh.session.recovery import RecoveryService
    from traceh.session.service import SessionService
    from traceh.tui.context_inspection import ContextInspectionReader

    sid, _ = await history(tmp_path)
    store = SqliteEventStore(tmp_path / "events")
    provider = SummaryProvider()
    runtime = build_default_runtime(config(tmp_path), provider=provider, event_store=store)
    try:
        await runtime.run_existing(sid, "Continue.")
        events = await runtime.sessions.read_session(sid)
        summary = next(e for e in events if e.type == "summary/input")
        cut = next(e.seq for e in events if e.seq > summary.seq and e.type == cut_kind)
        recovered_store = InMemoryEventStore()
        await recovered_store.append(
            f"session:{sid}",
            expected_seq=0,
            events=tuple(
                PendingEvent(**{f.name: getattr(e, f.name) for f in fields(PendingEvent)})
                for e in events
                if e.seq <= cut
            ),
        )
        sessions = SessionService(recovered_store)
        view = await ContextInspectionReader(sessions).load(sid)
        assert view.request.purpose == "semantic_summary"
        calls = len(provider.requests)
        report = await RecoveryService(sessions).recover(sid)
        assert report.changed
        after = await sessions.read_session(sid)
        assert len(provider.requests) == calls
        assert sum(e.type == "surface/replace" for e in after) == (cut_kind == "surface/replace")
        if cut_kind != "surface/replace":
            end = next(e for e in after if e.type == "model/attempt-end" and e.seq > cut)
            assert end.data["status"] == (
                "succeeded" if cut_kind == SUMMARY_RESPONSE else "unknown_after_crash"
            )
            assert "usage" not in end.data
        assert not CoreInvariantChecker().check(after)
        assert not (await RecoveryService(sessions).recover(sid)).changed
    finally:
        await runtime.dispose()
        await store.aclose()


@pytest.mark.parametrize("exhaust", [False, True])
async def test_summary_transport_retry_uses_same_request_and_exhaustion_keeps_history(
    tmp_path, exhaust
):
    from traceh.llm.failures import ProviderFailure, ProviderFailureCategory
    from traceh.llm.retry import ModelRetryPolicy

    sid, _ = await history(tmp_path)

    class Provider(SummaryProvider):
        async def complete(self, request):
            if "summary_input_seq" in request.metadata and (exhaust or not self.requests):
                self.requests.append(request)
                raise ProviderFailure("provider-timeout", ProviderFailureCategory.TIMEOUT)
            return await super().complete(request)

    provider = Provider()
    store = SqliteEventStore(tmp_path / "events")
    runtime = build_default_runtime(
        config(
            tmp_path,
            model_retry_policy=ModelRetryPolicy(
                max_attempts=2,
                max_elapsed_seconds=30,
                base_delay_seconds=0.001,
                max_delay_seconds=0.001,
                retry_after_cap_seconds=0.001,
                jitter_ratio=0,
            ),
        ),
        provider=provider,
        event_store=store,
    )
    try:
        if exhaust:
            with pytest.raises(ProviderFailure):
                await runtime.run_existing(sid, "Continue.")
        else:
            await runtime.run_existing(sid, "Continue.")
        assert provider.requests[0].to_dict() == provider.requests[1].to_dict()
        events = await runtime.sessions.read_session(sid)
        assert sum(e.type == "surface/replace" for e in events) == (not exhaust)
        assert not await runtime.check_invariants(sid)
    finally:
        await runtime.dispose()
        await store.aclose()
