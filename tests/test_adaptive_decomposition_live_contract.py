import json
import shutil
from pathlib import Path

import pytest
from live_dynamic_collaboration.decomposition import SCENARIOS, prepare

from traceh.evaluation.inputs import read_input
from traceh.evaluation.manifest import load_benchmark_manifest


def test_prepare_freezes_general_positive_and_negative_observations(tmp_path):
    repository = Path(__file__).parents[1]
    sandbox = tmp_path / "sandbox.json"
    sandbox.write_text('{"format":2}', encoding="utf-8")
    output = tmp_path / "observation"

    prepare(repository, sandbox, output)

    contract = read_input(output, "contract.json").data
    assert contract["scenario_order"] == [
        "separable-review",
        "coupled-review",
        "simple-read",
    ]
    assert {row["kind"] for row in contract["scenarios"].values()} == {
        "separable",
        "coupled",
        "simple",
    }
    assert contract["max_total_real_calls"] == 36
    assert contract["retry_attempts"] == 1
    assert contract["semantic_judge_calls"] == contract["baseline_trials"] == 0
    assert contract["adoption_authorized"] is False
    for scenario_id in contract["scenario_order"]:
        material = output / "materials" / scenario_id
        manifest = load_benchmark_manifest(material)
        assert manifest.document.sha256 == contract["materials"][scenario_id][
            "manifest_digest"
        ]
        assert manifest.dataset.sha256 == contract["materials"][scenario_id][
            "dataset_digest"
        ]
        requirement = read_input(material, "dataset.json").data["cases"][0]["requirement"]
        assert "delegate_investigation" not in requirement

    with pytest.raises(FileExistsError):
        prepare(repository, sandbox, output)


@pytest.mark.parametrize("scenario_id", SCENARIOS)
def test_frozen_verifier_rejects_missing_output_and_accepts_reference(
    tmp_path, monkeypatch, scenario_id
):
    repository = Path(__file__).parents[1]
    sandbox = tmp_path / "sandbox.json"
    sandbox.write_text('{"format":2}', encoding="utf-8")
    output = tmp_path / "observation"
    prepare(repository, sandbox, output)
    material = output / "materials" / scenario_id
    case = read_input(material, "dataset.json").data["cases"][0]
    verifier = case["verification"]["commands"][0]["argv"][3]
    work = tmp_path / "work"
    shutil.copytree(material / "initial", work)
    monkeypatch.chdir(work)

    with pytest.raises(AssertionError):
        exec(verifier, {})

    shutil.copy2(material / "reference-output.json", work / "answer.json")
    exec(verifier, {})
    assert json.loads((work / "answer.json").read_text(encoding="utf-8"))
