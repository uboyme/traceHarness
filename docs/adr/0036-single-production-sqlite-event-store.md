# ADR-0036: Single production SQLite EventStore

- Status: Accepted and implemented
- Date: 2026-08-29
- Stage: v0.8-F1

## Context

The JSONL EventStore made each Stream a separate file and lock. It preserved
append-only facts, but every stream read parsed its whole file, one data
directory accumulated many evidence and lock files, and cross-stream ownership,
consistent backup and schema rejection had no database boundary. v0.8 also
needs a local fact store that later UI and retrieval work can query without
inventing another source of truth.

This is a local, single-user product. Running PostgreSQL or MySQL would add a
service lifecycle, credentials, deployment and network failure modes without
improving the current authority boundary. Python's stdlib SQLite is sufficient,
but one database serializes all writers rather than locking each Stream
independently. That difference must be explicit and tested.

F1 is a breaking pre-1.0 cutover. It must not retain a JSONL reader, dual write,
automatic import, migration or fallback.

## Decision

### 1. SQLite is the only production EventStore

`SqliteEventStore` stores one database named `events.sqlite3` below the
host-selected EventStore root. `InMemoryEventStore` remains only as an explicit
deterministic test double. `JsonlEventStore` and `Durability.BATCHED` are removed;
every accepted append uses the one synchronous commit contract.

The current database identity is frozen by SQLite `application_id` plus
`user_version = 1`. The only application tables are:

- `streams(stream_id PRIMARY KEY, head_seq)`;
- `events(stream_id, seq, envelope_json, PRIMARY KEY(stream_id, seq))`, with a
  foreign key back to `streams`.

Both tables are `WITHOUT ROWID`. The full `EventEnvelope` is stored as one
canonical UTF-8 JSON text with sorted keys and compact separators. Opening a
store verifies the exact application id and schema version, the complete set of
persistent schema objects, the normalized table DDL, columns, foreign key,
SQLite integrity, every Stream head/count/contiguous sequence and every
canonical Envelope. An extra table, index, view or trigger is therefore not an
extension point: it makes the schema unsupported. Unknown, older, newer, blank,
malformed or non-canonical databases fail closed; the store does not repair
them.

### 2. One transaction owns append CAS and writer serialization

Every operation opens a short-lived SQLite connection inside the worker thread
that uses it. An existing database is first opened with URI
`mode=ro&immutable=1` and a private cache. That authority connection skips
locking, journal recovery and change detection and reads only the frozen schema
identity/objects/DDL/columns/foreign key; it does not inspect mutable history.
Only an exact-schema database may then be opened read-write, at which point
SQLite may recover its own hot journal before the Store repeats schema checks,
validates integrity and complete history, and finally enables persistent WAL.
TraceHarness never changes this schema after creation, so ordinary concurrent
Event writes cannot change the authority probe. A database rejected by that
probe therefore keeps its prior journal mode, main bytes and rollback journal
evidence, while a proven current database retains SQLite crash recovery. If a
proven-current database still fails integrity or history after
recovery, it is rejected, but the already-authorized SQLite recovery is not
claimed to be byte-neutral. Connection-local foreign-key and
`synchronous=FULL` settings apply to normal connections. The default SQLite
busy timeout is an explicit five seconds
(`DEFAULT_BUSY_TIMEOUT_SECONDS`) and may be replaced only by an explicit
positive Store constructor argument. There is no application-level retry loop.

`append(expected_seq)` uses `BEGIN IMMEDIATE`; reading the current Stream head,
inserting the entire Event batch and advancing the head happen in that one
transaction. Database uniqueness and the transaction therefore give one
cross-process linearization point. Same-head competitors cannot both commit.
Different Streams still share SQLite's single writer and may wait within the
busy bound; expiration becomes the stable `event-store-busy` availability
error, never a successful CAS or an automatic second append.

Reads are deterministically ordered and reconstruct detached Envelopes through
the canonical JSON boundary. Prefix enumeration uses a literal prefix
comparison rather than SQL `LIKE`, so `%` and `_` have no wildcard authority.
Opening history validation uses one read transaction so a concurrent valid
commit cannot be misreported as corruption.

### 3. The composition root owns Store lifetime; Runtime borrows it

`build_default_runtime()` and `build_default_runtime_async()` refuse to invent a
Store. Every production caller passes one explicitly:

- CLI `run`/`resume`, read/control commands and `chat` open one Store outside
  the Runtime, dispose the borrowing Runtime first and then close the Store;
- each Evaluation attempt owns one Store through evidence collection;
- each Evolution comparison case owns one Store through Runtime disposal and
  fact collection;
- Product child Agent Runtimes continue to borrow their host's one Store.

Build failure, ordinary failure, cancellation and cleanup failure all reach the
same ordering. An independent cleanup error is retained beside the primary
error. `aclose()` prevents new operations, waits every already admitted worker,
is idempotent and itself waits to convergence if its caller is cancelled.

SQLite calls run through `asyncio.to_thread()`. A cancelled caller cannot kill a
thread, so Store methods shield the worker, wait for that exact worker to
finish, retrieve its outcome and only then re-raise cancellation. A cancelled
append may therefore have committed. Fresh replay, not the cancellation object,
decides that fact.

### 4. Legacy evidence is refused, never consumed

If the selected root contains an old `.jsonl` or `.lock` trace, opening fails
with `event-store-legacy-jsonl-refused`. A mixed SQLite/JSONL root also fails.
The legacy bytes are not read, moved, deleted, imported or used to create a new
database. A linked/junction Store root or linked database file is rejected.
Users must select a new data directory.

This is intentionally one current schema. There is no compatibility reader or
schema migration framework in F1.

### 5. Backup and restore use SQLite's supported snapshot boundary

`backup()` uses SQLite's backup API and writes to a newly created temporary
directory beside a target that must not exist. The snapshot is validated with
the same schema, integrity and history rules before the temporary directory is
renamed to the target. `restore()` first opens and validates the backup, then
uses that same backup operation to a target that must not exist. It never
overwrites an active data directory and does not advertise raw file copy as a
consistent backup.

WAL plus `synchronous=FULL` means a normally returned SQLite commit is the
Store's durability boundary. Tests prove transaction, reopen and process-crash
behavior; they do not prove every filesystem, storage controller or sudden
power-loss guarantee. Feed publication still means only that the inner append
returned normally. Feed events may be missed on process death and are never a
second fact source.

## Rejected alternatives

### PostgreSQL or MySQL

Rejected for the current local single-user product because they introduce an
external service and credential lifecycle without a present distributed-store
requirement.

### One SQLite database per Stream

Rejected because it preserves the file proliferation and prevents one schema,
integrity and backup boundary. It would also hide SQLite's actual single-writer
contract instead of confronting it.

### Keep JSONL as a fallback or migration source

Rejected because two readers or an automatic importer create ambiguous current
facts and mutation risk. Pre-1.0 users must choose a fresh directory; old bytes
remain untouched as evidence.

### Retry `event-store-busy` in the application

Rejected because an append whose outcome is uncertain cannot safely be guessed
and repeated. SQLite owns bounded lock waiting; callers use durable identity and
fresh replay for reconciliation.

### Copy `events.sqlite3` directly

Rejected because WAL means a consistent database is not promised to be one raw
file at an arbitrary instant. The supported backup API is the only F1 backup
path.

## Consequences

- all production domains retain the same append-only EventStore fact source;
- cross-Stream writes are bounded and serialized, not independently locked;
- storage inspection and backup gain one explicit schema boundary;
- legacy v0.7 data is deliberately not readable by v0.8;
- future schema changes require a new explicit decision rather than an implicit
  compatibility layer.
