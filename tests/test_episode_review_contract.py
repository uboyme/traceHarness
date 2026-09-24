"""Public offline review and evaluator protocol refusal boundaries."""

import json
import shutil
import sqlite3

import pytest
from test_retrieval_episode_evaluator import BENCHMARK, runner

from traceh.cli.main import CliConfigurationError, _configure_from_environment, _eval, build_parser
from traceh.evaluation.errors import BenchmarkManifestError
from traceh.evaluation.evaluators.episode_manifest import load_episode_suite
from traceh.evaluation.inputs import digest_bytes
from traceh.evaluation.manifest import load_benchmark_manifest
from traceh.evaluation.review import export_review


@pytest.mark.frozen_unicode
async def test_retrieval_plan_runs_through_cli_and_assess_remains_offline(tmp_path, capsys):
    plan = json.loads((BENCHMARK / "run-plan.example.json").read_text(encoding="utf-8"))
    plan["model"].update(
        provider="scripted",
        model="test-model",
        base_url=None,
        api_key_env="UNUSED_TEST_KEY",
        script=None,
    )
    plan["execution"].update(sandbox_config=None, max_trials=1)
    plan["trials"]["selection"] = {"case_ids": ["s-absent"], "material_seeds": [113]}
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    args = build_parser().parse_args(
        ["eval", str(BENCHMARK), "--run-plan", str(path), "--output", str(tmp_path / "run")]
    )
    _configure_from_environment(args, environment={})
    assert await _eval(args) == 0
    assert json.loads(capsys.readouterr().out)["task_type"] == "retrieval_episode"
    export_review(tmp_path / "run", tmp_path / "review")
    path = tmp_path / "review/judgment-template.json"
    judgment = json.loads(path.read_text(encoding="utf-8"))
    judgment["reviewer"] = "test-reviewer"
    path.write_text(json.dumps(judgment))
    args = build_parser().parse_args(
        [
            "eval",
            "--assess",
            str(tmp_path / "run"),
            "--judgment-file",
            str(path),
            "--output",
            str(tmp_path / "assessment"),
        ]
    )
    _configure_from_environment(args, environment={"TRACEH_MODEL": "must-not-be-used"})
    assert await _eval(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["assessment_counts"]["pending_review"] == 1
    report = json.loads((tmp_path / "assessment/report.json").read_text(encoding="utf-8"))
    assert report["execution_run"] == str((tmp_path / "run").resolve())
    packet = json.loads((tmp_path / "review/review.json").read_text(encoding="utf-8"))
    event_file = tmp_path / "review" / packet["episodes"][0]["target_events"]
    assert any(
        e["type"] == "request/snapshot"
        for e in json.loads(event_file.read_text(encoding="utf-8"))["events"]
    )


@pytest.mark.parametrize(
    "flag,value",
    [
        ("--provider", "scripted"),
        ("--env-file", "absent.env"),
        ("--run-plan", "absent.json"),
        ("--sandbox-config", "absent.json"),
    ],
)
def test_review_rejects_execution_configuration_before_loading_anything(flag, value):
    args = build_parser().parse_args(
        ["eval", "--review", "existing", "--output", "new", flag, value]
    )
    with pytest.raises(CliConfigurationError, match="eval-review-arguments-conflict"):
        _configure_from_environment(args)


@pytest.mark.frozen_unicode
async def test_review_cli_is_offline_even_with_model_environment(tmp_path, monkeypatch, capsys):
    await runner(tmp_path, "m-direct").run()
    import traceh.cli.main as cli

    def forbidden(*args, **kwargs):
        raise AssertionError("review must never load provider or environment")

    monkeypatch.setattr(cli, "_provider_and_model", forbidden)
    monkeypatch.setattr(cli, "load_env_file", forbidden)
    args = build_parser().parse_args(
        ["eval", "--review", str(tmp_path / "run"), "--output", str(tmp_path / "review")]
    )
    _configure_from_environment(args, environment={"TRACEH_MODEL": "forbidden"})
    assert await _eval(args) == 0
    assert json.loads(capsys.readouterr().out)["entries"] == 1


@pytest.mark.frozen_unicode
async def test_changed_durable_evidence_is_rejected_before_export(tmp_path):
    await runner(tmp_path, "m-direct").run()
    path = tmp_path / "run/attempts/001/events/events.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE events SET envelope_json=replace(envelope_json,"
            "'evaluation-host','different-owner') WHERE envelope_json LIKE '%evaluation-host%'"
        )
    with pytest.raises(BenchmarkManifestError) as raised:
        export_review(tmp_path / "run", tmp_path / "review")
    assert raised.value.code == "evaluation-judgment-stale"
    assert not (tmp_path / "review").exists()


@pytest.mark.parametrize(
    "change", ["duplicate", "unknown-setup", "resource-drift", "wrong-family", "unreviewed-scorer"]
)
def test_strict_material_contract_before_execution(tmp_path, change):
    root = tmp_path / "suite"
    shutil.copytree(BENCHMARK, root)
    manifest = json.loads((root / "benchmark.json").read_text(encoding="utf-8"))
    dataset = json.loads((root / "dataset.json").read_text(encoding="utf-8"))
    if change == "duplicate":
        dataset["cases"][1] = dataset["cases"][0]
    elif change == "unknown-setup":
        dataset["cases"][0]["setup"]["callback"] = "arbitrary.module:run"
    elif change == "resource-drift":
        source = next((root / "materials").glob("*/reference.txt"))
        source.write_text("different", encoding="utf-8")
    elif change == "wrong-family":
        dataset["cases"][0]["family"] = "memory"
    else:
        manifest["assessment"]["requires_review"] = False
    raw = json.dumps(dataset, ensure_ascii=False).encode()
    (root / "dataset.json").write_bytes(raw)
    manifest["dataset"]["sha256"] = digest_bytes(raw)
    (root / "benchmark.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BenchmarkManifestError):
        load_episode_suite(load_benchmark_manifest(root))
