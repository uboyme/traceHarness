"""Textual presentation adapter over the existing Chat and Product owners."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.events import Resize
from textual.widgets import Button, Footer, Input, RichLog, Static

from traceh.chat.activity import (
    DEFAULT_HEARTBEAT_SECONDS,
    ActivityPhase,
    ActivityUpdate,
    Clock,
)
from traceh.chat.driver import (
    ChatDriver,
    ChatUpdate,
    SessionEventUpdate,
    TurnCompletedUpdate,
    TurnFailedUpdate,
    TurnInterruptedUpdate,
)
from traceh.chat.session import OpenedChatSession
from traceh.cli.console import contains_undecodable_input, normalize_input
from traceh.cli.timeline import TimelineRenderer, sanitize
from traceh.concurrency import await_worker_convergence
from traceh.product.chat import (
    ProductCommand,
    ProductCommandOperation,
    ProductStartRequest,
    parse_product_command,
)
from traceh.product.control import PendingProductProposal
from traceh.product.errors import ProductInputError
from traceh.product.host import ProductChatHost
from traceh.product.observation import ProductObservation, ProductObservationSession
from traceh.runtime.agent_runtime import AgentRuntime
from traceh.session.product_context import PRODUCT_CONTEXT_SNAPSHOT
from traceh.session.surface import SurfaceProjector
from traceh.session.surface_replacement import (
    SURFACE_COMPACTION_FAILED,
    SURFACE_REPLACE,
)
from traceh.tui.context_inspection import (
    ContextInspectionReader,
    ContextSnapshot,
)
from traceh.tui.presentation import (
    MODEL_SELF_REPORT_COLOR,
    OperationErrorView,
    ProductGateAction,
    TransientProductState,
    compaction_notice_text,
    context_status_line,
    format_age,
    operation_error_view,
    prefixed_display_lines,
    product_compact_text,
    product_panel_text,
    product_state_text,
    resolve_gate,
    safe_display_block,
)
from traceh.tui.screens import (
    ContextScreen,
    ProductChangesScreen,
    ProductIdentityScreen,
    TaskConversationScreen,
)
from traceh.tui.task_conversation import TaskConversationReader

_GATE_LABELS = {
    ProductGateAction.START: "START",
    ProductGateAction.CANCEL: "取消任务",
    ProductGateAction.APPROVE: "批准",
    ProductGateAction.REJECT: "驳回",
}
_GATE_TOKENS = {
    ProductGateAction.START: "START",
    ProductGateAction.CANCEL: "CANCEL",
    ProductGateAction.APPROVE: "APPROVE",
    ProductGateAction.REJECT: "REJECT",
}
_PRODUCT_OPERATION_LABELS = {
    ProductCommandOperation.APPROVE: "批准",
    ProductCommandOperation.REJECT: "驳回",
    ProductCommandOperation.CANCEL: "取消",
    ProductCommandOperation.ABANDON: "放弃",
}
_NARROW_TERMINAL_COLUMNS = 110
#: Session events that change model-visible history or the latest frozen
#: request. Only these trigger a Context refresh; everything else would just be
#: a redundant read.
_CONTEXT_EVENT_TYPES = frozenset(
    {
        "user/message",
        "assistant/message",
        "tool/result",
        PRODUCT_CONTEXT_SNAPSHOT,
        "request/snapshot",
        SURFACE_REPLACE,
        SURFACE_COMPACTION_FAILED,
    }
)
_HOST_TEAL = "#008080"
_USER_PREFIX = "你 › "
_USER_CONTINUATION = " " * Text(_USER_PREFIX).cell_len
_HOST_PREFIX = "宿主 › "
_HOST_CONTINUATION = " " * Text(_HOST_PREFIX).cell_len
_MODEL_MARKER = "  ▏ "
_MODEL_LABEL = "模型 · "
_MODEL_PREFIX = _MODEL_MARKER + _MODEL_LABEL
_MODEL_CONTINUATION = _MODEL_MARKER + " " * Text(_MODEL_LABEL).cell_len


def _available_log_width(log: RichLog) -> int:
    return max(1, log.content_region.width - log.styles.scrollbar_size_vertical)


class TracehTuiApp(App[int]):
    """The one TUI view; every action still crosses the existing host owner."""

    TITLE = "TraceHarness Chat"
    SUB_TITLE = "durable Session + controlled ProductTask"
    BINDINGS = [
        Binding("ctrl+c", "leave", "退出", priority=True),
        Binding("ctrl+q", "leave", "退出", priority=True),
        Binding("ctrl+p", "identity", "完整身份", priority=True),
        Binding("ctrl+d", "changes", "改动", priority=True),
        Binding("ctrl+t", "task_conversation", "任务对话", priority=True),
        Binding("ctrl+x", "context", "上下文", priority=True),
        Binding("escape", "cancel_confirmation", "取消确认", show=False),
    ]
    CSS = """
    Screen { layout: vertical; }
    #topbar { height: 3; padding: 1 2; background: $panel; }
    #context-bar { height: 1; padding: 0 2; color: $text-muted; }
    #main { height: 1fr; }
    #conversation-column { width: 1fr; min-width: 38; }
    #product-column { width: 1.05fr; min-width: 38; border-left: solid $primary; }
    #conversation-spacer { height: 1fr; min-height: 0; }
    #conversation { height: auto; max-height: 99%; padding: 0 1; }
    #activity {
        dock: bottom;
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    #product-state {
        height: 1fr;
        padding: 1 2;
        overflow-y: auto;
        content-align: left top;
    }
    #gate-area { height: auto; min-height: 0; padding: 0 2 1 2; }
    #gate-message { height: auto; color: $text-muted; }
    #gate-buttons { height: auto; }
    .gate-action {
        display: none;
        width: auto;
        min-width: 8;
        height: 3;
        padding: 0 2;
        margin-right: 1;
        border: round $foreground-muted;
        color: $foreground-muted;
        background: transparent;
        content-align: center middle;
    }
    .gate-action:hover, .gate-action:focus {
        background: transparent;
        text-style: bold;
    }
    #gate-primary { border: round $primary; color: $primary; }
    #confirmation-panel { display: none; height: auto; padding-top: 1; }
    #confirmation-label { height: auto; }
    #confirmation-input { width: 1fr; }
    #chat-input { dock: bottom; }
    Screen.narrow #main { layout: vertical; }
    Screen.narrow #conversation-column { width: 1fr; min-width: 0; height: 1fr; }
    Screen.narrow #product-column {
        width: 1fr;
        min-width: 0;
        height: 8;
        border-left: none;
        border-top: solid $primary;
    }
    Screen.narrow #product-state { padding: 0 1; }
    """

    def __init__(
        self,
        runtime: AgentRuntime,
        opened: OpenedChatSession,
        *,
        timeline: bool,
        heartbeat_seconds: float,
        product: ProductChatHost | None,
        clock: Clock,
    ) -> None:
        super().__init__()
        self.theme = "textual-light"
        self._runtime = runtime
        self._opened = opened
        self._session = opened.session
        self._heartbeat_seconds = heartbeat_seconds if timeline else 0.0
        self._product_refresh_seconds = (
            heartbeat_seconds
            if heartbeat_seconds > 0
            else DEFAULT_HEARTBEAT_SECONDS
        )
        self._product = product
        self._task_conversation = TaskConversationReader(runtime.sessions.store)
        self._context_reader = ContextInspectionReader(
            runtime.sessions,
            policy=runtime.compaction.policy,
        )
        # A discardable display snapshot for the status row only. The detail
        # screen always re-reads the Session; nothing here is a fact source.
        #
        # Deliberately not named ``_context``: ``textual.app.App`` already owns
        # that name as a method, and shadowing it stops the application from
        # ever starting.
        self._context_snapshot: ContextSnapshot | None = None
        self._context_error: str | None = None
        self._context_refreshing = False
        self._context_refresh_pending = False
        self._clock = clock
        self._chat_driver = ChatDriver(
            runtime,
            self._session.session_id,
            sink=self._receive_chat_update,
            timeline=timeline,
            heartbeat_seconds=self._heartbeat_seconds,
            clock=clock,
        )
        self._timeline_renderer = TimelineRenderer()
        self._proposal: PendingProductProposal | None = None
        self._start_request: ProductStartRequest | None = None
        self._task_id: str | None = None
        self._observation: ProductObservation | None = None
        self._observation_received_at: float | None = None
        self._observer: ProductObservationSession | None = None
        self._observer_task: asyncio.Task[None] | None = None
        self._observation_refresh_lock = asyncio.Lock()
        self._pulse_task: asyncio.Task[None] | None = None
        self._operation_task: asyncio.Task[None] | None = None
        self._operation_name = ""
        self._operation_started_at: float | None = None
        self._operation_error: OperationErrorView | None = None
        self._observation_error: OperationErrorView | None = None
        self._next_product_discovery_at = 0.0
        self._gate_actions: tuple[ProductGateAction, ...] = ()
        self._confirmation_action: ProductGateAction | None = None
        # ``MessagePump`` owns an internal ``_closing`` flag.  The TUI needs a
        # separate presentation state so rendering "closing" never prevents
        # Textual from delivering the eventual ExitApp message.
        self._ui_closing = False
        self._ui_closing_started_at: float | None = None
        self._shutdown_task: asyncio.Task[None] | None = None
        self._shutdown_complete = False
        self._shutdown_states = {
            "operation": "未开始",
            "driver": "未开始",
            "observer": "未开始",
            "product": "未开始",
            "runtime": "未开始",
        }

    def compose(self) -> ComposeResult:
        yield Static(self._topbar_text(), id="topbar", markup=False)
        yield Static("", id="context-bar", markup=False)
        with Horizontal(id="main"):
            with Vertical(id="conversation-column"):
                yield Static("", id="conversation-spacer")
                yield RichLog(
                    id="conversation",
                    markup=False,
                    highlight=False,
                    wrap=True,
                    auto_scroll=True,
                    max_lines=2_000,
                )
                yield Static("准备就绪", id="activity", markup=False)
            with Vertical(id="product-column"):
                yield Static("", id="product-state", markup=False)
                with Vertical(id="gate-area"):
                    yield Static("", id="gate-message", markup=False)
                    with Horizontal(id="gate-buttons"):
                        yield Button("", id="gate-primary", classes="gate-action")
                        yield Button("", id="gate-secondary", classes="gate-action")
                    with Vertical(id="confirmation-panel"):
                        yield Static("", id="confirmation-label", markup=False)
                        yield Input(id="confirmation-input")
        yield Input(placeholder="给 TraceHarness 发消息…", id="chat-input")
        yield Footer()

    async def on_mount(self) -> None:
        self._set_narrow(self.size.width < _NARROW_TERMINAL_COLUMNS)
        initial_system = [
            f"Session {self._session.session_id}\nWorkspace {self._session.workspace}"
        ]
        if self._opened.recovery is not None and self._opened.recovery.changed:
            report = self._opened.recovery
            initial_system.append(
                "Recovered durable work: "
                f"model_attempts={report.closed_model_attempts}; "
                f"tool_results={report.synthesized_tool_results}; "
                f"step={report.closed_step}; turn={report.closed_turn}"
            )
        await self._refresh_context()
        restored = await self._restore_conversation()
        if self._product is not None:
            await self._restore_product()
            self._pulse_task = asyncio.create_task(
                self._pulse(), name="traceh-tui-presentation-pulse"
            )
        self._refresh_product_view()
        self.call_after_refresh(
            self._render_initial_conversation,
            tuple(initial_system),
            restored,
        )

    async def on_resize(self, event: Resize) -> None:
        self._set_narrow(event.size.width < _NARROW_TERMINAL_COLUMNS)
        self.call_after_refresh(self._rewrap_conversation)
        # The status row picks its rendering from the width it actually has, so
        # it has to be recomposed once the new layout is known.
        self.call_after_refresh(self._render_context_bar)

    def _set_narrow(self, enabled: bool) -> None:
        screen = self.screen
        if screen.has_class("narrow") != enabled:
            screen.set_class(enabled, "narrow")
        self._render_context_bar()

    async def _restore_conversation(self) -> tuple[tuple[str, object], ...]:
        events = await self._runtime.sessions.read_session(self._session.session_id)
        return tuple(
            (message.role, message.content)
            for message in SurfaceProjector().project(events)
            if message.role in {"user", "assistant"} and message.content
        )

    def _render_initial_conversation(
        self,
        system_messages: tuple[str, ...],
        restored: tuple[tuple[str, object], ...],
    ) -> None:
        for message in system_messages:
            self._write_system(message)
        for role, content in restored:
            self._write_conversation(role, content)
        self.query_one("#chat-input", Input).focus()

    async def _restore_product(self) -> None:
        await self._retry_product_observation()

    async def _retry_product_observation(self) -> None:
        product = self._product
        if product is None or self._observer is not None or self._ui_closing:
            return
        try:
            task_id = self._task_id
            if task_id is None:
                task_id = await product.observation.current_task_id(
                    self._session.session_id
                )
            if task_id is None:
                self._observation_error = None
                self._refresh_product_view()
                return
            await self._observe_task(task_id)
        except Exception as error:
            self._show_observation_error(
                error,
                fallback="product-observation-unavailable",
            )
        finally:
            self._next_product_discovery_at = (
                self._clock.monotonic() + self._product_refresh_seconds
            )

    @on(Input.Submitted, "#chat-input")
    async def _input_submitted(self, event: Input.Submitted) -> None:
        text = normalize_input(event.value)
        event.input.value = ""
        if not text:
            return
        if contains_undecodable_input(text):
            self._write_system("输入没有发送：它不是有效的 Unicode 文本。")
            return
        if text in {"/exit", "/quit"}:
            await self.action_leave()
            return
        if self._busy or self._ui_closing or self._confirmation_action is not None:
            self._write_system("请等待当前操作收敛或先完成权限确认。")
            return
        if text.startswith("/task") and self._product is not None:
            try:
                command = parse_product_command(text)
            except ProductInputError:
                self._write_system(
                    "Usage: /task inspect|approve|reject|cancel|abandon TASK_ID"
                )
                return
            if command is not None:
                self._task_id = command.task_id
                self._launch(
                    self._execute_product(command),
                    name=command.operation.value,
                )
                return
        self._write_conversation("user", text)
        self._launch(self._run_turn(text), name="chat")

    @on(Input.Submitted, "#confirmation-input")
    async def _confirmation_submitted(self, event: Input.Submitted) -> None:
        action = self._confirmation_action
        if action is None:
            return
        expected = _GATE_TOKENS[action]
        entered = normalize_input(event.value)
        event.input.value = ""
        if entered != expected:
            self.query_one("#confirmation-label", Static).update(
                f"未执行。请完整输入 {expected}，或按 Esc 取消。"
            )
            event.input.focus()
            return
        self._end_confirmation()
        self._invoke_gate(action)

    @on(Button.Pressed)
    async def _button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if self._ui_closing:
            return
        index = (
            0
            if button_id == "gate-primary"
            else 1
            if button_id == "gate-secondary"
            else -1
        )
        if index < 0 or index >= len(self._gate_actions):
            return
        action = self._gate_actions[index]
        if self._busy and not (
            action is ProductGateAction.CANCEL and self._operation_name == "start"
        ):
            return
        self._begin_confirmation(action)

    @property
    def _busy(self) -> bool:
        return self._operation_task is not None and not self._operation_task.done()

    def _launch(self, operation: Coroutine[Any, Any, None], *, name: str) -> None:
        if self._busy or self._ui_closing:
            operation.close()
            return
        self._operation_error = None
        self._operation_name = name
        self._operation_started_at = self._clock.monotonic()
        self._operation_task = asyncio.create_task(
            self._own_operation(operation), name="traceh-tui-operation"
        )
        self._refresh_product_view()

    def _replace_start_with_cancel(self, task_id: str) -> None:
        start = self._operation_task
        if start is None or start.done() or self._operation_name != "start":
            self._launch(
                self._execute_product(
                    ProductCommand(ProductCommandOperation.CANCEL, task_id)
                ),
                name="cancel",
            )
            return

        async def cancel_after_start_converges() -> None:
            start.cancel()
            await await_worker_convergence(start)
            await self._execute_product(
                ProductCommand(ProductCommandOperation.CANCEL, task_id)
            )

        self._operation_error = None
        self._operation_name = "cancel"
        self._operation_started_at = self._clock.monotonic()
        self._operation_task = asyncio.create_task(
            self._own_operation(cancel_after_start_converges()),
            name="traceh-tui-operation",
        )
        self._refresh_product_view()

    async def _own_operation(self, operation: Coroutine[Any, Any, None]) -> None:
        try:
            await operation
        except asyncio.CancelledError:
            raise
        except Exception as error:
            code = _safe_error_code(error)
            self._operation_error = operation_error_view(code)
            self._write_system(f"宿主操作未完成（{code}）。右侧保留了可核对的状态。")
        finally:
            if self._operation_task is asyncio.current_task():
                self._operation_task = None
                self._operation_name = ""
                self._operation_started_at = None
                if self.is_mounted and not self._ui_closing:
                    self._refresh_product_view()
                    self.query_one("#chat-input", Input).focus()

    async def _run_turn(self, text: str) -> None:
        prepared = None
        if self._product is not None:
            prepared = await self._product.prepare_turn(self._session.session_id, text)
            # ``prepare_turn`` may append ``product/context-snapshot`` before the
            # ChatDriver subscribes, so that event never reaches the feed. This
            # is the host call boundary that owns it.
            await self._refresh_context()
        outcome = await self._chat_driver.run_turn(
            prepared.turn_input if prepared is not None else text
        )
        if outcome.result is None or prepared is None or self._product is None:
            if prepared is not None and self._product is not None:
                await self._product.discard_turn(self._session.session_id, None)
            return
        resolution = await self._product.resolve_turn(
            self._session.session_id,
            prepared,
            turn_id=outcome.result.turn_id,
        )
        if resolution.proposal is not None:
            await self._focus_pending_task(resolution.proposal)
            self._start_request = None
            self._render_proposal(resolution.proposal)
        elif resolution.notice_code is not None:
            self._write_system("任务确认已忽略：当前没有等待确认的提案。")
        elif resolution.start_request is not None:
            await self._focus_pending_task(resolution.start_request.pending)
            self._start_request = resolution.start_request
            self._proposal = resolution.start_request.pending
            self._task_id = resolution.start_request.pending.task_id
            self._render_start_request(resolution.start_request)

    async def _refresh_context(self) -> None:
        """Re-read the Session once and re-render the status row.

        Concurrent refreshes are coalesced rather than raced: while one read is
        in flight a second request only sets a flag, and the follow-up read runs
        afterwards, so a slower older read can never overwrite a newer head.
        There is no polling task and no second feed; every call is driven by a
        host boundary that already knows the Session changed.
        """

        if self._context_refreshing:
            self._context_refresh_pending = True
            return
        self._context_refreshing = True
        try:
            while True:
                self._context_refresh_pending = False
                try:
                    snapshot = await self._context_reader.load(
                        self._session.session_id
                    )
                except Exception as error:
                    # Context is a read-only view. Its failure must never reach
                    # the Turn, the ProductTask or shutdown.
                    self._context_snapshot = None
                    self._context_error = _safe_error_code(
                        error, "context-inspection-unavailable"
                    )
                else:
                    self._context_snapshot = snapshot
                    self._context_error = None
                if not self._context_refresh_pending:
                    break
        finally:
            self._context_refreshing = False
        self._render_context_bar()

    def _render_context_bar(self) -> None:
        if not self.is_mounted:
            return
        try:
            bar = self.query_one("#context-bar", Static)
        except NoMatches:
            return
        # The row is measured against the cells it really has, not the terminal
        # column count: ``#context-bar`` has horizontal padding. Before the first
        # layout ``content_region`` is still empty, so fall back to the screen
        # width minus that padding rather than rendering unmeasured.
        available = bar.content_region.width or max(1, self.size.width - 4)
        bar.update(
            context_status_line(
                self._context_snapshot,
                error_code=self._context_error,
                width=available,
            )
        )

    async def _focus_pending_task(self, pending: PendingProductProposal) -> None:
        """Move the single Product pane from old durable history to ``pending``."""

        observed = self._observation
        selected_task_id = (
            observed.task_id if observed is not None else self._task_id
        )
        if selected_task_id is not None and selected_task_id != pending.task_id:
            await self._close_observer()
            self._observation = None
            self._observation_received_at = None
            self._observation_error = None
        self._task_id = pending.task_id

    async def _receive_chat_update(self, update: ChatUpdate) -> None:
        if not self.is_mounted:
            return
        if isinstance(update, SessionEventUpdate):
            completed = update.completed_activity
            elapsed = None if completed is None else completed.elapsed_seconds
            line = self._timeline_renderer.render(
                update.event, elapsed_seconds=elapsed
            )
            if line is not None:
                self._set_activity(line)
            # Compaction changes what the model may still see, so it also lands
            # in the conversation column instead of scrolling past in the
            # transient activity line. Same durable event, no second state.
            notice = compaction_notice_text(update.event)
            if notice is not None:
                self._write_system(notice)
            if update.event.type in _CONTEXT_EVENT_TYPES:
                await self._refresh_context()
            return
        if isinstance(update, ActivityUpdate):
            phase = "仍在工作" if update.phase is ActivityPhase.WAITING else "已完成"
            self._set_activity(
                f"{sanitize(update.label)}：{phase}（{update.elapsed_seconds:.1f}s）"
            )
            return
        if isinstance(update, TurnCompletedUpdate):
            self._write_conversation("assistant", update.result.final_text)
            self._set_activity(
                f"Turn {sanitize(update.result.reason)}；steps={update.result.steps}"
            )
            return
        if isinstance(update, TurnFailedUpdate):
            self._write_system(f"Turn 失败（{sanitize(update.error_type)}）。")
            self._set_activity("Turn 失败")
            return
        if isinstance(update, TurnInterruptedUpdate):
            self._write_system("Turn 已在 durable 收敛后中断。")
            self._set_activity("Turn 已中断")

    def _render_proposal(self, pending: PendingProductProposal) -> None:
        self._proposal = pending
        self._task_id = pending.task_id
        self._write_system(
            "ProductTask 提议（进程内，尚未启动）\n"
            f"{pending.requirement}\n"
            f"模式：{pending.proposal.requested_mode.value}\n"
            "请在后续消息中自然确认；确认后宿主才会提供独立 START。"
        )
        self._refresh_product_view()

    def _render_start_request(self, request: ProductStartRequest) -> None:
        self._write_system(
            "精确 START 请求已冻结（进程内）\n"
            "尚无 durable ProductTask；右侧只显示当前合法的 START 闸门。"
        )
        self._refresh_product_view()

    async def _start_product(self, request: ProductStartRequest) -> None:
        assert self._product is not None
        self._task_id = request.pending.task_id
        await self._observe_task(self._task_id)
        result = await self._product.start(request)
        self._start_request = None
        await self._refresh_observation()
        self._write_system(
            f"ProductTask 已到达 durable 状态：{result.summary.status.value}。"
        )

    async def _execute_product(self, command: ProductCommand) -> None:
        assert self._product is not None
        await self._ensure_observer(command.task_id)
        result = await self._product.execute_command(command)
        if result.inspection is not None:
            inspection = result.inspection
            if inspection.evidence_error is not None:
                self._write_system(
                    "Durable evidence 不可用；批准入口保持关闭"
                    f"（{sanitize(inspection.evidence_error)}）。"
                )
        await self._refresh_observation()
        if result.advance is not None:
            operation = _PRODUCT_OPERATION_LABELS[command.operation]
            self._write_system(
                f"{operation}已完成；ProductTask 已到达 durable 状态："
                f"{result.advance.summary.status.value}。"
            )

    async def _ensure_observer(self, task_id: str) -> None:
        if (
            self._observer is not None
            and self._observation is not None
            and self._observation.task_id == task_id
        ):
            return
        await self._observe_task(task_id)

    async def _observe_task(self, task_id: str) -> None:
        assert self._product is not None
        await self._close_observer()
        self._task_id = task_id
        observer = self._product.observe(task_id)
        observation = await observer.start()
        self._observer = observer
        self._apply_observation(observation)
        self._observer_task = asyncio.create_task(
            self._watch_observation(observer),
            name=f"traceh-tui-product-observer-{task_id}",
        )

    async def _watch_observation(self, observer: ProductObservationSession) -> None:
        while True:
            dirty = asyncio.create_task(observer.wait_dirty())
            periodic = asyncio.create_task(
                self._clock.sleep(self._product_refresh_seconds)
            )
            try:
                done, _pending = await asyncio.wait(
                    (dirty, periodic), return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                for task in (dirty, periodic):
                    if not task.done():
                        task.cancel()
                        await await_worker_convergence(task)
            for task in done:
                await task
            if observer is not self._observer:
                return
            try:
                async with self._observation_refresh_lock:
                    observation = await observer.refresh()
                    if observer is self._observer:
                        self._apply_observation(observation)
            except Exception as error:
                self._show_observation_error(
                    error, fallback="product-observation-unavailable"
                )

    async def _pulse(self) -> None:
        while True:
            await self._clock.sleep(1.0)
            if not self.is_mounted:
                return
            now = self._clock.monotonic()
            if (
                self._observer is None
                and not self._busy
                and now >= self._next_product_discovery_at
            ):
                await self._retry_product_observation()
            self._refresh_product_view()

    async def _refresh_observation(self) -> None:
        observer = self._observer
        if observer is None:
            return
        async with self._observation_refresh_lock:
            observation = await observer.refresh()
            if observer is self._observer:
                self._apply_observation(observation)

    def _apply_observation(self, observation: ProductObservation) -> None:
        self._observation = observation
        self._observation_received_at = self._clock.monotonic()
        self._task_id = observation.task_id
        self._observation_error = None
        if observation.summary is not None:
            self._start_request = None
        self._refresh_product_view()

    def _show_observation_error(self, error: Exception, *, fallback: str) -> None:
        code = _safe_error_code(error, fallback)
        self._observation_error = operation_error_view(code)
        self._refresh_product_view()

    def _transient_state(self) -> TransientProductState:
        now = self._clock.monotonic()
        if self._ui_closing:
            waiting = (
                0
                if self._ui_closing_started_at is None
                else int(now - self._ui_closing_started_at)
            )
            return TransientProductState("closing", "leave", max(0, waiting))
        if self._busy:
            waiting = (
                0
                if self._operation_started_at is None
                else int(now - self._operation_started_at)
            )
            return TransientProductState(
                "operation_pending", self._operation_name, max(0, waiting)
            )
        durable_task_exists = (
            self._observation is not None and self._observation.summary is not None
        )
        if self._start_request is not None and not durable_task_exists:
            return TransientProductState("start_request")
        if self._proposal is not None and not durable_task_exists:
            return TransientProductState("proposal")
        return TransientProductState("none")

    def _refresh_product_view(self) -> None:
        if not self.is_mounted:
            return
        try:
            self.query_one("#product-state", Static)
        except NoMatches:
            # A forced terminal/test teardown may remove widgets while the
            # owned shutdown task is still closing durable resources.  Display
            # is best-effort at that point; lifecycle convergence is not.
            return
        transient = self._transient_state()
        summary = None if self._observation is None else self._observation.summary
        terminal = summary is not None and summary.settled
        if self._ui_closing:
            self._set_product_text(self._closing_text(transient.waiting_seconds))
        elif self.screen.has_class("narrow"):
            self._set_product_text(
                product_compact_text(
                    product_enabled=self._product is not None,
                    proposal=self._proposal,
                    start_request=self._start_request,
                    observation=self._observation,
                    transient=transient,
                    now_monotonic=self._clock.monotonic(),
                    observation_received_at=self._observation_received_at,
                    operation_error=self._operation_error,
                    observation_error=self._observation_error,
                ),
                terminal=terminal,
            )
        else:
            self._set_product_text(
                product_panel_text(
                    product_enabled=self._product is not None,
                    proposal=self._proposal,
                    start_request=self._start_request,
                    observation=self._observation,
                    transient=transient,
                    now_monotonic=self._clock.monotonic(),
                    observation_received_at=self._observation_received_at,
                    operation_error=self._operation_error,
                    observation_error=self._observation_error,
                ),
                terminal=terminal,
            )
        self._refresh_gate(transient)

    def _refresh_gate(self, transient: TransientProductState) -> None:
        decision = resolve_gate(transient, self._observation)
        self._gate_actions = decision.actions
        self.query_one("#gate-message", Static).update(decision.message)
        buttons = (
            self.query_one("#gate-primary", Button),
            self.query_one("#gate-secondary", Button),
        )
        for index, button in enumerate(buttons):
            visible = index < len(decision.actions) and self._confirmation_action is None
            button.display = visible
            if visible:
                button.label = _GATE_LABELS[decision.actions[index]]
        self.query_one("#chat-input", Input).disabled = (
            self._busy or self._ui_closing or self._confirmation_action is not None
        )

    def _begin_confirmation(self, action: ProductGateAction) -> None:
        token = _GATE_TOKENS[action]
        self._confirmation_action = action
        panel = self.query_one("#confirmation-panel", Vertical)
        panel.display = True
        self.query_one("#confirmation-label", Static).update(
            f"输入 {token} 确认 · Esc 取消"
        )
        field = self.query_one("#confirmation-input", Input)
        field.value = ""
        field.placeholder = token
        self._refresh_product_view()
        # A Button click may finish its own focus handling after the Pressed
        # handler returns.  Defer the confirmation focus until that message
        # queue and the newly-visible panel have both been refreshed, so the
        # user's immediate typing cannot be sent back to the hidden button.
        if not self.call_after_refresh(field.focus):
            field.focus()

    def _end_confirmation(self) -> None:
        self._confirmation_action = None
        self.query_one("#confirmation-panel", Vertical).display = False

    def _invoke_gate(self, action: ProductGateAction) -> None:
        if action is ProductGateAction.START:
            if self._start_request is not None:
                self._launch(self._start_product(self._start_request), name="start")
            return
        if self._task_id is None:
            return
        if action is ProductGateAction.CANCEL and self._busy:
            self._replace_start_with_cancel(self._task_id)
            return
        operation = {
            ProductGateAction.CANCEL: ProductCommandOperation.CANCEL,
            ProductGateAction.APPROVE: ProductCommandOperation.APPROVE,
            ProductGateAction.REJECT: ProductCommandOperation.REJECT,
        }.get(action)
        if operation is not None:
            self._launch(
                self._execute_product(ProductCommand(operation, self._task_id)),
                name=action.value,
            )

    def _write_conversation(self, role: str, content: object) -> None:
        log = self.query_one("#conversation", RichLog)
        body = safe_display_block(content)
        self._write_role_lines(log, role, body, labeled=True)

    def _write_system(self, content: object) -> None:
        body = safe_display_block(content)
        log = self.query_one("#conversation", RichLog)
        self._write_role_lines(log, "host", body, labeled=True)

    def _write_role_lines(
        self,
        log: RichLog,
        role: str,
        body: str,
        *,
        labeled: bool,
    ) -> None:
        if role == "user":
            first_prefix = _USER_PREFIX if labeled else _USER_CONTINUATION
            continuation_prefix = _USER_CONTINUATION
        elif role == "host":
            first_prefix = _HOST_PREFIX if labeled else _HOST_CONTINUATION
            continuation_prefix = _HOST_CONTINUATION
        else:
            first_prefix = _MODEL_PREFIX if labeled else _MODEL_CONTINUATION
            continuation_prefix = _MODEL_CONTINUATION

        for prefix, line in prefixed_display_lines(
            body,
            width=_available_log_width(log),
            first_prefix=first_prefix,
            continuation_prefix=continuation_prefix,
        ):
            if role == "host":
                rendered = Text(prefix + line, style=_HOST_TEAL)
            elif role == "user":
                rendered = Text(prefix + line)
            else:
                rendered = Text()
                rendered.append(_MODEL_MARKER, style="dim")
                rendered.append(
                    prefix[len(_MODEL_MARKER) :] + line,
                    style=f"italic {MODEL_SELF_REPORT_COLOR}",
                )
            log.write(rendered, width=max(1, rendered.cell_len))

    def _rewrap_conversation(self) -> None:
        try:
            log = self.query_one("#conversation", RichLog)
        except NoMatches:
            return
        width = _available_log_width(log)
        if not log.lines or all(line.cell_length <= width for line in log.lines):
            return

        lines = tuple(line.text for line in log.lines)
        log.clear()
        current_role: str | None = None
        for text in lines:
            labeled = True
            if text.startswith(_USER_PREFIX):
                current_role = "user"
                body = text[len(_USER_PREFIX) :]
            elif text.startswith(_HOST_PREFIX):
                current_role = "host"
                body = text[len(_HOST_PREFIX) :]
            elif text.startswith(_MODEL_PREFIX):
                current_role = "assistant"
                body = text[len(_MODEL_PREFIX) :]
            elif current_role == "user" and text.startswith(_USER_CONTINUATION):
                labeled = False
                body = text[len(_USER_CONTINUATION) :]
            elif current_role == "host" and text.startswith(_HOST_CONTINUATION):
                labeled = False
                body = text[len(_HOST_CONTINUATION) :]
            elif current_role == "assistant" and text.startswith(_MODEL_CONTINUATION):
                labeled = False
                body = text[len(_MODEL_CONTINUATION) :]
            else:
                current_role = "host"
                body = text
            self._write_role_lines(
                log,
                current_role,
                body,
                labeled=labeled,
            )

    def _set_activity(self, content: object) -> None:
        self.query_one("#activity", Static).update(
            safe_display_block(content, limit=500)
        )

    def _set_product_text(self, content: object, *, terminal: bool = False) -> None:
        self.query_one("#product-state", Static).update(
            product_state_text(content, terminal=terminal)
        )

    def _topbar_text(self) -> str:
        workspace = self._session.workspace.name or str(self._session.workspace)
        session = self._session.session_id[:8]
        config = self._runtime.config
        return safe_display_block(
            f"traceharness · {workspace} · session {session}    "
            f"{config.provider}/{config.model}",
            limit=500,
            max_lines=1,
        )

    async def action_identity(self) -> None:
        if self._ui_closing:
            return
        observation_reader = (
            None if self._product is None else self._product.observation
        )
        await self.push_screen(
            ProductIdentityScreen(
                chat_session_id=self._session.session_id,
                proposal=self._proposal,
                start_request=self._start_request,
                observation_reader=observation_reader,
                task_id=self._task_id,
            )
        )

    async def action_changes(self) -> None:
        if self._ui_closing:
            return
        observation_reader = (
            None if self._product is None else self._product.observation
        )
        await self.push_screen(
            ProductChangesScreen(
                observation_reader=observation_reader,
                task_id=self._task_id,
            )
        )

    async def action_context(self) -> None:
        if self._ui_closing:
            return
        await self.push_screen(
            ContextScreen(self._context_reader, self._session.session_id)
        )

    async def action_cancel_confirmation(self) -> None:
        if self._confirmation_action is None:
            return
        self._end_confirmation()
        self._refresh_product_view()
        self.query_one("#chat-input", Input).focus()

    async def action_task_conversation(self) -> None:
        if self._ui_closing:
            return
        observation_reader = (
            None if self._product is None else self._product.observation
        )
        await self.push_screen(
            TaskConversationScreen(
                self._task_conversation,
                observation_reader,
                self._task_id,
            )
        )

    async def action_leave(self) -> None:
        if self._ui_closing:
            return
        self._ui_closing = True
        self._ui_closing_started_at = self._clock.monotonic()
        self._end_confirmation()
        self._shutdown_task = asyncio.create_task(
            self._converge_and_exit(), name="traceh-tui-visible-shutdown"
        )
        self._refresh_product_view()

    async def _converge_and_exit(self) -> None:
        failed = False
        operation = self._operation_task
        self._shutdown_states["operation"] = "等待返回" if operation is not None else "无活动操作"
        self._refresh_product_view()
        if operation is not None and not operation.done():
            operation.cancel()
            await await_worker_convergence(operation)
        self._shutdown_states["operation"] = "已收敛"
        failed |= await self._close_stage("driver", self._chat_driver.aclose)
        failed |= await self._close_stage("observer", self._close_observer)
        if self._product is not None:
            failed |= await self._close_stage("product", self._product.aclose)
        else:
            self._shutdown_states["product"] = "未装配"
        failed |= await self._close_stage("runtime", self._runtime.dispose)
        self._shutdown_complete = True
        self._refresh_product_view()
        self.call_later(self.exit, 1 if failed else 130)

    async def _close_stage(self, name: str, close) -> bool:
        self._shutdown_states[name] = "等待中"
        self._refresh_product_view()
        try:
            await close()
        except BaseException as error:
            self._shutdown_states[name] = f"失败 · {_safe_error_code(error)}"
            self._refresh_product_view()
            return True
        self._shutdown_states[name] = "已关闭"
        self._refresh_product_view()
        return False

    def _closing_text(self, waiting_seconds: int) -> str:
        lines = ["正在安全收敛…", ""]
        labels = {
            "operation": "操作",
            "driver": "Chat driver",
            "observer": "Observer",
            "product": "Product host",
            "runtime": "Runtime",
        }
        for key in ("operation", "driver", "observer", "product", "runtime"):
            lines.append(f"  {labels[key]:<14} {self._shutdown_states[key]}")
        lines.extend(("", f"已等待 {format_age(waiting_seconds)}"))
        if waiting_seconds >= 20:
            lines.append(
                "收敛时间较长；界面仍在等待原 owner 返回，不提供绕过 owner 的强制退出。"
            )
        return "\n".join(lines)

    async def on_unmount(self) -> None:
        pulse = self._pulse_task
        self._pulse_task = None
        if pulse is not None and not pulse.done():
            pulse.cancel()
            await await_worker_convergence(pulse)
        if self._shutdown_complete:
            return
        operation = self._operation_task
        if operation is not None and not operation.done():
            operation.cancel()
            await await_worker_convergence(operation)
        await self._chat_driver.aclose()
        await self._close_observer()

    async def _close_observer(self) -> None:
        observer_task = self._observer_task
        self._observer_task = None
        observer = self._observer
        self._observer = None
        if observer_task is not None:
            observer_task.cancel()
            await await_worker_convergence(observer_task)
        if observer is not None:
            await observer.aclose()


def _safe_error_code(error: BaseException, fallback: str = "operation-failed") -> str:
    code = getattr(error, "code", None)
    if type(code) is not str or not code:
        code = fallback
    return sanitize(code)


__all__ = ["TracehTuiApp"]
