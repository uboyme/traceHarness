"""Headless Textual F4 checks; skipped by a core-only installation."""

from __future__ import annotations

import asyncio
import re
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from rich.cells import cell_len
from rich.color import Color
from rich.style import Style

pytest.importorskip("textual")

from product_fixtures import ORIGIN_SESSION, preflight, proposal
from promotion_fixtures import build_source_repository, git, make_bare_target
from test_product_f3_e2e import (
    _build_host,
    _chat_runtime,
    _GatedProductProvider,
    _SideEffectAttemptingChatProvider,
)
from textual.app import App
from textual.pilot import Pilot
from textual.widgets import Button, Input, RichLog, Static

import traceh.tui.app as tui_app
from traceh.agents.identity import AGENT_DIRECTORY_STREAM
from traceh.api.events import PendingEvent
from traceh.api.llm import ModelRequest, ModelResponse
from traceh.api.product import (
    PRODUCT_TASK_AWAITING,
    ProductTaskStatus,
    ProductTaskSummary,
    RequestedTaskMode,
)
from traceh.api.turns import TurnInput
from traceh.api.workflow import WorkflowStatus
from traceh.artifacts.cas import LocalArtifactCas
from traceh.artifacts.events import ARTIFACT_CATALOG_STREAM
from traceh.budgets import BUDGET_LEDGER_STREAM
from traceh.chat.activity import DEFAULT_HEARTBEAT_SECONDS, Clock, default_clock
from traceh.chat.session import open_chat_session
from traceh.cli.console import Console
from traceh.cli.product import LineProductAdapter
from traceh.product.chat import (
    ProductChatTurn,
    ProductCommandOperation,
    ProductCommandResult,
    ProductStartRequest,
    ProductTurnActions,
    ProductTurnResolution,
)
from traceh.product.control import (
    PendingProductProposal,
    ProductAdvanceResult,
)
from traceh.product.inspection import ProductTaskEvidence
from traceh.product.observation import (
    ObservedStreamHead,
    ProductObservation,
    ProductObservationSession,
)
from traceh.product.projection import ProductTaskStreamReader
from traceh.promotion.events import PROMOTION_LEDGER_STREAM
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.session.event_feed import SessionEventFeed
from traceh.session.event_store import InMemoryEventStore
from traceh.session.invariants import CoreInvariantChecker
from traceh.session.sqlite import SqliteEventStore
from traceh.tui.app import TracehTuiApp
from traceh.tui.presentation import ProductGateAction
from traceh.tui.runner import run_tui
from traceh.tui.screens import ProductIdentityScreen, TaskConversationScreen
from traceh.tui.task_conversation import (
    TaskConversationRole,
    TaskConversationSnapshot,
)


async def _wait_for_confirmation_focus(
    pilot: Pilot[object], confirmation: Input
) -> None:
    """Synchronize with the deferred focus handoff after a gate click."""

    for _ in range(20):
        await pilot.pause()
        if confirmation.has_focus:
            return
    raise AssertionError("confirmation input did not receive focus")


class _Provider:
    name = "tui-provider"

    def __init__(self, *, gate: bool = False) -> None:
        self.requests: list[ModelRequest] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.gate = gate

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        self.started.set()
        if self.gate:
            await self.release.wait()
        return ModelResponse(content="answer [bold] stays plain")


def _runtime(tmp_path: Path, provider: _Provider):
    store = InMemoryEventStore()
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "runtime",
            provider=provider.name,
            model="tui-model",
        ),
        provider=provider,
        event_store=store,
    )
    return runtime, store


async def _opened_app(tmp_path: Path, provider: _Provider, *, product=None):
    runtime, store = _runtime(tmp_path, provider)
    opened = await open_chat_session(runtime, workspace=tmp_path, session_id=None)
    app = TracehTuiApp(
        runtime,
        opened,
        timeline=True,
        heartbeat_seconds=10.0,
        product=product,
        clock=default_clock(),
    )
    return app, runtime, store, opened


async def test_tui_uses_the_shared_driver_and_durable_session(tmp_path: Path) -> None:
    provider = _Provider()
    app, runtime, store, opened = await _opened_app(tmp_path, provider)
    try:
        async with app.run_test(size=(100, 32)) as pilot:
            assert app.query_one("#conversation").markup is False
            product_state = app.query_one("#product-state", Static)
            product_state.update("[bold]plain[/bold]")
            assert str(product_state.render()) == "[bold]plain[/bold]"
            await pilot.resize_terminal(72, 24)
            await pilot.pause()
            assert app.screen.has_class("narrow")
            assert len(str(product_state.content).splitlines()) <= 2
            home = app.screen
            await pilot.press("ctrl+t")
            assert isinstance(app.screen, TaskConversationScreen)
            await pilot.press("escape")
            assert app.screen is home
            app.query_one("#chat-input", Input).value = "hello from TUI"
            await pilot.press("enter")
            await asyncio.wait_for(provider.started.wait(), timeout=2)
            for _ in range(4):
                await pilot.pause()
            app.exit(0)

        events = await runtime.sessions.read_session(opened.session.session_id)
        assert any(
            event.type == "user/message"
            and event.data.get("content") == "hello from TUI"
            for event in events
        )
        assert any(
            event.type == "assistant/message"
            and event.data.get("content") == "answer [bold] stays plain"
            for event in events
        )
        effects = await runtime.sessions.read_effects(opened.session.session_id)
        assert not CoreInvariantChecker().check(events, effects)
    finally:
        await runtime.dispose()
        del store


@pytest.mark.parametrize(
    ("width", "narrow"),
    ((99, True), (100, True), (109, True), (110, False)),
)
async def test_layout_breakpoint_preserves_full_fact_row_width(
    tmp_path: Path,
    width: int,
    narrow: bool,
) -> None:
    provider = _Provider()
    app, runtime, store, _opened = await _opened_app(tmp_path, provider)
    try:
        async with app.run_test(size=(width, 30)) as pilot:
            await pilot.pause()
            assert app.screen.has_class("narrow") is narrow
            if not narrow:
                assert app.query_one("#product-state", Static).content_size.width >= 52
    finally:
        await runtime.dispose()
        del store


async def test_short_conversation_bottom_anchors_and_long_log_auto_scrolls(
    tmp_path: Path,
) -> None:
    provider = _Provider()
    app, runtime, store, _opened = await _opened_app(tmp_path, provider)
    try:
        async with app.run_test(size=(110, 34)) as pilot:
            log = app.query_one("#conversation", RichLog)
            activity = app.query_one("#activity", Static)
            chat_input = app.query_one("#chat-input", Input)
            for _ in range(20):
                await pilot.pause()
                if log.lines:
                    break
            initial_lines = [line.text for line in log.lines]
            assert initial_lines
            assert initial_lines[0].startswith("宿主 › Session ")
            assert any(
                line.startswith(" " * 7 + "Workspace ") for line in initial_lines
            )
            assert all(
                line.startswith("宿主 › ") or line.startswith(" " * 7)
                for line in initial_lines
            )
            visible_rows = [
                row
                for row in range(log.size.height)
                if log.render_line(row).text.strip()
            ]

            assert app.theme == "textual-light"
            assert not app.current_theme.dark
            assert log.auto_scroll
            assert visible_rows
            last_message_y = log.region.y + visible_rows[-1]
            assert chat_input.region.y - last_message_y - 1 <= 2
            assert not log.region.overlaps(activity.region)

            app._write_conversation("user", "plain user request")
            app._write_system("authoritative host fact")
            app._write_conversation("assistant", "compact model answer")
            await pilot.pause()
            user_line, host_line, model_line = log.lines[-3:]
            assert user_line.text == "你 › plain user request"
            assert user_line._segments[0].style is None
            assert host_line.text == "宿主 › authoritative host fact"
            assert host_line._segments[0].style is not None
            assert host_line._segments[0].style.color == Color.parse("#008080")
            assert model_line.text == "  ▏ 模型 · compact model answer"
            assert "模型自述" not in model_line.text
            assert model_line._segments[0].text == "  ▏ "
            assert model_line._segments[0].style is not None
            assert model_line._segments[0].style.dim
            assert model_line._segments[1].style is not None
            assert model_line._segments[1].style.color == Color.parse("#7d6bab")
            assert not model_line._segments[1].style.dim
            assert model_line._segments[1].style.italic
            assert not model_line._segments[1].style.bold
            app._write_conversation("assistant", "first line\nsecond line")
            await pilot.pause()
            assert [line.text for line in log.lines[-2:]] == [
                "  ▏ 模型 · first line",
                "  ▏        second line",
            ]

            width = log.content_region.width - log.styles.scrollbar_size_vertical
            long_text = "库存预留需要在并发请求下保持原子性和输入不变性。" * 4
            before = len(log.lines)
            app._write_conversation("user", long_text)
            user_lines = log.lines[before:]
            assert len(user_lines) > 1
            assert user_lines[0].text.startswith("你 › ")
            assert all(line.text.startswith(" " * 5) for line in user_lines[1:])
            assert all(cell_len(line.text) <= width for line in user_lines)

            before = len(log.lines)
            app._write_system(long_text)
            host_lines = log.lines[before:]
            assert len(host_lines) > 1
            assert host_lines[0].text.startswith("宿主 › ")
            assert all(line.text.startswith(" " * 7) for line in host_lines[1:])
            assert all(cell_len(line.text) <= width for line in host_lines)

            before = len(log.lines)
            app._write_conversation("assistant", long_text)
            model_lines = log.lines[before:]
            assert len(model_lines) > 1
            assert model_lines[0].text.startswith("  ▏ 模型 · ")
            assert all(
                line.text.startswith("  ▏        ") for line in model_lines[1:]
            )
            assert all(cell_len(line.text) <= width for line in model_lines)
            assert all(
                line._segments[0].style is not None
                and line._segments[0].style.dim
                and line._segments[1].style is not None
                and line._segments[1].style.color == Color.parse("#7d6bab")
                and line._segments[1].style.italic
                and not line._segments[1].style.bold
                for line in model_lines
            )

            for index in range(120):
                app._write_system(f"long line {index:03d}")
            await pilot.pause()
            await pilot.pause()
            assert log.max_scroll_y > 0
            assert log.scroll_y == log.max_scroll_y
            assert not log.region.overlaps(activity.region)
            assert log.region.bottom <= activity.region.y
            app.exit(0)
    finally:
        await runtime.dispose()
        del store


async def test_conversation_rewraps_when_dual_pane_narrows_the_log(
    tmp_path: Path,
) -> None:
    provider = _Provider()
    app, runtime, store, _opened = await _opened_app(tmp_path, provider)
    message = "reservation-handler-keeps-every-request-atomic-without-partial-writes"
    try:
        async with app.run_test(size=(100, 30)) as pilot:
            log = app.query_one("#conversation", RichLog)
            app._write_conversation("assistant", message)
            await pilot.pause()
            model_lines = [line for line in log.lines if line.text.startswith("  ▏")]
            assert len(model_lines) == 1

            await pilot.resize_terminal(110, 30)
            for _ in range(20):
                await pilot.pause()
                width = max(
                    1,
                    log.content_region.width - log.styles.scrollbar_size_vertical,
                )
                model_lines = [
                    line for line in log.lines if line.text.startswith("  ▏")
                ]
                if len(model_lines) > 1 and all(
                    line.cell_length <= width for line in model_lines
                ):
                    break

            assert len(model_lines) > 1
            assert all(line.text.startswith("  ▏") for line in model_lines)
            assert all(line.cell_length <= width for line in model_lines)
            assert log.max_scroll_x == 0
            restored = "".join(
                line.text.removeprefix("  ▏ 模型 · ").removeprefix("  ▏        ")
                for line in model_lines
            )
            assert restored == message
            await pilot.press("ctrl+c")
    finally:
        await runtime.dispose()
        del store


async def test_identity_and_task_conversation_are_real_full_width_screens(
    tmp_path: Path,
) -> None:
    provider = _Provider()
    app, runtime, store, _opened = await _opened_app(tmp_path, provider)
    try:
        async with app.run_test(size=(110, 30)) as pilot:
            home = app.screen
            await pilot.press("ctrl+p")
            assert isinstance(app.screen, ProductIdentityScreen)
            await pilot.press("escape")
            assert app.screen is home

            await pilot.resize_terminal(72, 24)
            await pilot.pause()
            assert app.screen.has_class("narrow")
            await pilot.press("ctrl+t")
            assert isinstance(app.screen, TaskConversationScreen)
            await pilot.press("escape")
            assert app.screen is home
            await pilot.press("ctrl+c")
    finally:
        await runtime.dispose()
        del store


async def test_task_conversation_wraps_and_uses_warning_for_nonzero_exit() -> None:
    role = TaskConversationRole(
        role="coder",
        agent_id="agent-coder",
        session_id="wf-session-coder",
        turns_started=1,
        turns_completed=1,
        tool_calls=1,
        usage_tokens=42,
        usage_quality="exact",
        usage_state="available",
        last_fact_age_seconds=2,
        messages=(
            (
                "tool",
                "shell <已遮蔽 · 参数 128 字节>\t13–15\n完成 · exit=1",
            ),
            ("model", "long-model-message-" * 4),
            ("input", "long-user-requirement-" * 4),
        ),
    )
    snapshot = TaskConversationSnapshot(
        task_id="task-readability",
        roles=(role,),
        observed_at=datetime.now(UTC),
    )
    screen = TaskConversationScreen(
        reader=SimpleNamespace(),  # type: ignore[arg-type]
        observation_reader=None,
        task_id=None,
    )
    app = App[None]()

    async with app.run_test(size=(44, 28)) as pilot:
        await app.push_screen(screen)
        screen._snapshot = snapshot
        screen._selected = 0
        screen._expanded = {0}
        screen._render_snapshot()
        await pilot.pause()

        log = screen.query_one("#task-conversation-log", RichLog)
        width = log.scrollable_content_region.width
        indented = [line for line in log.lines if "▏" in line.text]
        assert indented
        assert all(line.cell_length <= width for line in indented)
        assert not any(re.search(r"13–15$", line.text) for line in log.lines)
        tool = next(line for line in log.lines if "工具 · shell" in line.text)
        assert tool._segments[0].text == "    ▏ "
        assert tool._segments[0].style is not None
        assert tool._segments[0].style.color == Color.parse("blue")
        assert tool._segments[1].style == Style()
        model = next(
            line for line in log.lines if "模型自述（非宿主证据）" in line.text
        )
        assert model._segments[0].style is not None
        assert model._segments[0].style.dim
        assert model._segments[1].style is not None
        assert model._segments[1].style.color == Color.parse("#7d6bab")
        assert model._segments[1].style.italic
        assert not model._segments[1].style.bold
        assert not model._segments[1].style.dim
        warning = next(line for line in log.lines if "完成 · exit=1" in line.text)
        assert warning._segments[0].style is not None
        assert warning._segments[0].style.color == Color.parse("blue")
        assert warning._segments[-1].style is not None
        assert warning._segments[-1].style.color == Color.parse("yellow")

        await pilot.press("escape")


def test_footer_advertises_only_implemented_global_actions() -> None:
    visible = {
        binding.key: (binding.action, binding.description)
        for binding in TracehTuiApp.BINDINGS
        if binding.show
    }

    assert visible == {
        "ctrl+c": ("leave", "退出"),
        "ctrl+q": ("leave", "退出"),
        "ctrl+p": ("identity", "完整身份"),
        "ctrl+t": ("task_conversation", "任务对话"),
    }


async def test_ctrl_q_runs_the_visible_shutdown_path(tmp_path: Path) -> None:
    provider = _Provider()
    app, runtime, store, _opened = await _opened_app(tmp_path, provider)
    try:
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.press("ctrl+q")
        assert app._shutdown_complete
    finally:
        await runtime.dispose()
        del store


async def test_identity_copy_falls_back_to_an_explicit_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _Provider()
    app, runtime, store, opened = await _opened_app(tmp_path, provider)
    fallback_path: Path | None = None

    def _clipboard_unavailable(_app, _value: str) -> None:
        raise RuntimeError("clipboard-unavailable")

    monkeypatch.setattr(TracehTuiApp, "copy_to_clipboard", _clipboard_unavailable)
    try:
        async with app.run_test(size=(110, 30)) as pilot:
            await pilot.press("ctrl+p")
            assert isinstance(app.screen, ProductIdentityScreen)
            await pilot.press("s")
            fallback_path = app.screen.fallback_path
            assert fallback_path is not None
            assert fallback_path.read_text(encoding="utf-8").strip() == (
                opened.session.session_id
            )
            assert str(fallback_path) in str(
                app.screen.query_one("#identity-status", Static).content
            )
            await pilot.press("escape")
            await pilot.press("ctrl+c")
    finally:
        if fallback_path is not None:
            fallback_path.unlink(missing_ok=True)
        await runtime.dispose()
        del store


async def test_ctrl_c_converges_an_active_provider_call(tmp_path: Path) -> None:
    provider = _Provider(gate=True)
    app, runtime, store, opened = await _opened_app(tmp_path, provider)
    try:
        async with app.run_test(size=(90, 28)) as pilot:
            app.query_one("#chat-input", Input).value = "block until cancelled"
            await pilot.press("enter")
            await asyncio.wait_for(provider.started.wait(), timeout=2)
            await pilot.press("ctrl+c")

        events = await runtime.sessions.read_session(opened.session.session_id)
        effects = await runtime.sessions.read_effects(opened.session.session_id)
        assert not CoreInvariantChecker().check(events, effects)
        assert any(event.type == "turn/end" for event in events)
        assert len(provider.requests) == 1
    finally:
        provider.release.set()
        await runtime.dispose()
        del store


async def test_tui_runner_converges_product_and_runtime_when_app_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _Provider()
    runtime, store = _runtime(tmp_path, provider)

    class _Product:
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    product = _Product()

    class _FailingApp:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        async def run_async(self) -> int:
            raise RuntimeError("fixture-app-failed")

    monkeypatch.setattr(tui_app, "TracehTuiApp", _FailingApp)
    with pytest.raises(RuntimeError, match="fixture-app-failed"):
        await run_tui(
            runtime,
            workspace=tmp_path,
            session_id=None,
            timeline=False,
            heartbeat_seconds=0.0,
            product=product,  # type: ignore[arg-type]
        )

    assert product.closed
    with pytest.raises(RuntimeError, match="runtime is disposed"):
        await runtime.run_existing("missing", "too late")
    del store


def _opened_summary(task_id: str) -> ProductTaskSummary:
    offered = proposal(binding=preflight(), session_id=ORIGIN_SESSION)
    return ProductTaskSummary(
        task_id=task_id,
        status=ProductTaskStatus.OPENED,
        requested_mode=offered.requested_mode,
        mode_source=offered.mode_source,
        requirement_digest=offered.requirement_digest,
        profile_digest=offered.preflight.profile_digest,
        preflight_digest=offered.preflight.digest,
        origin_session_id=ORIGIN_SESSION,
        origin_turn_id="origin-turn",
        origin_message_id="origin-message",
        confirmation_session_id=ORIGIN_SESSION,
        confirmation_turn_id="confirm-turn",
        confirmation_message_id="confirm-message",
        head_seq=1,
    )


class _Observer:
    def __init__(
        self,
        observation: ProductObservation,
        *,
        start_started: asyncio.Event | None = None,
        start_release: asyncio.Event | None = None,
    ) -> None:
        self.observation = observation
        self.closed = False
        self._dirty = asyncio.Event()
        self._start_started = start_started
        self._start_release = start_release

    async def start(self) -> ProductObservation:
        if self._start_started is not None:
            self._start_started.set()
        if self._start_release is not None:
            await self._start_release.wait()
        return self.observation

    async def refresh(self) -> ProductObservation:
        return self.observation

    async def wait_dirty(self) -> None:
        await self._dirty.wait()
        self._dirty.clear()

    async def aclose(self) -> None:
        self.closed = True
        self._dirty.set()


class _CurrentTaskReader:
    def __init__(self, observation: ProductObservation | None) -> None:
        self.observation = observation
        self.task_id = None if observation is None else observation.task_id
        self.loads = 0

    async def current_task_id(self, session_id: str) -> str | None:
        assert session_id
        return self.task_id

    async def load(self, task_id: str) -> ProductObservation:
        self.loads += 1
        assert self.observation is not None
        assert task_id == self.observation.task_id
        return self.observation


class _FlakyCurrentTaskReader:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.calls = 0

    async def current_task_id(self, session_id: str) -> str | None:
        assert session_id
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("fixture-observation-read-failed")
        return self.task_id


class _ProductHost:
    def __init__(
        self,
        request: ProductStartRequest,
        observation: ProductObservation,
        *,
        restore_task: bool = False,
        gate_command: bool = False,
        gate_start: bool = False,
        gate_observer: bool = False,
        gate_close: bool = False,
    ) -> None:
        self.request = request
        self.observation = _CurrentTaskReader(observation if restore_task else None)
        self._observed = observation
        self.observers: list[_Observer] = []
        self.started_requests: list[ProductStartRequest] = []
        self.start_started = asyncio.Event()
        self.start_release = asyncio.Event()
        self.commands = []
        self.command_started = asyncio.Event()
        self.command_release = asyncio.Event()
        self.gate_command = gate_command
        self.gate_start = gate_start
        self.observer_started = asyncio.Event()
        self.observer_release = asyncio.Event()
        self.gate_observer = gate_observer
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()
        self.gate_close = gate_close

    def observe(self, task_id: str) -> _Observer:
        assert task_id == self._observed.task_id
        observer = _Observer(
            self._observed,
            start_started=self.observer_started if self.gate_observer else None,
            start_release=self.observer_release if self.gate_observer else None,
        )
        self.observers.append(observer)
        return observer

    async def prepare_turn(self, session_id: str, text: str) -> ProductChatTurn:
        assert session_id == self.request.session_id
        return ProductChatTurn(
            TurnInput(content=text, message_id=str(uuid4()), source="user"),
            had_pending_proposal=True,
        )

    async def resolve_turn(self, session_id, prepared, *, turn_id):
        del session_id, prepared, turn_id
        return ProductTurnResolution(start_request=self.request)

    async def start(self, request: ProductStartRequest) -> ProductAdvanceResult:
        self.started_requests.append(request)
        self.start_started.set()
        if self.gate_start:
            await self.start_release.wait()
        return ProductAdvanceResult(
            summary=replace(
                self._observed.summary,
                status=ProductTaskStatus.FAILED,
                failure_code="fixture-finished",
            )
        )

    async def execute_command(self, command):
        self.commands.append(command)
        self.command_started.set()
        if self.gate_command:
            await self.command_release.wait()
        return ProductCommandResult(
            command,
            advance=ProductAdvanceResult(summary=self._observed.summary),
        )

    async def discard_turn(self, *args) -> None:
        del args

    async def aclose(self) -> None:
        self.observer_release.set()
        self.start_release.set()
        self.command_release.set()
        self.close_started.set()
        if self.gate_close:
            await self.close_release.wait()


class _RefreshClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.waiters: asyncio.Queue[tuple[float, asyncio.Future[None]]] = (
            asyncio.Queue()
        )

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        gate = asyncio.get_running_loop().create_future()
        await self.waiters.put((seconds, gate))
        await gate
        self.now += seconds

    async def advance_until(self, expected_seconds: float) -> None:
        for _ in range(10):
            seconds, gate = await asyncio.wait_for(self.waiters.get(), timeout=2)
            gate.set_result(None)
            if seconds == expected_seconds:
                return
        raise AssertionError(f"no clock waiter for {expected_seconds}")


class _AwaitingProductStore(InMemoryEventStore):
    """Expose the durable Approval barrier as a deterministic test signal."""

    def __init__(self) -> None:
        super().__init__()
        self.awaiting = asyncio.Event()

    async def append(
        self,
        stream_id: str,
        *,
        expected_seq: int,
        events: tuple[PendingEvent, ...],
        durability=None,
    ):
        appended = await super().append(
            stream_id,
            expected_seq=expected_seq,
            events=events,
            durability=durability,
        )
        if any(event.type == PRODUCT_TASK_AWAITING for event in events):
            self.awaiting.set()
        return appended


def _start_fixture(task_id: str = "task-tui"):
    pending = PendingProductProposal(
        task_id=task_id,
        proposal=proposal(binding=preflight(), session_id=ORIGIN_SESSION),
        profile_id="profile-tui",
        requirement="change [bold]inventory[/bold] safely\x1b[2J",
    )
    request = ProductStartRequest(
        pending=pending,
        session_id=ORIGIN_SESSION,
        confirming_turn_id="confirm-turn",
        confirming_message_id="confirm-message",
    )
    summary = _opened_summary(task_id)
    observation = ProductObservation(
        task_id=task_id,
        summary=summary,
        workflow=None,
        evidence=None,
        review=None,
        approval=None,
        promotion=None,
        approval_digest=None,
        stream_heads=(),
        observed_at=datetime.now(UTC),
    )
    return request, observation


async def test_product_view_periodically_refreshes_sqlite_without_a_feed_notification(
    tmp_path: Path,
) -> None:
    request, observation = _start_fixture("task-periodic-refresh")
    durable_store = SqliteEventStore(tmp_path / "external-events")
    state_stream = "product-refresh-probe"

    class _DurableReader:
        async def load(self, task_id: str) -> ProductObservation:
            assert task_id == observation.task_id
            head = await durable_store.head(state_stream)
            summary = observation.summary
            if head > 0:
                summary = replace(
                    summary,
                    status=ProductTaskStatus.FAILED,
                    failure_code="external-writer-failed",
                )
            latest = None
            if head:
                latest = (await durable_store.read(state_stream, from_seq=head))[-1]
            return replace(
                observation,
                summary=summary,
                stream_heads=(
                    ObservedStreamHead(
                        state_stream,
                        head,
                        None if latest is None else latest.type,
                        None if latest is None else latest.occurred_at,
                        True,
                    ),
                ),
                observed_at=datetime.now(UTC),
            )

    observer = ProductObservationSession(  # type: ignore[arg-type]
        _DurableReader(), SessionEventFeed(), observation.task_id
    )

    class _DurableHost(_ProductHost):
        def observe(self, task_id: str) -> ProductObservationSession:  # type: ignore[override]
            assert task_id == observation.task_id
            return observer

    host = _DurableHost(request, observation, restore_task=True)
    provider = _Provider()
    runtime, store = _runtime(tmp_path, provider)
    session_id = await runtime.create_session(tmp_path, metadata={"cli": "chat"})
    opened = await open_chat_session(runtime, workspace=None, session_id=session_id)
    refresh_clock = _RefreshClock()
    app = TracehTuiApp(
        runtime,
        opened,
        timeline=False,
        heartbeat_seconds=0.0,
        product=host,  # type: ignore[arg-type]
        clock=Clock(refresh_clock.monotonic, refresh_clock.sleep),
    )
    try:
        async with app.run_test(size=(110, 30)) as pilot:
            assert "durable 开 ✓" in str(
                app.query_one("#product-state", Static).content
            )
            await durable_store.append(
                state_stream,
                expected_seq=0,
                events=(PendingEvent(type="probe/state-changed", data={}),),
            )
            assert not observer.dirty

            await refresh_clock.advance_until(DEFAULT_HEARTBEAT_SECONDS)
            for _ in range(3):
                await pilot.pause()

            assert "任务已记录失败 · external-writer-failed" in str(
                app.query_one("#product-state", Static).content
            )
            app.exit(0)
    finally:
        await runtime.dispose()
        await durable_store.aclose()
        del store


async def test_initial_observation_failure_is_visible_and_periodically_recovers(
    tmp_path: Path,
) -> None:
    request, observation = _start_fixture("task-observation-recovery")
    host = _ProductHost(request, observation)
    reader = _FlakyCurrentTaskReader(observation.task_id)
    host.observation = reader
    provider = _Provider()
    runtime, store = _runtime(tmp_path, provider)
    session_id = await runtime.create_session(tmp_path, metadata={"cli": "chat"})
    opened = await open_chat_session(runtime, workspace=None, session_id=session_id)
    refresh_clock = _RefreshClock()
    app = TracehTuiApp(
        runtime,
        opened,
        timeline=False,
        heartbeat_seconds=1.0,
        product=host,  # type: ignore[arg-type]
        clock=Clock(refresh_clock.monotonic, refresh_clock.sleep),
    )
    try:
        async with app.run_test(size=(100, 30)) as pilot:
            failed = str(app.query_one("#product-state", Static).content)
            assert "ProductTask 状态暂不可读" in failed
            assert "product-observation-unavailable" in failed
            assert "当前：尚无提案" not in failed

            await refresh_clock.advance_until(1.0)
            for _ in range(4):
                await pilot.pause()
            recovered = str(app.query_one("#product-state", Static).content)
            assert reader.calls == 2
            assert app._observation is not None
            assert app._observation_error is None
            assert "product-observation-unavailable" not in recovered
            assert "任务已打开" in str(
                app.query_one("#gate-message", Static).content
            )
            await pilot.press("ctrl+c")
    finally:
        await runtime.dispose()
        del store


async def test_successful_refresh_clears_only_the_stale_observation_error(
    tmp_path: Path,
) -> None:
    request, observation = _start_fixture("task-refresh-recovery")

    class _RecoveringObserver(_Observer):
        def __init__(self) -> None:
            super().__init__(observation)
            self.refresh_calls = 0

        async def refresh(self) -> ProductObservation:
            self.refresh_calls += 1
            if self.refresh_calls == 1:
                raise RuntimeError("fixture-refresh-failed")
            return self.observation

    observer = _RecoveringObserver()

    class _RecoveringHost(_ProductHost):
        def observe(self, task_id: str) -> _RecoveringObserver:  # type: ignore[override]
            assert task_id == observation.task_id
            return observer

    host = _RecoveringHost(request, observation, restore_task=True)
    provider = _Provider()
    runtime, store = _runtime(tmp_path, provider)
    session_id = await runtime.create_session(tmp_path, metadata={"cli": "chat"})
    opened = await open_chat_session(runtime, workspace=None, session_id=session_id)
    app = TracehTuiApp(
        runtime,
        opened,
        timeline=False,
        heartbeat_seconds=0.0,
        product=host,  # type: ignore[arg-type]
        clock=default_clock(),
    )
    try:
        async with app.run_test(size=(100, 30)) as pilot:
            observer._dirty.set()
            for _ in range(10):
                await pilot.pause()
                if app._observation_error is not None:
                    break
            assert app._observation_error is not None
            assert "product-observation-unavailable" in str(
                app.query_one("#product-state", Static).content
            )

            observer._dirty.set()
            for _ in range(10):
                await pilot.pause()
                if observer.refresh_calls >= 2:
                    break
            assert observer.refresh_calls == 2
            assert app._observation_error is None
            assert "product-observation-unavailable" not in str(
                app.query_one("#product-state", Static).content
            )
            await pilot.press("ctrl+c")
    finally:
        await runtime.dispose()
        del store


async def test_concurrent_product_refreshes_cannot_overwrite_newer_facts(
    tmp_path: Path,
) -> None:
    request, opened = _start_fixture("task-refresh-order")
    stale = replace(
        opened,
        summary=replace(opened.summary, status=ProductTaskStatus.STARTED),
        workflow=SimpleNamespace(status=WorkflowStatus.RUNNING),
    )
    fresh = replace(
        opened,
        summary=replace(
            opened.summary,
            status=ProductTaskStatus.AWAITING_APPROVAL,
            review_id="review-refresh-order",
        ),
        workflow=SimpleNamespace(status=WorkflowStatus.AWAITING_APPROVAL),
    )

    class _RacingObserver(_Observer):
        def __init__(self) -> None:
            super().__init__(opened)
            self.calls = 0
            self.first_started = asyncio.Event()
            self.first_release = asyncio.Event()

        async def refresh(self) -> ProductObservation:
            self.calls += 1
            if self.calls == 1:
                self.first_started.set()
                await self.first_release.wait()
                return stale
            return fresh

    observer = _RacingObserver()

    class _RacingHost(_ProductHost):
        def observe(self, task_id: str) -> _RacingObserver:  # type: ignore[override]
            assert task_id == opened.task_id
            return observer

    host = _RacingHost(request, opened, restore_task=True)
    provider = _Provider()
    runtime, store = _runtime(tmp_path, provider)
    session_id = await runtime.create_session(tmp_path, metadata={"cli": "chat"})
    actual = await open_chat_session(runtime, workspace=None, session_id=session_id)
    app = TracehTuiApp(
        runtime,
        actual,
        timeline=False,
        heartbeat_seconds=0.0,
        product=host,  # type: ignore[arg-type]
        clock=default_clock(),
    )
    try:
        async with app.run_test(size=(100, 30)) as pilot:
            first = asyncio.create_task(app._refresh_observation())
            await asyncio.wait_for(observer.first_started.wait(), timeout=2)
            second = asyncio.create_task(app._refresh_observation())
            await pilot.pause()
            assert observer.calls == 1
            observer.first_release.set()
            await asyncio.gather(first, second)
            assert app._observation is fresh
            assert "审批 ⋯" in str(
                app.query_one("#product-state", Static).content
            )
            await pilot.press("ctrl+c")
    finally:
        observer.first_release.set()
        await runtime.dispose()
        del store


async def test_line_adapter_passes_the_same_frozen_start_identity(
    tmp_path: Path,
) -> None:
    request, observation = _start_fixture("task-shared-identity")
    host = _ProductHost(request, observation)
    output: list[str] = []
    console = Console(read_line=lambda _prompt: "START", write=output.append)
    adapter = LineProductAdapter(host, data_dir=tmp_path)  # type: ignore[arg-type]

    await adapter.finish_turn(
        request.session_id,
        ProductChatTurn(
            TurnInput(content="confirm", message_id="message-line", source="user"),
            had_pending_proposal=True,
        ),
        turn_id="turn-line",
        console=console,
    )

    assert host.started_requests == [request]
    assert any(request.pending.task_id in line for line in output)
    await adapter.aclose()


async def test_real_auto_product_host_reaches_approval_through_the_tui(
    tmp_path: Path,
) -> None:
    source, _base = build_source_repository(tmp_path / "source")
    target = make_bare_target(source, tmp_path / "target.git")
    store = _AwaitingProductStore()
    actions = ProductTurnActions()
    product = await _build_host(
        tmp_path,
        store,
        source,
        target,
        LocalArtifactCas(tmp_path / "cas"),
        actions,
        RequestedTaskMode.AUTO,
        line_adapter=False,
    )
    chat_requests: list[ModelRequest] = []
    runtime = _chat_runtime(
        tmp_path,
        store,
        actions,
        _SideEffectAttemptingChatProvider(chat_requests),
    )
    opened = await open_chat_session(runtime, workspace=source, session_id=None)
    app = TracehTuiApp(
        runtime,
        opened,
        timeline=True,
        heartbeat_seconds=1.0,
        product=product,
        clock=default_clock(),
    )
    try:
        async with app.run_test(size=(120, 38)) as pilot:
            app.query_one("#chat-input", Input).value = "propose controlled work"
            await pilot.press("enter")
            proposal_turn = app._operation_task
            if proposal_turn is not None:
                await asyncio.wait_for(asyncio.shield(proposal_turn), timeout=5)
            assert "已提议" in str(
                app.query_one("#gate-message", Static).content
            )
            app.query_one("#chat-input", Input).value = "confirm controlled work"
            await pilot.press("enter")
            confirmation_turn = app._operation_task
            if confirmation_turn is not None:
                await asyncio.wait_for(asyncio.shield(confirmation_turn), timeout=5)
            assert app.query_one("#gate-primary", Button).display
            assert str(app.query_one("#gate-primary", Button).label) == "START"
            await pilot.click("#gate-primary")
            confirmation = app.query_one("#confirmation-input", Input)
            await _wait_for_confirmation_focus(pilot, confirmation)
            confirmation.value = "START"
            await pilot.press("enter")
            await pilot.pause()
            starting_panel = str(app.query_one("#product-state", Static).content)
            assert (
                "START 已被宿主接受" in starting_panel
                or "durable 开 ✓" in starting_panel
            )
            operation = app._operation_task
            assert operation is not None
            try:
                await asyncio.wait_for(store.awaiting.wait(), timeout=45)
            except TimeoutError:
                panel = str(app.query_one("#product-state", Static).content)
                stack = [frame.f_code.co_name for frame in operation.get_stack()]
                raise AssertionError((panel, operation.done(), stack)) from None
            await asyncio.wait_for(asyncio.shield(operation), timeout=5)
            panel = str(app.query_one("#product-state", Static).content)
            assert app.query_one("#gate-primary", Button).display, panel
            assert str(app.query_one("#gate-primary", Button).label) == "批准"
            assert "auto → multi" in panel
            assert "证据" in panel
            assert sum(
                bool(line.strip()) and set(line.strip()) == {"─"}
                for line in panel.splitlines()
            ) == 3
            assert PRODUCT_TASK_AWAITING in panel
            assert git("status", "--porcelain", cwd=source) == ""
            assert chat_requests
            observation = app._observation
            assert observation is not None
            heads = {head.stream_id: head for head in observation.stream_heads}
            for stream_id in (
                AGENT_DIRECTORY_STREAM,
                ARTIFACT_CATALOG_STREAM,
                BUDGET_LEDGER_STREAM,
                PROMOTION_LEDGER_STREAM,
            ):
                assert not heads[stream_id].task_bound
            assert all(
                head.task_bound
                for stream_id, head in heads.items()
                if stream_id
                not in {
                    AGENT_DIRECTORY_STREAM,
                    ARTIFACT_CATALOG_STREAM,
                    BUDGET_LEDGER_STREAM,
                    PROMOTION_LEDGER_STREAM,
                }
            )
            await pilot.press("ctrl+t")
            assert isinstance(app.screen, TaskConversationScreen)
            await pilot.pause()
            conversation_log = app.screen.query_one(
                "#task-conversation-log", RichLog
            )
            for _ in range(20):
                if conversation_log.lines:
                    break
                await pilot.pause()
            rendered_roles = "\n".join(line.text for line in conversation_log.lines)
            for role in ("router", "parent", "reviewer", "coder"):
                assert role in rendered_roles
            assert "模型自述（非宿主证据）" in rendered_roles
            assert "▏" in rendered_roles
            assert "│" not in rendered_roles
            sequenced_tools = [
                line
                for line in conversation_log.lines
                if re.search(r"\d+–\d+$", line.text)
            ]
            assert sequenced_tools
            sequence_widths = [cell_len(line.text) for line in sequenced_tools]
            assert all(
                cell_len(line.text)
                == conversation_log.scrollable_content_region.width
                for line in sequenced_tools
            ), (sequence_widths, conversation_log.scrollable_content_region.width)
            assert all(
                line._segments[-1].style is not None
                and line._segments[-1].style.dim
                for line in sequenced_tools
            )
            wide_sequence_count = len(sequenced_tools)
            await pilot.resize_terminal(44, 38)
            for _ in range(20):
                await pilot.pause()
                sequenced_tools = [
                    line
                    for line in conversation_log.lines
                    if re.search(r"\d+–\d+$", line.text)
                ]
                if len(sequenced_tools) < wide_sequence_count:
                    break
            assert len(sequenced_tools) < wide_sequence_count
            available_width = conversation_log.scrollable_content_region.width
            indented_message_lines = [
                line for line in conversation_log.lines if "▏" in line.text
            ]
            assert indented_message_lines
            assert all(
                line.cell_length <= available_width
                for line in indented_message_lines
            )
            await pilot.press("escape")
            await pilot.press("ctrl+c")
    finally:
        await product.aclose()
        await runtime.dispose()


async def test_model_confirmation_cannot_bypass_the_tui_start_gesture(
    tmp_path: Path,
) -> None:
    request, observation = _start_fixture()
    observation = replace(
        observation,
        stream_heads=(
            ObservedStreamHead(
                f"product-task:{observation.task_id}",
                1,
                "product/task-opened",
                observation.observed_at - timedelta(seconds=19),
                True,
            ),
        ),
    )
    host = _ProductHost(
        request,
        observation,
        gate_start=True,
        gate_observer=True,
    )
    provider = _Provider()
    runtime, store = _runtime(tmp_path, provider)
    # The fake Product identities deliberately match this real Chat Session.
    session_id = await runtime.create_session(tmp_path, metadata={"cli": "chat"})
    actual = await open_chat_session(runtime, workspace=None, session_id=session_id)
    host.request = replace(host.request, session_id=session_id)
    host.request = replace(
        host.request,
        pending=replace(
            host.request.pending,
            proposal=replace(
                host.request.pending.proposal,
                origin_session_id=session_id,
            ),
        ),
    )
    host.observation = _CurrentTaskReader(None)
    refresh_clock = _RefreshClock()
    app = TracehTuiApp(
        runtime,
        actual,
        timeline=False,
        heartbeat_seconds=0.0,
        product=host,  # type: ignore[arg-type]
        clock=Clock(refresh_clock.monotonic, refresh_clock.sleep),
    )
    try:
        async with app.run_test(size=(110, 34)) as pilot:
            app.query_one("#chat-input", Input).value = "I confirm the proposal"
            await pilot.press("enter")
            await asyncio.wait_for(provider.started.wait(), timeout=2)
            for _ in range(4):
                await pilot.pause()
            assert host.started_requests == []
            start_button = app.query_one("#gate-primary", Button)
            assert start_button.display
            assert str(start_button.label) == "START"
            assert not app.query_one("#gate-secondary", Button).display
            assert app.theme == "textual-light"
            assert not app.current_theme.dark
            assert start_button.has_class("gate-action")
            assert start_button.styles.background.a == 0
            assert start_button.styles.border_top[0] == "round"
            product_state = app.query_one("#product-state", Static)
            visible_rows = [
                row
                for row in range(product_state.size.height)
                if product_state.render_line(row).text.strip()
            ]
            assert visible_rows[0] <= 1
            assert start_button.region.width < product_state.region.width // 2
            panel = str(product_state.content)
            assert "change [bol" in panel
            assert "\x1b" not in panel
            await pilot.click("#gate-primary")
            confirmation = app.query_one("#confirmation-input", Input)
            await _wait_for_confirmation_focus(pilot, confirmation)
            assert confirmation.display
            assert str(product_state.content) == panel
            assert product_state.region.height > 0
            assert any(
                product_state.render_line(row).text.strip()
                for row in range(product_state.size.height)
            )
            assert app.query_one("#confirmation-panel").region.y >= product_state.region.bottom
            assert str(app.query_one("#confirmation-label", Static).content) == (
                "输入 START 确认 · Esc 取消"
            )
            assert len(app.query("#cancel-confirmation")) == 0
            await pilot.press("escape")
            await pilot.pause()
            assert app._confirmation_action is None
            assert not app.query_one("#confirmation-panel").display
            assert str(product_state.content) == panel
            assert app.query_one("#chat-input", Input).has_focus
            await pilot.click("#gate-primary")
            await _wait_for_confirmation_focus(pilot, confirmation)
            confirmation.value = "start"
            await pilot.press("enter")
            assert host.started_requests == []
            assert "请完整输入 START" in str(
                app.query_one("#confirmation-label", Static).content
            )
            confirmation.value = "START"
            await pilot.press("enter")
            await asyncio.wait_for(host.observer_started.wait(), timeout=0.5)
            assert "START 已被宿主接受 · 等待返回" in str(
                app.query_one("#product-state", Static).content
            )
            assert "尚未观察到 durable 任务事实" not in str(
                app.query_one("#product-state", Static).content
            )
            host.observer_release.set()
            await asyncio.wait_for(host.start_started.wait(), timeout=2)
            assert host.started_requests == [host.request]
            assert "START 已被宿主接受 · 等待返回" in str(
                app.query_one("#product-state", Static).content
            )
            assert "product/task-opened" in str(
                app.query_one("#product-state", Static).content
            )
            assert "19 秒前" in str(
                app.query_one("#product-state", Static).content
            )
            assert app.query_one("#chat-input", Input).disabled
            refresh_clock.now = 24.0
            await refresh_clock.advance_until(1.0)
            await pilot.pause()
            stalled = str(app.query_one("#product-state", Static).content)
            assert "START 已被宿主接受 · 等待返回" in stalled
            assert "警告：无新任务事实" in stalled
            assert "19 秒前" not in stalled
            await pilot.press("ctrl+c")
    finally:
        host.start_release.set()
        await runtime.dispose()
        del store


async def test_new_confirmed_proposal_replaces_a_terminal_task_view(
    tmp_path: Path,
) -> None:
    _old_request, old_observation = _start_fixture("task-finished")
    old_observation = replace(
        old_observation,
        summary=replace(
            old_observation.summary,
            status=ProductTaskStatus.FAILED,
            failure_code="provider-response-invalid",
        ),
    )
    next_request, _next_observation = _start_fixture("task-next")
    next_request = replace(
        next_request,
        pending=replace(
            next_request.pending,
            requirement="Refine a bounded service contract and run its checks",
        ),
    )

    class _NextTaskHost(_ProductHost):
        resolutions = 0

        async def resolve_turn(self, session_id, prepared, *, turn_id):
            del session_id, prepared, turn_id
            self.resolutions += 1
            if self.resolutions == 1:
                return ProductTurnResolution(proposal=self.request.pending)
            return ProductTurnResolution(start_request=self.request)

    host = _NextTaskHost(next_request, old_observation, restore_task=True)
    provider = _Provider()
    runtime, store = _runtime(tmp_path, provider)
    session_id = await runtime.create_session(tmp_path, metadata={"cli": "chat"})
    actual = await open_chat_session(runtime, workspace=None, session_id=session_id)
    host.request = replace(host.request, session_id=session_id)
    host.request = replace(
        host.request,
        pending=replace(
            host.request.pending,
            proposal=replace(
                host.request.pending.proposal,
                origin_session_id=session_id,
            ),
        ),
    )
    app = TracehTuiApp(
        runtime,
        actual,
        timeline=False,
        heartbeat_seconds=10.0,
        product=host,  # type: ignore[arg-type]
        clock=default_clock(),
    )
    try:
        async with app.run_test(size=(110, 34)) as pilot:
            assert "任务已记录失败" in str(
                app.query_one("#product-state", Static).content
            )

            app.query_one("#chat-input", Input).value = "propose the next task"
            await pilot.press("enter")
            await asyncio.wait_for(provider.started.wait(), timeout=2)
            for _ in range(4):
                await pilot.pause()
            operation = app._operation_task
            if operation is not None:
                await asyncio.wait_for(asyncio.shield(operation), timeout=2)
            await pilot.pause()

            panel = str(app.query_one("#product-state", Static).content)
            assert "Refine a bo" in panel
            assert "provider-response-invalid" not in panel
            assert not app.query_one("#gate-primary", Button).display
            assert app._task_id == host.request.pending.task_id
            assert len(host.observers) == 1
            assert host.observers[0].closed

            provider.started.clear()
            app.query_one("#chat-input", Input).value = "confirm the next proposal"
            await pilot.press("enter")
            await asyncio.wait_for(provider.started.wait(), timeout=2)
            for _ in range(4):
                await pilot.pause()
            operation = app._operation_task
            if operation is not None:
                await asyncio.wait_for(asyncio.shield(operation), timeout=2)
            await pilot.pause()

            start = app.query_one("#gate-primary", Button)
            panel = str(app.query_one("#product-state", Static).content)
            assert start.display, panel
            assert str(start.label) == "START"
            assert "Refine a bo" in panel
            assert "provider-response-invalid" not in panel
            assert app._task_id == host.request.pending.task_id
            assert len(host.observers) == 1
            assert host.observers[0].closed
            await pilot.press("ctrl+c")
    finally:
        await runtime.dispose()
        del store


async def test_running_start_keeps_typed_cancel_reachable(tmp_path: Path) -> None:
    request, opened_observation = _start_fixture("task-running-cancel")
    host = _ProductHost(
        request,
        opened_observation,
        gate_command=True,
        gate_start=True,
    )
    provider = _Provider()
    runtime, store = _runtime(tmp_path, provider)
    session_id = await runtime.create_session(tmp_path, metadata={"cli": "chat"})
    actual = await open_chat_session(runtime, workspace=None, session_id=session_id)
    app = TracehTuiApp(
        runtime,
        actual,
        timeline=False,
        heartbeat_seconds=0.0,
        product=host,  # type: ignore[arg-type]
        clock=default_clock(),
    )
    try:
        async with app.run_test(size=(110, 34)) as pilot:
            app._proposal = request.pending
            app._start_request = request
            app._task_id = request.pending.task_id
            app._refresh_product_view()
            await pilot.pause()
            start = app.query_one("#gate-primary", Button)
            assert start.display
            assert str(start.label) == "START"
            await pilot.click("#gate-primary")
            confirmation = app.query_one("#confirmation-input", Input)
            await _wait_for_confirmation_focus(pilot, confirmation)
            assert app._confirmation_action is ProductGateAction.START
            confirmation.value = "START"
            await pilot.press("enter")
            await asyncio.wait_for(host.start_started.wait(), timeout=2)

            observer = app._observer
            assert isinstance(observer, _Observer)
            observer.observation = replace(
                opened_observation,
                summary=replace(
                    opened_observation.summary,
                    status=ProductTaskStatus.STARTED,
                ),
                workflow=SimpleNamespace(status=WorkflowStatus.RUNNING),
            )
            await app._refresh_observation()
            await pilot.pause()

            cancel = app.query_one("#gate-primary", Button)
            assert cancel.display
            assert str(cancel.label) == "取消任务"
            assert app._busy
            assert app._operation_name == "start"
            assert app._gate_actions == (ProductGateAction.CANCEL,)
            assert not cancel.disabled
            await pilot.click("#gate-primary")
            confirmation = app.query_one("#confirmation-input", Input)
            await _wait_for_confirmation_focus(pilot, confirmation)
            assert app._confirmation_action is ProductGateAction.CANCEL
            confirmation.value = "cancel"
            await pilot.press("enter")
            assert host.commands == []
            assert "请完整输入 CANCEL" in str(
                app.query_one("#confirmation-label", Static).content
            )
            confirmation.value = "CANCEL"
            await pilot.press("enter")
            await asyncio.wait_for(host.command_started.wait(), timeout=2)

            assert len(host.commands) == 1
            assert host.commands[0].operation is ProductCommandOperation.CANCEL
            assert host.commands[0].task_id == request.pending.task_id
            operation = app._operation_task
            assert operation is not None
            host.command_release.set()
            await asyncio.wait_for(asyncio.shield(operation), timeout=2)
            app.exit(0)
    finally:
        host.start_release.set()
        await runtime.dispose()
        del store


async def test_real_running_product_is_cancelled_through_the_existing_owner(
    tmp_path: Path,
) -> None:
    source, _base = build_source_repository(tmp_path / "source")
    target = make_bare_target(source, tmp_path / "target.git")
    store = InMemoryEventStore()
    actions = ProductTurnActions()
    product_provider = _GatedProductProvider()
    product = await _build_host(
        tmp_path,
        store,
        source,
        target,
        LocalArtifactCas(tmp_path / "cas"),
        actions,
        RequestedTaskMode.SINGLE,
        product_provider,
        line_adapter=False,
    )
    runtime = _chat_runtime(tmp_path, store, actions)
    workspace = tmp_path / "requester"
    workspace.mkdir()
    opened = await open_chat_session(runtime, workspace=workspace, session_id=None)
    app = TracehTuiApp(
        runtime,
        opened,
        timeline=True,
        heartbeat_seconds=1.0,
        product=product,
        clock=default_clock(),
    )
    try:
        async with app.run_test(size=(120, 38)) as pilot:
            app.query_one("#chat-input", Input).value = "please add the accepted file"
            await pilot.press("enter")
            proposal_turn = app._operation_task
            if proposal_turn is not None:
                await asyncio.wait_for(asyncio.shield(proposal_turn), timeout=5)

            app.query_one("#chat-input", Input).value = "yes, do it"
            await pilot.press("enter")
            confirmation_turn = app._operation_task
            if confirmation_turn is not None:
                await asyncio.wait_for(asyncio.shield(confirmation_turn), timeout=5)
            for _ in range(20):
                await pilot.pause()
                start = app.query_one("#gate-primary", Button)
                if start.display and str(start.label) == "START":
                    break
            else:
                raise AssertionError(
                    str(app.query_one("#product-state", Static).content)
                )

            start.press()
            confirmation = app.query_one("#confirmation-input", Input)
            await _wait_for_confirmation_focus(pilot, confirmation)
            assert app._confirmation_action is ProductGateAction.START
            confirmation.value = "START"
            await pilot.press("enter")
            await asyncio.wait_for(product_provider.entered.wait(), timeout=10)

            for _ in range(100):
                await pilot.pause()
                cancel = app.query_one("#gate-primary", Button)
                if cancel.display and str(cancel.label) == "取消任务":
                    break
            else:
                raise AssertionError(
                    str(app.query_one("#product-state", Static).content)
                )

            task_id = app._task_id
            assert task_id is not None
            cancel.press()
            confirmation = app.query_one("#confirmation-input", Input)
            await _wait_for_confirmation_focus(pilot, confirmation)
            assert app._confirmation_action is ProductGateAction.CANCEL
            confirmation.value = "CANCEL"
            await pilot.press("enter")
            await pilot.pause()
            cancellation = app._operation_task
            assert cancellation is not None
            await asyncio.wait_for(asyncio.shield(cancellation), timeout=15)

            summary = await ProductTaskStreamReader(store).load(task_id)
            assert summary is not None
            assert summary.status is ProductTaskStatus.CANCELLED
            assert summary.reason_code == "user-cancelled"
            assert product_provider.entered.is_set()
            app.exit(0)
    finally:
        await product.aclose()
        await runtime.dispose()


async def test_approval_button_requires_durable_review_and_serializes_clicks(
    tmp_path: Path,
) -> None:
    request, opened_observation = _start_fixture("task-approval")
    summary = replace(
        opened_observation.summary,
        status=ProductTaskStatus.AWAITING_APPROVAL,
        review_id="review-tui",
    )
    review = SimpleNamespace(
        review_id="review-tui",
        patch_sha256="a" * 64,
        target_ref="refs/heads/main",
        expected_revision="b" * 40,
        integration_commit="c" * 40,
    )
    observation = replace(
        opened_observation,
        summary=summary,
        workflow=SimpleNamespace(status=WorkflowStatus.AWAITING_APPROVAL),
        evidence=ProductTaskEvidence("awaiting_approval", (), None),
        review=review,
        approval_digest="d" * 64,
    )
    host = _ProductHost(
        request,
        observation,
        restore_task=True,
        gate_command=True,
    )
    provider = _Provider()
    runtime, store = _runtime(tmp_path, provider)
    session_id = await runtime.create_session(tmp_path, metadata={"cli": "chat"})
    actual = await open_chat_session(runtime, workspace=None, session_id=session_id)
    host.observation = _CurrentTaskReader(observation)
    app = TracehTuiApp(
        runtime,
        actual,
        timeline=False,
        heartbeat_seconds=0.0,
        product=host,  # type: ignore[arg-type]
        clock=default_clock(),
    )
    try:
        async with app.run_test(size=(110, 34)) as pilot:
            approve = app.query_one("#gate-primary", Button)
            reject = app.query_one("#gate-secondary", Button)
            assert approve.display and str(approve.label) == "批准"
            assert reject.display and str(reject.label) == "驳回"
            assert approve.has_class("gate-action")
            assert reject.has_class("gate-action")
            assert approve.styles.background.a == 0
            assert reject.styles.background.a == 0
            assert approve.styles.border_top[0] == "round"
            assert reject.styles.border_top[0] == "round"
            panel = str(app.query_one("#product-state", Static).content)
            assert "审批    review-tui" in panel
            assert "→  main @" in panel
            assert ("d" * 12) in panel
            assert ("d" * 64) not in panel
            stale_observation = app._observation
            assert stale_observation is not None
            host.observation.observation = replace(
                observation,
                approval_digest="e" * 64,
            )
            await pilot.press("ctrl+p")
            assert isinstance(app.screen, ProductIdentityScreen)
            assert any(field.value == ("e" * 64) for field in app.screen.fields)
            assert all(field.value != ("d" * 64) for field in app.screen.fields)
            assert host.observation.loads == 1
            await pilot.press("escape")
            host.observation.observation = observation
            await pilot.press("ctrl+t")
            assert isinstance(app.screen, TaskConversationScreen)
            assert host.observation.loads == 2
            await pilot.press("escape")
            await pilot.double_click("#gate-primary")
            assert host.commands == []
            confirmation = app.query_one("#confirmation-input", Input)
            await _wait_for_confirmation_focus(pilot, confirmation)
            confirmation.value = "APPROVE"
            await pilot.press("enter")
            await asyncio.wait_for(host.command_started.wait(), timeout=2)
            assert len(host.commands) == 1
            assert host.commands[0].operation is ProductCommandOperation.APPROVE
            assert host.commands[0].task_id == observation.task_id
            assert [field.name for field in fields(host.commands[0])] == [
                "operation",
                "task_id",
            ]
            completed = replace(
                observation,
                summary=replace(
                    summary,
                    status=ProductTaskStatus.COMPLETED,
                    promotion_id="promotion-tui",
                ),
                workflow=SimpleNamespace(status=WorkflowStatus.COMPLETED),
            )
            host._observed = completed
            host.observation.observation = completed
            for observer in host.observers:
                observer.observation = completed
            operation = app._operation_task
            assert operation is not None
            host.command_release.set()
            await asyncio.wait_for(asyncio.shield(operation), timeout=2)
            await pilot.pause()
            panel = str(app.query_one("#product-state", Static).content)
            gate = str(app.query_one("#gate-message", Static).content)
            combined = f"{panel}\n{gate}"
            assert combined.count("已合入 · Promotion receipt 已记录") == 1
            assert "完成：Promotion receipt 已记录" not in combined
            assert "任务已经到达 durable 终态" not in combined
            conversation = "\n".join(
                line.text for line in app.query_one("#conversation", RichLog).lines
            )
            assert (
                "宿主 › 批准已完成；ProductTask 已到达 durable 状态：completed。"
                in conversation.replace("\n       ", "")
            )
            events = await runtime.sessions.read_session(session_id)
            assert all("批准已完成" not in str(event.data) for event in events)
            app.exit(0)
    finally:
        host.command_release.set()
        await runtime.dispose()
        del store


async def test_diverged_facts_show_no_approval_gate_and_reconcile_is_explicit(
    tmp_path: Path,
) -> None:
    request, opened = _start_fixture("task-diverged")
    observation = replace(
        opened,
        summary=replace(opened.summary, status=ProductTaskStatus.STARTED),
        workflow=SimpleNamespace(status=WorkflowStatus.AWAITING_APPROVAL),
    )
    host = _ProductHost(request, observation, restore_task=True)
    provider = _Provider()
    runtime, store = _runtime(tmp_path, provider)
    session_id = await runtime.create_session(tmp_path, metadata={"cli": "chat"})
    actual = await open_chat_session(runtime, workspace=None, session_id=session_id)
    app = TracehTuiApp(
        runtime,
        actual,
        timeline=False,
        heartbeat_seconds=0.0,
        product=host,  # type: ignore[arg-type]
        clock=default_clock(),
    )
    try:
        async with app.run_test(size=(110, 34)) as pilot:
            panel = str(app.query_one("#product-state", Static).content)
            assert "未对账：Product started ┊ Workflow awaiting_approval" in panel
            gate_message = str(app.query_one("#gate-message", Static).content)
            assert "本轮 TUI 不提供写入型对账快捷键" in gate_message
            assert "^i" not in gate_message
            assert not app.query_one("#gate-primary", Button).display
            assert not app.query_one("#gate-secondary", Button).display
            for _ in range(3):
                await pilot.pause()
            assert host.commands == []
            await pilot.press("ctrl+i")
            await pilot.pause()
            assert host.commands == []
            await pilot.press("ctrl+c")
    finally:
        await runtime.dispose()
        del store


async def test_leave_renders_closing_owners_before_the_app_exits(
    tmp_path: Path,
) -> None:
    request, observation = _start_fixture("task-closing")
    host = _ProductHost(
        request,
        observation,
        restore_task=True,
        gate_close=True,
    )
    provider = _Provider()
    runtime, store = _runtime(tmp_path, provider)
    session_id = await runtime.create_session(tmp_path, metadata={"cli": "chat"})
    actual = await open_chat_session(runtime, workspace=None, session_id=session_id)
    app = TracehTuiApp(
        runtime,
        actual,
        timeline=False,
        heartbeat_seconds=0.0,
        product=host,  # type: ignore[arg-type]
        clock=default_clock(),
    )
    try:
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("ctrl+c")
            await asyncio.wait_for(host.close_started.wait(), timeout=2)
            closing = str(app.query_one("#product-state", Static).content)
            assert "正在安全收敛" in closing
            assert "Chat driver" in closing
            assert "Observer" in closing
            assert "Product host" in closing
            assert "等待中" in closing
            host.close_release.set()
    finally:
        host.close_release.set()
        await runtime.dispose()
        del store
