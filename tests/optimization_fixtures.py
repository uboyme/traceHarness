"""Explicit AO contract examples; none are production defaults or real grades."""

from datetime import UTC, datetime, timedelta

from traceh.api.json_types import fingerprint
from traceh.api.optimization import (
    CandidateProposal,
    DevelopmentObservation,
    OptimizationRequest,
    TextEdit,
)
from traceh.evaluation.contracts import CheckStatus, ConvergenceStatus
from traceh.evaluation.variants import source_digest, source_files
from traceh.evolution.optimization_contract import (
    OptimizationContract,
    OptimizationLimits,
    OptimizationProgress,
    editable_text,
)


def example():
    _, files = source_files()
    now = datetime(2026, 1, 2, tzinfo=UTC)
    contract = OptimizationContract(
        experiment_id="explicit-contract-test",
        base_source_digest=source_digest(files),
        benchmark_digest=fingerprint("explicit benchmark fixture"),
        run_plan_digest=fingerprint("explicit plan fixture"),
        development_dataset_digest=fingerprint("explicit development fixture"),
        development_case_ids=("navigation-case", "source-case"),
        editable_text=editable_text(
            files,
            (
                ("tools/reference_search.py", "SkillSearchTool.description"),
                ("runtime/prompt.py", "_REFERENCE_GUIDANCE"),
            ),
        ),
        limits=OptimizationLimits(3, 3, 20, 2, 2, 2, 4, 20000, 200000),
        deadline_utc=now + timedelta(hours=1),
    )
    request = OptimizationRequest(
        contract.digest,
        contract.base_source_digest,
        contract.development_dataset_digest,
        1,
        2000,
        (
            DevelopmentObservation(
                "navigation-case",
                "candidate-without-evidence",
                "Fixture: a catalog was observed but the body was not dispatched.",
                ("explicit-review/trial/request-location",),
            ),
        ),
        contract.editable_text,
        (),
    )
    proposal = CandidateProposal(
        request.digest,
        contract.base_source_digest,
        "Fixture candidate explains evidence scope.",
        ("candidate-without-evidence",),
        tuple(
            TextEdit(
                t.file,
                t.selector,
                t.old_sha256,
                t.text + "\nEXPLICIT TEST CANDIDATE: preserve the evidence scope.",
            )
            for t in request.editable_text
        ),
        "Fixture: possibly more reading and tokens; no gain claimed.",
    )
    progress = OptimizationProgress(
        contract.digest,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        False,
        False,
        ConvergenceStatus.CONVERGED,
        CheckStatus.PASSED,
    )
    return files, contract, request, proposal, progress, now
