from pathlib import Path
from types import SimpleNamespace

import pytest
from live_dynamic_collaboration.budget_autonomy import CappedProvider, prepare

from traceh.evaluation.inputs import read_input
from traceh.evaluation.manifest import load_benchmark_manifest


def test_prepare_freezes_three_explicit_observations(tmp_path):
    repository = Path(__file__).parents[1]
    sandbox = tmp_path / "sandbox.json"
    sandbox.write_text('{"format":2}', encoding="utf-8")
    output = tmp_path / "observation"

    prepare(repository, sandbox, output)

    contract = read_input(output, "contract.json").data
    assert contract["scenario_order"] == [
        "complex-headroom",
        "sufficient-simple",
        "constrained-headroom",
    ]
    assert contract["max_total_real_calls"] == 37
    assert contract["retry_attempts"] == 1
    assert contract["semantic_judge_calls"] == contract["baseline_trials"] == 0
    for scenario_id in contract["scenario_order"]:
        manifest = load_benchmark_manifest(output / "materials" / scenario_id)
        assert manifest.document.sha256 == contract["materials"][scenario_id][
            "manifest_digest"
        ]
        assert manifest.dataset.sha256 == contract["materials"][scenario_id][
            "dataset_digest"
        ]

    with pytest.raises(FileExistsError):
        prepare(repository, sandbox, output)


@pytest.mark.asyncio
async def test_capped_provider_counts_role_and_rejects_before_extra_call():
    class Inner:
        name = "scripted-test"

        def __init__(self):
            self.calls = 0

        async def complete(self, request):
            self.calls += 1
            return request

    inner = Inner()
    provider = CappedProvider(inner, 2)

    await provider.complete(
        SimpleNamespace(tools=[SimpleNamespace(name="decide_investigation_budget")])
    )
    await provider.complete(
        SimpleNamespace(tools=[SimpleNamespace(name="request_investigation_budget")])
    )
    with pytest.raises(RuntimeError, match="budget-autonomy-real-call-limit"):
        await provider.complete(SimpleNamespace(tools=[]))

    assert inner.calls == provider.calls == 2
    assert provider.roles == {"main": 1, "child": 1}
    assert provider.limit_reached is True
