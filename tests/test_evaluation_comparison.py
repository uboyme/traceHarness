"""UE-3 public runs, immutable candidates and offline paired measurements."""

import ast
import copy
import json
import os
import sqlite3
from pathlib import Path

import pytest
from test_retrieval_episode_evaluator import BENCHMARK

from traceh.api.json_types import fingerprint
from traceh.cli.main import _configure_from_environment, _eval, build_parser
from traceh.evaluation.comparison import compare_experiment, paired_measurements
from traceh.evaluation.errors import BenchmarkManifestError
from traceh.evaluation.inputs import digest_bytes
from traceh.evaluation.plan import load_run_options
from traceh.evaluation.review import assess_run, export_review
from traceh.evaluation.variants import apply_candidate, source_digest, source_files, text_node


def candidate_patch(new_text=None):
    _, files = source_files()
    node = text_node(ast.parse(dict(files)["runtime/prompt.py"]), "_REFERENCE_GUIDANCE")
    return {
        "format": 1,
        "base_source_digest": source_digest(files),
        "edits": [
            {
                "file": "runtime/prompt.py",
                "selector": "_REFERENCE_GUIDANCE",
                "old_sha256": digest_bytes(node.value.encode()),
                "new_text": new_text
                or node.value + "\nEXPLICIT TEST CANDIDATE TEXT: preserve evidence scope.",
            }
        ],
    }


def pair_plan(root, *, patch=False, case_id="s-absent", model=None, execution=None):
    root.mkdir(parents=True, exist_ok=True)
    data = json.loads((BENCHMARK / "run-plan.example.json").read_text(encoding="utf-8"))
    (root / "script.json").write_text(
        json.dumps(
            [
                {
                    "content": "当前资料中未找到依据。",
                    "usage": {"input_tokens": 20, "output_tokens": 10},
                }
            ]
        ),
        encoding="utf-8",
    )
    data["variants"] = [
        {"variant_id": "reference", "role": "baseline", "source": "current"},
        {"variant_id": "experiment", "role": "candidate", "source": "current"},
    ]
    if patch:
        path = root / "candidate.json"
        path.write_text(json.dumps(candidate_patch()), encoding="utf-8")
        data["variants"][1]["source"] = {
            "file": path.name,
            "sha256": digest_bytes(path.read_bytes()),
        }
    data["model"].update(
        provider="scripted",
        model="explicit-fixture",
        base_url=None,
        api_key_env="UNUSED_EVALUATION_KEY",
        script="script.json",
    )
    if model:
        data["model"].update(model)
    data["execution"].update(
        sandbox_config=None,
        max_trials=2,
        timeout_seconds=120,
        shutdown_seconds=30,
        first_arm="baseline",
        network_mode="direct",
    )
    if execution:
        data["execution"].update(execution)
    data["trials"]["selection"] = {"case_ids": [case_id], "material_seeds": [113]}
    data["comparison"] = {
        "kind": "text_candidate",
        "requested_modes": None,
        "format": 3,
        "min_pass_gain": None,
        "max_token_ratio": 1.1,
        "max_tool_call_delta": 0,
    }
    path = root / "plan.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    (root / "empty.env").write_bytes(b"")
    return path


async def run_pair(root, **kwargs):
    plan = pair_plan(root, **kwargs)
    output = root / "experiment"
    args = build_parser().parse_args(
        [
            "eval",
            str(BENCHMARK),
            "--run-plan",
            str(plan),
            "--output",
            str(output),
            "--env-file",
            str(root / "empty.env"),
        ]
    )
    _configure_from_environment(args)
    code = await _eval(args)
    return output, code


def judge_pair(root, statuses):
    refs = {}
    for index, status in enumerate(statuses, 1):
        run = root / f"arms/{index:02d}/run"
        review = root.parent / f"review-{index}"
        assessment = root.parent / f"assessment-{index}"
        export_review(run, review)
        path = review / "judgment-template.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        report = json.loads((run / "report.json").read_text(encoding="utf-8"))
        data["reviewer"] = "fixture-human-review"
        data["judgments"] = [
            {
                "trial_id": t["identity"]["trial_id"],
                "status": status,
                "reason": "Explicit deterministic review fixture.",
            }
            for t in report["trials"]
        ]
        path.write_text(json.dumps(data))
        assess_run(run, path, assessment)
        artifact = assessment / "assessment.json"
        refs[report["trials"][0]["identity"]["variant_id"]] = {
            "file": str(artifact),
            "sha256": digest_bytes(artifact.read_bytes()),
        }
    manifest = root.parent / "assessments.json"
    manifest.write_text(
        json.dumps(
            {
                "format": 1,
                "experiment_digest": fingerprint(
                    json.loads((root / "experiment.json").read_text(encoding="utf-8"))
                ),
                "assessments": refs,
            }
        )
    )
    return manifest


@pytest.mark.frozen_unicode
@pytest.mark.parametrize("patch", [False, True])
async def test_public_cli_isolated_workers_load_exact_sources_and_keep_pending(tmp_path, patch):
    root, code = await run_pair(tmp_path, patch=patch)
    report = json.loads((root / "comparison/report.json").read_text(encoding="utf-8"))
    assert code == 0, report
    assert report["complete"] and report["status"] == "inconclusive"
    assert report["planned_pairs"] == 1 and report["changes"]["unknown"] == 1
    assert report["cost_delta"] == {"total_tokens": 0, "tool_calls": 0}
    receipts = [
        json.loads((root / f"arms/{i:02d}/worker-receipt.json").read_text(encoding="utf-8"))
        for i in (1, 2)
    ]
    assert receipts[0]["pid"] != receipts[1]["pid"] != os.getpid()
    assert all(Path(r["source_root"]).is_relative_to(root) for r in receipts)
    assert (receipts[0]["source_digest"] != receipts[1]["source_digest"]) is patch
    if patch:
        original = (root / "arms/01/code/traceh/runtime/prompt.py").read_bytes()
        changed = (root / "arms/02/code/traceh/runtime/prompt.py").read_bytes()
        assert b"EXPLICIT TEST CANDIDATE TEXT" not in original
        assert b"EXPLICIT TEST CANDIDATE TEXT" in changed
        with sqlite3.connect(root / "arms/02/run/attempts/001/events/events.sqlite3") as db:
            rows = db.execute("SELECT envelope_json FROM events").fetchall()
            rows = [r for r in rows if json.loads(r[0])["type"] == "request/snapshot"]
        assert rows and any("EXPLICIT TEST CANDIDATE TEXT" in r[0] for r in rows)
    judged = judge_pair(root, ("passed", "passed"))
    compared = compare_experiment(root, tmp_path / "judged", assessments=judged)
    assert compared["status"] == "no_change", compared
    assert compared["changes"] == {"gain": 0, "loss": 0, "unchanged": 1, "unknown": 0}
    markdown = (tmp_path / "judged/report.md").read_text(encoding="utf-8")
    encoded = markdown.split("```json\n", 1)[1].split("\n```", 1)[0]
    assert json.loads(encoded) == compared
    assert compared["adoption_authorized"] is False


@pytest.mark.parametrize("fault", ["base", "grader", "budget", "old-text", "duplicate"])
def test_patch_refuses_wrong_baseline_and_non_text_owners(fault):
    _, files = source_files()
    patch = candidate_patch()
    if fault == "base":
        patch["base_source_digest"] = "0" * 64
    elif fault == "grader":
        patch["edits"][0]["file"] = "evaluation/evaluators/episode_assessment.py"
    elif fault == "budget":
        patch["edits"][0]["selector"] = "HistorySearchTool.input_schema"
        patch["edits"][0]["file"] = "tools/reference_search.py"
    elif fault == "old-text":
        patch["edits"][0]["old_sha256"] = "0" * 64
    else:
        patch["edits"].append(patch["edits"][0])
    with pytest.raises(BenchmarkManifestError):
        apply_candidate(files, patch)


def test_non_example_unicode_edit_cannot_become_executable_python():
    _, files = source_files()
    value = "证据范围\n'; raise RuntimeError('must remain text')\n"
    patch = candidate_patch(value.rstrip())
    changed = dict(apply_candidate(files, patch))
    node = text_node(ast.parse(changed["runtime/prompt.py"]), "_REFERENCE_GUIDANCE")
    assert node.value == value.rstrip()
    assert all(changed[p] == b for p, b in files if p != "runtime/prompt.py")


@pytest.mark.parametrize("selector", ["PLAN_REQUIREMENT", "CollaborationPlanTool.description"])
def test_candidate_cannot_edit_mandatory_collaboration_contract(selector):
    _, files = source_files()
    patch = candidate_patch()
    patch["edits"][0].update(file="supervision/structured_collaboration.py", selector=selector)
    with pytest.raises(BenchmarkManifestError) as refused:
        apply_candidate(files, patch)
    assert refused.value.code == "evaluation-candidate-scope-invalid"


@pytest.mark.parametrize("change", ["gain", "loss", "unknown-cost", "transport", "mixed", "prep"])
def test_pure_comparison_keeps_denominator_and_distinguishes_cost_and_failure(change):
    identity = {
        "case_id": "separate-domain-case",
        "group_id": "separate-group",
        "material_digest": "f" * 64,
        "material_seed": 909,
        "replicate": 1,
        "requested_mode": None,
        "trial_id": "test-trial",
        "variant_id": "arm",
    }
    trial = {
        "identity": identity,
        "execution": {"status": "completed", "reason": None},
        "assessment": {"status": "passed"},
        "invariants": "passed",
        "convergence": "converged",
        "measured": True,
    }
    reports = [{"trials": [copy.deepcopy(trial)]} for _ in range(2)]
    statistics = [{"test-trial": {"total_tokens": 100, "tool_calls": 1}} for _ in range(2)]
    packets = [{"test-trial": {"preparation_text_digest": "same"}} for _ in range(2)]
    policy = {
        "kind": "text_candidate",
        "requested_modes": None,
        "min_pass_gain": None,
        "max_token_ratio": 1.2,
        "max_tool_call_delta": 1,
    }
    if change in ("gain", "mixed"):
        reports[0]["trials"][0]["assessment"]["status"] = "failed"
    elif change == "loss":
        reports[1]["trials"][0]["assessment"]["status"] = "failed"
    elif change == "unknown-cost":
        statistics[1]["test-trial"]["total_tokens"] = None
    elif change == "transport":
        reports[1]["trials"][0]["execution"] = {"status": "failed", "reason": "provider-network"}
        reports[1]["trials"][0]["assessment"]["status"] = "unassessable"
    else:
        packets[1]["test-trial"]["preparation_text_digest"] = "different"
    if change == "mixed":
        statistics[1]["test-trial"]["total_tokens"] = 150
    result = paired_measurements(reports, statistics, packets, policy)
    assert result["status"] == {"gain": "improved", "loss": "regressed", "mixed": "mixed"}.get(
        change, "inconclusive"
    )
    assert result["planned_pairs"] == 1 and len(result["pairs"]) == 1
    if change == "unknown-cost":
        assert result["thresholds"]["max_token_ratio"] is None


@pytest.mark.frozen_unicode
def test_two_variant_max_trials_counts_both_arms(tmp_path):
    plan = pair_plan(tmp_path, execution={"max_trials": 1})
    from traceh.evaluation.runner import EvaluationRunner
    from traceh.llm.scripted import ScriptedLlmProvider

    with pytest.raises(BenchmarkManifestError, match="max_trials"):
        EvaluationRunner(
            BENCHMARK,
            tmp_path / "out",
            provider=ScriptedLlmProvider(()),
            model_id="explicit-fixture",
            options=load_run_options(plan),
        )
