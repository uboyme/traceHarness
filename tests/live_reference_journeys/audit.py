"""Recalculate completed live reports from original SQLite and frozen request evidence."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from live_skill_navigation.run import write_json

from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.invariants import CoreInvariantChecker
from traceh.session.service import SessionService
from traceh.session.sqlite import SqliteEventStore
from traceh.session.surface import SurfaceProjector

from .assessment import assess, summarize
from .bootstrap import question
from .run import attempt_metrics


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def audit_run(root, corpus, frozen_record, frozen_root):
    report = read(root / "report.json")
    manifest = read(root / "manifest.json")
    assert report["manifest"] == manifest
    assert not manifest["diagnostic_subset"]
    assert manifest["corpus_sha256"] == digest(
        frozen_root / "tests/live_reference_journeys/corpus.json"
    )
    assert manifest["skill_corpus_sha256"] == digest(
        frozen_root / "tests/live_skill_navigation/corpus.json"
    )
    assert manifest["source_sha256"] == {
        name.removeprefix("src/traceh/"): value
        for name, value in frozen_record["files_sha256"].items()
        if name.startswith("src/traceh/") and name.endswith(".py")
    }
    for name, value in manifest["journey_helpers_sha256"].items():
        assert value == frozen_record["files_sha256"]["tests/live_reference_journeys/" + name]
    by_id = {case["id"]: case for case in corpus["cases"]}
    assert [(r["repeat"], r["case"]) for r in report["results"]] == [
        (repeat, case["id"])
        for repeat in range(corpus["acceptance"]["repeats"])
        for case in corpus["cases"]
    ]
    recalculated, evidence_hashes = [], {}
    for recorded in report["results"]:
        folder = root / f"{recorded['repeat']}-{recorded['case']}"
        assert read(folder / "result.json") == recorded, "Case result differs from report"
        streams = read(folder / "streams.json")
        store = SqliteEventStore(folder / "events")
        try:
            raw = {
                stream: [e.to_dict() for e in await store.read(stream)]
                for stream in await store.list_streams()
            }
            assert raw == streams, "SQLite/export mismatch"
            sessions, surface = SessionService(store), SurfaceProjector()
            replay, invariants = [], []
            events_by_session = {}
            for session in recorded["sessions"]:
                events = await sessions.read_session(session)
                assert all(
                    e.data["policy"]["config"] == manifest["context_policies"][recorded["case"]]
                    for e in events
                    if e.type == "context/input"
                ), "Actual Context policy differs from frozen manifest"
                events_by_session[session] = events
                replay.extend(
                    {"session_id": session, **item}
                    for item in await verify_request_snapshots(sessions, surface, session)
                )
                invariants.extend(
                    {"session_id": session, "error": str(item)}
                    for item in CoreInvariantChecker().check(events)
                )
            result = dict(recorded)
            if recorded["bootstrap_completed"] and "final_text" in recorded:
                expected = read(folder / "expected.json")
                case = by_id[recorded["case"]]
                assert expected["case"] == case
                target_events = [
                    e
                    for e in events_by_session[recorded["session_id"]]
                    if e.seq >= expected["expected"]["target_start_seq"]
                ]
                assert [e.data["content"] for e in target_events if e.type == "user/message"] == [
                    question(case)
                ]
                starts = [e for e in target_events if e.type == "turn/start"]
                ends = [e for e in target_events if e.type == "turn/end"]
                assert len(starts) == len(ends) == 1
                assert (
                    starts[0].data["turn_id"]
                    == ends[0].data["turn_id"]
                    == recorded["target_turn_id"]
                )
                final_text = [e for e in target_events if e.type == "assistant/message"][-1].data[
                    "content"
                ]
                reason = ends[0].data["reason"]
                assert final_text == recorded["final_text"], "Final response differs from SQLite"
                assert reason == recorded["reason"], "Turn outcome differs from SQLite"
                assessment = assess(
                    case,
                    expected["expected"],
                    events_by_session[recorded["session_id"]],
                    final_text,
                    reason,
                    replay,
                    invariants,
                    corpus["bounds"]["allowed_tools"],
                )
                assert all(recorded[key] == value for key, value in assessment.items())
                result.update(assessment)
            else:
                assert not recorded["task_passed"]
                result.update(replay_errors=replay, invariants=invariants)
            metrics = attempt_metrics(streams, recorded.get("target_turn_id"))
            assert metrics == recorded["attempt_metrics"]
            result["attempt_metrics"] = metrics
            recalculated.append(result)
        finally:
            await store.aclose()
        evidence_hashes[folder.name] = {
            name: digest(folder / name)
            for name in ("result.json", "expected.json", "fixture-policy.json", "streams.json")
            if (folder / name).exists()
        }
    summary = summarize(corpus, recalculated, complete=True)
    assert summary == report["summary"]
    return {
        "model": manifest["model"],
        "run": root.name,
        "summary": summary,
        "results": recalculated,
        "evidence_sha256": evidence_hashes,
        "manifest_sha256": digest(root / "manifest.json"),
        "report_sha256": digest(root / "report.json"),
    }


async def main(args):
    frozen = read(args.frozen_record)
    for name, expected in frozen["files_sha256"].items():
        assert digest(args.frozen_root / name) == expected, "Frozen input changed"
    corpus = read(args.frozen_root / "tests/live_reference_journeys/corpus.json")
    runs = [await audit_run(root, corpus, frozen, args.frozen_root) for root in args.runs]
    assert sorted(r["model"] for r in runs) == sorted(corpus["acceptance"]["models"])
    output = {
        "format": 1,
        "scope": (
            "Independent recalculation from SQLite exports and fresh original "
            "Session/replay/invariant readers; no Provider calls."
        ),
        "audit_source_sha256": digest(Path(__file__)),
        "frozen_record_sha256": digest(args.frozen_record),
        "c2_acceptance_passed": all(r["summary"]["acceptance_passed"] for r in runs),
        "runs": runs,
    }
    write_json(args.output, output)
    print(
        json.dumps(
            {
                "c2_acceptance_passed": output["c2_acceptance_passed"],
                "models": [r["summary"] for r in runs],
            }
        )
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frozen-record", type=Path, required=True)
    parser.add_argument("--frozen-root", type=Path, required=True)
    parser.add_argument("--runs", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
