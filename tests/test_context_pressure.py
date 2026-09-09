"""E3 user-visible refusal from the same real Runtime and durable proof."""

import pytest

from traceh.api.llm import ModelResponse
from traceh.chat.activity import default_clock
from traceh.chat.context_pressure import read_context_pressure
from traceh.chat.driver import ChatDriver, TurnFailedUpdate
from traceh.cli.chat import _LineUpdateAdapter
from traceh.cli.console import Console
from traceh.cli.context_pressure import context_pressure_text
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.llm.token_meter import RequestTokenBudgetExceeded, TokenBudgetPolicy
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.session.event_store import InMemoryEventStore
from traceh.session.sqlite import SqliteEventStore


@pytest.mark.parametrize("diagnostic_fails", [False, True])
async def test_driver_line_refusal_is_audited_and_diagnostic_failure_is_safe(
    tmp_path, monkeypatch, diagnostic_fails
):
    provider = ScriptedLlmProvider((ModelResponse(content="unused"),))
    store = SqliteEventStore(tmp_path / "events")
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path, token_budget=TokenBudgetPolicy("cl100k_base", 900, 256, 100)
        ),
        provider=provider,
        event_store=store,
    )
    output, updates = [], []
    adapter = _LineUpdateAdapter(Console(read_line=lambda _: "", write=output.append))

    async def consume(update):
        updates.append(update)
        await adapter.consume(update)

    if diagnostic_fails:

        async def fail(*_):
            raise OSError("private diagnostic content must not be displayed")

        monkeypatch.setattr("traceh.chat.driver.read_context_pressure", fail)
    try:
        sid = await runtime.create_session(tmp_path)
        driver = ChatDriver(
            runtime, sid, sink=consume, timeline=False, heartbeat_seconds=0, clock=default_clock()
        )
        outcome = await driver.run_turn("A private question that cannot fit the complete request.")
        assert outcome.failed and not provider.requests
        failed = next(u for u in updates if isinstance(u, TurnFailedUpdate))
        assert failed.context_limit_exceeded
        text = "\n".join(output)
        assert "上下文空间不足" in text and "未发送" in text
        assert "private" not in text and "ValueError" not in text
        if diagnostic_fails:
            assert failed.context_pressure is None and "无法核对" in text
        else:
            events = await runtime.sessions.read_session(sid)
            data = next(e.data for e in events if e.type == "request/token-measurement")
            assert str(data["measurement"]["input_tokens"]) in text
            assert "工具定义" in text and "不会自动重跑工具" in text
            error = RequestTokenBudgetExceeded(
                session_id=sid, turn_id=data["turn_id"], step_id=data["step_id"]
            )
            # A later failed Turn cannot replace the identified failure's counts.
            await driver.run_turn("different " * 200)
            assert await read_context_pressure(runtime.sessions, error) == failed.context_pressure
        assert not await runtime.check_invariants(sid)
    finally:
        await runtime.dispose()
        await store.aclose()


async def test_non_budget_failure_does_not_claim_local_refusal(tmp_path):
    class FailedProvider:
        name = "failing-provider"

        async def complete(self, request):
            raise ValueError("network failure")

    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path, provider=FailedProvider.name),
        provider=FailedProvider(),
        include_default_tools=False,
        event_store=InMemoryEventStore(),
    )
    updates = []

    async def consume(update):
        updates.append(update)

    try:
        sid = await runtime.create_session(tmp_path)
        driver = ChatDriver(
            runtime, sid, sink=consume, timeline=False, heartbeat_seconds=0, clock=default_clock()
        )
        assert (await driver.run_turn("hello")).failed
        failed = next(u for u in updates if isinstance(u, TurnFailedUpdate))
        assert not failed.context_limit_exceeded and failed.context_pressure is None
    finally:
        await runtime.dispose()


async def test_tui_displays_actual_refusal_details(tmp_path):
    pytest.importorskip("textual")
    from textual.widgets import RichLog

    from traceh.chat.session import open_chat_session
    from traceh.tui.app import TracehTuiApp

    provider = ScriptedLlmProvider((ModelResponse(content="unused"),))
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path, token_budget=TokenBudgetPolicy("cl100k_base", 900, 256, 100)
        ),
        provider=provider,
        event_store=InMemoryEventStore(),
    )
    try:
        session = await open_chat_session(runtime, workspace=tmp_path, session_id=None)
        app = TracehTuiApp(
            runtime,
            session,
            timeline=False,
            heartbeat_seconds=0,
            clock=default_clock(),
            product=None,
        )
        async with app.run_test(size=(150, 42)) as pilot:
            driver = ChatDriver(
                runtime,
                session.session.session_id,
                sink=app._receive_chat_update,
                timeline=False,
                heartbeat_seconds=0,
                clock=default_clock(),
            )
            assert (await driver.run_turn("hello")).failed
            await pilot.pause()
            text = "\n".join(line.text for line in app.query_one(RichLog).lines)
            assert "上下文空间不足" in text and "输入" in text and "工具定义" in text
            assert "不会自动重跑工具" in text and not provider.requests
    finally:
        await runtime.dispose()


def test_summary_refusal_wording_distinguishes_maintenance():
    from traceh.chat.context_pressure import ContextPressureView

    view = ContextPressureView(
        110,
        100,
        20,
        10,
        (
            ("conversation", 85),
            ("references_and_current_request", 0),
            ("product", 0),
            ("system", 10),
            ("tools", 5),
            ("envelope", 10),
        ),
        (("tool-fold", 1), ("automatic", 0), ("semantic", 0)),
        1,
        0,
        True,
    )
    text = context_pressure_text(view)
    assert "摘要请求" in text and "旧工具折叠 1 次" in text and "维护失败 1 次" in text


async def test_refusal_reports_actual_maintenance_from_current_turn(tmp_path):
    from traceh.session.compaction import CompactionPolicy

    store = SqliteEventStore(tmp_path / "events")
    provider = ScriptedLlmProvider((ModelResponse(content="received"),))
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path), event_store=store, provider=provider
    )
    updates = []

    async def consume(update):
        updates.append(update)

    try:
        sid = await runtime.create_session(tmp_path)
        await runtime.run_existing(sid, "old inspection notes " * 300)
        await runtime.dispose()
        runtime = build_default_runtime(
            RuntimeConfig(
                data_dir=tmp_path,
                token_budget=TokenBudgetPolicy("cl100k_base", 900, 256, 100),
                compaction=CompactionPolicy(True, 1, 500, 0),
            ),
            event_store=store,
            provider=provider,
        )
        driver = ChatDriver(
            runtime, sid, sink=consume, timeline=False, heartbeat_seconds=0, clock=default_clock()
        )
        assert (await driver.run_turn("A new question.")).failed
        failed = next(u for u in updates if isinstance(u, TurnFailedUpdate))
        assert failed.context_pressure is not None
        assert dict(failed.context_pressure.maintenance)["automatic"] == 1
        assert "旧历史摘录 1 次" in context_pressure_text(failed.context_pressure)
        assert len(provider.requests) == 1
        assert not await runtime.check_invariants(sid)
    finally:
        await runtime.dispose()
        await store.aclose()
