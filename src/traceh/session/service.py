"""High-level Session and Effect stream service."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

from traceh.api.events import EventEnvelope, PendingEvent
from traceh.api.json_types import JsonValue, fingerprint
from traceh.api.llm import (
    ModelAttemptIdentity,
    ModelRequest,
    dispatch_request_matches_composed,
    model_attempt_reservation_id,
)
from traceh.session.event_store import ConcurrencyConflict, Durability, EventStore


class SessionNotFoundError(LookupError):
    pass


class ModelAttemptConflictError(RuntimeError):
    """The caller did not acquire this Step ordinal's dispatch permit."""

    code = "model-attempt-dispatch-conflict"

    def __init__(self, *, ownership_lost: bool = False) -> None:
        super().__init__(self.code)
        self.ownership_lost = ownership_lost


class SessionService:
    def __init__(self, store: EventStore) -> None:
        self.store = store
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def session_stream(session_id: str) -> str:
        return f"session:{session_id}"

    @staticmethod
    def effect_stream(session_id: str) -> str:
        return f"effects:{session_id}"

    def _lock(self, stream_id: str) -> asyncio.Lock:
        return self._locks.setdefault(stream_id, asyncio.Lock())

    async def _append(
        self,
        stream_id: str,
        event_type: str,
        data: dict[str, JsonValue],
        *,
        expected_seq: int | None = None,
        durability: Durability = Durability.SYNC,
        actor_id: str | None = None,
        correlation_id: UUID | None = None,
        causation_id: UUID | None = None,
        composition_revision: str | None = None,
    ) -> EventEnvelope:
        async with self._lock(stream_id):
            actual_expected_seq = (
                await self.store.head(stream_id)
                if expected_seq is None
                else expected_seq
            )
            appended = await self.store.append(
                stream_id,
                expected_seq=actual_expected_seq,
                events=(
                    PendingEvent(
                        type=event_type,
                        data=data,
                        actor_id=actor_id,
                        correlation_id=correlation_id,
                        causation_id=causation_id,
                        composition_revision=composition_revision,
                    ),
                ),
                durability=durability,
            )
            return appended[0]

    async def create_session(
        self,
        workspace: Path,
        *,
        metadata: dict[str, JsonValue] | None = None,
        session_id: str | None = None,
    ) -> str:
        session_id = session_id or str(uuid4())
        stream_id = self.session_stream(session_id)
        existing = await self.store.read(stream_id)
        if existing:
            raise ValueError(f"session already exists: {session_id}")
        await self._append(
            stream_id,
            "session/created",
            {
                "session_id": session_id,
                "workspace": str(workspace.resolve()),  # noqa: ASYNC240
                "metadata": metadata or {},
            },
        )
        return session_id

    async def ensure_session(self, session_id: str) -> None:
        events = await self.read_session(session_id)
        if not events or events[0].type != "session/created":
            raise SessionNotFoundError(session_id)

    async def append_session(
        self,
        session_id: str,
        event_type: str,
        data: dict[str, JsonValue],
        *,
        expected_seq: int | None = None,
        durability: Durability = Durability.SYNC,
        actor_id: str | None = None,
        correlation_id: UUID | None = None,
        causation_id: UUID | None = None,
        composition_revision: str | None = None,
    ) -> EventEnvelope:
        return await self._append(
            self.session_stream(session_id),
            event_type,
            data,
            expected_seq=expected_seq,
            durability=durability,
            actor_id=actor_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            composition_revision=composition_revision,
        )

    async def start_model_attempt(
        self,
        session_id: str,
        *,
        attempt: ModelAttemptIdentity,
        source_seq: int,
        composition_revision: str,
        composed_request: ModelRequest,
        composed_fingerprint: str,
        dispatch_request: ModelRequest,
        dispatch_fingerprint: str,
        reservation_id: str | None,
        retry_wait_milliseconds: int = 0,
        retry_failure_code: str | None = None,
        retry_failure_category: str | None = None,
        correlation_id: UUID | None = None,
    ) -> tuple[EventEnvelope, EventEnvelope]:
        """Atomically freeze the Step request and claim one dispatch ordinal.

        The first ordinal appends ``request/snapshot`` and
        ``model/attempt-start`` in one Store CAS. Later ordinals reuse that one
        snapshot. A concurrent or recovered owner can therefore observe the
        existing permit, but can never append another ordinal by treating fact
        idempotency as execution authority.
        """

        if attempt.session_id != session_id:
            raise ModelAttemptConflictError
        if type(source_seq) is not int or source_seq < 1:
            raise ModelAttemptConflictError
        if not isinstance(composition_revision, str) or not composition_revision:
            raise ModelAttemptConflictError
        expected_metadata = {
            "session_id": session_id,
            "turn_id": attempt.turn_id,
            "step_id": attempt.step_id,
            "composition_revision": composition_revision,
        }
        for request in (composed_request, dispatch_request):
            if any(
                request.metadata.get(key) != value
                for key, value in expected_metadata.items()
            ):
                raise ModelAttemptConflictError
        if fingerprint(composed_request.to_dict()) != composed_fingerprint:
            raise ModelAttemptConflictError
        if fingerprint(dispatch_request.to_dict()) != dispatch_fingerprint:
            raise ModelAttemptConflictError
        if not dispatch_request_matches_composed(composed_request, dispatch_request):
            raise ModelAttemptConflictError
        expected_reservation_id = model_attempt_reservation_id(attempt)
        if reservation_id is not None and reservation_id != expected_reservation_id:
            raise ModelAttemptConflictError
        if type(retry_wait_milliseconds) is not int or retry_wait_milliseconds < 0:
            raise ModelAttemptConflictError
        if attempt.ordinal == 1:
            if (
                retry_wait_milliseconds != 0
                or retry_failure_code is not None
                or retry_failure_category is not None
            ):
                raise ModelAttemptConflictError
        elif (
            not isinstance(retry_failure_code, str)
            or not retry_failure_code
            or not isinstance(retry_failure_category, str)
            or not retry_failure_category
        ):
            raise ModelAttemptConflictError

        stream_id = self.session_stream(session_id)
        async with self._lock(stream_id):
            events = await self.store.read(stream_id)
            if not events or events[0].type != "session/created":
                raise SessionNotFoundError(session_id)
            # Import at the ownership boundary rather than module import time:
            # the invariant checker also validates plugin identity and that
            # dependency graph reaches ToolRuntime, which itself uses this
            # service.
            from traceh.session.invariants import CoreInvariantChecker

            if CoreInvariantChecker().check(events):
                # This service owns the only durable dispatch permit.  A later
                # Attempt cannot be authorized from history that the shared
                # Session projector already proves is not legally replayable.
                raise ModelAttemptConflictError(ownership_lost=True)
            head = events[-1].seq
            if source_seq > head:
                raise ModelAttemptConflictError

            open_turn: str | None = None
            open_step: str | None = None
            starts: list[EventEnvelope] = []
            all_attempt_ids: set[str] = set()
            ended: set[str] = set()
            attempt_ends: dict[str, EventEnvelope] = {}
            snapshots: list[EventEnvelope] = []
            for event in events:
                if event.type == "turn/start":
                    open_turn = str(event.data.get("turn_id", ""))
                elif event.type == "turn/end":
                    open_turn = None
                elif event.type == "step/start":
                    open_step = str(event.data.get("step_id", ""))
                elif event.type == "step/end":
                    open_step = None
                elif event.type == "request/snapshot" and (
                    event.data.get("turn_id") == attempt.turn_id
                    and event.data.get("step_id") == attempt.step_id
                ):
                    snapshots.append(event)
                elif event.type == "model/attempt-start" and (
                    event.data.get("turn_id") == attempt.turn_id
                    and event.data.get("step_id") == attempt.step_id
                ):
                    starts.append(event)
                    all_attempt_ids.add(str(event.data.get("attempt_id", "")))
                elif event.type == "model/attempt-start":
                    all_attempt_ids.add(str(event.data.get("attempt_id", "")))
                elif event.type == "model/attempt-end":
                    ended_id = str(event.data.get("attempt_id", ""))
                    ended.add(ended_id)
                    attempt_ends[ended_id] = event

            if open_turn != attempt.turn_id or open_step != attempt.step_id:
                raise ModelAttemptConflictError(ownership_lost=True)
            if any(
                str(event.data.get("attempt_id", "")) not in ended
                for event in starts
            ):
                raise ModelAttemptConflictError(ownership_lost=True)
            if attempt.attempt_id in all_attempt_ids:
                raise ModelAttemptConflictError(ownership_lost=True)
            if any(
                event.data.get("ordinal") == attempt.ordinal
                for event in starts
            ):
                raise ModelAttemptConflictError(ownership_lost=True)
            ordinals = [event.data.get("ordinal") for event in starts]
            if ordinals != list(range(1, len(starts) + 1)):
                raise ModelAttemptConflictError(ownership_lost=True)
            if attempt.ordinal != len(starts) + 1:
                raise ModelAttemptConflictError(ownership_lost=True)
            if attempt.ordinal > 1:
                previous = starts[-1]
                previous_end = attempt_ends.get(
                    str(previous.data.get("attempt_id", ""))
                )
                if (
                    previous_end is None
                    or previous_end.data.get("status") != "failed"
                    or previous_end.data.get("failure_code") != retry_failure_code
                    or previous_end.data.get("failure_category")
                    != retry_failure_category
                ):
                    raise ModelAttemptConflictError(ownership_lost=True)

            pending: list[PendingEvent] = []
            if attempt.ordinal == 1:
                if snapshots:
                    raise ModelAttemptConflictError(ownership_lost=True)
                request_snapshot_seq = head + 1
                snapshot_data: dict[str, JsonValue] = {
                    "turn_id": attempt.turn_id,
                    "step_id": attempt.step_id,
                    "source_seq": source_seq,
                    "composition_revision": composition_revision,
                    "composed_fingerprint": composed_fingerprint,
                    "dispatch_fingerprint": dispatch_fingerprint,
                    "composed_request": composed_request.to_dict(),
                    "dispatch_request": dispatch_request.to_dict(),
                }
                pending.append(
                    PendingEvent(
                        type="request/snapshot",
                        data=snapshot_data,
                        correlation_id=correlation_id,
                        composition_revision=composition_revision,
                    )
                )
            else:
                if len(snapshots) != 1:
                    raise ModelAttemptConflictError
                snapshot = snapshots[0]
                if (
                    snapshot.data.get("composed_fingerprint")
                    != composed_fingerprint
                    or snapshot.data.get("dispatch_fingerprint")
                    != dispatch_fingerprint
                ):
                    raise ModelAttemptConflictError
                request_snapshot_seq = snapshot.seq

            start_data: dict[str, JsonValue] = {
                "turn_id": attempt.turn_id,
                "step_id": attempt.step_id,
                "attempt_id": attempt.attempt_id,
                "ordinal": attempt.ordinal,
                "request_snapshot_seq": request_snapshot_seq,
                "dispatch_fingerprint": dispatch_fingerprint,
                "reservation_id": reservation_id,
                "provider": dispatch_request.provider,
                "model": dispatch_request.model,
                "retry_wait_milliseconds": retry_wait_milliseconds,
                "retry_failure_code": retry_failure_code,
                "retry_failure_category": retry_failure_category,
            }
            pending.append(
                PendingEvent(
                    type="model/attempt-start",
                    data=start_data,
                    correlation_id=correlation_id,
                    composition_revision=composition_revision,
                )
            )
            try:
                appended = await self.store.append(
                    stream_id,
                    expected_seq=head,
                    events=tuple(pending),
                    durability=Durability.SYNC,
                )
            except ConcurrencyConflict:
                raise ModelAttemptConflictError(ownership_lost=True) from None
            if attempt.ordinal == 1:
                return appended[0], appended[1]
            return snapshots[0], appended[0]

    async def append_effect(
        self,
        session_id: str,
        event_type: str,
        data: dict[str, JsonValue],
        *,
        durability: Durability = Durability.SYNC,
        causation_id: UUID | None = None,
        correlation_id: UUID | None = None,
    ) -> EventEnvelope:
        return await self._append(
            self.effect_stream(session_id),
            event_type,
            data,
            durability=durability,
            causation_id=causation_id,
            correlation_id=correlation_id,
        )

    async def read_session(self, session_id: str) -> tuple[EventEnvelope, ...]:
        return await self.store.read(self.session_stream(session_id))

    async def read_effects(self, session_id: str) -> tuple[EventEnvelope, ...]:
        return await self.store.read(self.effect_stream(session_id))

    async def list_sessions(self) -> tuple[str, ...]:
        streams = await self.store.list_streams(prefix="session:")
        return tuple(stream.removeprefix("session:") for stream in streams)

    async def workspace_for(self, session_id: str) -> Path:
        await self.ensure_session(session_id)
        events = await self.read_session(session_id)
        workspace = events[0].data.get("workspace")
        if not isinstance(workspace, str):
            raise ValueError(f"session {session_id} has no valid workspace")
        return Path(workspace)
