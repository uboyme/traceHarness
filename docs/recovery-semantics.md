# Recovery semantics

TraceHarness does not promise exactly-once execution for arbitrary external side
effects. It promises durable intent, explicit uncertainty and append-only lifecycle
repair.

## Crash windows

```text
Model Attempt started
  -> provider may or may not answer
  -> assistant chunks may be durable
  -> assistant message durable
  -> Model Attempt end durable

Tool Call recorded
  -> Effect Intent durable
  -> Effect dispatched
  -> world may change
  -> Effect Outcome durable
  -> Tool Result durable
```

A process can stop between any two arrows.

## Recovery algorithm

1. Load Session and Effect streams.
2. Project open Turn and Step state.
3. Find `model/attempt-start` events with no `model/attempt-end` and close each one
   (see below).
4. Find Tool Calls without Tool Results.
5. If a durable Effect Outcome exists, synthesize the missing Tool Result from it.
6. Otherwise append `effect/reconciled(status=unknown_after_crash)` when an Intent exists
   and synthesize an unknown Tool Result.
7. Close an open Step with `reason=interrupted`.
8. Close an open Turn with `reason=interrupted`.
9. If any Model Attempt, Tool Result or lifecycle repair was appended, append
   `runtime/recovered`; otherwise append nothing.

The order matters: an attempt is closed before the tool results, step and turn that
depend on it, so a repaired stream reads in the same order a healthy one would.

Recovery never deletes or rewrites an event and never automatically repeats a write or
process operation solely because its result is absent.

## Closing an unfinished Model Attempt

A started attempt says nothing about whether the provider ever answered, so evidence
decides the status. Both branches append exactly one `model/attempt-end` carrying
`recovered=true`, inheriting the start event's `correlation_id` and
`composition_revision`, and pointing `causation_id` at the start event.

| Durable evidence | Recovered status | Payload |
|---|---|---|
| At least one qualifying `assistant/message` | `succeeded` | `recovered_from=assistant/message` |
| Nothing qualifying, or only `assistant/chunk` events | `unknown_after_crash` | `error_type=RecoveredAfterCrash`, `recovered_from=none`, `partial_chunks=<count>` |

An event qualifies as evidence for an attempt only when **all** of the following hold, and
the same filter decides which chunks are counted in `partial_chunks`:

1. its `attempt_id` equals the start's `attempt_id`;
2. its `turn_id` and `step_id` equal the start's, compared by value so `1` never passes
   for `"1"`;
3. its `seq` is greater than the start's, because an event written before the attempt
   began cannot describe it.

Any single qualifying message is enough, so a wrongly scoped message followed by a correct
one is recovered as `succeeded`.

An `attempt_id` is only usable when it is a non-empty, non-blank string. `None`, numbers,
booleans, `""` and whitespace are missing identities: the start is skipped, the skip is
recorded in the report notes, and no attempt end is written. Coercing them would invent an
attempt literally called `"None"` and merge unrelated broken events into it.

Rules that hold in both branches:

- the provider is never called again;
- `usage` and `finish_reason` are never invented, because they were never observed;
- chunks are audit evidence only and are never merged into an `assistant/message`;
- an existing `assistant/message` is evidence, never something to duplicate;
- an attempt that already has an end is left untouched, so recovery is idempotent;
- several unfinished attempts converge in the order their start events were appended.

A message that carries the same `attempt_id` but a different turn or step is *not*
accepted as evidence: it cannot prove that this attempt completed.

## Repairing sessions written by an older version

Older recoveries closed the Step and Turn but left the attempt unpaired. Because the log
is append-only, the repair cannot be inserted in its historical position. Recovery
appends the missing `model/attempt-end` after the existing `step/end` and `turn/end`, and
`CoreInvariantChecker` deliberately pairs attempts over the whole stream rather than at
`step/end`, so such a session becomes invariant-clean instead of failing forever.

That late arrival is an exemption, not a free pass. A `model/attempt-end` written after its
step closed is only accepted when it carries `recovered=true` **and** its `causation_id`
points at the `event_id` of the start it repairs. A plain late end, or a `recovered=true`
end that names some other event, still violates `attempt-end-inside-step`.

## Extension point

Future tools can provide domain reconcilers. A file tool can compare hashes; a Git tool
can query a commit ID; a remote API tool can query an idempotency key. Those reconcilers
should append `effect/reconciled` evidence and still leave the original Intent intact.
