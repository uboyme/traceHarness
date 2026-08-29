"""F1 contract for the single production SQLite EventStore."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from traceh.api.events import PendingEvent
from traceh.session.event_feed import PublishingEventStore, SessionEventFeed
from traceh.session.sqlite import (
    APPLICATION_ID,
    DATABASE_FILENAME,
    EventStoreClosed,
    EventStoreCorrupt,
    EventStorePathError,
    EventStoreSchemaError,
    LegacyEventStoreError,
    SqliteEventStore,
)


class AfterCommitGateStore(SqliteEventStore):
    """Keep the worker alive after SQLite committed, using explicit test gates."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.committed = threading.Event()
        self.release = threading.Event()

    def _append_sync(self, *args):
        result = super()._append_sync(*args)
        self.committed.set()
        assert self.release.wait(60), "test did not release committed worker"
        return result


class CloseEnteredStore(AfterCommitGateStore):
    """Expose when the close coroutine has begun without inspecting internals."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.close_entered = asyncio.Event()

    async def aclose(self) -> None:
        self.close_entered.set()
        await super().aclose()


async def _append(store, stream: str, value: int = 1):
    return await store.append(
        stream,
        expected_seq=0,
        events=(PendingEvent("test/event", {"value": value}),),
    )


@pytest.mark.parametrize("legacy_name", ["session%3Aold.jsonl", "session%3Aold.lock"])
def test_legacy_jsonl_is_refused_without_creating_or_changing_files(tmp_path, legacy_name) -> None:
    root = tmp_path / "events"
    root.mkdir()
    legacy = root / legacy_name
    legacy.write_bytes(b"legacy-evidence")

    with pytest.raises(LegacyEventStoreError, match="event-store-legacy-jsonl-refused"):
        SqliteEventStore(root)

    assert legacy.read_bytes() == b"legacy-evidence"
    assert sorted(path.name for path in root.iterdir()) == [legacy_name]


def test_mixed_sqlite_and_jsonl_is_refused_without_touching_either(tmp_path) -> None:
    root = tmp_path / "events"
    store = SqliteEventStore(root)
    database_before = store.path.read_bytes()
    legacy = root / "session%3Aold.jsonl"
    legacy.write_bytes(b"legacy-evidence")

    with pytest.raises(LegacyEventStoreError, match="event-store-legacy-jsonl-refused"):
        SqliteEventStore(root)

    assert legacy.read_bytes() == b"legacy-evidence"
    assert store.path.read_bytes() == database_before


@pytest.mark.parametrize(
    "application_id,version", [(APPLICATION_ID, 0), (APPLICATION_ID, 2), (7, 1)]
)
def test_unknown_older_and_newer_schema_are_all_refused(tmp_path, application_id, version) -> None:
    root = tmp_path / f"schema-{application_id}-{version}"
    store = SqliteEventStore(root)
    connection = sqlite3.connect(store.path, isolation_level=None)
    try:
        connection.execute(f"PRAGMA application_id = {application_id}")
        connection.execute(f"PRAGMA user_version = {version}")
    finally:
        connection.close()

    with pytest.raises(EventStoreSchemaError, match="event-store-schema-unsupported"):
        SqliteEventStore(root)


def test_unknown_delete_mode_database_is_rejected_without_mutation(tmp_path) -> None:
    root = tmp_path / "unknown-database"
    root.mkdir()
    database = root / DATABASE_FILENAME
    connection = sqlite3.connect(database, isolation_level=None)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        connection.execute("CREATE TABLE foreign_facts (value TEXT NOT NULL)")
        connection.execute("INSERT INTO foreign_facts(value) VALUES ('preserve-me')")
    finally:
        connection.close()
    digest_before = hashlib.sha256(database.read_bytes()).hexdigest()

    with pytest.raises(EventStoreSchemaError, match="event-store-schema-unsupported"):
        SqliteEventStore(root)

    assert hashlib.sha256(database.read_bytes()).hexdigest() == digest_before
    assert not (root / f"{DATABASE_FILENAME}-wal").exists()
    assert not (root / f"{DATABASE_FILENAME}-shm").exists()
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert connection.execute("SELECT value FROM foreign_facts").fetchall() == [
            ("preserve-me",)
        ]
    finally:
        connection.close()


async def test_extra_persistent_schema_object_is_rejected_without_mutation(tmp_path) -> None:
    root = tmp_path / "triggered-database"
    store = SqliteEventStore(root)
    await store.aclose()
    connection = sqlite3.connect(store.path, isolation_level=None)
    try:
        connection.execute(
            "CREATE TRIGGER erase_inserted_event AFTER INSERT ON events BEGIN "
            "DELETE FROM events WHERE stream_id = NEW.stream_id AND seq = NEW.seq; END"
        )
    finally:
        connection.close()
    digest_before = hashlib.sha256(store.path.read_bytes()).hexdigest()

    with pytest.raises(EventStoreSchemaError, match="event-store-schema-unsupported"):
        SqliteEventStore(root)

    assert hashlib.sha256(store.path.read_bytes()).hexdigest() == digest_before
    connection = sqlite3.connect(store.path)
    try:
        assert connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'trigger'"
        ).fetchall() == [("erase_inserted_event",)]
    finally:
        connection.close()


async def test_successful_append_matches_fresh_replay_after_reopen(tmp_path) -> None:
    root = tmp_path / "fresh-replay"
    store = SqliteEventStore(root)
    appended = await _append(store, "session:fresh-replay", value=7)
    assert await store.read("session:fresh-replay") == appended
    await store.aclose()

    reopened = SqliteEventStore(root)
    try:
        assert await reopened.read("session:fresh-replay") == appended
        assert await reopened.head("session:fresh-replay") == 1
    finally:
        await reopened.aclose()


def test_database_symlink_is_refused(tmp_path) -> None:
    external = SqliteEventStore(tmp_path / "external")
    root = tmp_path / "linked"
    root.mkdir()
    try:
        (root / DATABASE_FILENAME).symlink_to(external.path)
    except OSError as error:
        pytest.skip(f"file symlink unavailable: {error}")

    with pytest.raises(EventStorePathError, match="event-store-path-invalid"):
        SqliteEventStore(root)


def test_broken_database_symlink_is_refused_as_a_linked_path(tmp_path) -> None:
    root = tmp_path / "broken-link"
    root.mkdir()
    try:
        (root / DATABASE_FILENAME).symlink_to(tmp_path / "missing.sqlite3")
    except OSError as error:
        pytest.skip(f"file symlink unavailable: {error}")

    with pytest.raises(EventStorePathError, match="event-store-path-invalid"):
        SqliteEventStore(root)
    assert (root / DATABASE_FILENAME).is_symlink()


async def test_prefix_filter_treats_percent_and_underscore_as_plain_text(tmp_path) -> None:
    store = SqliteEventStore(tmp_path / "events")
    try:
        for stream in ("%private", "%public", "_private", "ordinary"):
            await _append(store, stream)
        assert await store.list_streams(prefix="%p") == ("%private", "%public")
        assert await store.list_streams(prefix="_p") == ("_private",)
    finally:
        await store.aclose()


async def test_many_streams_and_feed_subscribers_converge_without_store_errors(
    tmp_path,
) -> None:
    inner = SqliteEventStore(tmp_path / "events")
    feed = SessionEventFeed()
    store = PublishingEventStore(inner, feed)
    streams = tuple(f"session:{index:02d}" for index in range(24))
    subscriptions = {stream: feed.subscribe(stream) for stream in streams}
    start = asyncio.Event()

    async def writer(stream: str) -> None:
        await start.wait()
        await _append(store, stream)

    tasks = [asyncio.create_task(writer(stream)) for stream in streams]
    start.set()
    await asyncio.gather(*tasks)
    try:
        assert await inner.list_streams(prefix="session:") == streams
        for stream, subscription in subscriptions.items():
            subscription.close()
            observed = [event async for event in subscription]
            assert [(event.stream_id, event.seq) for event in observed] == [(stream, 1)]
    finally:
        for subscription in subscriptions.values():
            subscription.close()
        await inner.aclose()


async def test_cancellation_after_commit_waits_for_worker_and_fresh_replay_proves_commit(
    tmp_path,
) -> None:
    store = AfterCommitGateStore(tmp_path / "events")
    append_task = asyncio.create_task(_append(store, "session:cancel"))
    assert await asyncio.to_thread(store.committed.wait, 60)
    append_task.cancel()
    await asyncio.sleep(0)
    try:
        assert not append_task.done()
    finally:
        store.release.set()
    with pytest.raises(asyncio.CancelledError):
        await append_task
    assert await store.head("session:cancel") == 1
    assert [event.seq for event in await store.read("session:cancel")] == [1]
    await store.aclose()


async def test_close_waits_for_active_worker_and_then_rejects_every_operation(tmp_path) -> None:
    store = AfterCommitGateStore(tmp_path / "events")
    append_task = asyncio.create_task(_append(store, "session:close"))
    assert await asyncio.to_thread(store.committed.wait, 60)
    close_task = asyncio.create_task(store.aclose())
    await asyncio.sleep(0)
    try:
        assert not close_task.done()
    finally:
        store.release.set()
    await append_task
    await close_task
    assert store.closed is True

    with pytest.raises(EventStoreClosed, match="event-store-closed"):
        await store.read("session:close")
    with pytest.raises(EventStoreClosed, match="event-store-closed"):
        await store.append("session:empty", expected_seq=0, events=())
    with pytest.raises(EventStoreClosed, match="event-store-closed"):
        await store.backup(tmp_path / "closed-backup")
    await store.aclose()


async def test_cancelled_close_still_converges_active_worker_and_closes(tmp_path) -> None:
    store = CloseEnteredStore(tmp_path / "events")
    append_task = asyncio.create_task(_append(store, "session:cancel-close"))
    assert await asyncio.to_thread(store.committed.wait, 60)
    close_task = asyncio.create_task(store.aclose())
    await store.close_entered.wait()
    close_task.cancel()
    store.release.set()
    await append_task
    with pytest.raises(asyncio.CancelledError):
        await close_task
    assert store.closed is True
    await store.aclose()


async def test_backup_and_restore_are_validated_and_never_overwrite(tmp_path) -> None:
    store = SqliteEventStore(tmp_path / "events")
    await _append(store, "session:one", value=7)
    backup = tmp_path / "backup"
    receipt = await store.backup(backup)
    assert receipt.schema_version == 1
    assert receipt.database_filename == DATABASE_FILENAME
    await store.aclose()

    sentinel = tmp_path / "existing"
    sentinel.mkdir()
    (sentinel / "keep.txt").write_text("keep", encoding="utf-8")
    source = SqliteEventStore(backup)
    try:
        with pytest.raises(EventStorePathError, match="event-store-path-invalid"):
            await source.backup(sentinel)
    finally:
        await source.aclose()
    assert (sentinel / "keep.txt").read_text(encoding="utf-8") == "keep"

    restored = await SqliteEventStore.restore(backup, tmp_path / "restored")
    try:
        events = await restored.read("session:one")
        assert events[0].data == {"value": 7}
    finally:
        await restored.aclose()


async def test_restore_refuses_invalid_backup_before_creating_target(tmp_path) -> None:
    source = SqliteEventStore(tmp_path / "events")
    await _append(source, "session:one")
    backup = tmp_path / "backup"
    await source.backup(backup)
    await source.aclose()
    connection = sqlite3.connect(backup / DATABASE_FILENAME, isolation_level=None)
    try:
        connection.execute("PRAGMA user_version = 999")
    finally:
        connection.close()

    target = tmp_path / "target"
    with pytest.raises(EventStoreSchemaError, match="event-store-schema-unsupported"):
        await SqliteEventStore.restore(backup, target)
    assert not target.exists()


async def test_backup_observes_committed_history_while_append_worker_converges(
    tmp_path,
) -> None:
    store = AfterCommitGateStore(tmp_path / "events")
    append_task = asyncio.create_task(_append(store, "session:backup-race", value=9))
    assert await asyncio.to_thread(store.committed.wait, 60)
    backup = tmp_path / "backup"
    try:
        receipt = await store.backup(backup)
        assert receipt.schema_version == 1
    finally:
        store.release.set()
        await append_task
        await store.aclose()

    restored = SqliteEventStore(backup)
    try:
        events = await restored.read("session:backup-race")
        assert [event.data for event in events] == [{"value": 9}]
    finally:
        await restored.aclose()


async def test_sequence_gap_is_not_repaired(tmp_path) -> None:
    root = tmp_path / "events"
    store = SqliteEventStore(root)
    await store.append(
        "session:broken",
        expected_seq=0,
        events=(
            PendingEvent("test/event", {"value": 1}),
            PendingEvent("test/event", {"value": 2}),
        ),
    )
    await store.aclose()
    connection = sqlite3.connect(root / DATABASE_FILENAME, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "DELETE FROM events WHERE stream_id = ? AND seq = 1", ("session:broken",)
        )
    finally:
        connection.close()

    with pytest.raises(EventStoreCorrupt, match="event-store-corrupt"):
        SqliteEventStore(root)


async def test_noncanonical_envelope_is_not_rewritten_or_accepted(tmp_path) -> None:
    root = tmp_path / "events"
    store = SqliteEventStore(root)
    await _append(store, "session:noncanonical")
    await store.aclose()
    connection = sqlite3.connect(root / DATABASE_FILENAME, isolation_level=None)
    try:
        raw = connection.execute(
            "SELECT envelope_json FROM events WHERE stream_id = ? AND seq = 1",
            ("session:noncanonical",),
        ).fetchone()[0]
        noncanonical = json.dumps(json.loads(raw), ensure_ascii=False, sort_keys=False)
        connection.execute(
            "UPDATE events SET envelope_json = ? WHERE stream_id = ? AND seq = 1",
            (noncanonical, "session:noncanonical"),
        )
    finally:
        connection.close()

    with pytest.raises(EventStoreCorrupt, match="event-store-corrupt"):
        SqliteEventStore(root)
