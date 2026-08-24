"""Deterministic full-worktree Patch capture through a temporary Git index."""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from traceh.api.artifacts import PatchCaptureLimits
from traceh.artifacts.errors import ArtifactGitError, ArtifactInputError
from traceh.artifacts.manifest import (
    MAX_PROTOCOL_CHANGED_PATHS,
    MAX_PROTOCOL_PATH_BYTES,
    freeze_capture_limits,
    freeze_changed_paths,
    require_hex_digest,
)
from traceh.concurrency import await_worker_convergence
from traceh.process_control import converge_process

_DEFAULT_GIT_TIMEOUT_SECONDS = 30.0
_SMALL_OUTPUT_LIMIT = 1024 * 1024


@dataclass(frozen=True, slots=True)
class GitPatchSnapshot:
    workspace_head_revision: str
    candidate_tree: str
    changed_paths: tuple[str, ...]
    patch_bytes: bytes
    total_file_bytes: int


@dataclass(frozen=True, slots=True)
class _GitOutcome:
    exit_code: int | None
    stdout: bytes
    timed_out: bool = False
    start_failed: bool = False
    output_exceeded: bool = False


@dataclass(frozen=True, slots=True)
class _ChangedEntry:
    old_mode: str
    new_mode: str
    new_object_id: str
    status: str
    path: str


class _GitRunner:
    async def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: float,
        max_output_bytes: int,
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
                    name="traceh-artifact-git-spawn",
                )
                try:
                    process = await asyncio.shield(spawn)
                except asyncio.CancelledError as cancellation:
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
            if size > max_output_bytes:
                return _GitOutcome(
                    process.returncode, b"", output_exceeded=True
                )
            capture.seek(0)
            return _GitOutcome(process.returncode, capture.read())


class GitPatchBuilder:
    """Capture all Git-visible changes without touching the original index."""

    __slots__ = ("_git", "_runner", "_timeout_seconds")

    def __init__(
        self,
        *,
        git_executable: str = "git",
        timeout_seconds: float = _DEFAULT_GIT_TIMEOUT_SECONDS,
    ) -> None:
        if (
            type(git_executable) is not str
            or not git_executable
            or git_executable != git_executable.strip()
            or "\0" in git_executable
        ):
            raise ArtifactInputError("artifact-git-executable-invalid", "git")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ArtifactInputError("artifact-git-timeout-invalid", "timeout")
        self._git = git_executable
        self._timeout_seconds = float(timeout_seconds)
        self._runner = _GitRunner()

    async def capture(
        self,
        workspace_root: Path,
        *,
        base_revision: str,
        repository_fingerprint: str,
        limits: PatchCaptureLimits,
    ) -> GitPatchSnapshot:
        limits = freeze_capture_limits(limits)
        base_revision = require_hex_digest(
            base_revision, lengths=(40, 64), field="base-revision"
        )
        repository_fingerprint = require_hex_digest(
            repository_fingerprint,
            lengths=(64,),
            field="repository-fingerprint",
        )
        try:
            root = _absolute_path(workspace_root)
        except Exception:
            raise ArtifactInputError("artifact-workspace-path-invalid", "workspace") from None
        if not root.is_absolute() or not root.is_dir() or _has_reparse_component(root):
            raise ArtifactGitError("artifact-workspace-path-unsafe")

        _require_no_reparse_entries(root)
        identity_before = await self._workspace_identity(root)
        if identity_before[2] != repository_fingerprint:
            raise ArtifactGitError("artifact-repository-mismatch")
        index_before = _file_fingerprint(identity_before[1])
        first = await self._capture_once(root, base_revision, limits)
        _require_no_reparse_entries(root)
        identity_middle = await self._workspace_identity(root)
        index_middle = _file_fingerprint(identity_middle[1])
        if identity_middle != identity_before or index_middle != index_before:
            raise ArtifactGitError("artifact-workspace-drift")
        second = await self._capture_once(root, base_revision, limits)
        _require_no_reparse_entries(root)
        identity_after = await self._workspace_identity(root)
        index_after = _file_fingerprint(identity_after[1])
        if identity_after != identity_before or index_after != index_before:
            raise ArtifactGitError("artifact-workspace-drift")
        if second != first:
            raise ArtifactGitError("artifact-workspace-drift")
        return first

    async def _capture_once(
        self,
        root: Path,
        base_revision: str,
        limits: PatchCaptureLimits,
    ) -> GitPatchSnapshot:
        head = await self._capture_oid(root, "rev-parse", "--verify", "HEAD^{commit}")
        with tempfile.TemporaryDirectory(prefix="traceh-patch-index-") as raw:
            index_file = _absolute_path(raw) / "index"
            await self._run_required(
                root,
                "read-tree",
                head,
                index_file=index_file,
                max_output_bytes=_SMALL_OUTPUT_LIMIT,
            )
            await self._run_required(
                root,
                "add",
                "-A",
                "--",
                ".",
                index_file=index_file,
                max_output_bytes=_SMALL_OUTPUT_LIMIT,
            )
            candidate_tree = await self._capture_oid(
                root, "write-tree", index_file=index_file
            )
            await self._validate_candidate_tree(
                root, candidate_tree, index_file=index_file
            )
            raw_limit = min(
                1024 * 1024 * 1024,
                limits.max_changed_paths * (limits.max_path_bytes + 256),
            )
            raw_diff = await self._run_required(
                root,
                "diff-tree",
                "--no-commit-id",
                "--raw",
                "-z",
                "--no-renames",
                "--no-abbrev",
                base_revision,
                candidate_tree,
                "--",
                index_file=index_file,
                max_output_bytes=raw_limit,
            )
            entries = _parse_raw_diff(raw_diff)
            try:
                changed_paths = freeze_changed_paths(
                    tuple(entry.path for entry in entries),
                    max_paths=limits.max_changed_paths,
                    max_path_bytes=limits.max_path_bytes,
                )
            except ArtifactInputError as error:
                raise ArtifactGitError(error.code) from None
            total_file_bytes = await self._validate_entries(
                root, entries, limits, index_file=index_file
            )
            patch = await self._run_required(
                root,
                "diff-tree",
                "--no-commit-id",
                "--binary",
                "--full-index",
                "--no-renames",
                "--no-ext-diff",
                "--no-textconv",
                "--no-color",
                "--src-prefix=a/",
                "--dst-prefix=b/",
                base_revision,
                candidate_tree,
                "--",
                index_file=index_file,
                max_output_bytes=limits.max_patch_bytes,
                output_code="artifact-patch-size-exceeded",
            )
        return GitPatchSnapshot(
            workspace_head_revision=head,
            candidate_tree=candidate_tree,
            changed_paths=changed_paths,
            patch_bytes=patch,
            total_file_bytes=total_file_bytes,
        )

    async def _validate_entries(
        self,
        root: Path,
        entries: tuple[_ChangedEntry, ...],
        limits: PatchCaptureLimits,
        *,
        index_file: Path,
    ) -> int:
        total = 0
        for entry in entries:
            for mode in (entry.old_mode, entry.new_mode):
                if mode in {"120000", "160000"}:
                    raise ArtifactGitError("artifact-special-git-object-rejected")
                if mode not in {"000000", "100644", "100755"}:
                    raise ArtifactGitError("artifact-git-mode-rejected")
            if entry.new_mode == "000000":
                continue
            raw_size = await self._run_required(
                root,
                "cat-file",
                "-s",
                entry.new_object_id,
                index_file=index_file,
                max_output_bytes=128,
            )
            size_text = _decode_line(raw_size)
            try:
                size = int(size_text)
            except ValueError:
                raise ArtifactGitError from None
            if size < 0 or size > limits.max_file_bytes:
                raise ArtifactGitError("artifact-file-size-exceeded")
            total += size
            if total > limits.max_total_file_bytes:
                raise ArtifactGitError("artifact-total-size-exceeded")
        return total

    async def _validate_candidate_tree(
        self,
        root: Path,
        candidate_tree: str,
        *,
        index_file: Path,
    ) -> None:
        raw_limit = min(
            1024 * 1024 * 1024,
            MAX_PROTOCOL_CHANGED_PATHS * (MAX_PROTOCOL_PATH_BYTES + 128),
        )
        raw = await self._run_required(
            root,
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            candidate_tree,
            index_file=index_file,
            max_output_bytes=raw_limit,
        )
        paths: list[str] = []
        if raw:
            records = raw.split(b"\0")
            if records[-1] != b"":
                raise ArtifactGitError("artifact-git-tree-invalid")
            records.pop()
            for record in records:
                header, separator, raw_path = record.partition(b"\t")
                parts = header.split(b" ")
                if not separator or len(parts) != 3:
                    raise ArtifactGitError("artifact-git-tree-invalid")
                mode, object_type, object_id = parts
                if mode in {b"120000", b"160000"}:
                    raise ArtifactGitError("artifact-special-git-object-rejected")
                if mode not in {b"100644", b"100755"} or object_type != b"blob":
                    raise ArtifactGitError("artifact-git-mode-rejected")
                if len(object_id) not in (40, 64):
                    raise ArtifactGitError("artifact-git-tree-invalid")
                try:
                    path = raw_path.decode("utf-8", errors="strict")
                except UnicodeError:
                    raise ArtifactGitError("artifact-git-tree-invalid") from None
                paths.append(path)
        try:
            freeze_changed_paths(
                tuple(paths),
                max_paths=MAX_PROTOCOL_CHANGED_PATHS,
                max_path_bytes=MAX_PROTOCOL_PATH_BYTES,
            )
        except ArtifactInputError as error:
            raise ArtifactGitError(error.code) from None

    async def _workspace_identity(self, root: Path) -> tuple[str, Path, str]:
        top = _absolute_path(
            await self._capture_line(
                root, "rev-parse", "--path-format=absolute", "--show-toplevel"
            )
        )
        if _path_key(top) != _path_key(root):
            raise ArtifactGitError("artifact-workspace-path-unsafe")
        admin = _absolute_path(
            await self._capture_line(
                root, "rev-parse", "--path-format=absolute", "--absolute-git-dir"
            )
        )
        common = _absolute_path(
            await self._capture_line(
                root, "rev-parse", "--path-format=absolute", "--git-common-dir"
            )
        )
        index = _absolute_path(
            await self._capture_line(
                root, "rev-parse", "--path-format=absolute", "--git-path", "index"
            )
        )
        if (
            not admin.is_dir()
            or not common.is_dir()
            or _has_reparse_component(admin)
            or _has_reparse_component(common)
            or _has_reparse_component(index.parent)
            or not _contains(admin, index)
        ):
            raise ArtifactGitError("artifact-workspace-path-unsafe")
        fingerprint = hashlib.sha256(_path_key(common).encode("utf-8")).hexdigest()
        return _path_key(admin), index, fingerprint

    async def _capture_oid(
        self,
        root: Path,
        *arguments: str,
        index_file: Path | None = None,
    ) -> str:
        value = await self._capture_line(
            root, *arguments, index_file=index_file
        )
        if (
            len(value) not in (40, 64)
            or value != value.lower()
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ArtifactGitError
        return value

    async def _capture_line(
        self,
        root: Path,
        *arguments: str,
        index_file: Path | None = None,
    ) -> str:
        return _decode_line(
            await self._run_required(
                root,
                *arguments,
                index_file=index_file,
                max_output_bytes=_SMALL_OUTPUT_LIMIT,
            )
        )

    async def _run_required(
        self,
        root: Path,
        *arguments: str,
        index_file: Path | None,
        max_output_bytes: int,
        output_code: str = "artifact-git-output-exceeded",
    ) -> bytes:
        outcome = await self._runner.run(
            (
                self._git,
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.quotePath=false",
                "-C",
                str(root),
                *arguments,
            ),
            cwd=root,
            timeout_seconds=self._timeout_seconds,
            max_output_bytes=max_output_bytes,
            index_file=index_file,
        )
        if outcome.output_exceeded:
            raise ArtifactGitError(output_code)
        if outcome.start_failed or outcome.timed_out or outcome.exit_code != 0:
            raise ArtifactGitError
        return outcome.stdout


def _parse_raw_diff(raw: bytes) -> tuple[_ChangedEntry, ...]:
    if not raw:
        return ()
    tokens = raw.split(b"\0")
    if tokens[-1] != b"":
        raise ArtifactGitError("artifact-git-diff-invalid")
    tokens.pop()
    if len(tokens) % 2 != 0:
        raise ArtifactGitError("artifact-git-diff-invalid")
    entries: list[_ChangedEntry] = []
    for offset in range(0, len(tokens), 2):
        header = tokens[offset]
        raw_path = tokens[offset + 1]
        if not header.startswith(b":"):
            raise ArtifactGitError("artifact-git-diff-invalid")
        parts = header[1:].split(b" ")
        if len(parts) != 5:
            raise ArtifactGitError("artifact-git-diff-invalid")
        old_mode, new_mode, old_oid, new_oid, status = parts
        if status not in {b"A", b"D", b"M", b"T"}:
            raise ArtifactGitError("artifact-git-diff-invalid")
        if len(old_oid) not in (40, 64) or len(new_oid) != len(old_oid):
            raise ArtifactGitError("artifact-git-diff-invalid")
        try:
            path = raw_path.decode("utf-8", errors="strict")
            old_mode_text = old_mode.decode("ascii", errors="strict")
            new_mode_text = new_mode.decode("ascii", errors="strict")
            new_oid_text = new_oid.decode("ascii", errors="strict")
            status_text = status.decode("ascii", errors="strict")
        except UnicodeError:
            raise ArtifactGitError("artifact-git-diff-invalid") from None
        entries.append(
            _ChangedEntry(
                old_mode=old_mode_text,
                new_mode=new_mode_text,
                new_object_id=new_oid_text,
                status=status_text,
                path=path,
            )
        )
    return tuple(entries)


def _git_environment(index_file: Path | None) -> dict[str, str]:
    # Git supports many injection variables, including dynamic names and
    # GIT_CONFIG_PARAMETERS. A denylist will inevitably miss one, so inherit
    # no caller-controlled GIT_* value and add back only host-owned controls.
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GCM_INTERACTIVE"] = "Never"
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["LC_ALL"] = "C"
    if index_file is not None:
        environment["GIT_INDEX_FILE"] = str(index_file)
    return environment


def _decode_line(raw: bytes) -> str:
    try:
        value = raw.decode("utf-8", errors="strict")
    except UnicodeError:
        raise ArtifactGitError from None
    if value.endswith("\n"):
        value = value[:-1]
        if value.endswith("\r"):
            value = value[:-1]
    if not value or "\n" in value or "\r" in value or "\0" in value:
        raise ArtifactGitError
    return value


def _file_fingerprint(path: Path) -> tuple[bool, int, str]:
    try:
        if not os.path.lexists(path):
            return False, 0, ""
        if not path.is_file() or _is_reparse(path):
            raise ArtifactGitError("artifact-index-unsafe")
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
        return True, size, digest.hexdigest()
    except ArtifactGitError:
        raise
    except OSError:
        raise ArtifactGitError("artifact-index-unsafe") from None


def _require_no_reparse_entries(root: Path) -> None:
    stack = [root]
    inspected = 0
    while stack:
        current = stack.pop()
        try:
            iterator = os.scandir(current)
        except OSError:
            raise ArtifactGitError("artifact-workspace-path-unsafe") from None
        try:
            with iterator:
                for entry in iterator:
                    inspected += 1
                    if inspected > MAX_PROTOCOL_CHANGED_PATHS:
                        raise ArtifactGitError(
                            "artifact-workspace-entry-limit-exceeded"
                        )
                    path = Path(entry.path)
                    if current == root and entry.name == ".git":
                        if _is_reparse(path) or not entry.is_file(
                            follow_symlinks=False
                        ):
                            raise ArtifactGitError("artifact-workspace-path-unsafe")
                        continue
                    if entry.name.casefold() == ".traceh":
                        raise ArtifactGitError("artifact-control-path-rejected")
                    if _is_reparse(path):
                        raise ArtifactGitError("artifact-special-path-rejected")
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(path)
                    except OSError:
                        raise ArtifactGitError(
                            "artifact-workspace-path-unsafe"
                        ) from None
        except ArtifactGitError:
            raise
        except OSError:
            raise ArtifactGitError("artifact-workspace-path-unsafe") from None


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


def _has_reparse_component(path: Path) -> bool:
    if not path.is_absolute():
        return True
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if os.path.lexists(current) and _is_reparse(current):
            return True
    return False


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path.absolute())))


def _absolute_path(value: object) -> Path:
    return Path(value).absolute()  # type: ignore[arg-type]


def _contains(root: Path, child: Path) -> bool:
    try:
        child.relative_to(root)
    except ValueError:
        return False
    return True


__all__ = ["GitPatchBuilder", "GitPatchSnapshot"]
