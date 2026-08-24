"""Host-owned immutable Patch capture transaction."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from traceh.agents.commit_reconciliation import committed_after_failure
from traceh.agents.inbox import AgentInboxReader
from traceh.api.artifacts import (
    ArtifactCas,
    PatchArtifact,
    PatchCaptureLimits,
    PatchManifest,
    WorkspaceCaptureGate,
)
from traceh.api.events import EventEnvelope, PendingEvent
from traceh.api.json_types import JsonValue
from traceh.api.workspaces import WorkspaceAccess, WorkspaceHandle, WorkspaceRecord, WorkspaceStatus
from traceh.artifacts.catalog import PatchArtifactCatalogReader
from traceh.artifacts.errors import (
    ArtifactCaptureStateError,
    ArtifactCatalogConflictError,
    ArtifactInputError,
    ArtifactOperationConflictError,
    ArtifactServiceClosedError,
    ArtifactWriteError,
)
from traceh.artifacts.events import (
    ARTIFACT_CATALOG_STREAM,
    ARTIFACT_SCHEMA_VERSION,
    PATCH_MANIFEST_RECORDED,
    is_patch_manifest_event,
    patch_manifest_data,
)
from traceh.artifacts.git_patch import GitPatchBuilder, GitPatchSnapshot
from traceh.artifacts.manifest import (
    freeze_capture_limits,
    patch_artifact_id,
    patch_capture_key,
    require_artifact_identifier,
)
from traceh.artifacts.reader import PatchArtifactReader, require_same_manifest
from traceh.concurrency import await_worker_convergence
from traceh.session.event_store import ConcurrencyConflict, Durability
from traceh.session.invariants import CoreInvariantChecker
from traceh.session.projections import StateProjector
from traceh.session.service import SessionService
from traceh.supervision.delivery import AgentDeliveryReader
from traceh.supervision.execution import durable_log_identity
from traceh.supervision.reports import AgentRunReportReader
from traceh.workspaces.service import WorkspaceService


class PatchSnapshotBuilder(Protocol):
    async def capture(
        self,
        workspace_root: Path,
        *,
        base_revision: str,
        repository_fingerprint: str,
        limits: PatchCaptureLimits,
    ) -> GitPatchSnapshot:
        ...


@dataclass(frozen=True, slots=True)
class _CaptureEvidence:
    record: WorkspaceRecord
    session_id: str
    message_id: str
    turn_id: str
    inbox_head: int
    delivery_head: int
    session_head: int
    effect_head: int

    @property
    def receipt(self) -> tuple[object, ...]:
        return (
            self.record.workspace_id,
            self.record.updated_seq,
            self.record.repository_fingerprint,
            self.record.base_revision,
            self.session_id,
            self.message_id,
            self.turn_id,
            self.inbox_head,
            self.delivery_head,
            self.session_head,
            self.effect_head,
        )


class PatchCaptureService:
    """Capture one terminal Agent message into CAS plus one Manifest fact.

    The service owns no Agent scheduler and no workspace path cache. It enters
    the workspace adapter's capture lease, replays every durable identity, and
    delegates the physical snapshot to a temporary-index Git builder.
    """

    __slots__ = (
        "_builder",
        "_cas",
        "_catalogs",
        "_close_task",
        "_closed",
        "_deliveries",
        "_gate",
        "_inboxes",
        "_limits",
        "_lock",
        "_pending",
        "_reader",
        "_reports",
        "_sessions",
        "_store",
        "_workspace",
    )

    def __init__(
        self,
        gate: WorkspaceCaptureGate,
        workspace: WorkspaceService,
        cas: ArtifactCas,
        *,
        limits: PatchCaptureLimits,
        builder: PatchSnapshotBuilder | None = None,
    ) -> None:
        if durable_log_identity(gate.store) is not durable_log_identity(workspace.store):
            raise ArtifactInputError("artifact-store-mismatch", "store")
        self._gate = gate
        self._workspace = workspace
        self._store = gate.store
        self._cas = cas
        self._limits = freeze_capture_limits(limits)
        self._builder = GitPatchBuilder() if builder is None else builder
        self._catalogs = PatchArtifactCatalogReader(self._store)
        self._reader = PatchArtifactReader(self._store, cas)
        self._reports = AgentRunReportReader(self._store)
        self._inboxes = AgentInboxReader(self._store)
        self._deliveries = AgentDeliveryReader(self._store)
        self._sessions = SessionService(self._store)
        self._lock = asyncio.Lock()
        self._pending: dict[tuple[str, str], asyncio.Task[PatchArtifact]] = {}
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    @property
    def store(self):
        return self._store

    @property
    def reader(self) -> PatchArtifactReader:
        return self._reader

    async def capture(self, agent_id: str, message_id: str) -> PatchArtifact:
        agent_id = require_artifact_identifier(agent_id, field="agent_id")
        message_id = require_artifact_identifier(message_id, field="message_id")
        key = (agent_id, message_id)
        async with self._lock:
            if self._closed:
                raise ArtifactServiceClosedError
            task = self._pending.get(key)
            if task is None:
                task = asyncio.create_task(
                    self._capture(agent_id, message_id),
                    name="traceh-patch-artifact-capture",
                )
                self._pending[key] = task
        try:
            return await _await_capture(task)
        finally:
            if task.done():
                async with self._lock:
                    if self._pending.get(key) is task:
                        self._pending.pop(key, None)

    async def aclose(self) -> None:
        async with self._lock:
            if self._close_task is None:
                self._closed = True
                tasks = tuple(self._pending.values())
                self._close_task = asyncio.create_task(
                    self._close(tasks), name="traceh-patch-artifact-close"
                )
            task = self._close_task
        await _await_capture(task)

    async def _close(self, tasks: tuple[asyncio.Task[PatchArtifact], ...]) -> None:
        failures: list[BaseException] = []
        for task in tasks:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                await await_worker_convergence(task)
                if task.cancelled():
                    failures.append(error)
                elif task.exception() is not None:
                    failures.append(task.exception())  # type: ignore[arg-type]
            except BaseException as error:
                failures.append(error)
        if failures:
            raise BaseExceptionGroup("Patch capture close failed", failures)

    async def _capture(self, agent_id: str, message_id: str) -> PatchArtifact:
        async with self._gate.capture_workspace(agent_id) as handle:
            before = await self._load_evidence(agent_id, message_id, handle)
            capture_key = patch_capture_key(
                agent_id=agent_id,
                message_id=message_id,
                workspace_id=before.record.workspace_id,
                workspace_generation=before.record.updated_seq,
            )
            existing = await self._reader.for_capture(capture_key)
            if existing is not None:
                _require_existing_binding(existing.manifest, before)
                return existing

            _require_cas_separate(self._cas.local_root, handle.root)
            first = await self._builder.capture(
                handle.root,
                base_revision=before.record.base_revision,
                repository_fingerprint=before.record.repository_fingerprint,
                limits=self._limits,
            )
            blob = await self._cas.put(first.patch_bytes)
            second = await self._builder.capture(
                handle.root,
                base_revision=before.record.base_revision,
                repository_fingerprint=before.record.repository_fingerprint,
                limits=self._limits,
            )
            after = await self._load_evidence(agent_id, message_id, handle)
            if second != first or after.receipt != before.receipt:
                raise ArtifactCaptureStateError("artifact-capture-drift")

            data = patch_manifest_data(
                artifact_id=patch_artifact_id(capture_key),
                capture_key=capture_key,
                blob=blob,
                agent_id=agent_id,
                session_id=before.session_id,
                message_id=message_id,
                turn_id=before.turn_id,
                workspace_id=before.record.workspace_id,
                workspace_generation=before.record.updated_seq,
                repository_fingerprint=before.record.repository_fingerprint,
                base_revision=before.record.base_revision,
                workspace_head_revision=first.workspace_head_revision,
                candidate_tree=first.candidate_tree,
                changed_paths=first.changed_paths,
            )
            manifest = await self._record(data)
            artifact = await self._reader.load(manifest.artifact_id)
            require_same_manifest(artifact.manifest, data)
            if artifact.content != first.patch_bytes:
                raise ArtifactOperationConflictError
            return artifact

    async def _load_evidence(
        self, agent_id: str, message_id: str, handle: WorkspaceHandle
    ) -> _CaptureEvidence:
        try:
            catalog = await self._workspace.catalog()
            record = catalog.for_agent(agent_id)
            if (
                record is None
                or record.status is not WorkspaceStatus.ATTACHED
                or record.access is not WorkspaceAccess.WRITABLE
                or record.workspace_id != handle.workspace_id
                or record.agent_id != agent_id
                or record.session_id != handle.session_id
                or record.base_revision != handle.base_revision
            ):
                raise ArtifactCaptureStateError("artifact-workspace-binding-invalid")

            report = await self._reports.load(agent_id, message_id)
            if (
                report.status != "completed"
                or report.message_id != message_id
                or report.session_id != record.session_id
                or report.turn_id is None
            ):
                raise ArtifactCaptureStateError("artifact-message-not-completed")

            inbox = await self._inboxes.load(agent_id)
            delivery = await self._deliveries.load(agent_id, inbox)
            if (
                not inbox.messages
                or inbox.messages[-1].message.message_id != message_id
                or delivery.has_open_claim()
                or len(delivery.claims) != len(inbox.messages)
                or any(
                    delivery.outcome_for_message(item.message.message_id) is None
                    for item in inbox.messages
                )
            ):
                raise ArtifactCaptureStateError("artifact-agent-not-quiescent")

            session_events = await self._sessions.read_session(record.session_id)
            effect_events = await self._sessions.read_effects(record.session_id)
            projection = StateProjector().project(session_events)
            if projection.open_turn_id is not None or projection.open_step_id is not None:
                raise ArtifactCaptureStateError("artifact-session-open")
            if CoreInvariantChecker().check(session_events, effect_events):
                raise ArtifactCaptureStateError("artifact-session-invalid")
            _require_latest_turn(session_events, report.turn_id)
            session_head = session_events[-1].seq if session_events else 0
            effect_head = effect_events[-1].seq if effect_events else 0
            if type(session_head) is not int or type(effect_head) is not int:
                raise ArtifactCaptureStateError("artifact-session-invalid")
            return _CaptureEvidence(
                record=record,
                session_id=record.session_id,
                message_id=message_id,
                turn_id=report.turn_id,
                inbox_head=inbox.head_seq,
                delivery_head=delivery.head_seq,
                session_head=session_head,
                effect_head=effect_head,
            )
        except ArtifactCaptureStateError:
            raise
        except Exception:
            raise ArtifactCaptureStateError("artifact-evidence-invalid") from None

    async def _record(self, data: dict[str, JsonValue]) -> PatchManifest:
        capture_key = str(data["capture_key"])
        catalog = await self._catalogs.load()
        existing = catalog.for_capture(capture_key)
        if existing is not None:
            require_same_manifest(existing, data)
            return existing
        try:
            appended = await self._store.append(
                ARTIFACT_CATALOG_STREAM,
                expected_seq=catalog.head_seq,
                events=(
                    PendingEvent(
                        type=PATCH_MANIFEST_RECORDED,
                        data=data,
                        schema_version=ARTIFACT_SCHEMA_VERSION,
                    ),
                ),
                durability=Durability.SYNC,
            )
        except asyncio.CancelledError as error:
            await self._committed(data)
            raise error
        except Exception as error:
            committed = await self._committed(data)
            if committed is True:
                stored = (await self._catalogs.load()).for_capture(capture_key)
                if stored is None:
                    raise ArtifactWriteError(committed=None) from None
                require_same_manifest(stored, data)
                return stored
            if isinstance(error, ConcurrencyConflict) and committed is False:
                raise ArtifactCatalogConflictError from None
            raise ArtifactWriteError(committed=committed) from None
        # Parse through a fresh catalog below; the append return is not a
        # second source of Manifest truth.
        del appended
        stored = (await self._catalogs.load()).for_capture(capture_key)
        if stored is None:
            raise ArtifactWriteError(committed=None)
        require_same_manifest(stored, data)
        return stored

    async def _committed(self, data: dict[str, JsonValue]) -> bool | None:
        def matches(event: EventEnvelope) -> bool:
            return is_patch_manifest_event(event, data)

        return await committed_after_failure(self._catalogs.read_events, matches)


async def _await_capture[T](task: asyncio.Task[T]) -> T:
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


def _require_latest_turn(events: tuple[EventEnvelope, ...], turn_id: str) -> None:
    try:
        starts: list[tuple[int, str]] = []
        ends: list[tuple[int, str]] = []
        for event in events:
            if type(event.seq) is not int or event.seq < 1:
                raise ArtifactCaptureStateError("artifact-session-invalid")
            if event.type == "turn/start":
                event_turn_id = event.data.get("turn_id")
                if type(event_turn_id) is not str:
                    raise ArtifactCaptureStateError("artifact-session-invalid")
                starts.append((event.seq, event_turn_id))
            elif event.type == "turn/end":
                event_turn_id = event.data.get("turn_id")
                if type(event_turn_id) is not str:
                    raise ArtifactCaptureStateError("artifact-session-invalid")
                ends.append((event.seq, event_turn_id))
        if not starts or not ends or starts[-1][1] != turn_id or ends[-1][1] != turn_id:
            raise ArtifactCaptureStateError("artifact-turn-not-latest")
        if starts[-1][0] >= ends[-1][0]:
            raise ArtifactCaptureStateError("artifact-turn-not-latest")
    except ArtifactCaptureStateError:
        raise
    except Exception:
        raise ArtifactCaptureStateError("artifact-session-invalid") from None


def _require_existing_binding(
    manifest: PatchManifest, evidence: _CaptureEvidence
) -> None:
    if (
        manifest.agent_id != evidence.record.agent_id
        or manifest.session_id != evidence.session_id
        or manifest.message_id != evidence.message_id
        or manifest.turn_id != evidence.turn_id
        or manifest.workspace_id != evidence.record.workspace_id
        or manifest.workspace_generation != evidence.record.updated_seq
        or manifest.repository_fingerprint != evidence.record.repository_fingerprint
        or manifest.base_revision != evidence.record.base_revision
    ):
        raise ArtifactOperationConflictError


def _require_cas_separate(cas_root: Path | None, workspace_root: Path) -> None:
    if cas_root is None:
        return
    try:
        cas = Path(cas_root).absolute()
        workspace = Path(workspace_root).absolute()
        if _contains(cas, workspace) or _contains(workspace, cas):
            raise ArtifactInputError("artifact-cas-workspace-overlap", "cas_root")
    except ArtifactInputError:
        raise
    except Exception:
        raise ArtifactInputError("artifact-cas-root-invalid", "cas_root") from None


def _contains(root: Path, child: Path) -> bool:
    root_key = os.path.normcase(os.path.normpath(str(root)))
    child_key = os.path.normcase(os.path.normpath(str(child)))
    try:
        return os.path.commonpath((root_key, child_key)) == root_key
    except ValueError:
        return False


__all__ = ["PatchCaptureService", "PatchSnapshotBuilder"]
