"""The paid DA driver must first pass the real public plan parser offline."""

import json
from pathlib import Path

import pytest
from live_dynamic_collaboration.optimize import compact_assessment, optimization_plan
from live_dynamic_collaboration.run import AA, MECHANISM, plan

from traceh.evaluation.errors import BenchmarkManifestError
from traceh.evaluation.manifest import load_benchmark_manifest
from traceh.evaluation.plan import load_run_options


@pytest.mark.parametrize(
    "cases,modes,first",
    [
        (AA, ["single", "single"], "candidate"),
        (MECHANISM, ["single", "multi"], "baseline"),
    ],
)
def test_real_driver_plan_enters_original_runner_offline(tmp_path, cases, modes, first):
    benchmark = Path(__file__).parents[1] / "benchmarks/dynamic_collaboration_v1/development"
    manifest = load_benchmark_manifest(benchmark)
    raw = plan(
        manifest,
        dict(
            provider="openai-compatible",
            model="qwen-plus",
            base_url="https://example.invalid/v1",
            api_key_env="UNUSED_DA_TEST_KEY",
        ),
        tmp_path / "not-read-by-plan.json",
        cases,
        modes,
        first,
    )
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(BenchmarkManifestError) as refused:
        load_run_options(path)
    assert refused.value.code == "evaluation-version-unsupported"


def test_optimizer_plan_uses_original_text_comparison_and_bounded_development(tmp_path):
    benchmark = Path(__file__).parents[1] / "benchmarks/dynamic_collaboration_v1/development"
    manifest = load_benchmark_manifest(benchmark)
    raw = optimization_plan(
        manifest,
        dict(
            provider="openai-compatible",
            model="qwen-plus",
            base_url="https://example.invalid/v1",
            api_key_env="UNUSED_DA_TEST_KEY",
        ),
        tmp_path / "not-read-by-plan.json",
    )
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(BenchmarkManifestError) as refused:
        load_run_options(path)
    assert refused.value.code == "evaluation-version-unsupported"


def test_development_summary_preserves_pending_and_marks_truncated_reason():
    pending = {"status": "pending_review"}
    assert compact_assessment(pending) == {
        "status": "pending_review",
        "origin": None,
        "reason_preview": None,
        "reason_truncated": False,
    }
    original = {"status": "failed", "origin": "model", "reason": "证据" * 200}
    result = compact_assessment(original)
    assert result["status"] == "failed" and result["origin"] == "model"
    assert len(result["reason_preview"]) == 160 and result["reason_truncated"]
    assert len(original["reason"]) == 400
