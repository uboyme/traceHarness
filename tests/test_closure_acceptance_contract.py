"""The authorized probe raises only explicit token limits and freezes its inputs."""

import json
from pathlib import Path

import pytest
from live_dynamic_collaboration import writable_acceptance as driver
from live_dynamic_collaboration.closure_acceptance import prepare

from traceh.evaluation.manifest import load_benchmark_manifest


def test_closure_probe_freezes_budget_without_expanding_capabilities(tmp_path, monkeypatch):
    # Material selection is scoped to this fixture, not later tests in the process.
    for name in ("FILES", "REFERENCE", "CHECKS", "REQUIREMENT"):
        monkeypatch.setattr(driver, name, getattr(driver, name))
    sandbox = tmp_path / "sandbox.json"
    sandbox.write_text("{}")
    root = tmp_path / "probe"
    prepare(Path(__file__).parents[1], sandbox, root)
    contract = driver.validate(root)
    assert (contract["max_real_calls"], contract["timeout_seconds"],
            contract["retry_attempts"], contract["trials"]) == (32, 600, 1, 1)
    load_benchmark_manifest(root / "material")
    settings = json.loads((root / "material/benchmark.json").read_text())["task_settings"]
    assert settings["task_budget"]["max_tokens"] == 480000
    assert settings["roles"]["coder"]["budget"]["max_tokens"] == 360000
    child = settings["roles"]["patch_author"]
    assert child["budget"]["max_tokens"] == 30000
    assert child["capability_grants"] == ["list_files", "read_file", "search_text", "apply_patch"]
    (root / "stability_materials.py").write_text("changed")
    with pytest.raises(ValueError, match="material-drift"):
        driver.validate(root)
