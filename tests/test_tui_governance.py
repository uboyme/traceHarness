"""Both governance adapters use the same service and explicit human confirmation."""

import asyncio

import pytest

pytest.importorskip("textual")

from rich.cells import cell_len
from test_cli_chat import FakeConsole
from test_memory_context import memory_case
from textual.widgets import Button, Input, Select, Static, TextArea

from traceh.chat.activity import default_clock
from traceh.chat.session import open_chat_session
from traceh.cli.chat import ResumeEnvironment, _handle_command
from traceh.tui.app import TracehTuiApp
from traceh.tui.governance import GovernanceScreen, MemoryScreen
from traceh.tui.text_selection import CopyMenu, SelectableLog


@pytest.fixture
def mounted_screens(monkeypatch):
    """Observe real screen completion without guessing async storage timing."""
    mounted = asyncio.Queue()
    original = GovernanceScreen.on_mount

    def record_mount(screen):
        original(screen)
        mounted.put_nowait(screen)

    monkeypatch.setattr(GovernanceScreen, "on_mount", record_mount)
    return mounted


async def test_line_governance_refusal_then_confirmation(tmp_path):
    async with memory_case(tmp_path) as (runtime, _, provider, session_id, _, _):
        opened = await open_chat_session(runtime, workspace=None, session_id=session_id)
        for answer, expected in (("no", 1), ("CONFIRM", 0)):
            console = FakeConsole((answer,))
            await _handle_command(
                runtime,
                console.console,
                opened.session,
                "/memory revoke context-fact reviewer",
                ResumeEnvironment(),
            )
            assert len((await runtime.memory.read(session_id)).active) == expected
            assert "predecessor_digest" in console.output
            assert "context-project" in console.output
        assert provider.requests == []


async def test_tui_reads_and_confirms_without_sending_slash_commands_to_model(
    tmp_path, mounted_screens
):
    async with memory_case(tmp_path) as (runtime, store, provider, session_id, _, _):
        opened = await open_chat_session(runtime, workspace=None, session_id=session_id)
        app = TracehTuiApp(
            runtime,
            opened,
            timeline=False,
            heartbeat_seconds=0,
            product=None,
            clock=default_clock(),
        )
        async with app.run_test(size=(65, 30)) as pilot:
            input_widget = app.query_one("#chat-input", Input)
            input_widget.value = "/memory"
            await pilot.press("enter")
            assert await asyncio.wait_for(mounted_screens.get(), 10) is app.screen
            await pilot.press("escape")
            input_widget.value = "/memory revoke context-fact reviewer"
            await pilot.press("enter")
            assert await asyncio.wait_for(mounted_screens.get(), 10) is app.screen
            assert len((await runtime.memory.read(session_id)).active) == 1
            await pilot.press("escape")
            assert await asyncio.wait_for(mounted_screens.get(), 10) is app.screen
            assert await store.head("memory:context-project") == 2
            await pilot.press("escape")  # close the cancelled result
            input_widget.value = "/memory revoke context-fact reviewer"
            await pilot.press("enter")
            assert await asyncio.wait_for(mounted_screens.get(), 10) is app.screen
            app.screen.query_one("#governance-confirm", Input).value = "CONFIRM"
            await pilot.press("enter")
            assert await asyncio.wait_for(mounted_screens.get(), 10) is app.screen
            assert (await runtime.memory.read(session_id)).active == ()
            assert await store.head("memory:context-project") == 3
            assert provider.requests == []


@pytest.mark.parametrize("command", ["/memory", "/memory revoke context-fact reviewer"])
async def test_tui_closing_pending_governance_confirmation_converges(
    tmp_path, mounted_screens, command
):
    async with memory_case(tmp_path) as (runtime, store, provider, session_id, _, _):
        opened = await open_chat_session(runtime, workspace=None, session_id=session_id)
        app = TracehTuiApp(
            runtime,
            opened,
            timeline=False,
            heartbeat_seconds=0,
            product=None,
            clock=default_clock(),
        )
        async with app.run_test(size=(50, 25)) as pilot:
            app.query_one("#chat-input", Input).value = command
            await pilot.press("enter")
            assert await asyncio.wait_for(mounted_screens.get(), 10) is app.screen
            await pilot.press("ctrl+q")
        assert await store.head("memory:context-project") == 2
        assert provider.requests == []
        with pytest.raises(RuntimeError, match="disposed"):
            await runtime.memory.read(session_id)


@pytest.mark.parametrize("clipboard_action", ["keyboard", "menu"])
async def test_conversation_drag_copy_in_place_without_writes(tmp_path, clipboard_action):
    from traceh.api.llm import ModelResponse
    from traceh.llm.scripted import ScriptedLlmProvider

    text = "中文复制 Alpha 🚀 123"
    reply = "\n".join(f"record {i}" for i in range(40)) + "\n" + text
    scripted = ScriptedLlmProvider((ModelResponse(content=reply),), repeat_last=True)
    async with memory_case(tmp_path, provider=scripted) as (
        runtime,
        store,
        provider,
        session_id,
        _,
        _,
    ):
        await runtime.run_existing(session_id, "输出测试段落")
        opened = await open_chat_session(runtime, workspace=None, session_id=session_id)
        app = TracehTuiApp(
            runtime,
            opened,
            timeline=False,
            heartbeat_seconds=0,
            product=None,
            clock=default_clock(),
        )
        async with app.run_test(size=(85, 35)) as pilot:
            await pilot.pause()
            head = await store.head(f"session:{session_id}")
            requests = len(provider.requests)
            assert not app.query("#chat-actions")
            assert not app.query_one("#product-column").display
            await pilot.press("ctrl+c")  # Empty selection never exits.
            log = app.query_one("#conversation", SelectableLog)
            log.scroll_end(animate=False, immediate=True)
            await pilot.pause()
            assert log.scroll_offset.y > 0
            row = next(i for i, line in enumerate(log.lines) if text in line.text)
            prefix = log.lines[row].text.split(text)[0]
            gutter = log.content_region.offset - log.region.offset
            start = (gutter.x + cell_len(prefix), gutter.y + row - log.scroll_offset.y)
            end = (start[0] + cell_len(text) - 1, start[1])
            before_style = app.screen.get_style_at(
                log.region.x + start[0], log.region.y + start[1],
            )
            await pilot.mouse_down(log, offset=start)
            await pilot.hover(log, offset=end)
            await pilot.mouse_up(log, offset=end)
            assert app.screen.get_selected_text() == text
            style = app.screen.get_style_at(log.region.x + start[0], log.region.y + start[1])
            # Terminal color filters may map RGB to monochrome; the highlight
            # must still visibly differ from the unselected conversation.
            assert style.bgcolor != before_style.bgcolor
            if clipboard_action == "keyboard":
                await pilot.press("ctrl+c")
            else:
                await pilot.click(log, offset=end, button=3)
                assert isinstance(app.screen, CopyMenu)
                await pilot.click("#selection-copy")
            assert app.clipboard == text
            assert len(app.screen_stack) == 1
            await pilot.click("#chat-input")
            await pilot.press("ctrl+v")
            assert app.query_one("#chat-input", Input).value == text
            assert await store.head(f"session:{session_id}") == head
            assert len(provider.requests) == requests


async def test_governance_right_click_copy_and_empty_selection_do_not_exit(
    tmp_path, mounted_screens, monkeypatch
):
    conversation_ready = asyncio.Event()
    original_render = TracehTuiApp._render_initial_conversation

    def record_conversation_render(app, *args):
        original_render(app, *args)
        # RichLog writes invalidate its auto-height. Wait for that layout before
        # Pilot converts the widget-relative offset to screen coordinates.
        app.call_after_refresh(conversation_ready.set)

    monkeypatch.setattr(
        TracehTuiApp, "_render_initial_conversation", record_conversation_render
    )
    copy_mounted = asyncio.Queue()
    original_mount = CopyMenu.on_mount

    def record_copy_mount(screen):
        original_mount(screen)
        copy_mounted.put_nowait(screen)

    monkeypatch.setattr(CopyMenu, "on_mount", record_copy_mount)
    async with memory_case(tmp_path) as (runtime, store, provider, session_id, _, _):
        opened = await open_chat_session(runtime, workspace=None, session_id=session_id)
        app = TracehTuiApp(
            runtime,
            opened,
            timeline=False,
            heartbeat_seconds=0,
            product=None,
            clock=default_clock(),
        )
        async with app.run_test(size=(85, 35)) as pilot:
            await asyncio.wait_for(conversation_ready.wait(), 10)
            head = await store.head(f"session:{session_id}")
            log = app.query_one("#conversation", SelectableLog)
            assert await pilot.click(log, offset=(2, 1), button=3)
            assert await asyncio.wait_for(copy_mounted.get(), 10) is app.screen
            assert isinstance(app.screen, CopyMenu)
            assert app.screen.query_one("#selection-copy", Button).disabled
            await pilot.press("ctrl+c", "escape")
            assert len(app.screen_stack) == 1 and app.clipboard == ""
            await app.push_screen(GovernanceScreen({"body": "可选择的审批证据"}))
            await asyncio.wait_for(mounted_screens.get(), 10)
            area = app.screen.query_one("#governance-evidence", TextArea)
            await pilot.click(area)
            await pilot.press("f7")
            expected = area.selected_text
            assert "可选择" in expected
            assert await pilot.click(area, offset=(3, 1), button=3)
            assert await asyncio.wait_for(copy_mounted.get(), 10) is app.screen
            assert isinstance(app.screen, CopyMenu)
            await pilot.click("#selection-copy")
            assert app.clipboard == expected
            await pilot.press("escape")
            entry = app.query_one("#chat-input", Input)
            entry.value = "输入框选择测试"
            await pilot.click(entry)
            await pilot.press("home", "shift+end", "ctrl+c")
            assert app.clipboard == entry.value
            assert await store.head(f"session:{session_id}") == head
            assert provider.requests == []


@pytest.mark.parametrize("operation", ["supersede", "revoke"])
async def test_memory_form_targets_active_fact_through_original_review(
    tmp_path, mounted_screens, operation
):
    from traceh.chat.governance import ChatGovernance

    async with memory_case(tmp_path) as (runtime, _, provider, session_id, _, _):

        async def yes(review):
            return True

        control = ChatGovernance(runtime, session_id)
        await control.run('/memory declare reviewer "更新后的独立会议规则"', confirm=yes)
        view = await control.memory()
        original = view["active"][0]
        proposal = view["proposals"][-1]["proposal_id"]
        opened = await open_chat_session(runtime, workspace=None, session_id=session_id)
        app = TracehTuiApp(
            runtime,
            opened,
            timeline=False,
            heartbeat_seconds=0,
            product=None,
            clock=default_clock(),
        )
        async with app.run_test(size=(100, 45)) as pilot:
            await pilot.press("f4")
            screen = await asyncio.wait_for(mounted_screens.get(), 10)
            screen.query_one("#memory-actor", Input).value = "reviewer"
            screen.query_one("#memory-active", Select).value = original["memory_id"]
            screen.query_one("#memory-proposal", Select).value = proposal
            await pilot.pause()
            assert screen.query_one("#memory-slot", Input).value == original["fact_slot"]
            screen.query_one(f"#memory-{operation}", Button).press()
            review = await asyncio.wait_for(mounted_screens.get(), 10)
            assert (await control.memory())["active"] == view["active"]
            review.query_one("#governance-confirm", Input).value = "CONFIRM"
            await pilot.press("enter")
            await asyncio.wait_for(mounted_screens.get(), 10)
            active = (await control.memory())["active"]
            if operation == "supersede":
                assert len(active) == 1 and active[0]["body"] == "更新后的独立会议规则"
                assert active[0]["fact_slot"] == original["fact_slot"]
            else:
                assert active == []
            assert provider.requests == []


async def test_memory_form_selects_real_proposal_then_cancel_and_approve(tmp_path, mounted_screens):
    from traceh.chat.governance import ChatGovernance

    async with memory_case(tmp_path) as (runtime, store, provider, session_id, _, _):

        async def yes(review):
            return True

        control = ChatGovernance(runtime, session_id)
        await control.run('/memory declare reviewer "新会议的签到规则"', confirm=yes)
        proposal = (await control.memory())["proposals"][-1]["proposal_id"]
        opened = await open_chat_session(runtime, workspace=None, session_id=session_id)
        app = TracehTuiApp(
            runtime,
            opened,
            timeline=False,
            heartbeat_seconds=0,
            product=None,
            clock=default_clock(),
        )
        async with app.run_test(size=(100, 45)) as pilot:
            for approve in (False, True):
                await pilot.press("f4")
                screen = await asyncio.wait_for(mounted_screens.get(), 10)
                assert isinstance(screen, MemoryScreen)
                before = await store.head("memory:context-project")
                await pilot.click("#memory-approve")
                assert "署名" in str(screen.query_one("#memory-error", Static).render())
                screen.query_one("#memory-actor", Input).value = "reviewer"
                screen.query_one("#memory-approve", Button).press()
                await pilot.pause()
                assert "请选择" in str(screen.query_one("#memory-error", Static).render())
                assert await store.head("memory:context-project") == before
                screen.query_one("#memory-proposal", Select).value = proposal
                screen.query_one("#memory-slot", Input).value = "attendance"
                screen.query_one("#memory-approve", Button).press()
                review = await asyncio.wait_for(mounted_screens.get(), 10)
                assert proposal in review.query_one("#governance-evidence", TextArea).text
                assert await store.head("memory:context-project") == before
                if approve:
                    review.query_one("#governance-confirm", Input).value = "CONFIRM"
                    await pilot.press("enter")
                else:
                    await pilot.press("escape")
                await asyncio.wait_for(mounted_screens.get(), 10)
                facts = (await control.memory())["active"]
                assert len(facts) == (2 if approve else 1)
                if approve:
                    assert any(
                        f["body"] == "新会议的签到规则" and f["fact_slot"] == "attendance"
                        for f in facts
                    )
                await pilot.press("escape")
            assert provider.requests == []
