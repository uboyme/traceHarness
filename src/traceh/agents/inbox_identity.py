"""What an accepted Inbox message is, written once for writer and replay.

The acceptance transaction and the Inbox projector must not develop two
readings of the same bytes. If the writer accepts a value the projector later
rejects, a message is accepted for one call and vanishes on replay - and unlike
a rejected creation, that failure is permanent in an append-only stream.

This module owns the stream name, the event type, the schema version, the exact
payload shape and the parse. `AgentInboxService` and `AgentInbox` both import
it; neither has a private rule.

**Accepted is not processed.** Everything here records that a message was
durably received and in what order. Nothing here claims it was delivered,
claimed by an Activation, executed, completed, failed or retried, and no field
means any of those. Stage B has no Supervisor to do them.
"""

from __future__ import annotations

from traceh.agents.errors import AgentInboxProtocolError, AgentMessageError
from traceh.agents.identity import is_agent_identifier
from traceh.api.agents import AcceptedMessage, AgentMessage, MessageTarget
from traceh.api.events import EventEnvelope
from traceh.api.json_types import JsonValue, canonical_json

AGENT_INBOX_STREAM_PREFIX = "agent-inbox:"
"""Namespace of the per-Agent Inbox streams.

One stream per Agent, not one shared stream: FIFO order is a property *of an
Agent's* Inbox, and a shared stream would make one Agent's traffic advance
another's `expected_seq` and serialize unrelated senders against each other.
"""

AGENT_MESSAGE_ACCEPTED = "agent/message-accepted"
"""The only event type an Inbox stream carries in Stage B.

Claim, completion, failure and retry are deliberately absent. They are
Activation facts, and inventing a field for them here would let a later reader
mistake "we wrote this down" for "we ran it".
"""

AGENT_INBOX_SCHEMA_VERSION = 1
"""The only payload shape this projector can read.

A later writer that changes what a field means must raise this, so an old
projector refuses the event rather than reading unfamiliar bytes as a v1
acceptance.
"""

MAX_MESSAGE_CONTENT_CHARS = 1_048_576
"""Upper bound on one message's ``content``.

The bound is a protocol fact, not a style preference. An event is persisted as
a single JSONL line, so unbounded content is an unbounded line for every future
reader, and the store offers no bound of its own. It is deliberately generous:
this is a limit on what one event may carry, not an opinion about how people
should write messages.
"""

AGENT_MESSAGE_ACCEPTED_KEYS = frozenset(
    {
        "agent_id",
        "message_id",
        "content",
        "source",
        "target",
        "wakeup",
        "correlation_id",
        "causation_id",
    }
)
"""Exactly the keys an ``agent/message-accepted`` payload carries at version 1."""

_TARGET_VALUES = frozenset(item.value for item in MessageTarget)


def require_message_identifier(value: object, *, field: str) -> str:
    """Validate a caller-supplied Inbox identifier before anything is written.

    Reuses `is_agent_identifier` - the rule for what an identifier is has one
    definition - but reports it as an `AgentMessageError`, so every rejection
    on the Inbox write surface has one type and one wording regardless of which
    field failed.
    """

    if not is_agent_identifier(value):
        raise AgentMessageError("inbox-identity-invalid", field)
    assert isinstance(value, str)
    return value


def agent_inbox_stream(agent_id: str) -> str:
    """The Inbox stream id for ``agent_id``.

    Every read, write and validation goes through this one constructor. The
    inverse is deliberately not provided: recovering an ``agent_id`` by
    splitting a stream name would make the id depend on parsing a string that
    an id may itself contain separators in. Validation instead builds the
    expected name forward from the payload's ``agent_id`` and compares.
    """

    return f"{AGENT_INBOX_STREAM_PREFIX}{require_message_identifier(agent_id, field='agent_id')}"


def is_message_content(value: object) -> bool:
    """Whether ``value`` can be carried as message content.

    Content is *not* an identifier and the single-line terminal rules must not
    be applied to it: a message is ordinary prose and may legitimately contain
    newlines, tabs and any script. What it may not be:

    * a non-``str``;
    * longer than `MAX_MESSAGE_CONTENT_CHARS`;
    * un-encodable as UTF-8. A lone surrogate survives ``json.dumps`` and then
      raises `UnicodeEncodeError` inside `JsonlEventStore.append()` - accepting
      it here would mean the writer admits content the store cannot persist,
      surfacing as a bare encoding error mid-transaction.
    """

    if not isinstance(value, str):
        return False
    if len(value) > MAX_MESSAGE_CONTENT_CHARS:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _read_identifier(data: dict[str, JsonValue], key: str, seq: int) -> str:
    value = data.get(key)
    if not is_agent_identifier(value):
        raise AgentInboxProtocolError("inbox-identity-invalid", seq)
    assert isinstance(value, str)
    return value


def _read_optional_identifier(data: dict[str, JsonValue], key: str, seq: int) -> str | None:
    # A missing key cannot reach here - the exact key-set gate has already
    # rejected it - so an explicit ``null`` unambiguously means "no relation"
    # rather than "the writer omitted it". ``dict.get()`` would collapse the two.
    value = data[key]
    if value is None:
        return None
    if not is_agent_identifier(value):
        raise AgentInboxProtocolError("inbox-identity-invalid", seq)
    assert isinstance(value, str)
    return value


def _read_target(data: dict[str, JsonValue], seq: int) -> MessageTarget:
    value = data.get("target")
    # Compared against the enum's real values; never ``MessageTarget(str(value))``,
    # which would coerce an unknown routing instruction into a known one.
    if not isinstance(value, str) or value not in _TARGET_VALUES:
        raise AgentInboxProtocolError("inbox-target-invalid", seq)
    return MessageTarget(value)


def _read_wakeup(data: dict[str, JsonValue], seq: int) -> bool:
    value = data.get("wakeup")
    # Strictly ``bool``. Truthiness would read ``1``, ``"false"`` or ``[]`` as a
    # wakeup decision, and this field will later decide whether an Activation is
    # started - a place where guessing is not acceptable.
    if not isinstance(value, bool):
        raise AgentInboxProtocolError("inbox-wakeup-invalid", seq)
    return value


def validate_message(message: AgentMessage, *, target: object, wakeup: object) -> None:
    """Reject a message before it can reach the event log."""

    if not isinstance(message, AgentMessage):
        raise AgentMessageError("inbox-message-invalid", "message")
    require_message_identifier(message.message_id, field="message_id")
    require_message_identifier(message.source, field="source")
    if not is_message_content(message.content):
        # The value is never echoed: message content is the most likely place
        # for a caller to have pasted something private.
        raise AgentMessageError("inbox-content-invalid", "content")
    if message.correlation_id is not None:
        require_message_identifier(message.correlation_id, field="correlation_id")
    if message.causation_id is not None:
        require_message_identifier(message.causation_id, field="causation_id")
    if not isinstance(target, MessageTarget):
        raise AgentMessageError("inbox-target-invalid", "target")
    if not isinstance(wakeup, bool):
        raise AgentMessageError("inbox-wakeup-invalid", "wakeup")


def message_accepted_data(
    *,
    agent_id: str,
    message: AgentMessage,
    target: MessageTarget,
    wakeup: bool,
) -> dict[str, JsonValue]:
    """Build the bounded, append-only acceptance payload.

    Every field is an immutable scalar, so this needs no deep copy: unlike an
    Agent's free-form ``metadata`` there is no nested graph a caller could
    mutate while the transaction is suspended. That is a property of the current
    message shape, not a permanent licence - a future `ContentBlock` or
    attachment list would reintroduce shared mutable state and this boundary
    would have to take its own copy again.

    Raises `AgentMessageError` for anything this protocol cannot carry, rather
    than substituting a value: it is public, and silently repairing a message
    would persist something the caller never wrote.
    """

    validate_message(message, target=target, wakeup=wakeup)
    return {
        "agent_id": require_message_identifier(agent_id, field="agent_id"),
        "message_id": message.message_id,
        "content": message.content,
        "source": message.source,
        "target": target.value,
        "wakeup": wakeup,
        "correlation_id": message.correlation_id,
        "causation_id": message.causation_id,
    }


def parse_message_accepted(event: EventEnvelope) -> AcceptedMessage:
    """Rebuild one accepted message from its event.

    The acceptance transaction parses the envelope it just appended through
    this same function, so the receipt a caller receives is exactly what replay
    produces. There is no second, more forgiving in-memory reading.
    """

    try:
        return _read_message_accepted(event)
    except AgentInboxProtocolError:
        raise
    except Exception:
        # The boundary covers the **whole** event, not only ``event.data``.
        # `EventEnvelope` is a public DTO that anything may construct, so its
        # protocol fields are as untrusted as its payload: a ``str`` subclass
        # with a raising ``__ne__`` as ``event.type`` breaks the very first
        # comparison. Leaking that would bypass the one stable outcome and
        # break `validate_agent_inbox_events`, whose contract is not to raise.
        # Only ``event.seq`` is read out here, and plain attribute access
        # cannot execute anything.
        #
        # ``Exception``, deliberately not ``BaseException``: `SystemExit` and
        # `KeyboardInterrupt` are not verdicts about the event.
        raise AgentInboxProtocolError("inbox-payload-invalid", event.seq) from None


def _read_message_accepted(event: EventEnvelope) -> AcceptedMessage:
    if event.type != AGENT_MESSAGE_ACCEPTED:
        raise AgentInboxProtocolError("inbox-event-type-unknown", event.seq)
    if event.schema_version != AGENT_INBOX_SCHEMA_VERSION:
        raise AgentInboxProtocolError("inbox-schema-version-unsupported", event.seq)
    data = event.data
    if not isinstance(data, dict):
        raise AgentInboxProtocolError("inbox-payload-invalid", event.seq)
    if set(data) != AGENT_MESSAGE_ACCEPTED_KEYS:
        # Exactly this key set. An extra key means the writer knows something
        # this reader does not, and reading the rest as a complete v1
        # acceptance would silently drop whatever it added.
        raise AgentInboxProtocolError("inbox-payload-keys-unexpected", event.seq)

    agent_id = _read_identifier(data, "agent_id", event.seq)
    # Built forward from the payload and compared, never parsed back out of the
    # stream name. ``agent_id`` has just been validated by the same rule the
    # constructor applies, so building it here cannot fail. An acceptance is
    # only a fact on its own Agent's Inbox: read out of another stream it would
    # let one Agent's traffic appear in another's FIFO order.
    if event.stream_id != agent_inbox_stream(agent_id):
        raise AgentInboxProtocolError("inbox-stream-unexpected", event.seq)

    content = data.get("content")
    if not is_message_content(content):
        raise AgentInboxProtocolError("inbox-content-invalid", event.seq)
    assert isinstance(content, str)

    return AcceptedMessage(
        agent_id=agent_id,
        message=AgentMessage(
            message_id=_read_identifier(data, "message_id", event.seq),
            content=content,
            source=_read_identifier(data, "source", event.seq),
            correlation_id=_read_optional_identifier(data, "correlation_id", event.seq),
            causation_id=_read_optional_identifier(data, "causation_id", event.seq),
        ),
        target=_read_target(data, event.seq),
        wakeup=_read_wakeup(data, event.seq),
        accepted_seq=event.seq,
    )


def acceptance_matches(accepted: AcceptedMessage, data: dict[str, JsonValue]) -> bool:
    """Whether ``accepted`` records the same message as this payload.

    Compared field by field against the frozen payload rather than the caller's
    live objects, so a repeated ``message_id`` is judged against exactly what
    this call committed to writing. Every field participates: unlike an Agent's
    free-form ``metadata``, there is nothing here that is merely cosmetic -
    different content under one ``message_id`` is a different message.
    """

    return (
        accepted.agent_id == data["agent_id"]
        and accepted.message.message_id == data["message_id"]
        and accepted.message.content == data["content"]
        and accepted.message.source == data["source"]
        and accepted.target.value == data["target"]
        and accepted.wakeup is data["wakeup"]
        and accepted.message.correlation_id == data["correlation_id"]
        and accepted.message.causation_id == data["causation_id"]
    )


def is_acceptance_fact(event: EventEnvelope, data: dict[str, JsonValue]) -> bool:
    """Whether ``event`` is exactly the acceptance ``data`` would have written.

    Used for commit reconciliation, where the question is *"did **our** event
    land"*. Two senders racing on one ``message_id`` write different messages,
    so matching on the id alone would tell the loser its message was recorded.

    Parsing proves the candidate is a well-formed acceptance on the right
    Agent's stream at the right schema version; ``canonical_json`` then proves
    it is ours exactly. The comparison is deliberately not ``==`` on the
    payloads: Python equality is not JSON identity - ``True == 1``,
    ``1 == 1.0`` and ``[True] == [1]`` are all true in Python while being
    different facts in a log.
    """

    try:
        parse_message_accepted(event)
    except AgentInboxProtocolError:
        # Definitively not a well-formed acceptance, so definitively not ours.
        # This is the *only* thing that justifies ``False``.
        return False
    # Anything else propagates on purpose, so `committed_after_failure` can
    # report ``None`` (unknown). A comparison that could not be made is not a
    # negative answer.
    return canonical_json(event.data) == canonical_json(data)


__all__ = [
    "AGENT_INBOX_SCHEMA_VERSION",
    "AGENT_INBOX_STREAM_PREFIX",
    "AGENT_MESSAGE_ACCEPTED",
    "AGENT_MESSAGE_ACCEPTED_KEYS",
    "MAX_MESSAGE_CONTENT_CHARS",
    "acceptance_matches",
    "agent_inbox_stream",
    "is_acceptance_fact",
    "is_message_content",
    "message_accepted_data",
    "parse_message_accepted",
    "require_message_identifier",
    "validate_message",
]
