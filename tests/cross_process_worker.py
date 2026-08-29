"""Independent-process worker for the SQLite EventStore contract tests."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from traceh.api.events import PendingEvent
from traceh.session.event_store import ConcurrencyConflict
from traceh.session.sqlite import DATABASE_FILENAME, SqliteEventStore

HANDSHAKE_TIMEOUT_SECONDS = 60.0
HANDSHAKE_POLL_SECONDS = 0.005


def signal(path: str) -> None:
    Path(path).write_text("1", encoding="utf-8")


def wait_for(path: str, timeout: float = HANDSHAKE_TIMEOUT_SECONDS) -> None:
    target = Path(path)
    deadline = time.monotonic() + timeout
    while not target.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"handshake file never appeared: {target}")
        time.sleep(HANDSHAKE_POLL_SECONDS)


async def race_once(root: str, stream: str, actor: str, ready: str, start: str) -> dict:
    store = SqliteEventStore(Path(root))
    try:
        head = await store.head(stream)
        signal(ready)
        wait_for(start)
        try:
            appended = await store.append(
                stream,
                expected_seq=head,
                events=(PendingEvent("turn/start", {"actor": actor}),),
            )
        except ConcurrencyConflict:
            return {"actor": actor, "outcome": "conflict", "expected_seq": head}
        return {
            "actor": actor,
            "outcome": "appended",
            "expected_seq": head,
            "seq": appended[0].seq,
        }
    finally:
        await store.aclose()


def hold_write_lock(root: str, held: str, release: str, *, die: bool) -> dict:
    connection = sqlite3.connect(Path(root) / DATABASE_FILENAME, isolation_level=None)
    connection.execute("PRAGMA busy_timeout = 60000")
    connection.execute("BEGIN IMMEDIATE")
    signal(held)
    if die:
        sys.stdout.flush()
        os._exit(9)
    wait_for(release)
    connection.execute("ROLLBACK")
    connection.close()
    return {"held": True}


def leave_hot_foreign_journal(database: str) -> None:
    """Crash after cache spill so the copied public path owns a hot journal."""

    connection = sqlite3.connect(database, isolation_level=None)
    connection.execute("PRAGMA journal_mode = DELETE")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA cache_size = 4")
    connection.execute("PRAGMA cache_spill = ON")
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "UPDATE foreign_facts SET value = zeroblob(4096) WHERE fact_id = 1"
    )
    connection.executemany(
        "INSERT INTO foreign_facts(value) VALUES (zeroblob(4096))",
        [()] * 64,
    )
    journal = Path(f"{database}-journal")
    if not journal.exists() or journal.stat().st_size <= 512:
        raise RuntimeError("test fixture did not create a hot rollback journal")
    sys.stdout.flush()
    os._exit(9)


def leave_hot_current_journal(database: str) -> None:
    """Crash with only Event data dirty; the frozen schema remains authoritative."""

    connection = sqlite3.connect(database, isolation_level=None)
    connection.execute("PRAGMA journal_mode = DELETE")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA cache_size = 4")
    connection.execute("PRAGMA cache_spill = ON")
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "UPDATE events SET envelope_json = CAST(zeroblob(4096) AS TEXT)"
    )
    journal = Path(f"{database}-journal")
    if not journal.exists() or journal.stat().st_size <= 512:
        raise RuntimeError("test fixture did not create a hot rollback journal")
    sys.stdout.flush()
    os._exit(9)


def main(argv: list[str]) -> int:
    command, args = argv[1], argv[2:]
    if command == "race-once":
        result = asyncio.run(race_once(*args))
    elif command == "hold-write-lock":
        result = hold_write_lock(*args, die=False)
    elif command == "hold-write-lock-and-die":
        result = hold_write_lock(args[0], args[1], "", die=True)
    elif command == "leave-hot-foreign-journal":
        leave_hot_foreign_journal(args[0])
        raise AssertionError("crash worker returned")
    elif command == "leave-hot-current-journal":
        leave_hot_current_journal(args[0])
        raise AssertionError("crash worker returned")
    else:
        raise SystemExit(f"unknown worker command: {command!r}")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
