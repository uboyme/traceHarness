"""Real Git coverage for temporary-index immutable Patch capture."""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from traceh.api.artifacts import PatchCaptureLimits
from traceh.artifacts import ArtifactGitError, GitPatchBuilder


def _git(*argv: str, cwd: Path, text: bool = True):
    completed = subprocess.run(
        ("git", *argv),
        cwd=cwd,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=text,
        encoding="utf-8" if text else None,
    )
    return completed.stdout.strip() if text else completed.stdout


def _repository(root: Path) -> tuple[Path, str]:
    root.mkdir()
    _git("init", "--initial-branch=main", cwd=root)
    _git("config", "user.name", "TraceHarness Fixture", cwd=root)
    _git("config", "user.email", "fixture@example.invalid", cwd=root)
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    (root / "deleted.txt").write_text("delete me\n", encoding="utf-8")
    _git("add", "tracked.txt", "deleted.txt", cwd=root)
    _git("commit", "-m", "fixture base", cwd=root)
    return root, _git("rev-parse", "HEAD", cwd=root)


def _worktree(source: Path, target: Path, revision: str) -> Path:
    _git("worktree", "add", "--detach", str(target), revision, cwd=source)
    _git("config", "user.name", "TraceHarness Fixture", cwd=target)
    _git("config", "user.email", "fixture@example.invalid", cwd=target)
    return target


def _limits(**overrides) -> PatchCaptureLimits:
    values = {
        "max_changed_paths": 100,
        "max_path_bytes": 512,
        "max_file_bytes": 1024 * 1024,
        "max_total_file_bytes": 4 * 1024 * 1024,
        "max_patch_bytes": 4 * 1024 * 1024,
        **overrides,
    }
    return PatchCaptureLimits(**values)


def _index_bytes(root: Path) -> bytes:
    index = Path(_git("rev-parse", "--path-format=absolute", "--git-path", "index", cwd=root))
    return index.read_bytes()


def _repository_fingerprint(root: Path) -> str:
    common = Path(
        _git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=root)
    ).absolute()
    return hashlib.sha256(
        os.path.normcase(os.path.normpath(str(common))).encode("utf-8")
    ).hexdigest()


async def test_full_patch_captures_committed_staged_unstaged_untracked_deleted_and_binary(
    tmp_path: Path,
) -> None:
    source, base = _repository(tmp_path / "source")
    workspace = _worktree(source, tmp_path / "workspace", base)

    (workspace / "committed.txt").write_text("committed\n", encoding="utf-8")
    _git("add", "committed.txt", cwd=workspace)
    _git("update-index", "--chmod=+x", "tracked.txt", cwd=workspace)
    _git("commit", "-m", "candidate commit", cwd=workspace)
    (workspace / "tracked.txt").write_text("unstaged final\n", encoding="utf-8")
    (workspace / "staged.txt").write_text("staged first\n", encoding="utf-8")
    _git("add", "staged.txt", cwd=workspace)
    (workspace / "staged.txt").write_text("worktree final\n", encoding="utf-8")
    (workspace / "binary.bin").write_bytes(bytes(range(256)) * 8)
    (workspace / "deleted.txt").unlink()
    index_before = _index_bytes(workspace)

    snapshot = await GitPatchBuilder().capture(
        workspace,
        base_revision=base,
        repository_fingerprint=_repository_fingerprint(workspace),
        limits=_limits(),
    )

    assert snapshot.changed_paths == (
        "binary.bin",
        "committed.txt",
        "deleted.txt",
        "staged.txt",
        "tracked.txt",
    )
    assert b"GIT binary patch" in snapshot.patch_bytes
    assert b"old mode 100644\nnew mode 100755" in snapshot.patch_bytes
    assert _index_bytes(workspace) == index_before
    assert snapshot.workspace_head_revision != base

    integration = _worktree(source, tmp_path / "integration", base)
    patch_file = tmp_path / "candidate.patch"
    patch_file.write_bytes(snapshot.patch_bytes)
    _git("apply", "--binary", "--index", str(patch_file), cwd=integration)
    assert _git("write-tree", cwd=integration) == snapshot.candidate_tree
    assert (integration / "staged.txt").read_text(encoding="utf-8") == "worktree final\n"
    assert not (integration / "deleted.txt").exists()


async def test_gitmodules_and_special_paths_are_rejected(tmp_path: Path) -> None:
    source, base = _repository(tmp_path / "source")
    workspace = _worktree(source, tmp_path / "workspace", base)
    (workspace / ".gitmodules").write_text("[submodule \"x\"]\n", encoding="utf-8")
    fingerprint = _repository_fingerprint(workspace)

    with pytest.raises(ArtifactGitError) as raised:
        await GitPatchBuilder().capture(
            workspace,
            base_revision=base,
            repository_fingerprint=fingerprint,
            limits=_limits(),
        )
    assert raised.value.code == "artifact-gitmodules-rejected"


async def test_committed_gitmodules_is_rejected_even_when_it_is_unchanged(
    tmp_path: Path,
) -> None:
    source, _ = _repository(tmp_path / "source")
    (source / ".gitmodules").write_text("[submodule \"x\"]\n", encoding="utf-8")
    _git("add", ".gitmodules", cwd=source)
    _git("commit", "-m", "fixture gitmodules", cwd=source)
    base = _git("rev-parse", "HEAD", cwd=source)
    workspace = _worktree(source, tmp_path / "workspace", base)

    with pytest.raises(ArtifactGitError) as raised:
        await GitPatchBuilder().capture(
            workspace,
            base_revision=base,
            repository_fingerprint=_repository_fingerprint(workspace),
            limits=_limits(),
        )
    assert raised.value.code == "artifact-gitmodules-rejected"


async def test_file_and_patch_limits_fail_closed(tmp_path: Path) -> None:
    source, base = _repository(tmp_path / "source")
    workspace = _worktree(source, tmp_path / "workspace", base)
    (workspace / "large.txt").write_text("x" * 2048, encoding="utf-8")
    fingerprint = _repository_fingerprint(workspace)

    with pytest.raises(ArtifactGitError) as raised:
        await GitPatchBuilder().capture(
            workspace,
            base_revision=base,
            repository_fingerprint=fingerprint,
            limits=_limits(max_file_bytes=1024),
        )
    assert raised.value.code == "artifact-file-size-exceeded"


async def test_capture_ignores_inherited_git_config_parameters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, base = _repository(tmp_path / "source")
    workspace = _worktree(source, tmp_path / "workspace", base)
    (workspace / "new.txt").write_text("new\n", encoding="utf-8")
    fingerprint = _repository_fingerprint(workspace)
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "not-a-valid-config-parameter")

    snapshot = await GitPatchBuilder().capture(
        workspace,
        base_revision=base,
        repository_fingerprint=fingerprint,
        limits=_limits(),
    )

    assert snapshot.changed_paths == ("new.txt",)


async def test_capture_recurses_into_a_new_regular_directory(tmp_path: Path) -> None:
    """A new directory is a container, not a special Git object.

    ``git diff-tree --raw`` reports the directory itself as mode ``040000``
    unless the reader asks for recursive leaf entries.  Treating that container
    as the candidate file rejects every ordinary Patch that adds its first file
    below a new directory.
    """

    source, base = _repository(tmp_path / "source")
    workspace = _worktree(source, tmp_path / "workspace", base)
    nested = workspace / "package"
    nested.mkdir()
    (nested / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    index_before = _index_bytes(workspace)

    snapshot = await GitPatchBuilder().capture(
        workspace,
        base_revision=base,
        repository_fingerprint=_repository_fingerprint(workspace),
        limits=_limits(),
    )

    assert snapshot.changed_paths == ("package/module.py",)
    assert b"diff --git a/package/module.py b/package/module.py" in snapshot.patch_bytes
    integration = _worktree(source, tmp_path / "integration", base)
    patch_file = tmp_path / "nested.patch"
    patch_file.write_bytes(snapshot.patch_bytes)
    _git("apply", "--binary", "--index", str(patch_file), cwd=integration)
    assert _git("write-tree", cwd=integration) == snapshot.candidate_tree
    assert _index_bytes(workspace) == index_before


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink unavailable")
async def test_symlink_change_is_rejected_before_manifest_creation(tmp_path: Path) -> None:
    source, base = _repository(tmp_path / "source")
    workspace = _worktree(source, tmp_path / "workspace", base)
    try:
        (workspace / "linked.txt").symlink_to(workspace / "tracked.txt")
    except OSError:
        pytest.skip("symlink creation is unavailable")
    fingerprint = _repository_fingerprint(workspace)

    with pytest.raises(ArtifactGitError) as raised:
        await GitPatchBuilder().capture(
            workspace,
            base_revision=base,
            repository_fingerprint=fingerprint,
            limits=_limits(),
        )
    assert raised.value.code == "artifact-special-path-rejected"


@pytest.mark.skipif(os.name != "nt", reason="Windows Junction boundary")
async def test_junction_change_is_rejected_without_reading_the_target(
    tmp_path: Path,
) -> None:
    source, base = _repository(tmp_path / "source")
    workspace = _worktree(source, tmp_path / "workspace", base)
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "outside.txt"
    marker.write_text("outside\n", encoding="utf-8")
    junction = workspace / "linked-outside"
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

    try:
        with pytest.raises(ArtifactGitError) as raised:
            await GitPatchBuilder().capture(
                workspace,
                base_revision=base,
                repository_fingerprint=_repository_fingerprint(workspace),
                limits=_limits(),
            )
        assert raised.value.code == "artifact-special-path-rejected"
        assert marker.read_text(encoding="utf-8") == "outside\n"
    finally:
        junction.rmdir()
