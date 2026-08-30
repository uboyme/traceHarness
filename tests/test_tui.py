"""Headless Textual F4 checks; skipped by a core-only installation."""

from __future__ import annotations

import asyncio
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("textual")

from product_fixtures import ORIGIN_SESSION, preflight, proposal
from textual.widgets import Button, Input, Static

import traceh.tui.app as tui_app
from traceh.api.events import PendingEvent
from traceh.api.llm import ModelRequest, ModelResponse
from traceh.api.product import ProductTaskStatus, ProductTaskSummary
from traceh.api.turns import TurnInput
from traceh.api.workflow import WorkflowStatus
from traceh.chat.activity import DEFAULT_HEARTBEAT_SECONDS, Clock, default_clock
from traceh.chat.session import open_chat_session
from traceh.cli.console import Console
from traceh.cli.product import LineProductAdapter
from traceh.product.chat import (
    ProductChatTurn,
    ProductCommandOperation,
    ProductCommandResult,
    ProductStartRequest,
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
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.session.event_feed import SessionEventFeed
from traceh.session.event_store import InMemoryEventStore
from traceh.session.invariants import CoreInvariantChecker
from traceh.session.sqlite import SqliteEventStore
from traceh.tui.app import TracehTuiApp
from traceh.tui.runner import run_tui


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


async def test_ctrl_c_converges_an_active_provider_call(tmp_path: Path) -> None:
    provider = _Provider(gate=True)
    app, runtime, store, opened = await _opened_app(tmp_path, provider)
    try:
        async with app.run_test(size=(90, 28)) as pilot:
            app.query_one("#chat-input", Input).value = "block until cancelled"
            await pilot.press("enter")
            await asyncio.wait_for(provider.started.wait(), timeout=2)
            await pilot.press("ctrl+c")
            await pilot.pause()

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
    def __init__(self, observation: ProductObservation) -> None:
        self.observation = observation
        self.closed = False
        self._dirty = asyncio.Event()

    async def start(self) -> ProductObservation:
        return self.observation

    async def refresh(self) -> ProductObservation:
        return self.observation

    async def wait_dirty(self) -> None:
        await self._dirty.wait()

    async def aclose(self) -> None:
        self.closed = True
        self._dirty.set()


class _CurrentTaskReader:
    def __init__(self, task_id: str | None) -> None:
        self.task_id = task_id

    async def current_task_id(self, session_id: str) -> str | None:
        assert session_id
        return self.task_id


class _ProductHost:
    def __init__(
        self,
        request: ProductStartRequest,
        observation: ProductObservation,
        *,
        restore_task: bool = False,
        gate_command: bool = False,
    ) -> None:
        self.request = request
        self.observation = _CurrentTaskReader(
            observation.task_id if restore_task else None
        )
        self._observed = observation
        self.started_requests: list[ProductStartRequest] = []
        self.commands = []
        self.command_started = asyncio.Event()
        self.command_release = asyncio.Event()
        self.gate_command = gate_command

    def observe(self, task_id: str) -> _Observer:
        assert task_id == self._observed.task_id
        return _Observer(self._observed)

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
        self.command_release.set()


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

    async def advance_one_waiter(self) -> float:
        seconds, gate = await asyncio.wait_for(self.waiters.get(), timeout=2)
        gate.set_result(None)
        return seconds


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
            return replace(
                observation,
                summary=summary,
                stream_heads=(ObservedStreamHead(state_stream, head),),
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
        async with app.run_test(size=(100, 30)) as pilot:
            assert "Product: opened" in str(
                app.query_one("#product-state", Static).content
            )
            await durable_store.append(
                state_stream,
                expected_seq=0,
                events=(PendingEvent(type="probe/state-changed", data={}),),
            )
            assert not observer.dirty

            interval = await refresh_clock.advance_one_waiter()
            for _ in range(3):
                await pilot.pause()

            assert interval == DEFAULT_HEARTBEAT_SECONDS
            assert "Product: failed" in str(
                app.query_one("#product-state", Static).content
            )
            app.exit(0)
    finally:
        await runtime.dispose()
        await durable_store.aclose()
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


async def test_model_confirmation_cannot_bypass_the_tui_start_gesture(
    tmp_path: Path,
) -> None:
    request, observation = _start_fixture()
    host = _ProductHost(request, observation)
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
            app.query_one("#chat-input", Input).value = "I confirm the proposal"
            await pilot.press("enter")
            await asyncio.wait_for(provider.started.wait(), timeout=2)
            for _ in range(4):
                await pilot.pause()
            assert host.started_requests == []
            assert app.query_one("#start-task", Button).disabled is False
            assert app.query_one("#approve-task", Button).disabled is True
            panel = str(app.query_one("#product-state", Static).content)
            assert "[bold]" in panel
            assert "\x1b" not in panel
            assert "\\x1b" in panel
            await pilot.click("#start-task")
            for _ in range(3):
                await pilot.pause()
            assert host.started_requests == [host.request]
            app.exit(0)
    finally:
        await runtime.dispose()
        del store


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
    host.observation = _CurrentTaskReader(observation.task_id)
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
            assert app.query_one("#approve-task", Button).disabled is False
            panel = str(app.query_one("#product-state", Static).render())
            assert "review-tui" in panel
            assert "refs/heads/main" in panel
            assert ("d" * 64) in panel
            await pilot.double_click("#approve-task")
            await asyncio.wait_for(host.command_started.wait(), timeout=2)
            assert len(host.commands) == 1
            assert host.commands[0].operation is ProductCommandOperation.APPROVE
            assert host.commands[0].task_id == observation.task_id
            assert [field.name for field in fields(host.commands[0])] == [
                "operation",
                "task_id",
            ]
            host.command_release.set()
            await pilot.pause()
            app.exit(0)
    finally:
        host.command_release.set()
        await runtime.dispose()
        del store
