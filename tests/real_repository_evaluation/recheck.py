"""Recheck retained evidence with original readers, without running a model."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from traceh.api.product import RequestedTaskMode
from traceh.evaluation.evaluators.product_manifest import BENCHMARK_TARGET_ID, load_product_suite
from traceh.evaluation.evaluators.product_metrics import collect_attempt_evidence
from traceh.evaluation.evaluators.product_report import AttemptReport
from traceh.evaluation.evidence import load_run
from traceh.evaluation.manifest import load_benchmark_manifest
from traceh.evaluation.repositories import read_target_revision
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.invariants import CoreInvariantChecker
from traceh.session.service import SessionService
from traceh.session.sqlite import SqliteEventStore
from traceh.session.surface import SurfaceProjector


async def recheck(root, materials, output):
    if output.exists():
        raise FileExistsError(output)
    _, report, binding = load_run(root)
    suite = load_product_suite(
        load_benchmark_manifest(materials / "material"), provider_id="offline", model_id="offline"
    )
    cases = {task.task_id: task for task in suite.tasks}
    snapshots = 0
    for item in report["task_report"]["attempts"]:
        directory = root / item["directory"]
        database = directory / "ev/events.sqlite3"
        before = hashlib.sha256(database.read_bytes()).hexdigest()
        store = SqliteEventStore(database.parent)
        try:
            task = cases[item["benchmark_task_id"]]
            old = item["evidence"]
            target = await read_target_revision(
                (directory / "tgt.git").resolve(), old["target_ref"]
            )
            derived = await collect_attempt_evidence(
                store,
                task_id=old["task_id"],
                promotion_target_id=BENCHMARK_TARGET_ID,
                target_ref=old["target_ref"],
                target_revision=target,
                verification_plan=task.settings.host_profile.verification_plan,
            )
            rendered = AttemptReport(
                attempt_id=item["attempt_id"],
                benchmark_task_id=item["benchmark_task_id"],
                requested_mode=RequestedTaskMode(item["requested_mode"]),
                repetition=item["repetition"],
                directory=item["directory"],
                error_code=item["error_code"],
                evidence=derived,
                timing=None,
                retrieval=None,
            ).to_dict()["evidence"]
            if rendered != old:
                raise ValueError("rederived Product evidence differs")
            service = SessionService(store)
            refs = next(
                trial
                for trial in report["trials"]
                if trial["identity"]["trial_id"] == item["attempt_id"]
            )["evidence"]
            for stream in sorted(
                {ref["stream_id"] for ref in refs if ref["stream_id"].startswith("session:")}
            ):
                session = stream.removeprefix("session:")
                events = await service.read_session(session)
                effects = await service.read_effects(session)
                if CoreInvariantChecker().check(events, effects):
                    raise ValueError("Session invariant failure")
                if await verify_request_snapshots(service, SurfaceProjector(), session):
                    raise ValueError("request reconstruction failure")
                snapshots += sum(event.type == "request/snapshot" for event in events)
        finally:
            await store.aclose()
        if hashlib.sha256(database.read_bytes()).hexdigest() != before:
            raise ValueError("reread changed the original database")
    value = dict(
        binding=binding,
        attempts=len(report["trials"]),
        snapshots=snapshots,
        original_databases_unchanged=True,
        target_and_evidence_rederived=True,
    )
    with output.open("x", encoding="utf-8", newline="\n") as file:
        json.dump(value, file, indent=2)
        file.write("\n")
    print("Verified", value["attempts"], "attempts and", snapshots, "request snapshots")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("run", "materials", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    options = parser.parse_args()
    asyncio.run(
        recheck(options.run.resolve(), options.materials.resolve(), options.output.resolve())
    )
