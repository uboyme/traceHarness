"""Stage C contracts for the durable managed workspace catalog."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from traceh.agents import AgentRegistrar
from traceh.api.agents import AgentSpec
from traceh.api.events import EventEnvelope, PendingEvent
from traceh.api.workspaces import (
    WorkspaceAccess,
    WorkspaceHandle,
    WorkspaceLocalState,
    WorkspaceProvisioningRequest,
    WorkspaceRecord,
    WorkspaceSourceSnapshot,
    WorkspaceStatus,
)
from traceh.session.event_store import Durability, InMemoryEventStore
from traceh.session.service import SessionService
from traceh.workspaces import (
    WorkspaceCatalog,
    WorkspaceCatalogReader,
    WorkspaceDirectoryMismatchError,
    WorkspaceDirtyError,
    WorkspaceInputError,
    WorkspaceOperationConflictError,
    WorkspacePathError,
    WorkspaceProtocolError,
    WorkspaceQuarantinedError,
    WorkspaceService,
    WorkspaceSessionMismatchError,
    WorkspaceSourceError,
    WorkspaceStateError,
    validate_workspace_catalog_events,
    workspace_operation_id,
)
from traceh.workspaces.events import (
    QUARANTINE_REASONS,
    RELEASE_REASONS,
    WORKSPACE_ATTACHED,
    WORKSPACE_CATALOG_STREAM,
    WORKSPACE_PROVISIONED,
    WORKSPACE_QUARANTINED,
    WORKSPACE_RELEASED,
    attached_data,
    lifecycle_data,
    parse_workspace_fact,
    provisioned_data,
    require_workspace_identifier,
)

_REPOSITORY_FINGERPRINT = "a" * 64
_BASE_REVISION = "b" * 40


class _HostileString(str):
    failure: type[BaseException] = ValueError

    def __ne__(self, other):
        del other
        raise self.failure("hostile fixture")


class _HostileIdentifier(str):
    def strip(self, chars=None):
        del chars
        raise ValueError("hostile fixture")


class _DelayedHostileEventType(str):
    def __new__(cls, value: str):
        instance = super().__new__(cls, value)
        instance.comparisons = 0
        return instance

    def __eq__(self, other):
        self.comparisons += 1
        if self.comparisons > 1:
            raise RuntimeError("hostile late comparison")
        return super().__eq__(other)


class _HostileSequenceEvent:
    @property
    def seq(self):
        raise ValueError("hostile fixture")


def _request() -> WorkspaceProvisioningRequest:
    return WorkspaceProvisioningRequest(
        source_id="source-main",
        revision="main",
        access=WorkspaceAccess.WRITABLE,
    )


def _provision_data(
    *,
    operation_id: str = "provision-op",
    workspace_id: str = "workspace-one",
    request_id: str = "create-request",
) -> dict:
    return provisioned_data(
        operation_id=operation_id,
        workspace_id=workspace_id,
        creation_request_id=request_id,
        source_id="source-main",
        source_revision="main",
        repository_fingerprint=_REPOSITORY_FINGERPRINT,
        base_revision=_BASE_REVISION,
        access=WorkspaceAccess.WRITABLE,
        owner_agent_id="owner-agent",
    )


def _event(
    seq: int,
    event_type: str,
    data: dict,
    *,
    stream_id: str = WORKSPACE_CATALOG_STREAM,
    schema_version: int = 1,
) -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid4(),
        stream_id=stream_id,
        seq=seq,
        type=event_type,
        schema_version=schema_version,
        data=data,
        occurred_at=datetime.now(UTC),
    )


class _MemoryProvider:
    def __init__(self, root: Path) -> None:
        self._root = root
        self.states: dict[str, WorkspaceLocalState] = {}
        self.remove_calls = 0

    @property
    def managed_root(self) -> Path:
        return self._root

    async def resolve_source(
        self, source_id: str, revision: str
    ) -> WorkspaceSourceSnapshot:
        return WorkspaceSourceSnapshot(
            source_id=source_id,
            requested_revision=revision,
            repository_fingerprint=_REPOSITORY_FINGERPRINT,
            base_revision=_BASE_REVISION,
        )

    async def materialize(self, record: WorkspaceRecord) -> WorkspaceHandle:
        root = self._root / record.workspace_id
        root.mkdir(parents=True, exist_ok=True)
        self.states.setdefault(record.workspace_id, WorkspaceLocalState.CLEAN)
        return WorkspaceHandle(
            workspace_id=record.workspace_id,
            root=root,
            source_id=record.source_id,
            base_revision=record.base_revision,
            access=record.access,
            status=record.status,
            owner_agent_id=record.owner_agent_id,
            agent_id=record.agent_id,
            session_id=record.session_id,
        )

    async def inspect(self, record: WorkspaceRecord) -> WorkspaceLocalState:
        return self.states.get(record.workspace_id, WorkspaceLocalState.MISSING)

    async def remove(self, record: WorkspaceRecord) -> None:
        self.remove_calls += 1
        state = await self.inspect(record)
        if state is WorkspaceLocalState.DIRTY:
            raise WorkspaceDirtyError
        if state is WorkspaceLocalState.UNSAFE:
            raise RuntimeError("unsafe fixture")
        self.states[record.workspace_id] = WorkspaceLocalState.MISSING
        root = self._root / record.workspace_id
        if root.exists():
            root.rmdir()


class _GatedRemoveProvider(_MemoryProvider):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.remove_entered = asyncio.Event()
        self.remove_gate = asyncio.Event()

    async def remove(self, record: WorkspaceRecord) -> None:
        self.remove_entered.set()
        await self.remove_gate.wait()
        await super().remove(record)


class _MismatchedSourceProvider(_MemoryProvider):
    async def resolve_source(
        self, source_id: str, revision: str
    ) -> WorkspaceSourceSnapshot:
        del source_id
        return WorkspaceSourceSnapshot(
            source_id="different-source",
            requested_revision=revision,
            repository_fingerprint=_REPOSITORY_FINGERPRINT,
            base_revision=_BASE_REVISION,
        )


class _MismatchedHandleProvider(_MemoryProvider):
    def __init__(self, root: Path, wrong_root: Path) -> None:
        super().__init__(root)
        self.wrong_root = wrong_root

    async def materialize(self, record: WorkspaceRecord) -> WorkspaceHandle:
        handle = await super().materialize(record)
        return WorkspaceHandle(
            workspace_id=handle.workspace_id,
            root=self.wrong_root,
            source_id=handle.source_id,
            base_revision=handle.base_revision,
            access=handle.access,
            status=handle.status,
            owner_agent_id=handle.owner_agent_id,
            agent_id=handle.agent_id,
            session_id=handle.session_id,
        )


class _CommitThenCancelStore:
    def __init__(self) -> None:
        self.inner = InMemoryEventStore()
        self.cancel_workspace_append = True

    async def append(
        self,
        stream_id: str,
        *,
        expected_seq: int,
        events: tuple[PendingEvent, ...],
        durability: Durability = Durability.SYNC,
    ):
        result = await self.inner.append(
            stream_id,
            expected_seq=expected_seq,
            events=events,
            durability=durability,
        )
        if stream_id == WORKSPACE_CATALOG_STREAM and self.cancel_workspace_append:
            self.cancel_workspace_append = False
            raise asyncio.CancelledError
        return result

    async def read(self, stream_id: str, *, from_seq: int = 1):
        return await self.inner.read(stream_id, from_seq=from_seq)

    async def head(self, stream_id: str) -> int:
        return await self.inner.head(stream_id)

    async def list_streams(self, *, prefix: str | None = None):
        return await self.inner.list_streams(prefix=prefix)


class _CancelReleaseStore(_CommitThenCancelStore):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_workspace_append = False

    async def append(
        self,
        stream_id: str,
        *,
        expected_seq: int,
        events: tuple[PendingEvent, ...],
        durability: Durability = Durability.SYNC,
    ):
        result = await self.inner.append(
            stream_id,
            expected_seq=expected_seq,
            events=events,
            durability=durability,
        )
        if events and events[0].type == WORKSPACE_RELEASED:
            raise asyncio.CancelledError
        return result


class _FailingRemoveProvider(_MemoryProvider):
    async def remove(self, record: WorkspaceRecord) -> None:
        del record
        raise RuntimeError("fixture remove failure")


def test_catalog_rebuilds_the_complete_lifecycle() -> None:
    provision = _provision_data()
    attach = attached_data(
        operation_id="attach-op",
        workspace_id="workspace-one",
        agent_id="child-agent",
        session_id="child-session",
    )
    quarantine = lifecycle_data(
        operation_id="quarantine-op",
        workspace_id="workspace-one",
        reason="git-state-unknown",
        allowed_reasons=QUARANTINE_REASONS,
    )
    reattach = attached_data(
        operation_id="reattach-op",
        workspace_id="workspace-one",
        agent_id="child-agent",
        session_id="child-session",
    )
    release = lifecycle_data(
        operation_id="release-op",
        workspace_id="workspace-one",
        reason="explicit-release",
        allowed_reasons=RELEASE_REASONS,
    )
    catalog = WorkspaceCatalog.rebuild(
        (
            _event(1, WORKSPACE_PROVISIONED, provision),
            _event(2, WORKSPACE_ATTACHED, attach),
            _event(3, WORKSPACE_QUARANTINED, quarantine),
            _event(4, WORKSPACE_ATTACHED, reattach),
            _event(5, WORKSPACE_RELEASED, release),
        )
    )

    record = catalog.get("workspace-one")
    assert record is not None
    assert record.status is WorkspaceStatus.RELEASED
    assert record.agent_id == "child-agent"
    assert record.session_id == "child-session"
    assert record.reason == "explicit-release"
    assert catalog.for_request("create-request") == record
    assert catalog.for_agent("child-agent") == record
    assert catalog.for_session("child-session") == record
    assert catalog.head_seq == 5


@pytest.mark.parametrize(
    ("events", "code"),
    (
        (
            (_event(2, WORKSPACE_PROVISIONED, _provision_data()),),
            "workspace-sequence-invalid",
        ),
        (
            (
                _event(
                    1,
                    WORKSPACE_PROVISIONED,
                    _provision_data(),
                    stream_id="workspace:other",
                ),
            ),
            "workspace-stream-unexpected",
        ),
        (
            (
                _event(
                    1,
                    WORKSPACE_PROVISIONED,
                    _provision_data(),
                    schema_version=2,
                ),
            ),
            "workspace-schema-version-unsupported",
        ),
        (
            (
                _event(
                    1,
                    WORKSPACE_ATTACHED,
                    attached_data(
                        operation_id="attach-op",
                        workspace_id="missing-workspace",
                        agent_id="child-agent",
                        session_id="child-session",
                    ),
                ),
            ),
            "workspace-unknown",
        ),
        (
            (
                _event(1, WORKSPACE_PROVISIONED, _provision_data()),
                _event(
                    2,
                    WORKSPACE_PROVISIONED,
                    _provision_data(operation_id="other-op"),
                ),
            ),
            "workspace-id-duplicate",
        ),
    ),
)
def test_catalog_rejects_malformed_or_contradictory_history(
    events: tuple[EventEnvelope, ...], code: str
) -> None:
    with pytest.raises(WorkspaceProtocolError) as raised:
        WorkspaceCatalog.rebuild(events)
    assert raised.value.code == code


async def test_catalog_validation_returns_one_stable_issue() -> None:
    store = InMemoryEventStore()
    data = _provision_data()
    data.pop("access")
    await store.append(
        WORKSPACE_CATALOG_STREAM,
        expected_seq=0,
        events=(PendingEvent(type=WORKSPACE_PROVISIONED, data=data),),
    )
    issues = await validate_workspace_catalog_events(store)
    assert tuple((issue.code, issue.seq) for issue in issues) == (
        ("workspace-payload-keys-unexpected", 1),
    )


def test_public_workspace_parser_normalizes_hostile_envelope_reads() -> None:
    event = _event(1, WORKSPACE_PROVISIONED, _provision_data())
    object.__setattr__(event, "stream_id", _HostileString(WORKSPACE_CATALOG_STREAM))
    with pytest.raises(WorkspaceProtocolError) as raised:
        parse_workspace_fact(event)
    assert raised.value.code == "workspace-payload-invalid"
    assert raised.value.seq == 1


def test_workspace_parser_does_not_swallow_process_interrupts() -> None:
    class _InterruptingString(_HostileString):
        failure = KeyboardInterrupt

    event = _event(1, WORKSPACE_PROVISIONED, _provision_data())
    object.__setattr__(
        event, "stream_id", _InterruptingString(WORKSPACE_CATALOG_STREAM)
    )
    with pytest.raises(KeyboardInterrupt):
        parse_workspace_fact(event)


def test_catalog_reports_an_unreadable_sequence_at_its_expected_position() -> None:
    with pytest.raises(WorkspaceProtocolError) as raised:
        WorkspaceCatalog.rebuild((_HostileSequenceEvent(),))
    assert raised.value.code == "workspace-payload-invalid"
    assert raised.value.seq == 1


def test_catalog_freezes_event_type_before_operation_matching() -> None:
    event_type = _DelayedHostileEventType(WORKSPACE_PROVISIONED)
    data = _provision_data()
    catalog = WorkspaceCatalog.rebuild((_event(1, event_type, data),))

    assert catalog.operation_matches("provision-op", WORKSPACE_PROVISIONED, data)
    assert event_type.comparisons == 1


def test_public_identifier_validation_does_not_leak_subclass_code() -> None:
    with pytest.raises(WorkspaceInputError) as raised:
        require_workspace_identifier(
            _HostileIdentifier("workspace-one"), field="workspace_id"
        )
    assert raised.value.code == "workspace-identity-invalid"


def test_operation_identity_rejects_unencodable_parts_with_a_stable_error() -> None:
    with pytest.raises(WorkspaceInputError) as raised:
        workspace_operation_id("provision", hostile={object()})
    assert raised.value.code == "workspace-operation-input-invalid"


async def test_service_attaches_only_an_exact_directory_and_session_binding(
    tmp_path: Path,
) -> None:
    store = InMemoryEventStore()
    provider = _MemoryProvider(tmp_path / "managed")
    service = WorkspaceService(store, provider)
    operation_id = "provision-op"
    handle = await service.provision(
        operation_id=operation_id,
        creation_request_id="create-request",
        request=_request(),
        owner_agent_id=None,
    )
    session_id = await SessionService(store).create_session(handle.root)
    durable = await AgentRegistrar(store).create_agent(
        AgentSpec(preset="coder", workspace_id=handle.workspace_id),
        request_id="create-request",
        agent_id="child-agent",
        session_id=session_id,
    )

    record = await service.finish_agent_creation(
        provision_operation_id=operation_id,
        primary=None,
    )
    assert record is not None
    assert record.status is WorkspaceStatus.ATTACHED
    assert record.agent_id == durable.agent_id
    resolved = await service.resolve_for_agent(durable.agent_id)
    assert resolved.root == handle.root
    assert await service.resolve_for_session(session_id) == resolved

    fresh = await WorkspaceCatalogReader(store).load()
    assert fresh.get(handle.workspace_id) == record


async def test_provider_cannot_substitute_another_source_identity(
    tmp_path: Path,
) -> None:
    service = WorkspaceService(
        InMemoryEventStore(), _MismatchedSourceProvider(tmp_path / "managed")
    )
    with pytest.raises(WorkspaceSourceError):
        await service.provision(
            operation_id="provision-op",
            creation_request_id="create-request",
            request=_request(),
            owner_agent_id=None,
        )
    assert (await service.catalog()).workspaces == ()


async def test_provider_cannot_substitute_an_uncatalogued_handle_path(
    tmp_path: Path,
) -> None:
    store = InMemoryEventStore()
    outside = tmp_path / "outside"
    outside.mkdir()
    service = WorkspaceService(
        store,
        _MismatchedHandleProvider(tmp_path / "managed", outside),
    )
    with pytest.raises(WorkspacePathError):
        await service.provision(
            operation_id="provision-op",
            creation_request_id="create-request",
            request=_request(),
            owner_agent_id=None,
        )
    record = (await service.catalog()).for_operation("provision-op")
    assert record is not None
    assert record.status is WorkspaceStatus.RELEASED


async def test_absent_agent_releases_a_clean_provisional_workspace(
    tmp_path: Path,
) -> None:
    store = InMemoryEventStore()
    provider = _MemoryProvider(tmp_path / "managed")
    service = WorkspaceService(store, provider)
    await service.provision(
        operation_id="provision-op",
        creation_request_id="create-request",
        request=_request(),
        owner_agent_id=None,
    )

    record = await service.finish_agent_creation(
        provision_operation_id="provision-op",
        primary=None,
    )
    assert record is not None
    assert record.status is WorkspaceStatus.RELEASED
    assert record.reason == "agent-not-created"
    assert provider.remove_calls == 1

    with pytest.raises(WorkspaceStateError):
        await service.provision(
            operation_id="provision-op",
            creation_request_id="create-request",
            request=_request(),
            owner_agent_id=None,
        )


async def test_dirty_unattached_workspace_is_quarantined_and_never_deleted(
    tmp_path: Path,
) -> None:
    store = InMemoryEventStore()
    provider = _MemoryProvider(tmp_path / "managed")
    service = WorkspaceService(store, provider)
    handle = await service.provision(
        operation_id="provision-op",
        creation_request_id="create-request",
        request=_request(),
        owner_agent_id=None,
    )
    provider.states[handle.workspace_id] = WorkspaceLocalState.DIRTY

    with pytest.raises(WorkspaceDirtyError):
        await service.finish_agent_creation(
            provision_operation_id="provision-op",
            primary=None,
        )
    record = (await service.catalog()).get(handle.workspace_id)
    assert record is not None
    assert record.status is WorkspaceStatus.QUARANTINED
    assert handle.root.exists()

    with pytest.raises(WorkspaceQuarantinedError):
        await service.provision(
            operation_id="provision-op",
            creation_request_id="create-request",
            request=_request(),
            owner_agent_id=None,
        )


async def test_session_path_mismatch_quarantines_instead_of_attaching(
    tmp_path: Path,
) -> None:
    store = InMemoryEventStore()
    provider = _MemoryProvider(tmp_path / "managed")
    service = WorkspaceService(store, provider)
    handle = await service.provision(
        operation_id="provision-op",
        creation_request_id="create-request",
        request=_request(),
        owner_agent_id=None,
    )
    other = tmp_path / "other"
    other.mkdir()
    session_id = await SessionService(store).create_session(other)
    await AgentRegistrar(store).create_agent(
        AgentSpec(preset="coder", workspace_id=handle.workspace_id),
        request_id="create-request",
        agent_id="child-agent",
        session_id=session_id,
    )

    with pytest.raises(WorkspaceSessionMismatchError):
        await service.finish_agent_creation(
            provision_operation_id="provision-op",
            primary=None,
        )
    record = (await service.catalog()).get(handle.workspace_id)
    assert record is not None
    assert record.status is WorkspaceStatus.QUARANTINED


async def test_same_operation_with_different_request_is_rejected_before_git(
    tmp_path: Path,
) -> None:
    service = WorkspaceService(
        InMemoryEventStore(), _MemoryProvider(tmp_path / "managed")
    )
    await service.provision(
        operation_id="provision-op",
        creation_request_id="create-request",
        request=_request(),
        owner_agent_id=None,
    )
    with pytest.raises(WorkspaceOperationConflictError):
        await service.provision(
            operation_id="provision-op",
            creation_request_id="different-request",
            request=_request(),
            owner_agent_id=None,
        )


async def test_cancelled_committed_provision_can_be_reconciled_and_released(
    tmp_path: Path,
) -> None:
    store = _CommitThenCancelStore()
    provider = _MemoryProvider(tmp_path / "managed")
    service = WorkspaceService(store, provider)
    operation_id = workspace_operation_id(
        "provision", creation_request_id="create-request", owner_agent_id=None
    )
    with pytest.raises(asyncio.CancelledError):
        await service.provision(
            operation_id=operation_id,
            creation_request_id="create-request",
            request=_request(),
            owner_agent_id=None,
        )

    record = (await service.catalog()).for_operation(operation_id)
    assert record is not None
    assert record.status is WorkspaceStatus.RELEASED


async def test_different_directory_request_cannot_adopt_a_workspace(
    tmp_path: Path,
) -> None:
    store = InMemoryEventStore()
    provider = _MemoryProvider(tmp_path / "managed")
    service = WorkspaceService(store, provider)
    handle = await service.provision(
        operation_id="provision-op",
        creation_request_id="create-request",
        request=_request(),
        owner_agent_id=None,
    )
    session_id = await SessionService(store).create_session(handle.root)
    await AgentRegistrar(store).create_agent(
        AgentSpec(preset="coder", workspace_id=handle.workspace_id),
        request_id="different-request",
        agent_id="different-agent",
        session_id=session_id,
    )
    with pytest.raises(WorkspaceDirectoryMismatchError):
        await service.finish_agent_creation(
            provision_operation_id="provision-op", primary=None
        )
    record = (await service.catalog()).get(handle.workspace_id)
    assert record is not None
    assert record.status is WorkspaceStatus.QUARANTINED


async def test_exact_attachment_can_reconcile_after_quarantine(
    tmp_path: Path,
) -> None:
    store = InMemoryEventStore()
    provider = _MemoryProvider(tmp_path / "managed")
    service = WorkspaceService(store, provider)
    handle = await service.provision(
        operation_id="provision-op",
        creation_request_id="create-request",
        request=_request(),
        owner_agent_id=None,
    )
    session_id = await SessionService(store).create_session(handle.root)
    await AgentRegistrar(store).create_agent(
        AgentSpec(preset="coder", workspace_id=handle.workspace_id),
        request_id="create-request",
        agent_id="child-agent",
        session_id=session_id,
    )
    attached = await service.finish_agent_creation(
        provision_operation_id="provision-op", primary=None
    )
    assert attached is not None
    await store.append(
        WORKSPACE_CATALOG_STREAM,
        expected_seq=attached.updated_seq,
        events=(
            PendingEvent(
                type=WORKSPACE_QUARANTINED,
                data=lifecycle_data(
                    operation_id="quarantine-op",
                    workspace_id=handle.workspace_id,
                    reason="git-state-unknown",
                    allowed_reasons=QUARANTINE_REASONS,
                ),
            ),
        ),
    )

    reconciled = await service.finish_agent_creation(
        provision_operation_id="provision-op", primary=None
    )
    assert reconciled is not None
    assert reconciled.status is WorkspaceStatus.ATTACHED
    assert reconciled.agent_id == "child-agent"
    assert reconciled.updated_seq == attached.updated_seq + 2


async def test_reconcile_quarantines_an_attached_workspace_that_became_unsafe(
    tmp_path: Path,
) -> None:
    store = InMemoryEventStore()
    provider = _MemoryProvider(tmp_path / "managed")
    service = WorkspaceService(store, provider)
    handle = await service.provision(
        operation_id="provision-op",
        creation_request_id="create-request",
        request=_request(),
        owner_agent_id=None,
    )
    session_id = await SessionService(store).create_session(handle.root)
    await AgentRegistrar(store).create_agent(
        AgentSpec(preset="coder", workspace_id=handle.workspace_id),
        request_id="create-request",
        agent_id="child-agent",
        session_id=session_id,
    )
    await service.finish_agent_creation(
        provision_operation_id="provision-op", primary=None
    )
    provider.states[handle.workspace_id] = WorkspaceLocalState.UNSAFE

    with pytest.raises(WorkspacePathError):
        await service.reconcile(handle.workspace_id)
    record = (await service.catalog()).get(handle.workspace_id)
    assert record is not None
    assert record.status is WorkspaceStatus.QUARANTINED


def test_quarantined_attachment_cannot_change_agent_or_session() -> None:
    events = (
        _event(1, WORKSPACE_PROVISIONED, _provision_data()),
        _event(
            2,
            WORKSPACE_ATTACHED,
            attached_data(
                operation_id="attach-one",
                workspace_id="workspace-one",
                agent_id="agent-one",
                session_id="session-one",
            ),
        ),
        _event(
            3,
            WORKSPACE_QUARANTINED,
            lifecycle_data(
                operation_id="quarantine-op",
                workspace_id="workspace-one",
                reason="git-state-unknown",
                allowed_reasons=QUARANTINE_REASONS,
            ),
        ),
        _event(
            4,
            WORKSPACE_ATTACHED,
            attached_data(
                operation_id="attach-two",
                workspace_id="workspace-one",
                agent_id="agent-two",
                session_id="session-two",
            ),
        ),
    )
    with pytest.raises(WorkspaceProtocolError) as raised:
        WorkspaceCatalog.rebuild(events)
    assert raised.value.code == "workspace-state-invalid"
    assert raised.value.seq == 4


async def test_release_absorbs_repeated_cancellation_and_records_terminal_state(
    tmp_path: Path,
) -> None:
    store = InMemoryEventStore()
    provider = _GatedRemoveProvider(tmp_path / "managed")
    service = WorkspaceService(store, provider)
    handle = await service.provision(
        operation_id="provision-op",
        creation_request_id="create-request",
        request=_request(),
        owner_agent_id=None,
    )

    releasing = asyncio.create_task(service.release(handle.workspace_id))
    await provider.remove_entered.wait()
    releasing.cancel()
    releasing.cancel()
    releasing.cancel()
    assert not releasing.done()
    provider.remove_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await releasing

    record = (await service.catalog()).get(handle.workspace_id)
    assert record is not None
    assert record.status is WorkspaceStatus.RELEASED
    assert record.reason == "explicit-release"
    with pytest.raises(WorkspaceOperationConflictError):
        await service.release(handle.workspace_id, reason="rejected")


async def test_release_append_commit_then_cancel_leaves_one_terminal_fact(
    tmp_path: Path,
) -> None:
    store = _CancelReleaseStore()
    provider = _MemoryProvider(tmp_path / "managed")
    service = WorkspaceService(store, provider)
    handle = await service.provision(
        operation_id="provision-op",
        creation_request_id="create-request",
        request=_request(),
        owner_agent_id=None,
    )
    with pytest.raises(asyncio.CancelledError):
        await service.release(handle.workspace_id)
    record = (await service.catalog()).get(handle.workspace_id)
    assert record is not None
    assert record.status is WorkspaceStatus.RELEASED
    assert provider.remove_calls == 1
    events = await store.read(WORKSPACE_CATALOG_STREAM)
    assert sum(event.type == WORKSPACE_RELEASED for event in events) == 1


async def test_failed_remove_quarantines_without_claiming_release(
    tmp_path: Path,
) -> None:
    store = InMemoryEventStore()
    provider = _FailingRemoveProvider(tmp_path / "managed")
    service = WorkspaceService(store, provider)
    handle = await service.provision(
        operation_id="provision-op",
        creation_request_id="create-request",
        request=_request(),
        owner_agent_id=None,
    )
    with pytest.raises(RuntimeError, match="fixture remove failure"):
        await service.release(handle.workspace_id)
    record = (await service.catalog()).get(handle.workspace_id)
    assert record is not None
    assert record.status is WorkspaceStatus.QUARANTINED
    assert record.reason == "git-state-unknown"
