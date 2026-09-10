"""Shared measurement contracts, independent of Product and Runtime state."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from traceh.api.json_types import JsonValue


class TaskType(StrEnum):
    PRODUCT_TASK = "product_task"
    RETRIEVAL_EPISODE = "retrieval_episode"


class ExecutionStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NOT_STARTED = "not_started"


class AssessmentStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    PENDING_REVIEW = "pending_review"
    UNASSESSABLE = "unassessable"


class CheckStatus(StrEnum):
    PASSED = "passed"
    VIOLATED = "violated"
    UNPROVEN = "unproven"


class ConvergenceStatus(StrEnum):
    CONVERGED = "converged"
    UNKNOWN = "unknown"
    FAILED = "failed"


class TaskResult(Protocol):
    def to_dict(self) -> dict[str, JsonValue]: ...


class TaskReport(TaskResult, Protocol):
    @property
    def complete(self) -> bool: ...

    def markdown(self) -> str: ...


@dataclass(frozen=True, slots=True)
class TrialSpec:
    trial_id: str
    case_id: str
    group_id: str
    material_digest: str
    material_seed: int | None
    replicate: int
    requested_mode: str | None
    variant_id: str


@dataclass(frozen=True, slots=True)
class TrialContext:
    run_id: str
    spec: TrialSpec
    directory: Path
    relative_directory: str


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    file: str
    stream_id: str
    first_seq: int
    last_seq: int
    sha256: str


@dataclass(frozen=True, slots=True)
class TrialResult:
    spec: TrialSpec
    execution: ExecutionStatus
    reason: str | None
    assessment: AssessmentStatus
    invariants: CheckStatus
    convergence: ConvergenceStatus
    measured: bool
    task_result: TaskResult | None
    evidence: tuple[EvidenceRef, ...] = ()
    usage: tuple[tuple[str, JsonValue], ...] = ()
    errors: tuple[str, ...] = ()


class Evaluator(Protocol):
    """Owned execute returns only after resources converge; scoring is read-only."""

    def trials(self, variant_id: str, repetitions: int) -> tuple[TrialSpec, ...]: ...
    def verify_inputs(self) -> None: ...
    def frozen_settings(self) -> dict[str, JsonValue]: ...
    def frozen_materials(self) -> tuple[tuple[str, bytes], ...]: ...
    async def execute(self, context: TrialContext) -> TrialResult: ...
    def summarize(self, results: tuple[TrialResult, ...]) -> TaskReport: ...
