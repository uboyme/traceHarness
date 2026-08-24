"""Pure reconstruction of the append-only managed workspace catalog."""

from __future__ import annotations

from dataclasses import dataclass, replace

from traceh.api.events import EventEnvelope
from traceh.api.json_types import JsonValue, canonical_json
from traceh.api.workspaces import WorkspaceRecord, WorkspaceStatus
from traceh.session.event_store import EventStore
from traceh.workspaces.errors import WorkspaceProtocolError
from traceh.workspaces.events import (
    WORKSPACE_ATTACHED,
    WORKSPACE_CATALOG_STREAM,
    WORKSPACE_PROVISIONED,
    WORKSPACE_QUARANTINED,
    WORKSPACE_RELEASED,
    WorkspaceAttachedFact,
    WorkspaceProvisionedFact,
    WorkspaceQuarantinedFact,
    WorkspaceReleasedFact,
    parse_workspace_fact,
)


@dataclass(frozen=True, slots=True)
class _Operation:
    event_type: str
    payload: str
    seq: int


@dataclass(frozen=True, slots=True)
class WorkspaceCatalogIssue:
    code: str
    seq: int


class WorkspaceCatalog:
    """Immutable view reconstructed only from ``workspaces:catalog``."""

    __slots__ = (
        "_agent_ids",
        "_head_seq",
        "_operations",
        "_request_ids",
        "_session_ids",
        "_workspaces",
    )

    def __init__(
        self,
        *,
        workspaces: dict[str, WorkspaceRecord],
        operations: dict[str, _Operation],
        request_ids: dict[str, str],
        agent_ids: dict[str, str],
        session_ids: dict[str, str],
        head_seq: int,
    ) -> None:
        self._workspaces = dict(workspaces)
        self._operations = dict(operations)
        self._request_ids = dict(request_ids)
        self._agent_ids = dict(agent_ids)
        self._session_ids = dict(session_ids)
        self._head_seq = head_seq

    @classmethod
    def empty(cls) -> WorkspaceCatalog:
        return cls(
            workspaces={},
            operations={},
            request_ids={},
            agent_ids={},
            session_ids={},
            head_seq=0,
        )

    @classmethod
    def rebuild(cls, events: tuple[EventEnvelope, ...]) -> WorkspaceCatalog:
        workspaces: dict[str, WorkspaceRecord] = {}
        operations: dict[str, _Operation] = {}
        request_ids: dict[str, str] = {}
        agent_ids: dict[str, str] = {}
        session_ids: dict[str, str] = {}
        head_seq = 0

        for expected_seq, event in enumerate(events, start=1):
            try:
                event_seq = event.seq
            except Exception:
                raise WorkspaceProtocolError(
                    "workspace-payload-invalid", expected_seq
                ) from None
            if type(event_seq) is not int or event_seq != expected_seq:
                raise WorkspaceProtocolError(
                    "workspace-sequence-invalid", expected_seq
                )
            try:
                fact = parse_workspace_fact(event)
                operation_id = fact.operation_id
                if operation_id in operations:
                    raise WorkspaceProtocolError(
                        "workspace-operation-duplicate", event.seq
                    )
                operations[operation_id] = _Operation(
                    event_type=_fact_event_type(fact),
                    payload=canonical_json(event.data),
                    seq=event.seq,
                )

                if isinstance(fact, WorkspaceProvisionedFact):
                    if fact.workspace_id in workspaces:
                        raise WorkspaceProtocolError(
                            "workspace-id-duplicate", fact.seq
                        )
                    if fact.creation_request_id in request_ids:
                        raise WorkspaceProtocolError(
                            "workspace-request-duplicate", fact.seq
                        )
                    workspaces[fact.workspace_id] = WorkspaceRecord(
                        workspace_id=fact.workspace_id,
                        provision_operation_id=fact.operation_id,
                        creation_request_id=fact.creation_request_id,
                        source_id=fact.source_id,
                        source_revision=fact.source_revision,
                        repository_fingerprint=fact.repository_fingerprint,
                        base_revision=fact.base_revision,
                        access=fact.access,
                        owner_agent_id=fact.owner_agent_id,
                        status=WorkspaceStatus.PROVISIONAL,
                        agent_id=None,
                        session_id=None,
                        reason=None,
                        provisioned_seq=fact.seq,
                        updated_seq=fact.seq,
                    )
                    request_ids[fact.creation_request_id] = fact.workspace_id
                elif isinstance(fact, WorkspaceAttachedFact):
                    record = workspaces.get(fact.workspace_id)
                    if record is None:
                        raise WorkspaceProtocolError(
                            "workspace-unknown", fact.seq
                        )
                    if record.status not in {
                        WorkspaceStatus.PROVISIONAL,
                        WorkspaceStatus.QUARANTINED,
                    }:
                        raise WorkspaceProtocolError(
                            "workspace-state-invalid", fact.seq
                        )
                    if (
                        record.agent_id is not None
                        and record.agent_id != fact.agent_id
                    ) or (
                        record.session_id is not None
                        and record.session_id != fact.session_id
                    ):
                        raise WorkspaceProtocolError(
                            "workspace-state-invalid", fact.seq
                        )
                    if (
                        fact.agent_id in agent_ids
                        and agent_ids[fact.agent_id] != fact.workspace_id
                    ):
                        raise WorkspaceProtocolError(
                            "workspace-agent-duplicate", fact.seq
                        )
                    if (
                        fact.session_id in session_ids
                        and session_ids[fact.session_id] != fact.workspace_id
                    ):
                        raise WorkspaceProtocolError(
                            "workspace-session-duplicate", fact.seq
                        )
                    workspaces[fact.workspace_id] = replace(
                        record,
                        status=WorkspaceStatus.ATTACHED,
                        agent_id=fact.agent_id,
                        session_id=fact.session_id,
                        reason=None,
                        updated_seq=fact.seq,
                    )
                    agent_ids[fact.agent_id] = fact.workspace_id
                    session_ids[fact.session_id] = fact.workspace_id
                elif isinstance(fact, WorkspaceQuarantinedFact):
                    record = workspaces.get(fact.workspace_id)
                    if record is None:
                        raise WorkspaceProtocolError(
                            "workspace-unknown", fact.seq
                        )
                    if record.status not in {
                        WorkspaceStatus.PROVISIONAL,
                        WorkspaceStatus.ATTACHED,
                    }:
                        raise WorkspaceProtocolError(
                            "workspace-state-invalid", fact.seq
                        )
                    workspaces[fact.workspace_id] = replace(
                        record,
                        status=WorkspaceStatus.QUARANTINED,
                        reason=fact.reason,
                        updated_seq=fact.seq,
                    )
                elif isinstance(fact, WorkspaceReleasedFact):
                    record = workspaces.get(fact.workspace_id)
                    if record is None:
                        raise WorkspaceProtocolError(
                            "workspace-unknown", fact.seq
                        )
                    if record.status is WorkspaceStatus.RELEASED:
                        raise WorkspaceProtocolError(
                            "workspace-state-invalid", fact.seq
                        )
                    workspaces[fact.workspace_id] = replace(
                        record,
                        status=WorkspaceStatus.RELEASED,
                        reason=fact.reason,
                        updated_seq=fact.seq,
                    )
                head_seq = fact.seq
            except WorkspaceProtocolError:
                raise
            except Exception:
                raise WorkspaceProtocolError(
                    "workspace-payload-invalid", expected_seq
                ) from None

        return cls(
            workspaces=workspaces,
            operations=operations,
            request_ids=request_ids,
            agent_ids=agent_ids,
            session_ids=session_ids,
            head_seq=head_seq,
        )

    @property
    def head_seq(self) -> int:
        return self._head_seq

    @property
    def workspaces(self) -> tuple[WorkspaceRecord, ...]:
        return tuple(self._workspaces.values())

    def get(self, workspace_id: str) -> WorkspaceRecord | None:
        return self._workspaces.get(workspace_id)

    def for_request(self, request_id: str) -> WorkspaceRecord | None:
        workspace_id = self._request_ids.get(request_id)
        return None if workspace_id is None else self._workspaces[workspace_id]

    def for_agent(self, agent_id: str) -> WorkspaceRecord | None:
        workspace_id = self._agent_ids.get(agent_id)
        return None if workspace_id is None else self._workspaces[workspace_id]

    def for_session(self, session_id: str) -> WorkspaceRecord | None:
        workspace_id = self._session_ids.get(session_id)
        return None if workspace_id is None else self._workspaces[workspace_id]

    def for_operation(self, operation_id: str) -> WorkspaceRecord | None:
        operation = self._operations.get(operation_id)
        if operation is None:
            return None
        for record in self._workspaces.values():
            if record.provision_operation_id == operation_id:
                return record
        return None

    def operation_exists(self, operation_id: str) -> bool:
        return operation_id in self._operations

    def operation_matches(
        self,
        operation_id: str,
        event_type: str,
        data: dict[str, JsonValue],
    ) -> bool:
        operation = self._operations.get(operation_id)
        return operation is not None and operation == _Operation(
            event_type=event_type,
            payload=canonical_json(data),
            seq=operation.seq,
        )


def _fact_event_type(
    fact: WorkspaceProvisionedFact
    | WorkspaceAttachedFact
    | WorkspaceQuarantinedFact
    | WorkspaceReleasedFact,
) -> str:
    """Return an owned protocol constant, never an envelope-provided object."""

    if isinstance(fact, WorkspaceProvisionedFact):
        return WORKSPACE_PROVISIONED
    if isinstance(fact, WorkspaceAttachedFact):
        return WORKSPACE_ATTACHED
    if isinstance(fact, WorkspaceQuarantinedFact):
        return WORKSPACE_QUARANTINED
    if isinstance(fact, WorkspaceReleasedFact):
        return WORKSPACE_RELEASED
    raise AssertionError("unreachable workspace fact type")


class WorkspaceCatalogReader:
    __slots__ = ("_store",)

    def __init__(self, store: EventStore) -> None:
        self._store = store

    @property
    def store(self) -> EventStore:
        return self._store

    async def read_events(self) -> tuple[EventEnvelope, ...]:
        return await self._store.read(WORKSPACE_CATALOG_STREAM)

    async def load(self) -> WorkspaceCatalog:
        return WorkspaceCatalog.rebuild(await self.read_events())


async def validate_workspace_catalog_events(
    store: EventStore,
) -> tuple[WorkspaceCatalogIssue, ...]:
    try:
        await WorkspaceCatalogReader(store).load()
    except WorkspaceProtocolError as error:
        return (WorkspaceCatalogIssue(error.code, error.seq),)
    return ()


__all__ = [
    "WorkspaceCatalog",
    "WorkspaceCatalogIssue",
    "WorkspaceCatalogReader",
    "validate_workspace_catalog_events",
]
