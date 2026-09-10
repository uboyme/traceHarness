"""AO-0 admission uses real source and the existing UE-3 AST owner, offline."""

import ast
import json
from dataclasses import FrozenInstanceError, replace

import pytest
from optimization_fixtures import example

from traceh.api.json_types import fingerprint
from traceh.api.optimization import CandidateHistory, NoCandidate, TextEdit
from traceh.evaluation.contracts import CheckStatus, ConvergenceStatus
from traceh.evaluation.errors import BenchmarkManifestError
from traceh.evaluation.variants import apply_candidate, source_digest, text_node
from traceh.evolution.optimization_contract import (
    OptimizationContractError,
    admit_proposal,
    decide_next,
    editable_text,
    validate_request,
)


@pytest.fixture
def sample():
    return example()


def admit(sample, proposal=None, *, seen=()):
    files, contract, request, original, *_ = sample
    return admit_proposal(
        contract,
        request,
        original if proposal is None else proposal,
        files,
        seen_candidate_digests=seen,
    )


def decision(sample, **changes):
    _, contract, _, _, progress, now = sample
    return decide_next(
        contract,
        replace(progress, **changes),
        now_utc=now,
        next_trials=4,
        next_analysis_tokens=2000,
    )


def test_candidate_is_an_original_ue3_patch_and_only_selected_strings_change(sample):
    files, contract, request, proposal, *_ = sample
    validate_request(contract, request, files)
    accepted = admit(sample)
    patch = json.loads(accepted.patch_json)
    changed = apply_candidate(files, patch)
    assert accepted.candidate_digest == fingerprint(patch)
    assert accepted.source_digest == source_digest(changed) != source_digest(files)
    expected = {(e.file, e.selector): e.new_text for e in proposal.edits}
    for name, original in files:
        tree = ast.parse(original)
        for (path, selector), value in expected.items():
            if name == path:
                text_node(tree, selector).value = value
        assert ast.dump(ast.parse(dict(changed)[name])) == ast.dump(tree), name
    assert set(patch) == {"format", "base_source_digest", "edits"}
    with pytest.raises(FrozenInstanceError):
        request.round_number = 2


def test_same_edits_reordered_or_explained_differently_have_one_identity(sample):
    proposal = sample[3]
    first = admit(sample)
    renamed = replace(proposal, rationale="A different explanation", edits=proposal.edits[::-1])
    assert admit(sample, renamed) == first
    with pytest.raises(OptimizationContractError, match="duplicate-candidate"):
        admit(sample, renamed, seen=(first.candidate_digest,))


def test_history_also_prevents_same_candidate_with_new_request(sample):
    files, contract, request, proposal, *_ = sample
    first = admit(sample)
    request = replace(
        request, history=(CandidateHistory(first.candidate_digest, "Already measured, no gain."),)
    )
    proposal = replace(proposal, request_digest=request.digest)
    with pytest.raises(OptimizationContractError, match="duplicate-candidate"):
        admit_proposal(contract, request, proposal, files, seen_candidate_digests=())


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("request_digest", "0" * 64, "proposal-binding-mismatch"),
        ("base_source_digest", "0" * 64, "base-drift"),
        ("edits", (), "proposal-invalid"),
        ("rationale", "", "proposal-invalid"),
        ("targeted_failure_classes", ["a list"], "proposal-invalid"),
    ],
)
def test_invalid_or_stale_proposal(sample, field, value, code):
    with pytest.raises(OptimizationContractError, match=code):
        admit(sample, replace(sample[3], **{field: value}))


@pytest.mark.parametrize(
    "file,selector",
    [
        ("runtime/agent_loop.py", "anything"),
        ("evaluation/evaluators/episode_assessment.py", "anything"),
        ("tools/reference_search.py", "MemorySearchTool.description"),
        ("tools/reference_search.py", "SkillSearchTool.input_schema"),
        ("../runtime/prompt.py", "_REFERENCE_GUIDANCE"),
    ],
)
def test_proposal_cannot_expand_contract_or_modify_logic(sample, file, selector):
    # Memory description is globally editable in UE-3, but excluded by this contract.
    edit = replace(sample[3].edits[0], file=file, selector=selector)
    with pytest.raises(OptimizationContractError, match="edit-scope-invalid"):
        admit(sample, replace(sample[3], edits=(edit,)))


def test_old_text_noop_duplicate_nodes_and_ue3_size_limit_are_rejected(sample):
    edit = sample[3].edits[0]
    for changed, error, match in (
        ((replace(edit, old_sha256="0" * 64),), OptimizationContractError, "text-drift"),
        (
            (replace(edit, new_text=sample[2].editable_text[0].text),),
            OptimizationContractError,
            "no-change",
        ),
        ((edit, edit), BenchmarkManifestError, "scope-invalid"),
        ((replace(edit, new_text="x" * 65537),), BenchmarkManifestError, "scope-invalid"),
    ):
        with pytest.raises(error) as caught:
            admit(sample, replace(sample[3], edits=changed))
        assert match in caught.value.code


def test_source_drift_in_unedited_file_is_still_a_stale_baseline(sample):
    files, contract, request, proposal, *_ = sample
    changed = list(files)
    name, raw = changed[0]
    changed[0] = (name, raw + b"\n# Explicit drift fixture\n")
    with pytest.raises(OptimizationContractError, match="base-drift"):
        admit_proposal(contract, request, proposal, tuple(changed), seen_candidate_digests=())


def test_global_ue3_permission_cannot_replace_the_narrow_experiment_permission(sample):
    target = editable_text(
        sample[0], (("tools/reference_search.py", "MemorySearchTool.description"),)
    )[0]
    edit = TextEdit(
        target.file,
        target.selector,
        target.old_sha256,
        target.text + "\nEXPLICIT TEST: outside this experiment's scope.",
    )
    proposal = replace(sample[3], edits=(edit,))
    # Valid for the original UE-3 owner, but invalid for this narrower experiment.
    patch = {
        "format": 1,
        "base_source_digest": sample[1].base_source_digest,
        "edits": [
            {
                "file": edit.file,
                "selector": edit.selector,
                "old_sha256": edit.old_sha256,
                "new_text": edit.new_text,
            }
        ],
    }
    assert source_digest(apply_candidate(sample[0], patch)) != sample[1].base_source_digest
    with pytest.raises(OptimizationContractError, match="edit-scope-invalid"):
        admit(sample, proposal)


def test_no_candidate_is_explicit_and_bound(sample):
    request = sample[2]
    answer = NoCandidate(request.digest, "No defensible change from these observations.")
    assert admit(sample, answer) == answer
    with pytest.raises(OptimizationContractError, match="binding-mismatch"):
        admit(sample, replace(answer, request_digest="0" * 64))


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("contract_digest", "0" * 64, "binding-mismatch"),
        ("development_dataset_digest", "0" * 64, "binding-mismatch"),
        ("round_number", 0, "budget-invalid"),
        ("analysis_max_tokens", True, "budget-invalid"),
        ("analysis_max_tokens", 999999, "budget-invalid"),
        ("editable_text", (), "text-drift"),
        ("observations", [], "request-invalid"),
    ],
)
def test_request_is_bound_and_immutable_in_shape(sample, field, value, code):
    files, contract, request, *_ = sample
    with pytest.raises(OptimizationContractError, match=code):
        validate_request(contract, replace(request, **{field: value}), files)


def test_undeclared_case_and_oversized_request_never_reach_strategy(sample):
    files, contract, request, *_ = sample
    with pytest.raises(OptimizationContractError, match="development-scope-invalid"):
        validate_request(
            contract,
            replace(
                request,
                observations=(
                    replace(request.observations[0], case_id="not-an-approved-development-case"),
                ),
            ),
            files,
        )
    small = replace(contract, limits=replace(contract.limits, max_request_bytes=1))
    with pytest.raises(OptimizationContractError, match="request-too-large"):
        validate_request(small, replace(request, contract_digest=small.digest), files)


def test_contract_rejects_unknown_version_unbounded_limits_and_illegal_text_scope(sample):
    files, contract, *_ = sample
    for values, code in (
        ({"format": 2}, "version-unsupported"),
        ({"development_case_ids": ("x", "x")}, "development-scope-invalid"),
        ({"deadline_utc": contract.deadline_utc.replace(tzinfo=None)}, "deadline-invalid"),
    ):
        with pytest.raises(OptimizationContractError, match=code):
            replace(contract, **values)
    for limit in (0, -1, True, None):
        with pytest.raises(OptimizationContractError, match="limits-invalid"):
            replace(contract.limits, max_trials=limit)
    with pytest.raises(OptimizationContractError, match="edit-scope-invalid"):
        editable_text(files, (("runtime/agent_loop.py", "anything"),))


def test_pending_review_never_allows_new_experiments_even_at_last_round(sample):
    assert decision(sample).action == "continue"
    assert decision(sample, pending_reviews=1).action == "await_review"
    assert decision(sample, pending_reviews=1, rounds_started=3).action == "await_review"
    assert decision(sample, pending_reviews=1, cancelled=True).reason == "cancelled"


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"cancelled": True}, "cancelled"),
        ({"no_candidate": True}, "no-candidate"),
        ({"analysis_tokens": None}, "analysis-usage-unknown"),
        ({"convergence": ConvergenceStatus.UNKNOWN}, "convergence-unproven"),
        ({"convergence": ConvergenceStatus.FAILED}, "convergence-unproven"),
        ({"evidence": CheckStatus.UNPROVEN}, "evidence-not-passed"),
        ({"evidence": CheckStatus.VIOLATED}, "evidence-not-passed"),
        ({"rounds_started": 3}, "max-rounds"),
        ({"candidates_admitted": 3}, "max-candidates"),
        ({"trials_started": 20}, "max-trials"),
        ({"consecutive_no_gain": 2}, "max-consecutive-no-gain"),
        ({"invalid_proposals": 2}, "max-invalid-proposals"),
        ({"duplicate_proposals": 2}, "max-duplicate-proposals"),
        ({"analysis_calls": 4}, "max-analysis-calls"),
        ({"analysis_tokens": 20000}, "max-analysis-tokens"),
        ({"trials_started": 17}, "trial-batch-exceeds-limit"),
        ({"analysis_tokens": 18001}, "analysis-reservation-exceeds-limit"),
    ],
)
def test_limits_and_failed_evidence_stop(sample, changes, reason):
    result = decision(sample, **changes)
    assert (result.action, result.reason) == ("stop", reason)


def test_complete_batch_boundary_deadline_and_wrong_progress_binding(sample):
    _, contract, _, _, progress, _ = sample
    assert decision(sample, trials_started=16, analysis_tokens=18000).action == "continue"
    assert (
        decide_next(
            contract, progress, now_utc=contract.deadline_utc, next_trials=0, next_analysis_tokens=0
        ).reason
        == "deadline"
    )
    with pytest.raises(OptimizationContractError, match="binding-mismatch"):
        decision(sample, contract_digest="0" * 64)
    with pytest.raises(OptimizationContractError, match="progress-invalid"):
        decision(sample, analysis_tokens=-1)


def test_same_sdk_contract_and_dependency_direction():
    import traceh.api as api
    import traceh.api.optimization as public
    import traceh.plugins as sdk

    for name in (
        "OPTIMIZATION_ANALYSIS",
        "OPTIMIZATION_STRATEGY",
        "CandidateHistory",
        "CandidateProposal",
        "DevelopmentObservation",
        "EditableText",
        "NoCandidate",
        "OptimizationAnalysis",
        "OptimizationAnalysisResult",
        "OptimizationRequest",
        "OptimizationStrategy",
        "TextEdit",
    ):
        assert getattr(api, name) is getattr(public, name) is getattr(sdk, name)
    assert str(api.OPTIMIZATION_STRATEGY) == "traceh.optimization.strategy@1"
    from pathlib import Path

    tree = ast.parse(Path(public.__file__).read_text(encoding="utf-8"))
    assert all(
        node.module.startswith("traceh.api.")
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("traceh.")
    )
    # The AO contract delegates all AST surgery to the existing owner.
    import traceh.evolution.optimization_contract as host

    assert host.apply_candidate is apply_candidate
    assert TextEdit is api.TextEdit
