"""Public immutable values and host seams for Patch review and promotion.

Every value here is a *host* decision or a durable observation. Nothing in this
module accepts a model-supplied string as a repository path, a verifier command
or an approval. Only a trusted ``PromotionTargetResolver`` turns a configured
``target_id`` into a concrete bare repository, and only an explicit host call
carrying the exact approval digest can authorise a ref update.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class VerifierCommand:
    """One frozen host verifier invocation.

    ``argv`` is executed directly; there is no shell, no candidate-supplied
    command and no interpolation of Patch or Workspace content.
    """

    command_id: str
    argv: tuple[str, ...]
    timeout_ms: int


@dataclass(frozen=True, slots=True)
class VerifierEnvironmentPolicy:
    """The exact environment a host grants every verifier command.

    ``passthrough`` names are inherited from the host process; ``overrides``
    are explicit host values. Every inherited ``GIT_*`` variable is removed
    before either is applied, so a caller cannot inject Git configuration.
    """

    policy_id: str
    passthrough: tuple[str, ...]
    overrides: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class VerificationPlan:
    """A host-frozen verification definition bound into every Review Report."""

    plan_id: str
    plan_version: int
    commands: tuple[VerifierCommand, ...]
    environment: VerifierEnvironmentPolicy
    max_output_bytes: int
    protocol_version: int


@dataclass(frozen=True, slots=True)
class VerifierOutcome:
    """One bounded, structured verifier result.

    Only identities, an exit status and output digests/sizes are retained.
    Raw stdout/stderr never enters the Event Log, so a verifier cannot write
    unbounded text, terminal control sequences or secrets into durable history.
    """

    command_id: str
    argv_digest: str
    status: str
    exit_code: int | None
    stdout_sha256: str
    stdout_bytes: int
    stderr_sha256: str
    stderr_bytes: int

    @property
    def passed(self) -> bool:
        return self.status == "passed"


@dataclass(frozen=True, slots=True)
class PromotionTargetBinding:
    """Host configuration mapping one target id to one bare repository ref."""

    repository_path: Path
    target_ref: str


@dataclass(frozen=True, slots=True)
class PromotionTarget:
    """One resolved promotion target.

    ``repository_path`` is host configuration and is deliberately *not* part of
    any durable fact; the Event Log stores only ``target_id``, the repository
    fingerprint and the exact ref/revision.
    """

    target_id: str
    repository_path: Path
    repository_fingerprint: str
    target_ref: str
    expected_revision: str


class PromotionTargetResolver(Protocol):
    """Trusted host seam from a configured target id to a bare repository."""

    async def resolve(self, target_id: str) -> PromotionTarget:
        ...


@dataclass(frozen=True, slots=True)
class PatchReviewReport:
    """One immutable Review fact rebuilt from the promotion ledger."""

    review_id: str
    review_request_id: str
    artifact_id: str
    manifest_digest: str
    patch_sha256: str
    patch_size_bytes: int
    target_id: str
    repository_fingerprint: str
    target_ref: str
    expected_revision: str
    integration_tree: str
    integration_commit: str
    verifier_definition_digest: str
    verification_evidence_digest: str
    results: tuple[VerifierOutcome, ...]
    passed: bool
    merge_policy_version: int
    promotion_protocol_version: int
    reviewed_at: datetime
    review_digest: str
    recorded_seq: int


@dataclass(frozen=True, slots=True)
class PatchApproval:
    """One immutable human approval of an exact Review content digest."""

    operation_id: str
    review_id: str
    approval_digest: str
    approver_id: str
    approved_at: datetime
    recorded_seq: int


@dataclass(frozen=True, slots=True)
class PatchPromotion:
    """One immutable durable result of a successful Git ref compare-and-swap."""

    promotion_id: str
    review_id: str
    approval_digest: str
    target_id: str
    repository_fingerprint: str
    target_ref: str
    previous_revision: str
    new_revision: str
    integration_tree: str
    merge_policy_version: int
    promotion_protocol_version: int
    promoted_at: datetime
    recorded_seq: int


__all__ = [
    "PatchApproval",
    "PatchPromotion",
    "PatchReviewReport",
    "PromotionTarget",
    "PromotionTargetBinding",
    "PromotionTargetResolver",
    "VerificationPlan",
    "VerifierCommand",
    "VerifierEnvironmentPolicy",
    "VerifierOutcome",
]
