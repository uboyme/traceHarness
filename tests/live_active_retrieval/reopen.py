"""Offline independent Reader replay from SQLite backups; no Provider or runtime startup."""

import argparse
import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.invariants import CoreInvariantChecker
from traceh.session.service import SessionService
from traceh.session.sqlite import SqliteEventStore
from traceh.session.surface import SurfaceProjector


def runtime_source_root():
    import traceh

    return Path(traceh.__file__).resolve().parent.parent


async def run(root, arm, copies):
    frozen = json.loads((root / "frozen.json").read_text(encoding="utf-8"))
    source_root = runtime_source_root()
    for name, digest in frozen["source_files"][arm].items():
        assert hashlib.sha256((source_root / name).read_bytes()).hexdigest() == digest, name
    results = []
    for folder in sorted((root / arm).iterdir()):
        report = json.loads((folder / "report.json").read_text(encoding="utf-8"))
        target = copies / arm / folder.name
        target.mkdir(parents=True, exist_ok=False)
        original = (folder / "events/events.sqlite3").resolve()
        with sqlite3.connect(original.as_uri() + "?mode=ro", uri=True) as src:
            with sqlite3.connect(target / "events.sqlite3") as dst:
                src.backup(dst)
        store = SqliteEventStore(target)
        try:
            sessions = SessionService(store)
            events = await sessions.read_session(report["session_id"])
            effects = await sessions.read_effects(report["session_id"])
            expected = json.loads((folder / "source-events.json").read_text(encoding="utf-8"))
            assert [e.to_dict() for e in events] == expected
            replay = await verify_request_snapshots(
                sessions, SurfaceProjector(), report["session_id"]
            )
            invariants = CoreInvariantChecker().check(events, effects)
            result = {
                "identity": folder.name,
                "requests": sum(e.type == "request/snapshot" for e in events),
                "attempts_started": sum(e.type == "model/attempt-start" for e in events),
                "attempts_ended": sum(e.type == "model/attempt-end" for e in events),
                "replay_errors": list(replay),
                "invariant_errors": [str(v) for v in invariants],
                "source_events_equal_recorded_export": True,
            }
            assert await store.head("session:" + report["session_id"]) == len(events)
            assert await store.head("effects:" + report["session_id"]) == len(effects)
            results.append(result)
        finally:
            await store.aclose()
    result = {"arm": arm, "cases": len(results), "provider_calls": 0, "results": results}
    (root / (arm + "-reopen.json")).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"arm": arm, "cases": len(results), "provider_calls": 0}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("arm", choices=("baseline", "candidate"))
    parser.add_argument("copies", type=Path)
    args = parser.parse_args()
    asyncio.run(run(args.root, args.arm, args.copies))
