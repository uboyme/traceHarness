"""Textual presentation adapter over the existing Chat and Product owners."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Footer, Header, Input, RichLog, Static

from traceh.api.product import ProductTaskStatus
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
from traceh.session.surface import SurfaceProjector
from traceh.tui.presentation import product_observation_text, safe_display_block


class TracehTuiApp(App[int]):
    """One optional view; all actions still cross the existing host owners."""

    TITLE = "TraceHarness Chat"
    SUB_TITLE = "durable Session + controlled ProductTask"
    BINDINGS = [
        ("ctrl+c", "leave", "Leave"),
        ("ctrl+q", "leave", "Leave"),
    ]
    CSS = """
    Screen { layout: vertical; }
    #main { height: 1fr; }
    #conversation-column { width: 3fr; min-width: 38; }
    #product-column { width: 2fr; min-width: 34; border-left: solid $primary; }
    #conversation { height: 1fr; padding: 0 1; }
    #activity { height: 3; padding: 0 1; color: $text-muted; }
    #product-title { height: 3; padding: 1; text-style: bold; }
    #product-state { height: 1fr; padding: 0 1; overflow-y: auto; }
    #product-actions { height: auto; padding: 1; }
    #chat-input { dock: bottom; }
    Button { margin-right: 1; margin-bottom: 1; }
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
        self._start_request: ProductStartRequest | None = None
        self._task_id: str | None = None
        self._observation: ProductObservation | None = None
        self._observer: ProductObservationSession | None = None
        self._observer_task: asyncio.Task[None] | None = None
        self._operation_task: asyncio.Task[None] | None = None
        self._closing = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main"):
            with Vertical(id="conversation-column"):
                yield RichLog(
                    id="conversation",
                    markup=False,
                    highlight=False,
                    wrap=True,
                    max_lines=2_000,
                )
                yield Static("Ready", id="activity", markup=False)
            with Vertical(id="product-column"):
                yield Static("ProductTask", id="product-title", markup=False)
                yield Static(
                    "Product mode is disabled for this Chat."
                    if self._product is None
                    else "No active ProductTask.",
                    id="product-state",
                    markup=False,
                )
                with Horizontal(id="product-actions"):
                    yield Button("START task", id="start-task", variant="primary")
                    yield Button("Inspect", id="inspect-task")
                    yield Button("Approve", id="approve-task", variant="success")
                    yield Button("Reject", id="reject-task", variant="warning")
                    yield Button("Cancel", id="cancel-task", variant="error")
        yield Input(placeholder="Message TraceHarness…", id="chat-input")
        yield Footer()

    async def on_mount(self) -> None:
        self._write_system(
            f"Session {self._session.session_id}\nWorkspace {self._session.workspace}"
        )
        if self._opened.recovery is not None and self._opened.recovery.changed:
            report = self._opened.recovery
            self._write_system(
                "Recovered durable work: "
                f"model_attempts={report.closed_model_attempts}; "
                f"tool_results={report.synthesized_tool_results}; "
                f"step={report.closed_step}; turn={report.closed_turn}"
            )
        await self._restore_conversation()
        if self._product is not None:
            await self._restore_product()
        self._refresh_controls()
        self.query_one("#chat-input", Input).focus()

    async def _restore_conversation(self) -> None:
        events = await self._runtime.sessions.read_session(self._session.session_id)
        for message in SurfaceProjector().project(events):
            if message.role in {"user", "assistant"} and message.content:
                self._write_conversation(message.role, message.content)

    async def _restore_product(self) -> None:
        assert self._product is not None
        try:
            task_id = await self._product.observation.current_task_id(
                self._session.session_id
            )
            if task_id is not None:
                await self._observe_task(task_id)
        except Exception as error:
            self._show_product_error(error, fallback="product-observation-unavailable")

    @on(Input.Submitted, "#chat-input")
    async def _input_submitted(self, event: Input.Submitted) -> None:
        text = normalize_input(event.value)
        event.input.value = ""
        if not text:
            return
        if contains_undecodable_input(text):
            self._write_system("Input was not sent because it was not valid Unicode text.")
            return
        if text in {"/exit", "/quit"}:
            self.exit(0)
            return
        if self._busy:
            self._write_system("Wait for the current operation to converge.")
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
                self._launch(self._execute_product(command))
                return
        self._write_conversation("user", text)
        self._launch(self._run_turn(text))

    @on(Button.Pressed)
    async def _button_pressed(self, event: Button.Pressed) -> None:
        if self._busy:
            return
        button_id = event.button.id
        if button_id == "start-task":
            if self._start_request is not None:
                self._launch(self._start_product(self._start_request))
            return
        operations = {
            "inspect-task": ProductCommandOperation.INSPECT,
            "approve-task": ProductCommandOperation.APPROVE,
            "reject-task": ProductCommandOperation.REJECT,
            "cancel-task": ProductCommandOperation.CANCEL,
        }
        operation = operations.get(button_id)
        if operation is not None and self._task_id is not None:
            self._launch(
                self._execute_product(ProductCommand(operation, self._task_id))
            )

    @property
    def _busy(self) -> bool:
        return self._operation_task is not None and not self._operation_task.done()

    def _launch(self, operation: Coroutine[Any, Any, None]) -> None:
        if self._busy:
            operation.close()
            return
        self._operation_task = asyncio.create_task(
            self._own_operation(operation), name="traceh-tui-operation"
        )
        self._refresh_controls()

    async def _own_operation(self, operation: Coroutine[Any, Any, None]) -> None:
        try:
            await operation
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._write_system(f"Operation failed ({_safe_error_code(error)}).")
        finally:
            self._operation_task = None
            if self.is_mounted:
                self._refresh_controls()
                self.query_one("#chat-input", Input).focus()

    async def _run_turn(self, text: str) -> None:
        prepared = None
        if self._product is not None:
            prepared = await self._product.prepare_turn(self._session.session_id, text)
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
            self._render_proposal(resolution.proposal)
        elif resolution.notice_code is not None:
            self._write_system("Task confirmation ignored: no proposal is pending.")
        elif resolution.start_request is not None:
            self._start_request = resolution.start_request
            self._task_id = resolution.start_request.pending.task_id
            self._render_start_request(resolution.start_request)

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
            return
        if isinstance(update, ActivityUpdate):
            phase = "working" if update.phase is ActivityPhase.WAITING else "completed"
            self._set_activity(
                f"{sanitize(update.label)}: {phase} ({update.elapsed_seconds:.1f}s)"
            )
            return
        if isinstance(update, TurnCompletedUpdate):
            self._write_conversation("assistant", update.result.final_text)
            self._set_activity(
                f"Turn {sanitize(update.result.reason)}; steps={update.result.steps}"
            )
            return
        if isinstance(update, TurnFailedUpdate):
            self._write_system(f"Turn failed ({sanitize(update.error_type)}).")
            self._set_activity("Turn failed")
            return
        if isinstance(update, TurnInterruptedUpdate):
            self._write_system("Turn interrupted after durable convergence.")
            self._set_activity("Turn interrupted")

    def _render_proposal(self, pending: PendingProductProposal) -> None:
        proposal = pending.proposal
        binding = proposal.preflight
        self._set_product_text(
            "Task proposal (not started)\n"
            f"Task if confirmed: {pending.task_id}\n"
            f"Requirement: {pending.requirement}\n"
            f"Profile: {pending.profile_id}\n"
            f"Mode: {proposal.requested_mode.value}\n"
            f"Mode source: {proposal.mode_source.value}\n"
            f"Source: {binding.base_revision}\n"
            f"Target: {binding.promotion_target_ref} at "
            f"{binding.promotion_expected_revision}\n\n"
            "This proposal has not started work. Confirm it naturally in a later "
            "message; the host will then expose a separate START button."
        )

    def _render_start_request(self, request: ProductStartRequest) -> None:
        pending = request.pending
        self._set_product_text(
            "Explicit host authorization required\n"
            f"Task: {pending.task_id}\n"
            f"Requirement: {pending.requirement}\n"
            f"Mode: {pending.proposal.requested_mode.value}\n\n"
            "No task has started. Press START task to authorize this exact request."
        )
        self._refresh_controls()

    async def _start_product(self, request: ProductStartRequest) -> None:
        assert self._product is not None
        self._start_request = None
        self._task_id = request.pending.task_id
        await self._observe_task(self._task_id)
        result = await self._product.start(request)
        await self._refresh_observation()
        self._write_system(
            f"ProductTask {result.summary.task_id}: {result.summary.status.value}."
        )

    async def _execute_product(self, command: ProductCommand) -> None:
        assert self._product is not None
        await self._ensure_observer(command.task_id)
        result = await self._product.execute_command(command)
        if result.inspection is not None:
            inspection = result.inspection
            if inspection.evidence_error is not None:
                self._write_system(
                    "Durable evidence is unavailable; approval remains disabled "
                    f"({sanitize(inspection.evidence_error)})."
                )
        await self._refresh_observation()

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
                self._apply_observation(await observer.refresh())
            except Exception as error:
                self._show_product_error(
                    error, fallback="product-observation-unavailable"
                )

    async def _refresh_observation(self) -> None:
        if self._observer is None:
            return
        self._apply_observation(await self._observer.refresh())

    def _apply_observation(self, observation: ProductObservation) -> None:
        self._observation = observation
        self._task_id = observation.task_id
        self._set_product_text(product_observation_text(observation))
        self._refresh_controls()

    def _show_product_error(self, error: Exception, *, fallback: str) -> None:
        self._observation = None
        self._set_product_text(f"ProductTask unavailable ({_safe_error_code(error, fallback)}).")
        self._refresh_controls()

    def _refresh_controls(self) -> None:
        if not self.is_mounted:
            return
        busy = self._busy or self._closing
        observation = self._observation
        status = None if observation is None else observation.product_status
        review_ready = bool(
            observation is not None
            and status is ProductTaskStatus.AWAITING_APPROVAL
            and not observation.streams_diverged
            and observation.review is not None
            and observation.evidence is not None
            and observation.approval_digest is not None
            and observation.approval is None
            and observation.promotion is None
        )
        self.query_one("#chat-input", Input).disabled = busy
        self.query_one("#start-task", Button).disabled = (
            busy or self._start_request is None
        )
        self.query_one("#inspect-task", Button).disabled = (
            busy or self._product is None or self._task_id is None
        )
        self.query_one("#approve-task", Button).disabled = busy or not review_ready
        self.query_one("#reject-task", Button).disabled = busy or not review_ready
        self.query_one("#cancel-task", Button).disabled = (
            busy
            or observation is None
            or observation.summary is None
            or observation.summary.settled
        )

    def _write_conversation(self, role: str, content: object) -> None:
        label = "You" if role == "user" else "Assistant"
        self.query_one("#conversation", RichLog).write(
            f"{label}> {safe_display_block(content)}"
        )

    def _write_system(self, content: object) -> None:
        self.query_one("#conversation", RichLog).write(
            f"System> {safe_display_block(content)}"
        )

    def _set_activity(self, content: object) -> None:
        self.query_one("#activity", Static).update(safe_display_block(content, limit=500))

    def _set_product_text(self, content: object) -> None:
        self.query_one("#product-state", Static).update(safe_display_block(content))

    async def action_leave(self) -> None:
        self.exit(130)

    async def on_unmount(self) -> None:
        self._closing = True
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


def _safe_error_code(error: Exception, fallback: str = "operation-failed") -> str:
    code = getattr(error, "code", None)
    if type(code) is not str or not code:
        code = fallback
    return sanitize(code)


__all__ = ["TracehTuiApp"]
