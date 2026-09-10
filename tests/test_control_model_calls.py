"""AO-2 control models execute real Runtime/Budget/SQLite paths, no external API."""

import asyncio
import json
from dataclasses import replace

import pytest

from traceh.api.llm import ModelResponse, Usage, UsageQuality
from traceh.evaluation.model_evidence import load_model_call, model_events
from traceh.evaluation.model_service import ModelCallConfig, run_model_call
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.llm.token_meter import RequestTokenBudgetExceeded
from traceh.runtime.agent_runtime import AgentRuntime
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.service import SessionService
from traceh.session.sqlite import SqliteEventStore
from traceh.session.surface import SurfaceProjector


def config(**changes):
    return replace(
        ModelCallConfig(
            "scripted",
            "control-fixture",
            0.0,
            "cl100k_base",
            16000,
            3000,
            500,
            30,
            "fixture-connection",
        ),
        **changes,
    )


def responder(text='{"status":"passed","reason":"Evidence supports the answer."}', *, usage=None):
    return ScriptedLlmProvider(
        [ModelResponse(content=text, usage=usage or Usage(20, 10, UsageQuality.EXACT))]
    )


async def invoke(root, provider=None, **changes):
    return await run_model_call(
        provider=provider or responder(),
        config=config(**changes),
        system="Evaluate this explicit test fixture.",
        input_text="An explicit request.",
        binding={"purpose": "fixture"},
        output_dir=root,
    )


async def test_control_call_has_original_identity_budget_and_independent_replay(tmp_path):
    result = await invoke(tmp_path / "call")
    definition, original, _ = load_model_call(tmp_path / "call")
    assert original == result and original["converged"]
    observed = original["observation"]
    assert observed["requests"] == 1 and observed["usage"]["total_tokens"] == 30
    assert observed["budget"]["charged"]["tokens"] == 30
    assert observed["budget"]["charged"]["tool_calls"] == 0
    events = model_events(tmp_path / "call")
    snapshot = next(e for e in events if e.type == "request/snapshot")
    assert snapshot.data["dispatch_request"]["tools"] == []
    store = SqliteEventStore(tmp_path / "call/data")
    try:
        assert not await verify_request_snapshots(
            SessionService(store), SurfaceProjector(), definition["session_id"]
        )
    finally:
        await store.aclose()


async def test_tampered_call_input_cannot_rebind_real_response(tmp_path):
    await invoke(tmp_path / "call")
    path = tmp_path / "call/call.json"
    data = json.loads(path.read_text())
    data["input"] = "Different question."
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="evidence-mismatch"):
        load_model_call(tmp_path / "call")


async def test_input_over_budget_stops_before_provider_and_retains_failure(tmp_path):
    provider = responder()
    with pytest.raises(RequestTokenBudgetExceeded):
        await invoke(
            tmp_path / "call", provider, token_limit=200, output_tokens=50, safety_tokens=10
        )
    _, receipt, _ = load_model_call(tmp_path / "call")
    assert receipt["errors"] and not receipt["observation"]["completed"]
    assert receipt["observation"]["usage"]["attempts"] == 0
    assert not any(e.type == "model/attempt-start" for e in model_events(tmp_path / "call"))


async def test_unknown_usage_is_not_zero(tmp_path):
    await invoke(tmp_path / "call", responder(usage=Usage(0, 0, UsageQuality.UNKNOWN)))
    _, receipt, _ = load_model_call(tmp_path / "call")
    assert receipt["observation"]["usage"]["total_tokens"] is None
    assert receipt["observation"]["budget"]["charged"]["tokens"] == 16000


async def test_reported_usage_above_grant_cannot_qualify(tmp_path):
    await invoke(tmp_path / "call", responder(usage=Usage(17000, 2, UsageQuality.EXACT)))
    _, receipt, _ = load_model_call(tmp_path / "call")
    assert receipt["observation"]["usage"]["total_tokens"] == 17002
    assert not receipt["observation"]["budget_usage_exact"]


async def test_actual_failure_and_cleanup_failure_are_both_retained(tmp_path, monkeypatch):
    original = AgentRuntime.dispose

    async def fail_after_close(runtime):
        await original(runtime)
        raise RuntimeError("explicit fixture cleanup failure after release")

    monkeypatch.setattr(AgentRuntime, "dispose", fail_after_close)
    provider = ScriptedLlmProvider([ModelResponse(content="unused")])
    # An input limit failure occurs through the actual Runtime; cleanup then fails independently.
    with pytest.raises(BaseExceptionGroup) as error:
        await invoke(
            tmp_path / "call", provider, token_limit=200, output_tokens=50, safety_tokens=10
        )
    assert len(error.value.exceptions) == 2
    _, receipt, _ = load_model_call(tmp_path / "call")
    assert not receipt["converged"]
    assert "request-token-budget-exceeded" in receipt["errors"]
    assert "RuntimeError" in receipt["errors"]


class GatedProvider:
    name = "scripted"

    def __init__(self):
        self.entered, self.closing, self.release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.finished = False

    async def complete(self, request):
        self.entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.closing.set()
            await self.release.wait()
            self.finished = True


async def test_repeated_cancel_waits_for_actual_provider_and_closes_database(tmp_path):
    provider = GatedProvider()
    task = asyncio.create_task(invoke(tmp_path / "call", provider))
    await asyncio.wait_for(provider.entered.wait(), 5)
    task.cancel()
    await asyncio.wait_for(provider.closing.wait(), 5)
    task.cancel()
    assert not task.done() and not provider.finished
    provider.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.finished
    _, receipt, _ = load_model_call(tmp_path / "call")
    assert receipt["converged"] and receipt["errors"]
    assert not receipt["observation"]["completed"]
    database = tmp_path / "call/data/events.sqlite3"
    database.rename(database.with_suffix(".closed"))
