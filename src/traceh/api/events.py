"""Durable event primitives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from traceh.api.json_types import JsonValue, to_json_value


def attempt_identity(data: dict[str, JsonValue]) -> str | None:
    """Read a usable `attempt_id` from a model attempt payload.

    Only a non-empty, non-blank string identifies an attempt. ``None``, numbers,
    booleans, empty strings and whitespace are missing identities, not values to
    be coerced: ``str(None)`` would invent an attempt called ``"None"`` and make
    unrelated broken events look like the same attempt.
    """

    value = data.get("attempt_id")
    if isinstance(value, str) and value.strip():
        return value
    return None


@dataclass(frozen=True, slots=True)
class PendingEvent:
    type: str
    data: dict[str, JsonValue]
    schema_version: int = 1
    event_id: UUID | None = None
    causation_id: UUID | None = None
    correlation_id: UUID | None = None
    actor_id: str | None = None
    composition_revision: str | None = None
    occurred_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    event_id: UUID
    stream_id: str
    seq: int
    type: str
    schema_version: int
    data: dict[str, JsonValue]
    occurred_at: datetime
    causation_id: UUID | None = None
    correlation_id: UUID | None = None
    actor_id: str | None = None
    composition_revision: str | None = None

    @classmethod
    def materialize(cls, stream_id: str, seq: int, pending: PendingEvent) -> "EventEnvelope":
        return cls(
            event_id=pending.event_id or uuid4(),
            stream_id=stream_id,
            seq=seq,
            type=pending.type,
            schema_version=pending.schema_version,
            data={str(k): to_json_value(v) for k, v in pending.data.items()},
            occurred_at=pending.occurred_at or datetime.now(UTC),
            causation_id=pending.causation_id,
            correlation_id=pending.correlation_id,
            actor_id=pending.actor_id,
            composition_revision=pending.composition_revision,
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "event_id": str(self.event_id),
            "stream_id": self.stream_id,
            "seq": self.seq,
            "type": self.type,
            "schema_version": self.schema_version,
            "data": self.data,
            "occurred_at": self.occurred_at.isoformat(),
            "causation_id": str(self.causation_id) if self.causation_id else None,
            "correlation_id": str(self.correlation_id) if self.correlation_id else None,
            "actor_id": self.actor_id,
            "composition_revision": self.composition_revision,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, JsonValue]) -> "EventEnvelope":
        data = raw.get("data")
        if not isinstance(data, dict):
            raise ValueError("event.data must be an object")
        return cls(
            event_id=UUID(str(raw["event_id"])),
            stream_id=str(raw["stream_id"]),
            seq=int(raw["seq"]),
            type=str(raw["type"]),
            schema_version=int(raw.get("schema_version", 1)),
            data={str(k): v for k, v in data.items()},
            occurred_at=datetime.fromisoformat(str(raw["occurred_at"])),
            causation_id=UUID(str(raw["causation_id"])) if raw.get("causation_id") else None,
            correlation_id=(
                UUID(str(raw["correlation_id"])) if raw.get("correlation_id") else None
            ),
            actor_id=str(raw["actor_id"]) if raw.get("actor_id") is not None else None,
            composition_revision=(
                str(raw["composition_revision"])
                if raw.get("composition_revision") is not None
                else None
            ),
        )
