"""D2-A: fixed verification and immutable Review Reports on real Git."""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path

import pytest
from promotion_fixtures import (
    build_source_repository,
    failing_command,
    git,
    git_status,
    make_bare_target,
    make_patch,
    passing_command,
    promotion_service,
    promotion_targets,
    record_artifact,
    slow_command,
    verification_plan,
)

from traceh.artifacts.cas import LocalArtifactCas
from traceh.artifacts.errors import ArtifactCasError
from traceh.promotion import (
    HostVerificationRunner,
    LocalGitPromotionEngine,
    PromotionGitError,
    PromotionStateError,
    PromotionTargetDriftError,
)
from traceh.promotion.events import PROMOTION_LEDGER_STREAM
from traceh.promotion.models import expected_approval_digest
from traceh.session.event_store import InMemoryEventStore

DEFAULT_CHANGES = {"added.txt": "added\n", "tracked.txt": "changed\n"}


class _GatedRunner:
    def __init__(self, inner=None) -> None:
        self.inner = HostVerificationRunner() if inner is None else inner
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.roots: list[Path] = []

    async def run(self, plan, *, cwd):
        self.roots.append(cwd)
        self.entered.set()
        await self.release.wait()
        return await self.inner.run(plan, cwd=cwd)


class _GatedEngine:
    """Real engine with one deterministic gate at a named stage."""

    def __init__(self, stage: str) -> None:
        self.inner = LocalGitPromotionEngine()
        self.stage = stage
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    @contextlib.asynccontextmanager
    async def integration(self, *args, **kwargs):
        if self.stage == "clone":
            self.entered.set()
            await self.release.wait()
        async with self.inner.integration(*args, **kwargs) as environment:
            if self.stage == "apply":
                self.entered.set()
                await self.release.wait()
            yield environment

    async def rebuild_in_target(self, *args, **kwargs):
        return await self.inner.rebuild_in_target(*args, **kwargs)

    async def compare_and_swap(self, *args, **kwargs):
        return await self.inner.compare_and_swap(*args, **kwargs)

    async def read_ref(self, *args, **kwargs):
        return await self.inner.read_ref(*args, **kwargs)

    async def require_target_identity(self, *args, **kwargs):
        return await self.inner.require_target_identity(*args, **kwargs)


class _GatedAppendStore:
    def __init__(self) -> None:
        self.inner = InMemoryEventStore()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def append(self, stream_id, *, expected_seq, events, durability=None):
        if stream_id == PROMOTION_LEDGER_STREAM:
            self.entered.set()
            await self.release.wait()
        if durability is None:
            return await self.inner.append(
                stream_id, expected_seq=expected_seq, events=events
            )
        return await self.inner.append(
            stream_id,
            expected_seq=expected_seq,
            events=events,
            durability=durability,
        )

    async def read(self, stream_id, *, from_seq=1):
        return await self.inner.read(stream_id, from_seq=from_seq)

    async def head(self, stream_id):
        return await self.inner.head(stream_id)

    async def list_streams(self, *, prefix=None):
        return await self.inner.list_streams(prefix=prefix)


async def _assembly(
    tmp_path: Path,
    *,
    plan=None,
    engine=None,
    runner=None,
    changes=None,
    store=None,
    advance_target: dict[str, str] | None = None,
    executable: tuple[str, ...] = (),
):
    source, _ = build_source_repository(tmp_path / "source")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    patch_source = make_patch(
        source,
        scratch,
        DEFAULT_CHANGES if changes is None else changes,
        executable=executable,
    )
    if advance_target is not None:
        for relative, content in advance_target.items():
            (source / relative).write_text(content, encoding="utf-8")
        git("add", "-A", cwd=source)
        git("commit", "--quiet", "-m", "target moved", cwd=source)
    target = make_bare_target(source, tmp_path / "target.git")
    store = InMemoryEventStore() if store is None else store
    cas = LocalArtifactCas(tmp_path / "cas")
    artifact = await record_artifact(store, cas, patch_source)
    service = promotion_service(
        store,
        cas,
        promotion_targets("main-target", target),
        plan=verification_plan() if plan is None else plan,
        engine=engine,
        runner=runner,
    )
    return store, cas, target, artifact, service


async def test_review_applies_the_exact_patch_and_records_a_passing_report(
    tmp_path: Path,
) -> None:
    store, _, target, artifact, service = await _assembly(tmp_path)
    base = git("rev-parse", "refs/heads/main", cwd=target)

    report = await service.review(
        review_request_id="review-request-1",
        artifact_id=artifact.manifest.artifact_id,
        target_id="main-target",
    )

    assert report.passed is True
    assert report.artifact_id == artifact.manifest.artifact_id
    assert report.manifest_digest == artifact.manifest.manifest_digest
    assert report.patch_sha256 == artifact.manifest.blob.sha256
    assert report.target_id == "main-target"
    assert report.target_ref == "refs/heads/main"
    assert report.expected_revision == base
    assert report.merge_policy_version == 1
    assert len(report.results) == 1
    assert report.results[0].status == "passed"
    assert report.results[0].exit_code == 0
    # Review must leave the real target completely untouched: the ref does not
    # move and the integration objects live only in the temporary clone.
    assert git("rev-parse", "refs/heads/main", cwd=target) == base
    exit_code, _ = git_status(
        "cat-file", "-e", report.integration_commit, cwd=target
    )
    assert exit_code != 0
    assert len(await store.read(PROMOTION_LEDGER_STREAM)) == 1
    await service.aclose()


async def test_review_is_deterministic_and_idempotent_per_request(
    tmp_path: Path,
) -> None:
    store, _, _, artifact, service = await _assembly(tmp_path)
    first = await service.review(
        review_request_id="review-request-1",
        artifact_id=artifact.manifest.artifact_id,
        target_id="main-target",
    )
    repeated = await service.review(
        review_request_id="review-request-1",
        artifact_id=artifact.manifest.artifact_id,
        target_id="main-target",
    )
    second = await service.review(
        review_request_id="review-request-2",
        artifact_id=artifact.manifest.artifact_id,
        target_id="main-target",
    )

    assert repeated == first
    assert second.review_id != first.review_id
    assert second.integration_tree == first.integration_tree
    assert second.integration_commit == first.integration_commit
    assert len(await store.read(PROMOTION_LEDGER_STREAM)) == 2
    await service.aclose()


async def test_failed_verifier_produces_a_durable_report_that_cannot_be_approved(
    tmp_path: Path,
) -> None:
    store, _, _, artifact, service = await _assembly(
        tmp_path, plan=verification_plan(passing_command(), failing_command())
    )
    report = await service.review(
        review_request_id="review-request-1",
        artifact_id=artifact.manifest.artifact_id,
        target_id="main-target",
    )

    assert report.passed is False
    assert [item.status for item in report.results] == ["passed", "failed"]
    assert report.results[1].exit_code == 3
    assert len(await store.read(PROMOTION_LEDGER_STREAM)) == 1

    from traceh.promotion import PromotionApprovalError

    with pytest.raises(PromotionApprovalError) as raised:
        await service.approve(
            review_id=report.review_id,
            approval_digest=expected_approval_digest(report),
            approver_id="release-manager",
            operation_id="approve-1",
        )
    assert raised.value.code == "promotion-review-not-passed"
    assert len(await store.read(PROMOTION_LEDGER_STREAM)) == 1
    await service.aclose()


async def test_verifier_timeout_is_recorded_as_a_bounded_failed_result(
    tmp_path: Path,
) -> None:
    _, _, _, artifact, service = await _assembly(
        tmp_path, plan=verification_plan(slow_command())
    )
    report = await service.review(
        review_request_id="review-request-1",
        artifact_id=artifact.manifest.artifact_id,
        target_id="main-target",
    )
    assert report.passed is False
    assert report.results[0].status == "timed-out"
    assert report.results[0].exit_code is None
    assert report.results[0].stdout_bytes == 0
    await service.aclose()


async def test_apply_conflict_fails_closed_without_any_review(
    tmp_path: Path,
) -> None:
    store, _, _, artifact, service = await _assembly(
        tmp_path, advance_target={"tracked.txt": "conflicting\n"}
    )
    with pytest.raises(PromotionGitError) as raised:
        await service.review(
            review_request_id="review-request-1",
            artifact_id=artifact.manifest.artifact_id,
            target_id="main-target",
        )
    assert raised.value.code == "promotion-patch-apply-conflict"
    assert await store.read(PROMOTION_LEDGER_STREAM) == ()
    await service.aclose()


async def test_target_drift_during_verification_fails_closed(
    tmp_path: Path,
) -> None:
    runner = _GatedRunner()
    store, _, target, artifact, service = await _assembly(tmp_path, runner=runner)
    reviewing = asyncio.create_task(
        service.review(
            review_request_id="review-request-1",
            artifact_id=artifact.manifest.artifact_id,
            target_id="main-target",
        )
    )
    await runner.entered.wait()
    tree = git("rev-parse", "refs/heads/main^{tree}", cwd=target)
    moved = git("commit-tree", tree, "-m", "drift", cwd=target)
    git("update-ref", "refs/heads/main", moved, cwd=target)
    runner.release.set()

    with pytest.raises(PromotionTargetDriftError):
        await reviewing
    assert await store.read(PROMOTION_LEDGER_STREAM) == ()
    await service.aclose()


async def test_tampered_cas_bytes_are_rejected_before_any_review(
    tmp_path: Path,
) -> None:
    store, cas, _, artifact, service = await _assembly(tmp_path)
    digest = artifact.manifest.blob.sha256
    blob_path = cas.local_root / "sha256" / digest[:2] / digest
    blob_path.chmod(0o600)
    blob_path.write_bytes(b"tampered\n")

    with pytest.raises(ArtifactCasError) as raised:
        await service.review(
            review_request_id="review-request-1",
            artifact_id=artifact.manifest.artifact_id,
            target_id="main-target",
        )
    assert raised.value.code == "artifact-cas-collision"
    assert await store.read(PROMOTION_LEDGER_STREAM) == ()
    await service.aclose()


async def test_inherited_git_environment_cannot_reach_the_integration_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, target, artifact, service = await _assembly(tmp_path)
    clean = await service.review(
        review_request_id="review-request-1",
        artifact_id=artifact.manifest.artifact_id,
        target_id="main-target",
    )

    # Every one of these would change or break the integration if it reached
    # the child Git process: a redirected repository, an unwritable index, a
    # foreign work tree, injected configuration and a foreign commit identity.
    monkeypatch.setenv("GIT_DIR", str(target))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "absent" / "deeper" / "index"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "source"))
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.bare=true' 'core.autocrlf=true'")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Attacker")
    monkeypatch.setenv("GIT_AUTHOR_DATE", "@1700000000 +0000")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "@1700000000 +0000")

    hostile = await service.review(
        review_request_id="review-request-2",
        artifact_id=artifact.manifest.artifact_id,
        target_id="main-target",
    )
    assert os.environ["GIT_DIR"] == str(target)
    assert hostile.integration_tree == clean.integration_tree
    assert hostile.integration_commit == clean.integration_commit
    assert hostile.passed is True
    await service.aclose()


async def test_repeated_cancellation_before_clone_converges_to_one_review(
    tmp_path: Path,
) -> None:
    engine = _GatedEngine("clone")
    store, _, _, artifact, service = await _assembly(tmp_path, engine=engine)
    reviewing = asyncio.create_task(
        service.review(
            review_request_id="review-request-1",
            artifact_id=artifact.manifest.artifact_id,
            target_id="main-target",
        )
    )
    await engine.entered.wait()
    reviewing.cancel()
    reviewing.cancel()
    reviewing.cancel()
    assert not reviewing.done()
    engine.release.set()
    with pytest.raises(asyncio.CancelledError):
        await reviewing

    recovered = await service.review(
        review_request_id="review-request-1",
        artifact_id=artifact.manifest.artifact_id,
        target_id="main-target",
    )
    assert recovered.passed is True
    assert len(await store.read(PROMOTION_LEDGER_STREAM)) == 1
    await service.aclose()


async def test_cancellation_after_apply_still_converges_the_owned_review(
    tmp_path: Path,
) -> None:
    engine = _GatedEngine("apply")
    store, _, _, artifact, service = await _assembly(tmp_path, engine=engine)
    reviewing = asyncio.create_task(
        service.review(
            review_request_id="review-request-1",
            artifact_id=artifact.manifest.artifact_id,
            target_id="main-target",
        )
    )
    await engine.entered.wait()
    reviewing.cancel()
    engine.release.set()
    with pytest.raises(asyncio.CancelledError):
        await reviewing
    assert len(await store.read(PROMOTION_LEDGER_STREAM)) == 1
    await service.aclose()


async def test_cancellation_during_verification_converges(tmp_path: Path) -> None:
    runner = _GatedRunner()
    store, _, _, artifact, service = await _assembly(tmp_path, runner=runner)
    reviewing = asyncio.create_task(
        service.review(
            review_request_id="review-request-1",
            artifact_id=artifact.manifest.artifact_id,
            target_id="main-target",
        )
    )
    await runner.entered.wait()
    reviewing.cancel()
    runner.release.set()
    with pytest.raises(asyncio.CancelledError):
        await reviewing
    assert len(await store.read(PROMOTION_LEDGER_STREAM)) == 1
    await service.aclose()


async def test_cancellation_during_report_append_converges(tmp_path: Path) -> None:
    store = _GatedAppendStore()
    _, _, _, artifact, service = await _assembly(tmp_path, store=store)
    reviewing = asyncio.create_task(
        service.review(
            review_request_id="review-request-1",
            artifact_id=artifact.manifest.artifact_id,
            target_id="main-target",
        )
    )
    await store.entered.wait()
    reviewing.cancel()
    store.release.set()
    with pytest.raises(asyncio.CancelledError):
        await reviewing
    assert len(await store.read(PROMOTION_LEDGER_STREAM)) == 1
    await service.aclose()


async def test_scratch_cleanup_failure_never_hides_the_primary_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import traceh.promotion.local_git as local_git

    def _explode(root: Path) -> None:
        raise OSError(f"cleanup refused for {root.name}")

    monkeypatch.setattr(local_git, "_remove_tree", _explode)
    store, _, _, artifact, service = await _assembly(
        tmp_path, advance_target={"tracked.txt": "conflicting\n"}
    )
    with pytest.raises(BaseExceptionGroup) as raised:
        await service.review(
            review_request_id="review-request-1",
            artifact_id=artifact.manifest.artifact_id,
            target_id="main-target",
        )
    codes = [
        getattr(error, "code", None) for error in raised.value.exceptions
    ]
    assert "promotion-patch-apply-conflict" in codes
    assert any(isinstance(error, OSError) for error in raised.value.exceptions)
    assert await store.read(PROMOTION_LEDGER_STREAM) == ()
    await service.aclose()


async def test_scratch_cleanup_failure_alone_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import traceh.promotion.local_git as local_git

    def _explode(root: Path) -> None:
        raise OSError(f"cleanup refused for {root.name}")

    monkeypatch.setattr(local_git, "_remove_tree", _explode)
    store, _, _, artifact, service = await _assembly(tmp_path)
    with pytest.raises(PromotionGitError) as raised:
        await service.review(
            review_request_id="review-request-1",
            artifact_id=artifact.manifest.artifact_id,
            target_id="main-target",
        )
    assert raised.value.code == "promotion-scratch-cleanup-failed"
    assert await store.read(PROMOTION_LEDGER_STREAM) == ()
    await service.aclose()


async def test_a_substituted_verifier_definition_is_refused(tmp_path: Path) -> None:
    class _WrongDefinition:
        async def run(self, plan, *, cwd):
            from traceh.promotion.verification import VerificationEvidence

            del plan, cwd
            return VerificationEvidence(
                definition_digest="0" * 64,
                evidence_digest="1" * 64,
                results=(),
                passed=True,
            )

    store, _, _, artifact, service = await _assembly(
        tmp_path, runner=_WrongDefinition()
    )
    with pytest.raises(PromotionStateError) as raised:
        await service.review(
            review_request_id="review-request-1",
            artifact_id=artifact.manifest.artifact_id,
            target_id="main-target",
        )
    assert raised.value.code == "promotion-verifier-definition-mismatch"
    assert await store.read(PROMOTION_LEDGER_STREAM) == ()
    await service.aclose()


class _TamperingRunner:
    """A verifier that mutates the integration worktree it was asked to check."""

    def __init__(self) -> None:
        self.inner = HostVerificationRunner()

    async def run(self, plan, *, cwd):
        (Path(cwd) / "added.txt").write_text("tampered\n", encoding="utf-8")
        return await self.inner.run(plan, cwd=cwd)


class _ForeignResultRunner:
    """A runner that returns a well-formed result for a command not in the plan."""

    async def run(self, plan, *, cwd):
        from traceh.api.promotion import VerifierOutcome
        from traceh.promotion.models import (
            verification_evidence_digest,
            verifier_definition_digest,
        )
        from traceh.promotion.verification import VerificationEvidence

        del cwd
        definition = verifier_definition_digest(plan)
        results = tuple(
            VerifierOutcome(
                command_id="not-in-plan",
                argv_digest="0" * 64,
                status="passed",
                exit_code=0,
                stdout_sha256="0" * 64,
                stdout_bytes=0,
                stderr_sha256="0" * 64,
                stderr_bytes=0,
            )
            for _ in plan.commands
        )
        return VerificationEvidence(
            definition_digest=definition,
            evidence_digest=verification_evidence_digest(definition, results),
            results=results,
            passed=True,
        )


async def test_a_verifier_that_edits_the_worktree_cannot_produce_a_passing_review(
    tmp_path: Path,
) -> None:
    store, _, _, artifact, service = await _assembly(
        tmp_path, runner=_TamperingRunner()
    )
    with pytest.raises(PromotionGitError) as raised:
        await service.review(
            review_request_id="review-request-1",
            artifact_id=artifact.manifest.artifact_id,
            target_id="main-target",
        )
    assert raised.value.code == "promotion-integration-worktree-drift"
    assert await store.read(PROMOTION_LEDGER_STREAM) == ()
    await service.aclose()


async def test_an_in_flight_review_is_not_shared_with_a_different_request(
    tmp_path: Path,
) -> None:
    runner = _GatedRunner()
    store, cas, target, artifact, service = await _assembly(tmp_path, runner=runner)
    from promotion_fixtures import make_patch, record_artifact

    other = make_patch(tmp_path / "source", tmp_path / "scratch", {"second.txt": "2\n"})
    second = await record_artifact(store, cas, other, message_id="work-message-2")

    first = asyncio.create_task(
        service.review(
            review_request_id="review-request-1",
            artifact_id=artifact.manifest.artifact_id,
            target_id="main-target",
        )
    )
    await runner.entered.wait()
    from traceh.promotion import PromotionOperationConflictError

    intruder = asyncio.create_task(
        service.review(
            review_request_id="review-request-1",
            artifact_id=second.manifest.artifact_id,
            target_id="main-target",
        )
    )
    try:
        for _ in range(8):
            await asyncio.sleep(0)
        # It must be refused immediately, not silently joined to the in-flight
        # review of a completely different artifact.
        assert intruder.done()
        with pytest.raises(PromotionOperationConflictError):
            intruder.result()
    finally:
        runner.release.set()
        await asyncio.gather(intruder, return_exceptions=True)
    report = await first
    assert report.artifact_id == artifact.manifest.artifact_id
    del target
    await service.aclose()


async def test_a_repeated_request_under_a_different_plan_is_refused(
    tmp_path: Path,
) -> None:
    from promotion_fixtures import promotion_service, promotion_targets

    store, cas, target, artifact, service = await _assembly(tmp_path)
    first = await service.review(
        review_request_id="review-request-1",
        artifact_id=artifact.manifest.artifact_id,
        target_id="main-target",
    )
    assert first.passed is True

    stricter = promotion_service(
        store,
        cas,
        promotion_targets("main-target", target),
        plan=verification_plan(passing_command(), failing_command()),
    )
    from traceh.promotion import PromotionOperationConflictError

    with pytest.raises(PromotionOperationConflictError):
        await stricter.review(
            review_request_id="review-request-1",
            artifact_id=artifact.manifest.artifact_id,
            target_id="main-target",
        )
    await stricter.aclose()
    await service.aclose()


async def test_a_result_for_a_command_outside_the_plan_is_refused(
    tmp_path: Path,
) -> None:
    store, _, _, artifact, service = await _assembly(
        tmp_path, runner=_ForeignResultRunner()
    )
    with pytest.raises(PromotionStateError) as raised:
        await service.review(
            review_request_id="review-request-1",
            artifact_id=artifact.manifest.artifact_id,
            target_id="main-target",
        )
    assert raised.value.code == "promotion-verifier-result-mismatch"
    assert await store.read(PROMOTION_LEDGER_STREAM) == ()
    await service.aclose()


class _WorkspacePollutingRunner:
    """A verifier that leaves a file behind in the integration worktree."""

    def __init__(self, relative: str) -> None:
        self.relative = relative
        self.inner = HostVerificationRunner()

    async def run(self, plan, *, cwd):
        target = Path(cwd) / self.relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("residue\n", encoding="utf-8")
        return await self.inner.run(plan, cwd=cwd)


async def test_even_an_ignored_build_artifact_blocks_a_review(
    tmp_path: Path,
) -> None:
    """Ignore rules ship inside the candidate, so they cannot grant permission."""

    store, _, _, artifact, service = await _assembly(
        tmp_path,
        changes={**DEFAULT_CHANGES, ".gitignore": "build-cache/\n"},
        runner=_WorkspacePollutingRunner("build-cache/report.txt"),
    )
    with pytest.raises(PromotionGitError) as raised:
        await service.review(
            review_request_id="review-request-1",
            artifact_id=artifact.manifest.artifact_id,
            target_id="main-target",
        )
    assert raised.value.code == "promotion-integration-worktree-drift"
    assert await store.read(PROMOTION_LEDGER_STREAM) == ()
    await service.aclose()


async def test_a_verifier_may_write_to_the_granted_scratch_outside_the_worktree(
    tmp_path: Path,
) -> None:
    import sys

    from traceh.api.promotion import VerifierCommand

    host_temp = os.environ.get("TEMP")
    # The command exits 0 only when its temporary directory is host-granted
    # scratch, lives outside the checkout, and is writable.
    scratch_check = VerifierCommand(
        command_id="writes-to-granted-scratch",
        argv=(
            sys.executable,
            "-c",
            "import pathlib, tempfile, sys\n"
            "scratch = pathlib.Path(tempfile.gettempdir())\n"
            "if 'traceh-verifier-scratch-' not in scratch.name:\n"
            "    raise SystemExit('scratch was not granted')\n"
            "if scratch.resolve() in pathlib.Path.cwd().resolve().parents:\n"
            "    raise SystemExit('scratch contains the checkout')\n"
            "if pathlib.Path.cwd().resolve() in scratch.resolve().parents:\n"
            "    raise SystemExit('scratch is inside the checkout')\n"
            "(scratch / 'verifier-report.txt').write_text('ok', encoding='utf-8')\n"
            "sys.exit(0)\n",
        ),
        timeout_ms=60_000,
    )
    _, _, _, artifact, service = await _assembly(
        tmp_path, plan=verification_plan(scratch_check)
    )
    report = await service.review(
        review_request_id="review-request-1",
        artifact_id=artifact.manifest.artifact_id,
        target_id="main-target",
    )
    assert report.passed is True
    assert report.results[0].exit_code == 0
    # The host process' own environment is untouched.
    assert os.environ.get("TEMP") == host_temp
    await service.aclose()


async def test_an_unignored_file_left_by_a_verifier_fails_the_review(
    tmp_path: Path,
) -> None:
    store, _, _, artifact, service = await _assembly(
        tmp_path,
        changes={**DEFAULT_CHANGES, ".gitignore": "build-cache/\n"},
        runner=_WorkspacePollutingRunner("stray_module.py"),
    )
    with pytest.raises(PromotionGitError) as raised:
        await service.review(
            review_request_id="review-request-1",
            artifact_id=artifact.manifest.artifact_id,
            target_id="main-target",
        )
    assert raised.value.code == "promotion-integration-worktree-drift"
    assert await store.read(PROMOTION_LEDGER_STREAM) == ()
    await service.aclose()


class _IgnoredHelperRunner:
    """A verifier that runs code the approved tree does not contain."""

    def __init__(self) -> None:
        self.inner = HostVerificationRunner()

    async def run(self, plan, *, cwd):
        helper = Path(cwd) / "ignored_helper.py"
        helper.write_text("VALUE = 'not in the approved tree'\n", encoding="utf-8")
        return await self.inner.run(plan, cwd=cwd)


class _AssumeUnchangedRunner:
    """A verifier that hides tracked drift behind a mutable index flag."""

    def __init__(self) -> None:
        self.inner = HostVerificationRunner()

    async def run(self, plan, *, cwd):
        git("update-index", "--assume-unchanged", "added.txt", cwd=Path(cwd))
        (Path(cwd) / "added.txt").write_text("tampered\n", encoding="utf-8")
        return await self.inner.run(plan, cwd=cwd)


async def test_an_ignored_file_cannot_smuggle_code_past_the_worktree_proof(
    tmp_path: Path,
) -> None:
    store, _, _, artifact, service = await _assembly(
        tmp_path,
        changes={**DEFAULT_CHANGES, ".gitignore": "ignored_helper.py\n"},
        runner=_IgnoredHelperRunner(),
    )
    with pytest.raises(PromotionGitError) as raised:
        await service.review(
            review_request_id="review-request-1",
            artifact_id=artifact.manifest.artifact_id,
            target_id="main-target",
        )
    assert raised.value.code == "promotion-integration-worktree-drift"
    assert await store.read(PROMOTION_LEDGER_STREAM) == ()
    await service.aclose()


async def test_assume_unchanged_cannot_hide_tracked_drift(tmp_path: Path) -> None:
    store, _, _, artifact, service = await _assembly(
        tmp_path, runner=_AssumeUnchangedRunner()
    )
    with pytest.raises(PromotionGitError) as raised:
        await service.review(
            review_request_id="review-request-1",
            artifact_id=artifact.manifest.artifact_id,
            target_id="main-target",
        )
    assert raised.value.code == "promotion-integration-worktree-drift"
    assert await store.read(PROMOTION_LEDGER_STREAM) == ()
    await service.aclose()


async def test_an_eol_attribute_cannot_make_the_verifier_read_other_bytes(
    tmp_path: Path,
) -> None:
    """`.gitattributes` ships in the candidate and must not rewrite the checkout."""

    store, _, _, artifact, service = await _assembly(
        tmp_path,
        changes={**DEFAULT_CHANGES, ".gitattributes": "*.txt text eol=crlf\n"},
    )
    with pytest.raises(PromotionGitError) as raised:
        await service.review(
            review_request_id="review-request-1",
            artifact_id=artifact.manifest.artifact_id,
            target_id="main-target",
        )
    assert raised.value.code == "promotion-integration-worktree-not-tree-exact"
    assert await store.read(PROMOTION_LEDGER_STREAM) == ()
    await service.aclose()


_EXACT_BYTES_PROGRAM = r"""
import pathlib, sys
target = pathlib.Path('added.txt')
if not target.exists():
    sys.exit(40)
actual = target.read_bytes()
if actual == b'added\n':
    sys.exit(0)
if actual == b'added\r\n':
    sys.exit(41)
sys.exit(42)
"""


async def test_the_verifier_reads_exactly_the_bytes_of_the_approved_tree(
    tmp_path: Path,
) -> None:
    """Host Git configuration must not convert what the verifier exercises.

    The exit code distinguishes "saw the approved bytes" from "saw a converted
    copy", so a machine whose global ``core.autocrlf`` is on cannot make this
    pass by accident. Reading in text mode would hide exactly that difference.
    """

    import sys

    from traceh.api.promotion import VerifierCommand

    exact_bytes = VerifierCommand(
        command_id="reads-approved-bytes",
        argv=(sys.executable, "-c", _EXACT_BYTES_PROGRAM),
        timeout_ms=60_000,
    )
    _, _, _target, artifact, service = await _assembly(
        tmp_path, plan=verification_plan(exact_bytes)
    )
    report = await service.review(
        review_request_id="review-request-1",
        artifact_id=artifact.manifest.artifact_id,
        target_id="main-target",
    )
    assert report.results[0].exit_code == 0, report.results[0]
    assert report.passed is True
    await service.aclose()


async def test_a_repository_with_an_executable_script_can_be_reviewed(
    tmp_path: Path,
) -> None:
    """Mode 100755 is ordinary; a platform that cannot store the bit must not
    turn every such repository into a refused review."""

    _, _, _target, artifact, service = await _assembly(
        tmp_path,
        changes={**DEFAULT_CHANGES, "run.sh": "#!/bin/sh\necho hi\n"},
        executable=("run.sh",),
    )
    report = await service.review(
        review_request_id="review-request-1",
        artifact_id=artifact.manifest.artifact_id,
        target_id="main-target",
    )
    assert report.passed is True
    await service.aclose()


class _ModeChangingRunner:
    """A verifier that rewrites the recorded mode instead of the bytes."""

    def __init__(self) -> None:
        self.inner = HostVerificationRunner()

    async def run(self, plan, *, cwd):
        git("update-index", "--chmod=-x", "run.sh", cwd=Path(cwd))
        return await self.inner.run(plan, cwd=cwd)


async def test_a_mode_change_is_caught_by_the_git_side_re_derivation(
    tmp_path: Path,
) -> None:
    """The filesystem cannot carry the bit on every platform, so the tree must.

    This is the guard that makes carrying the tree's mode safe: a verifier that
    changes the recorded mode changes the tree Git rebuilds, and that is
    compared against the tree under review.
    """

    store, _, _target, artifact, service = await _assembly(
        tmp_path,
        changes={**DEFAULT_CHANGES, "run.sh": "#!/bin/sh\necho hi\n"},
        executable=("run.sh",),
        runner=_ModeChangingRunner(),
    )
    with pytest.raises(PromotionGitError) as raised:
        await service.review(
            review_request_id="review-request-1",
            artifact_id=artifact.manifest.artifact_id,
            target_id="main-target",
        )
    assert raised.value.code == "promotion-integration-drift"
    assert await store.read(PROMOTION_LEDGER_STREAM) == ()
    await service.aclose()


class _BlockingRemoveTree:
    """Block inside the integration scratch removal, then fail."""

    def __init__(self) -> None:
        import threading

        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, root: Path) -> None:
        self.entered.set()
        self.release.wait(30)
        raise OSError(f"forced integration cleanup failure for {Path(root).name}")


async def test_cancelling_during_integration_cleanup_keeps_the_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cleanup failure that happens after cancellation is still evidence."""

    import traceh.promotion.local_git as local_git

    blocking = _BlockingRemoveTree()
    monkeypatch.setattr(local_git, "_remove_tree", blocking)

    _, _, _target, artifact, service = await _assembly(tmp_path)
    running = asyncio.create_task(
        service.review(
            review_request_id="review-request-1",
            artifact_id=artifact.manifest.artifact_id,
            target_id="main-target",
        )
    )
    await asyncio.to_thread(blocking.entered.wait, 30)
    running.cancel()
    running.cancel()
    await asyncio.sleep(0)
    assert not running.done()
    blocking.release.set()
    with pytest.raises(asyncio.CancelledError) as raised:
        await running

    # The cancellation lands at the service boundary, so the owned review task
    # converges first and its failure becomes the cause. What matters is that
    # the removal failure is still reachable rather than discarded.
    chain = _failure_chain(raised.value)
    assert any(
        isinstance(item, OSError)
        and "forced integration cleanup failure" in str(item)
        for item in chain
    ), chain
    assert any(
        getattr(item, "code", None) == "promotion-scratch-cleanup-failed"
        for item in chain
    ), chain
    await service.aclose()


def _failure_chain(error: BaseException) -> list[BaseException]:
    """Every exception reachable from ``error`` by cause, context or group."""

    seen: list[BaseException] = []
    pending = [error]
    while pending:
        item = pending.pop()
        if item is None or any(item is found for found in seen):
            continue
        seen.append(item)
        if isinstance(item, BaseExceptionGroup):
            pending.extend(item.exceptions)
        pending.append(item.__cause__)
        pending.append(item.__context__)
    return seen
