"""Opt-in semantic review. Execution evidence and hard gates stay with evaluation."""

import asyncio
from dataclasses import asdict
from datetime import UTC, datetime

from traceh.api.json_types import fingerprint
from traceh.concurrency import combine_failures
from traceh.evaluation.inputs import read_input
from traceh.evaluation.model_evidence import load_model_call
from traceh.evaluation.model_review_protocol import (
    REVIEW_SYSTEM,
    call_judgment,
    hard_rejection,
    review_input,
)
from traceh.evaluation.model_service import run_model_call
from traceh.evaluation.review import _destination, _episode_events, _run, assess_run
from traceh.evaluation.variant_execution import write_json


async def review_with_model(root, output, *, provider, config, max_calls, max_tokens, deadline_utc):
    """Freeze all review conditions before calling; return a fresh original assessment."""
    root, output = _destination(root, output)
    report, binding, rubric = _run(root)
    packets = {p["trial_id"]: p for p in report["task_report"]["episodes"]}
    eligible = [
        t for t in report["trials"] if t["measured"] and t["execution"]["status"] == "completed"
    ]
    calls_needed = sum(not hard_rejection(t, packets[t["identity"]["trial_id"]]) for t in eligible)
    if (
        type(max_calls) is not int
        or max_calls < calls_needed
        or type(max_tokens) is not int
        or max_tokens < calls_needed * config.token_limit
        or deadline_utc.tzinfo is not UTC
        or deadline_utc <= datetime.now(UTC)
    ):
        raise ValueError("evaluation-model-review-budget-insufficient")
    output.mkdir(parents=True, exist_ok=False)
    write_json(
        output / "review-contract.json",
        {
            "format": 1,
            "binding": binding,
            "config": asdict(config),
            "max_calls": max_calls,
            "max_tokens": max_tokens,
            "deadline_utc": deadline_utc.isoformat(),
            "reserved_calls": calls_needed,
            "rubric": rubric,
            "system": REVIEW_SYSTEM,
        },
    )
    judgments, calls = [], []
    stop = None
    primary = None
    for index, trial in enumerate(eligible, 1):
        trial_id = trial["identity"]["trial_id"]
        packet = packets[trial_id]
        rejection = hard_rejection(trial, packet)
        if rejection:
            value = {"status": "failed", "reason": rejection}
        else:
            if datetime.now(UTC) >= deadline_utc:
                stop = "deadline"
                break
            _, events = _episode_events(root, trial, packet)
            text = review_input(rubric, packet, events)
            directory = output / "calls" / f"{index:04d}"
            try:
                async with asyncio.timeout((deadline_utc - datetime.now(UTC)).total_seconds()):
                    await run_model_call(
                        provider=provider,
                        config=config,
                        system=REVIEW_SYSTEM,
                        input_text=text,
                        binding={
                            "purpose": "semantic-review",
                            "evaluation": binding,
                            "trial_id": trial_id,
                            "input_digest": fingerprint(text),
                        },
                        output_dir=directory,
                    )
            except BaseException as error:
                primary = error
            try:
                _, receipt, digest = load_model_call(directory)
            except BaseException as error:
                raise combine_failures(
                    primary, error, "judge call and evidence read failed"
                ) from None
            calls.append({"trial_id": trial_id, "directory": str(directory), "sha256": digest})
            value = call_judgment(receipt)
        judgments.append({"trial_id": trial_id, **value})
        if primary is not None or value["status"] == "pending_review":
            stop = "review-inconclusive"
            break
    judgment = {
        "format": 2,
        "binding": binding,
        "reviewer": "model:" + config.provider + "/" + config.model,
        "origin": {"kind": "model", "config": asdict(config), "calls": calls},
        "supersedes": None,
        "judgments": judgments,
    }
    try:
        write_json(output / "judgment.json", judgment)
        totals = [
            load_model_call(c["directory"], digest=c["sha256"])[1]["observation"]["usage"][
                "total_tokens"
            ]
            for c in calls
        ]
        write_json(
            output / "cost.json",
            {
                "calls": len(calls),
                "tokens": sum(totals) if all(t is not None for t in totals) else None,
                "stop": stop,
            },
        )
        result = assess_run(root, output / "judgment.json", output / "assessment")
    except BaseException as error:
        raise combine_failures(primary, error, "judge and assessment publication failed") from None
    if primary is not None and (
        not isinstance(primary, Exception) or isinstance(primary, BaseExceptionGroup)
    ):
        raise primary
    return {
        **result,
        "assessment": str(output / "assessment/assessment.json"),
        "cost": read_input(output, "cost.json").data,
    }
