"""Durable event primitives."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

from traceh.api.json_types import JsonValue, to_json_value


def _detach_json_data(data: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Copy an event payload so it shares no mutable container with ``data``.

    Implementation detail of this module; the reusable boundary helper is
    ``detach_event()``.

    Copying goes through ``to_json_value()`` so that one rule decides both what
    an event payload may contain and what it is normalized into, instead of
    adding a second, looser copying rule beside it. That rule is wider than
    `JsonValue`: framework types the encoder supports - ``Path``, ``UUID``,
    ``datetime``, ``Enum``, dataclasses, arbitrary ``Mapping`` and ``Sequence``
    values other than ``str``, ``bytes`` and ``bytearray``, such as ``tuple`` -
    are converted into their JSON form rather than rejected, so a ``tuple``
    becomes a ``list`` and a ``Path`` becomes a string. Only genuinely
    unsupported values (``set``, ``bytes``, arbitrary objects) raise
    ``TypeError``. Immutable scalars are passed through unchanged.
    """

    return {str(key): to_json_value(value) for key, value in data.items()}


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
    """A persisted event.

    ``frozen=True`` only prevents rebinding the fields themselves; ``data`` is
    an ordinary JSON dict whose nested dicts and lists stay mutable. Ownership
    of that graph is therefore a contract carried by specific boundaries, not a
    language guarantee.

    What is guaranteed: an envelope returned by a TraceHarness distribution
    boundary - today an `EventStore`'s ``append()`` and ``read()`` - is
    caller-owned, and editing it cannot rewrite stored history.

    What is not: nothing isolates two consumers automatically. An envelope is a
    plain object, so passing one to two consumers shares one mutable payload
    between them. Any component that fans an event out to several recipients
    owes each of them its own ``detach_event()`` copy. (No such fan-out exists
    in this version.)
    """

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
            # Detaches the payload from the caller's PendingEvent: later edits
            # to the input graph cannot rewrite an event that was already made.
            data=_detach_json_data(pending.data),
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
            # Detached: the returned dict is the caller's to edit or serialize,
            # and editing it must not reach back into this envelope.
            "data": _detach_json_data(self.data),
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
            # Detached: a shallow rebuild would still share every nested dict
            # and list with ``raw``, so editing the input would edit the event.
            data=_detach_json_data(data),
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


def detach_event(event: EventEnvelope) -> EventEnvelope:
    """Return an equal envelope whose payload is detached from ``event.data``.

    Every field except ``data`` is carried over by value; only the JSON graph is
    rebuilt, and no JSON text round trip is involved, so ``UUID`` and
    ``datetime`` metadata keep their types. Payload values are normalized by the
    same rule as any other event payload - see ``_detach_json_data()``.

    This is the reusable helper for any boundary that hands an event to someone
    who may edit it: it lets a store return an event without also returning a
    writable reference into its own history, and one copy per recipient is what
    a future fan-out would need.
    """

    return replace(event, data=_detach_json_data(event.data))
