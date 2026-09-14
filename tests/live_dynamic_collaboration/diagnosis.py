"""Bounded explicit/natural/simple real-provider diagnostic via original Product evaluation."""

import argparse
import asyncio
import json
from pathlib import Path

from live_unified_evaluation.baseline import connection

from live_dynamic_collaboration.diagnosis_audit import describe_run
from live_dynamic_collaboration.materials import write
from traceh.api.json_types import fingerprint
from traceh.evaluation.manifest import load_benchmark_manifest
from traceh.evaluation.plan import RunOptions
from traceh.evaluation.runner import EvaluationRunner
from traceh.evaluation.variants import source_digest, source_files
from traceh.llm.retry import NO_MODEL_RETRY
from traceh.sandbox.config import load_sandbox_file


def positive_control_started(result):
    # A model attempt alone can be rejected. The original Product handoffs
    # require an owned Directory child with an accepted Inbox work message.
    return any(row["delegate_calls"] and row["handoffs"] for row in result["rows"])


async def execute(options):
    output = options.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    manifest = load_benchmark_manifest(options.benchmark.resolve())
    if tuple(c["case_id"] for c in manifest.dataset.data["cases"]) != (
        "explicit",
        "natural",
        "simple",
    ):
        raise ValueError("diagnostic-case-contract-mismatch")
    raise RuntimeError(
        "historical-collaboration-contract: use frozen source; current multi requires WC-1F"
    )
    args, provider, model = connection(options.profile.resolve())
    if (provider.name, model) != ("openai-compatible", "qwen-plus"):
        raise ValueError("explicit-diagnostic-model-mismatch")
    provider.timeout_seconds = 90
    sandbox = load_sandbox_file(options.sandbox.resolve())
    if sandbox.plugin_grants or sandbox.policy.network != "none":
        raise ValueError("diagnostic-sandbox-scope-invalid")
    write(
        output / "contract.json",
        dict(
            format=1,
            source_digest=source_digest(source_files()[1]),
            manifest_digest=manifest.document.sha256,
            dataset_digest=manifest.dataset.sha256,
            source_protocol=3,
            provider=provider.name,
            model=model,
            connection_digest=fingerprint(args.base_url),
            network="direct",
            retries=1,
            max_trials=6,
            repetitions=2,
            order=["explicit", "natural", "simple"],
            source_material_is_runtime_source=False,
            structural_verification_only=True,
            analysis_or_judge_calls=0,
            adoption_authorized=False,
        ),
    )
    (output / "sandbox.json").write_bytes(options.sandbox.resolve().read_bytes())
    for case in ("explicit", "natural", "simple"):
        print(json.dumps(dict(action="run", case=case, repetitions=2)), flush=True)
        runner = EvaluationRunner(
            options.benchmark.resolve(),
            output / case,
            provider=provider,
            model_id=model,
            retry_policy=NO_MODEL_RETRY,
            sandbox=sandbox.policy,
            options=RunOptions(repetitions=2, case_ids=(case,), max_trials=2, timeout_seconds=2400),
            provider_binding=dict(
                connection_digest=fingerprint(args.base_url),
                network_mode="direct",
                timeout_seconds=90,
            ),
        )
        await runner.run()
        result = describe_run(output / case)
        write(output / (case + "-diagnostic.json"), result)
        print(
            json.dumps(
                dict(
                    action="result",
                    case=case,
                    outcomes=[
                        {
                            key: r[key]
                            for key in (
                                "delegate_calls",
                                "delegation_after_actual_read",
                                "structural_success",
                                "error",
                            )
                        }
                        for r in result["rows"]
                    ],
                )
            ),
            flush=True,
        )
        if case == "explicit" and not positive_control_started(result):
            write(
                output / "stopped.json",
                dict(reason="explicit-condition-never-delegated", remaining_trials_not_run=4),
            )
            return


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("profile", "sandbox", "benchmark", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    asyncio.run(execute(parser.parse_args()))
