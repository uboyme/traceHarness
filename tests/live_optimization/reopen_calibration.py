"""Recompute calibration observations and replay original control-model sessions."""

import argparse
import asyncio
import json
from pathlib import Path

from live_optimization.calibrate import summarize
from live_optimization.reopen import replay

from traceh.api.json_types import fingerprint
from traceh.evaluation.inputs import read_input
from traceh.evaluation.model_evidence import load_model_call
from traceh.evaluation.model_review_protocol import call_judgment
from traceh.evaluation.variant_execution import write_json


def observations(root):
    plan = read_input(root, "plan.json").data
    by_id = {s["sample_id"]: s for s in plan["samples"]}
    seen, rows = set(), []
    for path in sorted((root / "calls").glob("*/call.json")):
        definition, receipt, digest = load_model_call(path.parent)
        binding = definition["binding"]
        sample, condition, repeat = binding["sample_id"], binding["condition"], binding["replicate"]
        identity = (sample, condition, repeat)
        assert sample in by_id and condition in {"baseline", "revised"} and repeat in {1, 2}
        assert identity not in seen
        seen.add(identity)
        expected = by_id[sample][condition]
        assert (
            definition["input"] == expected["input"] and definition["system"] == expected["system"]
        )
        assert definition["config"] == plan["config"]
        assert binding == {
            "purpose": "judge-calibration",
            "plan_digest": fingerprint(plan),
            "sample_id": sample,
            "condition": condition,
            "replicate": repeat,
            "input_digest": fingerprint(expected["input"]),
        }
        rows.append(
            {
                "sample_id": sample,
                "condition": condition,
                "replicate": repeat,
                "call": path.parent.relative_to(root).as_posix(),
                "receipt_sha256": digest,
                "judgment": call_judgment(receipt),
                "tokens": receipt["observation"]["usage"]["total_tokens"],
            }
        )
    # The semantic data of both conditions must be identical; only scope explanation
    # and the explicit review-task envelope differ. Expectations never enter this loop.
    for sample in plan["samples"]:
        baseline = json.loads(sample["baseline"]["input"])
        revised_input = sample["revised"]["input"]
        revised = json.loads(
            revised_input if revised_input.startswith("{") else revised_input.splitlines()[1]
        )
        del revised["evidence_scope"]
        assert revised == baseline
    observed = summarize(plan, rows)
    original = read_input(root, "result.json").data
    assert all(original[k] == v for k, v in observed.items())
    return observed


async def execute(options):
    output = options.output.resolve()
    roots = [p.resolve() for p in options.run]
    if any(output.is_relative_to(p) or p.is_relative_to(output) for p in roots):
        raise ValueError("calibration-reopen-overlap")
    output.mkdir(parents=True, exist_ok=False)
    results = []
    for root in roots:
        result = {"run": str(root), "calibration": observations(root), "replay": await replay(root)}
        assert result["replay"]["recorded_model_attempts"] <= 65
        results.append(result)
    summary = {
        "new_provider_calls": 0,
        "runs": results,
        "recorded_model_attempts": sum(r["replay"]["recorded_model_attempts"] for r in results),
        "requests": sum(r["replay"]["requests"] for r in results),
        "tokens": sum(r["replay"]["total_tokens"] for r in results),
    }
    assert summary["recorded_model_attempts"] <= 74
    write_json(output / "independent-replay.json", summary)
    print(json.dumps({k: v for k, v in summary.items() if k != "runs"}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(execute(parser.parse_args()))
