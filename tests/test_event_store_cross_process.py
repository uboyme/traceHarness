"""Cross-process CAS and bounded SQLite writer contention."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import traceh
from traceh.api.events import PendingEvent
from traceh.session.sqlite import (
    DATABASE_FILENAME,
    EventStoreBusy,
    EventStoreSchemaError,
    SqliteEventStore,
)

WORKER = Path(__file__).with_name("cross_process_worker.py")
SRC_ROOT = Path(traceh.__file__).resolve().parents[1]
HANDSHAKE_TIMEOUT_SECONDS = 60.0
HANDSHAKE_POLL_SECONDS = 0.005
WORKER_TIMEOUT_SECONDS = 120.0


class _EnteredAppendStore(SqliteEventStore):
    """Signal when the worker has crossed the public append boundary."""

    def __init__(self, root: Path, *, busy_timeout_seconds: float) -> None:
        super().__init__(root, busy_timeout_seconds=busy_timeout_seconds)
        self.entered = threading.Event()

    def _append_sync(self, *args):
        self.entered.set()
        return super()._append_sync(*args)


def spawn_worker(*args: object) -> subprocess.Popen[str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(SRC_ROOT) + (os.pathsep + existing if existing else "")
    return subprocess.Popen(
        [sys.executable, str(WORKER), *[str(arg) for arg in args]],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def finish_worker(process: subprocess.Popen[str]) -> dict:
    stdout, stderr = process.communicate(timeout=WORKER_TIMEOUT_SECONDS)
    assert process.returncode == 0, f"worker failed ({process.returncode}):\n{stderr}"
    return json.loads(stdout.strip().splitlines()[-1])


def wait_for_file(path: Path, timeout: float = HANDSHAKE_TIMEOUT_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, f"handshake file never appeared: {path}"
        time.sleep(HANDSHAKE_POLL_SECONDS)


async def test_two_processes_same_expected_seq_commit_exactly_one_batch(tmp_path) -> None:
    root = tmp_path / "events"
    creator = SqliteEventStore(root)
    await creator.aclose()
    start = tmp_path / "start"
    ready = (tmp_path / "ready-a", tmp_path / "ready-b")
    processes = [
        spawn_worker("race-once", root, "session:race", actor, marker, start)
        for actor, marker in zip(("a", "b"), ready, strict=True)
    ]
    try:
        for marker in ready:
            await asyncio.to_thread(wait_for_file, marker)
        start.write_text("go", encoding="utf-8")
        outcomes = [await asyncio.to_thread(finish_worker, process) for process in processes]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=WORKER_TIMEOUT_SECONDS)

    assert sorted(outcome["outcome"] for outcome in outcomes) == ["appended", "conflict"]
    store = SqliteEventStore(root)
    try:
        events = await store.read("session:race")
        assert len(events) == 1
        assert events[0].seq == 1
    finally:
        await store.aclose()


async def test_different_stream_writer_lock_times_out_with_stable_store_error(tmp_path) -> None:
    root = tmp_path / "events"
    creator = SqliteEventStore(root)
    await creator.aclose()
    held = tmp_path / "held"
    release = tmp_path / "release"
    process = spawn_worker("hold-write-lock", root, held, release)
    try:
        await asyncio.to_thread(wait_for_file, held)
        store = SqliteEventStore(root, busy_timeout_seconds=0.05)
        try:
            with pytest.raises(EventStoreBusy, match="event-store-busy"):
                await store.append(
                    "workflow:other",
                    expected_seq=0,
                    events=(PendingEvent("workflow/started", {"run_id": "r"}),),
                )
        finally:
            await store.aclose()
        release.write_text("go", encoding="utf-8")
        assert (await asyncio.to_thread(finish_worker, process))["held"] is True
    finally:
        release.write_text("go", encoding="utf-8")
        if process.poll() is None:
            process.kill()
            process.wait(timeout=WORKER_TIMEOUT_SECONDS)


async def test_different_stream_writer_waits_within_bound_then_commits(tmp_path) -> None:
    root = tmp_path / "events"
    creator = SqliteEventStore(root)
    await creator.aclose()
    held = tmp_path / "held"
    release = tmp_path / "release"
    process = spawn_worker("hold-write-lock", root, held, release)
    try:
        await asyncio.to_thread(wait_for_file, held)
        store = _EnteredAppendStore(root, busy_timeout_seconds=2.0)
        try:
            append_task = asyncio.create_task(
                store.append(
                    "workflow:ordinary-contention",
                    expected_seq=0,
                    events=(PendingEvent("workflow/started", {"run_id": "r"}),),
                )
            )
            assert await asyncio.to_thread(store.entered.wait, HANDSHAKE_TIMEOUT_SECONDS)
            release.write_text("go", encoding="utf-8")
            appended = await append_task
            assert [event.seq for event in appended] == [1]
        finally:
            await store.aclose()
        assert (await asyncio.to_thread(finish_worker, process))["held"] is True
    finally:
        release.write_text("go", encoding="utf-8")
        if process.poll() is None:
            process.kill()
            process.wait(timeout=WORKER_TIMEOUT_SECONDS)


async def test_process_death_releases_sqlite_writer_lock(tmp_path) -> None:
    root = tmp_path / "events"
    creator = SqliteEventStore(root)
    await creator.aclose()
    held = tmp_path / "held"
    process = spawn_worker("hold-write-lock-and-die", root, held)
    await asyncio.to_thread(wait_for_file, held)
    process.wait(timeout=WORKER_TIMEOUT_SECONDS)
    assert process.returncode == 9

    store = SqliteEventStore(root, busy_timeout_seconds=1.0)
    try:
        appended = await store.append(
            "session:after-crash",
            expected_seq=0,
            events=(PendingEvent("session/created", {"ok": True}),),
        )
        assert appended[0].seq == 1
    finally:
        await store.aclose()


def test_unknown_hot_journal_is_rejected_without_recovery_or_evidence_loss(
    tmp_path,
) -> None:
    root = tmp_path / "foreign-hot-journal"
    root.mkdir()
    database = root / DATABASE_FILENAME
    connection = sqlite3.connect(database, isolation_level=None)
    try:
        assert connection.execute("PRAGMA journal_mode = DELETE").fetchone() == (
            "delete",
        )
        connection.execute(
            "CREATE TABLE foreign_facts ("
            "fact_id INTEGER PRIMARY KEY, value BLOB NOT NULL)"
        )
        connection.execute(
            "INSERT INTO foreign_facts(value) VALUES (zeroblob(4096))"
        )
    finally:
        connection.close()

    process = spawn_worker("leave-hot-foreign-journal", database)
    process.wait(timeout=WORKER_TIMEOUT_SECONDS)
    assert process.returncode == 9
    journal = Path(f"{database}-journal")
    assert journal.exists()
    assert journal.stat().st_size > 512
    database_before = database.read_bytes()
    journal_before = journal.read_bytes()
    database_digest_before = hashlib.sha256(database_before).hexdigest()

    with pytest.raises(EventStoreSchemaError, match="event-store-schema-unsupported"):
        SqliteEventStore(root)

    assert hashlib.sha256(database.read_bytes()).hexdigest() == database_digest_before
    assert database.read_bytes() == database_before
    assert journal.read_bytes() == journal_before
    assert not (root / f"{DATABASE_FILENAME}-wal").exists()
    assert not (root / f"{DATABASE_FILENAME}-shm").exists()


async def test_current_hot_journal_recovers_only_after_exact_schema_authority(
    tmp_path,
) -> None:
    root = tmp_path / "current-hot-journal"
    store = SqliteEventStore(root)
    expected = await store.append(
        "session:recover-authorized",
        expected_seq=0,
        events=tuple(
            PendingEvent("test/event", {"value": value}) for value in range(64)
        ),
    )
    await store.aclose()
    connection = sqlite3.connect(store.path, isolation_level=None)
    try:
        assert connection.execute("PRAGMA journal_mode = DELETE").fetchone() == (
            "delete",
        )
    finally:
        connection.close()

    process = spawn_worker("leave-hot-current-journal", store.path)
    process.wait(timeout=WORKER_TIMEOUT_SECONDS)
    assert process.returncode == 9
    journal = Path(f"{store.path}-journal")
    assert await asyncio.to_thread(journal.exists)
    assert await asyncio.to_thread(lambda: journal.stat().st_size > 512)

    recovered = SqliteEventStore(root)
    try:
        assert await recovered.read("session:recover-authorized") == expected
    finally:
        await recovered.aclose()
    assert not await asyncio.to_thread(journal.exists)
