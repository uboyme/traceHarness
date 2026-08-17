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

`JsonlEventStore` needs no store-specific `detach_event()` call, because its history lives
in the file and both directions already pass through the shared `EventEnvelope`
serialization boundary. That boundary still rebuilds the payload: `read()` runs
`json.loads()` and then `from_dict()` normalizes the result into a fresh graph, and
`append()` has `to_dict()` rebuild the payload before serializing it. So the copying is
reached through serialization, not avoided. `InMemoryEventStore` keeps the objects it
returns, so it detaches explicitly instead of exposing its internal list. `head()` copies
nothing.

Cost follows from this rather than from detachment: a copy is one event payload, so a
`read()` returning many events costs the total payload of what it parses and returns. In
`JsonlEventStore` that is the whole stream - `from_seq` filters after the full parse rather
than seeking - which is the pre-existing JSONL full-scan boundary.

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

- Sequence numbers are contiguous in the JSONL implementation.
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

Every event has `schema_version`. v0.3 emits version 1. Later readers should upcast old
payloads in memory rather than rewriting historical logs.

## Manual Surface compaction

`traceh compact` appends `surface/replace` with the exact source event sequences and one
replacement message. Original events remain in JSONL and request reconstruction uses the
new Surface projection for later Steps.
