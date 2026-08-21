"""Read-only reconstruction of durable Agent identities.

`AgentDirectory` answers "which Agents exist, and which Session does each one
own" from the event log alone. It never holds a Runtime, a Task or a Handle, so
a fresh process with nothing but an `EventStore` produces the same answers as
the process that created the Agents.

Three properties are load-bearing and are not conveniences:

* **Identity is durable, activation is not.** A record here describes an Agent
  that exists. Whether some process currently has it running is a separate,
  in-memory question that this module deliberately cannot answer, so stopping
  or restarting an Activation cannot change an identity.
* **This is not a mutable registry.** A second ``agent/created`` for a known
  ``agent_id`` is a contradiction in an append-only log, not an update. Last
  write does not win; replay fails closed.
* **Malformed history is reported, never repaired.** A directory that skipped a
  broken record would confidently describe an Agent set that never existed.

What is *not* here, by decision: no Inbox, no message delivery, no wakeup, no
Activation state, no parent/child disposal. Communication is a different
relation on different streams; `owner_agent_id` records lifecycle
responsibility only. See ADR-0019.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from traceh.agents.errors import AgentDirectoryProtocolError
from traceh.agents.identity import (
    AGENT_CREATED,
    AGENT_DIRECTORY_STREAM,
    detach_record,
    parse_agent_created,
)
from traceh.api.agents import AgentRecord
from traceh.api.events import EventEnvelope
from traceh.session.event_store import EventStore


@dataclass(frozen=True, slots=True)
class AgentDirectoryIssue:
    """A stable, non-secret validation result for one directory event.

    Mirrors `PluginIdentityIssue`: a fixed ``code`` and the ``seq`` it was found
    at, never the offending payload.
    """

    code: str
    seq: int


def _scan(
    events: tuple[EventEnvelope, ...],
) -> tuple[tuple[AgentRecord, ...], int, tuple[AgentDirectoryIssue, ...]]:
    records: list[AgentRecord] = []
    by_agent: dict[str, AgentRecord] = {}
    by_session: dict[str, AgentRecord] = {}
    by_request: dict[str, AgentRecord] = {}
    issues: list[AgentDirectoryIssue] = []
    head_seq = 0

    for event in events:
        head_seq = event.seq
        if event.type != AGENT_CREATED:
            # Unknown types are not skipped. This stream carries identity facts
            # only; anything else means the reader and the writer disagree
            # about what the stream is, and guessing would produce a directory
            # that describes neither.
            issues.append(AgentDirectoryIssue("agent-event-type-unknown", event.seq))
            continue
        try:
            record = parse_agent_created(event)
        except AgentDirectoryProtocolError as error:
            issues.append(AgentDirectoryIssue(error.code, error.seq))
            continue

        conflicts: list[AgentDirectoryIssue] = []
        if record.agent_id in by_agent:
            conflicts.append(AgentDirectoryIssue("agent-id-duplicate", event.seq))
        if record.session_id in by_session:
            conflicts.append(AgentDirectoryIssue("agent-session-duplicate", event.seq))
        if record.request_id in by_request:
            conflicts.append(AgentDirectoryIssue("agent-request-duplicate", event.seq))
        if record.owner_agent_id is not None:
            if record.owner_agent_id == record.agent_id:
                conflicts.append(AgentDirectoryIssue("agent-owner-self", event.seq))
            elif record.owner_agent_id not in by_agent:
                # Ownership must point at an Agent that already exists at this
                # point in the log, so a payload cannot invent an owner or
                # attach itself to one that is recorded later.
                conflicts.append(AgentDirectoryIssue("agent-owner-unknown", event.seq))
        if conflicts:
            issues.extend(conflicts)
            continue

        records.append(record)
        by_agent[record.agent_id] = record
        by_session[record.session_id] = record
        by_request[record.request_id] = record

    return tuple(records), head_seq, tuple(issues)


def validate_agent_directory_events(
    events: tuple[EventEnvelope, ...],
) -> tuple[AgentDirectoryIssue, ...]:
    """Return every stable issue code in ``events`` without raising."""

    _, _, issues = _scan(events)
    return issues


class AgentDirectory:
    """An immutable, replay-derived view of the durable Agent set.

    Every lookup returns a **detached** record. `AgentRecord` is frozen, but
    ``metadata`` is an ordinary nested JSON graph, so a directory that returned
    the object it keeps would let a caller write through it:
    ``directory.get(a).metadata[k] = v`` would change what every later query on
    that same directory answers. The event log stayed correct, but the shared
    projector had acquired a second, mutable version of the truth - the exact
    failure `InMemoryEventStore` avoids by detaching, for the same reason.

    The cost is a payload copy per lookup, and there is deliberately no cache:
    a cache would hand one copy to several callers and reintroduce the sharing.
    """

    __slots__ = ("_by_agent", "_by_request", "_by_session", "_head_seq", "_records")

    def __init__(self, records: tuple[AgentRecord, ...], head_seq: int) -> None:
        self._records = records
        self._head_seq = head_seq
        self._by_agent = {record.agent_id: record for record in records}
        self._by_session = {record.session_id: record for record in records}
        self._by_request = {record.request_id: record for record in records}

    @classmethod
    def rebuild(cls, events: tuple[EventEnvelope, ...]) -> AgentDirectory:
        """Rebuild the directory, or raise on the first protocol issue."""

        records, head_seq, issues = _scan(events)
        if issues:
            first = issues[0]
            raise AgentDirectoryProtocolError(first.code, first.seq)
        return cls(records, head_seq)

    @property
    def head_seq(self) -> int:
        """The stream sequence this view was rebuilt through.

        It is the ``expected_seq`` a creation append must use, which is what
        makes two racing creators linearize instead of both succeeding.
        """

        return self._head_seq

    @property
    def records(self) -> tuple[AgentRecord, ...]:
        """Every durable Agent, in creation order, detached."""

        return tuple(detach_record(record) for record in self._records)

    def get(self, agent_id: str) -> AgentRecord | None:
        record = self._by_agent.get(agent_id)
        return detach_record(record) if record is not None else None

    def for_session(self, session_id: str) -> AgentRecord | None:
        """The single Agent that owns ``session_id``, if any."""

        record = self._by_session.get(session_id)
        return detach_record(record) if record is not None else None

    def for_request(self, request_id: str) -> AgentRecord | None:
        """The Agent a previous creation request produced, if it committed."""

        record = self._by_request.get(request_id)
        return detach_record(record) if record is not None else None

    def children_of(self, agent_id: str) -> tuple[AgentRecord, ...]:
        """Agents whose lifecycle this Agent owns.

        This is the ownership relation only. It does not mean those Agents were
        forked from this one's history, and it grants no message route.
        """

        return tuple(
            detach_record(record)
            for record in self._records
            if record.owner_agent_id == agent_id
        )

    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self) -> Iterator[AgentRecord]:
        return iter(self.records)


class AgentDirectoryReader:
    """Loads `AgentDirectory` from an `EventStore` and nothing else.

    Constructing a reader against a store that has never seen an Agent yields
    an empty directory; it does not create the stream.
    """

    __slots__ = ("_store",)

    def __init__(self, store: EventStore) -> None:
        self._store = store

    @property
    def store(self) -> EventStore:
        return self._store

    async def read_events(self) -> tuple[EventEnvelope, ...]:
        return await self._store.read(AGENT_DIRECTORY_STREAM)

    async def load(self) -> AgentDirectory:
        return AgentDirectory.rebuild(await self.read_events())


__all__ = [
    "AGENT_DIRECTORY_STREAM",
    "AgentDirectory",
    "AgentDirectoryIssue",
    "AgentDirectoryReader",
    "validate_agent_directory_events",
]
