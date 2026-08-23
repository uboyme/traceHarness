"""The Agent creation transaction.

An Agent identity exists exactly when its ``agent/created`` event is in the
control-plane stream. Nothing else establishes it: not a returned object, not
an entry in a process-local dictionary, not the fact that this code ran. That
is the whole point of the module - a supervisor that decided "the Agent exists"
from its own memory would disagree with the log after any restart.

The registrar therefore owns three things and no more: validating input before
it can reach the log, linearizing the read-then-append window, and telling the
truth about an append whose outcome is uncertain.

It deliberately does **not** create the Agent's Session, start a Runtime,
deliver messages or dispose anything. Those are Activation concerns and belong
to `traceh.supervision`, which depends on this module and never the other way
round; see ADR-0019 and ADR-0021.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from traceh.agents.commit_reconciliation import committed_after_failure
from traceh.agents.directory import AgentDirectory, AgentDirectoryReader
from traceh.agents.errors import (
    AgentCreationError,
    AgentDirectoryConflictError,
    AgentIdentityConflictError,
    AgentOwnerNotFoundError,
    AgentRequestConflictError,
    AgentSessionConflictError,
)
from traceh.agents.identity import (
    AGENT_CREATED,
    AGENT_DIRECTORY_STREAM,
    AGENT_EVENT_SCHEMA_VERSION,
    agent_created_data,
    creation_matches,
    is_creation_fact,
    parse_agent_created,
    require_identifier,
    validate_spec,
)
from traceh.api.agents import AgentRecord, AgentSpec
from traceh.api.events import EventEnvelope, PendingEvent
from traceh.api.json_types import JsonValue
from traceh.session.event_store import ConcurrencyConflict, Durability, EventStore


class AgentRegistrar:
    """Records durable Agent identities in one `EventStore`."""

    __slots__ = ("_lock", "_reader", "_store")

    def __init__(self, store: EventStore) -> None:
        self._store = store
        self._reader = AgentDirectoryReader(store)
        # An in-process lock, and only that. It closes the read-then-append
        # window for callers sharing this object so two concurrent creations
        # queue instead of both reading the same head. It is not the source of
        # truth and it is not consulted to decide whether a write succeeded:
        # ``expected_seq`` is what actually rejects a second writer, and it
        # keeps working across processes where this lock does not exist.
        self._lock = asyncio.Lock()

    @property
    def store(self) -> EventStore:
        return self._store

    async def directory(self) -> AgentDirectory:
        """Reload the durable directory from the event log."""

        return await self._reader.load()

    async def create_agent(
        self,
        spec: AgentSpec,
        *,
        request_id: str,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> AgentRecord:
        """Record one Agent identity and return the record replay will produce.

        ``request_id`` is required and has no default. It is what makes a retry
        decidable: repeating a call with the same ``request_id`` returns the
        Agent that request already created instead of creating a second one.
        Generating it here would defeat that, because every retry would arrive
        as a different request.

        ``agent_id`` and ``session_id`` may be omitted, in which case fresh
        identifiers are assigned - assignment, not a default with meaning. A
        caller that wants a retry to be safe without a stable ``request_id``
        should pin ``agent_id`` instead, and the second attempt then fails with
        `AgentIdentityConflictError` rather than creating a twin.

        Raises `AgentIdentityError` for unusable input, the conflict errors for
        an identity that is already taken, `AgentDirectoryConflictError` when
        the stream moved underneath the read (nothing was written), and
        `AgentCreationError` when the append itself failed.
        """

        request_id = require_identifier(request_id, field="request_id")
        requested_agent_id = (
            require_identifier(agent_id, field="agent_id") if agent_id is not None else None
        )
        requested_session_id = (
            require_identifier(session_id, field="session_id") if session_id is not None else None
        )
        validate_spec(spec)
        agent_id = requested_agent_id if requested_agent_id is not None else str(uuid4())
        session_id = requested_session_id if requested_session_id is not None else str(uuid4())

        # The complete request is frozen here, before the first suspension
        # point. Everything after this - the conflict checks, the append and
        # the reconciliation - reads only this snapshot, never the caller's
        # `AgentSpec` again. ``AgentSpec`` is frozen but its ``metadata`` is an
        # ordinary nested graph, so a caller mutating it while this coroutine
        # is suspended in the directory read would otherwise decide what got
        # persisted.
        data = agent_created_data(
            agent_id=agent_id,
            session_id=session_id,
            request_id=request_id,
            spec=spec,
        )
        owner_agent_id = data["owner_agent_id"]

        async with self._lock:
            directory = await self._reader.load()

            existing = directory.for_request(request_id)
            if existing is not None:
                return self._reconcile_request(
                    existing,
                    requested_agent_id,
                    requested_session_id,
                    data,
                )

            if directory.get(agent_id) is not None:
                raise AgentIdentityConflictError()
            if directory.for_session(session_id) is not None:
                raise AgentSessionConflictError()
            if owner_agent_id is not None and directory.get(str(owner_agent_id)) is None:
                # Self-ownership is caught here too: ``agent_id`` is not in the
                # directory yet, so naming itself as owner is unknown.
                raise AgentOwnerNotFoundError()

            appended = await self._append(
                expected_seq=directory.head_seq,
                data=data,
            )
            # Parsed back through the projector's own reader so the returned
            # record cannot differ from the one replay will rebuild.
            return parse_agent_created(appended)

    @staticmethod
    def _reconcile_request(
        existing: AgentRecord,
        requested_agent_id: str | None,
        requested_session_id: str | None,
        data: dict[str, JsonValue],
    ) -> AgentRecord:
        """Answer a repeated ``request_id`` without creating a second Agent.

        A retry that pinned an identity is checked against what the first
        attempt actually recorded; otherwise one request id could be reused to
        mean a different Agent and silently return the wrong identity. The
        comparison uses the frozen payload, not the caller's live spec, so it
        cannot drift for the same reason the append cannot.
        """

        if requested_agent_id is not None and existing.agent_id != requested_agent_id:
            raise AgentRequestConflictError()
        if requested_session_id is not None and existing.session_id != requested_session_id:
            raise AgentRequestConflictError()
        if not creation_matches(existing, data):
            raise AgentRequestConflictError()
        return existing

    async def _append(
        self,
        *,
        expected_seq: int,
        data: dict[str, JsonValue],
    ) -> EventEnvelope:
        try:
            appended = await self._store.append(
                AGENT_DIRECTORY_STREAM,
                expected_seq=expected_seq,
                events=(
                    PendingEvent(
                        type=AGENT_CREATED,
                        data=data,
                        schema_version=AGENT_EVENT_SCHEMA_VERSION,
                    ),
                ),
                durability=Durability.SYNC,
            )
        except asyncio.CancelledError as error:
            raise await self._explain_failed_append(error, data) from None
        except Exception as error:
            raise await self._explain_failed_append(error, data) from None
        # Any other ``BaseException`` - `SystemExit`, `KeyboardInterrupt`, or a
        # future one - propagates untouched, deliberately without a handler.
        # Only `CancelledError` needs the convergence treatment; rewriting an
        # interpreter-level signal into a domain error would make a shutdown
        # look like a storage problem and swallow the interrupt.
        return appended[0]

    async def _explain_failed_append(
        self,
        error: BaseException,
        data: dict[str, JsonValue],
    ) -> BaseException:
        """Decide what a failed or cancelled append actually did.

        `EventStore` documents a commit-point boundary: a cancellation that
        lands inside the critical section raises `CancelledError` to the caller
        while the event is already durable, and there is no automatic retry. "I
        was cancelled" therefore does not mean "nothing was written", so the
        only honest answer is to look rather than assume.

        The re-read runs unconditionally, including on the cancellation path
        where its answer is not part of the raised error. That costs one extra
        read of a small stream during an already-exceptional path, and buys one
        code path that decides every outcome and one place that guarantees no
        reconciliation read is left in flight on any exit. It is converged
        through `await_worker_convergence()`, so a second and third
        cancellation cannot release the caller early.
        """

        committed = await self._request_committed(data)
        if isinstance(error, asyncio.CancelledError):
            # Cancellation is never swallowed, whichever way the append went.
            # The caller asked to stop, so it is told it stopped; if it still
            # wants the identity it reconciles by ``request_id`` through
            # ``directory()``, which is decidable precisely because
            # ``request_id`` is caller-supplied and stable across retries.
            return error
        if isinstance(error, ConcurrencyConflict) and committed is False:
            # This error type promises nothing was written, so it is only used
            # when the re-read positively proved that. An unknown answer must
            # not be upgraded into that promise.
            return AgentDirectoryConflictError()
        return AgentCreationError(committed=committed)

    async def _request_committed(self, data: dict[str, JsonValue]) -> bool | None:
        """Whether **our** creation event is in the stream.

        The question is "did *this* event land", so the match is against the
        complete frozen fact, not just ``request_id``. A writer racing us on the
        same id may have committed a different Agent; matching on the id alone
        would report ``committed=True`` for a creation that never happened and
        hand the caller somebody else's fact as its own.

        Parsing through the projector also re-checks stream, schema version and
        key set, so nothing that is not a well-formed creation fact can be
        mistaken for ours. A malformed unrelated event is skipped rather than
        making the answer unknown: it cannot be ours either way.

        Delegates to the shared reconciler so Agent creation and Inbox
        acceptance cannot drift into two different answers about when a caller
        may be released and what "not committed" is allowed to claim. The three
        states are unchanged: ``True``, ``False``, and ``None`` for "the re-read
        could not answer".
        """

        def matches(event: EventEnvelope) -> bool:
            return is_creation_fact(event, data)

        return await committed_after_failure(self._reader.read_events, matches)


__all__ = ["AgentRegistrar"]
