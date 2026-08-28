"""Real Git worktree checks for the Stage C local provider."""

from __future__ import annotations

import asyncio
import os
import stat
import subprocess
from pathlib import Path

import pytest

from traceh.api.workspaces import (
    WorkspaceAccess,
    WorkspaceLocalState,
    WorkspaceProvisioningRequest,
    WorkspaceStatus,
)
from traceh.session.event_store import InMemoryEventStore
from traceh.workspaces import (
    LocalGitWorkspaceProvider,
    WorkspaceDirtyError,
    WorkspaceInputError,
    WorkspacePathError,
    WorkspaceService,
    WorkspaceSourceError,
    workspace_identity,
    workspace_operation_id,
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


def _repository(root: Path) -> tuple[Path, str]:
    root.mkdir()
    _git("init", "--initial-branch=main", cwd=root)
    _git("config", "user.name", "TraceHarness Fixture", cwd=root)
    _git("config", "user.email", "fixture@example.invalid", cwd=root)
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git("add", "tracked.txt", cwd=root)
    _git("commit", "-m", "fixture base", cwd=root)
    return root, _git("rev-parse", "HEAD", cwd=root)


def _request(access: WorkspaceAccess = WorkspaceAccess.WRITABLE):
    return WorkspaceProvisioningRequest("trusted-source", "main", access)


def _service(source: Path, managed: Path):
    store = InMemoryEventStore()
    provider = LocalGitWorkspaceProvider(
        managed_root=managed,
        sources={"trusted-source": source},
    )
    return store, provider, WorkspaceService(store, provider)


def test_workspace_identity_keeps_the_full_digest_without_a_path_label() -> None:
    identity = workspace_identity(
        operation_id="provision-op",
        creation_request_id="create-request",
    )

    assert identity.startswith("ws-")
    assert len(identity) == 67
    assert all(character in "0123456789abcdef" for character in identity[3:])
    assert identity != workspace_identity(
        operation_id="provision-op",
        creation_request_id="another-create-request",
    )


@pytest.mark.skipif(os.name != "nt", reason="Git for Windows path boundary")
async def test_workspace_identity_fits_a_nested_git_admin_path(
    tmp_path: Path,
) -> None:
    operation_id = "provision-op"
    creation_request_id = "create-request"
    identity = workspace_identity(
        operation_id=operation_id,
        creation_request_id=creation_request_id,
    )
    suffix = Path("source") / ".git" / "worktrees" / identity
    probe = tmp_path / "p" / suffix
    padding = 1 + (229 - len(str(probe)))
    if padding < 1 or padding > 200:
        pytest.skip("the host temporary root cannot form the boundary path")
    base = tmp_path / ("p" * padding)
    base.mkdir()
    source, _ = _repository(base / "source")
    _, _, service = _service(source, base / "managed")

    handle = await service.provision(
        operation_id=operation_id,
        creation_request_id=creation_request_id,
        request=_request(),
        owner_agent_id=None,
    )

    assert handle.workspace_id == identity
    assert len(str(source / ".git" / "worktrees" / identity)) == 229
    await service.release(handle.workspace_id)


async def test_real_worktree_is_pinned_and_clean_release_unregisters_it(
    tmp_path: Path,
) -> None:
    source, base = _repository(tmp_path / "source")
    managed = tmp_path / "managed"
    _, provider, service = _service(source, managed)

    handle = await service.provision(
        operation_id="provision-op",
        creation_request_id="create-request",
        request=_request(),
        owner_agent_id=None,
    )
    record = (await service.catalog()).get(handle.workspace_id)
    assert record is not None
    assert handle.root.parent == managed
    assert handle.base_revision == base
    assert _git("rev-parse", "HEAD", cwd=handle.root) == base
    assert await provider.inspect(record) is WorkspaceLocalState.CLEAN

    released = await service.release(handle.workspace_id)
    assert released.status is WorkspaceStatus.RELEASED
    assert not handle.root.exists()
    listed = _git("worktree", "list", "--porcelain", cwd=source)
    assert str(handle.root).replace("\\", "/") not in listed.replace("\\", "/")


async def test_dirty_worktree_is_never_force_removed(tmp_path: Path) -> None:
    source, _ = _repository(tmp_path / "source")
    managed = tmp_path / "managed"
    _, provider, service = _service(source, managed)
    handle = await service.provision(
        operation_id="provision-op",
        creation_request_id="create-request",
        request=_request(),
        owner_agent_id=None,
    )
    record = (await service.catalog()).get(handle.workspace_id)
    assert record is not None
    marker = handle.root / "candidate-change.txt"
    marker.write_text("candidate\n", encoding="utf-8")
    assert await provider.inspect(record) is WorkspaceLocalState.DIRTY

    with pytest.raises(WorkspaceDirtyError):
        await service.release(handle.workspace_id, reason="rejected")
    assert marker.read_text(encoding="utf-8") == "candidate\n"
    assert (await service.catalog()).get(handle.workspace_id).status is (
        WorkspaceStatus.QUARANTINED
    )

    marker.unlink()
    released = await service.release(handle.workspace_id, reason="rejected")
    assert released.status is WorkspaceStatus.RELEASED


async def test_captured_release_refuses_a_tree_that_was_not_the_frozen_artifact(
    tmp_path: Path,
) -> None:
    source, _ = _repository(tmp_path / "source")
    _, _, service = _service(source, tmp_path / "managed")
    handle = await service.provision(
        operation_id="provision-op",
        creation_request_id="create-request",
        request=_request(),
        owner_agent_id=None,
    )
    marker = handle.root / "candidate-change.txt"
    marker.write_text("candidate\n", encoding="utf-8")

    with pytest.raises(WorkspaceDirtyError):
        await service.release_captured(
            handle.workspace_id,
            candidate_tree="0" * 40,
            reason="rejected",
        )

    record = (await service.catalog()).get(handle.workspace_id)
    assert record is not None and record.status is WorkspaceStatus.QUARANTINED
    assert marker.read_text(encoding="utf-8") == "candidate\n"


async def test_dirty_provisional_retry_is_quarantined_instead_of_adopted(
    tmp_path: Path,
) -> None:
    source, _ = _repository(tmp_path / "source")
    _, _, service = _service(source, tmp_path / "managed")
    handle = await service.provision(
        operation_id="provision-op",
        creation_request_id="create-request",
        request=_request(),
        owner_agent_id=None,
    )
    marker = handle.root / "unowned-change.txt"
    marker.write_text("preserve\n", encoding="utf-8")

    with pytest.raises(WorkspaceDirtyError):
        await service.provision(
            operation_id="provision-op",
            creation_request_id="create-request",
            request=_request(),
            owner_agent_id=None,
        )
    record = (await service.catalog()).get(handle.workspace_id)
    assert record is not None
    assert record.status is WorkspaceStatus.QUARANTINED
    assert marker.read_text(encoding="utf-8") == "preserve\n"


async def test_existing_unregistered_directory_is_quarantined_not_deleted(
    tmp_path: Path,
) -> None:
    source, _ = _repository(tmp_path / "source")
    managed = tmp_path / "managed"
    _, _, service = _service(source, managed)
    operation_id = workspace_operation_id(
        "provision", creation_request_id="create-request", owner_agent_id=None
    )
    workspace_id = workspace_identity(
        operation_id=operation_id,
        creation_request_id="create-request",
    )
    occupied = managed / workspace_id
    occupied.mkdir(parents=True)
    marker = occupied / "user-file.txt"
    marker.write_text("keep\n", encoding="utf-8")

    with pytest.raises(WorkspacePathError):
        await service.provision(
            operation_id=operation_id,
            creation_request_id="create-request",
            request=_request(),
            owner_agent_id=None,
        )
    record = (await service.catalog()).get(workspace_id)
    assert record is not None
    assert record.status is WorkspaceStatus.QUARANTINED
    assert marker.read_text(encoding="utf-8") == "keep\n"


async def test_dangling_symlink_target_is_quarantined_not_replaced(
    tmp_path: Path,
) -> None:
    source, _ = _repository(tmp_path / "source")
    managed = tmp_path / "managed"
    _, _, service = _service(source, managed)
    operation_id = workspace_operation_id(
        "provision", creation_request_id="create-request", owner_agent_id=None
    )
    workspace_id = workspace_identity(
        operation_id=operation_id,
        creation_request_id="create-request",
    )
    managed.mkdir()
    target = managed / workspace_id
    try:
        target.symlink_to(tmp_path / "missing-target", target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")

    with pytest.raises(WorkspacePathError):
        await service.provision(
            operation_id=operation_id,
            creation_request_id="create-request",
            request=_request(),
            owner_agent_id=None,
        )
    assert target.is_symlink()
    record = (await service.catalog()).get(workspace_id)
    assert record is not None
    assert record.status is WorkspaceStatus.QUARANTINED


async def test_dirty_source_is_rejected_before_a_catalog_fact(tmp_path: Path) -> None:
    source, _ = _repository(tmp_path / "source")
    (source / "untracked.txt").write_text("dirty\n", encoding="utf-8")
    _, _, service = _service(source, tmp_path / "managed")

    with pytest.raises(WorkspaceSourceError):
        await service.provision(
            operation_id="provision-op",
            creation_request_id="create-request",
            request=_request(),
            owner_agent_id=None,
        )
    assert (await service.catalog()).workspaces == ()


async def test_idempotent_retry_keeps_the_original_commit_after_source_moves(
    tmp_path: Path,
) -> None:
    source, base = _repository(tmp_path / "source")
    _, _, service = _service(source, tmp_path / "managed")
    first = await service.provision(
        operation_id="provision-op",
        creation_request_id="create-request",
        request=_request(WorkspaceAccess.READ_ONLY),
        owner_agent_id=None,
    )
    (source / "tracked.txt").write_text("later\n", encoding="utf-8")
    _git("add", "tracked.txt", cwd=source)
    _git("commit", "-m", "move source", cwd=source)

    second = await service.provision(
        operation_id="provision-op",
        creation_request_id="create-request",
        request=_request(WorkspaceAccess.READ_ONLY),
        owner_agent_id=None,
    )
    assert second.workspace_id == first.workspace_id
    assert second.base_revision == first.base_revision == base
    assert second.writable is False


async def test_paths_with_spaces_keep_the_exact_worktree_identity(
    tmp_path: Path,
) -> None:
    source, _ = _repository(tmp_path / "source repo")
    managed = tmp_path / "managed worktrees"
    _, provider, service = _service(source, managed)
    handle = await service.provision(
        operation_id="provision-op",
        creation_request_id="create-request",
        request=_request(),
        owner_agent_id=None,
    )
    record = (await service.catalog()).get(handle.workspace_id)
    assert record is not None
    assert handle.root.parent == managed
    assert await provider.inspect(record) is WorkspaceLocalState.CLEAN


async def test_swapped_valid_git_markers_are_rejected_without_removal(
    tmp_path: Path,
) -> None:
    source, _ = _repository(tmp_path / "source")
    _, provider, service = _service(source, tmp_path / "managed")
    first = await service.provision(
        operation_id="provision-first",
        creation_request_id="create-first",
        request=_request(),
        owner_agent_id=None,
    )
    second = await service.provision(
        operation_id="provision-second",
        creation_request_id="create-second",
        request=_request(),
        owner_agent_id=None,
    )
    first_record = (await service.catalog()).get(first.workspace_id)
    second_record = (await service.catalog()).get(second.workspace_id)
    assert first_record is not None
    assert second_record is not None
    first_marker = first.root / ".git"
    second_marker = second.root / ".git"
    first_contents = first_marker.read_bytes()
    second_contents = second_marker.read_bytes()
    first_marker.chmod(first_marker.stat().st_mode | stat.S_IWRITE)
    second_marker.chmod(second_marker.stat().st_mode | stat.S_IWRITE)
    first_marker.unlink()
    second_marker.unlink()
    first_marker.write_bytes(second_contents)
    second_marker.write_bytes(first_contents)

    assert await provider.inspect(first_record) is WorkspaceLocalState.UNSAFE
    assert await provider.inspect(second_record) is WorkspaceLocalState.UNSAFE
    with pytest.raises(WorkspacePathError):
        await service.release(first.workspace_id)
    assert first.root.is_dir()
    assert second.root.is_dir()


async def test_worktree_creation_does_not_execute_repository_hooks(
    tmp_path: Path,
) -> None:
    source, _ = _repository(tmp_path / "source")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    marker = tmp_path / "hook-ran.txt"
    hook = hooks / "post-checkout"
    hook.write_text(
        f"#!/bin/sh\nprintf ran > '{marker.as_posix()}'\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    _git("config", "core.hooksPath", str(hooks), cwd=source)
    _, _, service = _service(source, tmp_path / "managed")

    await service.provision(
        operation_id="provision-op",
        creation_request_id="create-request",
        request=_request(),
        owner_agent_id=None,
    )
    assert not marker.exists()


async def test_managed_root_inside_source_is_rejected_without_dirtying_source(
    tmp_path: Path,
) -> None:
    source, _ = _repository(tmp_path / "source")
    nested_root = source / "managed"
    _, _, service = _service(source, nested_root)
    with pytest.raises(WorkspacePathError):
        await service.provision(
            operation_id="provision-op",
            creation_request_id="create-request",
            request=_request(),
            owner_agent_id=None,
        )
    assert not nested_root.exists()
    assert _git("status", "--porcelain", cwd=source) == ""


@pytest.mark.parametrize("timeout", (float("nan"), float("inf"), 0.0, True))
def test_git_timeout_must_be_a_finite_positive_number(
    tmp_path: Path, timeout: object
) -> None:
    source, _ = _repository(tmp_path / f"source-{type(timeout).__name__}")
    with pytest.raises(WorkspaceInputError) as raised:
        LocalGitWorkspaceProvider(
            managed_root=tmp_path / "managed",
            sources={"trusted-source": source},
            timeout_seconds=timeout,
        )
    assert raised.value.code == "workspace-timeout-invalid"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction boundary")
async def test_windows_junction_target_is_quarantined_without_traversal(
    tmp_path: Path,
) -> None:
    source, _ = _repository(tmp_path / "source")
    managed = tmp_path / "managed"
    _, _, service = _service(source, managed)
    operation_id = workspace_operation_id(
        "provision", creation_request_id="create-request", owner_agent_id=None
    )
    workspace_id = workspace_identity(
        operation_id=operation_id,
        creation_request_id="create-request",
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "outside.txt"
    marker.write_text("outside\n", encoding="utf-8")
    managed.mkdir()
    junction = managed / workspace_id
    process = await asyncio.create_subprocess_exec(
        "cmd",
        "/c",
        "mklink",
        "/J",
        str(junction),
        str(outside),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    if await process.wait() != 0:
        pytest.skip("junction creation is unavailable")

    with pytest.raises(WorkspacePathError):
        await service.provision(
            operation_id=operation_id,
            creation_request_id="create-request",
            request=_request(),
            owner_agent_id=None,
        )
    assert marker.read_text(encoding="utf-8") == "outside\n"
    assert (await service.catalog()).get(workspace_id).status is (
        WorkspaceStatus.QUARANTINED
    )
