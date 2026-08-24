"""Shared real-Git and durable-Artifact fixtures for the v0.7-D2 test suite."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from traceh.api.artifacts import PatchArtifact, PatchCaptureLimits
from traceh.api.events import PendingEvent
from traceh.api.promotion import (
    PromotionTargetBinding,
    VerificationPlan,
    VerifierCommand,
    VerifierEnvironmentPolicy,
)
from traceh.artifacts.cas import LocalArtifactCas
from traceh.artifacts.events import (
    ARTIFACT_CATALOG_STREAM,
    ARTIFACT_SCHEMA_VERSION,
    PATCH_MANIFEST_RECORDED,
    patch_manifest_data,
)
from traceh.artifacts.manifest import patch_artifact_id, patch_capture_key
from traceh.artifacts.reader import PatchArtifactReader
from traceh.promotion import (
    LocalBareGitPromotionTargets,
    LocalGitPromotionEngine,
    PatchPromotionService,
)
from traceh.session.event_store import EventStore

PASSTHROUGH_ENVIRONMENT = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "TMPDIR",
    "HOME",
    "LANG",
    "LC_ALL",
)


def clean_git_environment() -> dict[str, str]:
    """Fixture Git must not inherit whatever a hostile-environment test set."""

    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_AUTHOR_NAME"] = "TraceHarness Fixture"
    environment["GIT_AUTHOR_EMAIL"] = "fixture@example.invalid"
    environment["GIT_COMMITTER_NAME"] = "TraceHarness Fixture"
    environment["GIT_COMMITTER_EMAIL"] = "fixture@example.invalid"
    environment["GIT_AUTHOR_DATE"] = "@1000000000 +0000"
    environment["GIT_COMMITTER_DATE"] = "@1000000000 +0000"
    return environment


def git_status(*argv: str, cwd: Path) -> tuple[int, bytes]:
    """Run one fixture Git command and return its exact status and stdout."""

    completed = subprocess.run(
        ("git", *argv),
        cwd=cwd,
        check=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        env=clean_git_environment(),
    )
    return completed.returncode, completed.stdout


def git(*argv: str, cwd: Path) -> str:
    completed = subprocess.run(
        ("git", *argv),
        cwd=cwd,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=clean_git_environment(),
    )
    return completed.stdout.strip()


def _configure(root: Path) -> None:
    git("config", "user.name", "TraceHarness Fixture", cwd=root)
    git("config", "user.email", "fixture@example.invalid", cwd=root)
    git("config", "commit.gpgsign", "false", cwd=root)


@dataclass(frozen=True, slots=True)
class PatchSource:
    """One real Patch plus the Git facts an immutable Manifest must carry."""

    patch: bytes
    base_revision: str
    head_revision: str
    candidate_tree: str
    changed_paths: tuple[str, ...]
    repository_fingerprint: str


def build_source_repository(root: Path) -> tuple[Path, str]:
    """Create a normal repository with one base commit."""

    root.mkdir(parents=True)
    git("init", "--quiet", "--initial-branch=main", cwd=root)
    _configure(root)
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    (root / "kept.txt").write_text("kept\n", encoding="utf-8")
    git("add", "-A", cwd=root)
    git("commit", "--quiet", "-m", "fixture base", cwd=root)
    return root, git("rev-parse", "HEAD", cwd=root)


def make_patch(
    source: Path,
    scratch: Path,
    changes: dict[str, str | None],
    *,
    executable: tuple[str, ...] = (),
) -> PatchSource:
    """Produce the exact bytes a D1 capture would have recorded.

    ``executable`` marks paths as mode ``100755`` in the recorded tree, which is
    how a real repository carries a runnable script.
    """

    base = git("rev-parse", "HEAD", cwd=source)
    key = hashlib.sha256(repr(sorted(changes.items())).encode()).hexdigest()[:12]
    work = scratch / f"work-{key}"
    git("clone", "--quiet", "--", str(source), str(work), cwd=scratch)
    _configure(work)
    for relative, content in changes.items():
        target = work / relative
        if content is None:
            target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git("add", "-A", cwd=work)
    for relative in executable:
        git("update-index", "--chmod=+x", relative, cwd=work)
    git("commit", "--quiet", "-m", "fixture candidate", cwd=work)
    head = git("rev-parse", "HEAD", cwd=work)
    tree = git("rev-parse", "HEAD^{tree}", cwd=work)
    raw = subprocess.run(
        (
            "git",
            "diff-tree",
            "-p",
            "--no-commit-id",
            "--binary",
            "--full-index",
            "--no-renames",
            "--no-ext-diff",
            "--no-color",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            base,
            tree,
            "--",
        ),
        cwd=work,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        env=clean_git_environment(),
    )
    fingerprint = hashlib.sha256(
        os.path.normcase(os.path.normpath(str((source / ".git").absolute()))).encode(
            "utf-8"
        )
    ).hexdigest()
    return PatchSource(
        patch=raw.stdout,
        base_revision=base,
        head_revision=head,
        candidate_tree=tree,
        changed_paths=tuple(sorted(changes)),
        repository_fingerprint=fingerprint,
    )


def make_bare_target(source: Path, destination: Path) -> Path:
    git(
        "clone",
        "--quiet",
        "--bare",
        "--",
        str(source),
        str(destination),
        cwd=source.parent,
    )
    _configure(destination)
    return destination


def capture_limits() -> PatchCaptureLimits:
    return PatchCaptureLimits(
        max_changed_paths=100,
        max_path_bytes=512,
        max_file_bytes=1024 * 1024,
        max_total_file_bytes=4 * 1024 * 1024,
        max_patch_bytes=4 * 1024 * 1024,
    )


async def record_artifact(
    store: EventStore,
    cas: LocalArtifactCas,
    patch_source: PatchSource,
    *,
    agent_id: str = "coder-agent",
    session_id: str = "coder-session",
    message_id: str = "work-message",
    turn_id: str = "work-turn",
    workspace_id: str = "workspace-1",
    workspace_generation: int = 1,
) -> PatchArtifact:
    """Append one real schema-1 Manifest through the D1 public builder."""

    blob = await cas.put(patch_source.patch)
    capture_key = patch_capture_key(
        agent_id=agent_id,
        message_id=message_id,
        workspace_id=workspace_id,
        workspace_generation=workspace_generation,
    )
    data = patch_manifest_data(
        artifact_id=patch_artifact_id(capture_key),
        capture_key=capture_key,
        blob=blob,
        agent_id=agent_id,
        session_id=session_id,
        message_id=message_id,
        turn_id=turn_id,
        workspace_id=workspace_id,
        workspace_generation=workspace_generation,
        repository_fingerprint=patch_source.repository_fingerprint,
        base_revision=patch_source.base_revision,
        workspace_head_revision=patch_source.head_revision,
        candidate_tree=patch_source.candidate_tree,
        changed_paths=patch_source.changed_paths,
    )
    head = await store.head(ARTIFACT_CATALOG_STREAM)
    await store.append(
        ARTIFACT_CATALOG_STREAM,
        expected_seq=head,
        events=(
            PendingEvent(
                type=PATCH_MANIFEST_RECORDED,
                data=data,
                schema_version=ARTIFACT_SCHEMA_VERSION,
            ),
        ),
    )
    return await PatchArtifactReader(store, cas).load(str(data["artifact_id"]))


def environment_policy(policy_id: str = "verifier-env-1") -> VerifierEnvironmentPolicy:
    return VerifierEnvironmentPolicy(
        policy_id=policy_id,
        passthrough=PASSTHROUGH_ENVIRONMENT,
        overrides=(("PYTHONIOENCODING", "utf-8"),),
    )


def verification_plan(
    *commands: VerifierCommand,
    plan_id: str = "python-integration-check",
    plan_version: int = 1,
    environment: VerifierEnvironmentPolicy | None = None,
    max_output_bytes: int = 1024 * 1024,
) -> VerificationPlan:
    return VerificationPlan(
        plan_id=plan_id,
        plan_version=plan_version,
        commands=commands if commands else (passing_command(),),
        environment=environment_policy() if environment is None else environment,
        max_output_bytes=max_output_bytes,
        protocol_version=1,
    )


def passing_command(
    command_id: str = "integration-present", timeout_ms: int = 60_000
) -> VerifierCommand:
    return VerifierCommand(
        command_id=command_id,
        argv=(
            sys.executable,
            "-c",
            "import pathlib, sys;"
            "sys.exit(0 if pathlib.Path('added.txt').read_text() == 'added\\n' else 1)",
        ),
        timeout_ms=timeout_ms,
    )


def failing_command(command_id: str = "always-fails") -> VerifierCommand:
    return VerifierCommand(
        command_id=command_id,
        argv=(sys.executable, "-c", "raise SystemExit(3)"),
        timeout_ms=60_000,
    )


def slow_command(command_id: str = "always-hangs") -> VerifierCommand:
    return VerifierCommand(
        command_id=command_id,
        argv=(sys.executable, "-c", "import time; time.sleep(60)"),
        timeout_ms=400,
    )


def promotion_targets(
    target_id: str, repository: Path, ref: str = "refs/heads/main"
) -> LocalBareGitPromotionTargets:
    return LocalBareGitPromotionTargets(
        targets={
            target_id: PromotionTargetBinding(
                repository_path=repository, target_ref=ref
            )
        }
    )


def promotion_service(
    store: EventStore,
    cas: LocalArtifactCas,
    resolver,
    *,
    plan: VerificationPlan,
    engine: LocalGitPromotionEngine | None = None,
    runner=None,
) -> PatchPromotionService:
    return PatchPromotionService(
        store,
        PatchArtifactReader(store, cas),
        resolver,
        plan=plan,
        engine=engine,
        runner=runner,
    )


__all__ = [
    "PASSTHROUGH_ENVIRONMENT",
    "PatchSource",
    "build_source_repository",
    "clean_git_environment",
    "capture_limits",
    "environment_policy",
    "failing_command",
    "git",
    "git_status",
    "make_bare_target",
    "make_patch",
    "passing_command",
    "promotion_service",
    "promotion_targets",
    "record_artifact",
    "slow_command",
    "verification_plan",
]
