"""Current Session protocol and exact Step source boundaries."""

from __future__ import annotations

from traceh.api.events import EventEnvelope

CONTEXT_PROTOCOL = 13
SESSION_CREATED_KEYS = frozenset({"session_id", "workspace", "metadata", "context_protocol"})


class SessionProtocolError(ValueError):
    """A Session cannot be interpreted using this release's one protocol."""

    code = "session-context-protocol-unsupported"

    def __init__(self) -> None:
        super().__init__(self.code)


def require_session_protocol(
    events: tuple[EventEnvelope, ...], *, session_id: str | None = None
) -> None:
    """Reject old or malformed nonempty Sessions without modifying history.

    An absent stream remains absent; SessionService owns its not-found error.
    This reader is also used by consumers receiving events directly.
    """

    if not events:
        return
    first = events[0]
    data = first.data
    identity = data.get("session_id")
    if (
        first.type != "session/created"
        or type(first.seq) is not int
        or first.seq != 1
        or type(first.schema_version) is not int
        or first.schema_version != 1
        or set(data) != SESSION_CREATED_KEYS
        or type(data.get("context_protocol")) is not int
        or data["context_protocol"] != CONTEXT_PROTOCOL
        or not isinstance(identity, str)
        or not identity
        or first.stream_id != f"session:{identity}"
        or (session_id is not None and identity != session_id)
        or not isinstance(data.get("workspace"), str)
        or not data["workspace"]
        or not isinstance(data.get("metadata"), dict)
    ):
        raise SessionProtocolError


def read_step_composition_event(
    events: tuple[EventEnvelope, ...],
    *,
    session_id: str,
    turn_id: str,
    step_id: str,
    through_seq: int,
) -> EventEnvelope:
    """Resolve one exact Composition boundary in the named open Step.

    A revision identifies content, not a Step: equal revisions from another
    Step can never be used to repair a missing or incorrect source boundary.
    """

    require_session_protocol(events, session_id=session_id)
    if type(through_seq) is not int or through_seq < 1:
        raise ValueError("request-source-boundary-invalid")
    open_turn: str | None = None
    open_step: str | None = None
    compositions: list[EventEnvelope] = []
    for event in events:
        if event.seq > through_seq:
            break
        if event.stream_id != f"session:{session_id}":
            raise ValueError("request-source-session-mismatch")
        if event.type == "turn/start":
            open_turn = event.data.get("turn_id")
        elif event.type == "turn/end":
            open_turn = None
        elif event.type == "step/start":
            open_step = event.data.get("step_id")
        elif event.type == "step/end":
            open_step = None
        elif event.type == "composition/snapshot" and (
            open_turn == turn_id and open_step == step_id
        ):
            compositions.append(event)
    if (
        open_turn != turn_id
        or open_step != step_id
        or len(compositions) != 1
        or compositions[0].seq != through_seq
    ):
        raise ValueError("request-source-boundary-invalid")
    return compositions[0]
