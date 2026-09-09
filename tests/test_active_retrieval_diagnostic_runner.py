"""Diagnostic paths must satisfy the same Skill root contract as the grid runner."""

import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest
from live_active_retrieval import output_diagnosis as runner
from skill_fixtures import contribution

from traceh.api.skills import SkillResourceRoot


@pytest.mark.asyncio
@pytest.mark.parametrize("relative", [True, False])
async def test_diagnostic_case_paths_satisfy_skill_root_contract(tmp_path, monkeypatch, relative):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "experiment"
    root.mkdir()
    (root / "experiment.json").write_text(json.dumps({
        "source_files": {"baseline": {}}, "provider": "test", "model": "test",
        "manifest": {"limits": {"provider_timeout_seconds": 1}},
        "fixtures": [{"identity": "explicit-test"}],
    }), encoding="utf-8")
    monkeypatch.setattr(runner, "source_files", lambda _: {})
    monkeypatch.setattr(runner, "load_provider", lambda _: (SimpleNamespace(name="test"), "test"))
    visited = []

    async def case(folder, fixture, frozen, provider, model):
        resources = folder / "plugin-resources"
        resources.mkdir(parents=True)
        owner = contribution("diagnostic.test", "manual", "body").descriptor.plugin
        binding = SkillResourceRoot(owner, resources)
        visited.append(binding)
        return {"provisional_joint_pass": False}

    monkeypatch.setattr(runner, "run_case", case)
    await runner.main(Namespace(output=Path("experiment") if relative else root,
                                arm="baseline", profile=None))
    assert len(visited) == 1


@pytest.mark.asyncio
async def test_wrong_frozen_source_stops_before_provider_loading(tmp_path, monkeypatch):
    (tmp_path / "experiment.json").write_text(json.dumps({
        "source_files": {"candidate": {"different": "source"}},
    }), encoding="utf-8")
    monkeypatch.setattr(runner, "source_files", lambda _: {})

    def forbidden(_):
        pytest.fail("Provider must not load for a mismatched source")

    monkeypatch.setattr(runner, "load_provider", forbidden)
    with pytest.raises(AssertionError):
        await runner.main(Namespace(output=tmp_path, arm="candidate", profile=None))
    assert not (tmp_path / "candidate").exists()
