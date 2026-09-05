# Event protocol

## Envelope

Every event has a globally unique ID, stream ID, monotonically increasing stream
sequence, event type, schema version, timestamp and optional causation/correlation and
Composition revision.

Core events use names such as `turn/start`. Future plugin events should use a namespace,
for example `com.example.git/commit-created`.

## Payload ownership

`EventEnvelope` is a frozen dataclass, which stops its fields from being rebound but does
not freeze what they point at. `data` is an ordinary JSON graph: its nested dicts and lists
are mutable, and `event.data["nested"]["value"] = ...` is legal Python. There is no
`FrozenDict`, and events are not recursively immutable.

History is therefore protected by an ownership rule rather than by the type system:
**a boundary that hands an event to someone who may edit it hands over a detached copy.**
The store boundary is `EventStore.append()` and `read()`. The rule is not automatic: an
envelope is a plain object, so anything that fans one event out to several recipients owes
each of them its own copy - see the fan-out paragraph below.

`detach_event()` rebuilds the JSON graph through `to_json_value()` and carries every other
field over by value, with no JSON text round trip, so ids and timestamps stay `UUID` and
`datetime` instead of degrading into their JSON spelling.

Payload values are normalized by the same rule that governs what a payload may contain,
and that rule is wider than `JsonValue`: `Path`, `UUID`, `datetime`, `Enum`, dataclasses,
arbitrary mappings and `Sequence` values other than `str`, `bytes` and `bytearray`, such as
`tuple`, are **converted** into their JSON form - a `tuple` becomes a `list`, a `Path`
becomes a string. Only genuinely unsupported values (`set`, `bytes`, arbitrary objects)
raise `TypeError`. This is a payload copy under the event
encoding rules, not a general deep copy.

Every `EventStore` implementation owes a caller the same guarantees:

| The caller mutates | Result |
|---|---|
| the original `PendingEvent.data` | stored history is unchanged |
| an event returned by `append()` | stored history is unchanged |
| an event returned by `read()` | stored history is unchanged; the next `read()` still shows the original fact |
| one of two `read()` results | the other is unaffected |
| the dict from `to_dict()` | the envelope is unchanged |
| the dict passed to `from_dict()` | the decoded envelope is unchanged |

Detachment covers the top-level payload, nested dicts, nested lists and dicts inside lists.
Two `PendingEvent`s that reuse one nested input object still produce independent events.
The guarantee is that stored history cannot be rewritten through a handed-out event - not
that the copy itself is immutable. A caller may freely edit its own copy.

Fan-out extends this rule rather than replacing it. `SessionEventFeed`
([`event-feed.md`](event-feed.md)) hands the same store-accepted event to several in-process
subscribers, so it calls `detach_event()` once **per subscriber**: one copy per publish
would give them a shared mutable payload. A subscriber mutating its own event changes
neither stored history, nor another subscriber's event, nor what a later subscriber
receives. Any future component that distributes one event to several recipients owes them
the same treatment.

The feed is not part of the persisted protocol: it introduces no event type, persists
nothing, replays no history and is visible only inside one process. Recovery, the inspector
and the invariant checks read the store, never the feed.

`SqliteEventStore` reaches this contract through its canonical full-envelope JSON
boundary: writes normalize before persistence and reads reconstruct a fresh graph while
checking row identity and a canonical round trip. `InMemoryEventStore` keeps the objects
it returns, so it detaches explicitly instead of exposing its internal list. `head()`
copies nothing.

Cost follows from this rather than from detachment: a `read()` returning many events costs
the total payload it parses and returns. SQLite uses `(stream_id, seq)` to locate
`from_seq`; opening a Store separately validates the full history.

## Session events

```text
session/created
inbox/accepted
inbox/claimed
turn/start
step/start
user/message
composition/snapshot
request/snapshot
model/attempt-start
assistant/chunk
assistant/message
model/attempt-end
tool/call
tool/admitted
tool/result
verification/result
step/end
turn/end
runtime/cancel-requested
runtime/error
runtime/recovered
surface/replace
surface/compaction-failed
```

## Effect events

```text
effect/intent
effect/dispatched
effect/outcome
effect/reconciled
```

## Model Attempt payloads

`model/attempt-start` carries `turn_id`, `step_id`, `attempt_id`, `provider` and `model`.
`model/attempt-end` carries the same scope plus `status`, and depending on how the attempt
finished:

| `status` | Written by | Extra fields |
|---|---|---|
| `succeeded` | AgentLoop | `finish_reason`, `usage` |
| `cancelled` | AgentLoop | `error_type=CancelledError`, `message` |
| `failed` | AgentLoop | `error_type`, `message` |
| `succeeded` | RecoveryService | `recovered=true`, `recovered_from=assistant/message`, `message` |
| `unknown_after_crash` | RecoveryService | `recovered=true`, `error_type=RecoveredAfterCrash`, `recovered_from=none`, `partial_chunks`, `message` |

Recovered ends never carry `usage` or `finish_reason`: those were never observed, so
inventing them would corrupt the audit trail. See
[`recovery-semantics.md`](recovery-semantics.md).

## Core invariants

- Sequence numbers are contiguous in the SQLite implementation.
- A Step exists inside an open Turn.
- At most one Step is open in a Session.
- A `tool/result` references an earlier call in the same Step.
- Each Tool Call receives at most one model-visible result.
- An Effect Outcome references an Effect Intent.
- A Turn closes after its Step closes.
- Request fingerprints are reconstructable from the persisted Surface boundary and
  Composition Snapshot.

Model Attempt invariants, each reported under a stable `InvariantViolation.name`:

| Name | Meaning |
|---|---|
| `attempt-id-present` | A `model/attempt-*` event carries no usable `attempt_id` |
| `single-attempt-start` | One `attempt_id` was started more than once |
| `single-attempt-end` | One `attempt_id` was ended more than once |
| `attempt-end-has-start` | An end has no earlier start with the same `attempt_id` |
| `attempt-end-same-scope` | Start and end payloads disagree about `turn_id` or `step_id` |
| `attempt-start-inside-step` | The attempt started with no open Turn/Step, or claimed a scope other than the one really open |
| `attempt-end-inside-step` | The attempt ended after its own Turn or Step was closed, without being a recognised repair |
| `single-open-attempt` | A second attempt started while one was still open in that Step |
| `attempt-has-end` | An attempt in an already closed Step never received an end |

`attempt_id` must be a non-empty, non-blank string. `None`, numbers, booleans, `""` and
whitespace are treated as missing rather than coerced, so nothing is ever tracked under an
invented id such as `"None"`.

The scope checks read the Turn and Step that are actually open at that point in the stream,
not just what the payload claims about itself.

`attempt-has-end` is evaluated over the whole stream, not at `step/end`, so an append-only
recovery that closes an attempt after the Step was already closed counts as paired. Such a
late end escapes `attempt-end-inside-step` only when it carries `recovered=true` and a
`causation_id` equal to the `event_id` of its own start. An attempt still running inside an
open Step is not a violation.

## Versioning

Every event has `schema_version`. Current event payloads use their explicitly validated
version. The pre-1.0 SQLite database has one current schema version and rejects unsupported
history; it does not upcast or rewrite legacy data.

## Surface compaction

`surface/replace` uses message format 2 and one exact key set: `format_version`, `method`
(`manual` or `automatic`), `cut_seq`, `source_seqs`, `source_digest`, `source_utf8_bytes`,
`history_utf8_bytes`, `kept_recent_turns`, `policy_digest`, `summarizer`, `summary`,
`summary_truncated` and `replacement`. An `automatic` replacement must bind both the
compaction policy digest and the summarizer identity; a `manual` one must bind neither.
The parser rebuilds the whole payload, including the message, and requires canonical
equality. Format 1 is rejected: this pre-1.0 cutover has no second parser, migration or
fallback, and older data requires a new data directory.

A cut boundary is always the sequence of a `turn/end` that really closed an open Turn, so
the current user message, an open Turn, a Step and an assistant Tool call together with its
`tool/result` are never split. `product/context-snapshot` is not a model-visible
conversation type and can never be a source.

`source_seqs` is ascending by sequence and the digest is taken over that same order, while
selection itself is by logical position; the two orders differ whenever an earlier summary
was appended after messages a wider cut now also covers.

A replacement is projected at the smallest logical position among its sources, computed
recursively, rather than at its own sequence. Original events remain in SQLite: a
`request/snapshot` frozen before a compaction still reconstructs the original history, and
a later one reconstructs the summarized history.

`CoreInvariantChecker` recomputes `source_seqs`, `source_digest`, `source_utf8_bytes` and
`history_utf8_bytes` from the history preceding the replacement and requires exact
equality, so a canonical-looking event cannot bind fabricated derived facts.

`surface/compaction-failed` records that one automatic compaction did not happen. It
carries `method`, a stable `code` and `committed` (`true`, `false` or `null` for unknown),
never history, and never reaches the model Surface. Only an exact `false` may be presented
as "history unchanged"; unknown stays unknown.

See [ADR-0042](adr/0042-host-owned-automatic-surface-compaction.md).
