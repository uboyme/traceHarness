"""Frozen WC-4 inputs and hard whole-tree provider boundary."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from live_dynamic_collaboration.writable_acceptance import BoundedProvider, prepare, validate

from traceh.evaluation.manifest import load_benchmark_manifest


def test_writable_contract_freezes_one_task_and_rejects_drift(tmp_path):
    sandbox = tmp_path / "sandbox.json"
    sandbox.write_text("{}")
    output = tmp_path / "frozen"
    prepare(Path(__file__).parents[1], sandbox, output)
    contract = validate(output)
    assert contract["max_real_calls"] == 32
    assert contract["timeout_seconds"] == 600
    assert contract["connection_timeout_seconds"] == 60
    assert contract["retry_attempts"] == contract["trials"] == 1
    assert load_benchmark_manifest(output / "material").task_type == "product_task"
    (output / "material/initial/telemetry_rules.py").write_text("drift")
    with pytest.raises(ValueError, match="material-drift"):
        validate(output)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, OSError, asyncio.CancelledError])
async def test_shared_cap_stops_after_first_failure_or_cancellation(failure):
    class Provider:
        name = "explicit-fixture"
        calls = 0

        async def complete(self, request):
            self.calls += 1
            if failure is not None:
                raise failure()
            return object()

    inner = Provider()
    gate = BoundedProvider(inner)
    main = SimpleNamespace(system_prompt="main")
    child = SimpleNamespace(system_prompt="traceh.product.patch-author")
    if failure is None:
        for index in range(32):
            await gate.complete(main if index % 2 else child)
        assert gate.roles == {"main": 16, "child": 16}
    else:
        with pytest.raises(failure):
            await gate.complete(main)
    with pytest.raises(RuntimeError):
        await gate.complete(child)
    assert inner.calls == gate.calls == (32 if failure is None else 1)
