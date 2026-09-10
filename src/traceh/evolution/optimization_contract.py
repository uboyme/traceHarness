"""AO-0 host contracts; no optimizer loop, mutable journal or new evaluator.

The scheduler derives progress from verified evaluation artifacts
and reserve a complete batch before starting it. Pure admission is not itself
a reservation, proof of an evaluation result, or permission to adopt a patch.
"""

from __future__ import annotations

import ast
import re
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime

from traceh.api.json_types import canonical_json, fingerprint
from traceh.api.optimization import (
    CandidateHistory,
    CandidateProposal,
    DevelopmentObservation,
    EditableText,
    NoCandidate,
    OptimizationRequest,
    RuntimeObservation,
    TextEdit,
)
from traceh.evaluation.contracts import CheckStatus, ConvergenceStatus
from traceh.evaluation.inputs import digest_bytes
from traceh.evaluation.variants import EDITABLE_TEXT, apply_candidate, source_digest, text_node


class OptimizationContractError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise OptimizationContractError(code)


def _text(value: object) -> bool:
    return type(value) is str and bool(value) and value == value.strip()


def _digest(value: object) -> bool:
    return type(value) is str and re.fullmatch("[0-9a-f]{64}", value) is not None


def _count(value: object, *, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _tuple(value: object, cls: type, *, nonempty: bool = False) -> bool:
    return (
        type(value) is tuple
        and (bool(value) or not nonempty)
        and all(type(item) is cls for item in value)
    )


@dataclass(frozen=True, slots=True)
class OptimizationLimits:
    max_rounds: int
    max_candidates: int
    max_trials: int
    max_consecutive_no_gain: int
    max_invalid_proposals: int
    max_duplicate_proposals: int
    max_analysis_calls: int
    max_analysis_tokens: int
    max_request_bytes: int

    def __post_init__(self) -> None:
        _require(
            all(_count(getattr(self, f.name), minimum=1) for f in fields(self)),
            "optimization-limits-invalid",
        )


@dataclass(frozen=True, slots=True)
class OptimizationContract:
    experiment_id: str
    base_source_digest: str
    benchmark_digest: str
    run_plan_digest: str
    development_dataset_digest: str
    development_case_ids: tuple[str, ...]
    editable_text: tuple[EditableText, ...]
    limits: OptimizationLimits
    deadline_utc: datetime
    format: int = 1

    def __post_init__(self) -> None:
        _require(type(self.format) is int and self.format == 1, "optimization-version-unsupported")
        _require(
            _text(self.experiment_id)
            and all(
                _digest(value)
                for value in (
                    self.base_source_digest,
                    self.benchmark_digest,
                    self.run_plan_digest,
                    self.development_dataset_digest,
                )
            ),
            "optimization-identity-invalid",
        )
        ids = self.development_case_ids
        _require(
            _tuple(ids, str, nonempty=True) and all(map(_text, ids)) and len(set(ids)) == len(ids),
            "optimization-development-scope-invalid",
        )
        _require(type(self.limits) is OptimizationLimits, "optimization-limits-invalid")
        _require(
            type(self.deadline_utc) is datetime and self.deadline_utc.tzinfo is UTC,
            "optimization-deadline-invalid",
        )
        _require(
            _tuple(self.editable_text, EditableText, nonempty=True),
            "optimization-edit-scope-invalid",
        )
        seen = set()
        for item in self.editable_text:
            _require(
                _text(item.file) and _text(item.selector) and type(item.text) is str,
                "optimization-edit-scope-invalid",
            )
            key = (item.file, item.selector)
            _require(
                item.selector in EDITABLE_TEXT.get(item.file, ()) and key not in seen,
                "optimization-edit-scope-invalid",
            )
            _require(
                item.old_sha256 == digest_bytes(item.text.encode("utf-8")),
                "optimization-text-drift",
            )
            seen.add(key)

    @property
    def digest(self) -> str:
        return fingerprint(self.to_dict())

    def to_dict(self):
        value = asdict(self)
        value["deadline_utc"] = self.deadline_utc.isoformat()
        return value

    @classmethod
    def from_dict(cls, value):
        from traceh.evaluation.inputs import object_fields

        raw = object_fields(value, {f.name for f in fields(cls)}, "optimization-contract")
        limit_fields = {f.name for f in fields(OptimizationLimits)}
        limits = OptimizationLimits(**object_fields(raw["limits"], limit_fields, "limits"))
        _require(
            type(raw["development_case_ids"]) is list and type(raw["editable_text"]) is list,
            "optimization-contract-invalid",
        )
        nodes = tuple(
            EditableText(
                **object_fields(v, {f.name for f in fields(EditableText)}, "editable-text")
            )
            for v in raw["editable_text"]
        )
        try:
            deadline = datetime.fromisoformat(raw["deadline_utc"])
        except (ValueError, TypeError):
            raise OptimizationContractError("optimization-deadline-invalid") from None
        return cls(
            **{
                **raw,
                "limits": limits,
                "editable_text": nodes,
                "development_case_ids": tuple(raw["development_case_ids"]),
                "deadline_utc": deadline,
            }
        )


def editable_text(files, selectors: tuple[tuple[str, str], ...]) -> tuple[EditableText, ...]:
    """Read host-selected nodes from the same source inventory as UE-3."""
    _require(type(selectors) is tuple and bool(selectors), "optimization-edit-scope-invalid")
    result, seen = [], set()
    for name, selector in selectors:
        _require(
            selector in EDITABLE_TEXT.get(name, ()) and (name, selector) not in seen,
            "optimization-edit-scope-invalid",
        )
        seen.add((name, selector))
        text = text_node(ast.parse(dict(files)[name]), selector).value
        result.append(EditableText(name, selector, digest_bytes(text.encode("utf-8")), text))
    return tuple(sorted(result, key=lambda item: (item.file, item.selector)))


def validate_request(contract: OptimizationContract, request: OptimizationRequest, files) -> None:
    """Validate before handing development data to a trusted strategy service."""
    _require(type(request) is OptimizationRequest, "optimization-request-invalid")
    _require(source_digest(files) == contract.base_source_digest, "optimization-base-drift")
    _require(
        request.contract_digest == contract.digest
        and request.base_source_digest == contract.base_source_digest
        and request.development_dataset_digest == contract.development_dataset_digest,
        "optimization-request-binding-mismatch",
    )
    actual = editable_text(files, tuple((t.file, t.selector) for t in contract.editable_text))
    _require(actual == contract.editable_text == request.editable_text, "optimization-text-drift")
    _require(
        _count(request.round_number, minimum=1)
        and request.round_number <= contract.limits.max_rounds
        and _count(request.analysis_max_tokens, minimum=1)
        and request.analysis_max_tokens <= contract.limits.max_analysis_tokens,
        "optimization-request-budget-invalid",
    )
    _require(
        type(request.observations) is tuple
        and bool(request.observations)
        and all(
            type(o) in (DevelopmentObservation, RuntimeObservation) for o in request.observations
        )
        and _tuple(request.history, CandidateHistory),
        "optimization-request-invalid",
    )
    for item in request.observations:
        if type(item) is DevelopmentObservation:
            _require(
                _text(item.case_id) and item.case_id in contract.development_case_ids,
                "optimization-development-scope-invalid",
            )
        else:
            _require(
                _text(item.session_id) and _text(item.turn_id)
                and _count(item.cut_seq, minimum=1) and _digest(item.evidence_digest),
                "optimization-runtime-observation-invalid",
            )
        _require(
            _text(item.failure_class)
            and _text(item.summary)
            and _tuple(item.evidence_locations, str, nonempty=True)
            and all(map(_text, item.evidence_locations)),
            "optimization-observation-invalid",
        )
    for item in request.history:
        _require(
            _digest(item.candidate_digest) and _text(item.summary), "optimization-history-invalid"
        )
    _require(
        len(canonical_json(request).encode("utf-8")) <= contract.limits.max_request_bytes,
        "optimization-request-too-large",
    )


@dataclass(frozen=True, slots=True)
class AdmittedCandidate:
    """Canonical UE-3 patch, not a comparison, reservation or adoption receipt."""

    candidate_digest: str
    source_digest: str
    patch_json: str


def admit_proposal(
    contract: OptimizationContract,
    request: OptimizationRequest,
    proposal: CandidateProposal | NoCandidate,
    files,
    *,
    seen_candidate_digests: tuple[str, ...],
) -> AdmittedCandidate | NoCandidate:
    validate_request(contract, request, files)
    _require(type(proposal) in (CandidateProposal, NoCandidate), "optimization-proposal-invalid")
    _require(proposal.request_digest == request.digest, "optimization-proposal-binding-mismatch")
    if type(proposal) is NoCandidate:
        _require(_text(proposal.reason), "optimization-proposal-invalid")
        return proposal
    _require(proposal.base_source_digest == contract.base_source_digest, "optimization-base-drift")
    _require(
        _text(proposal.rationale)
        and _text(proposal.expected_tradeoffs)
        and _tuple(proposal.targeted_failure_classes, str, nonempty=True)
        and all(map(_text, proposal.targeted_failure_classes))
        and _tuple(proposal.edits, TextEdit, nonempty=True),
        "optimization-proposal-invalid",
    )
    allowed = {(t.file, t.selector): t for t in contract.editable_text}
    for edit in proposal.edits:
        _require(_text(edit.file) and _text(edit.selector), "optimization-edit-scope-invalid")
        original = allowed.get((edit.file, edit.selector))
        _require(original is not None, "optimization-edit-scope-invalid")
        _require(edit.old_sha256 == original.old_sha256, "optimization-text-drift")
        _require(
            type(edit.new_text) is str and edit.new_text != original.text, "optimization-no-change"
        )
    patch = {
        "format": 1,
        "base_source_digest": contract.base_source_digest,
        "edits": [
            asdict(edit)
            for edit in sorted(proposal.edits, key=lambda edit: (edit.file, edit.selector))
        ],
    }
    changed = apply_candidate(files, patch)
    identity = fingerprint(patch)
    _require(
        _tuple(seen_candidate_digests, str) and all(map(_digest, seen_candidate_digests)),
        "optimization-history-invalid",
    )
    _require(
        identity not in seen_candidate_digests
        and identity not in {h.candidate_digest for h in request.history},
        "optimization-duplicate-candidate",
    )
    return AdmittedCandidate(identity, source_digest(changed), canonical_json(patch))


@dataclass(frozen=True, slots=True)
class OptimizationProgress:
    """A derived observation, never a new persistent authority for trials."""

    contract_digest: str
    rounds_started: int
    candidates_admitted: int
    trials_started: int
    consecutive_no_gain: int
    invalid_proposals: int
    duplicate_proposals: int
    analysis_calls: int
    analysis_tokens: int | None
    pending_reviews: int
    cancelled: bool
    no_candidate: bool
    convergence: ConvergenceStatus
    evidence: CheckStatus

    def __post_init__(self) -> None:
        _require(_digest(self.contract_digest), "optimization-progress-invalid")
        for f in fields(self):
            if f.name in (
                "contract_digest",
                "cancelled",
                "no_candidate",
                "convergence",
                "evidence",
            ):
                continue
            v = getattr(self, f.name)
            _require(
                (f.name == "analysis_tokens" and v is None) or _count(v),
                "optimization-progress-invalid",
            )
        _require(
            type(self.cancelled) is bool
            and type(self.no_candidate) is bool
            and type(self.convergence) is ConvergenceStatus
            and type(self.evidence) is CheckStatus,
            "optimization-progress-invalid",
        )


@dataclass(frozen=True, slots=True)
class OptimizationDecision:
    action: str
    reason: str


def decide_next(
    contract: OptimizationContract,
    progress: OptimizationProgress,
    *,
    now_utc: datetime,
    next_trials: int,
    next_analysis_tokens: int,
) -> OptimizationDecision:
    """Preflight a new optimization round, not each call within an admitted round.

    The single scheduler reserves the whole batch before advancing counters.
    Cancellation and original per-trial budgets still apply during execution.
    """
    _require(progress.contract_digest == contract.digest, "optimization-progress-binding-mismatch")
    _require(type(now_utc) is datetime and now_utc.tzinfo is UTC, "optimization-clock-invalid")
    _require(_count(next_trials) and _count(next_analysis_tokens), "optimization-batch-invalid")
    hard_stops = (
        (progress.cancelled, "cancelled"),
        (now_utc >= contract.deadline_utc, "deadline"),
        (progress.convergence is not ConvergenceStatus.CONVERGED, "convergence-unproven"),
        (progress.evidence is not CheckStatus.PASSED, "evidence-not-passed"),
        (progress.analysis_tokens is None, "analysis-usage-unknown"),
        (progress.no_candidate, "no-candidate"),
    )
    for stop, reason in hard_stops:
        if stop:
            return OptimizationDecision("stop", reason)
    if progress.pending_reviews:
        return OptimizationDecision("await_review", "pending-review")
    limits = contract.limits
    for field, limit in (
        ("rounds_started", "max_rounds"),
        ("candidates_admitted", "max_candidates"),
        ("trials_started", "max_trials"),
        ("consecutive_no_gain", "max_consecutive_no_gain"),
        ("invalid_proposals", "max_invalid_proposals"),
        ("duplicate_proposals", "max_duplicate_proposals"),
        ("analysis_calls", "max_analysis_calls"),
        ("analysis_tokens", "max_analysis_tokens"),
    ):
        if getattr(progress, field) >= getattr(limits, limit):
            return OptimizationDecision("stop", limit.replace("_", "-"))
    if progress.trials_started + next_trials > limits.max_trials:
        return OptimizationDecision("stop", "trial-batch-exceeds-limit")
    if progress.analysis_tokens + next_analysis_tokens > limits.max_analysis_tokens:
        return OptimizationDecision("stop", "analysis-reservation-exceeds-limit")
    return OptimizationDecision("continue", "within-contract")
