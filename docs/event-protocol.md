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
