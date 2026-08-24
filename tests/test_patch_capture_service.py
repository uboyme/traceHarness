"""D1 capture transaction on the real managed Supervisor and Git worktree."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from supervision_fixtures import GatedProvider, RuntimeFactory, message, scripted

from traceh.api.agents import AgentSpec, MessageTarget
from traceh.api.artifacts import PatchCaptureLimits
from traceh.api.workspaces import WorkspaceAccess, WorkspaceProvisioningRequest
from traceh.artifacts import (
    ArtifactCaptureStateError,
    ArtifactInputError,
    ArtifactReportingAgentSupervisor,
    ArtifactServiceClosedError,
    GitPatchBuilder,
    GitPatchSnapshot,
    LocalArtifactCas,
    PatchCaptureService,
)
from traceh.artifacts.events import ARTIFACT_CATALOG_STREAM
from traceh.session.event_store import InMemoryEventStore
from traceh.supervision import ProcessAgentSupervisor
from traceh.workspaces import (
    LocalGitWorkspaceProvider,
    WorkspaceManagedAgentSupervisor,
    WorkspaceService,
)


def _git(*argv: str, cwd: Path) -> str:
    completed = subprocess.run(
        ("git", *argv),
        cwd=cwd,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _repository(root: Path) -> Path:
    root.mkdir()
    _git("init", "--initial-branch=main", cwd=root)
    _git("config", "user.name", "TraceHarness Fixture", cwd=root)
    _git("config", "user.email", "fixture@example.invalid", cwd=root)
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git("add", "tracked.txt", cwd=root)
    _git("commit", "-m", "fixture base", cwd=root)
    return root


def _index_bytes(root: Path) -> bytes:
    return Path(
        _git(
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "index",
            cwd=root,
        )
    ).read_bytes()


class _Policy:
    def workspace_for_agent(self, spec: AgentSpec) -> WorkspaceProvisioningRequest:
        del spec
        return WorkspaceProvisioningRequest(
            source_id="trusted-source",
            revision="main",
            access=WorkspaceAccess.WRITABLE,
        )


class _CountingBuilder:
    def __init__(self, inner=None) -> None:
        self.inner = inner if inner is not None else GitPatchBuilder()
        self.calls = 0

    async def capture(self, *args, **kwargs):
        self.calls += 1
        return await self.inner.capture(*args, **kwargs)


class _GatedBuilder(_CountingBuilder):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def capture(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            await self.release.wait()
        return await self.inner.capture(*args, **kwargs)


class _SnapshotSequenceBuilder:
    def __init__(self, snapshots: tuple[GitPatchSnapshot, ...]) -> None:
        self._snapshots = list(snapshots)

    async def capture(self, *args, **kwargs) -> GitPatchSnapshot:
        del args, kwargs
        return self._snapshots.pop(0)


class _CommitArtifactThenCancelStore:
    def __init__(self) -> None:
        self.inner = InMemoryEventStore()
        self.cancelled = False

    async def append(self, stream_id, *, expected_seq, events, durability):
        result = await self.inner.append(
            stream_id,
            expected_seq=expected_seq,
            events=events,
            durability=durability,
        )
        if stream_id == ARTIFACT_CATALOG_STREAM and not self.cancelled:
            self.cancelled = True
            raise asyncio.CancelledError
        return result

    async def read(self, stream_id, *, from_seq=1):
        return await self.inner.read(stream_id, from_seq=from_seq)

    async def head(self, stream_id):
        return await self.inner.head(stream_id)

    async def list_streams(self, *, prefix=None):
        return await self.inner.list_streams(prefix=prefix)


class _ObservedInnerSupervisor:
    def __init__(self, inner) -> None:
        self.inner = inner
        self.send_entered = asyncio.Event()
        self.close_entered = asyncio.Event()

    @property
    def store(self):
        return self.inner.store

    async def send(self, *args, **kwargs):
        self.send_entered.set()
        return await self.inner.send(*args, **kwargs)

    async def aclose(self):
        self.close_entered.set()
        return await self.inner.aclose()

    def __getattr__(self, name):
        return getattr(self.inner, name)


class _ObservedWorkspaceService(WorkspaceService):
    __slots__ = ("resolve_entered",)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.resolve_entered = asyncio.Event()

    async def resolve_for_agent(self, agent_id):
        self.resolve_entered.set()
        return await super().resolve_for_agent(agent_id)


def _limits() -> PatchCaptureLimits:
    return PatchCaptureLimits(
        max_changed_paths=100,
        max_path_bytes=512,
        max_file_bytes=1024 * 1024,
        max_total_file_bytes=4 * 1024 * 1024,
        max_patch_bytes=4 * 1024 * 1024,
    )


def _assembly(
    tmp_path: Path,
    *,
    model_provider=None,
    builder=None,
    store=None,
    observe_inner: bool = False,
):
    source = _repository(tmp_path / "source")
    managed = tmp_path / "managed"
    store = InMemoryEventStore() if store is None else store
    provider = LocalGitWorkspaceProvider(
        managed_root=managed,
        sources={"trusted-source": source},
    )
    workspaces = _ObservedWorkspaceService(store, provider)
    factory = RuntimeFactory(
        store,
        managed,
        provider=scripted() if model_provider is None else model_provider,
    )
    process_supervisor = ProcessAgentSupervisor(store=store, factory=factory)
    inner = (
        _ObservedInnerSupervisor(process_supervisor)
        if observe_inner
        else process_supervisor
    )
    supervisor = WorkspaceManagedAgentSupervisor(
        inner, workspaces, workspace_policy=_Policy()
    )
    cas = LocalArtifactCas(tmp_path / "artifact-cas")
    capture = PatchCaptureService(
        supervisor,
        workspaces,
        cas,
        limits=_limits(),
        builder=builder,
    )
    return store, workspaces, supervisor, capture


async def _completed_agent(
    tmp_path: Path, *, builder=None, store=None, observe_inner: bool = False
):
    store, workspaces, supervisor, capture = _assembly(
        tmp_path,
        builder=builder,
        store=store,
        observe_inner=observe_inner,
    )
    handle = await supervisor.create(
        AgentSpec(preset="coder", workspace_id="workspace-intent"),
        request_id="create-request",
        agent_id="coder-agent",
        session_id="coder-session",
    )
    receipt = await supervisor.send(
        handle.agent_id,
        message("work-message"),
        target=MessageTarget.NEW_TURN,
        wakeup=True,
    )
    await supervisor.wait_message(handle.agent_id, receipt.message_id)
    workspace = await workspaces.resolve_for_agent(handle.agent_id)
    (workspace.root / "candidate.py").write_text("VALUE = 1\n", encoding="utf-8")
    return store, workspaces, supervisor, capture, handle, receipt, workspace


async def test_capture_binds_exact_durable_facts_and_reporting_is_read_only(
    tmp_path: Path,
) -> None:
    builder = _CountingBuilder()
    (
        _,
        _,
        supervisor,
        capture,
        handle,
        receipt,
        workspace,
    ) = await _completed_agent(tmp_path, builder=builder)
    original_index = _index_bytes(workspace.root)

    first = await capture.capture(handle.agent_id, receipt.message_id)
    assert first.manifest.agent_id == handle.agent_id
    assert first.manifest.session_id == handle.session_id
    assert first.manifest.message_id == receipt.message_id
    assert first.manifest.workspace_id == workspace.workspace_id
    assert first.manifest.changed_paths == ("candidate.py",)
    assert first.content.startswith(b"diff --git")
    assert builder.calls == 2
    assert _index_bytes(workspace.root) == original_index

    (workspace.root / "later.py").write_text("VALUE = 2\n", encoding="utf-8")
    retried = await capture.capture(handle.agent_id, receipt.message_id)
    assert retried == first
    assert builder.calls == 2

    reporting = ArtifactReportingAgentSupervisor(supervisor, capture.reader)
    report = await reporting.report(handle.agent_id, receipt.message_id)
    assert report.artifact_refs == (first.manifest.reference,)
    assert await capture.reader.resolve_reference(report.artifact_refs[0]) == first

    await capture.aclose()
    await supervisor.aclose()


async def test_capture_rejects_a_durable_open_claim(tmp_path: Path) -> None:
    provider = GatedProvider()
    _, workspaces, supervisor, capture = _assembly(
        tmp_path, model_provider=provider
    )
    handle = await supervisor.create(
        AgentSpec(preset="coder", workspace_id="workspace-intent"),
        request_id="create-request",
        agent_id="coder-agent",
        session_id="coder-session",
    )
    receipt = await supervisor.send(
        handle.agent_id,
        message("blocked-message"),
        target=MessageTarget.NEW_TURN,
        wakeup=True,
    )
    await provider.entered.wait()
    workspace = await workspaces.resolve_for_agent(handle.agent_id)
    (workspace.root / "candidate.py").write_text("VALUE = 1\n", encoding="utf-8")

    with pytest.raises(ArtifactCaptureStateError) as raised:
        await capture.capture(handle.agent_id, receipt.message_id)
    assert raised.value.code == "artifact-evidence-invalid"

    provider.release.set()
    await supervisor.wait_message(handle.agent_id, receipt.message_id)
    await capture.aclose()
    await supervisor.aclose()


async def test_repeated_capture_cancellation_converges_and_leaves_one_artifact(
    tmp_path: Path,
) -> None:
    builder = _GatedBuilder()
    _, _, supervisor, capture, handle, receipt, _ = await _completed_agent(
        tmp_path, builder=builder
    )
    capturing = asyncio.create_task(
        capture.capture(handle.agent_id, receipt.message_id)
    )
    await builder.entered.wait()
    capturing.cancel()
    capturing.cancel()
    capturing.cancel()
    assert not capturing.done()
    builder.release.set()
    with pytest.raises(asyncio.CancelledError):
        await capturing

    recovered = await capture.capture(handle.agent_id, receipt.message_id)
    assert recovered.manifest.message_id == receipt.message_id
    assert len(await capture.reader.refs_for(handle.agent_id, receipt.message_id)) == 1
    await capture.aclose()
    await supervisor.aclose()


async def test_concurrent_same_capture_identity_shares_one_transaction(
    tmp_path: Path,
) -> None:
    builder = _GatedBuilder()
    store, _, supervisor, capture, handle, receipt, _ = await _completed_agent(
        tmp_path, builder=builder
    )
    first = asyncio.create_task(capture.capture(handle.agent_id, receipt.message_id))
    await builder.entered.wait()
    second = asyncio.create_task(capture.capture(handle.agent_id, receipt.message_id))
    await asyncio.sleep(0)
    builder.release.set()

    first_result, second_result = await asyncio.gather(first, second)
    assert first_result == second_result
    assert builder.calls == 2
    assert len(await store.read(ARTIFACT_CATALOG_STREAM)) == 1
    await capture.aclose()
    await supervisor.aclose()


async def test_capture_lease_blocks_new_send(
    tmp_path: Path,
) -> None:
    builder = _GatedBuilder()
    _, workspaces, supervisor, capture, handle, receipt, _ = await _completed_agent(
        tmp_path, builder=builder
    )
    capturing = asyncio.create_task(capture.capture(handle.agent_id, receipt.message_id))
    await builder.entered.wait()
    workspaces.resolve_entered.clear()
    sending = asyncio.create_task(
        supervisor.send(
            handle.agent_id,
            message("later-message"),
            target=MessageTarget.NEW_TURN,
            wakeup=False,
        )
    )
    try:
        await asyncio.sleep(0)
        assert not workspaces.resolve_entered.is_set()
    finally:
        builder.release.set()
        await asyncio.gather(capturing, sending, return_exceptions=True)
    await capture.aclose()
    await supervisor.aclose()


async def test_capture_lease_blocks_workspace_close(tmp_path: Path) -> None:
    builder = _GatedBuilder()
    _, _, supervisor, capture, handle, receipt, _ = await _completed_agent(
        tmp_path, builder=builder, observe_inner=True
    )
    observer = supervisor._inner
    assert isinstance(observer, _ObservedInnerSupervisor)
    capturing = asyncio.create_task(capture.capture(handle.agent_id, receipt.message_id))
    await builder.entered.wait()
    closing = asyncio.create_task(supervisor.aclose())
    try:
        await asyncio.sleep(0)
        assert not observer.close_entered.is_set()
    finally:
        builder.release.set()
        await asyncio.gather(capturing, closing, return_exceptions=True)
    await capture.aclose()


async def test_local_cas_must_not_overlap_the_managed_workspace(tmp_path: Path) -> None:
    store, workspaces, supervisor, _, handle, receipt, workspace = await _completed_agent(
        tmp_path
    )
    overlapping = PatchCaptureService(
        supervisor,
        workspaces,
        LocalArtifactCas(workspace.root / "cas"),
        limits=_limits(),
    )
    with pytest.raises(ArtifactInputError) as raised:
        await overlapping.capture(handle.agent_id, receipt.message_id)
    assert raised.value.code == "artifact-cas-workspace-overlap"
    assert await store.read("artifacts:catalog") == ()
    await overlapping.aclose()
    await supervisor.aclose()


async def test_capture_rejects_git_or_evidence_drift_without_a_manifest(
    tmp_path: Path,
) -> None:
    first = GitPatchSnapshot(
        workspace_head_revision="1" * 40,
        candidate_tree="2" * 40,
        changed_paths=("candidate.py",),
        patch_bytes=b"first",
        total_file_bytes=5,
    )
    second = GitPatchSnapshot(
        workspace_head_revision="1" * 40,
        candidate_tree="3" * 40,
        changed_paths=("candidate.py",),
        patch_bytes=b"second",
        total_file_bytes=6,
    )
    store, _, supervisor, capture, handle, receipt, _ = await _completed_agent(
        tmp_path, builder=_SnapshotSequenceBuilder((first, second))
    )

    with pytest.raises(ArtifactCaptureStateError) as raised:
        await capture.capture(handle.agent_id, receipt.message_id)
    assert raised.value.code == "artifact-capture-drift"
    assert await store.read(ARTIFACT_CATALOG_STREAM) == ()
    await capture.aclose()
    await supervisor.aclose()


async def test_manifest_commit_then_cancel_is_recoverable_and_not_duplicated(
    tmp_path: Path,
) -> None:
    store = _CommitArtifactThenCancelStore()
    _, _, supervisor, capture, handle, receipt, _ = await _completed_agent(
        tmp_path,
        builder=_CountingBuilder(),
        store=store,
    )

    with pytest.raises(asyncio.CancelledError):
        await capture.capture(handle.agent_id, receipt.message_id)
    assert len(await store.read(ARTIFACT_CATALOG_STREAM)) == 1

    recovered = await capture.capture(handle.agent_id, receipt.message_id)
    assert recovered.manifest.message_id == receipt.message_id
    assert len(await store.read(ARTIFACT_CATALOG_STREAM)) == 1
    await capture.aclose()
    await supervisor.aclose()


async def test_capture_close_converges_under_repeated_cancellation(
    tmp_path: Path,
) -> None:
    builder = _GatedBuilder()
    _, _, supervisor, capture, handle, receipt, _ = await _completed_agent(
        tmp_path, builder=builder
    )
    capturing = asyncio.create_task(capture.capture(handle.agent_id, receipt.message_id))
    await builder.entered.wait()
    closing = asyncio.create_task(capture.aclose())
    await asyncio.sleep(0)
    closing.cancel()
    closing.cancel()
    closing.cancel()
    assert not closing.done()

    builder.release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    artifact = await capturing
    assert artifact.manifest.message_id == receipt.message_id
    with pytest.raises(ArtifactServiceClosedError) as raised:
        await capture.capture(handle.agent_id, receipt.message_id)
    assert getattr(raised.value, "code", None) == "artifact-service-closed"
    await supervisor.aclose()
