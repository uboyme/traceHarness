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


@pytest.mark.frozen_unicode
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


@pytest.mark.frozen_unicode
async def test_no_candidate_stops_without_trials(tmp_path):
    result = await execute(tmp_path, text='{"kind":"no_candidate","reason":"No supported change."}')
    assert result["reason"] == "no-candidate" and result["evaluation"] is None
    assert result["cost"]["analysis"]["attempts"] == 1
    assert not (tmp_path / "strategy/experiment").exists()


@pytest.mark.frozen_unicode
async def test_outside_scope_edit_is_rejected_without_trials(tmp_path):
    result = await execute(
        tmp_path, mutate=lambda p: p["edits"][0].update(file="evaluation/review.py")
    )
    assert result["stop"]["errors"] and result["evaluation"] is None
    assert result["cost"]["analysis"]["attempts"] == 1
    assert not (tmp_path / "strategy/experiment").exists()


@pytest.mark.frozen_unicode
async def test_real_observed_array_tradeoff_is_rejected_without_coercion(tmp_path):
    result = await execute(tmp_path, mutate=lambda p: p.update(expected_tradeoffs=["more tokens"]))
    assert result["stop"]["errors"] == ["optimization-proposal-invalid"]
    assert result["evaluation"] is None
    assert result["cost"]["analysis"]["attempts"] == 1


@pytest.mark.frozen_unicode
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


@pytest.mark.frozen_unicode
async def test_generated_candidate_cannot_be_replaced_by_edited_receipt(tmp_path):
    result = await execute(tmp_path)
    assert result["evaluation"] is not None
    path = tmp_path / "strategy/proposal.json"
    data = json.loads(path.read_text())
    data["rationale"] = "Different candidate rationale."
    path.write_text(json.dumps(data))
    with pytest.raises(OptimizationContractError):
        inspect_strategy_optimization(tmp_path / "strategy")


@pytest.mark.frozen_unicode
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


@pytest.mark.frozen_unicode
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


async def suggest(root, *, text=None, mutate=None, seen=()):
    from traceh.evolution.strategy import propose_strategy

    runner, contract, observations, response = setup(root)
    if mutate:
        mutate(response)
    return await propose_strategy(
        runner,
        contract,
        observations=observations,
        provider=responder(json.dumps(response) if text is None else text),
        analysis_config=config(),
        output_dir=root / "suggestion",
        seen_candidate_digests=seen,
    )


@pytest.mark.frozen_unicode
async def test_a_suggestion_is_one_analysis_and_a_ready_patch_with_no_trials(tmp_path):
    from traceh.evaluation.variants import apply_candidate, source_files

    result = await suggest(tmp_path)
    root = tmp_path / "suggestion"
    assert (result["mode"], result["action"], result["reason"]) == (
        "proposal-only",
        "review_candidate",
        "suggestion-ready",
    )
    assert result["cost"]["analysis"]["attempts"] == 1 and result["evaluation"] is None
    assert not (root / "experiment").exists() and result["adoption_authorized"] is False
    # The file is the exact AO patch a text_candidate run plan references as its source.
    patch = json.loads((root / "candidate.json").read_text(encoding="utf-8"))
    apply_candidate(source_files()[1], patch)
    assert inspect_strategy_optimization(root) == result


@pytest.mark.frozen_unicode
async def test_a_suggestion_cannot_be_swapped_after_the_model_answered(tmp_path):
    await suggest(tmp_path)
    path = tmp_path / "suggestion/candidate.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["edits"][0]["new_text"] = "A text the model never proposed."
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(OptimizationContractError, match="optimization-candidate-drift"):
        inspect_strategy_optimization(tmp_path / "suggestion")


@pytest.mark.frozen_unicode
async def test_no_candidate_and_invalid_answers_leave_no_patch(tmp_path):
    none = await suggest(
        tmp_path / "none", text='{"kind":"no_candidate","reason":"No supported change."}'
    )
    assert (none["action"], none["reason"], none["candidate"]) == ("stop", "no-candidate", None)
    bad = await suggest(tmp_path / "bad", mutate=lambda p: p.update(expected_tradeoffs=["x"]))
    assert bad["stop"]["errors"] == ["optimization-proposal-invalid"]
    for name in ("none", "bad"):
        assert not (tmp_path / name / "suggestion/candidate.json").exists()


@pytest.mark.frozen_unicode
async def test_a_suggestion_already_seen_is_refused_as_a_duplicate(tmp_path):
    first = await suggest(tmp_path / "first")
    again = await suggest(tmp_path / "again", seen=(first["candidate"]["digest"],))
    assert again["stop"]["errors"] == ["optimization-duplicate-candidate"]
    assert again["candidate"] is None
