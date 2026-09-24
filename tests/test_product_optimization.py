"""Product text candidates use original AO workers, verifier and semantic review."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from evaluation_fixtures import write_dataset
from sandbox_fixtures import real_sandbox_policy
from test_control_model_calls import config
from test_evaluation_comparison import pair_plan
from test_product_benchmark_e2e import build_benchmark

from traceh.api.json_types import to_json_value
from traceh.api.llm import ModelResponse, Usage, UsageQuality
from traceh.api.optimization import DevelopmentObservation
from traceh.evaluation.inputs import digest_bytes
from traceh.evaluation.plan import load_run_options
from traceh.evaluation.runner import EvaluationRunner
from traceh.evaluation.variants import source_digest, source_files
from traceh.evolution.optimization_contract import (
    OptimizationContract,
    OptimizationLimits,
    editable_text,
)
from traceh.evolution.strategy import inspect_strategy_optimization, run_strategy_optimization
from traceh.llm.scripted import ScriptedLlmProvider


@pytest.fixture
def product_ao_root():
    # Git for Windows worktree administration has a path-length limit even
    # with core.longpaths. Match the existing Product comparison short-root
    # fixture; this test owns candidate integration, not arbitrary Git paths.
    with TemporaryDirectory(prefix="da4-") as directory:
        yield Path(directory)


@pytest.mark.parametrize("mode", ["single", "multi"])
async def test_product_candidate_crosses_original_ao_and_reopens_without_adoption(
    product_ao_root, mode
):
    tmp_path = product_ao_root
    policy = real_sandbox_policy()
    benchmark = build_benchmark(tmp_path / "benchmark", arms=((mode, 1),))
    manifest = json.loads((benchmark / "benchmark.json").read_text())
    cases = json.loads((benchmark / "dataset.json").read_text())["cases"]
    assert len(cases) == 1
    case_id = cases[0]["case_id"]
    rubric = benchmark / "rubric.json"
    rubric.write_text(
        json.dumps(
            {
                "format": 1,
                "criteria": {case_id: ["Create added.txt with added followed by a newline."]},
            }
        )
    )
    manifest["assessment"] = {
        "scorer_id": "product-durable-semantic-v1",
        "version": 1,
        "rubric": {"file": rubric.name, "sha256": digest_bytes(rubric.read_bytes())},
        "requires_review": True,
    }
    write_dataset(benchmark, manifest, cases, format_version=3)
    plan_file = pair_plan(tmp_path, execution={"timeout_seconds": 180})
    plan = json.loads(plan_file.read_text())
    plan["benchmark_digest"] = digest_bytes((benchmark / "benchmark.json").read_bytes())
    plan["trials"] = {"repetitions": 1}
    plan["comparison"]["requested_modes"] = [mode, mode]
    plan["comparison"]["min_pass_gain"] = 1
    plan["execution"]["sandbox_config"] = "sandbox.json"
    plan_file.write_text(json.dumps(plan))
    (tmp_path / "sandbox.json").write_text(
        json.dumps(
            {
                "format": 2,
                "policy": to_json_value(policy),
                "plugin_grants": [],
            }
        )
    )
    from collaboration_fixtures import PLAN

    replies = [
        {"tool_calls": [{"id": "plan", "name": "submit_collaboration_plan", "arguments": PLAN}]},
        {"tool_calls": [{"id": "read", "name": "read_file", "arguments": {"path": "kept.txt"}}]},
        {
            "content": (
                "kept.txt:1 contains the original retained file; implementation belongs to main."
            )
        },
        {
            "tool_calls": [
                {
                    "id": "write",
                    "name": "apply_patch",
                    "arguments": {
                        "path": "added.txt",
                        "old_text": "",
                        "new_text": "added\n",
                        "create": True,
                    },
                }
            ]
        },
        {"content": "Added the requested file and used the source report."},
        {"content": "Delivery review: file edited; functional checks not run by this fixture."},
    ]
    if mode == "single":
        replies = replies[3:]
    for reply in replies:
        reply["usage"] = {"input_tokens": 20, "output_tokens": 10}
    (tmp_path / "script.json").write_text(json.dumps(replies))
    files = source_files()[1]
    selector = (
        ("product/execution.py", "CODER_GUIDANCE") if mode == "single"
        else ("supervision/structured_collaboration.py", "ALLOCATION_GUIDANCE")
    )
    edits = editable_text(files, (selector,))
    proposal = {
        "kind": "candidate",
        "rationale": "Explicit fixture tests the Product integration.",
        "targeted_failure_classes": ["collaboration-navigation"],
        "expected_tradeoffs": "No quality improvement is claimed by this fixture.",
        "edits": [
            {
                "file": edits[0].file,
                "selector": edits[0].selector,
                "new_text": edits[0].text + " Reuse available evidence before investigating.",
            }
        ],
    }
    provider = ScriptedLlmProvider(
        tuple(
            ModelResponse(content=json.dumps(value), usage=Usage(30, 20, UsageQuality.EXACT))
            for value in (
                proposal,
                {"status": "passed", "reason": "Fixture patch meets the criterion."},
                {"status": "passed", "reason": "Fixture patch meets the criterion."},
            )
        )
    )
    template = EvaluationRunner(
        benchmark,
        tmp_path / "unused",
        provider=provider,
        model_id="explicit-fixture",
        sandbox=policy,
        options=load_run_options(plan_file),
    )
    contract = OptimizationContract(
        "explicit-product-ao-test",
        source_digest(files),
        template.manifest.document.sha256,
        template.options.document.sha256,
        template.manifest.dataset.sha256,
        (case_id,),
        edits,
        OptimizationLimits(1, 1, 2, 1, 1, 1, 1, 16000, 200000),
        datetime.now(UTC) + timedelta(minutes=10),
    )
    original = source_digest(source_files()[1])
    output = tmp_path / "optimization"
    result = await run_strategy_optimization(
        template,
        contract,
        observations=(
            DevelopmentObservation(
                case_id,
                "collaboration-navigation",
                "Explicit integration fixture, not a real failure label.",
                (str(plan_file),),
            ),
        ),
        provider=provider,
        analysis_config=config(),
        judge_provider=provider,
        judge_config=config(),
        output_dir=output,
    )
    assert result == inspect_strategy_optimization(output)
    assert not result["adoption_authorized"]
    assert result["evaluation"]["progress"]["trials_started"] == 2
    comparison = result["evaluation"]["rounds"][0]["comparison"]
    assert comparison["complete"] and comparison["hard_constraints"] == "passed"
    assert comparison["changes"] == {"gain": 0, "loss": 0, "unchanged": 1, "unknown": 0}
    assert all(arm["assessment_counts"] == {"passed": 1} for arm in comparison["arms"]), comparison
    assert len(provider.requests) == 3  # task models execute in the original separate workers
    assert source_digest(source_files()[1]) == original
