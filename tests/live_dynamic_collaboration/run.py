"""Explicit DA batches using original paired Evaluation, review and replay owners."""

import argparse
import asyncio
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from live_optimization.reopen import replay
from live_unified_evaluation.baseline import connection

from traceh.api.json_types import fingerprint
from traceh.evaluation.comparison import compare_experiment
from traceh.evaluation.inputs import read_input
from traceh.evaluation.manifest import load_benchmark_manifest
from traceh.evaluation.model_review import review_with_model
from traceh.evaluation.model_service import ModelCallConfig
from traceh.evaluation.plan import RETRY_FIELDS, load_run_options
from traceh.evaluation.runner import EvaluationRunner
from traceh.evaluation.variant_execution import write_json
from traceh.evaluation.variants import source_digest, source_files
from traceh.llm.retry import NO_MODEL_RETRY
from traceh.sandbox.config import load_sandbox_file

AA = ("current-question-order", "worker-receipt-identity")
MECHANISM = (
    "bounded-identity",
    "current-question-order",
    "memory-scope-and-state",
    "worker-receipt-identity",
    "cancel-before-worker-start",
    "unknown-usage-qualification",
)


def plan(manifest, model_settings, sandbox_file, cases, modes, first_arm):
    return {
        "format": 1,
        "benchmark_digest": manifest.document.sha256,
        "variants": [
            {"variant_id": "reference", "role": "baseline", "source": "current"},
            {"variant_id": "experiment", "role": "candidate", "source": "current"},
        ],
        "model": {
            **model_settings,
            "script": None,
            "retry_policy": {name: getattr(NO_MODEL_RETRY, name) for name in RETRY_FIELDS},
            "timeout_seconds": 90,
        },
        "execution": {
            "sandbox_config": str(sandbox_file),
            "max_trials": len(cases) * 2,
            "timeout_seconds": 14400,
            "shutdown_seconds": 120,
            "first_arm": first_arm,
            "network_mode": "direct",
        },
        "trials": {
            "repetitions": 1,
            "selection": {"case_ids": list(cases), "material_seeds": None},
        },
        "comparison": {
            "format": 2,
            "kind": "execution_strategy",
            "requested_modes": modes,
            "min_pass_gain": 1,
            "max_token_ratio": 1.50,
            "max_tool_call_delta": 160,
        },
    }


async def execute(options):
    root, benchmark = options.output.resolve(), options.benchmark.resolve()
    root.mkdir(parents=True, exist_ok=False)
    manifest = load_benchmark_manifest(benchmark)
    if manifest.task_type.value != "product_task":
        raise ValueError("product-benchmark-required")
    # The host's private credential loader is reused. No credential enters any
    # plan, command line, driver archive, result or provider-binding fingerprint.
    raise RuntimeError(
        "historical-collaboration-contract: use frozen source; current multi requires WC-1F"
    )
    args, provider, model = connection(options.profile.resolve())
    if provider.name != "openai-compatible" or model != "qwen-plus":
        raise ValueError("explicit-da-model-contract-mismatch")
    provider.timeout_seconds = 90
    settings = dict(
        provider=provider.name, model=model, base_url=args.base_url, api_key_env=args.api_key_env
    )
    sandbox = load_sandbox_file(options.sandbox.resolve())
    if sandbox.plugin_grants or sandbox.policy.network != "none":
        raise ValueError("da-sandbox-scope-invalid")
    sandbox_file = root / "sandbox.json"
    sandbox_file.write_bytes(options.sandbox.resolve().read_bytes())
    cases = (
        AA
        if options.stage == "aa"
        else MECHANISM
        if options.stage == "mechanism"
        else tuple(case["case_id"] for case in manifest.dataset.data["cases"])
    )
    if options.stage == "development" and len(cases) != 12:
        raise ValueError("development-case-count-mismatch")
    repeats = 1 if options.stage == "mechanism" else 2
    modes = ["single", "single"] if options.stage == "aa" else ["single", "adaptive"]
    judge = ModelCallConfig(
        provider.name, model, 0.0, "cl100k_base", 64000, 2048, 2048, 90, fingerprint(args.base_url)
    )
    digest = source_digest(source_files()[1])
    driver_text = await asyncio.to_thread(Path(__file__).read_text, encoding="utf-8")
    frozen = dict(
        format=1,
        stage=options.stage,
        source_digest=digest,
        benchmark_digest=manifest.document.sha256,
        dataset_digest=manifest.dataset.sha256,
        case_ids=cases,
        repeats=repeats,
        arm_modes=modes,
        order=["baseline", "candidate"][:repeats],
        max_task_trials=len(cases) * 2 * repeats,
        max_judge_calls=len(cases) * 2 * repeats,
        judge=asdict(judge),
        max_task_tokens=600000 * len(cases) * 2 * repeats,
        max_judge_tokens=64000 * len(cases) * 2 * repeats,
        model=settings,
        provider_timeout_seconds=90,
        network="direct",
        retries=1,
        sandbox_digest=read_input(root, "sandbox.json").sha256,
        driver_digest=fingerprint(driver_text),
        adoption_authorized=False,
        interpretation="Descriptive paired experiment; model judgments are not human gold.",
    )
    write_json(root / "batch-contract.json", frozen)
    for repeat in range(1, repeats + 1):
        first = "baseline" if repeat == 1 else "candidate"
        plan_file = root / f"plan-{repeat}.json"
        write_json(plan_file, plan(manifest, settings, sandbox_file, cases, modes, first))
        runner = EvaluationRunner(
            benchmark,
            root / f"experiment-{repeat}",
            provider=provider,
            model_id=model,
            retry_policy=NO_MODEL_RETRY,
            sandbox=sandbox.policy,
            options=load_run_options(plan_file),
            worker_api_key=args.tui_api_key,
            provider_binding={
                "connection_digest": fingerprint(args.base_url),
                "network_mode": "direct",
                "timeout_seconds": 90,
            },
        )
        if len(runner.trials) != len(cases) * 2 or source_digest(source_files()[1]) != digest:
            raise ValueError("da-batch-input-drift")
        print(
            json.dumps(
                {
                    "stage": options.stage,
                    "repeat": repeat,
                    "action": "tasks",
                    "trials": len(runner.trials),
                    "first_arm": first,
                }
            ),
            flush=True,
        )
        await runner.run()
        experiment = root / f"experiment-{repeat}"
        definition = read_input(experiment, "experiment.json").data
        refs = {}
        for arm in definition["arms"]:
            run = experiment / arm["directory"] / "run"
            print(
                json.dumps(
                    {
                        "stage": options.stage,
                        "repeat": repeat,
                        "action": "semantic-review",
                        "variant_id": arm["variant_id"],
                    }
                ),
                flush=True,
            )
            result = await review_with_model(
                run,
                root / f"review-{repeat}-{arm['variant_id']}",
                provider=provider,
                config=judge,
                max_calls=len(cases),
                max_tokens=len(cases) * judge.token_limit,
                deadline_utc=datetime.now(UTC) + timedelta(seconds=len(cases) * 120 + 120),
            )
            file = Path(result["assessment"])
            refs[arm["variant_id"]] = {
                "file": str(file),
                "sha256": read_input(file.parent, file.name).sha256,
            }
        mapping = root / f"assessments-{repeat}.json"
        write_json(
            mapping,
            {"format": 1, "experiment_digest": fingerprint(definition), "assessments": refs},
        )
        comparison = compare_experiment(
            experiment, root / f"comparison-{repeat}", assessments=mapping
        )
        print(
            json.dumps(
                {
                    "stage": options.stage,
                    "repeat": repeat,
                    "action": "compared",
                    "status": comparison.get("status"),
                    "quality": comparison.get("quality_status"),
                    "changes": comparison.get("changes"),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    print(json.dumps({"stage": options.stage, "action": "replay"}), flush=True)
    evidence = await replay(root)
    write_json(root / "replay.json", evidence)
    print(
        json.dumps(
            {
                "stage": options.stage,
                "action": "done",
                "sessions": evidence["sessions"],
                "requests": evidence["requests"],
                "total_tokens": evidence["total_tokens"],
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=("aa", "mechanism", "development"), required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--sandbox", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(execute(parser.parse_args()))
