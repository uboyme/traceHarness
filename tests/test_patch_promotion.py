"""D2-B/C: human approval and Git compare-and-swap promotion on real Git."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import sys
from pathlib import Path

import pytest
from promotion_fixtures import (
    build_source_repository,
    git,
    git_status,
    make_bare_target,
    make_patch,
    promotion_service,
    promotion_targets,
    record_artifact,
    verification_plan,
)

from traceh.api.json_types import canonical_json
from traceh.api.promotion import PromotionTargetBinding, VerifierCommand
from traceh.artifacts.cas import LocalArtifactCas
from traceh.promotion import (
    LocalBareGitPromotionTargets,
    LocalGitPromotionEngine,
    PromotionApprovalError,
    PromotionGitError,
    PromotionLedgerReader,
    PromotionOperationConflictError,
    PromotionServiceClosedError,
    PromotionStateError,
    PromotionTargetDriftError,
    PromotionWriteError,
    expected_approval_digest,
)
from traceh.promotion.events import (
    PATCH_PROMOTION_COMMITTED,
    PROMOTION_LEDGER_STREAM,
)
from traceh.promotion.local_git import IntegrationBuild
from traceh.session.event_store import InMemoryEventStore


class _CountingEngine:
    """Real engine that records how often the linearization point ran."""

    def __init__(self, stage: str | None = None, parties: int = 1) -> None:
        self.inner = LocalGitPromotionEngine()
        self.stage = stage
        self.parties = parties
        self.swaps = 0
        self.arrived = asyncio.Event()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def integration(self, *args, **kwargs):
        return self.inner.integration(*args, **kwargs)

    async def rebuild_in_target(self, *args, **kwargs):
        return await self.inner.rebuild_in_target(*args, **kwargs)

    async def compare_and_swap(self, *args, **kwargs):
        self.swaps += 1
        if self.swaps >= self.parties:
            self.arrived.set()
        await self.arrived.wait()
        if self.stage == "before-swap":
            self.entered.set()
            await self.release.wait()
        result = await self.inner.compare_and_swap(*args, **kwargs)
        if self.stage == "after-swap":
            self.entered.set()
            await self.release.wait()
        return result

    async def read_ref(self, *args, **kwargs):
        return await self.inner.read_ref(*args, **kwargs)

    async def require_target_identity(self, *args, **kwargs):
        return await self.inner.require_target_identity(*args, **kwargs)


class _StagedSwapEngine(_CountingEngine):
    """Hold every promotion at the linearization point, then release in order.

    Both callers therefore complete their pre-checks and reconstruction while
    the ref is still at the shared expected-old revision. Whether a second
    promotion can move the ref afterwards is decided by the compare-and-swap
    alone, not by an earlier read.
    """

    def __init__(self, parties: int) -> None:
        super().__init__()
        self.parties = parties
        self.arrivals: list[asyncio.Event] = []
        self.all_arrived = asyncio.Event()

    async def compare_and_swap(self, *args, **kwargs):
        release = asyncio.Event()
        self.arrivals.append(release)
        if len(self.arrivals) >= self.parties:
            self.all_arrived.set()
        await release.wait()
        self.swaps += 1
        return await self.inner.compare_and_swap(*args, **kwargs)


class _MismatchingEngine(_CountingEngine):
    async def rebuild_in_target(self, *args, **kwargs):
        build = await self.inner.rebuild_in_target(*args, **kwargs)
        return IntegrationBuild(
            integration_tree=build.integration_tree,
            integration_commit="0" * 40,
        )


class _ScriptedLedgerStore:
    """Inject one failure into the promotion ledger append boundary."""

    def __init__(self, mode: str) -> None:
        self.inner = InMemoryEventStore()
        self.mode = mode
        self.fired = False
        self.hide_reads = False

    async def append(self, stream_id, *, expected_seq, events, durability=None):
        target = (
            stream_id == PROMOTION_LEDGER_STREAM
            and events[0].type == PATCH_PROMOTION_COMMITTED
            and not self.fired
        )
        if target and self.mode == "raise-before-commit":
            self.fired = True
            raise RuntimeError("ledger append failed before committing")
        result = await self._append(stream_id, expected_seq, events, durability)
        if target and self.mode == "commit-then-raise":
            self.fired = True
            raise RuntimeError("ledger append failed after committing")
        if target and self.mode == "commit-then-unknown":
            self.fired = True
            self.hide_reads = True
            raise RuntimeError("ledger append failed after committing")
        return result

    async def _append(self, stream_id, expected_seq, events, durability):
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
        if self.hide_reads and stream_id == PROMOTION_LEDGER_STREAM:
            self.hide_reads = False
            raise RuntimeError("the ledger cannot be re-read right now")
        return await self.inner.read(stream_id, from_seq=from_seq)

    async def head(self, stream_id):
        return await self.inner.head(stream_id)

    async def list_streams(self, *, prefix=None):
        return await self.inner.list_streams(prefix=prefix)


def _lenient_plan():
    """A plan every candidate satisfies, for tests about promotion itself."""

    return verification_plan(
        VerifierCommand(
            command_id="base-present",
            argv=(
                sys.executable,
                "-c",
                "import pathlib, sys;"
                "sys.exit(0 if pathlib.Path('tracked.txt').exists() else 1)",
            ),
            timeout_ms=60_000,
        )
    )


@contextlib.asynccontextmanager
async def _assembly(
    tmp_path: Path,
    *,
    engine=None,
    store=None,
    ref: str = "refs/heads/main",
    extra_messages: tuple[str, ...] = (),
    plan=None,
):
    source, _ = build_source_repository(tmp_path / "source")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    primary = make_patch(source, scratch, {"added.txt": "added\n"})
    target = make_bare_target(source, tmp_path / "target.git")
    store = InMemoryEventStore() if store is None else store
    cas = LocalArtifactCas(tmp_path / "cas")
    artifacts = [await record_artifact(store, cas, primary)]
    for index, message_id in enumerate(extra_messages, start=1):
        other = make_patch(source, scratch, {f"other-{index}.txt": f"other {index}\n"})
        artifacts.append(
            await record_artifact(store, cas, other, message_id=message_id)
        )
    service = promotion_service(
        store,
        cas,
        promotion_targets("main-target", target, ref),
        plan=verification_plan() if plan is None else plan,
        engine=engine,
    )
    try:
        yield store, target, tuple(artifacts), service
    finally:
        await service.aclose()


async def _approved(service, artifact, *, request_id="review-request-1", operation="approve-1"):
    report = await service.review(
        review_request_id=request_id,
        artifact_id=artifact.manifest.artifact_id,
        target_id="main-target",
    )
    digest = expected_approval_digest(report)
    approval = await service.approve(
        review_id=report.review_id,
        approval_digest=digest,
        approver_id="release-manager",
        operation_id=operation,
    )
    return report, approval


# ------------------------------------------------------------------ approval


async def test_a_passing_review_is_approved_by_its_exact_content_digest(
    tmp_path: Path,
) -> None:
    async with _assembly(tmp_path) as (store, _, artifacts, service):
        report, approval = await _approved(service, artifacts[0])
        assert approval.review_id == report.review_id
        assert approval.approval_digest == expected_approval_digest(report)
        assert approval.approver_id == "release-manager"
        assert approval.operation_id == "approve-1"
        assert len(await store.read(PROMOTION_LEDGER_STREAM)) == 2


async def test_a_stale_or_wrong_digest_is_refused(tmp_path: Path) -> None:
    async with _assembly(tmp_path) as (store, _, artifacts, service):
        report = await service.review(
            review_request_id="review-request-1",
            artifact_id=artifacts[0].manifest.artifact_id,
            target_id="main-target",
        )
        with pytest.raises(PromotionApprovalError) as raised:
            await service.approve(
                review_id=report.review_id,
                approval_digest="0" * 64,
                approver_id="release-manager",
                operation_id="approve-1",
            )
        assert raised.value.code == "promotion-approval-digest-stale"
        assert len(await store.read(PROMOTION_LEDGER_STREAM)) == 1


async def test_every_bound_field_invalidates_a_previous_approval_digest(
    tmp_path: Path,
) -> None:
    async with _assembly(tmp_path) as (_, _, artifacts, service):
        report = await service.review(
            review_request_id="review-request-1",
            artifact_id=artifacts[0].manifest.artifact_id,
            target_id="main-target",
        )
        original = expected_approval_digest(report)
        mutations = {
            "review_id": "review-" + "1" * 64,
            "review_request_id": "other-request",
            "artifact_id": "patch-" + "2" * 64,
            "manifest_digest": "3" * 64,
            "patch_sha256": "4" * 64,
            "patch_size_bytes": report.patch_size_bytes + 1,
            "target_id": "other-target",
            "repository_fingerprint": "5" * 64,
            "target_ref": "refs/heads/other",
            "expected_revision": "6" * 40,
            "integration_tree": "7" * 40,
            "integration_commit": "8" * 40,
            "verifier_definition_digest": "9" * 64,
            "verification_evidence_digest": "a" * 64,
            "merge_policy_version": 2,
            "passed": False,
        }
        for field, value in mutations.items():
            changed = dataclasses.replace(report, **{field: value})
            assert expected_approval_digest(changed) != original, field


async def test_the_same_operation_is_idempotent_and_a_different_payload_conflicts(
    tmp_path: Path,
) -> None:
    async with _assembly(tmp_path) as (store, _, artifacts, service):
        report, approval = await _approved(service, artifacts[0])
        repeated = await service.approve(
            review_id=report.review_id,
            approval_digest=approval.approval_digest,
            approver_id="release-manager",
            operation_id="approve-1",
        )
        assert repeated == approval
        assert len(await store.read(PROMOTION_LEDGER_STREAM)) == 2

        with pytest.raises(PromotionOperationConflictError):
            await service.approve(
                review_id=report.review_id,
                approval_digest=approval.approval_digest,
                approver_id="someone-else",
                operation_id="approve-1",
            )
        with pytest.raises(PromotionOperationConflictError):
            await service.approve(
                review_id=report.review_id,
                approval_digest=approval.approval_digest,
                approver_id="release-manager",
                operation_id="approve-2",
            )
        assert len(await store.read(PROMOTION_LEDGER_STREAM)) == 2


async def test_target_movement_between_review_and_approval_is_refused(
    tmp_path: Path,
) -> None:
    async with _assembly(tmp_path) as (store, target, artifacts, service):
        report = await service.review(
            review_request_id="review-request-1",
            artifact_id=artifacts[0].manifest.artifact_id,
            target_id="main-target",
        )
        tree = git("rev-parse", "refs/heads/main^{tree}", cwd=target)
        moved = git("commit-tree", tree, "-m", "drift", cwd=target)
        git("update-ref", "refs/heads/main", moved, cwd=target)

        with pytest.raises(PromotionTargetDriftError):
            await service.approve(
                review_id=report.review_id,
                approval_digest=expected_approval_digest(report),
                approver_id="release-manager",
                operation_id="approve-1",
            )
        assert len(await store.read(PROMOTION_LEDGER_STREAM)) == 1


# ----------------------------------------------------------------- promotion


async def test_promotion_moves_the_ref_to_the_exact_approved_commit(
    tmp_path: Path,
) -> None:
    engine = _CountingEngine()
    async with _assembly(tmp_path, engine=engine) as (store, target, artifacts, service):
        base = git("rev-parse", "refs/heads/main", cwd=target)
        report, approval = await _approved(service, artifacts[0])

        promotion = await service.promote(approval_digest=approval.approval_digest)

        assert promotion.previous_revision == base
        assert promotion.new_revision == report.integration_commit
        assert promotion.integration_tree == report.integration_tree
        assert promotion.review_id == report.review_id
        assert git("rev-parse", "refs/heads/main", cwd=target) == (
            report.integration_commit
        )
        assert git("rev-parse", "refs/heads/main^", cwd=target) == base
        assert git("rev-parse", "refs/heads/main^{tree}", cwd=target) == (
            report.integration_tree
        )
        assert engine.swaps == 1
        assert len(await store.read(PROMOTION_LEDGER_STREAM)) == 3

        repeated = await service.promote(approval_digest=approval.approval_digest)
        assert repeated == promotion
        assert engine.swaps == 1
        assert len(await store.read(PROMOTION_LEDGER_STREAM)) == 3


async def test_promotion_accepts_a_new_regular_directory(tmp_path: Path) -> None:
    """D2 validates recursive file entries rather than a tree container mode."""

    source, _ = build_source_repository(tmp_path / "source")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    patch_source = make_patch(
        source, scratch, {"package/module.py": "VALUE = 1\n"}
    )
    target = make_bare_target(source, tmp_path / "target.git")
    store = InMemoryEventStore()
    cas = LocalArtifactCas(tmp_path / "cas")
    artifact = await record_artifact(store, cas, patch_source)
    plan = verification_plan(
        VerifierCommand(
            command_id="nested-file-present",
            argv=(
                sys.executable,
                "-c",
                "import pathlib, sys;"
                "sys.exit(0 if pathlib.Path('package/module.py').read_text() "
                "== 'VALUE = 1\\n' else 1)",
            ),
            timeout_ms=60_000,
        )
    )
    service = promotion_service(
        store,
        cas,
        promotion_targets("main-target", target),
        plan=plan,
    )
    try:
        report, approval = await _approved(service, artifact)
        promotion = await service.promote(
            approval_digest=approval.approval_digest
        )

        assert promotion.new_revision == report.integration_commit
        assert git(
            "show", f"{promotion.new_revision}:package/module.py", cwd=target
        ) == "VALUE = 1"
    finally:
        await service.aclose()


async def test_an_unapproved_review_can_never_be_promoted(tmp_path: Path) -> None:
    engine = _CountingEngine()
    async with _assembly(tmp_path, engine=engine) as (store, target, artifacts, service):
        base = git("rev-parse", "refs/heads/main", cwd=target)
        report = await service.review(
            review_request_id="review-request-1",
            artifact_id=artifacts[0].manifest.artifact_id,
            target_id="main-target",
        )
        with pytest.raises(PromotionApprovalError) as raised:
            await service.promote(
                approval_digest=expected_approval_digest(report)
            )
        assert raised.value.code == "promotion-approval-unknown"
        assert engine.swaps == 0
        assert git("rev-parse", "refs/heads/main", cwd=target) == base
        assert len(await store.read(PROMOTION_LEDGER_STREAM)) == 1


async def test_target_drift_before_promotion_fails_closed_without_half_updating(
    tmp_path: Path,
) -> None:
    engine = _CountingEngine()
    async with _assembly(tmp_path, engine=engine) as (store, target, artifacts, service):
        _, approval = await _approved(service, artifacts[0])
        tree = git("rev-parse", "refs/heads/main^{tree}", cwd=target)
        moved = git("commit-tree", tree, "-m", "drift", cwd=target)
        git("update-ref", "refs/heads/main", moved, cwd=target)

        with pytest.raises(PromotionTargetDriftError):
            await service.promote(approval_digest=approval.approval_digest)
        assert engine.swaps == 0
        assert git("rev-parse", "refs/heads/main", cwd=target) == moved
        assert len(await store.read(PROMOTION_LEDGER_STREAM)) == 2


async def test_a_reconstruction_mismatch_never_reaches_the_ref(
    tmp_path: Path,
) -> None:
    engine = _MismatchingEngine()
    async with _assembly(tmp_path, engine=engine) as (store, target, artifacts, service):
        base = git("rev-parse", "refs/heads/main", cwd=target)
        _, approval = await _approved(service, artifacts[0])
        with pytest.raises(PromotionStateError) as raised:
            await service.promote(approval_digest=approval.approval_digest)
        assert raised.value.code == "promotion-reconstruction-mismatch"
        assert engine.swaps == 0
        assert git("rev-parse", "refs/heads/main", cwd=target) == base
        assert len(await store.read(PROMOTION_LEDGER_STREAM)) == 2


async def test_two_patches_racing_one_expected_old_produce_exactly_one_promotion(
    tmp_path: Path,
) -> None:
    engine = _StagedSwapEngine(parties=2)
    async with _assembly(
        tmp_path,
        engine=engine,
        extra_messages=("work-message-2",),
        plan=_lenient_plan(),
    ) as (store, target, artifacts, service):
        base = git("rev-parse", "refs/heads/main", cwd=target)
        first_report, first_approval = await _approved(
            service, artifacts[0], request_id="review-request-1", operation="approve-1"
        )
        second_report, second_approval = await _approved(
            service, artifacts[1], request_id="review-request-2", operation="approve-2"
        )
        assert first_report.expected_revision == base
        assert second_report.expected_revision == base
        assert first_report.integration_commit != second_report.integration_commit

        racing = {
            asyncio.create_task(
                service.promote(approval_digest=first_approval.approval_digest)
            ),
            asyncio.create_task(
                service.promote(approval_digest=second_approval.approval_digest)
            ),
        }
        # Both promotions are now holding at the compare-and-swap with the same
        # expected-old revision; release them strictly one after the other.
        await engine.all_arrived.wait()
        engine.arrivals[0].set()
        done, pending = await asyncio.wait(
            racing, return_when=asyncio.FIRST_COMPLETED
        )
        assert len(done) == 1
        engine.arrivals[1].set()
        results = [
            *(task.exception() or task.result() for task in done),
            *await asyncio.gather(*pending, return_exceptions=True),
        ]

        succeeded = [item for item in results if not isinstance(item, BaseException)]
        failed = [item for item in results if isinstance(item, BaseException)]
        assert len(succeeded) == 1
        assert len(failed) == 1
        assert isinstance(failed[0], PromotionTargetDriftError)
        assert engine.swaps == 2
        assert git("rev-parse", "refs/heads/main", cwd=target) == (
            succeeded[0].new_revision
        )
        promotions = (await PromotionLedgerReader(store).load()).promotions
        assert len(promotions) == 1
        assert promotions[0].new_revision == succeeded[0].new_revision


async def test_ref_update_committed_but_append_failed_is_recoverable(
    tmp_path: Path,
) -> None:
    store = _ScriptedLedgerStore("raise-before-commit")
    engine = _CountingEngine()
    async with _assembly(tmp_path, engine=engine, store=store) as (
        _,
        target,
        artifacts,
        service,
    ):
        report, approval = await _approved(service, artifacts[0])
        with pytest.raises(PromotionWriteError) as raised:
            await service.promote(approval_digest=approval.approval_digest)
        assert raised.value.committed is False
        # The Git mutation is already durable even though nothing was recorded.
        assert git("rev-parse", "refs/heads/main", cwd=target) == (
            report.integration_commit
        )
        assert engine.swaps == 1

        recovered = await service.promote(approval_digest=approval.approval_digest)
        assert recovered.new_revision == report.integration_commit
        assert engine.swaps == 1
        assert len((await PromotionLedgerReader(store).load()).promotions) == 1


async def test_append_reporting_failure_after_commit_still_returns_the_fact(
    tmp_path: Path,
) -> None:
    store = _ScriptedLedgerStore("commit-then-raise")
    engine = _CountingEngine()
    async with _assembly(tmp_path, engine=engine, store=store) as (
        _,
        target,
        artifacts,
        service,
    ):
        report, approval = await _approved(service, artifacts[0])
        promotion = await service.promote(approval_digest=approval.approval_digest)
        assert promotion.new_revision == report.integration_commit
        assert engine.swaps == 1
        assert git("rev-parse", "refs/heads/main", cwd=target) == (
            report.integration_commit
        )
        assert len((await PromotionLedgerReader(store).load()).promotions) == 1


async def test_an_unknown_append_outcome_is_never_reported_as_absent(
    tmp_path: Path,
) -> None:
    store = _ScriptedLedgerStore("commit-then-unknown")
    engine = _CountingEngine()
    async with _assembly(tmp_path, engine=engine, store=store) as (
        _,
        target,
        artifacts,
        service,
    ):
        report, approval = await _approved(service, artifacts[0])
        with pytest.raises(PromotionWriteError) as raised:
            await service.promote(approval_digest=approval.approval_digest)
        assert raised.value.committed is None

        recovered = await service.promote(approval_digest=approval.approval_digest)
        assert recovered.new_revision == report.integration_commit
        assert engine.swaps == 1
        assert git("rev-parse", "refs/heads/main", cwd=target) == (
            report.integration_commit
        )
        assert len((await PromotionLedgerReader(store).load()).promotions) == 1


async def test_repeated_cancellation_before_the_swap_still_converges(
    tmp_path: Path,
) -> None:
    engine = _CountingEngine("before-swap")
    async with _assembly(tmp_path, engine=engine) as (store, target, artifacts, service):
        report, approval = await _approved(service, artifacts[0])
        promoting = asyncio.create_task(
            service.promote(approval_digest=approval.approval_digest)
        )
        await engine.entered.wait()
        promoting.cancel()
        promoting.cancel()
        promoting.cancel()
        assert not promoting.done()
        engine.release.set()
        with pytest.raises(asyncio.CancelledError):
            await promoting

        assert git("rev-parse", "refs/heads/main", cwd=target) == (
            report.integration_commit
        )
        recovered = await service.promote(approval_digest=approval.approval_digest)
        assert recovered.new_revision == report.integration_commit
        assert engine.swaps == 1
        assert len((await PromotionLedgerReader(store).load()).promotions) == 1


async def test_cancellation_after_the_swap_converges_and_records_once(
    tmp_path: Path,
) -> None:
    engine = _CountingEngine("after-swap")
    async with _assembly(tmp_path, engine=engine) as (store, target, artifacts, service):
        report, approval = await _approved(service, artifacts[0])
        promoting = asyncio.create_task(
            service.promote(approval_digest=approval.approval_digest)
        )
        await engine.entered.wait()
        promoting.cancel()
        engine.release.set()
        with pytest.raises(asyncio.CancelledError):
            await promoting

        assert git("rev-parse", "refs/heads/main", cwd=target) == (
            report.integration_commit
        )
        assert engine.swaps == 1
        assert len((await PromotionLedgerReader(store).load()).promotions) == 1


async def test_a_fresh_reader_rebuilds_review_approval_and_promotion(
    tmp_path: Path,
) -> None:
    async with _assembly(tmp_path) as (store, _, artifacts, service):
        report, approval = await _approved(service, artifacts[0])
        promotion = await service.promote(approval_digest=approval.approval_digest)

    ledger = await PromotionLedgerReader(store).load()
    assert ledger.reviews == (report,)
    assert ledger.approvals == (approval,)
    assert ledger.promotions == (promotion,)
    assert ledger.review(report.review_id) == report
    assert ledger.approval_for_digest(approval.approval_digest) == approval
    assert ledger.promotion_for_approval(approval.approval_digest) == promotion
    assert ledger.promotion(promotion.promotion_id) == promotion
    assert ledger.head_seq == 3


# ---------------------------------------------------------------- target safety


async def test_no_durable_promotion_fact_contains_a_local_path(
    tmp_path: Path,
) -> None:
    async with _assembly(tmp_path) as (store, _, artifacts, service):
        _, approval = await _approved(service, artifacts[0])
        await service.promote(approval_digest=approval.approval_digest)

        needles = (
            str(tmp_path),
            str(tmp_path).replace("\\", "/"),
            os.sep,
            "/",
        )
        for event in await store.read(PROMOTION_LEDGER_STREAM):
            rendered = canonical_json(event.data)
            for needle in needles:
                if needle == "/":
                    # ``refs/heads/main`` is a Git ref name, not a filesystem path.
                    assert rendered.count("/") == rendered.count("refs/heads/")* 2
                    continue
                assert needle not in rendered


async def test_a_closed_service_admits_no_new_operation(tmp_path: Path) -> None:
    async with _assembly(tmp_path) as (_, _, artifacts, service):
        _, approval = await _approved(service, artifacts[0])
        await service.promote(approval_digest=approval.approval_digest)
        await service.aclose()
        with pytest.raises(PromotionServiceClosedError):
            await service.review(
                review_request_id="review-request-9",
                artifact_id=artifacts[0].manifest.artifact_id,
                target_id="main-target",
            )


async def test_a_normal_checkout_is_refused_as_a_promotion_target(
    tmp_path: Path,
) -> None:
    source, _ = build_source_repository(tmp_path / "source")
    resolver = promotion_targets("main-target", source)
    with pytest.raises(PromotionGitError) as raised:
        await resolver.resolve("main-target")
    assert raised.value.code == "promotion-target-not-bare"


async def test_a_reparse_point_on_the_target_path_is_refused(
    tmp_path: Path,
) -> None:
    source, _ = build_source_repository(tmp_path / "source")
    target = make_bare_target(source, tmp_path / "target.git")
    link = tmp_path / "linked.git"
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("this platform or user cannot create directory symlinks")
    resolver = promotion_targets("main-target", link)
    with pytest.raises(PromotionGitError) as raised:
        await resolver.resolve("main-target")
    assert raised.value.code == "promotion-target-path-unsafe"


async def test_an_unknown_or_missing_ref_is_refused(tmp_path: Path) -> None:
    source, _ = build_source_repository(tmp_path / "source")
    target = make_bare_target(source, tmp_path / "target.git")
    resolver = LocalBareGitPromotionTargets(
        targets={
            "main-target": PromotionTargetBinding(
                repository_path=target, target_ref="refs/heads/absent"
            )
        }
    )
    with pytest.raises(PromotionGitError) as raised:
        await resolver.resolve("main-target")
    assert raised.value.code == "promotion-target-ref-missing"


async def test_promotion_never_touches_a_working_checkout(tmp_path: Path) -> None:
    async with _assembly(tmp_path) as (_, target, artifacts, service):
        source = tmp_path / "source"
        before = sorted(
            item.name for item in source.iterdir() if item.name != ".git"
        )
        head_before = git("rev-parse", "HEAD", cwd=source)
        _, approval = await _approved(service, artifacts[0])
        await service.promote(approval_digest=approval.approval_digest)

        assert sorted(
            item.name for item in source.iterdir() if item.name != ".git"
        ) == before
        assert git("rev-parse", "HEAD", cwd=source) == head_before
        assert git_status("status", "--porcelain=v1", cwd=source) == (0, b"")
        assert git("rev-parse", "refs/heads/main", cwd=target) != head_before


class _GatedResolver:
    def __init__(self, inner) -> None:
        self.inner = inner
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.gate = False

    async def resolve(self, target_id):
        if self.gate:
            self.entered.set()
            await self.release.wait()
        return await self.inner.resolve(target_id)


async def test_an_in_flight_approval_is_not_shared_with_a_different_payload(
    tmp_path: Path,
) -> None:
    source, _ = build_source_repository(tmp_path / "source")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    patch_source = make_patch(source, scratch, {"added.txt": "added\n"})
    target = make_bare_target(source, tmp_path / "target.git")
    store = InMemoryEventStore()
    cas = LocalArtifactCas(tmp_path / "cas")
    artifact = await record_artifact(store, cas, patch_source)
    resolver = _GatedResolver(promotion_targets("main-target", target))
    service = promotion_service(
        store, cas, resolver, plan=verification_plan()
    )
    report = await service.review(
        review_request_id="review-request-1",
        artifact_id=artifact.manifest.artifact_id,
        target_id="main-target",
    )
    digest = expected_approval_digest(report)

    resolver.gate = True
    first = asyncio.create_task(
        service.approve(
            review_id=report.review_id,
            approval_digest=digest,
            approver_id="release-manager",
            operation_id="approve-1",
        )
    )
    await resolver.entered.wait()
    intruder = asyncio.create_task(
        service.approve(
            review_id=report.review_id,
            approval_digest=digest,
            approver_id="someone-else",
            operation_id="approve-1",
        )
    )
    try:
        for _ in range(8):
            await asyncio.sleep(0)
        assert intruder.done()
        with pytest.raises(PromotionOperationConflictError):
            intruder.result()
    finally:
        resolver.release.set()
        await asyncio.gather(intruder, return_exceptions=True)
    approval = await first
    assert approval.approver_id == "release-manager"
    assert len((await PromotionLedgerReader(store).load()).approvals) == 1
    await service.aclose()
