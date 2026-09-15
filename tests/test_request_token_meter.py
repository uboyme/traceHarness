"""Complete-request estimates, honest usage, refusal and shared replay."""

import asyncio
import json
from dataclasses import replace

import pytest

pytest.importorskip("tiktoken")

from traceh.api.llm import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolSchema,
    Usage,
    UsageQuality,
)
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.llm.token_meter import RequestTokenBudgetExceeded, RequestTokenMeter, TokenBudgetPolicy
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.runtime.request_builder import validate_token_measurements, verify_request_snapshots
from traceh.session.sqlite import SqliteEventStore
from traceh.tui.context_inspection import ContextInspectionReader


def policy(window=100_000):
    return TokenBudgetPolicy("cl100k_base", window, 256, 256)


@pytest.mark.parametrize(
    "content", ["中文界面🙂需要验证", "def parse_record(value): return value[0]"]
)
def test_every_request_part_counts_and_quality_is_estimated(content):
    meter = RequestTokenMeter(policy())
    request = ModelRequest(
        provider="fixture",
        model="configured-model",
        system_prompt=content,
        messages=(
            ModelMessage("system", "host state"),
            ModelMessage("user", content),
            ModelMessage("user", "reference and current question"),
        ),
        tools=(ToolSchema("inspect_record", content, {"type": "object"}),),
        max_output_tokens=256,
    )
    measured = meter.measure(request, product_messages=1)
    assert measured["counter"]["quality"] == "estimated"
    assert measured["input_tokens"] == sum(measured["parts"].values())
    assert all(value > 0 for value in measured["parts"].values())
    changed = meter.measure(replace(request, tools=()), product_messages=1)
    assert changed["input_tokens"] < measured["input_tokens"]
    assert measured["input_limit"] == 99_488


async def test_real_runtime_records_meter_and_actual_usage_then_reopens(tmp_path):
    store = SqliteEventStore(tmp_path / "events")
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path, token_budget=policy()),
        event_store=store,
        provider=ScriptedLlmProvider(
            (ModelResponse(content="done", usage=Usage(37, 4, UsageQuality.EXACT)),)
        ),
    )
    try:
        sid = await runtime.create_session(tmp_path)
        await runtime.run_existing(sid, "A different request.")
        events = await runtime.sessions.read_session(sid)
        measure = next(e for e in events if e.type == "request/token-measurement")
        snapshot = next(e for e in events if e.type == "request/snapshot")
        assert measure.seq < snapshot.seq
        assert (
            measure.data["measurement"]["request_fingerprint"]
            == snapshot.data["composed_fingerprint"]
        )
        assert not await runtime.check_invariants(sid)
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, sid)
        view = await ContextInspectionReader(runtime.sessions).load(sid)
        assert view.request.actual_input_tokens == 37
        assert view.request.actual_output_tokens == 4
        assert view.request.token_measurement["input_tokens"] != 37
        from traceh.tui.presentation import context_detail_lines, context_status_line

        assert "输入估算" in context_status_line(view, width=160)
        details = "\n".join(line.plain for line in context_detail_lines(view))
        assert "37 / 4 token" in details
        assert "非服务端精确计数" in details
    finally:
        await runtime.dispose()
        await store.aclose()
    store = SqliteEventStore(tmp_path / "events")
    runtime = build_default_runtime(RuntimeConfig(data_dir=tmp_path), event_store=store)
    try:
        view = await ContextInspectionReader(runtime.sessions).load(sid)
        assert view.request.token_measurement == measure.data["measurement"]
        assert not await runtime.check_invariants(sid)
        data = json.loads(json.dumps(measure.data))
        data["measurement"]["parts"]["tools"] = 0
        with pytest.raises(ValueError, match="token-measurement-mismatch"):
            validate_token_measurements(
                tuple(replace(e, data=data) if e.seq == measure.seq else e for e in events)
            )
    finally:
        await runtime.dispose()
        await store.aclose()


async def test_over_limit_records_refusal_without_provider_dispatch(tmp_path):
    provider = ScriptedLlmProvider((ModelResponse(content="should not dispatch"),))
    store = SqliteEventStore(tmp_path / "events")
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path, token_budget=policy(600)),
        provider=provider,
        event_store=store,
    )
    try:
        sid = await runtime.create_session(tmp_path)
        with pytest.raises(RequestTokenBudgetExceeded):
            await runtime.run_existing(sid, "This includes the full tools and system prompt.")
        events = await runtime.sessions.read_session(sid)
        assert not provider.requests
        assert not any(e.type == "request/snapshot" for e in events)
        measured = next(
            e.data["measurement"] for e in events if e.type == "request/token-measurement"
        )
        assert measured["over_limit"] and measured["parts"]["tools"] > 0
        assert not await runtime.check_invariants(sid)
    finally:
        await runtime.dispose()
        await store.aclose()


def test_missing_or_impossible_policy_rejected():
    with pytest.raises(ValueError):
        TokenBudgetPolicy("cl100k_base", 100, 90, 20)
    with pytest.raises(ValueError):
        TokenBudgetPolicy("", 100, 20, 20)


@pytest.mark.parametrize("failure", ["before", "after", "cancel", "race", "earlier-race"])
async def test_measurement_write_failure_and_double_cancel_never_dispatch(
    tmp_path, monkeypatch, failure
):
    store = SqliteEventStore(tmp_path / "events")
    provider = ScriptedLlmProvider((ModelResponse(content="not called"),))
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path, token_budget=policy()),
        event_store=store,
        provider=provider,
    )
    entered, release = asyncio.Event(), asyncio.Event()
    original = runtime.sessions.append_session

    async def append(sid, kind, data, **kwargs):
        if kind == "request/token-measurement":
            if failure == "before":
                raise OSError("before measurement commit")
            if failure == "race":
                await original(sid, "fixture/race", {})
            result = await original(sid, kind, data, **kwargs)
            entered.set()
            if failure == "cancel":
                await release.wait()
            elif failure == "after":
                raise OSError("after measurement commit")
            return result
        result = await original(sid, kind, data, **kwargs)
        if kind == "composition/snapshot" and failure == "earlier-race":
            await original(sid, "fixture/race", {})
        return result

    monkeypatch.setattr(runtime.sessions, "append_session", append)
    task = None
    try:
        sid = await runtime.create_session(tmp_path)
        task = asyncio.create_task(runtime.run_existing(sid, "Check cancellation."))
        if failure == "cancel":
            await asyncio.wait_for(entered.wait(), 10)
            task.cancel()
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            from traceh.session.event_store import ConcurrencyConflict
            from traceh.session.service import ModelAttemptConflictError

            with pytest.raises(
                (ConcurrencyConflict, ModelAttemptConflictError)
                if failure in {"race", "earlier-race"}
                else OSError
            ):
                await task
        assert not provider.requests
        events = await runtime.sessions.read_session(sid)
        assert not any(e.type == "model/attempt-start" for e in events)
        assert sum(e.type == "request/token-measurement" for e in events) == (
            1 if failure in {"after", "cancel"} else 0
        )
        assert not await runtime.check_invariants(sid)
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await runtime.dispose()
        await store.aclose()


def test_wrong_model_binding_and_trigger_limits_fail():
    meter = RequestTokenMeter(policy(), provider="first", model="model-a")
    with pytest.raises(ValueError, match="binding"):
        meter.measure(
            ModelRequest(provider="second", model="model-a", messages=(), max_output_tokens=256)
        )
    with pytest.raises(ValueError, match="trigger"):
        replace(policy(), trigger_percent=0)


def test_cli_profile_roundtrip_includes_token_inputs(tmp_path):
    from traceh.cli.main import _configure_from_environment, build_parser
    from traceh.cli.tui_config import apply_values, form_values, load_profile, save_profile

    args = build_parser().parse_args(
        [
            "chat",
            str(tmp_path),
            "--token-encoding",
            "cl100k_base",
            "--context-window-tokens",
            "40000",
            "--context-output-reserve",
            "1024",
            "--context-safety-margin",
            "2048",
            "--context-trigger-percent",
            "75",
        ]
    )
    path = tmp_path / "profile.json"
    save_profile(path, form_values(args))
    restored = apply_values(build_parser().parse_args(["chat", str(tmp_path)]), load_profile(path))
    _configure_from_environment(restored, environment={})
    assert restored.token_budget == TokenBudgetPolicy("cl100k_base", 40000, 1024, 2048, 75)


def test_token_timeline_malformed_payload_is_safe():
    from test_cli_timeline import envelope

    from traceh.cli.timeline import TimelineRenderer

    assert "unavailable" in TimelineRenderer().render(
        envelope("request/token-measurement", {"measurement": {"input_tokens": "\x1b[31m"}})
    )


async def test_active_tool_reply_is_kept_when_next_step_exceeds_budget(tmp_path):
    from traceh.api.llm import ToolCall

    (tmp_path / "source.txt").write_text("sample record " * 6000, encoding="utf-8")
    store = SqliteEventStore(tmp_path / "events")
    provider = ScriptedLlmProvider(
        (
            ModelResponse(tool_calls=(ToolCall("read-once", "read_file", {"path": "source.txt"}),)),
            ModelResponse(content="must not reach second request"),
        )
    )
    runtime = build_default_runtime(
        # read_file now returns a bounded page; choose a window that admits the
        # first request but cannot also admit that page's active Tool group.
        RuntimeConfig(data_dir=tmp_path, max_tool_output_chars=200_000, token_budget=policy(4000)),
        provider=provider,
        event_store=store,
    )
    try:
        sid = await runtime.create_session(tmp_path)
        with pytest.raises(RequestTokenBudgetExceeded):
            await runtime.run_existing(sid, "Read the source once.")
        events = await runtime.sessions.read_session(sid)
        assert len(provider.requests) == 1
        assert sum(e.type == "tool/result" for e in events) == 1
        assert not any(e.type == "surface/replace" for e in events)
        messages = runtime.surface.project(events)
        assert any(m.tool_calls and m.tool_calls[0].id == "read-once" for m in messages)
        assert any(m.role == "tool" and m.tool_call_id == "read-once" for m in messages)
        assert not await runtime.check_invariants(sid)
    finally:
        await runtime.dispose()
        await store.aclose()


async def test_token_pressure_compacts_closed_history_despite_large_byte_threshold(tmp_path):
    from test_tool_result_folding import prepare_history

    from traceh.session.compaction import CompactionPolicy

    sid, original, _, _, _ = await prepare_history(tmp_path)
    store = SqliteEventStore(tmp_path / "data" / "events")
    provider = ScriptedLlmProvider((ModelResponse(content="new answer"),))
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "data",
            token_budget=replace(policy(), trigger_percent=1),
            compaction=CompactionPolicy(True, 10_000_000, 400, 1),
        ),
        provider=provider,
        event_store=store,
    )
    try:
        await runtime.run_existing(sid, "Answer this current question.")
        events = await runtime.sessions.read_session(sid)
        folds = [e for e in events if e.type == "surface/replace"]
        assert folds and folds[0].data["method"] == "tool-fold"
        assert original in events
        messages = provider.requests[-1].messages
        assert any(m.content == "Keep this recent conversation." for m in messages)
        assert any(m.content == "Answer this current question." for m in messages)
        assert not await runtime.check_invariants(sid)
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, sid)
    finally:
        await runtime.dispose()
        await store.aclose()
