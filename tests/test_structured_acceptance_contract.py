"""WC-1C caps and frozen materials without external model calls."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from live_dynamic_collaboration.structured_acceptance import (
    REFERENCE,
    BoundedProvider,
    prepare,
    validate,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, RuntimeError, asyncio.CancelledError])
async def test_cap_and_failure_prevent_another_external_call(failure):
    class Inner:
        name = "explicit-test"
        calls = 0

        async def complete(self, request):
            self.calls += 1
            if failure:
                raise failure()
            return "ok"

    inner = Inner()
    provider = BoundedProvider(inner, 1)
    request = SimpleNamespace(tools=[])
    if failure:
        with pytest.raises(failure):
            await provider.complete(request)
    else:
        assert await provider.complete(request) == "ok"
    with pytest.raises(RuntimeError, match="wc1c-call-limit"):
        await provider.complete(request)
    assert inner.calls == provider.calls == 1


def test_materials_are_frozen_and_reference_is_not_in_model_tree(tmp_path):
    sandbox = tmp_path / "sandbox.json"
    sandbox.write_text("{}", encoding="utf-8")
    root = tmp_path / "batch"
    prepare(Path(__file__).parents[1], sandbox, root)
    assert validate(root)["max_real_calls"] == 16
    case = json.loads((root / "material/dataset.json").read_text(encoding="utf-8"))["cases"][0]
    assert "委派" in case["requirement"]
    original = root / "material/initial/replenishment.py"
    assert original.read_text(encoding="utf-8") != REFERENCE
    original.write_text(REFERENCE, encoding="utf-8")
    with pytest.raises(ValueError, match="wc1c-material-drift"):
        validate(root)
