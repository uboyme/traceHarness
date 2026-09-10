"""Explicit bounded judge calibration over original AO-2 evidence. No new evaluator."""

import argparse
import asyncio
import json
import zipfile
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from live_optimization.calibration_inputs import build_inputs
from live_unified_evaluation.baseline import connection

from traceh.api.json_types import fingerprint
from traceh.evaluation.comparison import inspect_experiment
from traceh.evaluation.inputs import digest_bytes, read_input
from traceh.evaluation.model_evidence import load_model_call
from traceh.evaluation.model_review_protocol import call_judgment
from traceh.evaluation.model_service import ModelCallConfig, error_codes, run_model_call
from traceh.evaluation.variant_execution import write_json
from traceh.evaluation.variants import source_digest, source_files
from traceh.evolution.strategy import _review_pair


def summarize(plan, rows):
    by_id = {s["sample_id"]: s for s in plan["samples"]}
    results = {}
    for condition in ("baseline", "revised"):
        selected = [r for r in rows if r["condition"] == condition]
        results[condition] = {
            "completed": len(selected),
            "planned": len(by_id) * 2,
            "matches_expectation": sum(
                r["judgment"]["status"] == by_id[r["sample_id"]]["expected"] for r in selected
            ),
            "false_pass": sum(
                r["judgment"]["status"] == "passed"
                and by_id[r["sample_id"]]["expected"] == "failed"
                for r in selected
            ),
            "false_fail": sum(
                r["judgment"]["status"] == "failed"
                and by_id[r["sample_id"]]["expected"] == "passed"
                for r in selected
            ),
            "pending": sum(r["judgment"]["status"] == "pending_review" for r in selected),
            "inconsistent_samples": [
                key
                for key in by_id
                if len({r["judgment"]["status"] for r in selected if r["sample_id"] == key}) > 1
            ],
            "tokens": sum(r["tokens"] for r in selected)
            if all(r["tokens"] is not None for r in selected)
            else None,
        }
    gate = results["revised"]["matches_expectation"] == len(by_id) * 2
    return {
        "conditions": results,
        "frozen_gate_passed": gate,
        "rows": rows,
        "adoption_authorized": False,
    }


async def execute(options):
    root = options.output.resolve()
    original = options.original.resolve()
    native_runs = {
        "development": original / "development/experiment/rounds/0001/evaluation",
        "heldout": original / "holdout",
    }
    before = read_input(options.before, "inputs.json")
    if (
        root.is_relative_to(original)
        or original.is_relative_to(root)
        or {Path(r["run"]) for r in before.data["rows"]}
        != {native / f"arms/{arm}/run" for native in native_runs.values() for arm in ("01", "02")}
    ):
        raise ValueError("calibration-original-run-mismatch")
    samples, audit = build_inputs(before.data)
    if len(samples) != 12 or len(audit) != 18:
        raise ValueError("calibration-explicit-corpus-mismatch")
    args, provider, model = connection(options.profile)
    config = ModelCallConfig(
        provider.name,
        model,
        0.0,
        "cl100k_base",
        64000,
        2048,
        2048,
        90,
        digest_bytes(args.base_url.encode()),
    )
    root.mkdir(parents=True, exist_ok=False)
    files = source_files()[1]
    with zipfile.ZipFile(root / "source.zip", "x", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files:
            archive.writestr(name, data)
    (root / "baseline-source.zip").write_bytes((options.before / "source.zip").read_bytes())
    (root / "baseline-inputs.json").write_bytes(before.content)
    for name in ("calibrate.py", "calibration_inputs.py"):
        (root / name).write_bytes(Path(__file__).with_name(name).read_bytes())
    plan = {
        "format": 1,
        "source_digest": source_digest(files),
        "baseline_inputs_digest": before.sha256,
        "baseline_source_digest": before.data["source_digest"],
        "config": asdict(config),
        "deadline_utc": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        "max_calls": 65,
        "calibration_calls": 48,
        "native_review_calls": 17,
        "max_tokens": 65 * config.token_limit,
        "native_runs": {name: str(path) for name, path in native_runs.items()},
        "repetitions": 2,
        "expectations_origin": "agent-contract-audit-not-independent-human-gold",
        "gate": "all 24 revised judgments match frozen expectations; baseline comparison retained",
        "samples": samples,
    }
    write_json(root / "plan.json", plan)
    write_json(root / "original-audit.json", audit)
    rows = []
    stop = None
    try:
        async with asyncio.timeout(7200):
            for repeat in range(2):
                order = samples if repeat == 0 else list(reversed(samples))
                for sample in order:
                    for condition in (
                        ("baseline", "revised") if repeat == 0 else ("revised", "baseline")
                    ):
                        index = len(rows) + 1
                        value = sample[condition]
                        directory = root / "calls" / f"{index:03d}"
                        await run_model_call(
                            provider=provider,
                            config=config,
                            system=value["system"],
                            input_text=value["input"],
                            binding={
                                "purpose": "judge-calibration",
                                "plan_digest": fingerprint(plan),
                                "sample_id": sample["sample_id"],
                                "condition": condition,
                                "replicate": repeat + 1,
                                "input_digest": fingerprint(value["input"]),
                            },
                            output_dir=directory,
                        )
                        _, receipt, digest = load_model_call(directory)
                        row = {
                            "sample_id": sample["sample_id"],
                            "condition": condition,
                            "replicate": repeat + 1,
                            "call": directory.relative_to(root).as_posix(),
                            "receipt_sha256": digest,
                            "judgment": call_judgment(receipt),
                            "tokens": receipt["observation"]["usage"]["total_tokens"],
                        }
                        rows.append(row)
                        print(
                            json.dumps(
                                {
                                    "call": index,
                                    "sample": row["sample_id"],
                                    "condition": condition,
                                    "status": row["judgment"]["status"],
                                }
                            ),
                            flush=True,
                        )
                        if (
                            receipt["errors"]
                            or not receipt["converged"]
                            or not receipt["observation"]["budget_usage_exact"]
                            or row["tokens"] is None
                        ):
                            raise ValueError("calibration-call-inconclusive")
            write_json(root / "calibration.json", summarize(plan, rows))
            for name, native in native_runs.items():
                print(json.dumps({"stage": "native-review", "suite": name}), flush=True)
                output = root / "review" / name
                await _review_pair(
                    native,
                    output,
                    provider=provider,
                    config=config,
                    deadline=datetime.fromisoformat(plan["deadline_utc"]),
                )
                comparison = inspect_experiment(native, assessments=output / "assessments.json")
                write_json(root / (name + "-comparison.json"), comparison)
                if comparison["changes"]["unknown"]:
                    raise ValueError("calibration-native-review-inconclusive")
    except BaseException as error:
        stop = error_codes(error)
        write_json(root / "stop.json", {"errors": stop})
        if not isinstance(error, Exception) or isinstance(error, BaseExceptionGroup):
            raise
    finally:
        # Retain fees even when a call failed before returning a normal judgment row.
        calls = [
            load_model_call(p.parent)[1]
            for p in root.rglob("result.json")
            if (p.parent / "call.json").exists()
        ]
        tokens = [c["observation"]["usage"]["total_tokens"] for c in calls]
        write_json(
            root / "result.json",
            {
                **summarize(plan, rows),
                "stop": stop,
                "all_recorded_calls": len(calls),
                "all_tokens": sum(tokens) if all(t is not None for t in tokens) else None,
            },
        )
    print(json.dumps({k: v for k, v in summarize(plan, rows).items() if k != "rows"}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("profile", "before", "original", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    asyncio.run(execute(parser.parse_args()))
