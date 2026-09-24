"""Public host admission against real Session records; no network calls (ADR-0082)."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from traceh.api.events import PendingEvent
from traceh.api.json_types import fingerprint
from traceh.evolution.background import (
    BackgroundOptimizationHost,
    BackgroundPeriod,
    EpisodeReservation,
    EpisodeSettlement,
)
from traceh.session.event_store import InMemoryEventStore
from traceh.session.service import SessionService

TURNS = ("turn-a", "turn-b", "turn-c", "turn-d")


def settled(evidence="explicit-test-evidence", candidate=None):
    return EpisodeSettlement(evidence, candidate, True, True)


async def fixture(
    tmp_path, execute, *, sessions=None, period=None, period_changes=None, reservation=None
):
    sessions = sessions or SessionService(InMemoryEventStore())
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    session = await sessions.create_session(workspace)
    stream = sessions.session_stream(session)
    await sessions.store.append(
        stream,
        expected_seq=await sessions.store.head(stream),
        events=tuple(
            PendingEvent(kind, {"turn_id": turn, "reason": "completed"})
            for turn in TURNS
            for kind in ("turn/start", "turn/end")
        ),
    )
    # Keep one frozen host clock, but derive its epoch from the real clock:
    # the proposal adapter also enforces the original AO UTC deadline.
    now = datetime.now(UTC)
    period = period or BackgroundPeriod(
        "explicit-test-period",
        str(workspace.resolve()),
        fingerprint("source"),
        fingerprint("plan"),
        now + timedelta(hours=1),
        2,
        20000,
        10,
        1,
    )
    if period_changes:
        period = replace(period, **period_changes)
    host = BackgroundOptimizationHost(
        sessions,
        period,
        reservation=reservation or EpisodeReservation(10000),
        execute=execute,
        clock=lambda: now,
    )
    await host.open()
    return host, session


async def two_sources(host, session, first="turn-a", second="turn-b"):
    """One mechanism reported from two distinct completed Turns."""
    one = await host.observe(session, first, feedback="It did not read the evidence.")
    two = await host.observe(session, second, feedback="It answered before reading.")
    return one, two


def later(host, seconds=5):
    now = host.clock()
    host.clock = lambda: now + timedelta(seconds=seconds)


async def test_one_source_waits_and_a_second_source_admits_one_suggestion(tmp_path):
    observed, finished = [], asyncio.Event()

    async def execute(identity, observations, seen, deadline):
        observed.extend(observations)
        finished.set()
        return settled()

    host, session = await fixture(tmp_path, execute)
    await host.set_enabled(True)
    assert await host.observe(session, "turn-a", feedback="It did not read the evidence.")
    state = await host.view()
    # A single occurrence is not a mechanism yet; no model call is spent on it.
    assert state["episodes"] == 0 and not finished.is_set()
    assert await host.observe(session, "turn-b", feedback="It answered before reading.")
    await finished.wait()
    await host.aclose()
    assert len(observed) == 2 and {o.turn_id for o in observed} == {"turn-a", "turn-b"}
    assert not any(hasattr(o, "case_id") for o in observed)  # Never a gold label.
    reopened = BackgroundOptimizationHost(
        host.sessions,
        host.period,
        reservation=host.reservation,
        execute=execute,
        clock=host.clock,
    )
    state = await reopened.open()
    assert (state["episodes"], state["control_tokens"]) == (1, 10000)
    assert not await reopened.observe(session, "turn-a", feedback="Duplicate report")
    assert not await reopened.kick()
    await reopened.aclose()


async def test_background_scope_and_enable_are_enforced_before_analysis(tmp_path):
    async def execute(*args):
        pytest.fail("rejected feedback must not call the model")

    host, session = await fixture(tmp_path, execute)
    with pytest.raises(ValueError, match="not-enabled"):
        await host.observe(session, "turn-a", feedback="Explicit feedback")
    await host.set_enabled(True)
    with pytest.raises(ValueError, match="not-completed"):
        await host.observe(session, "missing-turn", feedback="Explicit feedback")
    outside = tmp_path / "outside"
    outside.mkdir()
    foreign = await host.sessions.create_session(outside)
    await host.sessions.append_session(foreign, "turn/start", {"turn_id": "turn-a"})
    await host.sessions.append_session(foreign, "turn/end", {"turn_id": "turn-a"})
    with pytest.raises(ValueError, match="outside-scope"):
        await host.observe(foreign, "turn-a", feedback="Explicit feedback")
    assert (await host.view())["episodes"] == 0
    await host.aclose()


async def test_cancellation_waits_for_cleanup_blocks_and_needs_a_human_to_clear(tmp_path):
    started, cleaning, release, ended = (asyncio.Event() for _ in range(4))

    async def execute(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            ended.set()

    host, session = await fixture(tmp_path, execute)
    await host.set_enabled(True)
    await two_sources(host, session)
    await started.wait()
    closing = asyncio.create_task(host.pause_work())
    await cleaning.wait()
    assert not closing.done()
    repeated = asyncio.create_task(host.pause_work())
    # Both converge the same already-cancelled worker; neither injects another
    # cancellation into its resource cleanup.
    await asyncio.wait_for(host.view(), 5)
    release.set()
    await closing
    await repeated
    assert ended.is_set() and host._worker.cancelled()  # Cancellation was not swallowed.
    state = await host.view()
    assert state["active"] is None and state["blocked"] == "cancelled-unsettled"
    assert state["control_tokens"] == 10000  # Incomplete does not mean free.
    with pytest.raises(ValueError, match="unsettled"):
        await host.renew(host.clock() + timedelta(hours=2))
    with pytest.raises(ValueError, match="acknowledgement-invalid"):
        await host.acknowledge_blocked("   ")
    await host.acknowledge_blocked("Checked the original call: it never reached the provider.")
    state = await host.view()
    assert state["blocked"] is None and state["control_tokens"] == 10000
    await host.aclose()


async def test_a_failed_suggestion_blocks_until_acknowledged_then_work_resumes(tmp_path):
    calls = []

    async def execute(identity, *args):
        calls.append(identity)
        if len(calls) == 1:
            raise RuntimeError("provider-tls-eof")
        return settled()

    host, session = await fixture(tmp_path, execute)
    await host.set_enabled(True)
    await two_sources(host, session)
    state = await host.wait_idle()
    assert state["blocked"] == "suggestion-failed"
    later(host)
    await two_sources(host, session, "turn-c", "turn-d")
    assert len(calls) == 1  # Blocked: new findings are recorded, not acted on.
    await host.acknowledge_blocked("Provider outage confirmed; no partial result kept.")
    state = await host.wait_idle()
    assert len(calls) == 2 and state["blocked"] is None and state["episodes"] == 2
    await host.aclose()


async def test_work_observed_during_a_foreground_operation_waits_for_the_idle_kick(tmp_path):
    started = asyncio.Event()

    async def execute(*args):
        started.set()
        return settled()

    host, session = await fixture(tmp_path, execute)
    await host.set_enabled(True)
    await host.foreground(True)
    await two_sources(host, session)
    assert not started.is_set() and (await host.view())["episodes"] == 0
    assert not await host.kick()  # Foreground work always wins.
    await host.foreground(False)
    # The TUI's idle pulse is the owner of resuming; it calls the same kick.
    assert await host.kick()
    await asyncio.wait_for(started.wait(), 5)
    assert (await host.wait_idle())["episodes"] == 1
    await host.aclose()


async def test_background_two_hosts_cannot_start_two_suggestions(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()
    executions = []

    async def execute(identity, *args):
        executions.append(identity)
        started.set()
        await release.wait()
        return settled()

    first, session = await fixture(tmp_path, execute)
    second = BackgroundOptimizationHost(
        first.sessions,
        first.period,
        reservation=first.reservation,
        execute=execute,
        clock=first.clock,
    )
    await first.set_enabled(True)
    await two_sources(first, session)
    await started.wait()
    assert not await second.kick()
    assert len(executions) == 1
    release.set()
    await first.aclose()
    await second.aclose()


async def test_changed_settings_make_the_period_stale_until_a_new_one_is_approved(tmp_path):
    executions = []

    async def execute(identity, *args):
        executions.append(identity)
        return settled()

    host, session = await fixture(tmp_path, execute)
    await host.set_enabled(True)
    await two_sources(host, session)
    await host.wait_idle()
    await host.aclose()
    changed = BackgroundOptimizationHost(
        host.sessions,
        replace(host.period, baseline_digest=fingerprint("source after adoption")),
        reservation=host.reservation,
        execute=execute,
        clock=lambda: host.clock() + timedelta(seconds=5),
    )
    state = await changed.open()
    # The stream stays readable and keeps its quota; nothing new is admitted.
    assert state["stale"] and state["episodes"] == 1
    await two_sources(changed, session, "turn-c", "turn-d")
    assert len(executions) == 1
    await changed.renew(changed.clock() + timedelta(hours=2))
    state = await changed.view()
    assert not state["stale"] and state["period"]["baseline_digest"] == fingerprint(
        "source after adoption"
    )
    # The new period is disabled until a person enables it, and old findings stay consumed.
    await changed.set_enabled(True)
    assert not await changed.observe(session, "turn-c", feedback="Re-sent finding")
    await changed.aclose()


async def test_schema_one_background_streams_are_refused(tmp_path):
    async def execute(*args):
        pytest.fail("not triggered")

    sessions = SessionService(InMemoryEventStore())
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    stream = "optimization-background:" + fingerprint(str(workspace))
    await sessions.store.append(
        stream,
        expected_seq=0,
        events=(PendingEvent("optimization-background/enabled", {"enabled": True}),),
    )
    with pytest.raises(ValueError, match="background-protocol-unsupported"):
        await fixture(tmp_path, execute, sessions=sessions)


async def test_automatic_signals_are_counts_without_tool_payloads(tmp_path):
    delivered, done = [], asyncio.Event()

    async def execute(identity, observations, seen, deadline):
        delivered.extend(observations)
        done.set()
        return settled()

    host, session = await fixture(tmp_path, execute)
    for turn in ("denial-one", "denial-two"):
        await host.sessions.append_session(session, "turn/start", {"turn_id": turn})
        await host.sessions.append_session(
            session,
            "tool/result",
            {"turn_id": turn, "status": "denied", "content": "PRIVATE-TOOL-PAYLOAD"},
        )
        await host.sessions.append_session(session, "turn/end", {"turn_id": turn})
    await host.set_enabled(True)
    assert await host.observe_completed_turn(session, "denial-one")
    assert not done.is_set()
    assert await host.observe_completed_turn(session, "denial-two")
    await done.wait()
    await host.aclose()
    assert {o.failure_class for o in delivered} == {"tool-denied"}
    assert all(o.summary.startswith("1 tool calls were denied") for o in delivered)
    assert "PRIVATE-TOOL-PAYLOAD" not in repr(delivered)
    # A clean Turn produces no finding at all.
    assert not await host.observe_completed_turn(session, "turn-a")


async def test_close_immediately_after_admission_still_settles_owned_episode(tmp_path):
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def execute(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    host, session = await fixture(tmp_path, execute)
    await host.set_enabled(True)
    await two_sources(host, session)
    # No yield to the suggestion between admission and close.
    await host.aclose()
    assert entered.is_set() and cleaned.is_set()
    assert (await host.view())["active"] is None


async def test_competing_admissions_linearize_at_original_store_compare_and_append(tmp_path):
    class AdmissionGateStore(InMemoryEventStore):
        def __init__(self):
            super().__init__()
            self.arrivals = 0
            self.both = asyncio.Event()

        async def append(self, stream_id, *, expected_seq, events, **kwargs):
            if any(e.type == "optimization-background/admitted" for e in events):
                self.arrivals += 1
                if self.arrivals == 2:
                    self.both.set()
                await self.both.wait()
            return await super().append(
                stream_id, expected_seq=expected_seq, events=events, **kwargs
            )

    executions = []
    release = asyncio.Event()

    async def execute(identity, *args):
        executions.append(identity)
        await release.wait()
        return settled()

    first, session = await fixture(tmp_path, execute, sessions=SessionService(AdmissionGateStore()))
    await first.foreground(True)
    await first.set_enabled(True)
    await two_sources(first, session)
    second = BackgroundOptimizationHost(
        first.sessions,
        first.period,
        reservation=first.reservation,
        execute=execute,
        clock=first.clock,
    )
    await first.foreground(False)
    admitted = await asyncio.wait_for(asyncio.gather(first.kick(), second.kick()), 5)
    assert sorted(admitted) == [False, True]
    release.set()
    await asyncio.gather(first.wait_idle(), second.wait_idle())
    assert len(executions) == 1 and (await first.view())["episodes"] == 1
    await first.aclose()
    await second.aclose()


async def test_period_episode_limit_survives_new_findings_and_host_restart(tmp_path):
    executions = []

    async def execute(identity, *args):
        executions.append(identity)
        return settled()

    host, session = await fixture(tmp_path, execute, period_changes={"max_episodes": 1})
    await host.set_enabled(True)
    await two_sources(host, session)
    await host.wait_idle()
    await host.aclose()
    reopened = BackgroundOptimizationHost(
        host.sessions,
        host.period,
        reservation=host.reservation,
        execute=execute,
        clock=lambda: host.clock() + timedelta(seconds=5),
    )
    await reopened.open()
    await reopened.set_enabled(True)
    assert all(await two_sources(reopened, session, "turn-c", "turn-d"))
    await reopened.wait_idle()
    assert len(executions) == 1 and (await reopened.view())["episodes"] == 1
    await reopened.aclose()


async def test_a_pending_suggestion_holds_new_work_until_dismissed(tmp_path):
    executions = []

    async def execute(identity, *args):
        executions.append(identity)
        return settled(candidate=fingerprint(identity))

    host, session = await fixture(tmp_path, execute)
    await host.set_enabled(True)
    await two_sources(host, session)
    state = await host.wait_idle()
    assert state["pending_review"] == executions[0]
    later(host)
    await two_sources(host, session, "turn-c", "turn-d")
    assert len(executions) == 1
    await host.dismiss(executions[0])
    state = await host.wait_idle()
    assert len(executions) == 2 and fingerprint(executions[0]) in state["candidates"]
    await host.aclose()


def proposal_adapter(tmp_path, response, output):
    from test_control_model_calls import config
    from test_strategy_optimization import setup

    from traceh.api.llm import ModelResponse, Usage, UsageQuality
    from traceh.evolution.background_proposal import BackgroundProposal
    from traceh.llm.scripted import ScriptedLlmProvider

    runner, contract, observations, proposal = setup(tmp_path)
    provider = ScriptedLlmProvider(
        (
            ModelResponse(
                content=json.dumps(proposal) if response is None else response,
                usage=Usage(30, 20, UsageQuality.EXACT),
            ),
        ),
        repeat_last=True,
    )
    adapter = BackgroundProposal(
        runner,
        provider=provider,
        analysis_config=config(),
        output=output,
        timeout_seconds=120,
        selectors=tuple((e.file, e.selector) for e in contract.editable_text),
        max_request_bytes=200000,
    )
    return adapter, contract, observations, provider


async def test_the_proposal_adapter_suggests_without_running_any_trial(tmp_path):
    from traceh.chat.background import background_proposal_text
    from traceh.evolution.background_proposal import inspect_background_proposal

    adapter, contract, observations, provider = proposal_adapter(
        tmp_path, None, tmp_path / "suggestions"
    )
    settlement = await adapter("suggestion-fixture", observations, (), contract.deadline_utc)
    root = Path(settlement.evidence_path)
    assert settlement.converged and settlement.usage_known and settlement.candidate_digest
    assert len(provider.requests) == 1 and not (root / "experiment").exists()
    assert inspect_background_proposal(root) == settlement
    text = background_proposal_text(root)
    assert "没有自动采用或评测任何修改" in text and str(root / "candidate.json") in text
    # An edited suggestion is refused on reopening, not shown as the model's own.
    patch = json.loads((root / "candidate.json").read_text(encoding="utf-8"))
    patch["edits"][0]["new_text"] = "Edited after the fact."
    (root / "candidate.json").write_text(json.dumps(patch), encoding="utf-8")
    with pytest.raises(ValueError):
        inspect_background_proposal(root)


async def test_two_clusters_run_two_original_analyses_with_distinct_inputs(tmp_path):
    from traceh.evaluation.model_evidence import load_model_call

    output = tmp_path / "continuous-suggestions"
    adapter, _, _, provider = proposal_adapter(
        tmp_path, '{"kind":"no_candidate","reason":"Insufficient supported change."}', output
    )
    host, session = await fixture(
        tmp_path,
        adapter,
        reservation=adapter.reservation,
        period_changes={"max_control_tokens": adapter.reservation.control_tokens * 2},
    )
    await host.set_enabled(True)
    await two_sources(host, session)
    first = await host.wait_idle()
    assert first["blocked"] is None and first["episodes"] == 1
    later(host)
    await two_sources(host, session, "turn-c", "turn-d")
    second = await host.wait_idle()
    assert second["blocked"] is None and second["episodes"] == 2
    calls = [load_model_call(path / "analysis") for path in output.iterdir()]
    assert len(calls) == len(provider.requests) == 2
    assert calls[0][0]["input"] != calls[1][0]["input"]
    await host.aclose()


async def test_a_foreground_operation_lets_a_running_suggestion_finish(tmp_path):
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def execute(*args):
        started.set()
        await release.wait()
        finished.set()
        return settled()

    host, session = await fixture(tmp_path, execute)
    await host.set_enabled(True)
    await two_sources(host, session)
    await started.wait()
    await asyncio.wait_for(host.foreground(True), 5)  # Returns without cancelling it.
    assert not host._worker.done()
    release.set()
    state = await host.wait_idle()
    # Finished normally: no unknown usage, so nothing is blocked for a human check.
    assert finished.is_set() and state["blocked"] is None and state["episodes"] == 1
    await host.foreground(False)
    await host.aclose()
