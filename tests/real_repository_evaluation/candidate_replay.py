"""Diagnose a retained candidate under newly frozen material; never rewrite its trial."""

import argparse
import asyncio
import hashlib
import json
import shutil
from pathlib import Path

from traceh.api.sandbox import SandboxOwner
from traceh.artifacts.cas import LocalArtifactCas
from traceh.evaluation.evaluators.product_manifest import load_product_suite
from traceh.evaluation.evidence import load_run
from traceh.evaluation.manifest import load_benchmark_manifest
from traceh.sandbox.config import load_sandbox_file
from traceh.sandbox.service import GUEST_ENVIRONMENT, SandboxExecutionService
from traceh.session.sqlite import SqliteEventStore

from .materials import apply_patch


async def replay(run, materials, sandbox, output):
    _, report, binding = load_run(run)
    attempts = report["task_report"]["attempts"]
    if not report["complete"] or report["errors"] or len(attempts) != 1:
        raise ValueError("requires one complete original trial")
    attempt = attempts[0]
    suite = load_product_suite(
        load_benchmark_manifest(materials / "material"),
        provider_id="offline", model_id="offline",
    )
    task = next(task for task in suite.tasks if task.task_id == attempt["benchmark_task_id"])
    event_file = run / attempt["directory"] / "ev/events.sqlite3"
    import sqlite3

    with sqlite3.connect(event_file.resolve().as_uri() + "?mode=ro", uri=True) as db:
        rows = [json.loads(row[0]) for row in db.execute("select envelope_json from events")]
    reviews = [row["data"] for row in rows if row["type"] == "patch/review-recorded"]
    if len(reviews) != 1:
        raise ValueError("requires one original review bound to a Patch")
    patch_sha = reviews[0]["patch_sha256"]
    patch_file = run / attempt["directory"] / "cas/sha256" / patch_sha[:2] / patch_sha
    patch = patch_file.read_bytes()
    if hashlib.sha256(patch).hexdigest() != patch_sha:
        raise ValueError("original CAS Patch differs")
    workspace = output / "candidate"
    shutil.copytree(task.initial_dir, workspace)
    apply_patch(workspace, patch.decode("utf-8"))
    before = {
        path.relative_to(workspace).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in workspace.rglob("*") if path.is_file()
    }
    store = SqliteEventStore(output / "events")
    service = SandboxExecutionService(
        store=store, cas=LocalArtifactCas(output / "cas"),
        policy=load_sandbox_file(sandbox).policy,
    )
    plan = task.settings.host_profile.verification_plan
    env = dict(GUEST_ENVIRONMENT)
    env.update(plan.environment.overrides)
    try:
        async with service.scope(
            SandboxOwner("verification", "candidate-material-diagnostic"),
            stream_id="diagnostic:candidate-material", workspace=workspace,
            data_dir=None, publish_changes=False,
            environment=tuple(sorted(env.items())), retain_output=True,
        ) as scope:
            command = plan.commands[0]
            result = await scope.run(
                command.argv, timeout_seconds=command.timeout_ms / 1000,
                max_output_bytes=plan.max_output_bytes,
            )
        (output / "stdout.txt").write_bytes(result.stdout)
        (output / "stderr.txt").write_bytes(result.stderr)
        reports = [json.loads(line.split("=", 1)[1])
                   for line in result.stdout.decode("utf-8").splitlines()
                   if line.startswith("TRACEH_RR_RESULT=")]
        after = {
            path.relative_to(workspace).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in workspace.rglob("*") if path.is_file()
        }
        summary = dict(
            kind="independent-new-material-diagnostic-not-original-trial-result",
            original_binding=binding,
            original_report_sha256=hashlib.sha256((run / "report.json").read_bytes()).hexdigest(),
            original_success=attempt["success"],
            case_id=task.task_id,
            original_patch_sha256=patch_sha,
            new_material_verifier_plan_version=plan.plan_version,
            candidate_workspace_unchanged=before == after,
            status=result.status, exit_code=result.exit_code,
            report=reports[0] if len(reports) == 1 else None,
            receipt=result.receipt,
        )
        (output / "summary.json").write_text(
            json.dumps(summary, indent=2) + "\n", encoding="utf-8",
        )
        print(json.dumps({k: v for k, v in summary.items() if k not in ("report", "receipt")},
                         ensure_ascii=False))
        if reports:
            print("test counts", {k: len(reports[0][k])
                                  for k in ("passed", "failed", "errors", "missing")})
    finally:
        await store.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("run", "materials", "sandbox", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(exist_ok=False)
    asyncio.run(replay(args.run.resolve(), args.materials.resolve(),
                       args.sandbox.resolve(), args.output.resolve()))
