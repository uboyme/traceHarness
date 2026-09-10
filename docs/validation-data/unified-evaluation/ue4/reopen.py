"""Offline UE-4 recheck of every original Session, then the existing review export."""

import argparse
import asyncio
import hashlib
import json
from collections import Counter
from pathlib import Path

from traceh.evaluation.evidence import load_run
from traceh.evaluation.review import export_review
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


async def execute(options):
    root, output = options.run.resolve(), options.output.resolve()
    if output.is_relative_to(root) or root.is_relative_to(output):
        raise ValueError("reopen-output-overlap")
    before = hashes(root)
    frozen, report, binding = load_run(root)
    rows = []
    for trial in report["trials"]:
        refs = {
            (r["file"], r["stream_id"])
            for r in trial["evidence"]
            if r["stream_id"].startswith("session:")
        }
        for file, stream in sorted(refs):
            store = SqliteEventStore((root / file).parent)
            try:
                session = stream.removeprefix("session:")
                service = SessionService(store)
                events = await service.read_session(session)
                effects = await service.read_effects(session)
                replay = await verify_request_snapshots(service, SurfaceProjector(), session)
                invariants = CoreInvariantChecker().check(events, effects)
                rows.append(
                    {
                        "trial_id": trial["identity"]["trial_id"],
                        "session_id": session,
                        "requests": sum(e.type == "request/snapshot" for e in events),
                        "failed_attempts": sum(
                            e.type == "model/attempt-end" and e.data["status"] != "succeeded"
                            for e in events
                        ),
                        "replay_errors": list(replay),
                        "invariant_errors": [str(v) for v in invariants],
                    }
                )
            finally:
                await store.aclose()
    output.mkdir(parents=True, exist_ok=False)
    export_review(root, output / "review")
    assert before == hashes(root), "original-run-mutated"
    summary = {
        "run_id": report["run_id"],
        "binding": binding,
        "source_digest": frozen["variant"]["source_digest"],
        "provider_calls": 0,
        "original_files_unchanged": len(before),
        "sessions": len(rows),
        "requests": sum(r["requests"] for r in rows),
        "rows": rows,
    }
    (output / "independent-replay.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    packets = report["task_report"]["episodes"]
    groups = {}
    for family in sorted({p["family"] for p in packets}):
        selected = [p for p in packets if p["family"] == family]
        positives = [p for p in selected if p["expectation"]["kind"] == "value"]
        groups[family] = {
            "executed_packets": len(selected),
            "positive": len(positives),
            "provisional_joint": sum(p["provisional_joint_pass"] for p in positives),
            "answer_match": sum(bool(p["provisional_answer_match"]) for p in positives),
            "evidence_dispatched": sum(bool(p["dispatched_evidence"]) for p in positives),
            "negative_pending": len(selected) - len(positives),
            "gaps": dict(Counter(p["retrieval_diagnostics"]["gap"] for p in selected)),
        }
    result = {
        "run_id": report["run_id"],
        "planned": len(report["trials"]),
        "execution": dict(Counter(t["execution"]["status"] for t in report["trials"])),
        "assessment": dict(Counter(t["assessment"]["status"] for t in report["trials"])),
        "measurement_complete": report["complete"],
        "groups": groups,
        "provider_attempts": sum(p["usage"]["all"]["attempts"] for p in packets),
        "unknown_attempts": sum(p["usage"]["all"]["unknown_attempts"] for p in packets),
        "known_token_subtotal": sum(
            p["usage"]["all"]["known_subtotal"]["total_tokens"] for p in packets
        ),
        "rows": packets,
    }
    (output / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}, ensure_ascii=False))
    print(json.dumps({k: v for k, v in summary.items() if k not in {"rows", "binding"}}))
    if any(r["replay_errors"] or r["invariant_errors"] for r in rows):
        raise ValueError("independent-replay-failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    asyncio.run(execute(parser.parse_args()))
