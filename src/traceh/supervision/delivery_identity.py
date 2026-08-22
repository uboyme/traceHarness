"""What a delivery fact is, written once for writer and replay.

Stage B's Inbox stream answers *"which messages did this Agent accept, and in
what order"*. This stream answers the next question: *"which of them has been
claimed for execution, and how did that execution end"*. They are separate
streams on purpose - see ADR-0021.

The claim transaction and the delivery projector must not develop two readings
of the same bytes. If the writer accepts a claim the projector later rejects,
a message is claimed for one process and looks unclaimed to the next, and two
Turns run the same message. Every rule about the stream name, the event types,
the schema version, the exact payload shapes and the parse lives here.

**Nothing here records what happened inside a Turn.** Model output, tool
results and errors belong to the Session Event Log. A terminal fact carries a
stable reason or error *code* and the ``turn_id`` that points at the Session,
never a message body, an exception text or a traceback.
"""

from __future__ import annotations

from traceh.agents.identity import is_agent_identifier
from traceh.api.events import EventEnvelope
from traceh.api.json_types import JsonValue
from traceh.cli.text_safety import is_single_line_safe
from traceh.supervision.errors import DeliveryInputError, DeliveryProtocolError

AGENT_DELIVERY_STREAM_PREFIX = "agent-delivery:"
"""Namespace of the per-Agent delivery lifecycle streams.

One stream per Agent, so each Agent has its own ``expected_seq`` and two
Supervisors racing for *different* Agents never contend. It is deliberately not
the Inbox stream: Stage B's projector accepts exactly one event type and
rejects everything else, and that contract is worth keeping - an acceptance
history that could also contain execution state would no longer be a plain
answer to "what was received".
"""

AGENT_DELIVERY_SCHEMA_VERSION = 1
"""The only payload shape this projector can read."""

AGENT_MESSAGE_CLAIMED = "agent/message-claimed"
AGENT_MESSAGE_COMPLETED = "agent/message-completed"
AGENT_MESSAGE_FAILED = "agent/message-failed"
AGENT_MESSAGE_CANCELLED = "agent/message-cancelled"

TERMINAL_EVENT_TYPES = frozenset(
    {AGENT_MESSAGE_COMPLETED, AGENT_MESSAGE_FAILED, AGENT_MESSAGE_CANCELLED}
)
DELIVERY_EVENT_TYPES = frozenset({AGENT_MESSAGE_CLAIMED}) | TERMINAL_EVENT_TYPES

MAX_REASON_CHARS = 200
"""Upper bound on a persisted reason or error code.

A reason reaches this log from a caller (``interrupt(reason=...)``) or from a
`TurnResult`, and is later printed. Bounded and single-line-safe for the same
reasons an identifier is, so a reason cannot forge a line of output or hide
inside a bidirectional override.
"""

CLAIMED_KEYS = frozenset(
    {"agent_id", "message_id", "accepted_seq", "claim_id", "activation_id", "session_id"}
)
COMPLETED_KEYS = frozenset({"agent_id", "message_id", "claim_id", "turn_id", "reason"})
FAILED_KEYS = frozenset({"agent_id", "message_id", "claim_id", "error_code"})
CANCELLED_KEYS = frozenset({"agent_id", "message_id", "claim_id", "reason"})

_KEYS_BY_TYPE = {
    AGENT_MESSAGE_CLAIMED: CLAIMED_KEYS,
    AGENT_MESSAGE_COMPLETED: COMPLETED_KEYS,
    AGENT_MESSAGE_FAILED: FAILED_KEYS,
    AGENT_MESSAGE_CANCELLED: CANCELLED_KEYS,
}


def require_delivery_identifier(value: object, *, field: str) -> str:
    """Validate a caller-supplied delivery identifier before anything is written.

    Reuses `is_agent_identifier` - what an identifier is has one definition
    across the whole control plane - but reports it as a `DeliveryInputError`.
    """

    if not is_agent_identifier(value):
        raise DeliveryInputError("delivery-identity-invalid", field)
    assert isinstance(value, str)
    return value


def is_delivery_reason(value: object) -> bool:
    """Whether ``value`` can be persisted as a reason or error code."""

    if not isinstance(value, str):
        return False
    if not value or value != value.strip():
        return False
    if len(value) > MAX_REASON_CHARS:
        return False
    return is_single_line_safe(value)


def require_delivery_reason(value: object, *, field: str) -> str:
    """Validate a reason before it can reach the event log.

    The value is never echoed back in the error: an ``interrupt`` reason is
    caller text and may contain anything at all.
    """

    if not is_delivery_reason(value):
        raise DeliveryInputError("delivery-reason-invalid", field)
    assert isinstance(value, str)
    return value


def agent_delivery_stream(agent_id: str) -> str:
    """The delivery stream id for ``agent_id``.

    Every read, write and validation goes through this one constructor. The
    inverse is deliberately not provided: recovering an ``agent_id`` by
    splitting a stream name would make identity depend on parsing a string an
    id may itself contain separators in. Validation builds the expected name
    forward from the payload and compares.
    """

    return (
        f"{AGENT_DELIVERY_STREAM_PREFIX}"
        f"{require_delivery_identifier(agent_id, field='agent_id')}"
    )


def _read_identifier(data: dict[str, JsonValue], key: str, seq: int) -> str:
    value = data.get(key)
    if not is_agent_identifier(value):
        raise DeliveryProtocolError("delivery-identity-invalid", seq)
    assert isinstance(value, str)
    return value


def _read_reason(data: dict[str, JsonValue], key: str, seq: int) -> str:
    value = data.get(key)
    if not is_delivery_reason(value):
        raise DeliveryProtocolError("delivery-reason-invalid", seq)
    assert isinstance(value, str)
    return value


def _read_accepted_seq(data: dict[str, JsonValue], seq: int) -> int:
    value = data.get("accepted_seq")
    # ``bool`` is an ``int`` in Python; a sequence number of ``True`` is
    # malformed data, not position one.
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise DeliveryProtocolError("delivery-accepted-seq-invalid", seq)
    return value


def claimed_data(
    *,
    agent_id: str,
    message_id: str,
    accepted_seq: int,
    claim_id: str,
    activation_id: str,
    session_id: str,
) -> dict[str, JsonValue]:
    """Build the bounded claim payload.

    ``accepted_seq`` pins the claim to one exact position in the Inbox, so a
    claim cannot drift onto a different message that happens to share an id,
    and replay can prove the two streams agree.
    """

    if not isinstance(accepted_seq, int) or isinstance(accepted_seq, bool) or accepted_seq < 1:
        raise DeliveryInputError("delivery-accepted-seq-invalid", "accepted_seq")
    return {
        "agent_id": require_delivery_identifier(agent_id, field="agent_id"),
        "message_id": require_delivery_identifier(message_id, field="message_id"),
        "accepted_seq": accepted_seq,
        "claim_id": require_delivery_identifier(claim_id, field="claim_id"),
        "activation_id": require_delivery_identifier(activation_id, field="activation_id"),
        "session_id": require_delivery_identifier(session_id, field="session_id"),
    }


def completed_data(
    *,
    agent_id: str,
    message_id: str,
    claim_id: str,
    turn_id: str,
    reason: str,
) -> dict[str, JsonValue]:
    """Build the bounded completion payload.

    ``turn_id`` is the pointer into the Session Event Log. It is how a later
    reader finds what actually happened without this stream having to carry any
    of it.
    """

    return {
        "agent_id": require_delivery_identifier(agent_id, field="agent_id"),
        "message_id": require_delivery_identifier(message_id, field="message_id"),
        "claim_id": require_delivery_identifier(claim_id, field="claim_id"),
        "turn_id": require_delivery_identifier(turn_id, field="turn_id"),
        "reason": require_delivery_reason(reason, field="reason"),
    }


def failed_data(
    *,
    agent_id: str,
    message_id: str,
    claim_id: str,
    error_code: str,
) -> dict[str, JsonValue]:
    """Build the bounded failure payload.

    ``error_code`` is a stable code chosen by this repository. The original
    exception message and traceback are deliberately absent: they are arbitrary
    third-party text that may quote a request, a path or a credential.
    """

    return {
        "agent_id": require_delivery_identifier(agent_id, field="agent_id"),
        "message_id": require_delivery_identifier(message_id, field="message_id"),
        "claim_id": require_delivery_identifier(claim_id, field="claim_id"),
        "error_code": require_delivery_reason(error_code, field="error_code"),
    }


def cancelled_data(
    *,
    agent_id: str,
    message_id: str,
    claim_id: str,
    reason: str,
) -> dict[str, JsonValue]:
    """Build the bounded cancellation payload."""

    return {
        "agent_id": require_delivery_identifier(agent_id, field="agent_id"),
        "message_id": require_delivery_identifier(message_id, field="message_id"),
        "claim_id": require_delivery_identifier(claim_id, field="claim_id"),
        "reason": require_delivery_reason(reason, field="reason"),
    }


def parse_delivery_event(event: EventEnvelope) -> dict[str, JsonValue]:
    """Validate one delivery event and return its parsed payload.

    The whole event is read inside one boundary - protocol fields included.
    `EventEnvelope` is a public DTO that anything may construct, so
    ``event.type``, ``event.stream_id`` and ``event.schema_version`` are no more
    trusted than ``event.data``: a ``str`` subclass with a raising ``__ne__``
    would otherwise break the first comparison and leak a bare exception out of
    a projector whose contract is to report issues.

    ``Exception``, deliberately not ``BaseException``: `SystemExit` and
    `KeyboardInterrupt` are not verdicts about an event.
    """

    try:
        return _read_delivery_event(event)
    except DeliveryProtocolError:
        raise
    except Exception:
        raise DeliveryProtocolError("delivery-payload-invalid", event.seq) from None


def _read_delivery_event(event: EventEnvelope) -> dict[str, JsonValue]:
    if event.type not in DELIVERY_EVENT_TYPES:
        raise DeliveryProtocolError("delivery-event-type-unknown", event.seq)
    if event.schema_version != AGENT_DELIVERY_SCHEMA_VERSION:
        raise DeliveryProtocolError("delivery-schema-version-unsupported", event.seq)
    data = event.data
    if not isinstance(data, dict):
        raise DeliveryProtocolError("delivery-payload-invalid", event.seq)
    if set(data) != _KEYS_BY_TYPE[event.type]:
        raise DeliveryProtocolError("delivery-payload-keys-unexpected", event.seq)

    agent_id = _read_identifier(data, "agent_id", event.seq)
    # Built forward from the payload and compared, never parsed back out of the
    # stream name.
    if event.stream_id != agent_delivery_stream(agent_id):
        raise DeliveryProtocolError("delivery-stream-unexpected", event.seq)

    parsed: dict[str, JsonValue] = {
        "agent_id": agent_id,
        "message_id": _read_identifier(data, "message_id", event.seq),
        "claim_id": _read_identifier(data, "claim_id", event.seq),
    }
    if event.type == AGENT_MESSAGE_CLAIMED:
        parsed["accepted_seq"] = _read_accepted_seq(data, event.seq)
        parsed["activation_id"] = _read_identifier(data, "activation_id", event.seq)
        parsed["session_id"] = _read_identifier(data, "session_id", event.seq)
    elif event.type == AGENT_MESSAGE_COMPLETED:
        parsed["turn_id"] = _read_identifier(data, "turn_id", event.seq)
        parsed["reason"] = _read_reason(data, "reason", event.seq)
    elif event.type == AGENT_MESSAGE_FAILED:
        parsed["error_code"] = _read_reason(data, "error_code", event.seq)
    else:
        parsed["reason"] = _read_reason(data, "reason", event.seq)
    return parsed


__all__ = [
    "AGENT_DELIVERY_SCHEMA_VERSION",
    "AGENT_DELIVERY_STREAM_PREFIX",
    "AGENT_MESSAGE_CANCELLED",
    "AGENT_MESSAGE_CLAIMED",
    "AGENT_MESSAGE_COMPLETED",
    "AGENT_MESSAGE_FAILED",
    "CANCELLED_KEYS",
    "CLAIMED_KEYS",
    "COMPLETED_KEYS",
    "DELIVERY_EVENT_TYPES",
    "FAILED_KEYS",
    "MAX_REASON_CHARS",
    "TERMINAL_EVENT_TYPES",
    "agent_delivery_stream",
    "cancelled_data",
    "claimed_data",
    "completed_data",
    "failed_data",
    "is_delivery_reason",
    "parse_delivery_event",
    "require_delivery_identifier",
    "require_delivery_reason",
]
