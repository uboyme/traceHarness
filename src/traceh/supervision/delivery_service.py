"""The delivery lifecycle write transactions.

A message is claimed exactly when its ``agent/message-claimed`` event is in the
Agent's delivery stream, and a claim has ended exactly when a terminal event
referencing it is there too. Nothing else claims or ends anything: not a
returned object, not a flag on an Activation, not the fact that a worker got
this far.

That matters more here than anywhere else in the control plane, because the
claim is what makes it safe to call a model. **A Turn may only run once the
claim is provably durable** - if two workers both believe they hold a claim,
the same message is executed twice, and no amount of later bookkeeping undoes
a tool that already wrote to a workspace.

Commit reconciliation is the shared one from `traceh.agents`, for the same
reason Stage B shares it: two definitions of "did our event land" would be two
definitions of when it is safe to proceed.
"""

from __future__ import annotations

import asyncio

from traceh.agents.commit_reconciliation import committed_after_failure
from traceh.agents.inbox import AgentInbox, AgentInboxReader
from traceh.api.agents import AcceptedMessage
from traceh.api.events import EventEnvelope, PendingEvent
from traceh.api.json_types import JsonValue, canonical_json
from traceh.session.event_store import ConcurrencyConflict, Durability, EventStore
from traceh.supervision.delivery import AgentDeliveryLog, AgentDeliveryReader, MessageClaim
from traceh.supervision.delivery_identity import (
    AGENT_MESSAGE_CANCELLED,
    AGENT_MESSAGE_CLAIMED,
    AGENT_MESSAGE_COMPLETED,
    AGENT_MESSAGE_FAILED,
    agent_delivery_stream,
    cancelled_data,
    claimed_data,
    completed_data,
    failed_data,
    parse_delivery_event,
    require_delivery_identifier,
)
from traceh.supervision.errors import (
    DeliveryAppendError,
    DeliveryConflictError,
    DeliveryProtocolError,
)


def is_delivery_fact(event: EventEnvelope, event_type: str, data: dict[str, JsonValue]) -> bool:
    """Whether ``event`` is exactly the delivery fact ``data`` would have written.

    Two steps, and both are needed. Parsing proves the candidate is a
    well-formed fact of the right type on the right stream; comparing
    ``canonical_json`` then proves it is *ours*, exactly.

    The comparison is deliberately not ``==`` on the payloads, because Python
    equality is not JSON identity: ``True == 1``, ``1 == 1.0`` and
    ``[True] == [1]`` all hold in Python while being different facts in a log.

    Only a `DeliveryProtocolError` justifies answering "not ours" - that proves
    the candidate is not a well-formed fact at all. A canonical-encoding
    failure means the comparison could not be *made*, which is not a negative
    answer, so it propagates and the shared reconciler reports ``None``.
    """

    try:
        parse_delivery_event(event)
    except DeliveryProtocolError:
        return False
    return event.type == event_type and canonical_json(event.data) == canonical_json(data)


class AgentDeliveryService:
    """Records claims and terminal outcomes in one `EventStore`."""

    __slots__ = ("_deliveries", "_inboxes", "_locks", "_store")

    def __init__(self, store: EventStore) -> None:
        self._store = store
        self._deliveries = AgentDeliveryReader(store)
        self._inboxes = AgentInboxReader(store)
        # One lock per Agent: each Agent has its own stream and therefore its
        # own compare-and-swap, so serializing unrelated Agents would be an
        # invented constraint. These are linearization aids for callers sharing
        # this object; ``expected_seq`` is what actually rejects a second
        # writer, and it keeps working across processes where these do not
        # exist.
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def store(self) -> EventStore:
        return self._store

    def _lock(self, agent_id: str) -> asyncio.Lock:
        return self._locks.setdefault(agent_id, asyncio.Lock())

    async def delivery_log(self, agent_id: str, inbox: AgentInbox) -> AgentDeliveryLog:
        return await self._deliveries.load(
            require_delivery_identifier(agent_id, field="agent_id"), inbox
        )

    async def claim(
        self,
        *,
        agent_id: str,
        accepted: AcceptedMessage,
        claim_id: str,
        activation_id: str,
        session_id: str,
        inbox: AgentInbox,
        delivery: AgentDeliveryLog,
    ) -> MessageClaim:
        """Claim one accepted message for execution.

        ``delivery`` must be the view the caller decided from: its ``head_seq``
        is carried into the append as ``expected_seq``, so a claim built on a
        stale read is rejected rather than silently applied to a log that has
        moved. That is the linearization point - two workers that both read the
        same head cannot both claim.

        Returns only when the claim is **provably durable**. Every other
        outcome raises, including "unknown", because a caller that ran a Turn
        on an unproven claim could be the second one to run it.
        """

        agent_id = require_delivery_identifier(agent_id, field="agent_id")
        data = claimed_data(
            agent_id=agent_id,
            message_id=accepted.message.message_id,
            accepted_seq=accepted.accepted_seq,
            claim_id=claim_id,
            activation_id=activation_id,
            session_id=session_id,
        )
        async with self._lock(agent_id):
            authoritative_inbox = await self._inboxes.load(agent_id)
            authoritative_delivery = await self._deliveries.load(
                agent_id, authoritative_inbox
            )
            self._require_claim_inputs(
                agent_id=agent_id,
                accepted=accepted,
                inbox=inbox,
                delivery=delivery,
                authoritative_inbox=authoritative_inbox,
                authoritative_delivery=authoritative_delivery,
            )
            appended = await self._append(
                agent_id=agent_id,
                event_type=AGENT_MESSAGE_CLAIMED,
                expected_seq=authoritative_delivery.head_seq,
                data=data,
            )
        # Rebuilt through the projector's own reader so the claim a caller
        # holds cannot differ from the one replay produces.
        rebuilt = AgentDeliveryLog.rebuild(
            (*await self._deliveries.read_events(agent_id),),
            agent_id,
            authoritative_inbox,
        )
        claim = rebuilt.claim_for(accepted.message.message_id)
        if claim is None or claim.claim_id != data["claim_id"]:  # pragma: no cover - defensive
            raise DeliveryProtocolError("delivery-claim-unknown", appended.seq)
        return claim

    async def complete(
        self,
        *,
        agent_id: str,
        claim: MessageClaim,
        turn_id: str,
        reason: str,
    ) -> None:
        await self._terminal(
            agent_id=agent_id,
            claim=claim,
            event_type=AGENT_MESSAGE_COMPLETED,
            data=completed_data(
                agent_id=agent_id,
                message_id=claim.message_id,
                claim_id=claim.claim_id,
                turn_id=turn_id,
                reason=reason,
            ),
        )

    async def fail(self, *, agent_id: str, claim: MessageClaim, error_code: str) -> None:
        await self._terminal(
            agent_id=agent_id,
            claim=claim,
            event_type=AGENT_MESSAGE_FAILED,
            data=failed_data(
                agent_id=agent_id,
                message_id=claim.message_id,
                claim_id=claim.claim_id,
                error_code=error_code,
            ),
        )

    async def cancel(self, *, agent_id: str, claim: MessageClaim, reason: str) -> None:
        await self._terminal(
            agent_id=agent_id,
            claim=claim,
            event_type=AGENT_MESSAGE_CANCELLED,
            data=cancelled_data(
                agent_id=agent_id,
                message_id=claim.message_id,
                claim_id=claim.claim_id,
                reason=reason,
            ),
        )

    async def _terminal(
        self,
        *,
        agent_id: str,
        claim: MessageClaim,
        event_type: str,
        data: dict[str, JsonValue],
    ) -> None:
        agent_id = require_delivery_identifier(agent_id, field="agent_id")
        async with self._lock(agent_id):
            inbox = await self._inboxes.load(agent_id)
            delivery = await self._deliveries.load(agent_id, inbox)
            current = delivery.claim_for(claim.message_id)
            if (
                claim.agent_id != agent_id
                or current is None
                or canonical_json(current) != canonical_json(claim)
            ):
                raise DeliveryProtocolError("delivery-claim-unknown", delivery.head_seq)
            if delivery.outcome_for_claim(claim.claim_id) is not None:
                raise DeliveryProtocolError(
                    "delivery-claim-already-terminal", delivery.head_seq
                )
            await self._append(
                agent_id=agent_id,
                event_type=event_type,
                expected_seq=delivery.head_seq,
                data=data,
            )

    @staticmethod
    def _require_claim_inputs(
        *,
        agent_id: str,
        accepted: AcceptedMessage,
        inbox: AgentInbox,
        delivery: AgentDeliveryLog,
        authoritative_inbox: AgentInbox,
        authoritative_delivery: AgentDeliveryLog,
    ) -> None:
        """Prove every caller-supplied view before a claim can be appended."""

        if inbox.agent_id != agent_id or delivery.agent_id != agent_id:
            raise DeliveryProtocolError("delivery-inbox-agent-mismatch", 0)
        durable_accepted = authoritative_inbox.get(accepted.message.message_id)
        if (
            durable_accepted is None
            or canonical_json(durable_accepted) != canonical_json(accepted)
        ):
            raise DeliveryProtocolError(
                "delivery-message-unknown", authoritative_delivery.head_seq
            )
        if delivery.head_seq != authoritative_delivery.head_seq:
            raise DeliveryConflictError()
        if canonical_json(AgentDeliveryService._delivery_facts(delivery)) != canonical_json(
            AgentDeliveryService._delivery_facts(authoritative_delivery)
        ):
            raise DeliveryProtocolError(
                "delivery-view-stale", authoritative_delivery.head_seq
            )
        next_accepted = authoritative_delivery.next_unclaimed(authoritative_inbox)
        if next_accepted is None:
            code = (
                "delivery-claim-open"
                if authoritative_delivery.has_open_claim()
                else "delivery-claim-not-next"
            )
            raise DeliveryProtocolError(code, authoritative_delivery.head_seq)
        if canonical_json(next_accepted) != canonical_json(accepted):
            raise DeliveryProtocolError(
                "delivery-claim-not-next", authoritative_delivery.head_seq
            )

    @staticmethod
    def _delivery_facts(delivery: AgentDeliveryLog) -> dict[str, object]:
        return {
            "agent_id": delivery.agent_id,
            "head_seq": delivery.head_seq,
            "claims": list(delivery.claims),
            "outcomes": [
                delivery.outcome_for_claim(claim.claim_id) for claim in delivery.claims
            ],
        }

    async def _append(
        self,
        *,
        agent_id: str,
        event_type: str,
        expected_seq: int,
        data: dict[str, JsonValue],
    ) -> EventEnvelope:
        try:
            appended = await self._store.append(
                agent_delivery_stream(agent_id),
                expected_seq=expected_seq,
                events=(PendingEvent(type=event_type, data=data),),
                durability=Durability.SYNC,
            )
        except asyncio.CancelledError as error:
            raise await self._explain(error, agent_id, event_type, data) from None
        except Exception as error:
            raise await self._explain(error, agent_id, event_type, data) from None
        # Any other ``BaseException`` - `SystemExit`, `KeyboardInterrupt` - has
        # deliberately no handler.
        return appended[0]

    async def _explain(
        self,
        error: BaseException,
        agent_id: str,
        event_type: str,
        data: dict[str, JsonValue],
    ) -> BaseException:
        committed = await self._fact_committed(agent_id, event_type, data)
        if isinstance(error, asyncio.CancelledError):
            return error
        if isinstance(error, ConcurrencyConflict) and committed is False:
            # This error promises nothing was written, so it is only used when
            # the re-read positively proved that. For a claim it is the normal
            # outcome of losing a race, and the caller simply does not run.
            return DeliveryConflictError()
        return DeliveryAppendError(committed=committed)

    async def _fact_committed(
        self,
        agent_id: str,
        event_type: str,
        data: dict[str, JsonValue],
    ) -> bool | None:
        def matches(event: EventEnvelope) -> bool:
            return is_delivery_fact(event, event_type, data)

        return await committed_after_failure(
            lambda: self._deliveries.read_events(agent_id),
            matches,
        )


__all__ = ["AgentDeliveryService", "is_delivery_fact"]
