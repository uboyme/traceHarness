"""Read-only reconstruction of one Agent's accepted Inbox history.

`AgentInbox` answers "which messages has this Agent durably accepted, and in
what order" from the event log alone. It holds no Runtime, Task, Handle or
Supervisor, so a fresh process with nothing but an `EventStore` reproduces the
same FIFO order as the process that accepted the messages.

**Accepted is not processed.** Every message here was written down. None of
them has been delivered, claimed, executed, completed or retried - Stage B has
no Activation to do that, and this projector deliberately cannot express it.

Two properties are load-bearing:

* **Order is the point.** Acceptance order is stream order, so a broken record
  is never skipped: skipping one would report a FIFO sequence that never
  happened, which is worse than refusing to answer.
* **This is not a mutable queue.** A second acceptance of a known ``message_id``
  is a contradiction in an append-only stream, not an update, and there is no
  pop, ack or delete.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from traceh.agents.errors import AgentInboxProtocolError
from traceh.agents.inbox_identity import (
    AGENT_INBOX_STREAM_PREFIX,
    agent_inbox_stream,
    parse_message_accepted,
)
from traceh.api.agents import AcceptedMessage
from traceh.api.events import EventEnvelope
from traceh.session.event_store import EventStore


@dataclass(frozen=True, slots=True)
class AgentInboxIssue:
    """A stable, non-secret validation result for one Inbox event.

    Carries a fixed ``code`` and the ``seq`` it was found at - never the
    message content, source or any other payload value.
    """

    code: str
    seq: int


def _scan(
    events: tuple[EventEnvelope, ...],
    agent_id: str,
) -> tuple[tuple[AcceptedMessage, ...], int, tuple[AgentInboxIssue, ...]]:
    accepted: list[AcceptedMessage] = []
    by_message: dict[str, AcceptedMessage] = {}
    issues: list[AgentInboxIssue] = []
    head_seq = 0
    # Validates the caller's own argument, and normalizes it to the exact string
    # the parser will produce from a payload, so the comparison below is between
    # two values this module owns.
    expected_agent_id = agent_inbox_stream(agent_id).removeprefix(
        AGENT_INBOX_STREAM_PREFIX
    )

    for event in events:
        head_seq = event.seq
        # Event type and stream are checked *inside* the parser, not here.
        # Reading them twice would put one of those reads outside the parser's
        # exception boundary, and `EventEnvelope` is a public DTO whose fields
        # are as untrusted as its payload. Unknown types are still not skipped:
        # the parser reports ``inbox-event-type-unknown``, because this stream
        # carries acceptance facts only and a guess would report an order
        # describing neither.
        try:
            message = parse_message_accepted(event)
        except AgentInboxProtocolError as error:
            issues.append(AgentInboxIssue(error.code, error.seq))
            continue
        except Exception:
            issues.append(AgentInboxIssue("inbox-payload-invalid", event.seq))
            continue
        if message.agent_id != expected_agent_id:
            # The payload names a different Agent than the Inbox being rebuilt.
            # ``parse_message_accepted`` already proved payload and stream agree,
            # so this can only mean the caller asked for the wrong Inbox.
            issues.append(AgentInboxIssue("inbox-stream-unexpected", event.seq))
            continue
        if message.message.message_id in by_message:
            issues.append(AgentInboxIssue("inbox-message-id-duplicate", event.seq))
            continue
        accepted.append(message)
        by_message[message.message.message_id] = message

    return tuple(accepted), head_seq, tuple(issues)


def validate_agent_inbox_events(
    events: tuple[EventEnvelope, ...],
    agent_id: str,
) -> tuple[AgentInboxIssue, ...]:
    """Return every stable issue code in ``events`` without raising."""

    _, _, issues = _scan(events, agent_id)
    return issues


class AgentInbox:
    """An immutable, replay-derived view of one Agent's accepted messages."""

    __slots__ = ("_accepted", "_agent_id", "_by_message", "_head_seq")

    def __init__(
        self,
        agent_id: str,
        accepted: tuple[AcceptedMessage, ...],
        head_seq: int,
    ) -> None:
        self._agent_id = agent_id
        self._accepted = accepted
        self._head_seq = head_seq
        self._by_message = {item.message.message_id: item for item in accepted}

    @classmethod
    def rebuild(cls, events: tuple[EventEnvelope, ...], agent_id: str) -> AgentInbox:
        """Rebuild the Inbox, or raise on the first protocol issue.

        ``agent_id`` is required rather than recovered from the stream name.
        Splitting a stream id to guess an Agent would make identity depend on
        parsing a string the id may itself contain separators in; the expected
        name is built forward from it and compared instead.
        """

        accepted, head_seq, issues = _scan(events, agent_id)
        if issues:
            first = issues[0]
            raise AgentInboxProtocolError(first.code, first.seq)
        return cls(agent_id, accepted, head_seq)

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def head_seq(self) -> int:
        """The stream sequence this view was rebuilt through.

        It is the ``expected_seq`` an acceptance append must use, which is what
        makes two racing senders linearize instead of both succeeding.
        """

        return self._head_seq

    @property
    def messages(self) -> tuple[AcceptedMessage, ...]:
        """Every accepted message, in acceptance order.

        Returned directly rather than copied: `AcceptedMessage` and
        `AgentMessage` hold only immutable scalars, so no caller can write
        through one. If a mutable content block is ever added, this is the line
        that has to start detaching.
        """

        return self._accepted

    def get(self, message_id: str) -> AcceptedMessage | None:
        """The acceptance for ``message_id``, if this Agent has one."""

        return self._by_message.get(message_id)

    def __len__(self) -> int:
        return len(self._accepted)

    def __iter__(self) -> Iterator[AcceptedMessage]:
        return iter(self._accepted)


class AgentInboxReader:
    """Loads `AgentInbox` from an `EventStore` and nothing else.

    Reading an Agent that has never received a message yields an empty Inbox;
    it does not create the stream and does not assert that the Agent exists -
    that is the directory's answer, and the acceptance transaction asks it
    separately.
    """

    __slots__ = ("_store",)

    def __init__(self, store: EventStore) -> None:
        self._store = store

    @property
    def store(self) -> EventStore:
        return self._store

    async def read_events(self, agent_id: str) -> tuple[EventEnvelope, ...]:
        return await self._store.read(agent_inbox_stream(agent_id))

    async def load(self, agent_id: str) -> AgentInbox:
        return AgentInbox.rebuild(await self.read_events(agent_id), agent_id)


__all__ = [
    "AgentInbox",
    "AgentInboxIssue",
    "AgentInboxReader",
    "validate_agent_inbox_events",
]
