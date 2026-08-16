# Recovery semantics

TraceHarness does not promise exactly-once execution for arbitrary external side
effects. It promises durable intent, explicit uncertainty and append-only lifecycle
repair.

## Crash windows

```text
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
3. Find Tool Calls without Tool Results.
4. If a durable Effect Outcome exists, synthesize the missing Tool Result from it.
5. Otherwise append `effect/reconciled(status=unknown_after_crash)` when an Intent exists
   and synthesize an unknown Tool Result.
6. Close an open Step with `reason=interrupted`.
7. Close an open Turn with `reason=interrupted`.
8. Append `runtime/recovered`.

Recovery never deletes or rewrites an event and never automatically repeats a write or
process operation solely because its result is absent.

## Extension point

Future tools can provide domain reconcilers. A file tool can compare hashes; a Git tool
can query a commit ID; a remote API tool can query an idempotency key. Those reconcilers
should append `effect/reconciled` evidence and still leave the original Intent intact.
