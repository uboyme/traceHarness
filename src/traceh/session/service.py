"""High-level Session and Effect stream service."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

from traceh.api.events import EventEnvelope, PendingEvent
from traceh.api.json_types import JsonValue
from traceh.session.event_store import Durability, EventStore


class SessionNotFoundError(LookupError):
    pass


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
        durability: Durability = Durability.SYNC,
        actor_id: str | None = None,
        correlation_id: UUID | None = None,
        causation_id: UUID | None = None,
        composition_revision: str | None = None,
    ) -> EventEnvelope:
        async with self._lock(stream_id):
            expected_seq = await self.store.head(stream_id)
            appended = await self.store.append(
                stream_id,
                expected_seq=expected_seq,
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
                "workspace": str(workspace.resolve()),
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
            durability=durability,
            actor_id=actor_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            composition_revision=composition_revision,
        )

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
