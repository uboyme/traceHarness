"""Writable collaboration stays on the existing EvaluationRunner mainline."""

import json
from copy import deepcopy

import pytest
from evaluation_fixtures import write_dataset
from sandbox_fixtures import real_sandbox_policy
from test_patch_integration import IntegratingProvider
from test_product_benchmark_e2e import build_benchmark

from traceh.evaluation.plan import RunOptions
from traceh.evaluation.repositories import capture_initial_tree, initial_tree_digest
from traceh.evaluation.runner import EvaluationRunner
from traceh.llm.retry import NO_MODEL_RETRY


@pytest.mark.asyncio
@pytest.mark.parametrize('fail_after_dispatches', [None, 0, 1])
async def test_writable_product_evaluation_counts_child_and_converges(
    tmp_path, fail_after_dispatches,
):
    class Provider(IntegratingProvider):
        dispatches = 0

        async def complete(self, request):
            if 'integrate_child_patch' in {t.name for t in request.tools}:
                if self.dispatches == fail_after_dispatches:
                    raise RuntimeError('explicit-provider-failure-after-handoff')
                self.dispatches += 1
            return await super().complete(request)

    root = build_benchmark(tmp_path / "material", arms=(("multi", 1),))
    manifest = json.loads((root / "benchmark.json").read_text())
    dataset = json.loads((root / "dataset.json").read_text())
    case = dataset["cases"][0]
    initial = root / case["initial_tree"]
    (initial / ".gitattributes").write_bytes(b"* -text\n")
    (initial / "tracked.txt").write_bytes(b"base\n")
    case["sha256"] = initial_tree_digest(capture_initial_tree(initial))
    settings = manifest["task_settings"]
    child = deepcopy(settings["roles"]["investigator"])
    child["capability_grants"].append("apply_patch")
    child["budget"].update(max_steps=6, max_tool_calls=6)
    settings["roles"]["patch_author"] = child
    write_dataset(root, manifest, [case], format_version=2)
    report = await EvaluationRunner(
        root,
        tmp_path / "run",
        provider=Provider(),
        model_id="explicit-fixture",
        retry_policy=NO_MODEL_RETRY,
        options=RunOptions(repetitions=1, max_trials=1),
        sandbox=real_sandbox_policy(),
    ).run()
    assert report.complete
    (attempt,) = report.task_report.attempts
    assert attempt.success is (fail_after_dispatches is None)
    evidence = attempt.evidence
    assert evidence.converged
    assert bool(evidence.review_passed) is (fail_after_dispatches is None)
    assert len(evidence.execution.sessions) == 2
    if fail_after_dispatches is None:
        assert evidence.execution.tokens.total_tokens > 0
    else:
        assert evidence.execution.tokens is None  # Failed Provider usage stays unknown.
    (handoff,) = evidence.investigations
    assert handoff["role"] == "patch_author"
    assert handoff["report_status"] == "completed"
    assert bool(handoff["parent_report_dispatches"]) is (fail_after_dispatches != 0)
