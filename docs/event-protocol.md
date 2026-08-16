# Event protocol

## Envelope

Every event has a globally unique ID, stream ID, monotonically increasing stream
sequence, event type, schema version, timestamp and optional causation/correlation and
Composition revision.

Core events use names such as `turn/start`. Future plugin events should use a namespace,
for example `com.example.git/commit-created`.

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

## Versioning

Every event has `schema_version`. v0.3 emits version 1. Later readers should upcast old
payloads in memory rather than rewriting historical logs.

## Manual Surface compaction

`traceh compact` appends `surface/replace` with the exact source event sequences and one
replacement message. Original events remain in JSONL and request reconstruction uses the
new Surface projection for later Steps.
