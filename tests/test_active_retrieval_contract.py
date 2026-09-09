"""The opt-in AR evaluation must reject changed or answer-leaking contracts."""

import json
from pathlib import Path

import pytest
from live_active_retrieval.contract import freeze_manifest, load_manifest, verify_manifest

MANIFEST = Path(__file__).parent / "live_active_retrieval" / "manifest.json"


def test_contract_has_balanced_natural_tasks_and_no_target_answer(tmp_path):
    frozen = freeze_manifest(MANIFEST, tmp_path / "frozen.json")
    data = verify_manifest(MANIFEST, frozen)
    assert len(data["cases"]) == 24
    assert len(data["fixtures"]["domains"]) >= 2
    assert any(c["language"] == "en" for c in data["cases"])
    assert any(c["discovery"] for c in data["cases"])


@pytest.mark.parametrize("change", ["question", "gate", "evidence"])
def test_invalid_contract_is_rejected_before_provider(tmp_path, change):
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if change == "question":
        data["cases"][0]["question"] = "Use search_history and return {value}."
    elif change == "gate":
        data["acceptance"]["minimum_joint_pass_per_model"] = 1000
    else:
        data["cases"][0]["source_body"] = "No answer in this source."
    changed = tmp_path / "manifest.json"
    changed.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="ar-manifest-"):
        load_manifest(changed)


def test_changed_questions_cannot_reuse_a_frozen_score_contract(tmp_path):
    changed = tmp_path / "manifest.json"
    changed.write_bytes(MANIFEST.read_bytes())
    frozen = freeze_manifest(changed, tmp_path / "frozen.json")
    data = load_manifest(changed)
    data["cases"][0]["question"] += " Please."
    changed.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="ar-frozen-manifest-changed"):
        verify_manifest(changed, frozen)


def test_frozen_contract_is_not_overwritten(tmp_path):
    target = tmp_path / "frozen.json"
    freeze_manifest(MANIFEST, target)
    before = target.read_bytes()
    with pytest.raises(FileExistsError):
        freeze_manifest(MANIFEST, target)
    assert target.read_bytes() == before
