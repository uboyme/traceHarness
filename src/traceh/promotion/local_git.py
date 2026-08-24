"""Contained local Git effects for review integration and ref promotion.

Two boundaries live here and they are deliberately different:

* Review builds and verifies an integration commit in a **temporary clone**, so
  a review can never leave a ref, index or worktree side effect on the host's
  real target repository.
* Promotion rebuilds the identical tree and commit inside the target's own
  object database through a **temporary index**, and then moves the ref only
  through ``git update-ref <ref> <new> <expected-old>``.

Nothing here accepts a path, ref, revision or command from a model, a Patch or
a Workspace file; every input arrives from host configuration or an already
validated durable fact.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import shutil
import stat
import tempfile
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from traceh.api.promotion import PromotionTarget, PromotionTargetBinding
from traceh.artifacts.manifest import freeze_changed_paths
from traceh.concurrency import await_worker_convergence
from traceh.process_control import converge_process
from traceh.promotion.cleanup import release_scratch
from traceh.promotion.errors import PromotionGitError, PromotionInputError
from traceh.promotion.models import (
    INTEGRATION_AUTHOR_EMAIL,
    INTEGRATION_AUTHOR_NAME,
    INTEGRATION_TIMESTAMP,
    MAX_INTEGRATION_CHANGED_PATHS,
    MAX_INTEGRATION_WORKTREE_BYTES,
    MAX_PATCH_BYTES,
    require_finite_seconds,
    require_hex_digest,
    require_promotion_identifier,
    require_target_ref,
)

_DEFAULT_GIT_TIMEOUT_SECONDS = 120.0
_SMALL_OUTPUT_LIMIT = 1024 * 1024
_MAX_RAW_DIFF_BYTES = 256 * 1024 * 1024
_ALLOWED_MODES = frozenset({"000000", "100644", "100755"})
_SPECIAL_MODES = frozenset({"120000", "160000"})
_FILESYSTEM_TRACKS_EXECUTABLE_BIT = os.name != "nt"
"""Whether this platform can store Git's executable bit at all.

NTFS cannot, so a checkout of mode ``100755`` reports ``0o666``. This is a
property of the filesystem, not of the repository under review.
"""


@dataclass(frozen=True, slots=True)
class IntegrationBuild:
    """The exact deterministic result of applying one Patch to one revision."""

    integration_tree: str
    integration_commit: str


@dataclass(frozen=True, slots=True)
class _GitOutcome:
    exit_code: int | None
    stdout: bytes
    timed_out: bool = False
    start_failed: bool = False
    output_exceeded: bool = False

    @property
    def ok(self) -> bool:
        return (
            not self.start_failed
            and not self.timed_out
            and not self.output_exceeded
            and self.exit_code == 0
        )


class _GitRunner:
    """Direct-child Git runner with cancellation convergence and bounded output."""

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
                    name="traceh-promotion-git-spawn",
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
                return _GitOutcome(process.returncode, b"", output_exceeded=True)
            capture.seek(0)
            return _GitOutcome(process.returncode, capture.read())


def _git_environment(index_file: Path | None) -> dict[str, str]:
    # Git supports many injection variables, including dynamic names and
    # ``GIT_CONFIG_PARAMETERS``. A denylist will inevitably miss one, so inherit
    # no caller-controlled ``GIT_*`` value and add back only host-owned
    # controls, including the fixed integration commit identity.
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GCM_INTERACTIVE"] = "Never"
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["LC_ALL"] = "C"
    environment["GIT_AUTHOR_NAME"] = INTEGRATION_AUTHOR_NAME
    environment["GIT_AUTHOR_EMAIL"] = INTEGRATION_AUTHOR_EMAIL
    environment["GIT_AUTHOR_DATE"] = INTEGRATION_TIMESTAMP
    environment["GIT_COMMITTER_NAME"] = INTEGRATION_AUTHOR_NAME
    environment["GIT_COMMITTER_EMAIL"] = INTEGRATION_AUTHOR_EMAIL
    environment["GIT_COMMITTER_DATE"] = INTEGRATION_TIMESTAMP
    if index_file is not None:
        environment["GIT_INDEX_FILE"] = str(index_file)
    return environment


class _GitBoundary:
    """Shared argv, decoding and safety rules for every promotion Git call."""

    __slots__ = ("_git", "_runner", "_timeout_seconds")

    def __init__(self, *, git_executable: str, timeout_seconds: float) -> None:
        if (
            type(git_executable) is not str
            or not git_executable
            or git_executable != git_executable.strip()
            or "\0" in git_executable
        ):
            raise PromotionInputError("promotion-git-executable-invalid", "git")
        self._git = git_executable
        self._timeout_seconds = require_finite_seconds(
            timeout_seconds, field="git-timeout"
        )
        self._runner = _GitRunner()

    async def _run(
        self,
        *arguments: str,
        cwd: Path,
        index_file: Path | None = None,
        max_output_bytes: int = _SMALL_OUTPUT_LIMIT,
    ) -> _GitOutcome:
        return await self._runner.run(
            (
                self._git,
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.quotePath=false",
                "-c",
                "gc.auto=0",
                # The checkout must be the tree, byte for byte. Line-ending
                # conversion is configuration this host did not choose - it can
                # come from a global ``core.autocrlf`` on whatever machine runs
                # the review - and it would hand the verifier different bytes
                # from the ones the approval names.
                "-c",
                "core.autocrlf=false",
                "-c",
                "core.eol=lf",
                *arguments,
            ),
            cwd=cwd,
            timeout_seconds=self._timeout_seconds,
            max_output_bytes=max_output_bytes,
            index_file=index_file,
        )

    async def _required(
        self,
        *arguments: str,
        cwd: Path,
        index_file: Path | None = None,
        max_output_bytes: int = _SMALL_OUTPUT_LIMIT,
        code: str = "promotion-git-failed",
    ) -> bytes:
        outcome = await self._run(
            *arguments,
            cwd=cwd,
            index_file=index_file,
            max_output_bytes=max_output_bytes,
        )
        if not outcome.ok:
            raise PromotionGitError(code)
        return outcome.stdout

    async def _line(
        self,
        *arguments: str,
        cwd: Path,
        index_file: Path | None = None,
        code: str = "promotion-git-failed",
    ) -> str:
        return _decode_line(
            await self._required(
                *arguments, cwd=cwd, index_file=index_file, code=code
            )
        )

    async def _object_id(
        self,
        *arguments: str,
        cwd: Path,
        index_file: Path | None = None,
        code: str = "promotion-git-failed",
    ) -> str:
        value = await self._line(
            *arguments, cwd=cwd, index_file=index_file, code=code
        )
        if not _is_object_id(value):
            raise PromotionGitError(code)
        return value

    async def _read_ref(self, repository: Path, ref: str) -> str | None:
        """Read one exact ref, distinguishing "absent" from "unreadable".

        ``show-ref --verify`` exits 128 for a missing ref, which is also its
        generic fatal status. The quiet form answers existence with a dedicated
        exit code, so absence is never inferred from a generic failure.
        """

        presence = await self._run(
            "-C",
            str(repository),
            "show-ref",
            "--verify",
            "--quiet",
            "--",
            ref,
            cwd=repository,
        )
        if presence.exit_code == 1 and not presence.stdout:
            return None
        if not presence.ok:
            raise PromotionGitError("promotion-target-ref-unreadable")
        raw = await self._required(
            "-C",
            str(repository),
            "show-ref",
            "--verify",
            "--",
            ref,
            cwd=repository,
            code="promotion-target-ref-unreadable",
        )
        value, separator, name = _decode_line(raw).partition(" ")
        if not separator or name != ref or not _is_object_id(value):
            raise PromotionGitError("promotion-target-ref-unreadable")
        return value


class LocalBareGitPromotionTargets(_GitBoundary):
    """Resolve host-configured target ids into exact bare repositories.

    D2 v1 only promotes into a bare repository. A normal checkout has a working
    tree and an index that some other process may be using, and moving its
    branch behind that process' back is not a compare-and-swap on a fact - it is
    a surprise for whoever is standing in the directory.
    """

    __slots__ = ("_bindings",)

    def __init__(
        self,
        *,
        targets: Mapping[str, PromotionTargetBinding],
        git_executable: str = "git",
        timeout_seconds: float = _DEFAULT_GIT_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(
            git_executable=git_executable, timeout_seconds=timeout_seconds
        )
        try:
            items = tuple(targets.items())
        except Exception:
            raise PromotionInputError("promotion-targets-invalid", "targets") from None
        bindings: dict[str, PromotionTargetBinding] = {}
        for target_id, binding in items:
            target_id = require_promotion_identifier(target_id, field="target_id")
            if target_id in bindings:
                raise PromotionInputError("promotion-target-duplicate", "targets")
            if type(binding) is not PromotionTargetBinding:
                raise PromotionInputError("promotion-targets-invalid", "targets")
            require_target_ref(binding.target_ref)
            try:
                path = Path(binding.repository_path)
            except Exception:
                raise PromotionInputError(
                    "promotion-target-path-invalid", "targets"
                ) from None
            if not path.is_absolute():
                raise PromotionInputError("promotion-target-path-invalid", "targets")
            bindings[target_id] = PromotionTargetBinding(
                repository_path=path.absolute(), target_ref=binding.target_ref
            )
        if not bindings:
            raise PromotionInputError("promotion-target-missing", "targets")
        self._bindings = bindings

    async def resolve(self, target_id: str) -> PromotionTarget:
        target_id = require_promotion_identifier(target_id, field="target_id")
        binding = self._bindings.get(target_id)
        if binding is None:
            raise PromotionInputError("promotion-target-unknown", "target_id")
        repository = binding.repository_path
        if (
            not repository.is_dir()
            or _has_reparse_component(repository)
        ):
            raise PromotionGitError("promotion-target-path-unsafe")
        bare = await self._line(
            "-C", str(repository), "rev-parse", "--is-bare-repository", cwd=repository
        )
        if bare != "true":
            raise PromotionGitError("promotion-target-not-bare")
        common = _absolute(
            await self._line(
                "-C",
                str(repository),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
                cwd=repository,
            )
        )
        if (
            not common.is_dir()
            or _has_reparse_component(common)
            or _path_key(common) != _path_key(repository)
        ):
            raise PromotionGitError("promotion-target-path-unsafe")
        await self._required(
            "-C",
            str(repository),
            "check-ref-format",
            binding.target_ref,
            cwd=repository,
            code="promotion-target-ref-invalid",
        )
        revision = await self._read_ref(repository, binding.target_ref)
        if revision is None:
            raise PromotionGitError("promotion-target-ref-missing")
        return PromotionTarget(
            target_id=target_id,
            repository_path=repository,
            repository_fingerprint=_fingerprint(common),
            target_ref=binding.target_ref,
            expected_revision=revision,
        )


class IntegrationEnvironment:
    """One temporary clone holding the exact integration commit under review."""

    __slots__ = (
        "_engine",
        "_expected_revision",
        "_message",
        "_root",
        "_worktree_fingerprint",
        "build",
    )

    def __init__(
        self,
        engine: LocalGitPromotionEngine,
        *,
        root: Path,
        expected_revision: str,
        message: str,
        build: IntegrationBuild,
        worktree_fingerprint: str,
    ) -> None:
        self._engine = engine
        self._root = root
        self._expected_revision = expected_revision
        self._message = message
        self._worktree_fingerprint = worktree_fingerprint
        self.build = build

    @property
    def root(self) -> Path:
        return self._root

    @property
    def worktree_fingerprint(self) -> str:
        return self._worktree_fingerprint

    async def reverify(self) -> IntegrationBuild:
        """Re-observe the real bytes, HEAD, the tree and the commit identity."""

        return await self._engine._reverify_integration(
            self._root,
            expected_revision=self._expected_revision,
            message=self._message,
            build=self.build,
            worktree_fingerprint=self._worktree_fingerprint,
        )


class LocalGitPromotionEngine(_GitBoundary):
    """Build, verify and compare-and-swap exactly one approved integration."""

    __slots__ = ()

    def __init__(
        self,
        *,
        git_executable: str = "git",
        timeout_seconds: float = _DEFAULT_GIT_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(
            git_executable=git_executable, timeout_seconds=timeout_seconds
        )

    @contextlib.asynccontextmanager
    async def integration(
        self,
        target: PromotionTarget,
        *,
        patch: bytes,
        message: str,
    ) -> AsyncIterator[IntegrationEnvironment]:
        """Materialise the integration commit in an isolated temporary clone."""

        _require_patch(patch)
        _require_message(message)
        expected_revision = require_hex_digest(
            target.expected_revision, lengths=(40, 64), field="expected-revision"
        )
        async with _owned_temporary_directory() as scratch:
            patch_file = scratch / "patch.diff"
            await asyncio.to_thread(_write_private_file, patch_file, patch)
            repository = scratch / "integration"
            await self._required(
                "clone",
                "--quiet",
                "--no-checkout",
                "--no-tags",
                "--",
                str(target.repository_path),
                str(repository),
                cwd=scratch,
                code="promotion-integration-clone-failed",
            )
            if not repository.is_dir() or _is_reparse(repository):
                raise PromotionGitError("promotion-integration-path-unsafe")
            await self._required(
                "-C",
                str(repository),
                "update-ref",
                "--no-deref",
                "HEAD",
                expected_revision,
                cwd=repository,
                code="promotion-integration-revision-missing",
            )
            build = await self._build(
                repository,
                expected_revision=expected_revision,
                patch_file=patch_file,
                message=message,
                index_file=None,
            )
            await self._required(
                "-C",
                str(repository),
                "update-ref",
                "--no-deref",
                "HEAD",
                build.integration_commit,
                cwd=repository,
            )
            await self._required(
                "-C",
                str(repository),
                "read-tree",
                "--reset",
                build.integration_commit,
                cwd=repository,
            )
            await self._required(
                "-C", str(repository), "checkout-index", "-a", "-f", cwd=repository
            )
            # The verifier must start from a worktree that provably equals the
            # tree under review, otherwise "it passed" describes unknown bytes.
            await self._require_worktree_is_tree(repository, build.integration_tree)
            tree_modes = await self._tree_modes(repository, build.integration_tree)
            fingerprint = (await self._worktree_state(repository, tree_modes))[0]
            yield IntegrationEnvironment(
                self,
                root=repository,
                expected_revision=expected_revision,
                message=message,
                build=build,
                worktree_fingerprint=fingerprint,
            )

    async def rebuild_in_target(
        self,
        target: PromotionTarget,
        *,
        patch: bytes,
        message: str,
        expected_revision: str,
    ) -> IntegrationBuild:
        """Recreate the approved tree and commit inside the target repository.

        A temporary ``GIT_INDEX_FILE`` is used, so the target's own index - if a
        bare repository ever grows one - is never written.
        """

        _require_patch(patch)
        _require_message(message)
        expected_revision = require_hex_digest(
            expected_revision, lengths=(40, 64), field="expected-revision"
        )
        async with _owned_temporary_directory() as scratch:
            patch_file = scratch / "patch.diff"
            await asyncio.to_thread(_write_private_file, patch_file, patch)
            return await self._build(
                target.repository_path,
                expected_revision=expected_revision,
                patch_file=patch_file,
                message=message,
                index_file=scratch / "index",
            )

    async def compare_and_swap(
        self, target: PromotionTarget, *, expected_old: str, new: str
    ) -> bool:
        """The single linearization point of the whole D2 pipeline."""

        expected_old = require_hex_digest(
            expected_old, lengths=(40, 64), field="expected-revision"
        )
        new = require_hex_digest(new, lengths=(40, 64), field="new-revision")
        outcome = await self._run(
            "-C",
            str(target.repository_path),
            "update-ref",
            "--no-deref",
            target.target_ref,
            new,
            expected_old,
            cwd=target.repository_path,
        )
        if outcome.ok:
            return True
        if outcome.start_failed or outcome.timed_out:
            raise PromotionGitError("promotion-ref-update-unknown")
        return False

    async def read_ref(self, target: PromotionTarget) -> str | None:
        return await self._read_ref(target.repository_path, target.target_ref)

    async def require_target_identity(self, target: PromotionTarget) -> None:
        """Re-prove the repository is the exact bare repository under promotion."""

        repository = target.repository_path
        if not repository.is_dir() or _has_reparse_component(repository):
            raise PromotionGitError("promotion-target-path-unsafe")
        bare = await self._line(
            "-C", str(repository), "rev-parse", "--is-bare-repository", cwd=repository
        )
        if bare != "true":
            raise PromotionGitError("promotion-target-not-bare")
        common = _absolute(
            await self._line(
                "-C",
                str(repository),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
                cwd=repository,
            )
        )
        if (
            not common.is_dir()
            or _has_reparse_component(common)
            or _fingerprint(common) != target.repository_fingerprint
        ):
            raise PromotionGitError("promotion-target-identity-changed")

    async def _build(
        self,
        repository: Path,
        *,
        expected_revision: str,
        patch_file: Path,
        message: str,
        index_file: Path | None,
    ) -> IntegrationBuild:
        await self._required(
            "-C",
            str(repository),
            "read-tree",
            expected_revision,
            cwd=repository,
            index_file=index_file,
            code="promotion-integration-revision-missing",
        )
        check = await self._run(
            "-C",
            str(repository),
            "apply",
            "--check",
            "--cached",
            "--binary",
            "--whitespace=nowarn",
            "--",
            str(patch_file),
            cwd=repository,
            index_file=index_file,
        )
        if not check.ok:
            raise PromotionGitError("promotion-patch-apply-conflict")
        applied = await self._run(
            "-C",
            str(repository),
            "apply",
            "--cached",
            "--binary",
            "--whitespace=nowarn",
            "--",
            str(patch_file),
            cwd=repository,
            index_file=index_file,
        )
        if not applied.ok:
            raise PromotionGitError("promotion-patch-apply-conflict")
        tree = await self._object_id(
            "-C",
            str(repository),
            "write-tree",
            cwd=repository,
            index_file=index_file,
            code="promotion-integration-tree-failed",
        )
        await self._validate_changed_entries(
            repository,
            expected_revision=expected_revision,
            tree=tree,
            index_file=index_file,
        )
        commit = await self._object_id(
            "-C",
            str(repository),
            "commit-tree",
            tree,
            "-p",
            expected_revision,
            "-m",
            message,
            cwd=repository,
            index_file=index_file,
            code="promotion-integration-commit-failed",
        )
        return IntegrationBuild(integration_tree=tree, integration_commit=commit)

    async def _validate_changed_entries(
        self,
        repository: Path,
        *,
        expected_revision: str,
        tree: str,
        index_file: Path | None,
    ) -> None:
        raw = await self._required(
            "-C",
            str(repository),
            "diff-tree",
            "--no-commit-id",
            "--raw",
            "-z",
            "--no-renames",
            "--no-abbrev",
            expected_revision,
            tree,
            "--",
            cwd=repository,
            index_file=index_file,
            max_output_bytes=_MAX_RAW_DIFF_BYTES,
            code="promotion-integration-diff-failed",
        )
        if not raw:
            raise PromotionGitError("promotion-patch-empty")
        tokens = raw.split(b"\0")
        if tokens[-1] != b"":
            raise PromotionGitError("promotion-integration-diff-failed")
        tokens.pop()
        if len(tokens) % 2 != 0:
            raise PromotionGitError("promotion-integration-diff-failed")
        paths: list[str] = []
        for offset in range(0, len(tokens), 2):
            header = tokens[offset]
            if not header.startswith(b":"):
                raise PromotionGitError("promotion-integration-diff-failed")
            parts = header[1:].split(b" ")
            if len(parts) != 5:
                raise PromotionGitError("promotion-integration-diff-failed")
            for mode in (parts[0], parts[1]):
                text = mode.decode("ascii", errors="replace")
                if text in _SPECIAL_MODES:
                    raise PromotionGitError("promotion-special-git-object-rejected")
                if text not in _ALLOWED_MODES:
                    raise PromotionGitError("promotion-git-mode-rejected")
            try:
                paths.append(tokens[offset + 1].decode("utf-8", errors="strict"))
            except UnicodeError:
                raise PromotionGitError(
                    "promotion-integration-diff-failed"
                ) from None
        try:
            freeze_changed_paths(
                tuple(paths), max_paths=MAX_INTEGRATION_CHANGED_PATHS
            )
        except Exception:
            raise PromotionGitError("promotion-integration-path-rejected") from None

    async def _require_worktree_is_tree(
        self, repository: Path, tree: str
    ) -> None:
        """Prove the checkout Git produced is exactly the tree under review.

        Suppressing ``core.autocrlf``/``core.eol`` removes the configuration
        this host never chose, but a candidate can also ship a ``.gitattributes``
        that asks for conversion, and attributes outrank configuration. Rather
        than trust either, the materialised bytes are hashed as Git objects and
        compared against the tree's own blob ids. A checkout that is not the
        tree fails closed: approving bytes a verifier never exercised is exactly
        what the review exists to prevent.
        """

        expected = await self._tree_entries(repository, tree)
        modes = {path: mode for path, (mode, _) in expected.items()}
        actual = (await self._worktree_state(repository, modes))[1]
        if actual != expected:
            raise PromotionGitError("promotion-integration-worktree-not-tree-exact")

    async def _tree_modes(self, repository: Path, tree: str) -> dict[str, bool]:
        entries = await self._tree_entries(repository, tree)
        return {path: mode for path, (mode, _) in entries.items()}

    async def _tree_entries(
        self, repository: Path, tree: str
    ) -> dict[str, tuple[bool, str]]:
        raw = await self._required(
            "-C",
            str(repository),
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            tree,
            cwd=repository,
            max_output_bytes=_MAX_RAW_DIFF_BYTES,
            code="promotion-integration-tree-unreadable",
        )
        entries: dict[str, tuple[bool, str]] = {}
        if not raw:
            raise PromotionGitError("promotion-patch-empty")
        records = raw.split(b"\0")
        if records[-1] != b"":
            raise PromotionGitError("promotion-integration-tree-unreadable")
        records.pop()
        for record in records:
            header, separator, raw_path = record.partition(b"\t")
            parts = header.split(b" ")
            if not separator or len(parts) != 3:
                raise PromotionGitError("promotion-integration-tree-unreadable")
            mode, object_type, object_id = parts
            text_mode = mode.decode("ascii", errors="replace")
            if text_mode in _SPECIAL_MODES:
                raise PromotionGitError("promotion-special-git-object-rejected")
            if text_mode not in {"100644", "100755"} or object_type != b"blob":
                raise PromotionGitError("promotion-git-mode-rejected")
            try:
                path = raw_path.decode("utf-8", errors="strict")
                oid = object_id.decode("ascii", errors="strict")
            except UnicodeError:
                raise PromotionGitError(
                    "promotion-integration-tree-unreadable"
                ) from None
            if not _is_object_id(oid) or path in entries:
                raise PromotionGitError("promotion-integration-tree-unreadable")
            entries[path] = (text_mode == "100755", oid)
        return entries

    async def _worktree_state(
        self, repository: Path, tree_modes: dict[str, bool] | None = None
    ) -> tuple[str, dict[str, tuple[bool, str]]]:
        """Digest the real bytes of the checkout, asking Git nothing.

        Every Git-side answer about "is the checkout clean" is influenced by
        state the candidate controls or the verifier can rewrite:
        ``git write-tree`` reads only the index; ``git status`` honours the
        candidate's own ``.gitignore`` and skips paths marked
        ``--assume-unchanged`` or ``--skip-worktree``. A Patch can add ignore
        rules and a verifier can set index flags, so neither can witness what
        the verifier actually executed.

        This walks the filesystem instead and hashes relative path, executable
        bit and content for every file. Only the root ``.git`` administration
        directory is skipped: its churn is Git's own business, and the Git-side
        identity that matters is re-derived separately as HEAD, the integration
        tree and the commit id.
        """

        object_format = await self._line(
            "-C",
            str(repository),
            "rev-parse",
            "--show-object-format",
            cwd=repository,
            code="promotion-integration-tree-unreadable",
        )
        if object_format not in ("sha1", "sha256"):
            raise PromotionGitError("promotion-integration-tree-unreadable")
        return await asyncio.to_thread(
            _fingerprint_worktree, repository, object_format, tree_modes
        )

    async def _reverify_integration(
        self,
        repository: Path,
        *,
        expected_revision: str,
        message: str,
        build: IntegrationBuild,
        worktree_fingerprint: str,
    ) -> IntegrationBuild:
        tree_modes = await self._tree_modes(repository, build.integration_tree)
        if (
            await self._worktree_state(repository, tree_modes)
        )[0] != worktree_fingerprint:
            raise PromotionGitError("promotion-integration-worktree-drift")
        head = await self._object_id(
            "-C",
            str(repository),
            "rev-parse",
            "--verify",
            "HEAD^{commit}",
            cwd=repository,
            code="promotion-integration-drift",
        )
        tree = await self._object_id(
            "-C",
            str(repository),
            "write-tree",
            cwd=repository,
            code="promotion-integration-drift",
        )
        commit = await self._object_id(
            "-C",
            str(repository),
            "commit-tree",
            tree,
            "-p",
            expected_revision,
            "-m",
            message,
            cwd=repository,
            code="promotion-integration-drift",
        )
        if (
            head != build.integration_commit
            or tree != build.integration_tree
            or commit != build.integration_commit
        ):
            raise PromotionGitError("promotion-integration-drift")
        return IntegrationBuild(integration_tree=tree, integration_commit=commit)


@contextlib.asynccontextmanager
async def _owned_temporary_directory() -> AsyncIterator[Path]:
    """A managed scratch directory that converges on every exit path."""

    base = _absolute(tempfile.gettempdir())
    root = _absolute(
        await asyncio.to_thread(tempfile.mkdtemp, prefix="traceh-promotion-")
    )
    _require_owned_directory(base, root)
    primary: BaseException | None = None
    try:
        yield root
    except BaseException as error:
        primary = error
        raise
    finally:
        await release_scratch(
            root,
            primary,
            remove=_remove_tree,
            task_name="traceh-promotion-scratch-cleanup",
            alone_error=lambda: PromotionGitError(
                "promotion-scratch-cleanup-failed"
            ),
            group_message="promotion scratch cleanup failed",
        )


def _fingerprint_worktree(
    repository: Path, object_format: str, tree_modes: dict[str, bool] | None
) -> tuple[str, dict[str, tuple[bool, str]]]:
    """Hash every real file below ``repository``, excluding Git's admin dir.

    Each file is hashed the way Git hashes a blob, so the result can be compared
    directly against a tree listing and not merely against an earlier walk of
    itself. The walk is lexical and never follows a link: a symlink, Junction or
    other reparse point is rejected rather than traversed.
    """

    entries: dict[str, tuple[bool, str]] = {}
    total_bytes = 0
    stack = [repository]
    while stack:
        current = stack.pop()
        try:
            iterator = os.scandir(current)
        except OSError:
            raise PromotionGitError("promotion-integration-worktree-unreadable") from None
        with iterator:
            for entry in iterator:
                path = Path(entry.path)
                if current == repository and entry.name == ".git":
                    continue
                if _is_reparse(path):
                    raise PromotionGitError("promotion-integration-worktree-unsafe")
                try:
                    is_directory = entry.is_dir(follow_symlinks=False)
                    is_file = entry.is_file(follow_symlinks=False)
                except OSError:
                    raise PromotionGitError(
                        "promotion-integration-worktree-unreadable"
                    ) from None
                if is_directory:
                    stack.append(path)
                    continue
                if not is_file:
                    # A socket, FIFO or device node is not a reviewable byte
                    # sequence, and it is not something an approved tree can
                    # contain either.
                    raise PromotionGitError("promotion-integration-worktree-unsafe")
                if len(entries) >= MAX_INTEGRATION_CHANGED_PATHS:
                    raise PromotionGitError("promotion-integration-worktree-too-large")
                relative = path.relative_to(repository).as_posix()
                object_id, size = _hash_blob(path, object_format)
                total_bytes += size
                if total_bytes > MAX_INTEGRATION_WORKTREE_BYTES:
                    raise PromotionGitError("promotion-integration-worktree-too-large")
                inherited = (
                    False if tree_modes is None else tree_modes.get(relative, False)
                )
                entries[relative] = (_is_executable(path, inherited), object_id)
    digest = hashlib.sha256()
    digest.update(b"traceh-promotion-worktree-v1\0")
    for relative in sorted(entries):
        executable, object_id = entries[relative]
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(b"x" if executable else b"-")
        digest.update(object_id.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest(), entries


def _hash_blob(path: Path, object_format: str) -> tuple[str, int]:
    """Compute the Git object id of one file's exact bytes."""

    try:
        size = path.stat().st_size
        digest = hashlib.sha1() if object_format == "sha1" else hashlib.sha256()
        digest.update(b"blob %d\0" % size)
        read = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                read += len(chunk)
                digest.update(chunk)
    except OSError:
        raise PromotionGitError("promotion-integration-worktree-unreadable") from None
    if read != size:
        # The file changed while it was being hashed, which is drift rather
        # than a digest of anything.
        raise PromotionGitError("promotion-integration-worktree-unreadable")
    return digest.hexdigest(), size


def _is_executable(path: Path, tree_executable: bool) -> bool:
    """The one mode bit an approved Git tree can distinguish, where it exists.

    Mode ``100755`` is ordinary - any repository with a runnable script has it -
    but a Windows filesystem cannot store the bit, so `st_mode` reports ``0o666``
    for a file Git recorded as executable. Demanding the bit there would refuse
    every such repository while proving nothing.

    Where the platform cannot represent it, the tree's own mode is carried
    through unchanged. That is not a weakening: the promoted tree's modes come
    from the index Git built, never from this walk, and the walk's real job -
    proving the verifier read the approved *bytes* - is unaffected. On POSIX the
    bit is compared for real, so a genuine mode change is still caught.
    """

    if not _FILESYSTEM_TRACKS_EXECUTABLE_BIT:
        return tree_executable
    try:
        return bool(path.stat().st_mode & stat.S_IXUSR)
    except OSError:
        raise PromotionGitError("promotion-integration-worktree-unreadable") from None


def _remove_tree(root: Path) -> None:
    def _retry(function, path, _excinfo) -> None:
        # Git object files are created read-only; Windows refuses to unlink
        # them until the read-only attribute is cleared.
        try:
            os.chmod(path, stat.S_IWRITE)
        except OSError:
            raise
        function(path)

    shutil.rmtree(root, onexc=_retry)


def _write_private_file(path: Path, content: bytes) -> None:
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _require_owned_directory(base: Path, root: Path) -> None:
    """Reject any reparse point on the components this process just created."""

    try:
        components = root.relative_to(base).parts
    except ValueError:
        raise PromotionGitError("promotion-scratch-path-unsafe") from None
    if not components:
        raise PromotionGitError("promotion-scratch-path-unsafe")
    current = base
    for component in components:
        current /= component
        if not current.is_dir() or _is_reparse(current):
            raise PromotionGitError("promotion-scratch-path-unsafe")


def _require_patch(patch: object) -> bytes:
    if type(patch) is not bytes or not patch or len(patch) > MAX_PATCH_BYTES:
        raise PromotionInputError("promotion-patch-invalid", "patch")
    return patch


def _require_message(message: object) -> str:
    if (
        type(message) is not str
        or not message
        or len(message) > 4096
        or "\n" in message
        or "\r" in message
        or "\0" in message
    ):
        raise PromotionInputError("promotion-commit-message-invalid", "message")
    return message


def _decode_line(raw: bytes) -> str:
    try:
        value = raw.decode("utf-8", errors="strict")
    except UnicodeError:
        raise PromotionGitError("promotion-git-output-invalid") from None
    if value.endswith("\n"):
        value = value[:-1]
        if value.endswith("\r"):
            value = value[:-1]
    if not value or "\n" in value or "\r" in value or "\0" in value:
        raise PromotionGitError("promotion-git-output-invalid")
    return value


def _is_object_id(value: object) -> bool:
    return (
        type(value) is str
        and len(value) in (40, 64)
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


def _fingerprint(common_dir: Path) -> str:
    return hashlib.sha256(_path_key(common_dir).encode("utf-8")).hexdigest()


def _absolute(value: object) -> Path:
    return Path(value).absolute()  # type: ignore[arg-type]


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path.absolute())))


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


__all__ = [
    "IntegrationBuild",
    "IntegrationEnvironment",
    "LocalBareGitPromotionTargets",
    "LocalGitPromotionEngine",
]
