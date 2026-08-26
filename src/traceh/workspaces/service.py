"""Host-owned workspace lifecycle, reconciliation and path resolution."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from traceh.agents.commit_reconciliation import committed_after_failure
from traceh.agents.directory import AgentDirectoryReader
from traceh.api.events import EventEnvelope, PendingEvent
from traceh.api.json_types import JsonValue, fingerprint
from traceh.api.workspaces import (
    WorkspaceHandle,
    WorkspaceLocalState,
    WorkspaceProvider,
    WorkspaceProvisioningRequest,
    WorkspaceRecord,
    WorkspaceSourceSnapshot,
    WorkspaceStatus,
)
from traceh.concurrency import await_worker_convergence
from traceh.session.event_store import ConcurrencyConflict, Durability, EventStore
from traceh.session.service import SessionService
from traceh.workspaces.catalog import WorkspaceCatalog, WorkspaceCatalogReader
from traceh.workspaces.errors import (
    WorkspaceCatalogConflictError,
    WorkspaceDirectoryMismatchError,
    WorkspaceDirtyError,
    WorkspaceInputError,
    WorkspaceNotFoundError,
    WorkspaceOperationConflictError,
    WorkspacePathError,
    WorkspaceQuarantinedError,
    WorkspaceSessionMismatchError,
    WorkspaceSourceError,
    WorkspaceStateError,
    WorkspaceWriteError,
)
from traceh.workspaces.events import (
    QUARANTINE_REASONS,
    RELEASE_REASONS,
    WORKSPACE_ATTACHED,
    WORKSPACE_CATALOG_STREAM,
    WORKSPACE_PROVISIONED,
    WORKSPACE_QUARANTINED,
    WORKSPACE_RELEASED,
    WORKSPACE_SCHEMA_VERSION,
    attached_data,
    freeze_provisioning_request,
    is_workspace_fact,
    lifecycle_data,
    provisioned_data,
    require_workspace_identifier,
)


def workspace_operation_id(purpose: str, **parts: object) -> str:
    """Build one stable, generic workspace operation identity."""

    purpose = require_workspace_identifier(purpose, field="purpose")
    try:
        digest = fingerprint(parts)
    except Exception:
        raise WorkspaceInputError(
            "workspace-operation-input-invalid", "parts"
        ) from None
    return f"ws-{purpose}-{digest}"


async def converge_workspace_operation(coro, *, name: str) -> object:
    task = asyncio.create_task(coro, name=name)
    cancellation: asyncio.CancelledError | None = None
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as error:
        cancellation = error
        await await_worker_convergence(task)
    if task.cancelled():
        assert cancellation is not None
        raise cancellation
    failure = task.exception()
    if failure is not None:
        if cancellation is not None:
            raise cancellation from failure
        raise failure
    if cancellation is not None:
        raise cancellation
    return task.result()


class WorkspaceService:
    """Own the catalog and coordinate its exact local Git worktree.

    The service intentionally does not own Agent Activations.  Its only
    cross-domain operation is reconciliation against fresh Directory and
    Session facts after the existing Supervisor's create operation converges.
    """

    __slots__ = (
        "_catalogs",
        "_directory",
        "_lock",
        "_managed_root",
        "_provider",
        "_sessions",
        "_store",
    )

    def __init__(self, store: EventStore, provider: WorkspaceProvider) -> None:
        try:
            managed_root = Path(provider.managed_root)
        except Exception:
            raise WorkspaceInputError(
                "workspace-path-invalid", "managed_root"
            ) from None
        if not managed_root.is_absolute():
            raise WorkspaceInputError("workspace-path-invalid", "managed_root")
        self._store = store
        self._provider = provider
        self._managed_root = managed_root.absolute()
        self._catalogs = WorkspaceCatalogReader(store)
        self._directory = AgentDirectoryReader(store)
        self._sessions = SessionService(store)
        # One explicit service instance is the process-local Git mutation
        # coordinator. The EventStore CAS remains the durable concurrency gate.
        self._lock = asyncio.Lock()

    @property
    def store(self) -> EventStore:
        return self._store

    @property
    def managed_root(self) -> Path:
        return self._managed_root

    async def catalog(self) -> WorkspaceCatalog:
        return await self._catalogs.load()

    async def resolve_for_creation(self, workspace_id: str) -> WorkspaceHandle:
        """Return the exact provisional worktree during the managed create saga.

        This seam exists for an ``AgentActivationFactory`` invoked *inside*
        :class:`WorkspaceManagedAgentSupervisor.create`.  It never adopts an
        arbitrary path: the id must already name a catalogued provisional
        workspace and the provider re-validates/materializes that exact record.
        Attached workspaces continue to use ``resolve_for_agent/session``.
        """

        workspace_id = require_workspace_identifier(
            workspace_id, field="workspace_id"
        )
        catalog = await self._catalogs.load()
        record = catalog.get(workspace_id)
        if record is None:
            raise WorkspaceNotFoundError
        if record.status is not WorkspaceStatus.PROVISIONAL:
            raise WorkspaceStateError
        return await self._provider.materialize(record)

    async def provision(
        self,
        *,
        operation_id: str,
        creation_request_id: str,
        request: WorkspaceProvisioningRequest,
        owner_agent_id: str | None,
    ) -> WorkspaceHandle:
        operation_id = require_workspace_identifier(
            operation_id, field="operation_id"
        )
        creation_request_id = require_workspace_identifier(
            creation_request_id, field="creation_request_id"
        )
        frozen = freeze_provisioning_request(request)
        owner_agent_id = (
            None
            if owner_agent_id is None
            else require_workspace_identifier(
                owner_agent_id, field="owner_agent_id"
            )
        )
        entered = False
        try:
            async with self._lock:
                entered = True
                catalog = await self._catalogs.load()
                existing = catalog.for_operation(operation_id)
                if existing is not None:
                    self._require_same_provision(
                        existing,
                        creation_request_id=creation_request_id,
                        request=frozen,
                        owner_agent_id=owner_agent_id,
                    )
                    record = existing
                else:
                    if catalog.operation_exists(operation_id):
                        raise WorkspaceOperationConflictError
                    if catalog.for_request(creation_request_id) is not None:
                        raise WorkspaceOperationConflictError
                    snapshot = self._require_source_snapshot(
                        await self._provider.resolve_source(
                            frozen.source_id, frozen.revision
                        ),
                        frozen,
                    )
                    workspace_id = workspace_operation_id(
                        "workspace",
                        operation_id=operation_id,
                        creation_request_id=creation_request_id,
                    )
                    data = provisioned_data(
                        operation_id=operation_id,
                        workspace_id=workspace_id,
                        creation_request_id=creation_request_id,
                        source_id=snapshot.source_id,
                        source_revision=snapshot.requested_revision,
                        repository_fingerprint=snapshot.repository_fingerprint,
                        base_revision=snapshot.base_revision,
                        access=frozen.access,
                        owner_agent_id=owner_agent_id,
                    )
                    await self._append(
                        expected_seq=catalog.head_seq,
                        event_type=WORKSPACE_PROVISIONED,
                        data=data,
                    )
                    record = (await self._catalogs.load()).for_operation(operation_id)
                    if record is None:
                        raise WorkspaceWriteError(committed=None)

                if record.status is WorkspaceStatus.RELEASED:
                    raise WorkspaceStateError
                if record.status is WorkspaceStatus.QUARANTINED:
                    raise WorkspaceQuarantinedError
                try:
                    handle = await self._provider.materialize(record)
                    self._require_handle(record, handle)
                    return handle
                except BaseException as primary:
                    try:
                        await converge_workspace_operation(
                            self._settle_provision_failure_locked(record),
                            name="traceh-workspace-provision-rollback",
                        )
                    except BaseException as cleanup_error:
                        if isinstance(primary, asyncio.CancelledError):
                            raise primary from cleanup_error
                        raise BaseExceptionGroup(
                            "workspace provision and rollback both failed",
                            (primary, cleanup_error),
                        ) from None
                    raise
        except BaseException as primary:
            if entered:
                try:
                    await converge_workspace_operation(
                        self._settle_provision_operation(operation_id),
                        name="traceh-workspace-provision-finalize",
                    )
                except BaseException as cleanup_error:
                    if isinstance(primary, asyncio.CancelledError):
                        raise primary from cleanup_error
                    raise BaseExceptionGroup(
                        "workspace provision and final reconciliation both failed",
                        (primary, cleanup_error),
                    ) from None
            raise

    async def _settle_provision_operation(self, operation_id: str) -> None:
        async with self._lock:
            catalog = await self._catalogs.load()
            record = catalog.for_operation(operation_id)
            if record is None or record.status in {
                WorkspaceStatus.QUARANTINED,
                WorkspaceStatus.RELEASED,
            }:
                return
            await self._settle_provision_failure_locked(record)

    async def finish_agent_creation(
        self,
        *,
        provision_operation_id: str,
        primary: BaseException | None,
    ) -> WorkspaceRecord | None:
        """Attach, release or quarantine after Supervisor create converges.

        ``primary`` is preserved.  A cleanup/reconciliation failure is grouped
        with an ordinary primary error, or chained behind cancellation, so no
        failure is hidden and cancellation never returns before Git converges.
        """

        provision_operation_id = require_workspace_identifier(
            provision_operation_id, field="provision_operation_id"
        )
        try:
            result = await converge_workspace_operation(
                self._finish_agent_creation(provision_operation_id),
                name="traceh-workspace-agent-create-reconcile",
            )
        except BaseException as final_error:
            if primary is None:
                raise
            if isinstance(primary, asyncio.CancelledError):
                raise primary from final_error
            raise BaseExceptionGroup(
                "Agent creation and workspace reconciliation both failed",
                (primary, final_error),
            ) from None
        if primary is not None:
            raise primary
        assert result is None or isinstance(result, WorkspaceRecord)
        return result

    async def _finish_agent_creation(
        self, provision_operation_id: str
    ) -> WorkspaceRecord | None:
        async with self._lock:
            return await self._finish_agent_creation_locked(
                provision_operation_id
            )

    async def _finish_agent_creation_locked(
        self, provision_operation_id: str
    ) -> WorkspaceRecord | None:
        catalog = await self._catalogs.load()
        record = catalog.for_operation(provision_operation_id)
        if record is None:
            return None
        try:
            directory = await self._directory.load()
        except Exception:
            await self._quarantine_locked(record, "agent-identity-unknown")
            raise WorkspaceDirectoryMismatchError from None

        durable = directory.for_request(record.creation_request_id)
        if durable is not None:
            exact = (
                durable.workspace_id == record.workspace_id
                and durable.owner_agent_id == record.owner_agent_id
            )
            if not exact or record.status is WorkspaceStatus.RELEASED:
                await self._quarantine_locked(record, "agent-identity-conflict")
                raise WorkspaceDirectoryMismatchError
            try:
                workspace = await self._sessions.workspace_for(durable.session_id)
            except Exception:
                await self._quarantine_locked(
                    record, "session-workspace-mismatch"
                )
                raise WorkspaceSessionMismatchError from None
            expected_root = self._managed_root / record.workspace_id
            if not _same_path(workspace, expected_root):
                await self._quarantine_locked(
                    record, "session-workspace-mismatch"
                )
                raise WorkspaceSessionMismatchError
            local = await self._safe_inspect(record)
            if local not in {
                WorkspaceLocalState.CLEAN,
                WorkspaceLocalState.DIRTY,
            }:
                await self._quarantine_locked(record, "git-state-unknown")
                raise WorkspacePathError
            if record.status is WorkspaceStatus.ATTACHED:
                if (
                    record.agent_id != durable.agent_id
                    or record.session_id != durable.session_id
                ):
                    raise WorkspaceDirectoryMismatchError
                return record
            try:
                return await self._attach_locked(
                    record, durable.agent_id, durable.session_id
                )
            except WorkspaceWriteError:
                await self._quarantine_locked(
                    record, "workspace-write-unknown"
                )
                raise

        if any(
            candidate.workspace_id == record.workspace_id
            for candidate in directory.records
        ):
            await self._quarantine_locked(record, "agent-identity-conflict")
            raise WorkspaceDirectoryMismatchError
        if record.status is WorkspaceStatus.ATTACHED:
            await self._quarantine_locked(record, "agent-identity-conflict")
            raise WorkspaceDirectoryMismatchError
        if record.status is WorkspaceStatus.RELEASED:
            return record
        try:
            await self._provider.remove(record)
        except WorkspaceDirtyError:
            await self._quarantine_locked(record, "workspace-dirty")
            raise
        except BaseException:
            await self._quarantine_locked(record, "git-state-unknown")
            raise
        return await self._release_locked(record, "agent-not-created")

    async def _settle_provision_failure_locked(
        self, record: WorkspaceRecord
    ) -> None:
        local = await self._safe_inspect(record)
        if local is WorkspaceLocalState.UNSAFE:
            await self._quarantine_locked(record, "path-unsafe")
            return
        if local is WorkspaceLocalState.DIRTY:
            await self._quarantine_locked(record, "workspace-dirty")
            return
        await self._finish_agent_creation_locked(record.provision_operation_id)

    async def resolve_for_agent(self, agent_id: str) -> WorkspaceHandle:
        """Resolve one attached Agent from catalog, Directory and Session facts."""

        agent_id = require_workspace_identifier(agent_id, field="agent_id")
        # Read the dependent catalog first. Attachment is appended only after
        # Directory and Session facts, so the subsequent prerequisite reads
        # cannot be older than the catalog prefix being validated.
        catalog = await self._catalogs.load()
        record = catalog.for_agent(agent_id)
        if record is None:
            raise WorkspaceNotFoundError
        if record.status is not WorkspaceStatus.ATTACHED:
            raise WorkspaceStateError
        directory = await self._directory.load()
        durable = directory.get(agent_id)
        if (
            durable is None
            or durable.workspace_id != record.workspace_id
            or durable.session_id != record.session_id
            or durable.owner_agent_id != record.owner_agent_id
        ):
            raise WorkspaceDirectoryMismatchError
        try:
            session_workspace = await self._sessions.workspace_for(durable.session_id)
        except Exception:
            raise WorkspaceSessionMismatchError from None
        expected = self._managed_root / record.workspace_id
        if not _same_path(session_workspace, expected):
            raise WorkspaceSessionMismatchError
        state = await self._safe_inspect(record)
        if state not in {WorkspaceLocalState.CLEAN, WorkspaceLocalState.DIRTY}:
            raise WorkspacePathError
        return WorkspaceHandle(
            workspace_id=record.workspace_id,
            root=expected,
            source_id=record.source_id,
            base_revision=record.base_revision,
            access=record.access,
            status=record.status,
            owner_agent_id=record.owner_agent_id,
            agent_id=record.agent_id,
            session_id=record.session_id,
        )

    async def resolve_for_session(self, session_id: str) -> WorkspaceHandle:
        session_id = require_workspace_identifier(session_id, field="session_id")
        catalog = await self._catalogs.load()
        record = catalog.for_session(session_id)
        if record is None or record.agent_id is None:
            raise WorkspaceNotFoundError
        return await self.resolve_for_agent(record.agent_id)

    async def reconcile(self, workspace_id: str) -> WorkspaceRecord:
        workspace_id = require_workspace_identifier(
            workspace_id, field="workspace_id"
        )
        catalog = await self._catalogs.load()
        record = catalog.get(workspace_id)
        if record is None:
            raise WorkspaceNotFoundError
        if record.status is WorkspaceStatus.RELEASED:
            return record
        result = await self.finish_agent_creation(
            provision_operation_id=record.provision_operation_id,
            primary=None,
        )
        if result is None:
            raise WorkspaceNotFoundError
        return result

    async def release(
        self, workspace_id: str, *, reason: str = "explicit-release"
    ) -> WorkspaceRecord:
        workspace_id = require_workspace_identifier(
            workspace_id, field="workspace_id"
        )
        if type(reason) is not str or reason not in RELEASE_REASONS:
            lifecycle_data(
                operation_id="invalid",
                workspace_id=workspace_id,
                reason=reason,
                allowed_reasons=RELEASE_REASONS,
            )
        result = await converge_workspace_operation(
            self._release(workspace_id, reason),
            name="traceh-workspace-release",
        )
        assert isinstance(result, WorkspaceRecord)
        return result

    async def release_captured(
        self,
        workspace_id: str,
        *,
        candidate_tree: str,
        reason: str,
    ) -> WorkspaceRecord:
        """Release a dirty Workspace only through the provider's exact-tree proof."""

        workspace_id = require_workspace_identifier(
            workspace_id, field="workspace_id"
        )
        if type(reason) is not str or reason not in {"merged", "rejected"}:
            raise WorkspaceInputError("workspace-release-reason-invalid", "reason")
        result = await converge_workspace_operation(
            self._release_captured(workspace_id, candidate_tree, reason),
            name="traceh-workspace-captured-release",
        )
        assert isinstance(result, WorkspaceRecord)
        return result

    async def _release_captured(
        self, workspace_id: str, candidate_tree: str, reason: str
    ) -> WorkspaceRecord:
        async with self._lock:
            catalog = await self._catalogs.load()
            record = catalog.get(workspace_id)
            if record is None:
                raise WorkspaceNotFoundError
            if record.status is WorkspaceStatus.RELEASED:
                if record.reason != reason:
                    raise WorkspaceOperationConflictError
                return record
            try:
                await self._provider.remove_captured(
                    record, candidate_tree=candidate_tree
                )
            except WorkspaceDirtyError:
                await self._quarantine_locked(record, "workspace-dirty")
                raise
            except BaseException as primary:
                local = await self._safe_inspect(record)
                if local is WorkspaceLocalState.MISSING:
                    try:
                        return await self._release_locked(record, reason)
                    except BaseException as cleanup_error:
                        if isinstance(primary, asyncio.CancelledError):
                            raise primary from cleanup_error
                        raise BaseExceptionGroup(
                            "captured workspace removal and recording both failed",
                            (primary, cleanup_error),
                        ) from None
                try:
                    await self._quarantine_locked(record, "git-state-unknown")
                except BaseException as cleanup_error:
                    if isinstance(primary, asyncio.CancelledError):
                        raise primary from cleanup_error
                    raise BaseExceptionGroup(
                        "captured workspace removal and quarantine both failed",
                        (primary, cleanup_error),
                    ) from None
                raise
            return await self._release_locked(record, reason)

    async def _release(self, workspace_id: str, reason: str) -> WorkspaceRecord:
        async with self._lock:
            catalog = await self._catalogs.load()
            record = catalog.get(workspace_id)
            if record is None:
                raise WorkspaceNotFoundError
            if record.status is WorkspaceStatus.RELEASED:
                if record.reason != reason:
                    raise WorkspaceOperationConflictError
                return record
            try:
                await self._provider.remove(record)
            except WorkspaceDirtyError:
                await self._quarantine_locked(record, "workspace-dirty")
                raise
            except BaseException as primary:
                local = await self._safe_inspect(record)
                if local is WorkspaceLocalState.MISSING:
                    try:
                        await self._release_locked(record, reason)
                    except BaseException as cleanup_error:
                        if isinstance(primary, asyncio.CancelledError):
                            raise primary from cleanup_error
                        raise BaseExceptionGroup(
                            "workspace removal and release recording both failed",
                            (primary, cleanup_error),
                        ) from None
                else:
                    quarantine_reason = (
                        "workspace-dirty"
                        if local is WorkspaceLocalState.DIRTY
                        else "git-state-unknown"
                    )
                    try:
                        await self._quarantine_locked(record, quarantine_reason)
                    except BaseException as cleanup_error:
                        if isinstance(primary, asyncio.CancelledError):
                            raise primary from cleanup_error
                        raise BaseExceptionGroup(
                            "workspace removal and quarantine both failed",
                            (primary, cleanup_error),
                        ) from None
                raise
            return await self._release_locked(record, reason)

    async def _attach_locked(
        self, record: WorkspaceRecord, agent_id: str, session_id: str
    ) -> WorkspaceRecord:
        catalog = await self._catalogs.load()
        current = catalog.get(record.workspace_id)
        if current is None:
            raise WorkspaceNotFoundError
        if current.status is WorkspaceStatus.RELEASED:
            raise WorkspaceStateError
        operation_id = workspace_operation_id(
            "attach",
            workspace_id=current.workspace_id,
            agent_id=agent_id,
            session_id=session_id,
            prior_seq=current.updated_seq,
        )
        data = attached_data(
            operation_id=operation_id,
            workspace_id=current.workspace_id,
            agent_id=agent_id,
            session_id=session_id,
        )
        if catalog.operation_exists(operation_id):
            if not catalog.operation_matches(operation_id, WORKSPACE_ATTACHED, data):
                raise WorkspaceOperationConflictError
        else:
            await self._append(
                expected_seq=catalog.head_seq,
                event_type=WORKSPACE_ATTACHED,
                data=data,
            )
        result = (await self._catalogs.load()).get(current.workspace_id)
        if result is None:
            raise WorkspaceWriteError(committed=None)
        return result

    async def _quarantine_locked(
        self, record: WorkspaceRecord, reason: str
    ) -> WorkspaceRecord:
        catalog = await self._catalogs.load()
        current = catalog.get(record.workspace_id)
        if current is None:
            raise WorkspaceNotFoundError
        if current.status is WorkspaceStatus.QUARANTINED:
            return current
        if current.status is WorkspaceStatus.RELEASED:
            return current
        operation_id = workspace_operation_id(
            "quarantine",
            workspace_id=record.workspace_id,
            prior_seq=current.updated_seq,
            reason=reason,
        )
        data = lifecycle_data(
            operation_id=operation_id,
            workspace_id=record.workspace_id,
            reason=reason,
            allowed_reasons=QUARANTINE_REASONS,
        )
        if catalog.operation_exists(operation_id):
            if not catalog.operation_matches(
                operation_id, WORKSPACE_QUARANTINED, data
            ):
                raise WorkspaceOperationConflictError
        else:
            await self._append(
                expected_seq=catalog.head_seq,
                event_type=WORKSPACE_QUARANTINED,
                data=data,
            )
        result = (await self._catalogs.load()).get(record.workspace_id)
        if result is None:
            raise WorkspaceWriteError(committed=None)
        return result

    async def _release_locked(
        self, record: WorkspaceRecord, reason: str
    ) -> WorkspaceRecord:
        catalog = await self._catalogs.load()
        current = catalog.get(record.workspace_id)
        if current is None:
            raise WorkspaceNotFoundError
        if current.status is WorkspaceStatus.RELEASED:
            return current
        operation_id = workspace_operation_id(
            "release", workspace_id=record.workspace_id
        )
        data = lifecycle_data(
            operation_id=operation_id,
            workspace_id=record.workspace_id,
            reason=reason,
            allowed_reasons=RELEASE_REASONS,
        )
        if catalog.operation_exists(operation_id):
            if not catalog.operation_matches(
                operation_id, WORKSPACE_RELEASED, data
            ):
                raise WorkspaceOperationConflictError
        else:
            await self._append(
                expected_seq=catalog.head_seq,
                event_type=WORKSPACE_RELEASED,
                data=data,
            )
        result = (await self._catalogs.load()).get(record.workspace_id)
        if result is None:
            raise WorkspaceWriteError(committed=None)
        return result

    async def _safe_inspect(self, record: WorkspaceRecord) -> WorkspaceLocalState:
        try:
            return await self._provider.inspect(record)
        except Exception:
            return WorkspaceLocalState.UNSAFE

    @staticmethod
    def _require_same_provision(
        record: WorkspaceRecord,
        *,
        creation_request_id: str,
        request: WorkspaceProvisioningRequest,
        owner_agent_id: str | None,
    ) -> None:
        if (
            record.creation_request_id != creation_request_id
            or record.source_id != request.source_id
            or record.source_revision != request.revision
            or record.access is not request.access
            or record.owner_agent_id != owner_agent_id
        ):
            raise WorkspaceOperationConflictError

    @staticmethod
    def _require_source_snapshot(
        snapshot: object,
        request: WorkspaceProvisioningRequest,
    ) -> WorkspaceSourceSnapshot:
        if type(snapshot) is not WorkspaceSourceSnapshot:
            raise WorkspaceSourceError
        if (
            snapshot.source_id != request.source_id
            or snapshot.requested_revision != request.revision
        ):
            raise WorkspaceSourceError
        return snapshot

    def _require_handle(
        self, record: WorkspaceRecord, handle: object
    ) -> WorkspaceHandle:
        if type(handle) is not WorkspaceHandle:
            raise WorkspacePathError
        if (
            handle.workspace_id != record.workspace_id
            or not _same_path(
                handle.root, self._managed_root / record.workspace_id
            )
            or handle.source_id != record.source_id
            or handle.base_revision != record.base_revision
            or handle.access is not record.access
            or handle.status is not record.status
            or handle.owner_agent_id != record.owner_agent_id
            or handle.agent_id != record.agent_id
            or handle.session_id != record.session_id
        ):
            raise WorkspacePathError
        return handle

    async def _append(
        self,
        *,
        expected_seq: int,
        event_type: str,
        data: dict[str, JsonValue],
    ) -> EventEnvelope | None:
        try:
            appended = await self._store.append(
                WORKSPACE_CATALOG_STREAM,
                expected_seq=expected_seq,
                events=(
                    PendingEvent(
                        type=event_type,
                        data=data,
                        schema_version=WORKSPACE_SCHEMA_VERSION,
                    ),
                ),
                durability=Durability.SYNC,
            )
        except asyncio.CancelledError as error:
            await self._committed(event_type, data)
            raise error
        except Exception as error:
            committed = await self._committed(event_type, data)
            if isinstance(error, ConcurrencyConflict):
                if committed is True:
                    return None
                if committed is False:
                    raise WorkspaceCatalogConflictError from None
            raise WorkspaceWriteError(committed=committed) from None
        return appended[0]

    async def _committed(
        self, event_type: str, data: dict[str, JsonValue]
    ) -> bool | None:
        def matches(event: EventEnvelope) -> bool:
            return is_workspace_fact(event, event_type, data)

        return await committed_after_failure(self._catalogs.read_events, matches)


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path.absolute())))


def _same_path(first: Path, second: Path) -> bool:
    try:
        return _path_key(first) == _path_key(second)
    except Exception:
        return False


__all__ = [
    "WorkspaceService",
    "converge_workspace_operation",
    "workspace_operation_id",
]
