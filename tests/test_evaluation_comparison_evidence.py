"""Offline comparisons must bind original workers and human judgments."""

import json
import sqlite3
from pathlib import Path

import pytest
import pytest_asyncio
from test_evaluation_comparison import judge_pair, run_pair

from traceh.api.json_types import fingerprint
from traceh.cli.main import _configure_from_environment, _eval, build_parser
from traceh.evaluation.comparison import compare_experiment


@pytest_asyncio.fixture(scope="module")
async def experiment(tmp_path_factory):
    root, code = await run_pair(tmp_path_factory.mktemp("comparison-evidence"))
    assert code == 0
    return root


@pytest.mark.parametrize("fault", ["source", "model", "score", "worker", "evidence"])
def test_actual_run_drift_is_not_comparable(experiment, tmp_path, fault):
    path = (
        experiment
        / {
            "source": "arms/02/run/artifacts/source.zip",
            "model": "arms/02/run/frozen.json",
            "score": "arms/02/run/report.json",
            "worker": "arms/02/worker-receipt.json",
            "evidence": "arms/02/run/evidence-manifest.json",
        }[fault]
    )
    original = path.read_bytes()
    try:
        if fault == "source":
            path.write_bytes(original + b"changed")
        else:
            data = json.loads(original)
            if fault == "model":
                data["model"]["model_id"] = "different-model"
            elif fault == "score":
                data["trials"][0]["assessment"]["status"] = "passed"
            elif fault == "worker":
                data["pid"] += 1
            else:
                data["trials"][0]["references"][0]["sha256"] = "0" * 64
            path.write_text(json.dumps(data), encoding="utf-8")
        result = compare_experiment(experiment, tmp_path / "bad")
        assert result["status"] == "not_comparable" and not result["complete"]
        assert len(result["planned_trials"]) == 2
    finally:
        path.write_bytes(original)


async def test_comparison_cli_is_offline_and_keeps_original_pending(
    experiment, tmp_path, monkeypatch, capsys
):
    import traceh.cli.main as cli

    def forbidden(*args, **kwargs):
        raise AssertionError("offline comparison cannot load environment or provider")

    monkeypatch.setattr(cli, "load_env_file", forbidden)
    monkeypatch.setattr(cli, "_provider_and_model", forbidden)
    args = build_parser().parse_args(
        ["eval", "--compare", str(experiment), "--output", str(tmp_path / "offline")]
    )
    _configure_from_environment(args, environment={"TRACEH_MODEL": "unused"})
    assert await _eval(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "inconclusive"


def test_assessment_for_other_experiment_is_rejected(experiment, tmp_path):
    # A binding from another experiment cannot borrow otherwise valid judgments.
    path = tmp_path / "assessments.json"
    path.write_text(json.dumps({"format": 1, "experiment_digest": "0" * 64, "assessments": {}}))
    result = compare_experiment(experiment, tmp_path / "other", assessments=path)
    assert result["status"] == "not_comparable"


def test_edited_derived_assessment_cannot_replace_immutable_judgment(experiment, tmp_path):
    manifest = judge_pair(experiment, ("failed", "failed"))
    references = json.loads(manifest.read_text())["assessments"]
    artifact = Path(references["experiment"]["file"])
    path = artifact.parent / "report.json"
    original = path.read_bytes()
    try:
        report = json.loads(original)
        report["trials"][0]["assessment"]["status"] = "passed"
        path.write_text(json.dumps(report))
        result = compare_experiment(experiment, tmp_path / "forged", assessments=manifest)
        assert result["status"] == "not_comparable"
    finally:
        path.write_bytes(original)


def test_pair_key_mismatch_rejected_without_dropping_unmatched_trials():
    from traceh.evaluation.comparison import paired_measurements
    from traceh.evaluation.errors import BenchmarkManifestError

    def trial(mode):
        return {
            "identity": {
                "case_id": "alpha",
                "group_id": "group",
                "material_digest": "hash",
                "material_seed": None,
                "replicate": 1,
                "requested_mode": mode,
            }
        }

    with pytest.raises(BenchmarkManifestError):
        paired_measurements(
            [{"trials": [trial("single")]}, {"trials": [trial("multi")]}], [{}, {}], [{}, {}], {}
        )


def test_single_field_approval_report_change_is_detected_by_worker_digest(experiment, tmp_path):
    path = experiment / "arms/02/run/report.json"
    original = path.read_bytes()
    try:
        data = json.loads(original)
        data["trials"][0]["assessment"]["status"] = "passed"
        path.write_text(json.dumps(data), encoding="utf-8")
        result = compare_experiment(experiment, tmp_path / "worker-binding")
        assert result["status"] == "not_comparable"
        assert result["reason"] == "evaluation-comparison-incompatible"
    finally:
        path.write_bytes(original)


def test_material_digest_is_part_of_pair_identity():
    # Material changes are never rescued by matching case IDs.
    from traceh.evaluation.comparison import paired_measurements
    from traceh.evaluation.errors import BenchmarkManifestError

    raw = {
        "case_id": "ordinary-case",
        "group_id": "ordinary-group",
        "material_seed": 11,
        "replicate": 1,
        "requested_mode": None,
    }
    arms = [{"trials": [{"identity": {**raw, "material_digest": fingerprint(x)}}]} for x in (1, 2)]
    with pytest.raises(BenchmarkManifestError):
        paired_measurements(arms, [{}, {}], [{}, {}], {})


def test_public_comparison_closes_every_real_read_connection(experiment, tmp_path, monkeypatch):
    original = sqlite3.connect
    connections = []

    def observe(*args, **kwargs):
        connection = original(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", observe)
    result = compare_experiment(experiment, tmp_path / "closed")
    assert result["complete"] and connections
    try:
        for connection in connections:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                connection.execute("SELECT 1")
    finally:
        for connection in connections:
            connection.close()
