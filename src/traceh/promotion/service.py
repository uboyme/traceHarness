"""The host-owned Review, Approval and Promotion transactions.

This service is the only writer of ``patch-promotions:ledger``. It owns no
Agent scheduler, no Activation table, no Workspace state and no second Artifact
fact source: every decision is taken against a fresh replay of the durable
Artifact Catalog, the promotion ledger and the real Git target.

Three transactions, three linearization points:

* Review appends one immutable report after the integration commit and the
  fixed host verification have both been observed twice.
* Approval appends one exact human digest over an already durable Review.
* Promotion moves the target ref with ``git update-ref <ref> <new> <old>`` and
  only then records the result. Git mutation and Event append are not one
  transaction, so the ref is re-observed and the three possible values -
  approved-new, expected-old and anything else - are handled explicitly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from traceh.agents.commit_reconciliation import committed_after_failure
from traceh.api.events import EventEnvelope, PendingEvent
from traceh.api.json_types import JsonValue
from traceh.api.promotion import (
    PatchApproval,
    PatchPromotion,
    PatchReviewReport,
    PromotionTarget,
    PromotionTargetResolver,
    VerificationPlan,
    VerifierOutcome,
)
from traceh.artifacts.reader import PatchArtifactReader
from traceh.concurrency import await_worker_convergence
from traceh.promotion.errors import (
    PromotionApprovalError,
    PromotionInputError,
    PromotionLedgerConflictError,
    PromotionNotFoundError,
    PromotionOperationConflictError,
    PromotionServiceClosedError,
    PromotionStateError,
    PromotionTargetDriftError,
    PromotionWriteError,
)
from traceh.promotion.events import (
    PATCH_APPROVAL_RECORDED,
    PATCH_PROMOTION_COMMITTED,
    PATCH_REVIEW_RECORDED,
    PROMOTION_LEDGER_STREAM,
    PROMOTION_SCHEMA_VERSION,
    approval_recorded_data,
    is_promotion_fact,
    promotion_committed_data,
    review_recorded_data,
)
from traceh.promotion.local_git import LocalGitPromotionEngine
from traceh.promotion.models import (
    expected_approval_digest,
    freeze_verification_plan,
    freeze_verifier_outcome,
    integration_commit_message,
    is_hex_digest,
    promotion_identity,
    promotion_operation_digest,
    require_hex_digest,
    require_promotion_identifier,
    require_target_ref,
    review_identity,
    review_matches_verification_plan,
    verification_evidence_digest,
    verifier_command_digest,
    verifier_definition_digest,
)
from traceh.promotion.projection import PromotionLedger, PromotionLedgerReader
from traceh.promotion.verification import (
    HostVerificationRunner,
    VerificationEvidence,
    VerificationRunner,
)
from traceh.session.event_store import ConcurrencyConflict, Durability, EventStore
from traceh.supervision.execution import durable_log_identity

MAX_APPEND_ATTEMPTS = 8
"""How often a ledger compare-and-swap may lose to another writer.

The bound matters most after a successful ``update-ref``: the Git mutation has
already happened, so refusing to persist it because an unrelated writer won a
race would leave a promoted ref with no durable record.
"""


class PatchPromotionService:
    """Verify, approve and promote exactly one immutable Patch Artifact."""

    __slots__ = (
        "_artifacts",
        "_close_task",
        "_closed",
        "_definition_digest",
        "_engine",
        "_ledger",
        "_lock",
        "_pending",
        "_plan",
        "_resolver",
        "_runner",
        "_store",
    )

    def __init__(
        self,
        store: EventStore,
        artifacts: PatchArtifactReader,
        resolver: PromotionTargetResolver,
        *,
        plan: VerificationPlan,
        engine: LocalGitPromotionEngine | None = None,
        runner: VerificationRunner | None = None,
    ) -> None:
        if type(artifacts) is not PatchArtifactReader:
            raise PromotionInputError("promotion-artifact-reader-invalid", "artifacts")
        if durable_log_identity(store) is not durable_log_identity(artifacts.store):
            raise PromotionInputError("promotion-store-mismatch", "store")
        self._store = store
        self._artifacts = artifacts
        self._resolver = resolver
        self._plan = freeze_verification_plan(plan)
        self._engine = LocalGitPromotionEngine() if engine is None else engine
        self._runner = HostVerificationRunner() if runner is None else runner
        self._ledger = PromotionLedgerReader(store)
        self._lock = asyncio.Lock()
        self._definition_digest = verifier_definition_digest(self._plan)
        self._pending: dict[tuple[str, str], tuple[str, asyncio.Task[Any]]] = {}
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    @property
    def store(self) -> EventStore:
        return self._store

    @property
    def plan(self) -> VerificationPlan:
        return self._plan

    async def ledger(self) -> PromotionLedger:
        return await self._ledger.load()

    # ---------------------------------------------------------------- review

    async def review(
        self, *, review_request_id: str, artifact_id: str, target_id: str
    ) -> PatchReviewReport:
        """Verify one exact Patch against one exact target revision."""

        review_request_id = require_promotion_identifier(
            review_request_id, field="review_request_id"
        )
        artifact_id = require_promotion_identifier(artifact_id, field="artifact_id")
        target_id = require_promotion_identifier(target_id, field="target_id")
        review_id = review_identity(review_request_id)
        return await self._owned(
            ("review", review_id),
            promotion_operation_digest(
                "patch-review",
                review_request_id=review_request_id,
                artifact_id=artifact_id,
                target_id=target_id,
                verifier_definition_digest=self._definition_digest,
            ),
            lambda: self._review(review_request_id, artifact_id, target_id),
            name="traceh-patch-review",
        )

    async def _review(
        self, review_request_id: str, artifact_id: str, target_id: str
    ) -> PatchReviewReport:
        review_id = review_identity(review_request_id)
        ledger = await self._ledger.load()
        existing = ledger.review(review_id)
        if existing is not None:
            self._require_same_review(existing, artifact_id, target_id)
            return existing

        artifact = await self._artifacts.load(artifact_id)
        target = await self._resolve(target_id)
        message = integration_commit_message(
            artifact_id=artifact.manifest.artifact_id,
            manifest_digest=artifact.manifest.manifest_digest,
        )
        async with self._engine.integration(
            target, patch=artifact.content, message=message
        ) as environment:
            evidence = await self._verify(environment.root)
            build = await environment.reverify()

        if (
            build.integration_tree != environment.build.integration_tree
            or build.integration_commit != environment.build.integration_commit
        ):
            raise PromotionStateError("promotion-integration-drift")

        confirmed = await self._artifacts.load(artifact_id)
        if (
            confirmed.manifest.manifest_digest != artifact.manifest.manifest_digest
            or confirmed.manifest.blob.sha256 != artifact.manifest.blob.sha256
            or confirmed.content != artifact.content
        ):
            raise PromotionStateError("promotion-artifact-drift")

        await self._engine.require_target_identity(target)
        current = await self._engine.read_ref(target)
        if current != target.expected_revision:
            raise PromotionTargetDriftError

        data = review_recorded_data(
            review_request_id=review_request_id,
            artifact_id=artifact.manifest.artifact_id,
            manifest_digest=artifact.manifest.manifest_digest,
            patch_sha256=artifact.manifest.blob.sha256,
            patch_size_bytes=artifact.manifest.blob.size_bytes,
            target_id=target.target_id,
            repository_fingerprint=target.repository_fingerprint,
            target_ref=target.target_ref,
            expected_revision=target.expected_revision,
            integration_tree=build.integration_tree,
            integration_commit=build.integration_commit,
            verifier_definition_digest=evidence.definition_digest,
            results=evidence.results,
        )

        def find(current_ledger: PromotionLedger) -> PatchReviewReport | None:
            found = current_ledger.review(review_id)
            if found is None:
                return None
            self._require_same_review(found, artifact_id, target_id)
            return found

        return await self._record(PATCH_REVIEW_RECORDED, data, find)

    def _require_same_review(
        self, review: PatchReviewReport, artifact_id: str, target_id: str
    ) -> None:
        """A recorded review may only satisfy an identical request.

        The review id is derived from the request id alone, so the rest of the
        operation definition has to be compared explicitly. Returning a report
        produced by a different Patch, target or verifier definition would hand
        the caller evidence about something it never asked to verify.
        """

        if (
            review.artifact_id != artifact_id
            or review.target_id != target_id
            or not review_matches_verification_plan(review, self._plan)
        ):
            raise PromotionOperationConflictError

    async def _verify(self, root: Path) -> VerificationEvidence:
        evidence = await self._runner.run(self._plan, cwd=root)
        if type(evidence) is not VerificationEvidence:
            raise PromotionStateError("promotion-verification-invalid")
        if evidence.definition_digest != self._definition_digest:
            raise PromotionStateError("promotion-verifier-definition-mismatch")
        if type(evidence.results) is not tuple or len(evidence.results) != len(
            self._plan.commands
        ):
            raise PromotionStateError("promotion-verification-invalid")
        # Each recorded result must be the result of the corresponding frozen
        # command, in order. A digest over the whole set proves nothing if the
        # set itself describes commands the plan never contained.
        for command, outcome in zip(self._plan.commands, evidence.results, strict=True):
            if type(outcome) is not VerifierOutcome:
                raise PromotionStateError("promotion-verifier-result-mismatch")
            freeze_verifier_outcome(outcome)
            if outcome.command_id != command.command_id:
                raise PromotionStateError("promotion-verifier-result-mismatch")
            if outcome.argv_digest != verifier_command_digest(command):
                raise PromotionStateError("promotion-verifier-result-mismatch")
        if evidence.evidence_digest != verification_evidence_digest(
            self._definition_digest, evidence.results
        ):
            raise PromotionStateError("promotion-verifier-evidence-mismatch")
        if evidence.passed is not all(item.passed for item in evidence.results):
            raise PromotionStateError("promotion-verification-invalid")
        return evidence

    # -------------------------------------------------------------- approval

    async def approve(
        self,
        *,
        review_id: str,
        approval_digest: str,
        approver_id: str,
        operation_id: str,
    ) -> PatchApproval:
        """Record one explicit human approval of an exact Review digest.

        There is no ``approved=True`` form and no model-visible Tool: the caller
        must reproduce the complete content digest of the Review it approves.
        """

        review_id = require_promotion_identifier(review_id, field="review_id")
        approver_id = require_promotion_identifier(approver_id, field="approver_id")
        operation_id = require_promotion_identifier(
            operation_id, field="operation_id"
        )
        if not is_hex_digest(approval_digest, (64,)):
            raise PromotionInputError(
                "promotion-approval-digest-invalid", "approval_digest"
            )
        digest = str(approval_digest)
        return await self._owned(
            ("approve", operation_id),
            promotion_operation_digest(
                "patch-approval",
                operation_id=operation_id,
                review_id=review_id,
                approval_digest=digest,
                approver_id=approver_id,
            ),
            lambda: self._approve(review_id, digest, approver_id, operation_id),
            name="traceh-patch-approval",
        )

    async def _approve(
        self,
        review_id: str,
        approval_digest: str,
        approver_id: str,
        operation_id: str,
    ) -> PatchApproval:
        ledger = await self._ledger.load()
        data = approval_recorded_data(
            operation_id=operation_id,
            review_id=review_id,
            approval_digest=approval_digest,
            approver_id=approver_id,
        )
        recorded = ledger.approval_for_operation(operation_id)
        if recorded is not None:
            _require_same_approval(recorded, data)

        review = ledger.review(review_id)
        if review is None:
            raise PromotionNotFoundError("promotion-review-unknown")
        if not review_matches_verification_plan(review, self._plan):
            raise PromotionApprovalError(
                "promotion-review-verification-mismatch"
            )
        if recorded is not None:
            return recorded
        if not review.passed:
            raise PromotionApprovalError("promotion-review-not-passed")
        if approval_digest != expected_approval_digest(review):
            raise PromotionApprovalError("promotion-approval-digest-stale")
        if ledger.approval_for_review(review_id) is not None:
            raise PromotionOperationConflictError

        target = await self._resolve(review.target_id)
        if (
            target.target_id != review.target_id
            or target.repository_fingerprint != review.repository_fingerprint
            or target.target_ref != review.target_ref
            or target.expected_revision != review.expected_revision
        ):
            raise PromotionTargetDriftError

        def find(current_ledger: PromotionLedger) -> PatchApproval | None:
            found = current_ledger.approval_for_operation(operation_id)
            if found is None:
                other = current_ledger.approval_for_review(review_id)
                if other is not None:
                    raise PromotionOperationConflictError
                return None
            _require_same_approval(found, data)
            return found

        return await self._record(PATCH_APPROVAL_RECORDED, data, find)

    # ------------------------------------------------------------- promotion

    async def promote(self, *, approval_digest: str) -> PatchPromotion:
        """Move the target ref to the approved commit, or fail closed."""

        if not is_hex_digest(approval_digest, (64,)):
            raise PromotionInputError(
                "promotion-approval-digest-invalid", "approval_digest"
            )
        digest = str(approval_digest)
        return await self._owned(
            ("promote", promotion_identity(digest)),
            promotion_operation_digest("patch-promotion", approval_digest=digest),
            lambda: self._promote(digest),
            name="traceh-patch-promotion",
        )

    async def _promote(self, approval_digest: str) -> PatchPromotion:
        ledger = await self._ledger.load()
        recorded = ledger.promotion_for_approval(approval_digest)
        approval = ledger.approval_for_digest(approval_digest)
        if approval is None:
            raise PromotionApprovalError("promotion-approval-unknown")
        review = ledger.review(approval.review_id)
        if review is None or not review.passed:
            raise PromotionApprovalError("promotion-review-not-passed")
        if not review_matches_verification_plan(review, self._plan):
            raise PromotionApprovalError(
                "promotion-review-verification-mismatch"
            )
        if approval_digest != expected_approval_digest(review):
            raise PromotionApprovalError("promotion-approval-digest-stale")
        if recorded is not None:
            return recorded

        artifact = await self._artifacts.load(review.artifact_id)
        if (
            artifact.manifest.manifest_digest != review.manifest_digest
            or artifact.manifest.blob.sha256 != review.patch_sha256
            or artifact.manifest.blob.size_bytes != review.patch_size_bytes
        ):
            raise PromotionStateError("promotion-artifact-drift")

        target = await self._resolve(review.target_id)
        if (
            target.target_id != review.target_id
            or target.repository_fingerprint != review.repository_fingerprint
            or target.target_ref != review.target_ref
        ):
            raise PromotionTargetDriftError
        await self._engine.require_target_identity(target)

        converged = await self._ref_state(target, review)
        message = integration_commit_message(
            artifact_id=review.artifact_id, manifest_digest=review.manifest_digest
        )
        build = await self._engine.rebuild_in_target(
            target,
            patch=artifact.content,
            message=message,
            expected_revision=review.expected_revision,
        )
        if (
            build.integration_tree != review.integration_tree
            or build.integration_commit != review.integration_commit
        ):
            raise PromotionStateError("promotion-reconstruction-mismatch")

        if converged:
            # Reconstruction takes time, and another writer may move the ref
            # while it runs. Re-observe before recording that this promotion is
            # the ref's current state; falling back to expected-old means
            # someone reverted it, which is drift rather than a retry.
            if not await self._ref_state(target, review):
                raise PromotionTargetDriftError
        else:
            await self._compare_and_swap(target, review)

        data = promotion_committed_data(
            review_id=review.review_id,
            approval_digest=approval_digest,
            target_id=review.target_id,
            repository_fingerprint=review.repository_fingerprint,
            target_ref=review.target_ref,
            previous_revision=review.expected_revision,
            new_revision=review.integration_commit,
            integration_tree=review.integration_tree,
        )

        def find(current_ledger: PromotionLedger) -> PatchPromotion | None:
            return current_ledger.promotion_for_approval(approval_digest)

        return await self._record(PATCH_PROMOTION_COMMITTED, data, find)

    async def _ref_state(
        self, target: PromotionTarget, review: PatchReviewReport
    ) -> bool:
        """Whether the Git mutation already converged; drift fails closed."""

        current = await self._engine.read_ref(target)
        if current == review.integration_commit:
            return True
        if current == review.expected_revision:
            return False
        raise PromotionTargetDriftError

    async def _compare_and_swap(
        self, target: PromotionTarget, review: PatchReviewReport
    ) -> None:
        swapped = await self._engine.compare_and_swap(
            target,
            expected_old=review.expected_revision,
            new=review.integration_commit,
        )
        # "The call returned" is not the fact; the ref is. Re-read it either way.
        current = await self._engine.read_ref(target)
        if current == review.integration_commit:
            return
        if current == review.expected_revision:
            raise PromotionStateError(
                "promotion-ref-update-rejected"
                if not swapped
                else "promotion-ref-update-unknown"
            )
        raise PromotionTargetDriftError

    # ------------------------------------------------------------- internals

    async def _resolve(self, target_id: str) -> PromotionTarget:
        target = await self._resolver.resolve(target_id)
        if type(target) is not PromotionTarget:
            raise PromotionInputError("promotion-target-invalid", "target")
        resolved = require_promotion_identifier(target.target_id, field="target_id")
        if resolved != target_id:
            raise PromotionInputError("promotion-target-invalid", "target")
        require_hex_digest(
            target.repository_fingerprint,
            lengths=(64,),
            field="repository-fingerprint",
        )
        require_hex_digest(
            target.expected_revision, lengths=(40, 64), field="expected-revision"
        )
        require_target_ref(target.target_ref)
        try:
            repository = Path(target.repository_path)
        except Exception:
            raise PromotionInputError("promotion-target-path-invalid", "target") from None
        if not repository.is_absolute():
            raise PromotionInputError("promotion-target-path-invalid", "target")
        return target

    async def _record[T](
        self,
        event_type: str,
        data: dict[str, JsonValue],
        find: Callable[[PromotionLedger], T | None],
    ) -> T:
        attempts = 0
        while True:
            attempts += 1
            ledger = await self._ledger.load()
            found = find(ledger)
            if found is not None:
                return found
            try:
                await self._store.append(
                    PROMOTION_LEDGER_STREAM,
                    expected_seq=ledger.head_seq,
                    events=(
                        PendingEvent(
                            type=event_type,
                            data=data,
                            schema_version=PROMOTION_SCHEMA_VERSION,
                        ),
                    ),
                    durability=Durability.SYNC,
                )
            except asyncio.CancelledError as error:
                await self._committed(event_type, data)
                raise error
            except Exception as error:
                committed = await self._committed(event_type, data)
                if committed is True:
                    break
                if isinstance(error, ConcurrencyConflict) and committed is False:
                    if attempts >= MAX_APPEND_ATTEMPTS:
                        raise PromotionLedgerConflictError from None
                    continue
                raise PromotionWriteError(committed=committed) from None
            break
        stored = find(await self._ledger.load())
        if stored is None:
            raise PromotionWriteError(committed=None)
        return stored

    async def _committed(
        self, event_type: str, data: dict[str, JsonValue]
    ) -> bool | None:
        def matches(event: EventEnvelope) -> bool:
            return is_promotion_fact(event, event_type, data)

        return await committed_after_failure(self._ledger.read_events, matches)

    async def _owned[T](
        self,
        key: tuple[str, str],
        operation_digest: str,
        factory: Callable[[], Coroutine[Any, Any, T]],
        *,
        name: str,
    ) -> T:
        async with self._lock:
            if self._closed:
                raise PromotionServiceClosedError
            entry = self._pending.get(key)
            if entry is None:
                task = asyncio.create_task(factory(), name=name)
                self._pending[key] = (operation_digest, task)
            else:
                recorded, task = entry
                # Sharing an in-flight task is only correct when the request is
                # the same request. Otherwise the second caller would receive a
                # receipt for work it never described.
                if recorded != operation_digest:
                    raise PromotionOperationConflictError
        try:
            return await converge_promotion_task(task)
        finally:
            if task.done():
                async with self._lock:
                    current = self._pending.get(key)
                    if current is not None and current[1] is task:
                        self._pending.pop(key, None)

    async def aclose(self) -> None:
        async with self._lock:
            if self._close_task is None:
                self._closed = True
                tasks = tuple(task for _, task in self._pending.values())
                self._close_task = asyncio.create_task(
                    self._close(tasks), name="traceh-patch-promotion-close"
                )
            task = self._close_task
        await converge_promotion_task(task)

    async def _close(self, tasks: tuple[asyncio.Task[Any], ...]) -> None:
        failures: list[BaseException] = []
        for task in tasks:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                await await_worker_convergence(task)
                if task.cancelled():
                    failures.append(error)
                elif task.exception() is not None:
                    failures.append(task.exception())  # type: ignore[arg-type]
            except BaseException as error:
                failures.append(error)
        if failures:
            raise BaseExceptionGroup("Patch promotion close failed", failures)


async def converge_promotion_task[T](task: asyncio.Task[T]) -> T:
    """Wait for owned work; a cancelled caller still gets its own cancellation."""

    cancellation: asyncio.CancelledError | None = None
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as error:
        cancellation = error
        await await_worker_convergence(task)
    if task.cancelled():
        assert cancellation is not None
        raise cancellation
    failure = task.exception()
    if failure is not None:
        if cancellation is not None:
            raise cancellation from failure
        raise failure
    if cancellation is not None:
        raise cancellation
    return task.result()


def _require_same_approval(
    approval: PatchApproval, data: dict[str, JsonValue]
) -> None:
    if (
        approval.operation_id != data["operation_id"]
        or approval.review_id != data["review_id"]
        or approval.approval_digest != data["approval_digest"]
        or approval.approver_id != data["approver_id"]
    ):
        raise PromotionOperationConflictError


__all__ = [
    "MAX_APPEND_ATTEMPTS",
    "PatchPromotionService",
    "converge_promotion_task",
]
