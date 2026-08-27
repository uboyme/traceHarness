"""Throwaway Git repositories the benchmark creates for one attempt.

Every attempt gets its own source repository and its own one-shot **bare**
promotion target, both built here from the manifest's task tree.  That is the
structural reason a benchmark can never touch a real remote: no configuration
value names a repository, so there is nothing to point elsewhere.

The commit is deterministic - fixed tree, fixed author and committer identity,
fixed timestamps, fixed message - so every attempt of one task starts from the
*same* base revision.  The report checks that equality rather than assuming it,
which is how "single and multi ran against the same source revision" becomes a
verifiable fact instead of a claim.

Git runs as a direct child with the whole inherited ``GIT_*`` prefix removed and
only host-owned controls added back, the same positive allowlist the Workspace,
Artifact and Promotion domains use.  This module owns its copy because it is the
only caller that creates repositories; it does not read or write any other
domain's repository.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from traceh.evaluation.errors import BenchmarkExecutionError
from traceh.evaluation.manifest import BENCHMARK_SOURCE_REVISION, BENCHMARK_TARGET_REF
from traceh.process_control import converge_process

GIT_TIMEOUT_SECONDS = 60.0
MAX_GIT_OUTPUT_BYTES = 64 * 1024

MAX_INITIAL_FILES = 256
MAX_INITIAL_FILE_BYTES = 1024 * 1024
MAX_INITIAL_TOTAL_BYTES = 8 * 1024 * 1024

_COMMIT_IDENTITY = "TraceHarness Benchmark"
_COMMIT_EMAIL = "benchmark@traceharness.invalid"
_COMMIT_TIMESTAMP = "@0 +0000"
_COMMIT_MESSAGE = "benchmark base"

_REFUSED_NAMES = frozenset({".git", ".traceh"})


@dataclass(frozen=True, slots=True)
class AttemptRepositories:
    """The two repositories one attempt owns, and the revision they share."""

    source: Path
    target: Path
    base_revision: str
    target_ref: str = BENCHMARK_TARGET_REF


async def build_attempt_repositories(
    *, initial_dir: Path, source: Path, target: Path
) -> AttemptRepositories:
    """Materialize one attempt's source repository and its bare target."""

    _copy_initial_tree(initial_dir, source)
    await _git(("init", "--quiet", f"--initial-branch={BENCHMARK_SOURCE_REVISION}"), cwd=source)
    for name, value in (
        ("user.name", _COMMIT_IDENTITY),
        ("user.email", _COMMIT_EMAIL),
        ("commit.gpgsign", "false"),
        # Without this a Windows checkout would produce different blobs from a
        # POSIX one and the D2 review would fail closed on line-ending drift.
        ("core.autocrlf", "false"),
        ("core.eol", "lf"),
    ):
        await _git(("config", name, value), cwd=source)
    await _git(("add", "-A"), cwd=source)
    await _git(("commit", "--quiet", "-m", _COMMIT_MESSAGE), cwd=source)
    base_revision = await _git(("rev-parse", "HEAD"), cwd=source)
    await _git(
        ("clone", "--quiet", "--bare", "--", str(source), str(target)),
        cwd=source.parent,
    )
    for name, value in (
        ("core.autocrlf", "false"),
        ("core.eol", "lf"),
    ):
        await _git(("config", name, value), cwd=target)
    return AttemptRepositories(
        source=source, target=target, base_revision=base_revision
    )


async def read_target_revision(target: Path, ref: str) -> str | None:
    """The exact revision ``ref`` points at right now, or ``None`` if unset."""

    exit_code, _ = await _git_status(("show-ref", "--verify", "--quiet", ref), cwd=target)
    if exit_code != 0:
        return None
    return await _git(("rev-parse", ref), cwd=target)


def _copy_initial_tree(initial_dir: Path, source: Path) -> None:
    """Copy a bounded, link-free tree of regular files, or refuse."""

    source.mkdir(parents=True, exist_ok=False)
    files = 0
    total = 0
    for entry in sorted(_walk(initial_dir)):
        relative = entry.relative_to(initial_dir)
        if any(part in _REFUSED_NAMES for part in relative.parts):
            raise BenchmarkExecutionError("benchmark-initial-tree-refused")
        if _is_reparse(entry) or not entry.is_file():
            raise BenchmarkExecutionError("benchmark-initial-tree-refused")
        size = entry.stat().st_size
        files += 1
        total += size
        if (
            files > MAX_INITIAL_FILES
            or size > MAX_INITIAL_FILE_BYTES
            or total > MAX_INITIAL_TOTAL_BYTES
        ):
            raise BenchmarkExecutionError("benchmark-initial-tree-too-large")
        destination = source / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(entry, destination)
    if files == 0:
        raise BenchmarkExecutionError("benchmark-initial-tree-empty")


def _walk(root: Path):
    for parent, directories, names in os.walk(root):
        parent_path = Path(parent)
        if _is_reparse(parent_path) and parent_path != root:
            raise BenchmarkExecutionError("benchmark-initial-tree-refused")
        for directory in directories:
            if directory in _REFUSED_NAMES:
                raise BenchmarkExecutionError("benchmark-initial-tree-refused")
        for name in names:
            yield parent_path / name


def _is_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(os.path, "isjunction", None)
        if is_junction is not None and is_junction(path):
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        return bool(attributes & marker)
    except OSError:
        return True


async def _git(argv: tuple[str, ...], *, cwd: Path) -> str:
    exit_code, stdout = await _git_status(argv, cwd=cwd)
    if exit_code != 0:
        raise BenchmarkExecutionError("benchmark-git-failed")
    try:
        return stdout.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError:
        raise BenchmarkExecutionError("benchmark-git-failed") from None


async def _git_status(argv: tuple[str, ...], *, cwd: Path) -> tuple[int, bytes]:
    with tempfile.TemporaryFile() as capture:
        try:
            spawn = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    "git",
                    *argv,
                    cwd=cwd,
                    env=_git_environment(),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=capture,
                    stderr=asyncio.subprocess.DEVNULL,
                ),
                name="traceh-benchmark-git-spawn",
            )
            try:
                process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                from traceh.concurrency import await_worker_convergence

                await await_worker_convergence(spawn)
                if not spawn.cancelled() and spawn.exception() is None:
                    await converge_process(spawn.result())
                raise
        except (OSError, ValueError):
            raise BenchmarkExecutionError("benchmark-git-unavailable") from None
        try:
            async with asyncio.timeout(GIT_TIMEOUT_SECONDS):
                await process.wait()
        except asyncio.CancelledError:
            await converge_process(process)
            raise
        except TimeoutError:
            interrupted = await converge_process(process)
            if interrupted:
                raise asyncio.CancelledError from None
            raise BenchmarkExecutionError("benchmark-git-timeout") from None
        capture.seek(0, os.SEEK_END)
        if capture.tell() > MAX_GIT_OUTPUT_BYTES:
            raise BenchmarkExecutionError("benchmark-git-failed")
        capture.seek(0)
        return process.returncode or 0, capture.read()


def _git_environment() -> dict[str, str]:
    """Start from nothing the harness did not name for Git itself."""

    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["GIT_AUTHOR_NAME"] = _COMMIT_IDENTITY
    environment["GIT_AUTHOR_EMAIL"] = _COMMIT_EMAIL
    environment["GIT_COMMITTER_NAME"] = _COMMIT_IDENTITY
    environment["GIT_COMMITTER_EMAIL"] = _COMMIT_EMAIL
    environment["GIT_AUTHOR_DATE"] = _COMMIT_TIMESTAMP
    environment["GIT_COMMITTER_DATE"] = _COMMIT_TIMESTAMP
    return environment


__all__ = [
    "MAX_INITIAL_FILES",
    "MAX_INITIAL_FILE_BYTES",
    "MAX_INITIAL_TOTAL_BYTES",
    "AttemptRepositories",
    "build_attempt_repositories",
    "read_target_revision",
]
