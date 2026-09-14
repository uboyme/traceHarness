"""Public host admission against real Session records; no network calls."""

import asyncio
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
        events=(
            PendingEvent("turn/start", {"turn_id": "test-turn"}),
            PendingEvent("turn/end", {"turn_id": "test-turn", "reason": "completed"}),
        ),
    )
    # Keep one frozen host clock, but derive its epoch from the real clock:
    # BackgroundExperiment also enforces the original AO UTC deadline.
    now = datetime.now(UTC)
    period = period or BackgroundPeriod(
        "explicit-test-period",
        str(workspace.resolve()),
        fingerprint("source"),
        fingerprint("plan"),
        now + timedelta(hours=1),
        2,
        4,
        20000,
        10,
        1,
    )
    if period_changes:
        period = replace(period, **period_changes)
    host = BackgroundOptimizationHost(
        sessions,
        period,
        reservation=reservation or EpisodeReservation(2, 10000),
        execute=execute,
        clock=lambda: now,
    )
    await host.open()
    return host, session


async def test_background_feedback_runs_once_and_reopen_keeps_reservation(tmp_path):
    observed, finished = [], asyncio.Event()

    async def execute(identity, observations, seen, deadline):
        observed.extend(observations)
        finished.set()
        return EpisodeSettlement("explicit-test-evidence", None, False, True, True)

    host, session = await fixture(tmp_path, execute)
    await host.set_enabled(True)
    assert await host.observe(session, "test-turn", feedback="It did not read the evidence.")
    await finished.wait()
    await host.aclose()
    assert not await host.kick()
    assert len(observed) == 1 and observed[0].session_id == session
    assert not hasattr(observed[0], "case_id")  # A production user never becomes a gold label.
    reopened = BackgroundOptimizationHost(
        host.sessions,
        host.period,
        reservation=host.reservation,
        execute=execute,
        clock=host.clock,
    )
    state = await reopened.open()
    assert (state["episodes"], state["trials"], state["control_tokens"]) == (1, 2, 10000)
    assert not await reopened.observe(session, "test-turn", feedback="Duplicate report")
    assert not await reopened.kick()
    await reopened.aclose()


async def test_background_scope_and_enable_are_enforced_before_analysis(tmp_path):
    async def execute(*args):
        pytest.fail("rejected feedback must not call the model")

    host, session = await fixture(tmp_path, execute)
    with pytest.raises(ValueError, match="not-enabled"):
        await host.observe(session, "test-turn", feedback="Explicit feedback")
    await host.set_enabled(True)
    with pytest.raises(ValueError, match="not-completed"):
        await host.observe(session, "missing-turn", feedback="Explicit feedback")
    outside = tmp_path / "outside"
    outside.mkdir()
    foreign = await host.sessions.create_session(outside)
    await host.sessions.append_session(foreign, "turn/start", {"turn_id": "test-turn"})
    await host.sessions.append_session(foreign, "turn/end", {"turn_id": "test-turn"})
    with pytest.raises(ValueError, match="outside-scope"):
        await host.observe(foreign, "test-turn", feedback="Explicit feedback")
    assert (await host.view())["episodes"] == 0
    await host.aclose()


async def test_background_cancellation_waits_for_original_worker_cleanup(tmp_path):
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
    await host.observe(session, "test-turn", feedback="Read stopped prematurely")
    await started.wait()
    closing = asyncio.create_task(host.aclose())
    await cleaning.wait()
    assert not closing.done()
    repeated = asyncio.create_task(host.aclose())
    # Both close calls converge the same already-cancelled worker; neither
    # injects another cancellation into its resource cleanup.
    await asyncio.wait_for(host.view(), 5)
    release.set()
    await closing
    await repeated
    assert ended.is_set()
    state = await host.view()
    assert state["active"] is None and state["blocked"] == "cancelled-unsettled"
    assert state["control_tokens"] == 10000  # Incomplete does not mean free.
    await host.aclose()


async def test_background_two_hosts_cannot_start_two_experiments(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()
    executions = []

    async def execute(identity, *args):
        executions.append(identity)
        started.set()
        await release.wait()
        return EpisodeSettlement("explicit-evidence", None, False, True, True)

    first, session = await fixture(tmp_path, execute)
    second = BackgroundOptimizationHost(
        first.sessions,
        first.period,
        reservation=first.reservation,
        execute=execute,
        clock=first.clock,
    )
    await first.set_enabled(True)
    await first.observe(session, "test-turn", feedback="Evidence missing")
    await started.wait()
    assert not await second.kick()
    assert len(executions) == 1
    release.set()
    await first.aclose()
    await second.aclose()


async def test_background_changed_period_cannot_reset_budget_on_restart(tmp_path):
    async def execute(*args):
        pytest.fail("not triggered")

    host, _ = await fixture(tmp_path, execute)
    changed = BackgroundOptimizationHost(
        host.sessions,
        replace(host.period, experiment_settings_digest=fingerprint("other settings")),
        reservation=host.reservation,
        execute=execute,
    )
    with pytest.raises(ValueError, match="settings-mismatch"):
        await changed.open()
    await host.aclose()


async def test_background_runtime_observation_crosses_original_strategy_and_reopens(tmp_path):
    from test_control_model_calls import config, responder
    from test_strategy_optimization import setup

    from traceh.evolution.strategy import inspect_strategy_optimization, run_strategy_optimization

    runner, contract, _, _ = setup(tmp_path)
    done = asyncio.Event()

    async def execute(identity, observations, seen, deadline):
        await run_strategy_optimization(
            runner,
            contract,
            observations=observations,
            provider=responder('{"kind":"no_candidate","reason":"No supported change."}'),
            analysis_config=config(),
            output_dir=tmp_path / "actual-analysis",
            seen_candidate_digests=seen,
        )
        done.set()
        return EpisodeSettlement(str(tmp_path / "actual-analysis"), None, False, True, True)

    host, session = await fixture(tmp_path, execute)
    await host.set_enabled(True)
    await host.observe(session, "test-turn", feedback="Explicit user feedback; not a gold answer")
    await asyncio.wait_for(done.wait(), 30)
    await host.aclose()
    result = inspect_strategy_optimization(tmp_path / "actual-analysis")
    assert result["reason"] == "no-candidate" and result["cost"]["analysis"]["attempts"] == 1
    assert result["adoption_authorized"] is False


async def test_explicit_renewal_reopens_without_reusing_old_feedback(tmp_path):
    done = asyncio.Event()

    async def execute(*args):
        done.set()
        return EpisodeSettlement("original-evidence", None, False, True, True)

    host, session = await fixture(tmp_path, execute)
    await host.set_enabled(True)
    await host.observe(session, "test-turn", feedback="Feedback for this completed turn")
    await done.wait()
    await host.pause_work()
    await host.renew(host.clock() + timedelta(hours=2))
    assert not (await host.view())["enabled"]
    await host.set_enabled(True)
    assert not await host.observe(session, "test-turn", feedback="Re-sending old feedback")
    assert (await host.view())["episodes"] == 0
    reopened = BackgroundOptimizationHost(
        host.sessions,
        host.period,
        reservation=host.reservation,
        execute=execute,
        clock=host.clock,
    )
    assert (await reopened.open())["period"]["period_id"] == host.period.period_id
    await host.aclose()
    await reopened.aclose()


async def test_original_background_adapter_runs_candidate_in_independent_workers(tmp_path):
    import json

    from test_control_model_calls import config
    from test_strategy_optimization import setup

    from traceh.api.llm import ModelResponse, Usage, UsageQuality
    from traceh.evolution.background_experiment import (
        BackgroundExperiment,
        inspect_background_experiment,
    )
    from traceh.llm.scripted import ScriptedLlmProvider

    runner, contract, observations, proposal = setup(tmp_path)
    provider = ScriptedLlmProvider(
        tuple(
            ModelResponse(content=text, usage=Usage(30, 20, UsageQuality.EXACT))
            for text in (
                json.dumps(proposal),
                '{"status":"passed","reason":"Fixture evidence supports the answer."}',
                '{"status":"passed","reason":"Fixture evidence supports the answer."}',
            )
        )
    )
    adapter = BackgroundExperiment(
        runner,
        provider=provider,
        analysis_config=config(),
        judge_config=config(),
        output=tmp_path / "actual-experiments",
        timeout_seconds=120,
        selectors=tuple((e.file, e.selector) for e in contract.editable_text),
        max_request_bytes=200000,
    )
    settlement = await adapter("candidate-fixture", observations, (), contract.deadline_utc)
    assert settlement.converged and settlement.usage_known and settlement.candidate_digest
    assert inspect_background_experiment(settlement.evidence_path) == settlement
    # A broken OS ownership chain is a rejected comparison, not an empty successful run.
    process = next((Path(settlement.evidence_path) / "experiment").rglob("process.json"))
    raw = json.loads(process.read_text(encoding="utf-8"))
    raw["pid"], raw["owner_pid"] = -1, -2
    process.write_text(json.dumps(raw), encoding="utf-8")
    from traceh.chat.background import background_experiment_text

    assert not inspect_background_experiment(settlement.evidence_path).usage_known
    assert "评估证据尚不完整" in background_experiment_text(settlement.evidence_path)


async def test_distinct_feedback_runs_two_original_analysis_episodes(tmp_path):
    from test_control_model_calls import config
    from test_strategy_optimization import setup

    from traceh.api.llm import ModelResponse, Usage, UsageQuality
    from traceh.evaluation.model_evidence import load_model_call
    from traceh.evolution.background_experiment import BackgroundExperiment
    from traceh.llm.scripted import ScriptedLlmProvider

    runner, contract, _, _ = setup(tmp_path)
    provider = ScriptedLlmProvider(
        (ModelResponse(
            content='{"kind":"no_candidate","reason":"Insufficient supported change."}',
            usage=Usage(30, 20, UsageQuality.EXACT),
        ),),
        repeat_last=True,
    )
    output = tmp_path / "continuous-experiments"
    adapter = BackgroundExperiment(
        runner, provider=provider, analysis_config=config(), judge_config=config(),
        output=output, timeout_seconds=120,
        selectors=tuple((e.file, e.selector) for e in contract.editable_text),
        max_request_bytes=200000,
    )
    host, session = await fixture(
        tmp_path, adapter, reservation=adapter.reservation,
        period_changes={"max_control_tokens": adapter.reservation.control_tokens * 2},
    )
    await host.set_enabled(True)
    assert await host.observe(session, "test-turn", feedback="First unverified feedback")
    first = await host.wait_idle()
    assert first["blocked"] is None and first["episodes"] == 1
    previous = host.clock()
    host.clock = lambda: previous + timedelta(seconds=2)
    await host.sessions.append_session(session, "turn/start", {"turn_id": "another-turn"})
    await host.sessions.append_session(session, "turn/end", {"turn_id": "another-turn"})
    assert await host.observe(session, "another-turn", feedback="Different unverified feedback")
    second = await host.wait_idle()
    assert second["blocked"] is None and second["episodes"] == 2
    assert not await host.observe(session, "test-turn", feedback="Duplicate")
    calls = [load_model_call(path / "analysis") for path in output.iterdir()]
    assert len(calls) == len(provider.requests) == 2
    assert calls[0][0]["input"] != calls[1][0]["input"]
    await host.aclose()


async def test_automatic_signals_are_counts_without_tool_payloads(tmp_path):
    delivered, done = [], asyncio.Event()

    async def execute(identity, observations, seen, deadline):
        delivered.extend(observations)
        done.set()
        return EpisodeSettlement("evidence", None, False, True, True)

    host, session = await fixture(tmp_path, execute)
    await host.sessions.append_session(session, "turn/start", {"turn_id": "denial-turn"})
    await host.sessions.append_session(
        session,
        "tool/result",
        {
            "turn_id": "denial-turn",
            "status": "denied",
            "content": "PRIVATE-TOOL-PAYLOAD",
        },
    )
    await host.sessions.append_session(session, "turn/end", {"turn_id": "denial-turn"})
    await host.set_enabled(True)
    assert await host.observe_completed_turn(session, "denial-turn")
    await done.wait()
    await host.aclose()
    assert delivered[0].failure_class == "runtime-signal-unverified"
    assert "1 denied" in delivered[0].summary
    assert "PRIVATE-TOOL-PAYLOAD" not in repr(delivered)


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
    await host.observe(session, "test-turn", feedback="Feedback then immediate exit")
    # No yield to the experiment between admission and close.
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
        return EpisodeSettlement("evidence", None, False, True, True)

    first, session = await fixture(tmp_path, execute, sessions=SessionService(AdmissionGateStore()))
    await first.foreground(True)
    await first.set_enabled(True)
    await first.observe(session, "test-turn", feedback="One shared pending observation")
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


async def test_period_episode_limit_survives_new_feedback_and_host_restart(tmp_path):
    executions = []

    async def execute(identity, *args):
        executions.append(identity)
        return EpisodeSettlement("explicit-original-evidence", None, False, True, True)

    host, session = await fixture(tmp_path, execute, period_changes={"max_episodes": 1})
    await host.set_enabled(True)
    await host.observe(session, "test-turn", feedback="First feedback")
    await host.wait_idle()
    await host.aclose()
    reopened = BackgroundOptimizationHost(
        host.sessions, host.period, reservation=host.reservation, execute=execute,
        clock=lambda: host.clock() + timedelta(seconds=5),
    )
    await reopened.open()
    await host.sessions.append_session(session, "turn/start", {"turn_id": "second-turn"})
    await host.sessions.append_session(session, "turn/end", {"turn_id": "second-turn"})
    await reopened.set_enabled(True)
    assert await reopened.observe(session, "second-turn", feedback="New feedback after restart")
    await reopened.wait_idle()
    assert len(executions) == 1
    assert (await reopened.view())["episodes"] == 1
    await reopened.aclose()
