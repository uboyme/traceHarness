"""The single production SQLite event store.

The database is the append-only fact source. One transaction owns the stream
head check, event inserts, and head advance, so ``expected_seq`` remains the
linearization point across tasks, threads, and independent processes.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar
from uuid import uuid4

from traceh.api.events import EventEnvelope, PendingEvent
from traceh.concurrency import await_worker_convergence
from traceh.session.event_store import ConcurrencyConflict, Durability

_T = TypeVar("_T")

DATABASE_FILENAME = "events.sqlite3"
SCHEMA_VERSION = 1
APPLICATION_ID = 0x54524838  # "TRH8"
DEFAULT_BUSY_TIMEOUT_SECONDS = 5.0

_EVENT_KEYS = frozenset(
    {
        "event_id",
        "stream_id",
        "seq",
        "type",
        "schema_version",
        "data",
        "occurred_at",
        "causation_id",
        "correlation_id",
        "actor_id",
        "composition_revision",
    }
)

_CREATE_STREAMS_SQL = """
CREATE TABLE streams (
    stream_id TEXT PRIMARY KEY NOT NULL,
    head_seq INTEGER NOT NULL CHECK (head_seq >= 0)
) WITHOUT ROWID
"""

_CREATE_EVENTS_SQL = """
CREATE TABLE events (
    stream_id TEXT NOT NULL,
    seq INTEGER NOT NULL CHECK (seq >= 1),
    envelope_json TEXT NOT NULL,
    PRIMARY KEY (stream_id, seq),
    FOREIGN KEY (stream_id) REFERENCES streams(stream_id) ON DELETE RESTRICT
) WITHOUT ROWID
"""


class EventStoreError(RuntimeError):
    """Stable host-facing failure from the production EventStore."""

    code = "event-store-error"

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(self.code if detail is None else f"{self.code}: {detail}")


class EventStoreUnavailable(EventStoreError):
    code = "event-store-unavailable"


class EventStoreBusy(EventStoreUnavailable):
    code = "event-store-busy"


class EventStoreClosed(EventStoreUnavailable):
    code = "event-store-closed"


class EventStoreCorrupt(EventStoreError):
    code = "event-store-corrupt"


class EventStoreSchemaError(EventStoreError):
    code = "event-store-schema-unsupported"


class LegacyEventStoreError(EventStoreError):
    code = "event-store-legacy-jsonl-refused"


class EventStorePathError(EventStoreError):
    code = "event-store-path-invalid"


@dataclass(frozen=True, slots=True)
class EventStoreBackupReceipt:
    schema_version: int
    database_filename: str


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction is not None and is_junction())


def _canonical_event(event: EventEnvelope) -> str:
    return json.dumps(
        event.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_event(raw_json: object, *, stream_id: str, seq: int) -> EventEnvelope:
    if not isinstance(raw_json, str):
        raise EventStoreCorrupt("event JSON is not text")
    try:
        raw = json.loads(raw_json)
        if not isinstance(raw, dict) or frozenset(raw) != _EVENT_KEYS:
            raise ValueError("event envelope keys are invalid")
        event = EventEnvelope.from_dict(raw)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise EventStoreCorrupt("event envelope is invalid") from error
    if event.stream_id != stream_id or event.seq != seq:
        raise EventStoreCorrupt("event identity does not match its row")
    if event.occurred_at.tzinfo is None:
        raise EventStoreCorrupt("event timestamp has no timezone")
    if event.to_dict() != raw or _canonical_event(event) != raw_json:
        raise EventStoreCorrupt("event JSON is not canonical")
    return event


class SqliteEventStore:
    """SQLite-backed production EventStore with explicit lifecycle ownership.

    Connections are short-lived and created inside the worker thread that uses
    them. SQLite serializes writers across processes; the configured busy
    timeout gives ordinary contention a bounded chance to converge without an
    application-level retry loop.
    """

    def __init__(
        self,
        root: Path,
        *,
        busy_timeout_seconds: float = DEFAULT_BUSY_TIMEOUT_SECONDS,
    ) -> None:
        if busy_timeout_seconds <= 0:
            raise ValueError("busy_timeout_seconds must be positive")
        self.root = root.absolute()
        self.path = self.root / DATABASE_FILENAME
        self.busy_timeout_seconds = float(busy_timeout_seconds)
        self._busy_timeout_ms = max(1, round(self.busy_timeout_seconds * 1000))
        self._state_lock = asyncio.Lock()
        self._operations: set[asyncio.Task[object]] = set()
        self._closing = False
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._initialize()

    @property
    def closed(self) -> bool:
        return self._closed

    def _legacy_paths(self) -> tuple[Path, ...]:
        if not self.root.is_dir():
            return ()
        return tuple(sorted((*self.root.glob("*.jsonl"), *self.root.glob("*.lock"))))

    def _validate_paths(self, *, creating: bool) -> None:
        if _is_link(self.root):
            raise EventStorePathError("store root must be a real directory")
        if self.root.exists():
            if not self.root.is_dir():
                raise EventStorePathError("store root must be a real directory")
        elif not creating:
            raise EventStorePathError("store root does not exist")
        if _is_link(self.path) or (self.path.exists() and not self.path.is_file()):
            raise EventStorePathError("database must be a real file")

    def _initialize(self) -> None:
        self._validate_paths(creating=True)
        legacy = self._legacy_paths()
        database_exists = self.path.exists()
        if legacy:
            if database_exists:
                raise LegacyEventStoreError("mixed SQLite and JSONL data is refused")
            raise LegacyEventStoreError("choose a new data directory")
        if database_exists and self.path.stat().st_size == 0:
            raise EventStoreSchemaError("blank databases are not current schema")

        if not self.root.exists():
            try:
                self.root.mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                # Another process may have won creation. Validate what now
                # exists instead of treating that ordinary race as a Store
                # failure or deleting the winner's evidence.
                pass
            self._validate_paths(creating=False)

        # A concurrent creator may have populated the database after the first
        # path observation.  Reclassify it as existing before any read-write
        # connection is allowed to touch it.
        if not database_exists and self.path.exists():
            database_exists = True
            if self.path.stat().st_size == 0:
                raise EventStoreSchemaError("blank databases are not current schema")

        # SQLite checks for and repairs a hot rollback journal before its first
        # ordinary read.  An unknown database has not earned authority for that
        # write, so prove the complete persistent schema through a connection
        # that is both read-only and immutable before opening it normally.
        # TraceHarness never changes its schema after creation; pending Event
        # writes therefore cannot affect this authority probe.
        if database_exists:
            authority_connection = self._connect_for_authority()
            try:
                self._validate_schema_authority(authority_connection)
            finally:
                authority_connection.close()

        connection = self._connect(create=not database_exists)
        try:
            if not database_exists:
                self._create_or_join_schema(connection)
            self._validate_schema(connection)
            self._validate_history(connection)
            self._enable_wal(connection)
        finally:
            connection.close()
        self._validate_paths(creating=False)

    def _connect(self, *, create: bool = False) -> sqlite3.Connection:
        try:
            target: str | Path
            if create:
                target = self.path
            else:
                target = f"{self.path.as_uri()}?mode=rw&cache=private"
            connection = sqlite3.connect(
                target,
                timeout=self.busy_timeout_seconds,
                isolation_level=None,
                uri=not create,
            )
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except sqlite3.Error as error:
            raise self._translate_database_error(error) from None

    def _connect_for_authority(self) -> sqlite3.Connection:
        """Open an existing database without locking or journal recovery."""

        try:
            return sqlite3.connect(
                f"{self.path.as_uri()}?mode=ro&immutable=1&cache=private",
                timeout=self.busy_timeout_seconds,
                isolation_level=None,
                uri=True,
            )
        except sqlite3.Error as error:
            raise self._translate_database_error(error) from None

    def _create_or_join_schema(self, connection: sqlite3.Connection) -> None:
        try:
            connection.execute("BEGIN EXCLUSIVE")
            application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            has_schema_objects = (
                connection.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone()
                is not None
            )
            if application_id == APPLICATION_ID and version == SCHEMA_VERSION:
                connection.execute("COMMIT")
                return
            if application_id != 0 or version != 0 or has_schema_objects:
                raise EventStoreSchemaError("database is not current schema")
            connection.execute(_CREATE_STREAMS_SQL)
            connection.execute(_CREATE_EVENTS_SQL)
            connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.execute("COMMIT")
        except EventStoreError:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        except sqlite3.Error as error:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise self._translate_database_error(error) from None
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    def _enable_wal(self, connection: sqlite3.Connection) -> None:
        """Persist WAL only after this database has proven current and intact."""

        try:
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()
            if mode is None or str(mode[0]).lower() != "wal":
                raise EventStoreUnavailable("WAL mode is unavailable")
        except EventStoreError:
            raise
        except sqlite3.Error as error:
            raise self._translate_database_error(error) from None

    def _validate_schema_authority(self, connection: sqlite3.Connection) -> None:
        """Prove the frozen persistent schema without reading mutable rows."""

        try:
            application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if application_id != APPLICATION_ID or version != SCHEMA_VERSION:
                raise EventStoreSchemaError(
                    f"expected schema {SCHEMA_VERSION}, found application/version "
                    f"{application_id}/{version}"
                )
            objects = tuple(
                (
                    str(row[0]),
                    str(row[1]),
                    str(row[2]),
                    " ".join(str(row[3]).split()),
                )
                for row in connection.execute(
                    "SELECT type, name, tbl_name, sql FROM sqlite_schema "
                    "ORDER BY type, name"
                )
            )
            expected_objects = tuple(
                sorted(
                    (
                        (
                            "table",
                            "streams",
                            "streams",
                            " ".join(_CREATE_STREAMS_SQL.split()),
                        ),
                        (
                            "table",
                            "events",
                            "events",
                            " ".join(_CREATE_EVENTS_SQL.split()),
                        ),
                    )
                )
            )
            if objects != expected_objects:
                raise EventStoreSchemaError(
                    "database schema objects do not match current schema"
                )
            stream_columns = tuple(
                (str(row[1]), str(row[2]), int(row[3]), int(row[5]))
                for row in connection.execute("PRAGMA table_info(streams)")
            )
            event_columns = tuple(
                (str(row[1]), str(row[2]), int(row[3]), int(row[5]))
                for row in connection.execute("PRAGMA table_info(events)")
            )
            if stream_columns != (
                ("stream_id", "TEXT", 1, 1),
                ("head_seq", "INTEGER", 1, 0),
            ) or event_columns != (
                ("stream_id", "TEXT", 1, 1),
                ("seq", "INTEGER", 1, 2),
                ("envelope_json", "TEXT", 1, 0),
            ):
                raise EventStoreSchemaError("database columns do not match current schema")
            foreign_keys = connection.execute("PRAGMA foreign_key_list(events)").fetchall()
            if len(foreign_keys) != 1 or tuple(foreign_keys[0][2:7]) != (
                "streams",
                "stream_id",
                "stream_id",
                "NO ACTION",
                "RESTRICT",
            ):
                raise EventStoreSchemaError("database foreign key does not match current schema")
        except EventStoreError:
            raise
        except sqlite3.Error as error:
            raise self._translate_database_error(error) from None

    def _validate_schema(self, connection: sqlite3.Connection) -> None:
        self._validate_schema_authority(connection)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchall()
            if integrity != [("ok",)]:
                raise EventStoreCorrupt("SQLite integrity check failed")
        except EventStoreError:
            raise
        except sqlite3.Error as error:
            raise self._translate_database_error(error) from None

    def _validate_history(self, connection: sqlite3.Connection) -> None:
        owns_transaction = not connection.in_transaction
        try:
            if owns_transaction:
                # All head/count/envelope checks must observe one snapshot.
                # Otherwise a concurrent writer could commit between SELECTs
                # and make valid history look corrupt during open.
                connection.execute("BEGIN")
            streams = connection.execute(
                "SELECT stream_id, head_seq FROM streams ORDER BY stream_id"
            ).fetchall()
            for stream_id_raw, head_seq_raw in streams:
                if not isinstance(stream_id_raw, str) or not stream_id_raw:
                    raise EventStoreCorrupt("stream identity is invalid")
                if not isinstance(head_seq_raw, int) or isinstance(head_seq_raw, bool):
                    raise EventStoreCorrupt("stream head is invalid")
                rows = connection.execute(
                    "SELECT seq, envelope_json FROM events WHERE stream_id = ? ORDER BY seq",
                    (stream_id_raw,),
                ).fetchall()
                if len(rows) != head_seq_raw:
                    raise EventStoreCorrupt("stream head does not match event count")
                for expected_seq, (seq_raw, raw_json) in enumerate(rows, start=1):
                    if seq_raw != expected_seq:
                        raise EventStoreCorrupt("stream sequence is not contiguous")
                    _decode_event(raw_json, stream_id=stream_id_raw, seq=expected_seq)
            orphan = connection.execute(
                "SELECT 1 FROM events LEFT JOIN streams USING (stream_id) "
                "WHERE streams.stream_id IS NULL LIMIT 1"
            ).fetchone()
            if orphan is not None:
                raise EventStoreCorrupt("orphan event row")
            if owns_transaction:
                connection.execute("COMMIT")
        except EventStoreError:
            if owns_transaction and connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        except sqlite3.Error as error:
            if owns_transaction and connection.in_transaction:
                connection.execute("ROLLBACK")
            raise self._translate_database_error(error) from None
        except BaseException:
            if owns_transaction and connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    def _translate_database_error(self, error: sqlite3.Error) -> EventStoreError:
        message = str(error).lower()
        if "locked" in message or "busy" in message:
            return EventStoreBusy()
        if isinstance(error, sqlite3.IntegrityError):
            return EventStoreCorrupt("database constraint failed")
        return EventStoreCorrupt("SQLite operation failed")

    async def _run(self, work: Callable[[], _T]) -> _T:
        async with self._state_lock:
            if self._closing:
                raise EventStoreClosed()
            task = asyncio.create_task(asyncio.to_thread(work))
            self._operations.add(task)
            task.add_done_callback(self._operations.discard)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError as cancellation:
            await await_worker_convergence(task)
            raise cancellation

    def _append_sync(
        self,
        stream_id: str,
        expected_seq: int,
        pending_events: tuple[PendingEvent, ...],
    ) -> tuple[EventEnvelope, ...]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT head_seq FROM streams WHERE stream_id = ?", (stream_id,)
            ).fetchone()
            current_seq = 0 if row is None else int(row[0])
            if current_seq != expected_seq:
                raise ConcurrencyConflict(
                    f"stream {stream_id!r} expected seq {expected_seq}, "
                    f"current seq is {current_seq}"
                )
            if row is None:
                connection.execute(
                    "INSERT INTO streams(stream_id, head_seq) VALUES (?, 0)",
                    (stream_id,),
                )
            materialized = tuple(
                EventEnvelope.materialize(stream_id, current_seq + index, pending)
                for index, pending in enumerate(pending_events, start=1)
            )
            connection.executemany(
                "INSERT INTO events(stream_id, seq, envelope_json) VALUES (?, ?, ?)",
                tuple(
                    (event.stream_id, event.seq, _canonical_event(event)) for event in materialized
                ),
            )
            connection.execute(
                "UPDATE streams SET head_seq = ? WHERE stream_id = ?",
                (current_seq + len(materialized), stream_id),
            )
            connection.execute("COMMIT")
            return tuple(
                _decode_event(_canonical_event(event), stream_id=event.stream_id, seq=event.seq)
                for event in materialized
            )
        except ConcurrencyConflict:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        except EventStoreError:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        except sqlite3.Error as error:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise self._translate_database_error(error) from None
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    async def append(
        self,
        stream_id: str,
        *,
        expected_seq: int,
        events: tuple[PendingEvent, ...],
        durability: Durability = Durability.SYNC,
    ) -> tuple[EventEnvelope, ...]:
        if durability is not Durability.SYNC:
            raise ValueError("SQLite EventStore supports only sync durability")
        if not events:
            return await self._run(lambda: ())
        return await self._run(lambda: self._append_sync(stream_id, expected_seq, events))

    def _read_sync(self, stream_id: str, from_seq: int) -> tuple[EventEnvelope, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT seq, envelope_json FROM events "
                "WHERE stream_id = ? AND seq >= ? ORDER BY seq",
                (stream_id, from_seq),
            ).fetchall()
            return tuple(
                _decode_event(raw_json, stream_id=stream_id, seq=int(seq)) for seq, raw_json in rows
            )
        except EventStoreError:
            raise
        except sqlite3.Error as error:
            raise self._translate_database_error(error) from None
        finally:
            connection.close()

    async def read(self, stream_id: str, *, from_seq: int = 1) -> tuple[EventEnvelope, ...]:
        return await self._run(lambda: self._read_sync(stream_id, from_seq))

    def _head_sync(self, stream_id: str) -> int:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT head_seq FROM streams WHERE stream_id = ?", (stream_id,)
            ).fetchone()
            return 0 if row is None else int(row[0])
        except sqlite3.Error as error:
            raise self._translate_database_error(error) from None
        finally:
            connection.close()

    async def head(self, stream_id: str) -> int:
        return await self._run(lambda: self._head_sync(stream_id))

    def _list_streams_sync(self, prefix: str | None) -> tuple[str, ...]:
        connection = self._connect()
        try:
            if prefix is None:
                rows = connection.execute(
                    "SELECT stream_id FROM streams ORDER BY stream_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT stream_id FROM streams "
                    "WHERE substr(stream_id, 1, length(?)) = ? ORDER BY stream_id",
                    (prefix, prefix),
                ).fetchall()
            return tuple(str(row[0]) for row in rows)
        except sqlite3.Error as error:
            raise self._translate_database_error(error) from None
        finally:
            connection.close()

    async def list_streams(self, *, prefix: str | None = None) -> tuple[str, ...]:
        return await self._run(lambda: self._list_streams_sync(prefix))

    def _backup_sync(self, destination: Path) -> EventStoreBackupReceipt:
        target = destination.absolute()
        if target.exists():
            raise EventStorePathError("backup destination already exists")
        if not target.parent.is_dir() or _is_link(target.parent):
            raise EventStorePathError("backup parent must be a real existing directory")
        temporary = target.parent / f".{target.name}.traceh-backup-{uuid4().hex}"
        temporary.mkdir(exist_ok=False)
        target_database = temporary / DATABASE_FILENAME
        source_connection: sqlite3.Connection | None = None
        target_connection: sqlite3.Connection | None = None
        try:
            source_connection = self._connect()
            target_connection = sqlite3.connect(target_database, isolation_level=None)
            source_connection.backup(target_connection)
            target_connection.execute("PRAGMA foreign_keys = ON")
            mode = target_connection.execute("PRAGMA journal_mode = WAL").fetchone()
            if mode is None or str(mode[0]).lower() != "wal":
                raise EventStoreUnavailable("backup WAL mode is unavailable")
            target_connection.execute("PRAGMA synchronous = FULL")
            self._validate_schema(target_connection)
            self._validate_history(target_connection)
            target_connection.close()
            target_connection = None
            source_connection.close()
            source_connection = None
            temporary.rename(target)
            return EventStoreBackupReceipt(SCHEMA_VERSION, DATABASE_FILENAME)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        finally:
            if target_connection is not None:
                target_connection.close()
            if source_connection is not None:
                source_connection.close()

    async def backup(self, destination: Path) -> EventStoreBackupReceipt:
        return await self._run(lambda: self._backup_sync(destination))

    @classmethod
    async def restore(
        cls,
        backup_root: Path,
        target_root: Path,
        *,
        busy_timeout_seconds: float = DEFAULT_BUSY_TIMEOUT_SECONDS,
    ) -> SqliteEventStore:
        source = cls(backup_root, busy_timeout_seconds=busy_timeout_seconds)
        try:
            await source.backup(target_root)
        finally:
            await source.aclose()
        return cls(target_root, busy_timeout_seconds=busy_timeout_seconds)

    async def _finish_close(self, operations: tuple[asyncio.Task[object], ...]) -> None:
        for operation in operations:
            await await_worker_convergence(operation)
        async with self._state_lock:
            self._closed = True

    async def aclose(self) -> None:
        async with self._state_lock:
            if self._close_task is None:
                self._closing = True
                self._close_task = asyncio.create_task(self._finish_close(tuple(self._operations)))
            close_task = self._close_task
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError as cancellation:
            await await_worker_convergence(close_task)
            raise cancellation

    async def __aenter__(self) -> SqliteEventStore:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()
