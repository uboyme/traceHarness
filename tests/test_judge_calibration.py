"""Offline checks for the bounded AO-2+ research driver; no Provider calls."""

import copy

import pytest
from live_optimization.calibrate import summarize
from live_optimization.calibration_inputs import build_inputs
from live_optimization.reopen_calibration import observations

from traceh.evaluation.inputs import read_input
from traceh.evaluation.model_review_protocol import REVIEW_SYSTEM


def sample_plan():
    return {
        "samples": [
            {"sample_id": "supported", "expected": "passed"},
            {"sample_id": "unsupported", "expected": "failed"},
        ]
    }


def row(sample, status, *, tokens=31, repeat=1):
    return {
        "sample_id": sample,
        "condition": "revised",
        "replicate": repeat,
        "judgment": {"status": status},
        "tokens": tokens,
    }


def test_calibration_requires_every_planned_judgment_and_never_authorizes_adoption():
    plan = sample_plan()
    rows = [
        row(s["sample_id"], s["expected"], repeat=repeat)
        for repeat in (1, 2)
        for s in plan["samples"]
    ]
    assert summarize(plan, rows)["frozen_gate_passed"] is True
    assert summarize(plan, rows)["adoption_authorized"] is False
    assert summarize(plan, rows[:-1])["frozen_gate_passed"] is False


def test_pending_and_unknown_usage_are_retained_in_denominator():
    result = summarize(
        sample_plan(),
        [row("supported", "passed"), row("supported", "pending_review", tokens=None, repeat=2)],
    )
    revised = result["conditions"]["revised"]
    assert revised["completed"] == 2 and revised["planned"] == 4
    assert revised["matches_expectation"] == 1 and revised["pending"] == 1
    assert revised["inconsistent_samples"] == ["supported"]
    assert revised["tokens"] is None
    assert result["frozen_gate_passed"] is False


def test_false_pass_and_false_fail_are_separate():
    result = summarize(
        sample_plan(), [row("supported", "failed"), row("unsupported", "passed")]
    )["conditions"]["revised"]
    assert result["false_pass"] == result["false_fail"] == 1
    assert result["matches_expectation"] == 0


def test_restored_policy_refuses_to_run_historical_candidate():
    # The refusal happens before opening original runs or loading credentials.
    with pytest.raises(ValueError, match="calibration-requires-frozen-candidate-source"):
        build_inputs({"system": REVIEW_SYSTEM})


@pytest.mark.parametrize("tamper", [False, True])
def test_frozen_review_conditions_must_contain_identical_evidence(tmp_path, tamper):
    import json

    baseline = {"answer": "an observation", "visible_evidence": [{"body": "source text"}]}
    revised = {**copy.deepcopy(baseline), "evidence_scope": {"format": 1}}
    if tamper:
        revised["visible_evidence"][0]["body"] = "substituted source"
    plan = {
        "samples": [
            {
                "sample_id": "one",
                "expected": "passed",
                "baseline": {"input": json.dumps(baseline)},
                "revised": {"input": json.dumps(revised)},
            }
        ]
    }
    (tmp_path / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (tmp_path / "result.json").write_text(json.dumps(summarize(plan, [])), encoding="utf-8")
    if tamper:
        with pytest.raises(AssertionError):
            observations(tmp_path)
    else:
        assert observations(tmp_path) == read_input(tmp_path, "result.json").data
