from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace

import pytest
from live_dynamic_collaboration.complementary_work import (
    CONDITIONS,
    DESCRIPTIONS,
    prepare,
    run,
    transform,
)
from live_dynamic_collaboration.phase_diagnosis import assess
from test_independent_decision import prepared
from test_phase_diagnosis import local_call, request

from traceh.api.llm import ModelResponse, Usage, UsageQuality


@pytest.mark.parametrize("condition", CONDITIONS)
def test_only_three_descriptions_change_without_mutating_input(condition):
    original = request()
    before = copy.deepcopy(original.to_dict())
    changed = transform(original, condition)
    assert original.to_dict() == before
    actual = changed.to_dict()
    if condition == "described-fields":
        for key, description in DESCRIPTIONS.items():
            assert (
                actual["tools"][0]["input_schema"]["properties"][key].pop("description")
                == description
            )
    assert actual == before


def test_existing_valid_local_contract_still_passes():
    for condition in CONDITIONS:
        result = assess(transform(request(), condition), ModelResponse(tool_calls=(local_call(),)))
        assert result["decision_valid"] and not result["tool_executed"]


@pytest.mark.parametrize("fault", ["tool", "missing", "already-described"])
def test_reject_wrong_or_non_original_surface(fault):
    value = request()
    if fault == "tool":
        value = replace(value, tools=())
    elif fault == "missing":
        del value.tools[0].input_schema["properties"]["main_goal"]
    else:
        value.tools[0].input_schema["properties"]["main_goal"]["description"] = "Existing text"
    with pytest.raises(ValueError):
        transform(value, "described-fields")


def test_changed_prior_request_is_rejected(tmp_path):
    old = prepared(tmp_path)
    path = old / "plan.json"
    plan = json.loads(path.read_text(encoding="utf-8"))
    plan["rows"][2]["request"]["messages"][0]["content"] = "Changed evidence"
    path.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(ValueError, match="prior-request-changed"):
        prepare(path, tmp_path / "next")
    assert not (tmp_path / "next").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled"])
async def test_six_calls_or_failure_stop_and_no_rerun(tmp_path, monkeypatch, outcome):
    old = prepared(tmp_path)
    out = tmp_path / "next"
    prepare(old / "plan.json", out)
    calls = 0

    class Provider:
        name = "openai-compatible"

        async def complete(self, value):
            nonlocal calls
            calls += 1
            if outcome == "failure":
                raise RuntimeError("failed")
            if outcome == "cancelled":
                raise asyncio.CancelledError()
            return ModelResponse(tool_calls=(local_call(),), usage=Usage(2, 1, UsageQuality.EXACT))

    monkeypatch.setattr(
        "live_dynamic_collaboration.complementary_work.connection",
        lambda p: (None, Provider(), "test-model"),
    )
    with pytest.raises(RuntimeError, match="historical-collaboration-contract"):
        await run(out, tmp_path / "unused")
    assert calls == 0
    before = calls
    with pytest.raises(FileExistsError):
        await run(out, tmp_path / "unused")
    assert calls == before
