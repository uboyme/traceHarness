"""Line and headless Textual inspect the same real frozen search results without writes."""

import asyncio

import pytest
from test_cli_chat import FakeConsole
from test_history_runtime import SelectingProvider, policy
from test_reference_search_history import search, setup

from traceh.api.llm import ModelResponse
from traceh.chat.activity import default_clock
from traceh.chat.session import open_chat_session
from traceh.cli.chat import ResumeEnvironment, _handle_command
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.session.event_store import InMemoryEventStore


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("matches", "找到匹配片段"),
        ("no-hit", "没有匹配"),
        ("unavailable", "没有可用或有效来源"),
        ("budget", "因预算未准入"),
    ],
)
async def test_line_and_tui_explain_original_search_snapshot(
    tmp_path, monkeypatch, scenario, expected
):
    pytest.importorskip("textual")
    from textual.widgets import Input, TextArea

    from traceh.tui.app import TracehTuiApp
    from traceh.tui.governance import GovernanceScreen

    responses = [
        search("unseen" if scenario == "no-hit" else "handover"),
        ModelResponse(content="done"),
    ]
    if scenario == "unavailable":
        provider = SelectingProvider(responses)
        runtime = build_default_runtime(
            RuntimeConfig(data_dir=tmp_path / "data", context_input=policy()),
            provider=provider,
            event_store=InMemoryEventStore(),
        )
        session = await runtime.create_session(tmp_path)
    else:
        runtime, provider, session = await setup(
            tmp_path,
            responses,
            context_policy=policy(item_bytes=500) if scenario == "budget" else None,
        )
    try:
        await runtime.run_existing(session, "Find earlier arrangements")
        before = await runtime.sessions.read_session(session)
        requests = len(provider.requests)
        opened = await open_chat_session(runtime, workspace=None, session_id=session)
        console = FakeConsole(())
        await _handle_command(
            runtime, console.console, opened.session, "/context", ResumeEnvironment()
        )
        assert expected in console.output and "当时的冻结结果" in console.output
        mounted = asyncio.Queue()
        original = GovernanceScreen.on_mount

        def on_mount(screen):
            original(screen)
            mounted.put_nowait(screen)

        monkeypatch.setattr(GovernanceScreen, "on_mount", on_mount)
        app = TracehTuiApp(
            runtime,
            opened,
            timeline=False,
            heartbeat_seconds=0,
            product=None,
            clock=default_clock(),
        )
        async with app.run_test(size=(90, 32)) as pilot:
            app.query_one("#chat-input", Input).value = "/context"
            await pilot.press("enter")
            screen = await asyncio.wait_for(mounted.get(), 10)
            assert expected in screen.query_one("#governance-evidence", TextArea).text
            await pilot.press("escape")
        assert len(provider.requests) == requests
        assert await runtime.sessions.read_session(session) == before
    finally:
        await runtime.dispose()
