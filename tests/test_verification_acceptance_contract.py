"""The real acceptance is one frozen task with a whole-tree call ceiling."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from live_dynamic_collaboration.verification_acceptance import BoundedProvider, prepare, validate
from live_dynamic_collaboration.verification_materials import CHECKS, FILES, REFERENCE

from traceh.evaluation.manifest import load_benchmark_manifest


def test_new_multi_material_is_frozen_and_oracle_rejects_stub(tmp_path):
    sandbox = tmp_path / "sandbox.json"
    sandbox.write_text("{}")
    output = tmp_path / "contract"
    prepare(Path(__file__).parents[1], sandbox, output)
    contract = validate(output)
    assert contract["max_real_calls"] == 16
    assert contract["timeout_seconds"] == 600
    manifest = load_benchmark_manifest(output / "material")
    assert manifest.task_type == "product_task"
    raw = json.loads((output / "material/benchmark.json").read_text())
    assert raw["task_settings"]["modes"] == ["multi"]
    namespace = {}
    exec(REFERENCE, namespace)
    oracle = CHECKS.replace("from coverage_windows import coverage", "")
    exec(oracle, namespace)
    namespace = {}
    exec(FILES["coverage_windows.py"], namespace)
    with pytest.raises(AssertionError):
        exec(oracle, namespace)
    (output / "material/initial/validation.md").write_text("changed")
    with pytest.raises(ValueError, match="material-drift"):
        validate(output)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_real_provider_gate_never_retries_or_exceeds_tree_cap(failure):
    class Provider:
        name = "explicit-fixture"
        calls = 0

        async def complete(self, request):
            self.calls += 1
            if failure:
                raise OSError("fixture connection failure")
            return object()

    inner = Provider()
    gate = BoundedProvider(inner, maximum=1)
    request = SimpleNamespace(tools=())
    if failure:
        with pytest.raises(OSError):
            await gate.complete(request)
    else:
        await gate.complete(request)
    with pytest.raises(RuntimeError):
        await gate.complete(request)
    assert gate.calls == inner.calls == 1
