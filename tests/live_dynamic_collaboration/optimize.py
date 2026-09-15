"""One explicit DA-4 proposal on development evidence through the original AO owner."""

import argparse
import asyncio
import json
import shutil
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from live_optimization.reopen import replay
from live_unified_evaluation.baseline import connection

from live_dynamic_collaboration.run import MECHANISM, plan
from traceh.api.json_types import fingerprint
from traceh.api.optimization import DevelopmentObservation
from traceh.evaluation.comparison import inspect_experiment
from traceh.evaluation.inputs import read_input
from traceh.evaluation.manifest import load_benchmark_manifest
from traceh.evaluation.model_service import ModelCallConfig
from traceh.evaluation.plan import load_run_options
from traceh.evaluation.review import load_assessment
from traceh.evaluation.runner import EvaluationRunner
from traceh.evaluation.variant_execution import write_json
from traceh.evaluation.variants import source_digest, source_files
from traceh.evolution.optimization_contract import (
    OptimizationContract,
    OptimizationLimits,
    editable_text,
)
from traceh.evolution.strategy import run_strategy_optimization
from traceh.llm.retry import NO_MODEL_RETRY
from traceh.sandbox.config import load_sandbox_file
from traceh.session.service import SessionService
from traceh.session.sqlite import SqliteEventStore

SELECTORS = tuple(
    ("supervision/delegation.py", name)
    for name in ("DELEGATE_GUIDANCE", "FOLLOWUP_GUIDANCE", "COLLECT_GUIDANCE", "STOP_GUIDANCE")
)


def compact_assessment(assessment):
    reason = assessment.get("reason")
    return {
        "status": assessment["status"],
        "origin": assessment.get("origin"),
        "reason_preview": None if reason is None else reason[:160],
        "reason_truncated": reason is not None and len(reason) > 160,
    }


def optimization_plan(manifest, settings, sandbox_file):
    raw = plan(manifest, settings, sandbox_file, MECHANISM, ["adaptive", "adaptive"], "baseline")
    raw["comparison"].update(kind="text_candidate")
    raw["trials"]["repetitions"] = 2
    raw["execution"]["max_trials"] = 24
    return raw


async def observations(development):
    """A bounded diagnostic view; all grades still come from original assessment."""
    frozen = read_input(development, "batch-contract.json").data
    if frozen["stage"] != "development" or frozen["repeats"] != 2:
        raise ValueError("da-development-evidence-required")
    rows = {case: [] for case in MECHANISM}
    locations = {case: [] for case in rows}
    for repeat in (1, 2):
        experiment = development / f"experiment-{repeat}"
        assessment = development / f"assessments-{repeat}.json"
        compared = inspect_experiment(experiment, assessments=assessment)
        if not compared["complete"] or compared["status"] == "not_comparable":
            raise ValueError("da-original-evidence-not-comparable")
        definition = read_input(experiment, "experiment.json").data
        mapping = read_input(development, assessment.name).data["assessments"]
        for arm in definition["arms"]:
            root = experiment / arm["directory"] / "run"
            report = read_input(root, "report.json").data
            attempts = report["task_report"]["attempts"]
            assessed, _ = load_assessment(Path(mapping[arm["variant_id"]]["file"]))
            grades = {t["identity"]["trial_id"]: t["assessment"] for t in assessed["trials"]}
            for trial in report["trials"]:
                identity = trial["identity"]
                case = identity["case_id"]
                if case not in rows:
                    continue
                diagnostic = {
                    "repeat": repeat,
                    "trial_id": identity["trial_id"],
                    "mode": identity["requested_mode"],
                    "execution": trial["execution"],
                    "invariants": trial["invariants"],
                    "convergence": trial["convergence"],
                    "usage": trial["usage"],
                    "assessment": compact_assessment(grades[identity["trial_id"]]),
                    "paired_changes": compared.get("changes"),
                }
                if identity["requested_mode"] == "adaptive":
                    attempt = next(a for a in attempts if a["benchmark_task_id"] == case)
                    # These are verified original Session events, never another run's
                    # chat or a model-written completion report masquerading as proof.
                    # Session readers can retire WAL/SHM sidecars when closing.
                    # Inspect a byte-for-byte copy so discovery cannot alter the
                    # original experiment, even its temporary SQLite files.
                    copied = TemporaryDirectory(prefix="da-observation-")
                    database = Path(copied.name) / "ev"
                    await asyncio.to_thread(
                        shutil.copytree, root / attempt["directory"] / "ev", database
                    )
                    store = SqliteEventStore(database)
                    try:
                        trail = []
                        execution = attempt["evidence"]["execution"]
                        for session in execution["sessions"]:
                            events = await SessionService(store).read_session(session["session_id"])
                            relevant = [
                                e
                                for e in events
                                if e.type in {"tool/call", "tool/result"}
                                and e.data.get("tool_name")
                                in {
                                    "delegate_investigation",
                                    "followup_investigation",
                                    "collect_investigation",
                                    "stop_investigation",
                                }
                            ]
                            for event in relevant[:16]:
                                serialized = json.dumps(
                                    event.data, ensure_ascii=False, sort_keys=True
                                )
                                trail.append(
                                    {
                                        "ref": f"{event.stream_id}@{event.seq}",
                                        "event": event.type,
                                        "preview": serialized[:600],
                                        "truncated": len(serialized) > 600,
                                    }
                                )
                        diagnostic["delegation_events"] = trail
                        diagnostic["delegation_events_limit_per_session"] = 16
                    finally:
                        await store.aclose()
                        copied.cleanup()
                rows[case].append(diagnostic)
                locations[case].extend((str(root / "report.json"), str(assessment)))
    if any(len(values) != 4 for values in rows.values()):
        raise ValueError("da-development-pairs-incomplete")
    return tuple(
        DevelopmentObservation(
            case,
            "collaboration-efficiency-and-evidence",
            "Development observations only; pending grades are not failures. Improve general "
            "delegation, evidence handoff and stop guidance only when useful. Do not hardcode "
            "case names, answers or mandatory delegation. Preserve local-task efficiency and "
            "readonly authority.\n" + json.dumps(values, ensure_ascii=False, sort_keys=True),
            tuple(dict.fromkeys(locations[case])),
        )
        for case, values in rows.items()
    )


async def execute(options):
    root = options.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    benchmark = options.benchmark.resolve()
    manifest = load_benchmark_manifest(benchmark)
    raise RuntimeError(
        "historical-collaboration-contract: use frozen source; current multi requires WC-1F"
    )
    args, provider, model = connection(options.profile.resolve())
    if provider.name != "openai-compatible" or model != "qwen-plus":
        raise ValueError("explicit-da-model-contract-mismatch")
    provider.timeout_seconds = 90
    sandbox = load_sandbox_file(options.sandbox.resolve())
    if sandbox.plugin_grants or sandbox.policy.network != "none":
        raise ValueError("da-sandbox-scope-invalid")
    sandbox_file = root / "sandbox.json"
    sandbox_file.write_bytes(options.sandbox.resolve().read_bytes())
    settings = dict(
        provider=provider.name, model=model, base_url=args.base_url, api_key_env=args.api_key_env
    )
    raw = optimization_plan(manifest, settings, sandbox_file)
    file = root / "plan.json"
    write_json(file, raw)
    template = EvaluationRunner(
        benchmark,
        root / "unused",
        provider=provider,
        model_id=model,
        retry_policy=NO_MODEL_RETRY,
        sandbox=sandbox.policy,
        options=load_run_options(file),
        worker_api_key=args.tui_api_key,
        provider_binding={
            "connection_digest": fingerprint(args.base_url),
            "network_mode": "direct",
            "timeout_seconds": 90,
        },
    )
    if len(template.trials) != 24:
        raise ValueError("da-optimization-trial-count-mismatch")
    analysis = ModelCallConfig(
        provider.name, model, 0.0, "cl100k_base", 32000, 6000, 1024, 90, fingerprint(args.base_url)
    )
    judge = ModelCallConfig(
        provider.name, model, 0.0, "cl100k_base", 64000, 2048, 2048, 90, fingerprint(args.base_url)
    )
    files = source_files()[1]
    evidence = await observations(options.development.resolve())
    contract = OptimizationContract(
        "da4-one-readonly-collaboration-proposal",
        source_digest(files),
        manifest.document.sha256,
        template.options.document.sha256,
        manifest.dataset.sha256,
        MECHANISM,
        editable_text(files, SELECTORS),
        OptimizationLimits(1, 1, 24, 1, 1, 1, 1, 32000, 200000),
        datetime.now(UTC) + timedelta(hours=6),
    )
    write_json(
        root / "batch-contract.json",
        {
            "format": 1,
            "stage": "optimization",
            "contract": contract.to_dict(),
            "analysis": asdict(analysis),
            "judge": asdict(judge),
            "observations": [asdict(item) for item in evidence],
            "driver_digest": fingerprint(
                await asyncio.to_thread(Path(__file__).read_text, encoding="utf-8")
            ),
            "adoption_authorized": False,
            "holdout_available_to_analysis": False,
        },
    )
    print(
        json.dumps({"stage": "optimization", "action": "one-proposal", "max_trials": 24}),
        flush=True,
    )
    result = await run_strategy_optimization(
        template,
        contract,
        observations=evidence,
        provider=provider,
        analysis_config=analysis,
        judge_provider=provider,
        judge_config=judge,
        output_dir=root / "optimization",
    )
    print(
        json.dumps(
            {
                "stage": "optimization",
                "action": result["action"],
                "reason": result["reason"],
                "cost": result["cost"],
            }
        ),
        flush=True,
    )
    write_json(root / "replay.json", await replay(root))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for argument in ("profile", "benchmark", "sandbox", "development", "output"):
        parser.add_argument("--" + argument, type=Path, required=True)
    asyncio.run(execute(parser.parse_args()))
