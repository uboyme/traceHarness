"""The append-only control-flow vocabulary of Patch review and promotion.

One stream carries exactly three facts. Writers and replay share every rule in
this module, so a payload the writer accepted can never be a payload the
projector silently reads differently.
"""

from __future__ import annotations

from datetime import UTC, datetime

from traceh.api.events import EventEnvelope
from traceh.api.json_types import JsonValue, canonical_json
from traceh.api.promotion import VerifierOutcome
from traceh.promotion.errors import PromotionInputError, PromotionProtocolError
from traceh.promotion.models import (
    MAX_OUTPUT_BYTES,
    MAX_PATCH_BYTES,
    MAX_VERIFIER_COMMANDS,
    MERGE_POLICY_VERSION,
    PROMOTION_PROTOCOL_VERSION,
    VERIFIER_STATUSES,
    is_hex_digest,
    promotion_identity,
    protocol_identifier,
    require_bounded_int,
    require_hex_digest,
    require_promotion_identifier,
    require_target_ref,
    review_identity,
    verification_evidence_digest,
    verifier_result_data,
)

PROMOTION_LEDGER_STREAM = "patch-promotions:ledger"
PATCH_REVIEW_RECORDED = "patch/review-recorded"
PATCH_APPROVAL_RECORDED = "patch/approval-recorded"
PATCH_PROMOTION_COMMITTED = "patch/promotion-committed"
PROMOTION_SCHEMA_VERSION = 1

PROMOTION_EVENT_TYPES = (
    PATCH_REVIEW_RECORDED,
    PATCH_APPROVAL_RECORDED,
    PATCH_PROMOTION_COMMITTED,
)

_REVIEW_KEYS = frozenset(
    {
        "review_id",
        "review_request_id",
        "artifact_id",
        "manifest_digest",
        "patch_sha256",
        "patch_size_bytes",
        "target_id",
        "repository_fingerprint",
        "target_ref",
        "expected_revision",
        "integration_tree",
        "integration_commit",
        "verifier_definition_digest",
        "verification_evidence_digest",
        "results",
        "passed",
        "merge_policy_version",
        "promotion_protocol_version",
    }
)

_APPROVAL_KEYS = frozenset(
    {"operation_id", "review_id", "approval_digest", "approver_id"}
)

_PROMOTION_KEYS = frozenset(
    {
        "promotion_id",
        "review_id",
        "approval_digest",
        "target_id",
        "repository_fingerprint",
        "target_ref",
        "previous_revision",
        "new_revision",
        "integration_tree",
        "merge_policy_version",
        "promotion_protocol_version",
    }
)

_RESULT_KEYS = frozenset(
    {
        "command_id",
        "argv_digest",
        "status",
        "exit_code",
        "stdout_sha256",
        "stdout_bytes",
        "stderr_sha256",
        "stderr_bytes",
    }
)


def review_recorded_data(
    *,
    review_request_id: str,
    artifact_id: str,
    manifest_digest: str,
    patch_sha256: str,
    patch_size_bytes: int,
    target_id: str,
    repository_fingerprint: str,
    target_ref: str,
    expected_revision: str,
    integration_tree: str,
    integration_commit: str,
    verifier_definition_digest: str,
    results: tuple[VerifierOutcome, ...],
) -> dict[str, JsonValue]:
    review_request_id = require_promotion_identifier(
        review_request_id, field="review_request_id"
    )
    artifact_id = require_promotion_identifier(artifact_id, field="artifact_id")
    target_id = require_promotion_identifier(target_id, field="target_id")
    if type(results) is not tuple or not results or len(results) > MAX_VERIFIER_COMMANDS:
        raise PromotionInputError("promotion-verifier-result-invalid", "results")
    definition_digest = require_hex_digest(
        verifier_definition_digest,
        lengths=(64,),
        field="verifier-definition-digest",
    )
    payload_results = [verifier_result_data(outcome) for outcome in results]
    return {
        "review_id": review_identity(review_request_id),
        "review_request_id": review_request_id,
        "artifact_id": artifact_id,
        "manifest_digest": require_hex_digest(
            manifest_digest, lengths=(64,), field="manifest-digest"
        ),
        "patch_sha256": require_hex_digest(
            patch_sha256, lengths=(64,), field="patch-digest"
        ),
        "patch_size_bytes": require_bounded_int(
            patch_size_bytes, minimum=0, maximum=MAX_PATCH_BYTES, field="patch-bytes"
        ),
        "target_id": target_id,
        "repository_fingerprint": require_hex_digest(
            repository_fingerprint, lengths=(64,), field="repository-fingerprint"
        ),
        "target_ref": require_target_ref(target_ref),
        "expected_revision": require_hex_digest(
            expected_revision, lengths=(40, 64), field="expected-revision"
        ),
        "integration_tree": require_hex_digest(
            integration_tree, lengths=(40, 64), field="integration-tree"
        ),
        "integration_commit": require_hex_digest(
            integration_commit, lengths=(40, 64), field="integration-commit"
        ),
        "verifier_definition_digest": definition_digest,
        "verification_evidence_digest": verification_evidence_digest(
            definition_digest, results
        ),
        "results": payload_results,
        "passed": all(outcome.passed for outcome in results),
        "merge_policy_version": MERGE_POLICY_VERSION,
        "promotion_protocol_version": PROMOTION_PROTOCOL_VERSION,
    }


def approval_recorded_data(
    *,
    operation_id: str,
    review_id: str,
    approval_digest: str,
    approver_id: str,
) -> dict[str, JsonValue]:
    return {
        "operation_id": require_promotion_identifier(
            operation_id, field="operation_id"
        ),
        "review_id": require_promotion_identifier(review_id, field="review_id"),
        "approval_digest": require_hex_digest(
            approval_digest, lengths=(64,), field="approval-digest"
        ),
        "approver_id": require_promotion_identifier(
            approver_id, field="approver_id"
        ),
    }


def promotion_committed_data(
    *,
    review_id: str,
    approval_digest: str,
    target_id: str,
    repository_fingerprint: str,
    target_ref: str,
    previous_revision: str,
    new_revision: str,
    integration_tree: str,
) -> dict[str, JsonValue]:
    approval_digest = require_hex_digest(
        approval_digest, lengths=(64,), field="approval-digest"
    )
    return {
        "promotion_id": promotion_identity(approval_digest),
        "review_id": require_promotion_identifier(review_id, field="review_id"),
        "approval_digest": approval_digest,
        "target_id": require_promotion_identifier(target_id, field="target_id"),
        "repository_fingerprint": require_hex_digest(
            repository_fingerprint, lengths=(64,), field="repository-fingerprint"
        ),
        "target_ref": require_target_ref(target_ref),
        "previous_revision": require_hex_digest(
            previous_revision, lengths=(40, 64), field="previous-revision"
        ),
        "new_revision": require_hex_digest(
            new_revision, lengths=(40, 64), field="new-revision"
        ),
        "integration_tree": require_hex_digest(
            integration_tree, lengths=(40, 64), field="integration-tree"
        ),
        "merge_policy_version": MERGE_POLICY_VERSION,
        "promotion_protocol_version": PROMOTION_PROTOCOL_VERSION,
    }


def promotion_event_header(
    event: EventEnvelope,
) -> tuple[str, dict[str, JsonValue], datetime, int]:
    """Validate the parts every promotion fact shares before reading payload.

    The store hands back objects the projector does not own, so reading an
    attribute can itself fail. ``Exception`` is normalised into the stable
    protocol error; ``BaseException`` is not, because `KeyboardInterrupt` and
    `SystemExit` are not answers about a payload.
    """

    try:
        return _promotion_event_header(event)
    except PromotionProtocolError:
        raise
    except Exception:
        raise PromotionProtocolError("promotion-payload-invalid", _safe_seq(event)) from None


def _promotion_event_header(
    event: EventEnvelope,
) -> tuple[str, dict[str, JsonValue], datetime, int]:
    if type(event.stream_id) is not str or event.stream_id != PROMOTION_LEDGER_STREAM:
        raise PromotionProtocolError("promotion-stream-unexpected", _safe_seq(event))
    seq = event.seq
    if type(seq) is not int or seq < 1:
        raise PromotionProtocolError("promotion-sequence-invalid", 0)
    if (
        type(event.schema_version) is not int
        or event.schema_version != PROMOTION_SCHEMA_VERSION
    ):
        raise PromotionProtocolError("promotion-schema-version-unsupported", seq)
    if type(event.type) is not str or event.type not in PROMOTION_EVENT_TYPES:
        raise PromotionProtocolError("promotion-event-type-unknown", seq)
    if type(event.occurred_at) is not datetime or event.occurred_at.tzinfo is None:
        raise PromotionProtocolError("promotion-recorded-at-invalid", seq)
    data = event.data
    if type(data) is not dict:
        raise PromotionProtocolError("promotion-payload-invalid", seq)
    return str(event.type), data, event.occurred_at.astimezone(UTC), seq


def normalized_review_payload(
    data: dict[str, JsonValue], seq: int
) -> dict[str, JsonValue]:
    if set(data) != _REVIEW_KEYS:
        raise PromotionProtocolError("promotion-payload-keys-unexpected", seq)
    review_request_id = protocol_identifier(
        data.get("review_request_id"), "review_request_id", seq
    )
    review_id = protocol_identifier(data.get("review_id"), "review_id", seq)
    if review_id != review_identity(review_request_id):
        raise PromotionProtocolError("promotion-review-id-invalid", seq)
    results = _normalized_results(data.get("results"), seq)
    definition_digest = _digest(data, "verifier_definition_digest", (64,), seq)
    evidence_digest = _digest(data, "verification_evidence_digest", (64,), seq)
    outcomes = tuple(_outcome(item) for item in results)
    if evidence_digest != verification_evidence_digest(definition_digest, outcomes):
        raise PromotionProtocolError("promotion-evidence-digest-invalid", seq)
    passed = data.get("passed")
    if type(passed) is not bool:
        raise PromotionProtocolError("promotion-passed-invalid", seq)
    if passed != all(outcome.passed for outcome in outcomes):
        raise PromotionProtocolError("promotion-passed-invalid", seq)
    return {
        "review_id": review_id,
        "review_request_id": review_request_id,
        "artifact_id": protocol_identifier(data.get("artifact_id"), "artifact_id", seq),
        "manifest_digest": _digest(data, "manifest_digest", (64,), seq),
        "patch_sha256": _digest(data, "patch_sha256", (64,), seq),
        "patch_size_bytes": _integer(
            data, "patch_size_bytes", seq, minimum=0, maximum=MAX_PATCH_BYTES
        ),
        "target_id": protocol_identifier(data.get("target_id"), "target_id", seq),
        "repository_fingerprint": _digest(data, "repository_fingerprint", (64,), seq),
        "target_ref": _target_ref(data, seq),
        "expected_revision": _digest(data, "expected_revision", (40, 64), seq),
        "integration_tree": _digest(data, "integration_tree", (40, 64), seq),
        "integration_commit": _digest(data, "integration_commit", (40, 64), seq),
        "verifier_definition_digest": definition_digest,
        "verification_evidence_digest": evidence_digest,
        "results": results,
        "passed": passed,
        "merge_policy_version": _integer(
            data,
            "merge_policy_version",
            seq,
            minimum=MERGE_POLICY_VERSION,
            maximum=MERGE_POLICY_VERSION,
        ),
        "promotion_protocol_version": _integer(
            data,
            "promotion_protocol_version",
            seq,
            minimum=PROMOTION_PROTOCOL_VERSION,
            maximum=PROMOTION_PROTOCOL_VERSION,
        ),
    }


def normalized_approval_payload(
    data: dict[str, JsonValue], seq: int
) -> dict[str, JsonValue]:
    if set(data) != _APPROVAL_KEYS:
        raise PromotionProtocolError("promotion-payload-keys-unexpected", seq)
    return {
        "operation_id": protocol_identifier(
            data.get("operation_id"), "operation_id", seq
        ),
        "review_id": protocol_identifier(data.get("review_id"), "review_id", seq),
        "approval_digest": _digest(data, "approval_digest", (64,), seq),
        "approver_id": protocol_identifier(data.get("approver_id"), "approver_id", seq),
    }


def normalized_promotion_payload(
    data: dict[str, JsonValue], seq: int
) -> dict[str, JsonValue]:
    if set(data) != _PROMOTION_KEYS:
        raise PromotionProtocolError("promotion-payload-keys-unexpected", seq)
    approval_digest = _digest(data, "approval_digest", (64,), seq)
    promotion_id = protocol_identifier(data.get("promotion_id"), "promotion_id", seq)
    if promotion_id != promotion_identity(approval_digest):
        raise PromotionProtocolError("promotion-id-invalid", seq)
    return {
        "promotion_id": promotion_id,
        "review_id": protocol_identifier(data.get("review_id"), "review_id", seq),
        "approval_digest": approval_digest,
        "target_id": protocol_identifier(data.get("target_id"), "target_id", seq),
        "repository_fingerprint": _digest(data, "repository_fingerprint", (64,), seq),
        "target_ref": _target_ref(data, seq),
        "previous_revision": _digest(data, "previous_revision", (40, 64), seq),
        "new_revision": _digest(data, "new_revision", (40, 64), seq),
        "integration_tree": _digest(data, "integration_tree", (40, 64), seq),
        "merge_policy_version": _integer(
            data,
            "merge_policy_version",
            seq,
            minimum=MERGE_POLICY_VERSION,
            maximum=MERGE_POLICY_VERSION,
        ),
        "promotion_protocol_version": _integer(
            data,
            "promotion_protocol_version",
            seq,
            minimum=PROMOTION_PROTOCOL_VERSION,
            maximum=PROMOTION_PROTOCOL_VERSION,
        ),
    }


def is_promotion_fact(
    event: EventEnvelope, event_type: str, data: dict[str, JsonValue]
) -> bool:
    """Whether ``event`` is exactly the fact a failed append tried to write."""

    try:
        actual_type, _, _, _ = promotion_event_header(event)
    except PromotionProtocolError:
        return False
    if actual_type != event_type:
        return False
    # An encoding failure is unknowable and intentionally propagates to the
    # shared reconciler, which maps it to ``None`` rather than false absence.
    return canonical_json(event.data) == canonical_json(data)


def _outcome(item: object) -> VerifierOutcome:
    assert type(item) is dict
    return VerifierOutcome(
        command_id=str(item["command_id"]),
        argv_digest=str(item["argv_digest"]),
        status=str(item["status"]),
        exit_code=None if item["exit_code"] is None else int(item["exit_code"]),  # type: ignore[arg-type]
        stdout_sha256=str(item["stdout_sha256"]),
        stdout_bytes=int(item["stdout_bytes"]),  # type: ignore[arg-type]
        stderr_sha256=str(item["stderr_sha256"]),
        stderr_bytes=int(item["stderr_bytes"]),  # type: ignore[arg-type]
    )


def _normalized_results(value: object, seq: int) -> list[dict[str, JsonValue]]:
    if type(value) is not list or not value or len(value) > MAX_VERIFIER_COMMANDS:
        raise PromotionProtocolError("promotion-verifier-result-invalid", seq)
    normalized: list[dict[str, JsonValue]] = []
    identifiers: set[str] = set()
    for item in value:
        if type(item) is not dict or set(item) != _RESULT_KEYS:
            raise PromotionProtocolError("promotion-verifier-result-invalid", seq)
        command_id = protocol_identifier(item.get("command_id"), "command_id", seq)
        if command_id in identifiers:
            raise PromotionProtocolError("promotion-verifier-result-invalid", seq)
        identifiers.add(command_id)
        status = item.get("status")
        if type(status) is not str or status not in VERIFIER_STATUSES:
            raise PromotionProtocolError("promotion-verifier-status-invalid", seq)
        exit_code = item.get("exit_code")
        if exit_code is not None and (
            type(exit_code) is not int
            or exit_code < -1_000_000
            or exit_code > 1_000_000
        ):
            raise PromotionProtocolError("promotion-verifier-result-invalid", seq)
        if status == "passed" and exit_code != 0:
            raise PromotionProtocolError("promotion-verifier-status-invalid", seq)
        normalized.append(
            {
                "command_id": command_id,
                "argv_digest": _digest(item, "argv_digest", (64,), seq),
                "status": status,
                "exit_code": exit_code,
                "stdout_sha256": _digest(item, "stdout_sha256", (64,), seq),
                "stdout_bytes": _integer(
                    item, "stdout_bytes", seq, minimum=0, maximum=MAX_OUTPUT_BYTES
                ),
                "stderr_sha256": _digest(item, "stderr_sha256", (64,), seq),
                "stderr_bytes": _integer(
                    item, "stderr_bytes", seq, minimum=0, maximum=MAX_OUTPUT_BYTES
                ),
            }
        )
    return normalized


def _digest(
    data: dict[str, JsonValue], key: str, lengths: tuple[int, ...], seq: int
) -> str:
    value = data.get(key)
    if not is_hex_digest(value, lengths):
        raise PromotionProtocolError(
            f"promotion-{key.replace('_', '-')}-invalid", seq
        )
    assert isinstance(value, str)
    return str(value)


def _integer(
    data: dict[str, JsonValue],
    key: str,
    seq: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = data.get(key)
    if type(value) is not int or value < minimum or value > maximum:
        raise PromotionProtocolError(
            f"promotion-{key.replace('_', '-')}-invalid", seq
        )
    return value


def _target_ref(data: dict[str, JsonValue], seq: int) -> str:
    try:
        return require_target_ref(data.get("target_ref"))
    except PromotionInputError:
        raise PromotionProtocolError("promotion-target-ref-invalid", seq) from None


def _safe_seq(event: object) -> int:
    try:
        seq = event.seq  # type: ignore[attr-defined]
    except Exception:
        return 0
    return seq if type(seq) is int and seq >= 0 else 0


__all__ = [
    "PATCH_APPROVAL_RECORDED",
    "PATCH_PROMOTION_COMMITTED",
    "PATCH_REVIEW_RECORDED",
    "PROMOTION_EVENT_TYPES",
    "PROMOTION_LEDGER_STREAM",
    "PROMOTION_SCHEMA_VERSION",
    "approval_recorded_data",
    "is_promotion_fact",
    "normalized_approval_payload",
    "normalized_promotion_payload",
    "normalized_review_payload",
    "promotion_committed_data",
    "promotion_event_header",
    "review_recorded_data",
]
