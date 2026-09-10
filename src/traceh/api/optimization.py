"""Bounded, data-only strategy services for the development control plane.

These values grant neither execution nor promotion authority. A trusted host
selects sanitized development observations; text and locators are data, not
instructions or file handles. Services are borrowed under the original Lease.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from traceh.api.json_types import fingerprint
from traceh.api.llm import Usage
from traceh.api.services import ServiceKey


@dataclass(frozen=True, slots=True)
class EditableText:
    file: str
    selector: str
    old_sha256: str
    text: str


@dataclass(frozen=True, slots=True)
class TextEdit:
    file: str
    selector: str
    old_sha256: str
    new_text: str


@dataclass(frozen=True, slots=True)
class DevelopmentObservation:
    case_id: str
    failure_class: str
    summary: str
    evidence_locations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RuntimeObservation:
    """A user's scoped feedback on a completed Turn, never an evaluation label."""

    session_id: str
    turn_id: str
    cut_seq: int
    evidence_digest: str
    failure_class: str
    summary: str
    evidence_locations: tuple[str, ...]


def observation_from_dict(raw):
    """Read the two explicit observation identities without inventing case IDs."""
    cls = DevelopmentObservation if "case_id" in raw else RuntimeObservation
    return cls(**{**raw, "evidence_locations": tuple(raw["evidence_locations"])})


@dataclass(frozen=True, slots=True)
class CandidateHistory:
    candidate_digest: str
    summary: str


@dataclass(frozen=True, slots=True)
class OptimizationRequest:
    contract_digest: str
    base_source_digest: str
    development_dataset_digest: str
    round_number: int
    analysis_max_tokens: int
    observations: tuple[DevelopmentObservation | RuntimeObservation, ...]
    editable_text: tuple[EditableText, ...]
    history: tuple[CandidateHistory, ...]

    @property
    def digest(self) -> str:
        return fingerprint(self)


@dataclass(frozen=True, slots=True)
class CandidateProposal:
    request_digest: str
    base_source_digest: str
    rationale: str
    targeted_failure_classes: tuple[str, ...]
    edits: tuple[TextEdit, ...]
    expected_tradeoffs: str


@dataclass(frozen=True, slots=True)
class NoCandidate:
    request_digest: str
    reason: str


@dataclass(frozen=True, slots=True)
class OptimizationAnalysisResult:
    """References the host's independent analysis Session and original usage.

    A DTO is not proof that these records exist. The host analysis adapter owns
    dispatch, reservations and evidence verification, and must converge before
    returning or propagating cancellation. That adapter belongs to AO-2.
    """

    request_digest: str
    session_id: str
    turn_id: str
    evidence_digest: str
    text: str
    usage: Usage


class OptimizationAnalysis(Protocol):
    async def analyze(self, request: OptimizationRequest) -> OptimizationAnalysisResult:
        """Analyze only this frozen request through host-owned model services."""
        ...


class OptimizationStrategy(Protocol):
    async def propose(self, request: OptimizationRequest) -> CandidateProposal | NoCandidate:
        """Return a proposal; do not run experiments, grade or adopt changes."""
        ...


OPTIMIZATION_ANALYSIS: ServiceKey[OptimizationAnalysis] = ServiceKey("traceh.optimization.analysis")
OPTIMIZATION_STRATEGY: ServiceKey[OptimizationStrategy] = ServiceKey("traceh.optimization.strategy")
