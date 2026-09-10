"""Real plugin proposal generation enters AO-1 isolated workers and shared review."""

import asyncio
import json
from dataclasses import replace

import pytest
from test_control_model_calls import GatedProvider, config, responder
from test_manual_optimization import configured

from traceh.api.llm import ModelResponse, Usage, UsageQuality
from traceh.evaluation.model_evidence import load_model_call
from traceh.evolution.optimization_contract import OptimizationContractError
from traceh.evolution.strategy import inspect_strategy_optimization, run_strategy_optimization
from traceh.llm.scripted import ScriptedLlmProvider


def setup(root):
    runner, contract, draft, observations = configured(root)
    contract = replace(contract, limits=replace(contract.limits, max_analysis_tokens=16000))
    node = contract.editable_text[0]
    response = {
        "kind": "candidate",
        "rationale": draft.rationale,
        "targeted_failure_classes": list(draft.targeted_failure_classes),
        "edits": [
            {"file": node.file, "selector": node.selector, "new_text": draft.edits[0].new_text}
        ],
        "expected_tradeoffs": draft.expected_tradeoffs,
    }
    return runner, contract, observations, response


async def execute(root, *, text=None, with_judge=False, mutate=None):
    runner, contract, observations, response = setup(root)
    if mutate:
        mutate(response)
    judge = ScriptedLlmProvider(
        [
            ModelResponse(
                content='{"status":"passed","reason":"Scoped fixture answer is supported."}',
                usage=Usage(20, 10, UsageQuality.EXACT),
            )
        ],
        repeat_last=True,
    )
    return await run_strategy_optimization(
        runner,
        contract,
        observations=observations,
        provider=responder(json.dumps(response) if text is None else text),
        analysis_config=config(),
        output_dir=root / "strategy",
        judge_provider=judge if with_judge else None,
        judge_config=config() if with_judge else None,
    )


async def test_real_generation_lease_proposal_enters_original_workers(tmp_path):
    result = await execute(tmp_path, with_judge=True)
    assert result["stop"] is None, result
    assert result["evaluation"]["progress"]["trials_started"] == 2
    assert result["cost"]["analysis"]["attempts"] == 1
    assert result["cost"]["review_calls"] == 2
    assert result["cost"]["review_tokens"] == 60
    assert result["adoption_authorized"] is False
    reopened = inspect_strategy_optimization(tmp_path / "strategy")
    assert reopened == result
    definition, _, _ = load_model_call(tmp_path / "strategy/analysis")
    assert "rubric" not in json.loads(definition["input"])
    assert "expectation" not in json.loads(definition["input"])


async def test_no_candidate_stops_without_trials(tmp_path):
    result = await execute(tmp_path, text='{"kind":"no_candidate","reason":"No supported change."}')
    assert result["reason"] == "no-candidate" and result["evaluation"] is None
    assert result["cost"]["analysis"]["attempts"] == 1
    assert not (tmp_path / "strategy/experiment").exists()


async def test_outside_scope_edit_is_rejected_without_trials(tmp_path):
    result = await execute(
        tmp_path, mutate=lambda p: p["edits"][0].update(file="evaluation/review.py")
    )
    assert result["stop"]["errors"] and result["evaluation"] is None
    assert result["cost"]["analysis"]["attempts"] == 1
    assert not (tmp_path / "strategy/experiment").exists()


async def test_real_observed_array_tradeoff_is_rejected_without_coercion(tmp_path):
    result = await execute(tmp_path, mutate=lambda p: p.update(expected_tradeoffs=["more tokens"]))
    assert result["stop"]["errors"] == ["optimization-proposal-invalid"]
    assert result["evaluation"] is None
    assert result["cost"]["analysis"]["attempts"] == 1


async def test_whole_trial_budget_is_reserved_before_analysis(tmp_path):
    runner, contract, observations, _ = setup(tmp_path)
    contract = replace(contract, limits=replace(contract.limits, max_trials=1))
    provider = responder()
    with pytest.raises(OptimizationContractError):
        await run_strategy_optimization(
            runner,
            contract,
            observations=observations,
            provider=provider,
            analysis_config=config(),
            output_dir=tmp_path / "strategy",
        )
    assert not provider.requests and not (tmp_path / "strategy").exists()


async def test_generated_candidate_cannot_be_replaced_by_edited_receipt(tmp_path):
    result = await execute(tmp_path)
    assert result["evaluation"] is not None
    path = tmp_path / "strategy/proposal.json"
    data = json.loads(path.read_text())
    data["rationale"] = "Different candidate rationale."
    path.write_text(json.dumps(data))
    with pytest.raises(OptimizationContractError):
        inspect_strategy_optimization(tmp_path / "strategy")


async def test_unknown_judge_usage_stops_before_other_arm(tmp_path):
    runner, contract, observations, response = setup(tmp_path)
    judge = responder(usage=Usage(0, 0, UsageQuality.UNKNOWN))
    result = await run_strategy_optimization(
        runner,
        contract,
        observations=observations,
        provider=responder(json.dumps(response)),
        analysis_config=config(),
        judge_provider=judge,
        judge_config=config(),
        output_dir=tmp_path / "strategy",
    )
    assert result["stop"] is None
    assert result["cost"]["review_calls"] == 1
    assert result["cost"]["review_tokens"] is None
    assert len(judge.requests) == 1
    assert result["evaluation"]["progress"]["pending_reviews"] > 0
    assert not (tmp_path / "strategy/review/02").exists()


async def test_strategy_cancel_converges_actual_analysis_without_starting_trials(tmp_path):
    runner, contract, observations, _ = setup(tmp_path)
    provider = GatedProvider()
    task = asyncio.create_task(
        run_strategy_optimization(
            runner,
            contract,
            observations=observations,
            provider=provider,
            analysis_config=config(),
            output_dir=tmp_path / "strategy",
        )
    )
    await asyncio.wait_for(provider.entered.wait(), 5)
    task.cancel()
    await asyncio.wait_for(provider.closing.wait(), 5)
    task.cancel()
    assert not task.done() and not provider.finished
    provider.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.finished
    result = inspect_strategy_optimization(tmp_path / "strategy")
    assert result["stop"]["errors"] == ["CancelledError"]
    assert result["evaluation"] is None
    assert not (tmp_path / "strategy/experiment").exists()
