"""Execute an explicitly frozen, never-started verification batch; no proposal/resume."""

import argparse
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from live_unified_evaluation.baseline import connection

from traceh.evaluation.comparison import inspect_experiment
from traceh.evaluation.inputs import digest_bytes, read_input
from traceh.evaluation.model_service import ModelCallConfig
from traceh.evaluation.plan import load_run_options
from traceh.evaluation.runner import EvaluationRunner
from traceh.evaluation.variant_execution import write_json
from traceh.evaluation.variants import source_digest, source_files
from traceh.evolution.strategy import _review_pair, inspect_strategy_optimization


async def verify_heldout(root, *, provider, model):
    root = Path(root).resolve()  # noqa: ASYNC240 - synchronous admission before first await
    frozen = read_input(root, "frozen-smoke.json").data
    plan = read_input(root, frozen["holdout_plan"]["file"])
    if (
        plan.sha256 != frozen["holdout_plan"]["sha256"]
        or read_input(root / "benchmarks/holdout", "dataset.json").sha256
        != frozen["holdout_dataset"]
        or source_digest(source_files()[1]) != frozen["source_digest"]
        or (root / "holdout").exists()
        or provider.name != plan.data["model"]["provider"]
        or model != plan.data["model"]["model"]
    ):
        raise ValueError("ao2-heldout-frozen-input-mismatch")
    patch = root / "development/experiment/rounds/0001/candidate.json"
    development = inspect_strategy_optimization(root / "development")
    if (
        development["stop"] is not None
        or development["evaluation"] is None
        or development["evaluation"]["progress"]["pending_reviews"]
        or development["evaluation"]["progress"]["evidence"] != "passed"
        or development["evaluation"]["progress"]["convergence"] != "converged"
    ):
        raise ValueError("ao2-heldout-development-inconclusive")
    proposal = read_input(root / "development", "proposal.json").data
    if proposal["base_source_digest"] != frozen["source_digest"]:
        raise ValueError("ao2-heldout-candidate-mismatch")
    raw = plan.data
    raw["variants"][1]["source"] = {
        "file": patch.relative_to(root).as_posix(),
        "sha256": digest_bytes(patch.read_bytes()),
    }
    target = root / "holdout-effective-plan.json"
    write_json(target, raw)
    runner = EvaluationRunner(
        root / "benchmarks/holdout",
        root / "holdout",
        provider=provider,
        model_id=model,
        options=load_run_options(target),
        provider_binding={
            "network_mode": "direct",
            "connection_digest": frozen["judge"]["connection_digest"],
            "driver_digest": frozen["driver_digest"],
            "verification_driver_digest": digest_bytes(Path(__file__).read_bytes()),  # noqa: ASYNC240 - admission fingerprint
            "timeout_seconds": 60,
        },
    )
    print(json.dumps({"stage": "held-out-verification", "trials": len(runner.trials)}), flush=True)
    await runner.run()
    await _review_pair(
        root / "holdout",
        root / "holdout-review",
        provider=provider,
        config=ModelCallConfig(**frozen["judge"]),
        deadline=datetime.fromisoformat(frozen["contract"]["deadline_utc"]),
    )
    result = inspect_experiment(
        root / "holdout", assessments=root / "holdout-review/assessments.json"
    )
    write_json(root / "holdout-comparison.json", result)
    print(
        json.dumps(
            {
                "stage": "holdout-complete",
                "changes": result.get("changes"),
                "cost_delta": result.get("cost_delta"),
                "thresholds": result.get("thresholds"),
            }
        ),
        flush=True,
    )
    return result


async def execute(options):
    args, provider, model = connection(options.profile)
    root = options.experiment.resolve()
    frozen = read_input(root, "frozen-smoke.json").data
    if digest_bytes(args.base_url.encode()) != frozen["judge"]["connection_digest"]:
        raise ValueError("ao2-heldout-connection-mismatch")
    previous = os.environ.get(args.api_key_env)
    if args.tui_api_key:
        os.environ[args.api_key_env] = args.tui_api_key
    try:
        await verify_heldout(root, provider=provider, model=model)
    finally:
        if previous is None:
            os.environ.pop(args.api_key_env, None)
        else:
            os.environ[args.api_key_env] = previous


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--experiment", type=Path, required=True)
    asyncio.run(execute(parser.parse_args()))
