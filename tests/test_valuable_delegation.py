import asyncio
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from live_dynamic_collaboration import valuable_delegation as live

from traceh.evaluation.inputs import read_input
from traceh.evaluation.manifest import load_benchmark_manifest


@pytest.fixture
def prepared(tmp_path):
    sandbox = tmp_path / "sandbox.json"
    sandbox.write_text('{"format":2}', encoding="utf-8")
    output = tmp_path / "observation"
    live.prepare(Path(__file__).parents[1], sandbox, output)
    return output


def test_four_cases_use_existing_product_evaluator_without_forcing_helpers(prepared):
    contract = live.validate_inputs(prepared)
    assert len(contract["scenarios"]) == 4
    assert contract["max_total_real_calls"] == 68
    assert contract["adoption_authorized"] is False
    for name in contract["scenario_order"]:
        root = prepared / "materials" / name
        manifest = load_benchmark_manifest(root)
        assert manifest.document.data["task_type"] == "product_task"
        requirement = read_input(root, "dataset.json").data["cases"][0]["requirement"]
        assert "delegate_investigation" not in requirement
        assert "after_tool_results" not in requirement
    with pytest.raises(FileExistsError):
        live.prepare(Path(__file__).parents[1], prepared / "sandbox.json", prepared)


@pytest.mark.parametrize("name", live.SCENARIOS)
def test_real_frozen_verifier_rejects_missing_output_and_source_changes(
    prepared, tmp_path, monkeypatch, name
):
    material = prepared / "materials" / name
    work = tmp_path / "work"
    shutil.copytree(material / "initial", work)
    monkeypatch.chdir(work)
    verifier = read_input(material, "dataset.json").data["cases"][0]["verification"]["commands"][0][
        "argv"
    ][3]
    with pytest.raises(AssertionError):
        exec(verifier, {})
    shutil.copyfile(material / "reference-output.json", work / "answer.json")
    exec(verifier, {})
    source = work / live.SCENARIOS[name]["source_paths"][0]
    source.write_text("changed", encoding="utf-8")
    with pytest.raises(AssertionError):
        exec(verifier, {})


def test_first_section_invalid_line_is_rejected(prepared, tmp_path, monkeypatch):
    material = prepared / "materials/parallel-investigation"
    work = tmp_path / "work"
    shutil.copytree(material / "initial", work)
    monkeypatch.chdir(work)
    reference = read_input(material, "reference-output.json").data
    reference["sections"][0]["evidence"][0]["start_line"] = 999999
    (work / "answer.json").write_text(json.dumps(reference), encoding="utf-8")
    verifier = read_input(material, "dataset.json").data["cases"][0]["verification"]["commands"][0][
        "argv"
    ][3]
    with pytest.raises(AssertionError):
        exec(verifier, {})


def attempt(*, success=True, delegated=True, visible=1, overlap=1):
    return {
        "success": success,
        "error_code": None if success else "failed",
        "evidence": {
            "investigations": [{"agent_id": "owned-child"}] if delegated else [],
            "collaboration": {
                "reports_dispatched_to_parent": visible,
                "observed_peak_active_turns": overlap,
            },
            "product_status": "completed" if success else "failed",
            "budget": {"converged": True},
            "workspaces": {"live": 0},
            "execution": {"tokens": 100, "provider_failure_categories": {}},
        },
    }


@pytest.mark.parametrize("overlap", [1, 2, None])
def test_waiting_or_overlap_never_decides_value(overlap):
    row = live.observation(attempt(overlap=overlap))
    assert row["task_success"]
    assert row["chain_observation"] == "report-visible-use-unreviewed"
    assert row["report_use_review"] == "pending"
    assert row["collaboration_value"] == "unestablished"


def test_no_helper_is_valid_task_but_not_exercised_collaboration():
    row = live.observation(attempt(delegated=False, visible=0))
    assert row["task_success"]
    assert row["chain_observation"] == "not-exercised"


def test_failed_task_and_unseen_report_are_not_passes():
    row = live.observation(attempt(success=False, visible=0))
    assert not row["task_success"]
    assert row["chain_observation"] == "report-not-visible"
    assert row["report_use_review"] == "not-observed"


def test_changed_source_material_rejected_before_provider(prepared):
    source = prepared / "materials/simple-read/initial/src/traceh/version.py"
    source.write_text("different", encoding="utf-8")
    with pytest.raises(ValueError, match="source-material-drift"):
        live.validate_inputs(prepared)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_real_driver_preserves_failure_and_forbids_rerun(prepared, monkeypatch, failure):
    contract = live.validate_inputs(prepared)
    live.write(
        prepared / "preflight.json",
        {
            "contract_digest": read_input(prepared, "contract.json").sha256,
            "image": "test-image",
            "network": "none",
            "scenarios": [
                {
                    "scenario": name,
                    "outcomes": [
                        {"reference_output": False, "exit_code": 1, "contract_verified": True},
                        {"reference_output": True, "exit_code": 0, "contract_verified": True},
                    ],
                }
                for name in contract["scenario_order"]
            ],
        },
    )
    monkeypatch.setattr(
        live,
        "load_sandbox_file",
        lambda _: SimpleNamespace(
            plugin_grants=(), policy=SimpleNamespace(network="none", image="test-image")
        ),
    )
    provider = SimpleNamespace(name="test-provider")
    monkeypatch.setattr(
        live,
        "connection",
        lambda _: (SimpleNamespace(base_url="https://example.invalid"), provider, "test-model"),
    )
    calls = []

    class Runner:
        def __init__(self, *args, **kwargs):
            calls.append(kwargs["provider"])

        async def run(self):
            raise failure()

    monkeypatch.setattr(live, "EvaluationRunner", Runner)
    with pytest.raises(RuntimeError, match="historical-collaboration-contract"):
        await live.run(Path("unused"), prepared)
    assert len(calls) == 0
    summary = read_input(prepared, "summary.json").data
    assert summary["stopped"] == "RuntimeError"
    assert summary["outcomes"] == []
    with pytest.raises(FileExistsError):
        await live.run(Path("unused"), prepared)
    assert len(calls) == 0
