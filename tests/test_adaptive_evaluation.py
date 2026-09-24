"""Original Product evaluator: case-owned verification and complete owned-tree cost."""

import json
from dataclasses import asdict

import pytest
from evaluation_fixtures import material_case, write_dataset
from sandbox_fixtures import real_sandbox_policy
from test_product_adaptive import DelegatingProvider, profile
from test_product_benchmark_e2e import (
    PRODUCT_MODEL_ID,
    PRODUCT_PROVIDER_ID,
    _ProductProvider,
    build_benchmark,
)

from traceh.api.product import RequestedTaskMode
from traceh.evaluation.runner import EvaluationRunner


def _multi_material(root):
    build_benchmark(root, arms=(("multi", 1),))
    manifest = json.loads((root / "benchmark.json").read_text())
    cases = json.loads((root / "dataset.json").read_text())["cases"]
    settings = manifest["task_settings"]
    frozen = profile(RequestedTaskMode.MULTI)
    settings["task_budget"] = asdict(frozen.task_budget)
    settings["retained_tokens"] = frozen.retained_tokens
    settings["investigator_initial_tokens"] = frozen.investigator_initial_tokens
    for role in ("coder", "investigator"):
        declared = getattr(frozen, role)
        settings["roles"][role]["budget"] = asdict(declared.budget)
        settings["roles"][role]["max_turn_wall_milliseconds"] = declared.max_turn_wall_milliseconds
    initial = root / cases[0]["initial_tree"]
    (initial / "tracked.txt").write_text("base\n")
    cases[0]["sha256"] = material_case(root, "unused", "unused", cases[0]["initial_tree"])["sha256"]
    write_dataset(root, manifest, cases, format_version=3)


@pytest.mark.parametrize("fail_child", [False, True])
async def test_evaluator_counts_completed_and_failed_child(tmp_path, fail_child):
    root = tmp_path / "b"
    _multi_material(root)
    provider = DelegatingProvider(fail_child)
    provider.name = PRODUCT_PROVIDER_ID
    report = await EvaluationRunner(
        root,
        tmp_path / "out",
        provider=provider,
        model_id=PRODUCT_MODEL_ID,
        sandbox=real_sandbox_policy(),
    ).run()
    (attempt,) = report.task_report.attempts
    assert attempt.success is not fail_child
    assert attempt.evidence.converged
    evidence = attempt.evidence
    assert evidence.resolved_mode.value == "multi"
    assert len(evidence.execution.sessions) == 2
    assert evidence.unattributed.sessions == ()
    assert len(evidence.investigations) == 1
    diagnostics = evidence.collaboration
    assert diagnostics["available_reports"] == 1
    assert diagnostics["reports_dispatched_to_parent"] == (0 if fail_child else 1)
    assert diagnostics["identical_work_extra_deliveries"] == 0
    assert diagnostics["observed_peak_active_turns"] >= 2
    assert all(row["report_available"] for row in evidence.investigations)
    assert all(
        row["report_status"] == ("failed" if fail_child else "completed")
        for row in evidence.investigations
    )
    assert all(
        bool(row["child_visible_source_outputs"]) is not fail_child
        for row in evidence.investigations
    )
    assert all(
        bool(row["parent_report_dispatches"]) is not fail_child for row in evidence.investigations
    )
    # Count actual requests made by the fixture, including interrupted calls;
    # parent delegation reserves are not usage and must not be counted again.
    assert evidence.execution.model_attempts == len(provider.main_requests) + len(
        provider.child_requests
    )
    if fail_child:
        assert evidence.execution.tokens is None
    else:
        assert evidence.execution.tokens is not None
        assert evidence.execution.tokens.total_tokens == sum(
            session.tokens.total_tokens for session in evidence.execution.sessions
        )
        assert all(session.model_attempts > 0 for session in evidence.execution.sessions)


async def test_verifier_belongs_to_each_case_and_is_not_in_the_source_tree(tmp_path):
    root = build_benchmark(tmp_path / "b", arms=(("single", 1),), tasks=("first", "second"))
    manifest = json.loads((root / "benchmark.json").read_text())
    cases = json.loads((root / "dataset.json").read_text())["cases"]
    cases[1]["verification"]["commands"][0]["argv"] = [
        "python",
        "-c",
        "from pathlib import Path; assert Path('added.txt').read_text() == 'different'",
    ]
    write_dataset(root, manifest, cases, format_version=3)
    assert all(
        list((root / case["initial_tree"]).iterdir()) == [root / case["initial_tree"] / "kept.txt"]
        for case in cases
    )
    runner = EvaluationRunner(
        root,
        tmp_path / "out",
        provider=_ProductProvider(),
        model_id=PRODUCT_MODEL_ID,
        sandbox=real_sandbox_policy(),
    )
    plans = [task.verifier_definition_digest for task in runner.evaluator.suite.tasks]
    assert len(set(plans)) == 2
    report = await runner.run()
    first, second = report.task_report.attempts
    assert first.success
    assert not second.success and second.evidence.review_passed is False
    assert first.evidence.verifier_definition_digest == plans[0]
    assert second.evidence.verifier_definition_digest == plans[1]
