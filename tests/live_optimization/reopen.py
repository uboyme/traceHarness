"""Offline AO-2 replay from original logs, with an unchanged-files check."""

import argparse
import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

from traceh.evaluation.comparison import inspect_experiment
from traceh.evaluation.evaluators.episode_assessment import usage
from traceh.evaluation.model_evidence import load_model_call
from traceh.evaluation.review import load_assessment
from traceh.evaluation.variant_execution import write_json
from traceh.evolution.strategy import inspect_strategy_optimization
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.invariants import CoreInvariantChecker
from traceh.session.service import SessionService
from traceh.session.sqlite import SqliteEventStore
from traceh.session.surface import SurfaceProjector


def hashes(root):
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def session_ids(database):
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        return [
            row[0].removeprefix("session:")
            for row in connection.execute(
                "SELECT DISTINCT stream_id FROM events WHERE stream_id LIKE 'session:%'"
            )
        ]
    finally:
        connection.close()


async def replay(root):
    before, rows = hashes(root), []
    for database in sorted(root.rglob("events.sqlite3")):
        store = SqliteEventStore(database.parent)
        try:
            service = SessionService(store)
            for session_id in session_ids(database):
                events = await service.read_session(session_id)
                effects = await service.read_effects(session_id)
                errors = await verify_request_snapshots(service, SurfaceProjector(), session_id)
                invariants = CoreInvariantChecker().check(events, effects)
                rows.append(
                    {
                        "database": database.relative_to(root).as_posix(),
                        "session_id": session_id,
                        "requests": sum(e.type == "request/snapshot" for e in events),
                        "usage": usage(events),
                        "failed_attempts": sum(
                            e.type == "model/attempt-end" and e.data["status"] != "succeeded"
                            for e in events
                        ),
                        "replay_errors": list(errors),
                        "invariant_errors": [str(v) for v in invariants],
                    }
                )
        finally:
            await store.aclose()
    controls = []
    for definition in sorted(root.rglob("call.json")):
        _, receipt, digest = load_model_call(definition.parent)
        controls.append(
            {
                "directory": definition.parent.relative_to(root).as_posix(),
                "receipt_sha256": digest,
                "usage": receipt["observation"]["usage"],
                "converged": receipt["converged"],
                "errors": receipt["errors"],
            }
        )
    for assessment in root.rglob("assessment/assessment.json"):
        load_assessment(assessment)
    after = hashes(root)
    changed = {
        k: [before.get(k), after.get(k)]
        for k in before.keys() | after.keys()
        if before.get(k) != after.get(k)
    }
    assert not changed, changed
    assert not any(r["replay_errors"] or r["invariant_errors"] for r in rows)
    totals = [r["usage"]["total_tokens"] for r in rows]
    return {
        "original_files_unchanged": len(before),
        "file_hashes": before,
        "sessions": len(rows),
        "requests": sum(r["requests"] for r in rows),
        "recorded_model_attempts": sum(r["usage"]["attempts"] for r in rows),
        "failed_attempts": sum(r["failed_attempts"] for r in rows),
        "total_tokens": sum(totals) if all(t is not None for t in totals) else None,
        "control_calls": controls,
        "rows": rows,
    }


async def execute(options):
    root = options.accepted.resolve()
    rejected = options.rejected.resolve()
    output = options.output.resolve()
    if any(output.is_relative_to(p) or p.is_relative_to(output) for p in (root, rejected)):
        raise ValueError("replay-output-overlap")
    output.mkdir(parents=True, exist_ok=False)
    # Rejected proposal predates the clarified analysis prompt: its original call is
    # still independently verifiable, but is not rebound to the newer policy.
    runs = {"rejected": await replay(rejected), "accepted": await replay(root)}
    development = inspect_strategy_optimization(root / "development")
    holdout = inspect_experiment(
        root / "holdout", assessments=root / "holdout-review/assessments.json"
    )
    report = {
        "format": 1,
        "new_provider_calls": 0,
        "runs": runs,
        "sessions": sum(r["sessions"] for r in runs.values()),
        "requests": sum(r["requests"] for r in runs.values()),
        "recorded_model_attempts": sum(r["recorded_model_attempts"] for r in runs.values()),
        "failed_attempts": sum(r["failed_attempts"] for r in runs.values()),
        "total_tokens": sum(r["total_tokens"] for r in runs.values()),
        "development": development,
        "holdout": holdout,
    }
    write_json(output / "independent-replay.json", report)
    print(
        json.dumps({k: v for k, v in report.items() if k not in {"runs", "development", "holdout"}})
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accepted", type=Path, required=True)
    parser.add_argument("--rejected", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(execute(parser.parse_args()))
