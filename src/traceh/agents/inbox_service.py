"""The Inbox message acceptance transaction.

A message is accepted exactly when its ``agent/message-accepted`` event is in
that Agent's Inbox stream. Nothing else accepts it: not a returned receipt, not
an entry in a process-local queue, not the fact that this code ran.

**Accepted is not processed.** This module writes down that a message arrived
and where it sits in the Agent's FIFO order. It does not deliver the message,
start an Activation, claim, execute, complete, fail or retry it, and it does
not wake anything up - ``wakeup`` is recorded as the sender's *request*, for a
Stage C Supervisor that does not exist yet.

The service owns three things and no more: validating input before it can reach
the log, linearizing the read-then-append window per Agent, and telling the
truth about an append whose outcome is uncertain. It mirrors `AgentRegistrar`
deliberately, and shares that transaction's commit reconciliation rather than
restating it.
"""

from __future__ import annotations

import asyncio

from traceh.agents.commit_reconciliation import committed_after_failure
from traceh.agents.directory import AgentDirectoryReader
from traceh.agents.errors import (
    AgentInboxConflictError,
    AgentMessageAcceptError,
    AgentMessageConflictError,
    AgentUnknownError,
)
from traceh.agents.inbox import AgentInbox, AgentInboxReader
from traceh.agents.inbox_identity import (
    AGENT_MESSAGE_ACCEPTED,
    acceptance_matches,
    agent_inbox_stream,
    is_acceptance_fact,
    message_accepted_data,
    parse_message_accepted,
    require_message_identifier,
)
from traceh.api.agents import AgentMessage, MessageReceipt, MessageTarget
from traceh.api.events import EventEnvelope, PendingEvent
from traceh.api.json_types import JsonValue
from traceh.session.event_store import ConcurrencyConflict, Durability, EventStore


class AgentInboxService:
    """Accepts messages into durable per-Agent Inbox streams."""

    __slots__ = ("_directory", "_inboxes", "_locks", "_store")

    def __init__(self, store: EventStore) -> None:
        self._store = store
        self._directory = AgentDirectoryReader(store)
        self._inboxes = AgentInboxReader(store)
        # One lock per Agent, not one for the service. Each Agent has its own
        # stream and therefore its own ``expected_seq``, so serializing
        # unrelated Agents against each other would be an invented constraint.
        # These are linearization aids for callers sharing this object; the
        # compare-and-swap is what actually rejects a second writer, and it
        # keeps working across processes where these locks do not exist.
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def store(self) -> EventStore:
        return self._store

    def _lock(self, agent_id: str) -> asyncio.Lock:
        return self._locks.setdefault(agent_id, asyncio.Lock())

    async def inbox(self, agent_id: str) -> AgentInbox:
        """Reload one Agent's accepted Inbox history from the event log."""

        return await self._inboxes.load(require_message_identifier(agent_id, field="agent_id"))

    async def accept(
        self,
        agent_id: str,
        message: AgentMessage,
        *,
        target: MessageTarget,
        wakeup: bool,
    ) -> MessageReceipt:
        """Record one message into ``agent_id``'s Inbox and return its receipt.

        ``message.message_id`` is caller-supplied and required. It is what makes
        a retry decidable: repeating a call with the same ``message_id`` and the
        same content returns the original receipt instead of accepting the
        message twice. Reusing it for a *different* message is an error, not an
        update.

        Raises `AgentMessageError` for unusable input, `AgentUnknownError` when
        no such Agent is registered, `AgentMessageConflictError` on a reused
        ``message_id``, `AgentInboxConflictError` when the stream moved
        underneath the read (nothing was written), and
        `AgentMessageAcceptError` when the append itself failed.
        """

        # The complete request is frozen here, before the first suspension
        # point. Everything after this - the existence check, the idempotency
        # comparison, the append and the reconciliation - reads only this
        # payload, never the caller's objects again.
        data = message_accepted_data(
            agent_id=agent_id,
            message=message,
            target=target,
            wakeup=wakeup,
        )
        agent_id = str(data["agent_id"])
        message_id = str(data["message_id"])

        async with self._lock(agent_id):
            directory = await self._directory.load()
            if directory.get(agent_id) is None:
                # Asked before anything is written: an Inbox history for an
                # Agent that does not exist could never be claimed by anyone.
                raise AgentUnknownError()

            inbox = await self._inboxes.load(agent_id)
            existing = inbox.get(message_id)
            if existing is not None:
                if not acceptance_matches(existing, data):
                    raise AgentMessageConflictError()
                return existing.receipt()

            appended = await self._append(
                agent_id=agent_id,
                expected_seq=inbox.head_seq,
                data=data,
            )
            # Parsed back through the projector's own reader so the returned
            # receipt cannot differ from the one replay will rebuild.
            return parse_message_accepted(appended).receipt()

    async def _append(
        self,
        *,
        agent_id: str,
        expected_seq: int,
        data: dict[str, JsonValue],
    ) -> EventEnvelope:
        try:
            appended = await self._store.append(
                agent_inbox_stream(agent_id),
                expected_seq=expected_seq,
                events=(PendingEvent(type=AGENT_MESSAGE_ACCEPTED, data=data),),
                durability=Durability.SYNC,
            )
        except asyncio.CancelledError as error:
            raise await self._explain_failed_append(error, agent_id, data) from None
        except Exception as error:
            raise await self._explain_failed_append(error, agent_id, data) from None
        # Any other ``BaseException`` - `SystemExit`, `KeyboardInterrupt` - has
        # deliberately no handler. Only `CancelledError` needs the convergence
        # treatment; rewriting an interpreter-level signal into a domain error
        # would make a shutdown look like a storage problem.
        return appended[0]

    async def _explain_failed_append(
        self,
        error: BaseException,
        agent_id: str,
        data: dict[str, JsonValue],
    ) -> BaseException:
        """Decide what a failed or cancelled acceptance append actually did.

        Same commit-point reasoning as Agent creation, and the same shared
        reconciler: a cancellation inside the store's critical section leaves
        the event durable, so "I was cancelled" does not mean "nothing was
        written".
        """

        committed = await self._message_committed(agent_id, data)
        if isinstance(error, asyncio.CancelledError):
            # Cancellation is never swallowed, whichever way the append went. A
            # caller that still wants the message reconciles it by
            # ``message_id`` through ``inbox()``.
            return error
        if isinstance(error, ConcurrencyConflict) and committed is False:
            # This error type promises nothing was written, so it is only used
            # when the re-read positively proved that.
            return AgentInboxConflictError()
        return AgentMessageAcceptError(committed=committed)

    async def _message_committed(
        self,
        agent_id: str,
        data: dict[str, JsonValue],
    ) -> bool | None:
        """Whether **our** acceptance event is in this Inbox.

        Matched against the complete frozen fact through the projector own
        parser, not just ``message_id``. Two senders racing on one id write
        different messages, and matching on the id alone would tell the loser
        that its message was recorded when what landed was the other one.

        Parsing also re-checks stream, schema version and key set, so an event
        that is not a well-formed acceptance for this Agent can never be
        mistaken for ours; a malformed unrelated event is skipped rather than
        making the answer unknown.
        """

        def matches(event: EventEnvelope) -> bool:
            return is_acceptance_fact(event, data)

        return await committed_after_failure(
            lambda: self._inboxes.read_events(agent_id),
            matches,
        )


__all__ = ["AgentInboxService"]
