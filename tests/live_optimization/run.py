"""Opt-in AO-2 real smoke: original proposal/Runner/review, direct existing profile."""

import argparse
import asyncio
import json
import os
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from live_optimization.prepare import prepare
from live_optimization.verify import verify_heldout
from live_unified_evaluation.baseline import connection

from traceh.api.optimization import DevelopmentObservation
from traceh.evaluation.inputs import digest_bytes, read_input
from traceh.evaluation.model_service import ModelCallConfig
from traceh.evaluation.plan import load_run_options
from traceh.evaluation.runner import EvaluationRunner
from traceh.evaluation.variant_execution import write_json
from traceh.evaluation.variants import source_digest, source_files
from traceh.evolution.optimization_contract import (
    OptimizationContract,
    OptimizationLimits,
    editable_text,
)
from traceh.evolution.strategy import run_strategy_optimization


async def execute(options):
    root = options.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    suites = prepare(options.benchmark, root / "benchmarks")
    args, provider, model = connection(options.profile)
    original_key = os.environ.get(args.api_key_env)
    # The original credential loader supplies the authorized value; only child process
    # environment receives it. No credentials are serialized into plans or reports.
    if args.tui_api_key:
        os.environ[args.api_key_env] = args.tui_api_key
    try:
        connection_id = digest_bytes(args.base_url.encode())
        analysis = ModelCallConfig(
            provider.name, model, 0.0, "cl100k_base", 32000, 6000, 1024, 90, connection_id
        )
        judge = ModelCallConfig(
            provider.name, model, 0.0, "cl100k_base", 64000, 2048, 2048, 90, connection_id
        )
        driver_digest = digest_bytes(await asyncio.to_thread(Path(__file__).read_bytes))
        plans = {}
        for name in ("development", "holdout"):
            suite = suites / name
            count = len(read_input(suite, "dataset.json").data["cases"])
            raw = json.loads(
                (options.benchmark / "run-plan.example.json").read_text(encoding="utf-8")
            )
            raw.update(benchmark_digest=read_input(suite, "benchmark.json").sha256)
            raw["variants"] = [
                {"variant_id": "reference", "role": "baseline", "source": "current"},
                {"variant_id": "experiment", "role": "candidate", "source": "current"},
            ]
            raw["model"].update(
                provider=provider.name,
                model=model,
                base_url=args.base_url,
                api_key_env=args.api_key_env,
                script=None,
            )
            raw["execution"] = {
                "sandbox_config": None,
                "max_trials": count * 2,
                "timeout_seconds": 1800,
                "shutdown_seconds": 120,
                "first_arm": "baseline",
                "network_mode": "direct",
            }
            raw["trials"] = {"repetitions": 1}
            raw["comparison"] = {
                "kind": "text_candidate", "requested_modes": None,
                "format": 2,
                "min_pass_gain": 1,
                "max_token_ratio": 1.15,
                "max_tool_call_delta": 2,
            }
            path = root / (name + "-plan.json")
            write_json(path, raw)
            plans[name] = path

        def runner(name, plan, output):
            return EvaluationRunner(
                suites / name,
                output,
                provider=provider,
                model_id=model,
                options=load_run_options(plan),
                provider_binding={
                    "network_mode": "direct",
                    "connection_digest": connection_id,
                    "driver_digest": driver_digest,
                    "timeout_seconds": 60,
                },
            )

        template = runner("development", plans["development"], root / "unused")
        files = source_files()[1]
        observations = tuple(
            DevelopmentObservation(
                case,
                failure,
                summary,
                ("docs/validation-data/unified-evaluation/ue4/cases.md",),
            )
            for case, failure, summary in (
                (
                    "s-english",
                    "candidate-without-evidence",
                    "UE-4 case s-english material 419 found the manual, received a directory "
                    "read denial, then only said it would search again; no follow-up call or "
                    "answer evidence arrived. Improve recovery without repeating rejected calls.",
                ),
                (
                    "s-resource",
                    "regression-control",
                    "UE-4 resource reads succeeded. Preserve this flow; do not impose redundant "
                    "reads or fetch related but unrequested sections during the repair.",
                ),
                (
                    "s-absent",
                    "negative-scope",
                    "UE-4 case s-absent material 113 declared global absence after two metadata "
                    "searches and inferred absence from the manual topic. Keep negative claims "
                    "within proven search scope; do not turn ordinary conversation into a search.",
                ),
            )
        )
        contract = OptimizationContract(
            "ao2-real-one-proposal",
            source_digest(files),
            template.manifest.document.sha256,
            template.options.document.sha256,
            template.manifest.dataset.sha256,
            tuple(dict.fromkeys(t.case_id for t in template.trials)),
            editable_text(
                files,
                (
                    ("runtime/prompt.py", "_REFERENCE_GUIDANCE"),
                    ("tools/reference_search.py", "SkillSearchTool.description"),
                ),
            ),
            OptimizationLimits(
                1, 1, len(template.trials), 1, 1, 1, 1, analysis.token_limit, 200000
            ),
            datetime.now(UTC) + timedelta(hours=1),
        )
        write_json(
            root / "frozen-smoke.json",
            {
                "format": 1,
                "source_digest": contract.base_source_digest,
                "development_plan": read_input(root, plans["development"].name).reference(),
                "holdout_plan": read_input(root, plans["holdout"].name).reference(),
                "development_dataset": read_input(suites / "development", "dataset.json").sha256,
                "holdout_dataset": read_input(suites / "holdout", "dataset.json").sha256,
                "analysis": asdict(analysis),
                "judge": asdict(judge),
                "contract": contract.to_dict(),
                "holdout_role": "new groups withheld from proposal; one later verification",
                "maximum_analysis_calls": 1,
                "maximum_trial_runs": 18,
                "maximum_judge_calls": 18,
                "promotion": "human-only",
                "driver_digest": driver_digest,
            },
        )
        print(
            json.dumps(
                {
                    "stage": "proposal-and-development",
                    "provider": provider.name,
                    "model": model,
                    "network": "direct",
                    "output": str(root),
                }
            ),
            flush=True,
        )
        result = await run_strategy_optimization(
            template,
            contract,
            observations=observations,
            provider=provider,
            analysis_config=analysis,
            judge_provider=provider,
            judge_config=judge,
            output_dir=root / "development",
        )
        print(
            json.dumps(
                {
                    "stage": "development-complete",
                    "action": result["action"],
                    "reason": result["reason"],
                    "cost": result["cost"],
                }
            ),
            flush=True,
        )
        patch = root / "development/experiment/rounds/0001/candidate.json"
        if patch.exists() and result["stop"] is None:
            await verify_heldout(root, provider=provider, model=model)
    finally:
        if original_key is None:
            os.environ.pop(args.api_key_env, None)
        else:
            os.environ[args.api_key_env] = original_key


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(execute(parser.parse_args()))
