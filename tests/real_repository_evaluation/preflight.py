"""Material admission via the production sandbox; not an Agent success score."""

import argparse
import asyncio
import json
from pathlib import Path

from traceh.api.sandbox import SandboxOwner
from traceh.artifacts.cas import LocalArtifactCas
from traceh.evaluation.evaluators.product_manifest import load_product_suite
from traceh.evaluation.manifest import load_benchmark_manifest
from traceh.sandbox.config import load_sandbox_file
from traceh.sandbox.service import GUEST_ENVIRONMENT, SandboxExecutionService
from traceh.session.sqlite import SqliteEventStore

from .materials import expected_tests, write_json


async def run(materials, sandbox_file, output):
    output.mkdir(parents=True, exist_ok=False)
    suite = load_product_suite(
        load_benchmark_manifest(materials / "material"), provider_id="offline", model_id="offline"
    )
    policy = load_sandbox_file(sandbox_file).policy
    provenance = json.loads((materials / "provenance.json").read_bytes())
    specifications = {item["instance_id"]: item for item in provenance["selection"]["tasks"]}
    store = SqliteEventStore(output / "events")
    service = SandboxExecutionService(
        store=store, cas=LocalArtifactCas(output / "cas"), policy=policy
    )
    results = []
    try:
        for task in suite.tasks:
            row = json.loads((materials / "host" / task.task_id / "instance.json").read_bytes())
            f2p, p2p = map(set, expected_tests(row, specifications[task.task_id]))
            for variant, workspace in (
                ("baseline", task.initial_dir),
                ("reference", materials / "host" / task.task_id / "reference"),
            ):
                plan = task.settings.host_profile.verification_plan
                environment = dict(GUEST_ENVIRONMENT)
                environment.update(plan.environment.overrides)
                identity = task.task_id + "-" + variant
                async with service.scope(
                    SandboxOwner("verification", identity),
                    stream_id="admission:" + identity,
                    workspace=workspace,
                    data_dir=None,
                    publish_changes=False,
                    environment=tuple(sorted(environment.items())),
                    retain_output=True,
                ) as scope:
                    command = plan.commands[0]
                    result = await scope.run(
                        command.argv,
                        timeout_seconds=command.timeout_ms / 1000,
                        max_output_bytes=plan.max_output_bytes,
                    )
                (output / (identity + ".stdout")).write_bytes(result.stdout)
                (output / (identity + ".stderr")).write_bytes(result.stderr)
                reports = [
                    line.removeprefix("TRACEH_RR_RESULT=")
                    for line in result.stdout.decode("utf-8", errors="replace").splitlines()
                    if line.startswith("TRACEH_RR_RESULT=")
                ]
                report = json.loads(reports[0]) if len(reports) == 1 else None
                admitted = bool(
                    result.status == "finished"
                    and report is not None
                    and not report["errors"]
                    and not report["missing"]
                    and p2p <= set(report["passed"])
                    and (
                        f2p <= set(report["failed"]) and result.exit_code == 1
                        if variant == "baseline"
                        else f2p <= set(report["passed"]) and result.exit_code == 0
                    )
                )
                item = dict(
                    instance_id=task.task_id,
                    variant=variant,
                    status=result.status,
                    exit_code=result.exit_code,
                    admitted=admitted,
                    tests=report,
                    sandbox_receipt=result.receipt,
                )
                results.append(item)
                write_json(
                    output / "admission.json",
                    dict(
                        kind="material-admission-not-agent-score",
                        image=policy.image,
                        complete=len(results) == len(suite.tasks) * 2,
                        admitted=all(row["admitted"] for row in results),
                        results=results,
                    ),
                )
                print(identity, result.status, result.exit_code, "admitted", admitted, flush=True)
    finally:
        await store.aclose()
    return all(row["admitted"] for row in results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("materials", "sandbox", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    options = parser.parse_args()
    raise SystemExit(
        0
        if asyncio.run(
            run(options.materials.resolve(), options.sandbox.resolve(), options.output.resolve())
        )
        else 1
    )
