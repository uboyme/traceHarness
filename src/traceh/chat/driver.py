"""UI-neutral driver for one already-open Chat Session.

An adapter submits a :class:`TurnInput` and receives typed updates through an
async callback.  The driver never reads stdin and never turns an update into
terminal text.  Conversation state remains the Session event log projected by
``AgentRuntime.run_existing()``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from traceh.api.events import EventEnvelope
from traceh.api.turns import TurnInput
from traceh.chat.activity import (
    ActivityTracker,
    ActivityUpdate,
    Clock,
)
from traceh.concurrency import await_worker_convergence
from traceh.runtime.agent_loop import TurnResult
from traceh.runtime.agent_runtime import AgentRuntime
from traceh.session.event_feed import EventSubscription


@dataclass(frozen=True, slots=True)
class SessionEventUpdate:
    event: EventEnvelope
    completed_activity: ActivityUpdate | None = None


@dataclass(frozen=True, slots=True)
class TurnCompletedUpdate:
    result: TurnResult


@dataclass(frozen=True, slots=True)
class TurnFailedUpdate:
    """A failed Turn; ``message`` remains untrusted adapter input."""

    error_type: str
    message: str


@dataclass(frozen=True, slots=True)
class TurnInterruptedUpdate:
    pass


type ChatUpdate = (
    SessionEventUpdate
    | ActivityUpdate
    | TurnCompletedUpdate
    | TurnFailedUpdate
    | TurnInterruptedUpdate
)

type ChatUpdateSink = Callable[[ChatUpdate], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ChatTurnOutcome:
    result: TurnResult | None
    failed: bool = False
    interrupted: bool = False
    leave_after_convergence: bool = False


@dataclass(frozen=True, slots=True)
class _Narration:
    subscription: EventSubscription | None = None
    events: asyncio.Task[None] | None = None
    heartbeat: asyncio.Task[None] | None = None


class ChatDriver:
    """Drive Turns and their transient observation, with no UI authority."""

    __slots__ = (
        "_active",
        "_clock",
        "_heartbeat_seconds",
        "_runtime",
        "_session_id",
        "_sink",
        "_timeline",
    )

    def __init__(
        self,
        runtime: AgentRuntime,
        session_id: str,
        *,
        sink: ChatUpdateSink,
        timeline: bool,
        heartbeat_seconds: float,
        clock: Clock,
    ) -> None:
        self._runtime = runtime
        self._session_id = session_id
        self._sink = sink
        self._timeline = timeline
        self._heartbeat_seconds = heartbeat_seconds if timeline else 0.0
        self._clock = clock
        self._active: asyncio.Future[TurnResult] | None = None

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def active(self) -> bool:
        return self._active is not None and not self._active.done()

    async def run_turn(self, turn_input: str | TurnInput) -> ChatTurnOutcome:
        if self.active:
            raise RuntimeError("chat-driver-turn-active")
        narration = self._start_narration()
        turn = asyncio.ensure_future(
            self._runtime.run_existing(self._session_id, turn_input)
        )
        self._active = turn
        try:
            result = await asyncio.shield(turn)
        except asyncio.CancelledError:
            return await self._interrupt(narration, turn)
        except Exception as error:
            await self._stop_narration(narration)
            await self._emit(
                TurnFailedUpdate(type(error).__name__, str(error))
            )
            return ChatTurnOutcome(result=None, failed=True)
        except BaseException:
            await self._stop_narration(narration)
            raise
        finally:
            if turn.done() and self._active is turn:
                self._active = None
        await self._stop_narration(narration)
        await self._emit(TurnCompletedUpdate(result))
        return ChatTurnOutcome(result=result)

    async def cancel(self, *, reason: str) -> None:
        """Use the existing Runtime cancellation owner and wait for convergence."""

        cancellation = asyncio.create_task(
            self._runtime.cancel(self._session_id, reason=reason),
            name=f"traceh-chat-driver-cancel-{self._session_id}",
        )
        await await_worker_convergence(cancellation)

    async def aclose(self) -> None:
        if self.active:
            await self.cancel(reason="chat adapter closed during an active turn")
        active = self._active
        if active is not None:
            await asyncio.gather(active, return_exceptions=True)
            self._active = None

    def _start_narration(self) -> _Narration:
        if not self._timeline:
            return _Narration()
        subscription = self._runtime.events.subscribe(
            self._runtime.sessions.session_stream(self._session_id)
        )
        tracker = ActivityTracker(
            interval_seconds=self._heartbeat_seconds,
            monotonic=self._clock.monotonic,
        )
        events = asyncio.create_task(
            self._observe_events(subscription, tracker),
            name="traceh-chat-driver-events",
        )
        heartbeat = (
            asyncio.create_task(
                self._emit_heartbeat(tracker),
                name="traceh-chat-driver-heartbeat",
            )
            if tracker.enabled
            else None
        )
        return _Narration(subscription, events, heartbeat)

    async def _observe_events(
        self, subscription: EventSubscription, tracker: ActivityTracker
    ) -> None:
        async for event in subscription:
            completed = tracker.observe(event)
            await self._emit(SessionEventUpdate(event, completed))

    async def _emit_heartbeat(self, tracker: ActivityTracker) -> None:
        while True:
            delay = tracker.seconds_until_next_wait()
            if delay is None:
                await self._clock.sleep(tracker.interval_seconds)
            elif delay > 0:
                await self._clock.sleep(delay)
            for update in tracker.due_waits():
                await self._emit(update)

    async def _interrupt(
        self,
        narration: _Narration,
        turn: asyncio.Future[TurnResult],
    ) -> ChatTurnOutcome:
        caller = asyncio.current_task()
        before = caller.cancelling() if caller is not None else 0
        await self.cancel(reason="interrupted from the chat adapter")
        after = caller.cancelling() if caller is not None else 0
        if caller is not None:
            while caller.uncancel() > 0:
                pass
        outcome = (await asyncio.gather(turn, return_exceptions=True))[0]
        await self._stop_narration(narration)
        if not isinstance(outcome, BaseException):
            await self._emit(TurnCompletedUpdate(outcome))
            return ChatTurnOutcome(
                result=outcome,
                leave_after_convergence=after > before,
            )
        await self._emit(TurnInterruptedUpdate())
        return ChatTurnOutcome(
            result=None,
            interrupted=True,
            leave_after_convergence=after > before,
        )

    async def _stop_narration(self, narration: _Narration) -> None:
        if narration.heartbeat is not None:
            narration.heartbeat.cancel()
            await await_worker_convergence(narration.heartbeat)
        if narration.subscription is not None:
            narration.subscription.close()
        if narration.events is None:
            return
        try:
            await asyncio.shield(narration.events)
        except asyncio.CancelledError as cancellation:
            await await_worker_convergence(narration.events)
            raise cancellation
        except Exception:
            # Observation and adapter output cannot change the Turn outcome.
            pass

    async def _emit(self, update: ChatUpdate) -> None:
        # A broken UI must not become an Agent failure.  It will see no further
        # updates, while the operation and its durable lifecycle still converge.
        try:
            await self._sink(update)
        except Exception:
            pass


__all__ = [
    "ActivityUpdate",
    "ChatDriver",
    "ChatTurnOutcome",
    "ChatUpdate",
    "ChatUpdateSink",
    "SessionEventUpdate",
    "TurnCompletedUpdate",
    "TurnFailedUpdate",
    "TurnInterruptedUpdate",
]
