"""Contained local Git worktree effects for the managed workspace catalog."""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from traceh.api.workspaces import (
    WorkspaceHandle,
    WorkspaceLocalState,
    WorkspaceRecord,
    WorkspaceSourceSnapshot,
    WorkspaceStatus,
)
from traceh.process_control import converge_process
from traceh.workspaces.errors import (
    WorkspaceDirtyError,
    WorkspaceGitError,
    WorkspaceInputError,
    WorkspacePathError,
    WorkspaceSourceError,
)
from traceh.workspaces.events import require_workspace_identifier

_MAX_GIT_OUTPUT = 1024 * 1024
_DEFAULT_GIT_TIMEOUT_SECONDS = 30.0
@dataclass(frozen=True, slots=True)
class _GitOutcome:
    exit_code: int | None
    stdout: bytes
    timed_out: bool = False
    start_failed: bool = False


class _GitRunner:
    """Direct-child Git runner with cancellation convergence and bounded output."""

    async def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: float,
        index_file: Path | None = None,
    ) -> _GitOutcome:
        with tempfile.TemporaryFile() as capture:
            try:
                spawn = asyncio.create_task(
                    asyncio.create_subprocess_exec(
                        *argv,
                        cwd=cwd,
                        env=_git_environment(index_file),
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=capture,
                        stderr=asyncio.subprocess.DEVNULL,
                    ),
                    name="traceh-workspace-git-spawn",
                )
                try:
                    process = await asyncio.shield(spawn)
                except asyncio.CancelledError as cancellation:
                    from traceh.concurrency import await_worker_convergence

                    await await_worker_convergence(spawn)
                    if not spawn.cancelled() and spawn.exception() is None:
                        await converge_process(spawn.result())
                    raise cancellation
            except (OSError, ValueError):
                return _GitOutcome(None, b"", start_failed=True)

            try:
                async with asyncio.timeout(timeout_seconds):
                    await process.wait()
            except asyncio.CancelledError:
                await converge_process(process)
                raise
            except TimeoutError:
                interrupted = await converge_process(process)
                if interrupted:
                    raise asyncio.CancelledError from None
                return _GitOutcome(process.returncode, b"", timed_out=True)

            capture.seek(0, os.SEEK_END)
            size = capture.tell()
            if size > _MAX_GIT_OUTPUT:
                return _GitOutcome(process.returncode, b"", start_failed=True)
            capture.seek(0)
            return _GitOutcome(process.returncode, capture.read())


def _git_environment(index_file: Path | None = None) -> dict[str, str]:
    environment = dict(os.environ)
    for key in tuple(environment):
        if key.upper().startswith("GIT_"):
            environment.pop(key, None)
    if index_file is not None:
        environment["GIT_INDEX_FILE"] = str(index_file)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GCM_INTERACTIVE"] = "Never"
    environment["LC_ALL"] = "C"
    return environment


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


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path.absolute())))


def _path_entry_exists(path: Path) -> bool:
    return os.path.lexists(path)


def _contains(root: Path, child: Path) -> bool:
    try:
        child.relative_to(root)
    except ValueError:
        return False
    return True


def _decode_utf8(value: bytes) -> str:
    try:
        return value.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise WorkspaceGitError from None


def _decode_output_line(value: bytes) -> str:
    text = _decode_utf8(value)
    if text.endswith("\n"):
        text = text[:-1]
        if text.endswith("\r"):
            text = text[:-1]
    if not text or "\n" in text or "\r" in text or "\0" in text:
        raise WorkspaceGitError
    return text


def _has_reparse_component(path: Path) -> bool:
    if not path.is_absolute():
        return True
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if os.path.lexists(current) and _is_reparse(current):
            return True
    return False


class LocalGitWorkspaceProvider:
    """Create and remove only catalogued worktrees below one managed root.

    Source paths are explicit host configuration.  A ``source_id`` from a
    model or event never becomes a path segment, and worktree paths are derived
    solely from validated framework-generated workspace ids.
    """

    __slots__ = (
        "_git",
        "_managed_root",
        "_runner",
        "_sources",
        "_timeout_seconds",
    )

    def __init__(
        self,
        *,
        managed_root: Path,
        sources: Mapping[str, Path],
        git_executable: str = "git",
        timeout_seconds: float = _DEFAULT_GIT_TIMEOUT_SECONDS,
    ) -> None:
        try:
            root_input = Path(managed_root)
            source_items = tuple(sources.items())
        except Exception:
            raise WorkspaceInputError("workspace-path-invalid", "managed_root") from None
        if not root_input.is_absolute():
            raise WorkspaceInputError("workspace-path-invalid", "managed_root")
        root = root_input.absolute()
        if type(git_executable) is not str or not git_executable.strip():
            raise WorkspaceInputError("workspace-git-invalid", "git_executable")
        if (
            type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise WorkspaceInputError("workspace-timeout-invalid", "timeout_seconds")
        configured: dict[str, Path] = {}
        for source_id, source_path in source_items:
            source_id = require_workspace_identifier(source_id, field="source_id")
            if source_id in configured:
                raise WorkspaceInputError("workspace-source-duplicate", "sources")
            try:
                source = Path(source_path)
            except Exception:
                raise WorkspaceInputError("workspace-path-invalid", "sources") from None
            if not source.is_absolute():
                raise WorkspaceInputError("workspace-path-invalid", "sources")
            configured[source_id] = source.absolute()
        if not configured:
            raise WorkspaceInputError("workspace-source-missing", "sources")
        self._managed_root = root
        self._sources = configured
        self._git = git_executable
        self._timeout_seconds = float(timeout_seconds)
        self._runner = _GitRunner()

    @property
    def managed_root(self) -> Path:
        return self._managed_root

    async def resolve_source(
        self, source_id: str, revision: str
    ) -> WorkspaceSourceSnapshot:
        source_id = require_workspace_identifier(source_id, field="source_id")
        revision = require_workspace_identifier(revision, field="revision")
        source, common_dir = await self._source_context(source_id)
        await self._require_source_clean(source)
        commit = await self._capture_one(
            self._command(
                "-C",
                str(source),
                "rev-parse",
                "--verify",
                "--end-of-options",
                f"{revision}^{{commit}}",
            ),
            cwd=source,
        )
        if not _is_object_id(commit):
            raise WorkspaceSourceError
        fingerprint = hashlib.sha256(
            _path_key(common_dir).encode("utf-8")
        ).hexdigest()
        return WorkspaceSourceSnapshot(
            source_id=source_id,
            requested_revision=revision,
            repository_fingerprint=fingerprint,
            base_revision=commit,
        )

    async def materialize(self, record: WorkspaceRecord) -> WorkspaceHandle:
        if record.status in {
            WorkspaceStatus.QUARANTINED,
            WorkspaceStatus.RELEASED,
        }:
            raise WorkspacePathError
        state = await self.inspect(record)
        if state is WorkspaceLocalState.MISSING:
            source, common_dir = await self._source_context(record.source_id)
            if self._fingerprint(common_dir) != record.repository_fingerprint:
                raise WorkspaceSourceError
            commit = await self._capture_one(
                self._command(
                    "-C",
                    str(source),
                    "rev-parse",
                    "--verify",
                    "--end-of-options",
                    f"{record.base_revision}^{{commit}}",
                ),
                cwd=source,
            )
            if commit != record.base_revision:
                raise WorkspaceSourceError
            await self._require_source_clean(source)
            self._prepare_managed_root(source)
            target = self._target(record.workspace_id)
            outcome = await self._runner.run(
                self._command(
                    "-C",
                    str(source),
                    "worktree",
                    "add",
                    "--detach",
                    str(target),
                    record.base_revision,
                ),
                cwd=source,
                timeout_seconds=self._timeout_seconds,
            )
            if outcome.start_failed or outcome.timed_out or outcome.exit_code != 0:
                converged = await self.inspect(record)
                if converged not in {
                    WorkspaceLocalState.CLEAN,
                    WorkspaceLocalState.DIRTY,
                }:
                    raise WorkspaceGitError
            state = await self.inspect(record)
        if state not in {WorkspaceLocalState.CLEAN, WorkspaceLocalState.DIRTY}:
            raise WorkspacePathError
        if (
            state is WorkspaceLocalState.DIRTY
            and record.status is WorkspaceStatus.PROVISIONAL
        ):
            raise WorkspaceDirtyError
        return self._handle(record)

    async def inspect(self, record: WorkspaceRecord) -> WorkspaceLocalState:
        try:
            target = self._target(record.workspace_id)
            source, common_dir = await self._source_context(record.source_id)
            if self._fingerprint(common_dir) != record.repository_fingerprint:
                return WorkspaceLocalState.UNSAFE
            entries = await self._worktree_entries(source)
            entry = entries.get(_path_key(target))
            exists = _path_entry_exists(target)
            if entry is None:
                return (
                    WorkspaceLocalState.UNSAFE
                    if exists
                    else WorkspaceLocalState.MISSING
                )
            if not exists or not target.is_dir() or _is_reparse(target):
                return WorkspaceLocalState.UNSAFE
            git_marker = target / ".git"
            if (
                not git_marker.is_file()
                or git_marker.is_symlink()
                or _is_reparse(git_marker)
            ):
                return WorkspaceLocalState.UNSAFE
            registered_admin = self._registered_admin_directory(common_dir, target)
            linked_admin = await self._capture_path(
                self._command(
                    "-C",
                    str(target),
                    "rev-parse",
                    "--path-format=absolute",
                    "--absolute-git-dir",
                ),
                cwd=target,
            )
            if (
                _path_key(linked_admin) != _path_key(registered_admin)
                or _has_reparse_component(linked_admin)
            ):
                return WorkspaceLocalState.UNSAFE
            linked_common = await self._capture_path(
                self._command(
                    "-C",
                    str(target),
                    "rev-parse",
                    "--path-format=absolute",
                    "--git-common-dir",
                ),
                cwd=target,
            )
            if self._fingerprint(linked_common) != record.repository_fingerprint:
                return WorkspaceLocalState.UNSAFE
            head = await self._capture_one(
                self._command(
                    "-C",
                    str(target),
                    "rev-parse",
                    "--verify",
                    "HEAD^{commit}",
                ),
                cwd=target,
            )
            if not _is_object_id(head) or entry != head:
                return WorkspaceLocalState.UNSAFE
            status = await self._run_required(
                self._command(
                    "-C",
                    str(target),
                    "status",
                    "--porcelain=v1",
                    "-z",
                    "--untracked-files=all",
                ),
                cwd=target,
            )
            if status or head != record.base_revision:
                return WorkspaceLocalState.DIRTY
            return WorkspaceLocalState.CLEAN
        except (WorkspaceGitError, WorkspacePathError, WorkspaceSourceError):
            return WorkspaceLocalState.UNSAFE

    async def remove(self, record: WorkspaceRecord) -> None:
        state = await self.inspect(record)
        if state is WorkspaceLocalState.MISSING:
            return
        if state is WorkspaceLocalState.DIRTY:
            raise WorkspaceDirtyError
        if state is not WorkspaceLocalState.CLEAN:
            raise WorkspacePathError
        source, _ = await self._source_context(record.source_id)
        target = self._target(record.workspace_id)
        outcome = await self._runner.run(
            self._command(
                "-C",
                str(source),
                "worktree",
                "remove",
                str(target),
            ),
            cwd=source,
            timeout_seconds=self._timeout_seconds,
        )
        if outcome.start_failed or outcome.timed_out or outcome.exit_code != 0:
            if await self.inspect(record) is WorkspaceLocalState.MISSING:
                return
            raise WorkspaceGitError
        if await self.inspect(record) is not WorkspaceLocalState.MISSING:
            raise WorkspaceGitError

    async def remove_captured(
        self, record: WorkspaceRecord, *, candidate_tree: str
    ) -> None:
        """Remove only the exact dirty tree already frozen as an Artifact.

        ``--force`` is safe only after the registered worktree identity and the
        complete Git-visible tree have both been re-derived.  A later edit is a
        different tree and remains quarantinable evidence rather than data this
        method is allowed to discard.
        """

        if type(candidate_tree) is not str or not _is_object_id(candidate_tree):
            raise WorkspaceInputError("workspace-candidate-tree-invalid", "candidate_tree")
        state = await self.inspect(record)
        if state is WorkspaceLocalState.MISSING:
            return
        if state not in {WorkspaceLocalState.CLEAN, WorkspaceLocalState.DIRTY}:
            raise WorkspacePathError
        target = self._target(record.workspace_id)
        first = await self._candidate_tree(target)
        second = await self._candidate_tree(target)
        if first != candidate_tree or second != first:
            raise WorkspaceDirtyError
        source, _ = await self._source_context(record.source_id)
        outcome = await self._runner.run(
            self._command(
                "-C", str(source), "worktree", "remove", "--force", str(target)
            ),
            cwd=source,
            timeout_seconds=self._timeout_seconds,
        )
        if outcome.start_failed or outcome.timed_out or outcome.exit_code != 0:
            if await self.inspect(record) is WorkspaceLocalState.MISSING:
                return
            raise WorkspaceGitError
        if await self.inspect(record) is not WorkspaceLocalState.MISSING:
            raise WorkspaceGitError

    async def _candidate_tree(self, target: Path) -> str:
        with tempfile.TemporaryDirectory(prefix="traceh-workspace-index-") as raw:
            index = Path(raw) / "index"
            await self._run_required(
                self._command("-C", str(target), "read-tree", "HEAD"),
                cwd=target,
                index_file=index,
            )
            await self._run_required(
                self._command("-C", str(target), "add", "-A", "--", "."),
                cwd=target,
                index_file=index,
            )
            return await self._capture_one(
                self._command("-C", str(target), "write-tree"),
                cwd=target,
                index_file=index,
            )

    def _handle(self, record: WorkspaceRecord) -> WorkspaceHandle:
        return WorkspaceHandle(
            workspace_id=record.workspace_id,
            root=self._target(record.workspace_id),
            source_id=record.source_id,
            base_revision=record.base_revision,
            access=record.access,
            status=record.status,
            owner_agent_id=record.owner_agent_id,
            agent_id=record.agent_id,
            session_id=record.session_id,
        )

    def _target(self, workspace_id: str) -> Path:
        workspace_id = require_workspace_identifier(
            workspace_id, field="workspace_id"
        )
        target = (self._managed_root / workspace_id).absolute()
        if target.parent != self._managed_root or not _contains(
            self._managed_root, target
        ):
            raise WorkspacePathError
        if self._managed_root.exists() and _is_reparse(self._managed_root):
            raise WorkspacePathError
        if _path_entry_exists(target) and _is_reparse(target):
            raise WorkspacePathError
        return target

    def _prepare_managed_root(self, source: Path) -> None:
        source_key = Path(_path_key(source))
        root_key = Path(_path_key(self._managed_root))
        if source_key == root_key or _contains(source_key, root_key) or _contains(
            root_key, source_key
        ):
            raise WorkspacePathError
        if _has_reparse_component(self._managed_root):
            raise WorkspacePathError
        try:
            self._managed_root.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise WorkspacePathError from None
        if (
            not self._managed_root.is_dir()
            or _has_reparse_component(self._managed_root)
        ):
            raise WorkspacePathError

    async def _source_context(self, source_id: str) -> tuple[Path, Path]:
        source = self._sources.get(source_id)
        if source is None or not source.exists() or not source.is_dir():
            raise WorkspaceSourceError
        if _has_reparse_component(source):
            raise WorkspaceSourceError
        top = await self._capture_path(
            self._command("-C", str(source), "rev-parse", "--show-toplevel"),
            cwd=source,
        )
        if _path_key(top) != _path_key(source):
            raise WorkspaceSourceError
        common = await self._capture_path(
            self._command(
                "-C",
                str(source),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ),
            cwd=source,
        )
        if not common.exists() or _has_reparse_component(common):
            raise WorkspaceSourceError
        self._prepare_managed_root(source)
        return source, common

    async def _worktree_entries(self, source: Path) -> dict[str, str]:
        raw = await self._run_required(
            self._command(
                "-C",
                str(source),
                "worktree",
                "list",
                "--porcelain",
                "-z",
            ),
            cwd=source,
        )
        entries: dict[str, str] = {}
        path: str | None = None
        head: str | None = None
        for token in raw.split(b"\0"):
            if not token:
                if path is not None:
                    if head is None or not _is_object_id(head):
                        raise WorkspaceGitError
                    key = _path_key(Path(path))
                    if key in entries:
                        raise WorkspaceGitError
                    entries[key] = head
                path = None
                head = None
                continue
            if token.startswith(b"worktree "):
                if path is not None:
                    raise WorkspaceGitError
                path = _decode_utf8(token[len(b"worktree ") :])
                if (
                    not path
                    or "\n" in path
                    or "\r" in path
                    or not Path(path).is_absolute()
                ):
                    raise WorkspaceGitError
            elif token.startswith(b"HEAD "):
                head = _decode_utf8(token[len(b"HEAD ") :])
        if path is not None:
            raise WorkspaceGitError
        return entries

    @staticmethod
    def _registered_admin_directory(common_dir: Path, target: Path) -> Path:
        """Resolve the one registry entry that points back to ``target/.git``.

        ``git worktree list`` identifies the checkout path but does not expose
        its administration directory.  Both directions are required: the
        registry entry must point to this target, and the target marker must
        resolve back to that exact entry.  Otherwise swapping two valid marker
        files from the same repository would pass common-dir and HEAD checks.
        """

        worktrees_root = common_dir / "worktrees"
        if (
            not worktrees_root.is_dir()
            or _is_reparse(worktrees_root)
            or _has_reparse_component(worktrees_root)
        ):
            raise WorkspaceGitError
        expected_marker = _path_key(target / ".git")
        matches: list[Path] = []
        try:
            candidates = tuple(worktrees_root.iterdir())
        except OSError:
            raise WorkspaceGitError from None
        for candidate in candidates:
            if not candidate.is_dir() or _is_reparse(candidate):
                raise WorkspaceGitError
            pointer = candidate / "gitdir"
            try:
                if (
                    not pointer.is_file()
                    or pointer.is_symlink()
                    or _is_reparse(pointer)
                ):
                    raise WorkspaceGitError
                with pointer.open("rb") as stream:
                    raw_pointer = stream.read(_MAX_GIT_OUTPUT + 1)
                if len(raw_pointer) > _MAX_GIT_OUTPUT:
                    raise WorkspaceGitError
                linked_marker = Path(_decode_output_line(raw_pointer))
            except WorkspaceGitError:
                raise
            except (OSError, ValueError):
                raise WorkspaceGitError from None
            if not linked_marker.is_absolute():
                raise WorkspaceGitError
            if _path_key(linked_marker) == expected_marker:
                matches.append(candidate.absolute())
        if len(matches) != 1:
            raise WorkspaceGitError
        return matches[0]

    async def _capture_path(self, argv: tuple[str, ...], *, cwd: Path) -> Path:
        value = await self._capture_one(argv, cwd=cwd)
        path = Path(value)
        if not path.is_absolute():
            raise WorkspaceGitError
        return path

    async def _require_source_clean(self, source: Path) -> None:
        status = await self._run_required(
            self._command(
                "-C",
                str(source),
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ),
            cwd=source,
        )
        if status:
            raise WorkspaceSourceError

    async def _capture_one(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        index_file: Path | None = None,
    ) -> str:
        raw = await self._run_required(argv, cwd=cwd, index_file=index_file)
        if b"\0" in raw:
            raise WorkspaceGitError
        return _decode_output_line(raw)

    def _command(self, *arguments: str) -> tuple[str, ...]:
        # Worktree creation normally runs repository hooks.  Managed Git
        # plumbing must not execute a repository's post-checkout hook merely
        # because the host selected that repository as a source.
        return (self._git, "-c", "core.hooksPath=/dev/null", *arguments)

    async def _run_required(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        index_file: Path | None = None,
    ) -> bytes:
        outcome = await self._runner.run(
            argv,
            cwd=cwd,
            timeout_seconds=self._timeout_seconds,
            index_file=index_file,
        )
        if outcome.start_failed or outcome.timed_out or outcome.exit_code != 0:
            raise WorkspaceGitError
        return outcome.stdout

    @staticmethod
    def _fingerprint(common_dir: Path) -> str:
        return hashlib.sha256(_path_key(common_dir).encode("utf-8")).hexdigest()


def _is_object_id(value: object) -> bool:
    return (
        type(value) is str
        and len(value) in (40, 64)
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = ["LocalGitWorkspaceProvider"]
