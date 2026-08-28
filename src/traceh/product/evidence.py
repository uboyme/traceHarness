"""Proving, from the Session stream, that a person actually agreed.

A `ProposalConfirmation` carries a Session, a Turn and a message id. Those are a
*claim*: the value asserts that someone accepted an offer, in that conversation,
in that Turn, with that message. Nothing about the value itself makes the claim
true - a caller can construct one out of thin air - so the ProductTask writer
replays the Session and requires the facts to be there.

This is the boundary F0 deliberately stopped at. It could freeze the shape of
the claim; only a fresh read can decide whether it happened.

What is checked, for the requirement's origin and for the confirmation alike:

* the Session exists (``session/created``);
* the message was durably accepted into it (``inbox/accepted``);
* it was claimed into exactly the named Turn (``inbox/claimed``);
* that claimed Turn really started (``turn/start``);
* the proposing Turn reached its durable ``turn/end``;
* and the confirming message was accepted only after that end.

The claim ties a message to a Turn. The sequence comparison is what proves
"later": merely naming a different Turn lets an older requirement message pose
as confirmation. Waiting for ``turn/end`` also rejects a message queued while
the Proposal response was still being produced.
"""

from __future__ import annotations

from dataclasses import dataclass

from traceh.api.events import EventEnvelope
from traceh.api.product import ProductTaskProposal, ProposalConfirmation
from traceh.product.errors import ProductEvidenceError
from traceh.session.event_store import EventStore
from traceh.session.invariants import CoreInvariantChecker

SESSION_STREAM_PREFIX = "session:"
SESSION_CREATED = "session/created"
INBOX_ACCEPTED = "inbox/accepted"
INBOX_CLAIMED = "inbox/claimed"
TURN_STARTED = "turn/start"
TURN_ENDED = "turn/end"
SESSION_SCHEMA_VERSION = 1

_SESSION_CREATED_KEYS = frozenset({"session_id", "workspace", "metadata"})
_INBOX_ACCEPTED_KEYS = frozenset({"message_id", "source", "content", "target"})
_INBOX_CLAIMED_KEYS = frozenset({"message_id", "turn_id"})


@dataclass(frozen=True, slots=True)
class MessageEvidence:
    """One durable user message and the Turn that claimed it."""

    session_id: str
    message_id: str
    turn_id: str
    accepted_seq: int
    claimed_seq: int


class SessionEvidenceReader:
    """Fresh replay of one Session, reading only what this domain may rely on.

    The Session stream belongs to the Runtime, not here. This reader therefore
    treats every payload as untrusted input: a value that is not a plain ``str``
    is absent rather than coerced, and any failure while reading an object the
    store handed back becomes the same stable evidence error. ``Exception`` is
    normalised; `KeyboardInterrupt` and `SystemExit` are not answers about a
    Session and reach the caller unchanged.
    """

    __slots__ = ("_store",)

    def __init__(self, store: EventStore) -> None:
        self._store = store

    @property
    def store(self) -> EventStore:
        return self._store

    async def message(self, session_id: str, message_id: str) -> MessageEvidence | None:
        messages, _, _ = await self._facts(session_id)
        return messages.get(message_id)

    async def turn_start_seq(self, session_id: str, turn_id: str) -> int | None:
        """The durable start position of one Turn, after a fresh replay."""

        _, turn_starts, _ = await self._facts(session_id)
        return turn_starts.get(turn_id)

    async def turn_end_seq(self, session_id: str, turn_id: str) -> int | None:
        """The durable end position of one Turn, after a fresh replay."""

        _, _, turn_ends = await self._facts(session_id)
        return turn_ends.get(turn_id)

    async def _facts(
        self, session_id: str
    ) -> tuple[dict[str, MessageEvidence], dict[str, int], dict[str, int]]:
        try:
            events = await self._store.read(f"{SESSION_STREAM_PREFIX}{session_id}")
        except Exception:
            # A backend-specific read failure says only that the evidence is
            # unavailable. It must not leak implementation details or become a
            # different public outcome. BaseException remains caller control.
            raise ProductEvidenceError("product-session-unreadable") from None
        return _scan_session(events, session_id)


def _scan_session(
    events: tuple[EventEnvelope, ...], session_id: str
) -> tuple[dict[str, MessageEvidence], dict[str, int], dict[str, int]]:
    try:
        created = False
        accepted_ids: set[str] = set()
        claimed_ids: set[str] = set()
        user_acceptances: dict[str, int] = {}
        claims: dict[str, tuple[str, int]] = {}
        claimed_turns: dict[str, str] = {}
        turn_starts: dict[str, int] = {}
        turn_ends: dict[str, int] = {}
        stream_id = f"{SESSION_STREAM_PREFIX}{session_id}"
        expected_seq = 1
        for event in events:
            if (
                type(event.stream_id) is not str
                or event.stream_id != stream_id
                or type(event.seq) is not int
                or event.seq != expected_seq
                or type(event.schema_version) is not int
                or event.schema_version != SESSION_SCHEMA_VERSION
            ):
                raise ValueError("invalid Session envelope")
            expected_seq += 1
            event_type = event.type
            if type(event_type) is not str:
                raise ValueError("invalid Session event type")
            data = event.data
            if type(data) is not dict:
                raise ValueError("invalid Session payload")
            if event_type == SESSION_CREATED:
                if created or event.seq != 1 or set(data) != _SESSION_CREATED_KEYS:
                    raise ValueError("invalid Session creation")
                if (
                    _text(data.get("session_id")) != session_id
                    or type(data.get("workspace")) is not str
                    or type(data.get("metadata")) is not dict
                ):
                    raise ValueError("invalid Session creation payload")
                created = True
                continue
            if not created:
                raise ValueError("Session event precedes creation")
            if event_type in (TURN_STARTED, TURN_ENDED):
                turn_id = _text(data.get("turn_id"))
                if turn_id is None:
                    raise ValueError("invalid Turn identity")
                if event_type == TURN_STARTED:
                    message_id = _text(data.get("message_id"))
                    if (
                        message_id is None
                        or turn_id in turn_starts
                        or claims.get(message_id, (None, 0))[0] != turn_id
                    ):
                        raise ValueError("invalid Turn start")
                    turn_starts[turn_id] = event.seq
                else:
                    if turn_id not in turn_starts or turn_id in turn_ends:
                        raise ValueError("invalid Turn end")
                    turn_ends[turn_id] = event.seq
                continue
            if event_type not in (INBOX_ACCEPTED, INBOX_CLAIMED):
                continue
            keys = (
                _INBOX_ACCEPTED_KEYS
                if event_type == INBOX_ACCEPTED
                else _INBOX_CLAIMED_KEYS
            )
            if set(data) != keys:
                raise ValueError("invalid Inbox payload")
            candidate_id = _text(data.get("message_id"))
            if candidate_id is None:
                raise ValueError("invalid message identity")
            if event_type == INBOX_ACCEPTED:
                if candidate_id in accepted_ids:
                    raise ValueError("duplicate message acceptance")
                source = _text(data.get("source"))
                content = _text(data.get("content"))
                target = _text(data.get("target"))
                if (
                    source is None
                    or content is None
                    or target != "new_turn"
                ):
                    raise ValueError("invalid message acceptance")
                accepted_ids.add(candidate_id)
                if source == "user":
                    user_acceptances[candidate_id] = event.seq
            else:
                if candidate_id not in accepted_ids or candidate_id in claimed_ids:
                    raise ValueError("invalid message claim order")
                claimed_ids.add(candidate_id)
                claimed_turn = _text(data.get("turn_id"))
                if claimed_turn is None or claimed_turn in claimed_turns:
                    raise ValueError("invalid Turn identity")
                claims[candidate_id] = (claimed_turn, event.seq)
                claimed_turns[claimed_turn] = candidate_id
        # Product authorization does not own a private, weaker Turn/Step state
        # machine. The core checker remains the executable definition of a
        # valid Session lifecycle; this reader only adds the Product-specific
        # message/Turn/temporal evidence above it.
        if created and CoreInvariantChecker().check(events):
            raise ValueError("invalid Session lifecycle")
    except Exception:
        raise ProductEvidenceError("product-session-unreadable") from None
    if not created:
        return {}, {}, {}
    messages = {
        message_id: MessageEvidence(
            session_id=session_id,
            message_id=message_id,
            turn_id=turn_id,
            accepted_seq=accepted_seq,
            claimed_seq=claimed_seq,
        )
        for message_id, accepted_seq in user_acceptances.items()
        if (claim := claims.get(message_id)) is not None
        for turn_id, claimed_seq in (claim,)
    }
    return messages, turn_starts, turn_ends


def _text(value: object) -> str | None:
    # A ``str`` subclass may compare equal here and behave differently later, so
    # the value is normalised to a plain ``str`` before it is used or stored.
    return str(value) if type(value) is str else None


async def require_confirmation_evidence(
    reader: SessionEvidenceReader,
    proposal: ProductTaskProposal,
    confirmation: ProposalConfirmation,
) -> None:
    """Refuse to open a task unless the Session shows both message contexts.

    ``proposal_confirmable()`` has already decided the identity-only rules. This
    decides the facts and the temporal rule: the proposing Turn must have ended,
    and the confirming message must have been accepted afterwards. Both are needed:
    different ids do not prove order, and presence alone would let an older
    message through. This proves durable identity and time, not natural-language
    consent; the trusted host owns the separate start-authorization decision.
    """

    origin_session_id = _evidence_identity(proposal.origin_session_id)
    origin_message_id = _evidence_identity(proposal.origin_message_id)
    origin_turn_id = _evidence_identity(proposal.origin_turn_id)
    confirmation_session_id = _evidence_identity(
        confirmation.confirming_session_id
    )
    confirmation_message_id = _evidence_identity(
        confirmation.confirming_message_id
    )
    confirmation_turn_id = _evidence_identity(confirmation.confirming_turn_id)

    origin = await reader.message(origin_session_id, origin_message_id)
    if origin is None:
        raise ProductEvidenceError("product-origin-message-unknown")
    if origin.turn_id != origin_turn_id:
        raise ProductEvidenceError("product-origin-turn-mismatch")
    if await reader.turn_start_seq(origin_session_id, origin_turn_id) is None:
        raise ProductEvidenceError("product-origin-turn-unknown")

    confirming = await reader.message(
        confirmation_session_id, confirmation_message_id
    )
    if confirming is None:
        raise ProductEvidenceError("product-confirmation-message-unknown")
    if confirming.turn_id != confirmation_turn_id:
        raise ProductEvidenceError("product-confirmation-turn-mismatch")
    if (
        await reader.turn_start_seq(confirmation_session_id, confirmation_turn_id)
        is None
    ):
        raise ProductEvidenceError("product-confirmation-turn-unknown")
    proposed_turn_id = _evidence_identity(proposal.proposed_turn_id)
    proposed_end_seq = await reader.turn_end_seq(origin_session_id, proposed_turn_id)
    if proposed_end_seq is None:
        raise ProductEvidenceError("product-proposal-turn-incomplete")
    if confirming.accepted_seq <= proposed_end_seq:
        raise ProductEvidenceError("product-confirmation-not-after-proposal")


def _evidence_identity(value: object) -> str:
    """Evidence reads only the domain-owned values produced by normalization."""

    if type(value) is not str:
        raise ProductEvidenceError("product-confirmation-identity-invalid")
    return value


__all__ = [
    "INBOX_ACCEPTED",
    "INBOX_CLAIMED",
    "SESSION_CREATED",
    "SESSION_SCHEMA_VERSION",
    "SESSION_STREAM_PREFIX",
    "TURN_ENDED",
    "TURN_STARTED",
    "MessageEvidence",
    "SessionEvidenceReader",
    "require_confirmation_evidence",
]
