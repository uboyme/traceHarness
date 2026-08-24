"""D2 protocol, digest-binding and replay rules for the promotion ledger."""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest

from traceh.api.events import EventEnvelope, PendingEvent
from traceh.api.promotion import (
    VerificationPlan,
    VerifierCommand,
    VerifierEnvironmentPolicy,
    VerifierOutcome,
)
from traceh.promotion import (
    PromotionInputError,
    PromotionLedger,
    PromotionProtocolError,
    expected_approval_digest,
    freeze_verification_plan,
    validate_promotion_events,
    verifier_definition_digest,
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
from traceh.promotion.models import require_promotion_identifier, require_target_ref
from traceh.session.event_store import InMemoryEventStore

BASE = "a" * 40
TREE = "b" * 40
COMMIT = "c" * 40
FINGERPRINT = "d" * 64
MANIFEST = "e" * 64
PATCH = "f" * 64
ARTIFACT = f"patch-{'1' * 64}"


def _plan(**overrides) -> VerificationPlan:
    fields = {
        "plan_id": "python-quality",
        "plan_version": 1,
        "commands": (
            VerifierCommand(
                command_id="unit-tests", argv=("verifier", "run"), timeout_ms=1000
            ),
        ),
        "environment": VerifierEnvironmentPolicy(
            policy_id="env-1", passthrough=("PATH",), overrides=(("MODE", "ci"),)
        ),
        "max_output_bytes": 1024,
        "protocol_version": 1,
    }
    fields.update(overrides)
    return VerificationPlan(**fields)  # type: ignore[arg-type]


def _outcome(status: str = "passed", exit_code: int | None = 0) -> VerifierOutcome:
    return VerifierOutcome(
        command_id="unit-tests",
        argv_digest="9" * 64,
        status=status,
        exit_code=exit_code,
        stdout_sha256="0" * 64,
        stdout_bytes=12,
        stderr_sha256="0" * 64,
        stderr_bytes=0,
    )


def _review_data(**overrides):
    fields = {
        "review_request_id": "review-request-1",
        "artifact_id": ARTIFACT,
        "manifest_digest": MANIFEST,
        "patch_sha256": PATCH,
        "patch_size_bytes": 512,
        "target_id": "main-target",
        "repository_fingerprint": FINGERPRINT,
        "target_ref": "refs/heads/main",
        "expected_revision": BASE,
        "integration_tree": TREE,
        "integration_commit": COMMIT,
        "verifier_definition_digest": verifier_definition_digest(_plan()),
        "results": (_outcome(),),
    }
    fields.update(overrides)
    return review_recorded_data(**fields)  # type: ignore[arg-type]


def _envelope(seq: int, event_type: str, data, *, schema: int | None = None) -> EventEnvelope:
    return EventEnvelope.materialize(
        PROMOTION_LEDGER_STREAM,
        seq,
        PendingEvent(
            type=event_type,
            data=data,
            schema_version=PROMOTION_SCHEMA_VERSION if schema is None else schema,
        ),
    )


def _rebuild(*events: EventEnvelope) -> PromotionLedger:
    return PromotionLedger.rebuild(events)


def _approved_chain():
    review_event = _envelope(1, PATCH_REVIEW_RECORDED, _review_data())
    review = _rebuild(review_event).reviews[0]
    digest = expected_approval_digest(review)
    approval_event = _envelope(
        2,
        PATCH_APPROVAL_RECORDED,
        approval_recorded_data(
            operation_id="approve-1",
            review_id=review.review_id,
            approval_digest=digest,
            approver_id="release-manager",
        ),
    )
    promotion_event = _envelope(
        3,
        PATCH_PROMOTION_COMMITTED,
        promotion_committed_data(
            review_id=review.review_id,
            approval_digest=digest,
            target_id=review.target_id,
            repository_fingerprint=review.repository_fingerprint,
            target_ref=review.target_ref,
            previous_revision=review.expected_revision,
            new_revision=review.integration_commit,
            integration_tree=review.integration_tree,
        ),
    )
    return review, digest, (review_event, approval_event, promotion_event)


# --------------------------------------------------------- verifier definition


def test_every_verifier_definition_field_changes_the_definition_digest() -> None:
    original = verifier_definition_digest(_plan())
    variants = (
        _plan(plan_id="other-plan"),
        _plan(plan_version=2),
        _plan(max_output_bytes=2048),
        _plan(
            commands=(
                VerifierCommand(
                    command_id="unit-tests", argv=("verifier", "run", "-x"), timeout_ms=1000
                ),
            )
        ),
        _plan(
            commands=(
                VerifierCommand(
                    command_id="unit-tests", argv=("verifier", "run"), timeout_ms=2000
                ),
            )
        ),
        _plan(
            commands=(
                VerifierCommand(
                    command_id="other-id", argv=("verifier", "run"), timeout_ms=1000
                ),
            )
        ),
        _plan(
            environment=VerifierEnvironmentPolicy(
                policy_id="env-2", passthrough=("PATH",), overrides=(("MODE", "ci"),)
            )
        ),
        _plan(
            environment=VerifierEnvironmentPolicy(
                policy_id="env-1",
                passthrough=("PATH", "HOME"),
                overrides=(("MODE", "ci"),),
            )
        ),
        _plan(
            environment=VerifierEnvironmentPolicy(
                policy_id="env-1", passthrough=("PATH",), overrides=(("MODE", "dev"),)
            )
        ),
    )
    digests = {verifier_definition_digest(variant) for variant in variants}
    assert original not in digests
    assert len(digests) == len(variants)


@pytest.mark.parametrize(
    "plan",
    [
        _plan(commands=()),
        _plan(commands=[]),
        _plan(protocol_version=2),
        _plan(plan_version=0),
        _plan(plan_version=True),
        _plan(max_output_bytes=0),
        _plan(
            commands=(
                VerifierCommand(command_id="c", argv=(), timeout_ms=1000),
            )
        ),
        _plan(
            commands=(
                VerifierCommand(command_id="c", argv=("a\0b",), timeout_ms=1000),
            )
        ),
        _plan(
            commands=(
                VerifierCommand(command_id="c", argv=("verifier",), timeout_ms=True),
            )
        ),
        _plan(
            commands=(
                VerifierCommand(command_id="c", argv=("verifier",), timeout_ms=1.0),
            )
        ),
        _plan(
            commands=(
                VerifierCommand(command_id="c", argv=("verifier",), timeout_ms=0),
            )
        ),
        _plan(
            commands=(
                VerifierCommand(command_id="c", argv=("verifier",), timeout_ms=1000),
                VerifierCommand(command_id="c", argv=("verifier",), timeout_ms=1000),
            )
        ),
        _plan(
            environment=VerifierEnvironmentPolicy(
                policy_id="env", passthrough=("GIT_DIR",), overrides=()
            )
        ),
        _plan(
            environment=VerifierEnvironmentPolicy(
                policy_id="env",
                passthrough=(),
                overrides=(("GIT_CONFIG_PARAMETERS", "x"),),
            )
        ),
        _plan(
            environment=VerifierEnvironmentPolicy(
                policy_id="env", passthrough=("PATH", "PATH"), overrides=()
            )
        ),
        _plan(
            environment=VerifierEnvironmentPolicy(
                policy_id="env", passthrough=("1BAD",), overrides=()
            )
        ),
        _plan(environment=object()),
        "not-a-plan",
    ],
)
def test_hostile_or_incomplete_plans_are_refused(plan) -> None:
    with pytest.raises(PromotionInputError):
        freeze_verification_plan(plan)


@pytest.mark.parametrize(
    "ref",
    [
        "main",
        "refs/tags/v1",
        "refs/heads/",
        "refs/heads/../escape",
        "refs/heads/.hidden",
        "refs/heads/x.lock",
        "refs/heads/with space",
        "refs/heads/with^caret",
        "refs/heads/double//slash",
        "refs/heads/trailing/",
        b"refs/heads/main",
        None,
    ],
)
def test_only_an_explicit_branch_ref_is_accepted(ref) -> None:
    with pytest.raises(PromotionInputError):
        require_target_ref(ref)


def test_a_hostile_string_subclass_is_normalised_or_refused() -> None:
    class Hostile(str):
        def __eq__(self, other: object) -> bool:  # pragma: no cover - never true
            return True

        def __hash__(self) -> int:
            return hash(str(self))

    normalized = require_promotion_identifier(Hostile("agent-1"), field="agent_id")
    assert type(normalized) is str
    with pytest.raises(PromotionInputError):
        review_recorded_data(
            **{
                **{
                    "review_request_id": "review-request-1",
                    "artifact_id": ARTIFACT,
                    "manifest_digest": MANIFEST,
                    "patch_sha256": PATCH,
                    "patch_size_bytes": 512,
                    "target_id": "main-target",
                    "repository_fingerprint": FINGERPRINT,
                    "target_ref": "refs/heads/main",
                    "expected_revision": BASE,
                    "integration_tree": TREE,
                    "integration_commit": COMMIT,
                    "verifier_definition_digest": verifier_definition_digest(_plan()),
                    "results": (_outcome(),),
                },
                "manifest_digest": Hostile(MANIFEST.upper()),
            }
        )


def test_boolean_and_float_quantities_are_not_integers() -> None:
    with pytest.raises(PromotionInputError):
        _review_data(patch_size_bytes=True)
    with pytest.raises(PromotionInputError):
        _review_data(patch_size_bytes=512.0)


def test_an_empty_result_set_is_not_a_verification() -> None:
    with pytest.raises(PromotionInputError):
        _review_data(results=())


# --------------------------------------------------------------- replay rules


def test_a_complete_chain_rebuilds_all_three_facts() -> None:
    review, digest, events = _approved_chain()
    ledger = _rebuild(*events)
    assert ledger.head_seq == 3
    assert ledger.reviews[0].review_id == review.review_id
    assert ledger.approvals[0].approval_digest == digest
    assert ledger.promotions[0].new_revision == review.integration_commit
    assert ledger.approval_for_operation("approve-1") is ledger.approvals[0]
    assert len(ledger) == 3
    assert list(ledger) == list(ledger.reviews)


def test_a_review_digest_is_bound_to_its_recorded_timestamp() -> None:
    event = _envelope(1, PATCH_REVIEW_RECORDED, _review_data())
    later = dataclasses.replace(
        event, occurred_at=event.occurred_at + timedelta(seconds=1)
    )
    first = _rebuild(event).reviews[0]
    second = _rebuild(later).reviews[0]
    assert first.reviewed_at != second.reviewed_at
    assert first.review_digest != second.review_digest
    assert _rebuild(event).reviews[0].review_digest == first.review_digest


def test_a_sequence_gap_is_refused() -> None:
    with pytest.raises(PromotionProtocolError) as raised:
        _rebuild(_envelope(2, PATCH_REVIEW_RECORDED, _review_data()))
    assert raised.value.code == "promotion-sequence-invalid"


def test_an_unknown_schema_version_is_refused() -> None:
    with pytest.raises(PromotionProtocolError) as raised:
        _rebuild(_envelope(1, PATCH_REVIEW_RECORDED, _review_data(), schema=2))
    assert raised.value.code == "promotion-schema-version-unsupported"


def test_an_unknown_event_type_is_refused() -> None:
    with pytest.raises(PromotionProtocolError) as raised:
        _rebuild(_envelope(1, "patch/merged", _review_data()))
    assert raised.value.code == "promotion-event-type-unknown"


@pytest.mark.parametrize("mutation", ["extra", "missing"])
def test_unexpected_payload_keys_are_refused(mutation: str) -> None:
    data = dict(_review_data())
    if mutation == "extra":
        data["repository_path"] = "/somewhere"
    else:
        data.pop("target_ref")
    with pytest.raises(PromotionProtocolError) as raised:
        _rebuild(_envelope(1, PATCH_REVIEW_RECORDED, data))
    assert raised.value.code == "promotion-payload-keys-unexpected"


def test_a_forged_review_identity_is_recomputed_and_refused() -> None:
    data = dict(_review_data())
    data["review_id"] = "review-" + "0" * 64
    with pytest.raises(PromotionProtocolError) as raised:
        _rebuild(_envelope(1, PATCH_REVIEW_RECORDED, data))
    assert raised.value.code == "promotion-review-id-invalid"


def test_a_forged_passed_flag_is_recomputed_and_refused() -> None:
    data = dict(_review_data(results=(_outcome("failed", 1),)))
    data["passed"] = True
    with pytest.raises(PromotionProtocolError) as raised:
        _rebuild(_envelope(1, PATCH_REVIEW_RECORDED, data))
    assert raised.value.code == "promotion-passed-invalid"


def test_a_forged_evidence_digest_is_recomputed_and_refused() -> None:
    data = dict(_review_data())
    data["verification_evidence_digest"] = "0" * 64
    with pytest.raises(PromotionProtocolError) as raised:
        _rebuild(_envelope(1, PATCH_REVIEW_RECORDED, data))
    assert raised.value.code == "promotion-evidence-digest-invalid"


def test_duplicate_reviews_and_requests_are_refused() -> None:
    event = _envelope(1, PATCH_REVIEW_RECORDED, _review_data())
    duplicate = _envelope(2, PATCH_REVIEW_RECORDED, _review_data())
    with pytest.raises(PromotionProtocolError) as raised:
        _rebuild(event, duplicate)
    assert raised.value.code == "promotion-review-duplicate"


def test_an_approval_without_its_review_is_refused() -> None:
    _, digest, events = _approved_chain()
    with pytest.raises(PromotionProtocolError) as raised:
        _rebuild(
            EventEnvelope.materialize(
                PROMOTION_LEDGER_STREAM,
                1,
                PendingEvent(
                    type=PATCH_APPROVAL_RECORDED,
                    data=events[1].data,
                    schema_version=PROMOTION_SCHEMA_VERSION,
                ),
            )
        )
    assert raised.value.code == "promotion-review-unknown"
    assert digest


def test_an_approval_of_a_failed_review_is_refused() -> None:
    review_event = _envelope(
        1, PATCH_REVIEW_RECORDED, _review_data(results=(_outcome("failed", 1),))
    )
    review = _rebuild(review_event).reviews[0]
    approval_event = _envelope(
        2,
        PATCH_APPROVAL_RECORDED,
        approval_recorded_data(
            operation_id="approve-1",
            review_id=review.review_id,
            approval_digest=expected_approval_digest(review),
            approver_id="release-manager",
        ),
    )
    with pytest.raises(PromotionProtocolError) as raised:
        _rebuild(review_event, approval_event)
    assert raised.value.code == "promotion-review-not-passed"


def test_an_approval_digest_that_does_not_match_the_review_is_refused() -> None:
    review_event = _envelope(1, PATCH_REVIEW_RECORDED, _review_data())
    review = _rebuild(review_event).reviews[0]
    approval_event = _envelope(
        2,
        PATCH_APPROVAL_RECORDED,
        approval_recorded_data(
            operation_id="approve-1",
            review_id=review.review_id,
            approval_digest="0" * 64,
            approver_id="release-manager",
        ),
    )
    with pytest.raises(PromotionProtocolError) as raised:
        _rebuild(review_event, approval_event)
    assert raised.value.code == "promotion-approval-digest-invalid"


def test_a_promotion_without_its_approval_is_refused() -> None:
    _, _, events = _approved_chain()
    with pytest.raises(PromotionProtocolError) as raised:
        _rebuild(
            events[0],
            EventEnvelope.materialize(
                PROMOTION_LEDGER_STREAM,
                2,
                PendingEvent(
                    type=PATCH_PROMOTION_COMMITTED,
                    data=events[2].data,
                    schema_version=PROMOTION_SCHEMA_VERSION,
                ),
            ),
        )
    assert raised.value.code == "promotion-approval-unknown"


def test_a_forged_promotion_identity_is_refused() -> None:
    _, _, events = _approved_chain()
    data = dict(events[2].data)
    data["promotion_id"] = "promotion-" + "0" * 64
    with pytest.raises(PromotionProtocolError) as raised:
        _rebuild(events[0], events[1], _envelope(3, PATCH_PROMOTION_COMMITTED, data))
    assert raised.value.code == "promotion-id-invalid"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("target_id", "other-target"),
        ("repository_fingerprint", "9" * 64),
        ("target_ref", "refs/heads/other"),
        ("previous_revision", "9" * 40),
        ("new_revision", "9" * 40),
        ("integration_tree", "9" * 40),
    ],
)
def test_a_promotion_that_contradicts_its_review_is_refused(field, value) -> None:
    _, _, events = _approved_chain()
    data = dict(events[2].data)
    data[field] = value
    with pytest.raises(PromotionProtocolError) as raised:
        _rebuild(events[0], events[1], _envelope(3, PATCH_PROMOTION_COMMITTED, data))
    assert raised.value.code == "promotion-binding-invalid"


def test_duplicate_promotions_are_refused() -> None:
    _, _, events = _approved_chain()
    repeat = _envelope(4, PATCH_PROMOTION_COMMITTED, dict(events[2].data))
    with pytest.raises(PromotionProtocolError) as raised:
        _rebuild(*events, repeat)
    assert raised.value.code == "promotion-duplicate"


async def test_validate_promotion_events_reports_the_first_issue() -> None:
    store = InMemoryEventStore()
    assert await validate_promotion_events(store) == ()
    await store.append(
        PROMOTION_LEDGER_STREAM,
        expected_seq=0,
        events=(
            PendingEvent(
                type="patch/merged",
                data={"anything": 1},
                schema_version=PROMOTION_SCHEMA_VERSION,
            ),
        ),
    )
    issues = await validate_promotion_events(store)
    assert [issue.code for issue in issues] == ["promotion-event-type-unknown"]


def test_is_promotion_fact_refuses_a_hostile_envelope() -> None:
    class HostileType(str):
        def __eq__(self, other: object) -> bool:  # pragma: no cover - never true
            return True

        def __hash__(self) -> int:
            return hash(str(self))

    data = _review_data()
    good = _envelope(1, PATCH_REVIEW_RECORDED, data)
    assert is_promotion_fact(good, PATCH_REVIEW_RECORDED, data) is True
    assert is_promotion_fact(good, PATCH_APPROVAL_RECORDED, data) is False
    hostile = dataclasses.replace(good, type=HostileType(PATCH_REVIEW_RECORDED))
    assert is_promotion_fact(hostile, PATCH_REVIEW_RECORDED, data) is False


def test_a_hostile_envelope_property_becomes_a_stable_protocol_error() -> None:
    class Hostile:
        @property
        def stream_id(self) -> str:
            raise RuntimeError("the envelope refuses to be read")

        seq = 1
        schema_version = PROMOTION_SCHEMA_VERSION
        type = PATCH_REVIEW_RECORDED
        data: dict = {}
        occurred_at = None

    with pytest.raises(PromotionProtocolError) as raised:
        PromotionLedger.rebuild((Hostile(),))  # type: ignore[arg-type]
    assert raised.value.code == "promotion-payload-invalid"


def test_a_hostile_envelope_never_swallows_base_exceptions() -> None:
    class Interrupting:
        @property
        def stream_id(self) -> str:
            raise KeyboardInterrupt

        seq = 1

    with pytest.raises(KeyboardInterrupt):
        PromotionLedger.rebuild((Interrupting(),))  # type: ignore[arg-type]
