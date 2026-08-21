# ADR-0019: Durable Agent identity and the Activation boundary

## Status

Accepted. Implements v0.6 Stage A only.

## Context

v0.6 introduces `AgentSupervisor` and subagents. Every earlier design note in this
repository assumed that work would begin with the Supervisor itself, but a Supervisor needs
an answer to one question before it can be written at all: **what is an Agent, and where
does that fact live?**

The tempting answer is that an Agent is the object the Supervisor is holding - an
`AgentRuntime`, a Task, or an `AgentHandle`. That answer fails the moment anything stops:

- restart the process and every Agent disappears, because the identities were the objects;
- restart one Agent and it becomes a different Agent, because a new object was built;
- crash halfway through creation and nothing can decide whether the Agent exists;
- two callers creating "the same" Agent produce two, because uniqueness was a dictionary
  key in one process.

The same reasoning already settled the equivalent question for Sessions (ADR-001: the event
log is the source of truth) and for plugin composition (ADR-0010: a Session's durable plugin
identity is rebuilt from events, never from a live Runtime field). Stage A applies it to
Agents before any live control plane exists to get it wrong.

## Decision

### 1. Identity is a durable fact; a Runtime is an Activation

An Agent exists exactly when an `agent/created` event is in the control-plane stream.

`AgentRecord` is that fact. It is rebuilt from events by `traceh.agents`, holds no Runtime,
Task or Handle, and is unchanged by anything an Activation does. An `AgentRuntime` is an
Activation: a live, in-process object that may be created, stopped and created again. The
dependency points one way only - a future Supervisor holds Activations and reads identity
from this package; nothing in `traceh.agents` may hold or observe an Activation, and
`AgentRuntime` must never learn that a Supervisor exists.

`AgentLoop` is unchanged and has no reference to any of this, per ADR-006.

### 2. A separate control-plane stream, in the same EventStore

Agent facts are appended to one stream, `agents:directory`, through the existing
`EventStore`. No JSON file, no SQLite table, no process-global dictionary.

The boundary against the Session Event Log is explicit and load-bearing:

| Stream | Answers | Written by |
|---|---|---|
| `session:<id>` | what happened while one Agent ran | `SessionService`, via `AgentLoop`, `ToolRuntime`, recovery |
| `agents:directory` | which Agents exist and which Session each owns | `AgentRegistrar` only |

They are not merged, because "which Agents exist" must not require reading every Session,
and one Agent's execution history must not be able to assert facts about another Agent. They
are not separated into different stores, because the `EventStore` contract - `expected_seq`,
the cross-process file lock, the cancellation and commit-point semantics, the event ownership
rules - is exactly what this transaction needs, and a second store would be a second fact
source with none of it.

Reusing that store means Stage A inherits, rather than reimplements, the cross-process lock
(context §6.5) and the commit-point boundary (§6.6).

### 3. Three relations, kept apart

`agent/created` records these as separate fields, and they must never be collapsed:

- `session_id` - **which history this Agent owns.** Exactly one Agent may own a Session;
- `forked_from_session_id` - **history lineage.** Where this Agent's starting context came
  from. It confers no authority over anything;
- `owner_agent_id` - **lifecycle ownership.** Which Agent is responsible for disposing this
  one.

Collapsing lineage into ownership would mean forking a Session silently grants control over
the result. Collapsing ownership into communication would mean the right to stop an Agent
and the right to talk to it are the same right, permanently.

**Communication has no field in this event at all.** A message's source is a per-message
fact. Stage A leaves the boundary clear and implements nothing: no Inbox, no `send`, no
wakeup, no delivery. When they arrive they belong on per-Agent streams, not here.

`budget` is recorded because it is part of the creation request, and dropping it would lose
information the reservation work in v0.7 needs. Nothing reserves or enforces it in Stage A.

### 4. The writer applies exactly the rules replay applies

A validation rule the write path applies more loosely than the read path is not a weaker
check. It is a channel for appending a fact that can never be read back, and in an
append-only log that damage is permanent: one `Budget(max_steps=True)` used to commit
successfully and then fail every subsequent rebuild *and* every subsequent creation for that
store forever.

So each rule has one definition, used by both sides. `validate_spec()` checks the budget
mapping that would actually be persisted rather than merely that the value is a `Budget`
instance, and rejects booleans, negatives and NaN/±Infinity - the last of which pass a
`< 0` comparison and round-trip through Python's non-strict JSON while being neither JSON
nor budgets.

Numbers are also bounded to `MAX_BUDGET_VALUE = 2**53 - 1`, the largest integer an
IEEE-754 double represents exactly. A budget is persisted as a JSON number, so anything
larger cannot be guaranteed to round-trip - and, more sharply, `10**10000` is a valid
non-negative `int` that raises a bare `OverflowError` out of `float()`/`math.isfinite()`,
escaping the fixed `agent-budget-invalid` outcome on both paths at once. The range check is
integer-only and cannot itself raise.

The same principle fixes the payload shape. Replay requires `schema_version == 1`,
`stream_id == agents:directory` and a payload key set exactly equal to the ten protocol
fields. An extra key means the writer knows something this reader does not, and reading the
remainder as a complete v1 identity would silently drop it; an identity fact read out of a
Session Stream would let one Agent's execution history assert who exists. A later change to
field meaning must raise the schema version so old projectors refuse rather than reinterpret.

### 5. Replay fails closed; the directory is not a mutable registry

`AgentDirectory.rebuild()` raises rather than repairing. A second `agent/created` for a known
`agent_id` is a contradiction in an append-only log, not an update - **last write does not
win**. The same applies to a duplicated `session_id`, a duplicated `request_id`, a malformed
payload, an unknown event type on this stream, self-ownership, and an `owner_agent_id` that
does not already exist at that point in the log.

Skipping a broken record would be worse than failing: the directory would confidently
describe an Agent set that never existed. Issue codes are stable and carry the offending
`seq`; messages are fixed repository text and never echo a payload value, because an
`agent_id` is caller-supplied and may be a mis-pasted credential.

The write path fails closed too: creation onto history that cannot be read is refused rather
than layering a second Agent set on top of an unreadable one.

### 6. The ownership boundary has two entrances

Copying on the way out does not repair a reference kept on the way in, and both entrances
carry mutable state - `metadata` is the only part of a creation request that is not already
immutable.

**Replay side.** Parsing deep-copies and normalizes the metadata graph, so a directory owns
its own graph from the input events onward. Keeping `event.data["metadata"]` itself would
mean the retained record is poisoned before any lookup happens, and the caller mutating the
envelopes it still holds would change every later answer.

**Both sides.** `to_json_value()` recurses, so a self-referential or extremely deep metadata
graph raised a bare `RecursionError` - at whatever depth this interpreter, on this platform,
in this thread happened to exhaust the stack. That made the public error contract depend on
`sys.getrecursionlimit()`. Normalization therefore performs a bounded walk first, rejecting a
container that reappears among its own ancestors and anything deeper than
`MAX_METADATA_DEPTH`, and only then encodes.

All three steps - the key scan, the bounded walk and the encode - sit inside **one**
normalization boundary, because metadata is caller-supplied and therefore *looking* at it is
untrusted work: a `dict` subclass overriding only `values()` or `__iter__` breaks traversal
while remaining perfectly encodable through `items()`. A pre-check placed outside the
boundary leaked that as a bare exception and bypassed the single outcome, on both entrances.

The same boundary covers the whole **event**, not only the payload and not only the metadata
walk. `EventEnvelope` is a public DTO that anything may construct, so `event.type`,
`event.stream_id` and `event.schema_version` are exactly as untrusted as `event.data`: a `str`
subclass with a raising `__ne__` breaks the first comparison the parser makes. `_scan()`
therefore does not pre-check the event type either, since that read would sit outside the
parser's boundary. Only `event.seq` is read outside, and plain attribute access executes
nothing. `set(data)`,
`data.get()` and `data[key]` all run against a container the store handed back, and a `dict`
subclass raising from any of them leaked a bare exception out of both `rebuild()` and
`validate_agent_directory_events()` - whose contract is to return issues rather than raise.

The boundary catches `Exception`, deliberately **not** `BaseException`. `KeyboardInterrupt`,
`SystemExit` and `CancelledError` are not verdicts about the metadata and must reach the
caller unchanged - the same rule the creation transaction follows around its append.

Because `agent_created_data()` is exported, it raises rather than substituting a value: an
`or {}` fallback conflated "rejected" with "legitimately empty" and silently discarded the
caller's metadata instead of refusing it.

**Write side.** The complete creation payload is built and deep-copied **before the first
suspension point**, and every later step - conflict checks, append, `request_id`
reconciliation - reads only that snapshot. `AgentSpec` is frozen, but a caller can keep
editing its `metadata` while the transaction is suspended in the directory read; a shallow
copy taken afterwards persists the edit. Normalization at that moment also means a value the
store cannot encode is a pre-write `AgentIdentityError` rather than an `AgentCreationError`
raised mid-transaction.

The idempotency comparison likewise runs against the frozen payload, not the live spec, and
deliberately excludes generated `agent_id`/`session_id`: a retry that did not pin them gets
fresh ids that are *expected* to differ.

### 7. Creation is a transaction with a defined uncertain outcome

Order: validate input, read the directory, check conflicts, append with
`expected_seq = directory.head_seq`.

- **The linearization point is the append's `expected_seq`.** It is what rejects a second
  writer, and it is the only part that keeps working across processes. An
  `asyncio.Lock` on the registrar closes the read-then-append window for callers sharing one
  object so ordinary concurrent creations queue instead of colliding; it is a linearization
  aid and is never consulted to decide whether a write succeeded.
- **The append carries the sequence the directory read returned**, not a freshly read head.
  Re-reading the head at append time would accept a decision whose conflict checks were run
  against history that no longer exists.
- **Retry is decided by a caller-supplied `request_id`**, which is required and has no
  default. Repeating a call with the same `request_id` returns the Agent that request already
  created. Generating one internally would defeat the purpose, since every retry would arrive
  as a new request. Reusing a `request_id` for a different identity is an error, not an
  update.
- **A failed or cancelled append is never reported as success.** `EventStore` documents a
  commit-point boundary: a cancellation landing inside the critical section raises
  `CancelledError` while the event is already durable, and there is no automatic retry. So
  "I was cancelled" does not mean "nothing was written", and the registrar looks instead of
  guessing - it re-reads the stream and searches by `request_id`, exactly as
  `PluginCompositionCoordinator` reconciles a migration authorization. Cancellation is then
  re-raised unchanged. The re-read runs in its own Task converged through
  `await_worker_convergence()`, so repeated cancellation cannot release the caller early and
  no reconciliation Task outlives the call.
- **Reconciliation matches the complete frozen fact, not the id.** The question is "did
  *this* event land". A writer racing us on the same `request_id` may have committed a
  *different* Agent, and matching on the id alone reported `committed=True` for a creation
  that never happened - handing the caller somebody else's fact as its own. The match parses
  the candidate through the projector (which re-checks stream, schema and key set) and
  compares the two payloads as **canonical JSON**, deliberately not with `==`: Python equality
  is not JSON identity, so `True == 1`, `1 == 1.0` and `[True] == [1]` all hold in Python while
  being different facts in a log, and a plain comparison let `{"flag": 1}` match a racing
  writer's `{"flag": True}`. The canonical encoder is the one request fingerprints already use,
  so "are these the same JSON" has a single definition.

  Only a `AgentDirectoryProtocolError` justifies answering "not ours": that proves the candidate is not a
  well-formed fact at all. A *canonical-encoding* failure is not a negative answer - it means
  the comparison could not be made - so it propagates and the shared reconciler reports
  `None`. Collapsing it into `False` claimed "provably not committed" about an event that was
  sitting in the stream. A malformed unrelated event is skipped
  rather than making the answer unknown: it cannot be ours either way.
- **The commit answer has three states.** `AgentCreationError.committed` is `True`, `False`
  or `None`, where `None` means the reconciling read could not answer. Collapsing unknown
  into `False` would make the strongest claim from the weakest evidence, at the exact moment
  the store is already misbehaving, and a caller acting on it would create a second Agent for
  a request that had already committed. `AgentDirectoryConflictError` promises nothing was
  written, so it is used only when the re-read positively proved that. Retrying with the same
  `request_id` is safe under all three.
- **Only `CancelledError` gets special handling.** `SystemExit`, `KeyboardInterrupt` and any
  other direct `BaseException` propagate untouched. Rewriting an interpreter-level signal
  into a domain error would make a shutdown look like a storage failure and swallow the
  interrupt.

### 8. Module placement

`src/traceh/agents/` is a new control-plane package. `AgentRecord` joins the existing frozen
DTOs in `traceh.api.agents`.

Identity projection and the ownership graph are deliberately **not** in `AgentRuntime`:
Stage D0 (ADR-0011) had already extracted one control plane out of that facade, and putting
the next one back in would repeat the mistake. They are not in `AgentLoop` either, because
they do not change the meaning of Session, Turn or Step.

## Consequences

- A fresh process holding only an `EventStore` recovers every Agent identity.
- Every lookup returns a detached `AgentRecord`. Freezing the dataclass only stops fields
  being rebound; `metadata` is an ordinary nested graph, so returning the retained object
  would let one caller write through the directory and change what every later query on it
  answers - a mutable second version of the truth inside a shared projector, even though the
  event log stayed correct. This is the same ownership rule `EventStore` and
  `SessionEventFeed` already follow, and there is deliberately no cache.
- Stopping or rebuilding an Activation cannot change an identity, and losing every handle in
  a process does not remove an Agent.
- Two Agents cannot own one Session, and one Agent cannot become another Session on replay.
- Corrupt or contradictory history is visible instead of silently reinterpreted, at the cost
  of blocking reads and writes until it is understood. That is intended for a fact source.
- Strict replay means a later identity-lifecycle event type is a deliberate extension of this
  projector, not a silently ignored addition. Communication events avoid this entirely by
  living on different streams.
- Stage A adds no user-facing command, no CLI surface, no new Session event type and no
  change to `AgentLoop`, `AgentRuntime`, `PluginManager` or any Composition path.

## Explicitly not decided here

Live `AgentSupervisor`, single-activation enforcement, Inbox and message delivery, wakeup,
`spawn_agent` and the other subagent tools, parent-first or child-first disposal,
`WorkspaceProvider` and Git worktrees, Workflow, hierarchical budget reservation, isolated
plugins, MCP, TUI and streaming. Stage A deliberately provides the identity those need
without implying any of them exists.

## Rejected alternatives

- **Identity as the live object (`AgentRuntime`, Task or `AgentHandle`).** Cannot survive a
  restart, cannot be recovered after a crash, and makes uniqueness a per-process dictionary
  key. This is the failure the ADR exists to prevent.
- **Agent facts inside each Session Stream.** Enumerating Agents would require reading every
  Session, a Session could assert facts about other Agents, and creation could not be
  linearized against a single head.
- **A separate JSON or SQLite registry.** A second fact source with none of the `EventStore`
  guarantees, and immediately in conflict with ADR-001.
- **Last-write-wins on `agent_id`.** Turns an append-only log into a mutable registry and
  makes "who is this Agent" depend on read order.
- **Repairing malformed records during replay.** Produces a confident description of an Agent
  set that never existed.
- **Validating only that `budget` is a `Budget`.** The dataclass accepts any value for its
  fields, so the type proves nothing about what lands in the log.
- **Reporting an unreadable stream as "not committed".** States the strongest conclusion from
  the weakest evidence and turns one uncertain append into two Agents.
- **Catching `BaseException` around the append.** Launders `SystemExit` and
  `KeyboardInterrupt` into a storage error.
- **Relying on `frozen=True` for record immutability.** It is shallow; the payload graph
  underneath it is not.
- **Detaching only at the read surface.** Fixes callers writing through returned records but
  not a projector that kept a reference to its input.
- **Copying the request lazily, when the payload is built.** Anything after the first
  `await` is too late; the caller has had a window to change it.
- **Leaving budget numbers unbounded.** A valid non-negative `int` can still raise
  `OverflowError` out of the float conversion and escape the stable error protocol.
- **Catching `RecursionError` without a bounded walk.** Leaves *where* a graph is rejected up
  to the interpreter's recursion limit, so the public contract varies by platform and thread.
- **Falling back to an empty value in a public helper.** Conflates "rejected" with
  "legitimately empty" and turns a refusal into silent data loss.
- **Validating a graph outside the boundary that normalizes its failures.** Traversing
  caller-supplied containers can itself raise, so the guard needs the same protection as the
  work it guards.
- **Widening that boundary to `BaseException`.** Would report an interrupt as a caller
  mistake and lose it.
- **Generating `request_id` internally.** Every retry becomes a new request, so a
  may-have-committed cancellation can be turned into two Agents.
- **Returning success when a cancelled append turns out to have committed.** Swallows a
  cancellation the caller asked for. The record is reconcilable by `request_id` instead.
