"""The one Projector that rebuilds Review, Approval and Promotion facts.

There is no mutable balance, state file, registry or runtime cache behind these
values. Every relation - which Review a digest approved, which Approval a ref
update executed - is recomputed from the append-only stream on every load.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

from traceh.api.events import EventEnvelope
from traceh.api.promotion import (
    PatchApproval,
    PatchPromotion,
    PatchReviewReport,
    VerifierOutcome,
)
from traceh.promotion.errors import PromotionProtocolError
from traceh.promotion.events import (
    PATCH_APPROVAL_RECORDED,
    PATCH_REVIEW_RECORDED,
    PROMOTION_LEDGER_STREAM,
    normalized_approval_payload,
    normalized_promotion_payload,
    normalized_review_payload,
    promotion_event_header,
)
from traceh.promotion.models import (
    expected_approval_digest,
    promotion_identity,
    review_report_digest,
)
from traceh.session.event_store import EventStore


@dataclass(frozen=True, slots=True)
class PromotionLedgerIssue:
    code: str
    seq: int


class PromotionLedger:
    """Immutable projection of the one global promotion control-flow stream."""

    __slots__ = (
        "_approval_by_digest",
        "_approval_by_review",
        "_approvals",
        "_head_seq",
        "_operations",
        "_promotion_by_approval",
        "_promotions",
        "_review_by_id",
        "_review_by_request",
        "_reviews",
    )

    def __init__(
        self,
        *,
        reviews: tuple[PatchReviewReport, ...],
        approvals: tuple[PatchApproval, ...],
        promotions: tuple[PatchPromotion, ...],
        head_seq: int,
    ) -> None:
        self._reviews = reviews
        self._approvals = approvals
        self._promotions = promotions
        self._head_seq = head_seq
        self._review_by_id = {item.review_id: item for item in reviews}
        self._review_by_request = {item.review_request_id: item for item in reviews}
        self._approval_by_review = {item.review_id: item for item in approvals}
        self._approval_by_digest = {item.approval_digest: item for item in approvals}
        self._operations = {item.operation_id: item for item in approvals}
        self._promotion_by_approval = {
            item.approval_digest: item for item in promotions
        }

    @classmethod
    def empty(cls) -> PromotionLedger:
        return cls(reviews=(), approvals=(), promotions=(), head_seq=0)

    @classmethod
    def rebuild(cls, events: tuple[EventEnvelope, ...]) -> PromotionLedger:
        reviews: list[PatchReviewReport] = []
        approvals: list[PatchApproval] = []
        promotions: list[PatchPromotion] = []
        review_by_id: dict[str, PatchReviewReport] = {}
        request_ids: set[str] = set()
        approval_by_digest: dict[str, PatchApproval] = {}
        approved_reviews: set[str] = set()
        operation_ids: set[str] = set()
        promoted_digests: set[str] = set()
        promotion_ids: set[str] = set()
        expected_seq = 1
        for event in events:
            event_type, data, recorded_at, seq = promotion_event_header(event)
            if seq != expected_seq:
                raise PromotionProtocolError("promotion-sequence-invalid", seq)
            expected_seq += 1
            if event_type == PATCH_REVIEW_RECORDED:
                payload = normalized_review_payload(data, seq)
                review = _review(payload, recorded_at, seq)
                if review.review_id in review_by_id:
                    raise PromotionProtocolError("promotion-review-duplicate", seq)
                if review.review_request_id in request_ids:
                    raise PromotionProtocolError("promotion-request-duplicate", seq)
                review_by_id[review.review_id] = review
                request_ids.add(review.review_request_id)
                reviews.append(review)
            elif event_type == PATCH_APPROVAL_RECORDED:
                payload = normalized_approval_payload(data, seq)
                approval = PatchApproval(
                    operation_id=str(payload["operation_id"]),
                    review_id=str(payload["review_id"]),
                    approval_digest=str(payload["approval_digest"]),
                    approver_id=str(payload["approver_id"]),
                    approved_at=recorded_at,
                    recorded_seq=seq,
                )
                review = review_by_id.get(approval.review_id)
                if review is None:
                    raise PromotionProtocolError("promotion-review-unknown", seq)
                if not review.passed:
                    raise PromotionProtocolError("promotion-review-not-passed", seq)
                if approval.approval_digest != expected_approval_digest(review):
                    raise PromotionProtocolError(
                        "promotion-approval-digest-invalid", seq
                    )
                if approval.operation_id in operation_ids:
                    raise PromotionProtocolError("promotion-operation-duplicate", seq)
                if (
                    approval.review_id in approved_reviews
                    or approval.approval_digest in approval_by_digest
                ):
                    raise PromotionProtocolError("promotion-approval-duplicate", seq)
                operation_ids.add(approval.operation_id)
                approved_reviews.add(approval.review_id)
                approval_by_digest[approval.approval_digest] = approval
                approvals.append(approval)
            else:
                payload = normalized_promotion_payload(data, seq)
                promotion = _promotion(payload, recorded_at, seq)
                approval = approval_by_digest.get(promotion.approval_digest)
                if approval is None:
                    raise PromotionProtocolError("promotion-approval-unknown", seq)
                if promotion.review_id != approval.review_id:
                    raise PromotionProtocolError("promotion-review-mismatch", seq)
                if promotion.promotion_id in promotion_ids:
                    raise PromotionProtocolError("promotion-duplicate", seq)
                if promotion.approval_digest in promoted_digests:
                    raise PromotionProtocolError("promotion-duplicate", seq)
                review = review_by_id[approval.review_id]
                if (
                    promotion.target_id != review.target_id
                    or promotion.repository_fingerprint != review.repository_fingerprint
                    or promotion.target_ref != review.target_ref
                    or promotion.previous_revision != review.expected_revision
                    or promotion.new_revision != review.integration_commit
                    or promotion.integration_tree != review.integration_tree
                ):
                    raise PromotionProtocolError("promotion-binding-invalid", seq)
                promotion_ids.add(promotion.promotion_id)
                promoted_digests.add(promotion.approval_digest)
                promotions.append(promotion)
        return cls(
            reviews=tuple(reviews),
            approvals=tuple(approvals),
            promotions=tuple(promotions),
            head_seq=expected_seq - 1,
        )

    @property
    def head_seq(self) -> int:
        return self._head_seq

    @property
    def reviews(self) -> tuple[PatchReviewReport, ...]:
        return self._reviews

    @property
    def approvals(self) -> tuple[PatchApproval, ...]:
        return self._approvals

    @property
    def promotions(self) -> tuple[PatchPromotion, ...]:
        return self._promotions

    def review(self, review_id: str) -> PatchReviewReport | None:
        return self._review_by_id.get(review_id)

    def review_for_request(self, review_request_id: str) -> PatchReviewReport | None:
        return self._review_by_request.get(review_request_id)

    def approval_for_review(self, review_id: str) -> PatchApproval | None:
        return self._approval_by_review.get(review_id)

    def approval_for_digest(self, approval_digest: str) -> PatchApproval | None:
        return self._approval_by_digest.get(approval_digest)

    def approval_for_operation(self, operation_id: str) -> PatchApproval | None:
        return self._operations.get(operation_id)

    def promotion_for_approval(self, approval_digest: str) -> PatchPromotion | None:
        return self._promotion_by_approval.get(approval_digest)

    def promotion(self, promotion_id: str) -> PatchPromotion | None:
        for item in self._promotions:
            if item.promotion_id == promotion_id:
                return item
        return None

    def __len__(self) -> int:
        return len(self._reviews) + len(self._approvals) + len(self._promotions)

    def __iter__(self) -> Iterator[PatchReviewReport]:
        return iter(self._reviews)


def _review(
    payload: dict[str, object], recorded_at: datetime, seq: int
) -> PatchReviewReport:
    results = tuple(
        VerifierOutcome(
            command_id=str(item["command_id"]),
            argv_digest=str(item["argv_digest"]),
            status=str(item["status"]),
            exit_code=None if item["exit_code"] is None else int(item["exit_code"]),  # type: ignore[arg-type]
            stdout_sha256=str(item["stdout_sha256"]),
            stdout_bytes=int(item["stdout_bytes"]),  # type: ignore[arg-type]
            stderr_sha256=str(item["stderr_sha256"]),
            stderr_bytes=int(item["stderr_bytes"]),  # type: ignore[arg-type]
        )
        for item in payload["results"]  # type: ignore[union-attr]
    )
    return PatchReviewReport(
        review_id=str(payload["review_id"]),
        review_request_id=str(payload["review_request_id"]),
        artifact_id=str(payload["artifact_id"]),
        manifest_digest=str(payload["manifest_digest"]),
        patch_sha256=str(payload["patch_sha256"]),
        patch_size_bytes=int(payload["patch_size_bytes"]),  # type: ignore[arg-type]
        target_id=str(payload["target_id"]),
        repository_fingerprint=str(payload["repository_fingerprint"]),
        target_ref=str(payload["target_ref"]),
        expected_revision=str(payload["expected_revision"]),
        integration_tree=str(payload["integration_tree"]),
        integration_commit=str(payload["integration_commit"]),
        verifier_definition_digest=str(payload["verifier_definition_digest"]),
        verification_evidence_digest=str(payload["verification_evidence_digest"]),
        results=results,
        passed=bool(payload["passed"]),
        merge_policy_version=int(payload["merge_policy_version"]),  # type: ignore[arg-type]
        promotion_protocol_version=int(payload["promotion_protocol_version"]),  # type: ignore[arg-type]
        reviewed_at=recorded_at,
        review_digest=review_report_digest(payload, recorded_at),
        recorded_seq=seq,
    )


def _promotion(
    payload: dict[str, object], recorded_at: datetime, seq: int
) -> PatchPromotion:
    approval_digest = str(payload["approval_digest"])
    promotion_id = str(payload["promotion_id"])
    if promotion_id != promotion_identity(approval_digest):
        raise PromotionProtocolError("promotion-id-invalid", seq)
    return PatchPromotion(
        promotion_id=promotion_id,
        review_id=str(payload["review_id"]),
        approval_digest=approval_digest,
        target_id=str(payload["target_id"]),
        repository_fingerprint=str(payload["repository_fingerprint"]),
        target_ref=str(payload["target_ref"]),
        previous_revision=str(payload["previous_revision"]),
        new_revision=str(payload["new_revision"]),
        integration_tree=str(payload["integration_tree"]),
        merge_policy_version=int(payload["merge_policy_version"]),  # type: ignore[arg-type]
        promotion_protocol_version=int(payload["promotion_protocol_version"]),  # type: ignore[arg-type]
        promoted_at=recorded_at,
        recorded_seq=seq,
    )


class PromotionLedgerReader:
    __slots__ = ("_store",)

    def __init__(self, store: EventStore) -> None:
        self._store = store

    @property
    def store(self) -> EventStore:
        return self._store

    async def read_events(self) -> tuple[EventEnvelope, ...]:
        return await self._store.read(PROMOTION_LEDGER_STREAM)

    async def load(self) -> PromotionLedger:
        return PromotionLedger.rebuild(await self.read_events())


async def validate_promotion_events(
    store: EventStore,
) -> tuple[PromotionLedgerIssue, ...]:
    try:
        await PromotionLedgerReader(store).load()
    except PromotionProtocolError as error:
        return (PromotionLedgerIssue(error.code, error.seq),)
    return ()


__all__ = [
    "PromotionLedger",
    "PromotionLedgerIssue",
    "PromotionLedgerReader",
    "validate_promotion_events",
]
