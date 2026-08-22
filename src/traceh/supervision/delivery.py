"""Read-only reconstruction of one Agent's delivery lifecycle.

`AgentDeliveryLog` answers *"which accepted messages have been claimed, and how
did each claim end"* from the event log alone. It holds no Supervisor, Runtime,
Task or Activation, so a fresh process with nothing but an `EventStore` and the
Agent's Inbox reaches the same conclusions.

This projector is what stops a message being executed twice, so it fails closed
harder than a display projection would: a claim it could not validate is never
treated as "no claim", because that reading is the one that runs a Turn again.

State per accepted message::

    accepted (Stage B)  ->  claimed  ->  completed | failed | cancelled

There is no unclaim, no retry and no takeover of a stale claim. Those need an
attempt identity and a recovery policy that Stage C does not have, and guessing
would mean running a message a second time.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from traceh.agents.inbox import AgentInbox
from traceh.api.events import EventEnvelope
from traceh.api.json_types import JsonValue
from traceh.session.event_store import EventStore
from traceh.supervision.delivery_identity import (
    AGENT_MESSAGE_CLAIMED,
    AGENT_MESSAGE_COMPLETED,
    AGENT_MESSAGE_FAILED,
    agent_delivery_stream,
    parse_delivery_event,
)
from traceh.supervision.errors import DeliveryProtocolError


@dataclass(frozen=True, slots=True)
class DeliveryIssue:
    """A stable, non-secret validation result for one delivery event."""

    code: str
    seq: int


@dataclass(frozen=True, slots=True)
class MessageClaim:
    """One durable claim on an accepted message."""

    agent_id: str
    message_id: str
    accepted_seq: int
    claim_id: str
    activation_id: str
    session_id: str
    claimed_seq: int


@dataclass(frozen=True, slots=True)
class MessageOutcome:
    """How a claim ended.

    ``turn_id`` is present only for a completion, and is the pointer into the
    Session Event Log. ``code`` is a stable reason or error code from this
    repository - never third-party text.
    """

    claim_id: str
    message_id: str
    state: str
    code: str
    turn_id: str | None
    terminal_seq: int


def _scan(
    events: tuple[EventEnvelope, ...],
    agent_id: str,
    inbox: AgentInbox,
) -> tuple[
    tuple[MessageClaim, ...],
    dict[str, MessageOutcome],
    int,
    tuple[DeliveryIssue, ...],
]:
    claims: list[MessageClaim] = []
    by_claim_id: dict[str, MessageClaim] = {}
    by_message: dict[str, MessageClaim] = {}
    outcomes: dict[str, MessageOutcome] = {}
    issues: list[DeliveryIssue] = []
    head_seq = 0
    expected_agent_id = agent_delivery_stream(agent_id).removeprefix("agent-delivery:")

    for event in events:
        head_seq = event.seq
        # Event type, stream and schema are checked *inside* the parser. Reading
        # them here as well would put one of those reads outside the parser's
        # exception boundary.
        try:
            payload = parse_delivery_event(event)
            event_type = event.type
        except DeliveryProtocolError as error:
            issues.append(DeliveryIssue(error.code, error.seq))
            continue
        except Exception:
            issues.append(DeliveryIssue("delivery-payload-invalid", event.seq))
            continue

        if payload["agent_id"] != expected_agent_id:
            issues.append(DeliveryIssue("delivery-stream-unexpected", event.seq))
            continue

        message_id = str(payload["message_id"])
        claim_id = str(payload["claim_id"])

        if event_type == AGENT_MESSAGE_CLAIMED:
            accepted = inbox.get(message_id)
            if accepted is None:
                # A claim on a message this Agent never accepted would let the
                # delivery log invent work out of nothing.
                issues.append(DeliveryIssue("delivery-message-unknown", event.seq))
                continue
            if accepted.accepted_seq != payload["accepted_seq"]:
                issues.append(DeliveryIssue("delivery-accepted-seq-mismatch", event.seq))
                continue
            if claim_id in by_claim_id:
                issues.append(DeliveryIssue("delivery-claim-id-duplicate", event.seq))
                continue
            if message_id in by_message:
                # Exactly one claim per message. A second one is a contradiction
                # in an append-only log, and treating it as a retry would be
                # inventing a retry policy this Stage does not have.
                issues.append(DeliveryIssue("delivery-claim-duplicate", event.seq))
                continue
            next_message_id: str | None = None
            blocked_by_open_claim = False
            for queued in inbox.messages:
                prior = by_message.get(queued.message.message_id)
                if prior is None:
                    next_message_id = queued.message.message_id
                    break
                if prior.claim_id not in outcomes:
                    blocked_by_open_claim = True
                    break
            if blocked_by_open_claim:
                issues.append(DeliveryIssue("delivery-claim-open", event.seq))
                continue
            if next_message_id != message_id:
                issues.append(DeliveryIssue("delivery-claim-not-next", event.seq))
                continue
            claim = MessageClaim(
                agent_id=expected_agent_id,
                message_id=message_id,
                accepted_seq=int(payload["accepted_seq"]),  # type: ignore[arg-type]
                claim_id=claim_id,
                activation_id=str(payload["activation_id"]),
                session_id=str(payload["session_id"]),
                claimed_seq=event.seq,
            )
            claims.append(claim)
            by_claim_id[claim_id] = claim
            by_message[message_id] = claim
            continue

        claim = by_claim_id.get(claim_id)
        if claim is None:
            issues.append(DeliveryIssue("delivery-claim-unknown", event.seq))
            continue
        if claim.message_id != message_id:
            issues.append(DeliveryIssue("delivery-claim-message-mismatch", event.seq))
            continue
        if claim_id in outcomes:
            # completed, failed and cancelled are mutually exclusive, and each
            # claim reaches exactly one of them exactly once.
            issues.append(DeliveryIssue("delivery-claim-already-terminal", event.seq))
            continue
        if event_type == AGENT_MESSAGE_COMPLETED:
            state, code, turn_id = "completed", str(payload["reason"]), str(payload["turn_id"])
        elif event_type == AGENT_MESSAGE_FAILED:
            state, code, turn_id = "failed", str(payload["error_code"]), None
        else:
            state, code, turn_id = "cancelled", str(payload["reason"]), None
        outcomes[claim_id] = MessageOutcome(
            claim_id=claim_id,
            message_id=message_id,
            state=state,
            code=code,
            turn_id=turn_id,
            terminal_seq=event.seq,
        )

    return tuple(claims), outcomes, head_seq, tuple(issues)


def validate_agent_delivery_events(
    events: tuple[EventEnvelope, ...],
    agent_id: str,
    inbox: AgentInbox,
) -> tuple[DeliveryIssue, ...]:
    """Return every stable issue code in ``events`` without raising."""

    _, _, _, issues = _scan(events, agent_id, inbox)
    return issues


class AgentDeliveryLog:
    """An immutable, replay-derived view of one Agent's delivery lifecycle."""

    __slots__ = ("_agent_id", "_by_claim_id", "_by_message", "_claims", "_head_seq", "_outcomes")

    def __init__(
        self,
        agent_id: str,
        claims: tuple[MessageClaim, ...],
        outcomes: dict[str, MessageOutcome],
        head_seq: int,
    ) -> None:
        self._agent_id = agent_id
        self._claims = claims
        self._outcomes = dict(outcomes)
        self._head_seq = head_seq
        self._by_claim_id = {claim.claim_id: claim for claim in claims}
        self._by_message = {claim.message_id: claim for claim in claims}

    @classmethod
    def rebuild(
        cls,
        events: tuple[EventEnvelope, ...],
        agent_id: str,
        inbox: AgentInbox,
    ) -> AgentDeliveryLog:
        """Rebuild the delivery log, or raise on the first protocol issue.

        ``inbox`` is required, not optional: a claim is only meaningful against
        the acceptance it references, and validating that link is what proves
        the two streams agree about which message is being executed.
        """

        expected_agent_id = agent_delivery_stream(agent_id).removeprefix(
            "agent-delivery:"
        )
        if inbox.agent_id != expected_agent_id:
            raise DeliveryProtocolError("delivery-inbox-agent-mismatch", 0)

        claims, outcomes, head_seq, issues = _scan(events, agent_id, inbox)
        if issues:
            first = issues[0]
            raise DeliveryProtocolError(first.code, first.seq)
        return cls(agent_id, claims, outcomes, head_seq)

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def head_seq(self) -> int:
        """The stream sequence this view was rebuilt through.

        It is the ``expected_seq`` a claim append must use, which is what makes
        two racing workers linearize instead of both claiming.
        """

        return self._head_seq

    @property
    def claims(self) -> tuple[MessageClaim, ...]:
        """Every durable claim, in claim order.

        Returned directly: `MessageClaim` and `MessageOutcome` hold only
        immutable scalars, so no caller can write through one.
        """

        return self._claims

    def claim_for(self, message_id: str) -> MessageClaim | None:
        return self._by_message.get(message_id)

    def outcome_for_claim(self, claim_id: str) -> MessageOutcome | None:
        return self._outcomes.get(claim_id)

    def outcome_for_message(self, message_id: str) -> MessageOutcome | None:
        claim = self._by_message.get(message_id)
        return None if claim is None else self._outcomes.get(claim.claim_id)

    def is_claimed(self, message_id: str) -> bool:
        return message_id in self._by_message

    def next_unclaimed(self, inbox: AgentInbox):
        """The earliest accepted message that has not been claimed.

        Strict FIFO over `AgentInbox` order: the first unclaimed message wins,
        and a later one is never taken instead. Skipping ahead would silently
        reorder what the sender queued.
        """

        for accepted in inbox.messages:
            claim = self._by_message.get(accepted.message.message_id)
            if claim is None:
                return accepted
            if claim.claim_id not in self._outcomes:
                # Stage C has no stale-claim takeover or retry identity.  An
                # open claim at the FIFO head therefore blocks everything
                # behind it; skipping it would run later work out of order and
                # might run the open work twice after a crash.
                return None
        return None

    def has_open_claim(self) -> bool:
        """Whether some claim has not yet reached a terminal state."""

        return any(claim.claim_id not in self._outcomes for claim in self._claims)

    def __len__(self) -> int:
        return len(self._claims)

    def __iter__(self) -> Iterator[MessageClaim]:
        return iter(self._claims)


class AgentDeliveryReader:
    """Loads `AgentDeliveryLog` from an `EventStore` and nothing else."""

    __slots__ = ("_store",)

    def __init__(self, store: EventStore) -> None:
        self._store = store

    @property
    def store(self) -> EventStore:
        return self._store

    async def read_events(self, agent_id: str) -> tuple[EventEnvelope, ...]:
        return await self._store.read(agent_delivery_stream(agent_id))

    async def load(self, agent_id: str, inbox: AgentInbox) -> AgentDeliveryLog:
        return AgentDeliveryLog.rebuild(await self.read_events(agent_id), agent_id, inbox)


def delivery_payload_of(event: EventEnvelope) -> dict[str, JsonValue]:
    """Parsed payload of one delivery event, for reconciliation comparisons."""

    return parse_delivery_event(event)


__all__ = [
    "AgentDeliveryLog",
    "AgentDeliveryReader",
    "DeliveryIssue",
    "MessageClaim",
    "MessageOutcome",
    "delivery_payload_of",
    "validate_agent_delivery_events",
]
