"""Observation stays derived, selectable, and separate from model turns."""

from dataclasses import asdict

import pytest
from test_sandbox_contract import policy

from traceh.api.events import PendingEvent
from traceh.api.json_types import fingerprint
from traceh.api.sandbox import SandboxOwner
from traceh.chat.sandbox_inspection import sandbox_report
from traceh.session.event_store import InMemoryEventStore
from traceh.session.service import SessionService


async def test_request_without_receipt_never_implies_execution_or_convergence():
    store = InMemoryEventStore()
    session_id = "inspection-session"
    stream = SessionService.effect_stream(session_id)
    request = dict(
        execution_id="e" * 32,
        owner=asdict(SandboxOwner("verification", "completion-check")),
        policy=asdict(policy()), export_workspace=False,
    )
    request["digest"] = fingerprint(request)
    await store.append(
        stream, events=(PendingEvent("sandbox/request", request),), expected_seq=0,
    )
    before = await store.read(stream)
    report = await sandbox_report(store, session_id=session_id, policy=policy())
    assert "只有请求记录；是否启动、是否已收尾尚未确认" in report
    assert "执行资源收敛：已确认" not in report
    assert "上面是选定策略；实际后端是否可用，以执行回执为准" in report
    assert await store.read(stream) == before


async def test_line_inspection_is_not_a_model_turn_and_resume_keeps_policy_path(tmp_path):
    from test_cli_chat import FakeConsole

    from traceh.api.sandbox import SandboxConfiguration
    from traceh.cli.chat import ResumeEnvironment, run_chat
    from traceh.llm.scripted import ScriptedLlmProvider
    from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime

    store = InMemoryEventStore()
    provider = ScriptedLlmProvider(())
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data",
                      sandbox=SandboxConfiguration(policy(), tmp_path / "cas")),
        event_store=store, provider=provider,
    )
    console = FakeConsole(("/sandbox", "/exit"))
    try:
        assert await run_chat(
            runtime, console.console, workspace=tmp_path,
            resume_environment=ResumeEnvironment(sandbox_config=tmp_path / "sandbox.json"),
        ) == 0
        assert "Docker 连接：explicit-test-context" in console.output
        assert "--sandbox-config" in console.output
        assert "sandbox.json" in console.output
        assert not provider.requests
        session_id = (await runtime.sessions.list_sessions())[0]
        assert not await runtime.sessions.read_effects(session_id)
        events = await runtime.sessions.read_session(session_id)
        assert not any(e.type == "turn/start" for e in events)
    finally:
        await runtime.dispose()


async def test_tui_sandbox_command_is_selectable_read_only_and_returns_to_chat(tmp_path):
    pytest.importorskip("textual")
    from textual.widgets import Input, TextArea

    from traceh.chat.activity import default_clock
    from traceh.chat.session import open_chat_session
    from traceh.llm.scripted import ScriptedLlmProvider
    from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
    from traceh.tui.app import TracehTuiApp
    from traceh.tui.sandbox_inspection import SandboxScreen

    store = InMemoryEventStore()
    provider = ScriptedLlmProvider(())
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data"), event_store=store, provider=provider,
    )
    try:
        opened = await open_chat_session(runtime, workspace=tmp_path, session_id=None)
        before = await runtime.sessions.read_session(opened.session.session_id)
        app = TracehTuiApp(
            runtime, opened, timeline=False, heartbeat_seconds=0,
            product=None, clock=default_clock(),
        )
        async with app.run_test(size=(110, 32)) as pilot:
            home = app.screen
            app.query_one("#chat-input", Input).value = "/sandbox"
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, SandboxScreen)
            text = app.screen.query_one("#sandbox-report", TextArea)
            assert text.read_only
            assert "未启用" in text.text
            assert opened.session.session_id in text.text
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is home
            assert app.query_one("#chat-input", Input).has_focus
        assert not provider.requests
        assert await runtime.sessions.read_session(opened.session.session_id) == before
    finally:
        await runtime.dispose()
