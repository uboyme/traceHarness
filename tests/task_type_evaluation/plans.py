"""Frozen run plans for experiments A and B of the collection plan (no model calls).

A: one ``execution_strategy`` single/multi pair per case and repetition, so a
provider failure stops only that pair (plan S0-C). The first arm alternates
between repetitions. B: two ungoverned multi arms per selected case as one paired
plan on the ungoverned benchmark; B's cases are chosen from A's results by the
pre-registered rule and passed explicitly, never inferred here. Every plan is
paired so each arm runs in a private worker that connects directly, never via a
proxy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

from task_type_evaluation.spec import CASES

MODEL = {
    "provider": "openai-compatible",
    "model": "qwen3.6-plus",
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "api_key_env": "DASHSCOPE_API_KEY",
    "script": None,
    "retry_policy": {
        "max_attempts": 1,
        "max_elapsed_seconds": 0,
        "base_delay_seconds": 0,
        "max_delay_seconds": 0,
        "retry_after_cap_seconds": 0,
        "jitter_ratio": 0,
    },
    # One request's wait. qwen3.6-plus writes about 18s per 1,000 output tokens,
    # so an 8,192-token answer needs about 148s; 120s cut such answers off and
    # was misread as a hung provider (ADR-0086). 300s covers twice that.
    "timeout_seconds": 300,
}
REPETITIONS = 2


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write(directory, plan, sandbox):
    directory.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(sandbox, directory / "sandbox.json")
    (directory / "run-plan.json").write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    path = directory / "run-plan.json"
    return {"plan": str(path), "sha256": _digest(path)}


def experiment_a(benchmark, sandbox, output):
    digest = _digest(Path(benchmark) / "benchmark.json")
    plans = []
    for case in CASES:
        for repetition in range(1, REPETITIONS + 1):
            plan = {
                "format": 1,
                "benchmark_digest": digest,
                "variants": [
                    {"variant_id": "single", "role": "baseline", "source": "current"},
                    {"variant_id": "multi", "role": "candidate", "source": "current"},
                ],
                "model": MODEL,
                "execution": {
                    "sandbox_config": "sandbox.json",
                    "max_trials": 2,
                    "timeout_seconds": 4200,
                    "shutdown_seconds": 180,
                    "network_mode": "direct",
                    "first_arm": "baseline" if repetition % 2 else "candidate",
                },
                "trials": {
                    "repetitions": 1,
                    "selection": {"case_ids": [case["case_id"]], "material_seeds": None},
                },
                "comparison": {
                    "format": 3,
                    "kind": "execution_strategy",
                    "requested_modes": ["single", "multi"],
                    "min_pass_gain": None,
                    "max_token_ratio": None,
                    "max_tool_call_delta": None,
                },
            }
            entry = _write(Path(output) / f"{case['case_id']}-r{repetition}", plan, sandbox)
            plans.append({"case_id": case["case_id"], "repetition": repetition, **entry})
    return {"benchmark_digest": digest, "plans": plans}


def experiment_b(benchmark, sandbox, output, case_ids):
    """Two ungoverned multi runs per case as one paired plan.

    A paired plan runs each arm in a private worker with proxies disabled (the
    Provider connects directly); an unpaired plan would run in this process and
    could inherit a system proxy. The two identical arms are B's two repetitions.
    """
    digest = _digest(Path(benchmark) / "benchmark.json")
    known = {case["case_id"] for case in CASES}
    if not case_ids or not set(case_ids) <= known or len(case_ids) > 2:
        raise ValueError("experiment B takes one or two explicit known cases")
    plans = []
    for case_id in case_ids:
        plan = {
            "format": 1,
            "benchmark_digest": digest,
            "variants": [
                {"variant_id": "ungoverned-1", "role": "baseline", "source": "current"},
                {"variant_id": "ungoverned-2", "role": "candidate", "source": "current"},
            ],
            "model": MODEL,
            "execution": {
                "sandbox_config": "sandbox.json",
                "max_trials": 2,
                "timeout_seconds": 4200,
                "shutdown_seconds": 180,
                "network_mode": "direct",
                "first_arm": "baseline",
            },
            "trials": {
                "repetitions": 1,
                "selection": {"case_ids": [case_id], "material_seeds": None},
            },
            "comparison": {
                "format": 3,
                "kind": "execution_strategy",
                "requested_modes": ["multi", "multi"],
                "min_pass_gain": None,
                "max_token_ratio": None,
                "max_tool_call_delta": None,
            },
        }
        plans.append({"case_id": case_id, **_write(Path(output) / case_id, plan, sandbox)})
    return {"benchmark_digest": digest, "plans": plans}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment", choices=("a", "b"))
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--sandbox", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", action="append", default=[])
    options = parser.parse_args()
    if options.experiment == "a":
        result = experiment_a(options.benchmark, options.sandbox, options.output)
    else:
        result = experiment_b(options.benchmark, options.sandbox, options.output, options.case)
    print(json.dumps(result, indent=2))
