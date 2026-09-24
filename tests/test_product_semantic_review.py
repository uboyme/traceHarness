"""Product semantics reuse the original review/assess and model-call evidence owners."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from evaluation_fixtures import write_dataset
from sandbox_fixtures import real_sandbox_policy
from test_control_model_calls import config, responder
from test_product_benchmark_e2e import PRODUCT_MODEL_ID, _ProductProvider, build_benchmark

from traceh.evaluation.errors import BenchmarkManifestError
from traceh.evaluation.inputs import digest_bytes
from traceh.evaluation.model_review import review_with_model
from traceh.evaluation.review import export_review, load_assessment, reviewed_report
from traceh.evaluation.runner import EvaluationRunner


def material(root):
    build_benchmark(root, arms=(("single", 1),), tasks=("first", "second"))
    manifest = json.loads((root / "benchmark.json").read_text())
    cases = json.loads((root / "dataset.json").read_text())["cases"]
    cases[1]["verification"]["commands"][0]["argv"] = ["python", "-c", "raise SystemExit(1)"]
    rubric = {
        "format": 1,
        "criteria": {
            case["case_id"]: [
                "Create added.txt containing added followed by a newline.",
                "Do not claim checks beyond observed tool and verifier evidence.",
            ]
            for case in cases
        },
    }
    path = root / "rubric.json"
    path.write_text(json.dumps(rubric), encoding="utf-8")
    manifest["assessment"] = {
        "scorer_id": "product-durable-semantic-v1",
        "version": 1,
        "rubric": {"file": path.name, "sha256": digest_bytes(path.read_bytes())},
        "requires_review": True,
    }
    write_dataset(root, manifest, cases, format_version=3)
    return root


async def test_product_model_judgment_is_bound_to_patch_and_cannot_override_hard_failure(tmp_path):
    root = material(tmp_path / "b")
    output = tmp_path / "run"
    report = await EvaluationRunner(
        root,
        output,
        provider=_ProductProvider(),
        model_id=PRODUCT_MODEL_ID,
        sandbox=real_sandbox_policy(),
    ).run()
    assert [t.assessment.value for t in report.trials] == ["pending_review", "failed"]
    original = (output / "report.json").read_bytes()
    exported = tmp_path / "human"
    export_review(output, exported)
    packet = json.loads((exported / "review.json").read_text())
    assert packet["task_type"] == "product_task" and len(packet["tasks"]) == 2
    assert "added" in packet["tasks"][0]["artifact"]["patch"]
    judge = responder()
    result = await review_with_model(
        output,
        tmp_path / "judge",
        provider=judge,
        config=config(),
        max_calls=1,
        max_tokens=16000,
        deadline_utc=datetime.now(UTC) + timedelta(minutes=3),
    )
    assessed, _ = load_assessment(Path(result["assessment"]))
    assert [t["assessment"]["status"] for t in assessed["trials"]] == ["passed", "failed"]
    assert len(judge.requests) == 1 and result["cost"]["tokens"] == 30
    assert (output / "report.json").read_bytes() == original
    judge_text = "\n".join(m.content for m in judge.requests[0].messages)
    assert '"patch"' in judge_text and '"visible_tool_results"' in judge_text
    assert "apply_patch" in judge_text  # actual dispatched Tool result, not just a frozen request
    # A manual judgment cannot manufacture successful verification for the failed task.
    path = exported / "judgment-template.json"
    judgment = json.loads(path.read_text())
    judgment.update(
        reviewer="explicit-test-reviewer",
        judgments=[
            {
                "trial_id": report.trials[1].spec.trial_id,
                "status": "passed",
                "reason": "unsupported override",
            }
        ],
    )
    path.write_text(json.dumps(judgment))
    with pytest.raises(BenchmarkManifestError):
        reviewed_report(output, path)


def test_semantic_criteria_are_exact_case_bound_and_declared(tmp_path):
    root = material(tmp_path / "b")
    path = root / "rubric.json"
    rubric = json.loads(path.read_text())
    rubric["criteria"].pop("second")
    path.write_text(json.dumps(rubric))
    manifest_path = root / "benchmark.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["assessment"]["rubric"]["sha256"] = digest_bytes(path.read_bytes())
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(BenchmarkManifestError) as refused:
        EvaluationRunner(
            root, tmp_path / "out", provider=_ProductProvider(), model_id=PRODUCT_MODEL_ID
        )
    assert refused.value.field == "product-rubric-cases"
    assert not (tmp_path / "out").exists()
