"""Canonical identities, frozen host inputs and digests for D2 promotion.

Every derived identity in this module is a deterministic function of already
validated facts. Both the writer and the replay boundary recompute them, so a
shape-valid but incorrect ``review_id``, ``approval_digest`` or ``promotion_id``
in a payload is rejected instead of becoming a second source of truth.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from traceh.agents.identity import is_agent_identifier
from traceh.api.json_types import fingerprint
from traceh.api.promotion import (
    PatchReviewReport,
    VerificationPlan,
    VerifierCommand,
    VerifierEnvironmentPolicy,
    VerifierOutcome,
)
from traceh.promotion.errors import PromotionInputError, PromotionProtocolError

PROMOTION_PROTOCOL_VERSION = 1
"""The only promotion protocol this build reads or writes."""

MERGE_POLICY_VERSION = 1
"""How an approved Patch becomes a target revision.

Version 1 is: apply the exact Patch to the exact expected target revision, keep
the resulting tree, create one single-parent integration commit with a fixed
identity, and move the target ref only through a compare-and-swap against the
expected old revision. There is no merge, rebase, three-way resolution or
force update, so a change to any of those rules must raise this number.
"""

MAX_VERIFIER_COMMANDS = 32
MAX_VERIFIER_ARGUMENTS = 64
MAX_ARGUMENT_LENGTH = 4096
MAX_TIMEOUT_MS = 3_600_000
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_ENVIRONMENT_ENTRIES = 64
MAX_ENVIRONMENT_VALUE_LENGTH = 4096
MAX_INTEGRATION_CHANGED_PATHS = 100_000
MAX_INTEGRATION_WORKTREE_BYTES = 2 * 1024 * 1024 * 1024
"""How much of one integration checkout may be hashed for its proof.

The worktree proof reads every file, so it needs an explicit bound. A
target larger than this is refused rather than silently left unproven.
"""
MAX_PATCH_BYTES = 1024 * 1024 * 1024

VERIFIER_STATUSES = (
    "passed",
    "failed",
    "timed-out",
    "start-failed",
    "output-exceeded",
)

INTEGRATION_AUTHOR_NAME = "TraceHarness Promotion"
INTEGRATION_AUTHOR_EMAIL = "promotion@traceharness.invalid"
INTEGRATION_TIMESTAMP = "@0 +0000"
"""Fixed commit identity.

The integration commit must be reproducible from the approved facts alone, so
its parent, tree, message, author, committer and timestamp are all protocol
constants or approved inputs. Reading the local clock here would make the same
approved Patch produce a different commit id at Review and at Promotion time,
which would defeat the whole point of approving an exact commit.
"""

_ENVIRONMENT_NAME_START = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_"
)
_ENVIRONMENT_NAME_BODY = _ENVIRONMENT_NAME_START | frozenset("0123456789")


def require_promotion_identifier(value: object, *, field: str) -> str:
    try:
        valid = is_agent_identifier(value)
        normalized = str(value) if valid else ""
    except Exception:
        valid = False
        normalized = ""
    if not valid or not is_agent_identifier(normalized):
        raise PromotionInputError("promotion-identity-invalid", field)
    return normalized


def require_hex_digest(value: object, *, lengths: tuple[int, ...], field: str) -> str:
    if not is_hex_digest(value, lengths):
        raise PromotionInputError(f"promotion-{field}-invalid", field)
    assert isinstance(value, str)
    return str(value)


def is_hex_digest(value: object, lengths: tuple[int, ...]) -> bool:
    return (
        type(value) is str
        and len(value) in lengths
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


def require_bounded_int(
    value: object, *, minimum: int, maximum: int, field: str
) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        raise PromotionInputError(f"promotion-{field}-invalid", field)
    return value


def require_target_ref(value: object) -> str:
    """Accept only an explicit host branch ref.

    D2 v1 promotes into a host-managed bare repository branch. Tags, notes,
    ``HEAD`` and bare names are rejected here rather than normalised, because a
    guessed namespace would decide which ref a compare-and-swap moves.
    """

    if (
        type(value) is not str
        or not value.startswith("refs/heads/")
        or len(value) > 512
        or value != value.strip()
    ):
        raise PromotionInputError("promotion-target-ref-invalid", "target_ref")
    name = value.removeprefix("refs/heads/")
    if not name or name.startswith("/") or name.endswith("/") or "//" in name:
        raise PromotionInputError("promotion-target-ref-invalid", "target_ref")
    for component in name.split("/"):
        if (
            not component
            or component in (".", "..")
            or component.startswith(".")
            or component.endswith(".lock")
        ):
            raise PromotionInputError("promotion-target-ref-invalid", "target_ref")
    forbidden = {"~", "^", ":", "?", "*", "[", "\\", "\0", " ", "\t"}
    if any(character in forbidden for character in value):
        raise PromotionInputError("promotion-target-ref-invalid", "target_ref")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise PromotionInputError("promotion-target-ref-invalid", "target_ref")
    return value


def _require_environment_name(value: object, field: str) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 256
        or value[0] not in _ENVIRONMENT_NAME_START
        or any(character not in _ENVIRONMENT_NAME_BODY for character in value)
    ):
        raise PromotionInputError("promotion-environment-name-invalid", field)
    if value.upper().startswith("GIT_"):
        # Git supports many, and evolving, configuration injection variables.
        # A verifier definition must never be able to reintroduce one.
        raise PromotionInputError("promotion-environment-name-invalid", field)
    return value


def freeze_environment_policy(value: object) -> VerifierEnvironmentPolicy:
    if type(value) is not VerifierEnvironmentPolicy:
        raise PromotionInputError("promotion-environment-invalid", "environment")
    policy_id = require_promotion_identifier(
        value.policy_id, field="environment_policy_id"
    )
    if type(value.passthrough) is not tuple or type(value.overrides) is not tuple:
        raise PromotionInputError("promotion-environment-invalid", "environment")
    if len(value.passthrough) + len(value.overrides) > MAX_ENVIRONMENT_ENTRIES:
        raise PromotionInputError("promotion-environment-invalid", "environment")
    seen: set[str] = set()
    for name in value.passthrough:
        name = _require_environment_name(name, "passthrough")
        if name in seen:
            raise PromotionInputError("promotion-environment-invalid", "environment")
        seen.add(name)
    for entry in value.overrides:
        if type(entry) is not tuple or len(entry) != 2:
            raise PromotionInputError("promotion-environment-invalid", "environment")
        name = _require_environment_name(entry[0], "overrides")
        override_value = entry[1]
        if (
            type(override_value) is not str
            or len(override_value) > MAX_ENVIRONMENT_VALUE_LENGTH
            or "\0" in override_value
        ):
            raise PromotionInputError(
                "promotion-environment-value-invalid", "environment"
            )
        if name in seen:
            raise PromotionInputError("promotion-environment-invalid", "environment")
        seen.add(name)
    if policy_id != value.policy_id:
        raise PromotionInputError("promotion-environment-invalid", "environment")
    return value


def freeze_verifier_command(value: object) -> VerifierCommand:
    if type(value) is not VerifierCommand:
        raise PromotionInputError("promotion-verifier-command-invalid", "commands")
    require_promotion_identifier(value.command_id, field="command_id")
    if (
        type(value.argv) is not tuple
        or not value.argv
        or len(value.argv) > MAX_VERIFIER_ARGUMENTS
    ):
        raise PromotionInputError("promotion-verifier-argv-invalid", "argv")
    for index, argument in enumerate(value.argv):
        if (
            type(argument) is not str
            or len(argument) > MAX_ARGUMENT_LENGTH
            or "\0" in argument
        ):
            raise PromotionInputError("promotion-verifier-argv-invalid", "argv")
        if index == 0 and (not argument or argument != argument.strip()):
            raise PromotionInputError("promotion-verifier-argv-invalid", "argv")
    require_bounded_int(
        value.timeout_ms, minimum=1, maximum=MAX_TIMEOUT_MS, field="timeout-ms"
    )
    return value


def freeze_verification_plan(value: object) -> VerificationPlan:
    """Validate a host verification plan exactly once, at the public boundary."""

    if type(value) is not VerificationPlan:
        raise PromotionInputError("promotion-plan-invalid", "plan")
    require_promotion_identifier(value.plan_id, field="plan_id")
    require_bounded_int(
        value.plan_version, minimum=1, maximum=1_000_000, field="plan-version"
    )
    if (
        type(value.protocol_version) is not int
        or value.protocol_version != PROMOTION_PROTOCOL_VERSION
    ):
        raise PromotionInputError("promotion-plan-protocol-invalid", "plan")
    require_bounded_int(
        value.max_output_bytes,
        minimum=1,
        maximum=MAX_OUTPUT_BYTES,
        field="max-output-bytes",
    )
    if (
        type(value.commands) is not tuple
        or not value.commands
        or len(value.commands) > MAX_VERIFIER_COMMANDS
    ):
        raise PromotionInputError("promotion-plan-commands-invalid", "commands")
    identifiers: set[str] = set()
    for command in value.commands:
        frozen = freeze_verifier_command(command)
        if frozen.command_id in identifiers:
            raise PromotionInputError(
                "promotion-plan-commands-invalid", "commands"
            )
        identifiers.add(frozen.command_id)
    freeze_environment_policy(value.environment)
    return value


def environment_policy_digest(policy: VerifierEnvironmentPolicy) -> str:
    policy = freeze_environment_policy(policy)
    return fingerprint(
        {
            "protocol": PROMOTION_PROTOCOL_VERSION,
            "purpose": "verifier-environment-policy",
            "policy_id": policy.policy_id,
            "passthrough": sorted(policy.passthrough),
            "overrides": sorted([name, value] for name, value in policy.overrides),
        }
    )


def verifier_command_digest(command: VerifierCommand) -> str:
    command = freeze_verifier_command(command)
    return fingerprint(
        {
            "protocol": PROMOTION_PROTOCOL_VERSION,
            "purpose": "verifier-command",
            "command_id": command.command_id,
            "argv": list(command.argv),
            "timeout_ms": command.timeout_ms,
        }
    )


def verifier_definition_digest(plan: VerificationPlan) -> str:
    """Bind the complete verifier definition, not just its name.

    Changing argv, timeout, output bound, environment policy, plan id, plan
    version or protocol version must produce a different digest, because each
    one changes what "verified" actually proved.
    """

    plan = freeze_verification_plan(plan)
    return fingerprint(
        {
            "protocol": PROMOTION_PROTOCOL_VERSION,
            "purpose": "verifier-definition",
            "plan_id": plan.plan_id,
            "plan_version": plan.plan_version,
            "max_output_bytes": plan.max_output_bytes,
            "environment_policy_id": plan.environment.policy_id,
            "environment_policy_digest": environment_policy_digest(plan.environment),
            "commands": [
                {
                    "command_id": command.command_id,
                    "argv": list(command.argv),
                    "timeout_ms": command.timeout_ms,
                }
                for command in plan.commands
            ],
        }
    )


def freeze_verifier_outcome(value: object) -> VerifierOutcome:
    if type(value) is not VerifierOutcome:
        raise PromotionInputError("promotion-verifier-result-invalid", "results")
    require_promotion_identifier(value.command_id, field="command_id")
    require_hex_digest(value.argv_digest, lengths=(64,), field="argv-digest")
    require_hex_digest(value.stdout_sha256, lengths=(64,), field="stdout-digest")
    require_hex_digest(value.stderr_sha256, lengths=(64,), field="stderr-digest")
    if type(value.status) is not str or value.status not in VERIFIER_STATUSES:
        raise PromotionInputError("promotion-verifier-status-invalid", "results")
    if value.exit_code is not None and (
        type(value.exit_code) is not int
        or value.exit_code < -1_000_000
        or value.exit_code > 1_000_000
    ):
        raise PromotionInputError("promotion-verifier-exit-code-invalid", "results")
    if value.status == "passed" and value.exit_code != 0:
        raise PromotionInputError("promotion-verifier-status-invalid", "results")
    require_bounded_int(
        value.stdout_bytes, minimum=0, maximum=MAX_OUTPUT_BYTES, field="stdout-bytes"
    )
    require_bounded_int(
        value.stderr_bytes, minimum=0, maximum=MAX_OUTPUT_BYTES, field="stderr-bytes"
    )
    return value


def verifier_result_data(outcome: VerifierOutcome) -> dict[str, object]:
    outcome = freeze_verifier_outcome(outcome)
    return {
        "command_id": outcome.command_id,
        "argv_digest": outcome.argv_digest,
        "status": outcome.status,
        "exit_code": outcome.exit_code,
        "stdout_sha256": outcome.stdout_sha256,
        "stdout_bytes": outcome.stdout_bytes,
        "stderr_sha256": outcome.stderr_sha256,
        "stderr_bytes": outcome.stderr_bytes,
    }


def verification_evidence_digest(
    definition_digest: str, results: tuple[VerifierOutcome, ...]
) -> str:
    definition_digest = require_hex_digest(
        definition_digest, lengths=(64,), field="verifier-definition-digest"
    )
    if type(results) is not tuple or len(results) > MAX_VERIFIER_COMMANDS:
        raise PromotionInputError("promotion-verifier-result-invalid", "results")
    return fingerprint(
        {
            "protocol": PROMOTION_PROTOCOL_VERSION,
            "purpose": "verification-evidence",
            "verifier_definition_digest": definition_digest,
            "results": [verifier_result_data(outcome) for outcome in results],
        }
    )


def review_identity(review_request_id: str) -> str:
    review_request_id = require_promotion_identifier(
        review_request_id, field="review_request_id"
    )
    return "review-" + fingerprint(
        {
            "protocol": PROMOTION_PROTOCOL_VERSION,
            "purpose": "patch-review",
            "review_request_id": review_request_id,
        }
    )


def expected_approval_digest(report: PatchReviewReport) -> str:
    """The exact content a human must approve.

    The digest deliberately enumerates every decision-bearing field instead of
    reusing ``review_digest``. If it reused the report digest, dropping the
    verifier or evidence binding would still change the digest, and a test that
    exists to prove those two facts are bound would silently keep passing.
    """

    if type(report) is not PatchReviewReport:
        raise PromotionInputError("promotion-review-invalid", "review")
    return fingerprint(
        {
            "protocol": PROMOTION_PROTOCOL_VERSION,
            "purpose": "patch-promotion-approval",
            "review_id": report.review_id,
            "review_request_id": report.review_request_id,
            "artifact_id": report.artifact_id,
            "manifest_digest": report.manifest_digest,
            "patch_sha256": report.patch_sha256,
            "patch_size_bytes": report.patch_size_bytes,
            "target_id": report.target_id,
            "repository_fingerprint": report.repository_fingerprint,
            "target_ref": report.target_ref,
            "expected_revision": report.expected_revision,
            "integration_tree": report.integration_tree,
            "integration_commit": report.integration_commit,
            "verifier_definition_digest": report.verifier_definition_digest,
            "verification_evidence_digest": report.verification_evidence_digest,
            "merge_policy_version": report.merge_policy_version,
            "passed": report.passed,
        }
    )


def promotion_operation_digest(purpose: str, **parts: object) -> str:
    """The complete definition of one in-flight promotion-plane operation.

    Two callers may only share an owned task when every input that decides the
    outcome is identical. Keying a shared task by identity alone would let a
    second, differently-defined request receive the first one's result.
    """

    purpose = require_promotion_identifier(purpose, field="purpose")
    try:
        return fingerprint(
            {
                "protocol": PROMOTION_PROTOCOL_VERSION,
                "purpose": purpose,
                "parts": parts,
            }
        )
    except Exception:
        raise PromotionInputError("promotion-operation-input-invalid", "parts") from None


def promotion_identity(approval_digest: str) -> str:
    approval_digest = require_hex_digest(
        approval_digest, lengths=(64,), field="approval-digest"
    )
    return f"promotion-{approval_digest}"


def review_report_digest(data: object, recorded_at: datetime) -> str:
    if type(recorded_at) is not datetime or recorded_at.tzinfo is None:
        raise PromotionInputError("promotion-recorded-at-invalid", "recorded_at")
    timestamp = recorded_at.astimezone(UTC).isoformat()
    try:
        return fingerprint({"recorded_at": timestamp, "review": data})
    except Exception:
        raise PromotionInputError("promotion-review-invalid", "review") from None


def integration_commit_message(*, artifact_id: str, manifest_digest: str) -> str:
    """A deterministic single-line integration commit subject."""

    artifact_id = require_promotion_identifier(artifact_id, field="artifact_id")
    manifest_digest = require_hex_digest(
        manifest_digest, lengths=(64,), field="manifest-digest"
    )
    return (
        f"traceh-promotion/{PROMOTION_PROTOCOL_VERSION} "
        f"policy={MERGE_POLICY_VERSION} "
        f"artifact={artifact_id} manifest={manifest_digest}"
    )


def require_finite_seconds(value: object, *, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise PromotionInputError(f"promotion-{field}-invalid", field)
    return float(value)


def protocol_identifier(value: object, key: str, seq: int) -> str:
    if not is_agent_identifier(value):
        raise PromotionProtocolError(f"promotion-{key.replace('_', '-')}-invalid", seq)
    assert isinstance(value, str)
    return str(value)


__all__ = [
    "INTEGRATION_AUTHOR_EMAIL",
    "INTEGRATION_AUTHOR_NAME",
    "INTEGRATION_TIMESTAMP",
    "MAX_ARGUMENT_LENGTH",
    "MAX_INTEGRATION_CHANGED_PATHS",
    "MAX_INTEGRATION_WORKTREE_BYTES",
    "MAX_OUTPUT_BYTES",
    "MAX_PATCH_BYTES",
    "MAX_VERIFIER_COMMANDS",
    "MERGE_POLICY_VERSION",
    "PROMOTION_PROTOCOL_VERSION",
    "VERIFIER_STATUSES",
    "environment_policy_digest",
    "expected_approval_digest",
    "freeze_environment_policy",
    "freeze_verification_plan",
    "freeze_verifier_command",
    "freeze_verifier_outcome",
    "integration_commit_message",
    "is_hex_digest",
    "promotion_identity",
    "promotion_operation_digest",
    "protocol_identifier",
    "require_bounded_int",
    "require_finite_seconds",
    "require_hex_digest",
    "require_promotion_identifier",
    "require_target_ref",
    "review_identity",
    "review_report_digest",
    "verification_evidence_digest",
    "verifier_command_digest",
    "verifier_definition_digest",
    "verifier_result_data",
]
