# ADR-0035: Two-stage model admission and Session-owned dispatch permit

- Status: Accepted and implemented
- Date: 2026-08-29
- Stage: v0.8-F0

## Context

ADR-002 defines a Model Attempt as one provider request. The v0.7 execution
order did not preserve that meaning when Budget admission failed: `AgentLoop`
first persisted `request/snapshot` and `model/attempt-start`, then
`BudgetedLlmRuntime` checked capacity, reserved Tokens and possibly changed
`max_output_tokens`. A zero or insufficient Budget therefore produced durable
Attempt evidence even though the Provider was never called, while the saved
request described the composed request rather than the exact request admitted
for dispatch.

ADR-0027 also used one Step-scoped Token reservation as both a cost hold and an
anti-duplicate Provider-call guard. That was safe for one Attempt per Step, but
it cannot represent independent Attempt costs or let the Session execution
owner decide which of two concurrent owners obtained dispatch authority. Fact
idempotency in the Budget ledger is not an external-effect permit.

v0.8-F0 must correct these boundaries before retry exists. It must not add a
retry policy, another Agent loop, a second Session state machine or mutable
runtime state as a fact source.

## Decision

### 1. Model invocation has separate admission and dispatch phases

`LlmRuntime.admit()` is side-effect-free with respect to the Provider. It
returns one process-local `LlmAdmission` containing:

- the exact provider-bound `ModelRequest`;
- one host-owned `ModelAttemptIdentity`;
- the Attempt's Budget reservation identity, when Token authority is bounded.

The returned value is a concrete host dispatch capability, not an extensible
dispatch strategy. `AgentLoop` accepts only the exact `LlmAdmission` type and
requires its Provider object to be the same object resolved by the active
Composition Lease and its Attempt to equal the host-generated identity. The
base capability itself calls that bound Provider with that bound request;
Admission subclasses or a rebound Provider/Attempt are rejected before Session
evidence is written.

`BudgetedLlmRuntime.admit()` validates Agent/Session ownership, computes any
Budget output cap, creates one PENDING Attempt-scoped Token reservation and
passes the final request to the inner runtime. It does not START usage and does
not call the Provider. The inner result must retain the same concrete Provider
and Attempt binding. Budget then attaches an accounting-only lifecycle to the
concrete admission: `abort()` releases the PENDING hold, while one-shot
`dispatch()` STARTs and settles it around the host-owned Provider call. The
accounting hook never receives authority to replace the Provider or request.

An unbounded Token account has no invented reservation. The durable Attempt
records `reservation_id: null`, and replay rejects a non-null Token hold for
that account.

### 2. Session CAS, not Budget, grants dispatch authority

Each admission receives a fresh host-generated Attempt id. Its positive
ordinal belongs to the current Turn and Step. The Token reservation identity
is derived from Session, Turn, Step, Attempt id and ordinal, so two competing
admissions for the same Step hold distinct resources.

`SessionService.start_model_attempt()` is the dispatch-permit boundary. It
freshly checks the open Turn/Step, existing Attempt terminals, unique identity,
contiguous ordinal and frozen request binding under the Session writer lock.
For ordinal one it appends `request/snapshot` and `model/attempt-start` as one
EventStore batch with the observed stream head as `expected_seq`. Later
ordinals must reuse the one snapshot and both fingerprints. A CAS loss or any
scope/evidence conflict returns the stable
`model-attempt-dispatch-conflict`; the losing owner aborts its admission before
returning and never calls the Provider.

The successful durable append is the only live dispatch permit. A commit whose
return is unavailable is treated conservatively: the caller does not dispatch,
fresh Session evidence closes the Attempt in the live failure/cancellation
owner when possible, and cold recovery only closes an open Attempt. Recovery
never creates a later ordinal or calls a Provider.

The public Runtime-injection contract still requires `admit()` itself to remain
Provider-side-effect-free; arbitrary injected Python that directly performs an
unrelated network call is outside this capability contract. What the host now
enforces is that any returned admission consumed by the production loop cannot
swap the resolved Provider, Attempt or exact request at dispatch time.

### 3. One snapshot proves composition and dispatch separately

The current `request/snapshot` schema has an exact key set and contains both:

- `composed_request` and `composed_fingerprint`, independently reconstructed
  from Composition, Surface and `source_seq` by `verify_request_snapshots()`;
- `dispatch_request` and `dispatch_fingerprint`, canonicalized from the exact
  admitted request sent to the Provider.

The dispatch request must equal the composed request in every field except
that `max_output_tokens` may be lowered to a positive value. Provider and model
cannot change. Each Attempt start and end repeats ordinal, snapshot seq,
dispatch fingerprint and reservation identity; Core invariants and Budget
reconciliation fail closed on drift.

Attempt identity and reservation identity do not enter `ModelRequest.metadata`
and therefore cannot change either request fingerprint.

### 4. Lifecycle finalization remains owned by the existing loop

If admission or the Session claim fails before a permit exists, the admission
is aborted and no Attempt fact is invented. If the start batch became durable
but its return, Provider outcome or terminal append did not, `AgentLoop`
freshly closes the open Attempt before Step and Turn on both ordinary failure
and cancellation. Independent cleanup failure remains visible together with
the original failure.

The existing Budget START and settlement operations remain owned Tasks. A
cancellation racing either operation reaches a terminal RELEASED or SETTLED
ledger state before the public call returns. No background append is allowed
to outlive that return.

### 5. Terminal exception rendering is a separate narrow F0 correction

The final `traceh chat` Turn-error line now applies the existing bounded
terminal sanitizer independently to the exception type and message before
rendering one line. Newlines, control sequences, bidirectional formatting and
oversized text cannot forge a Product status or Approval line. This does not
claim that Provider exceptions are typed and safe in durable evidence; that
adapter-level contract remains v0.8-F2.

## Superseded part of ADR-0027

ADR-0027 §5 remains the historical v0.7-B decision for Token capacity,
START/SETTLED lifecycle, usage quality and cancellation convergence. Its claim
that one Step-scoped reservation is the model-call anti-duplicate guard is
superseded by this ADR. In the current protocol:

- Budget reservation is an Attempt-scoped cost hold;
- Session `model/attempt-start` CAS is the external dispatch permit;
- replaying a Budget operation never authorizes a Provider call.

Tool, wall, child and process enforcement decisions in ADR-0027 are unchanged.

## Rejected alternatives

### Keep `invoke()` and add callbacks around it

Rejected because callers still could not prove whether Budget request shaping
happened before the durable Attempt, or whether the Provider boundary had
already been crossed.

### Keep one deterministic Attempt or reservation per Step ordinal

Rejected because concurrent owners would collide in Budget admission before
Session CAS. That silently makes the ledger the execution lock and prevents
independent cost evidence for later Attempts.

### Write the snapshot and Attempt start in separate appends

Rejected because a crash between them creates a snapshot with no admitted
Attempt and complicates both ownership and retry. One EventStore batch gives
them one CAS verdict.

### Treat idempotent Budget START as a retry permit

Rejected for the same reason as ADR-0027's original warning: durable fact
idempotency does not prove that the caller owns an external side effect.

### Dispatch when the Attempt append may have committed

Rejected because an unavailable append result cannot distinguish this owner
from a concurrent winner. Dispatching would allow two paid calls. The safe
outcome is no Provider call followed by fresh evidence reconciliation.

### Put Attempt fields in request metadata

Rejected because host lifecycle identity would change the provider-bound
request bytes and make composed/dispatch reconstruction circular.

### Let Admission subclasses own Provider dispatch

Rejected because Session could validate only the subclass's declared DTO while
an override called another Provider or rewrote the request after durable start.
Budget needs lifecycle accounting, not ownership of `Provider.complete()`.

## Consequences and explicit boundaries

- A durable Model Attempt once again means that one dispatch was admitted; a
  Budget rejection leaves no false Attempt start.
- The exact request sent to a Provider is durable and independently
  reconstructable from the composed request.
- Two owners may reserve independently, but only one Session CAS winner calls
  the Provider; the loser releases its hold.
- A Runtime-returned capability cannot claim one Provider/request in durable
  evidence and use another through an Admission dispatch override.
- This forward-only schema does not read old request/Attempt shapes. v0.8-F1
  will reject legacy JSONL data rather than add an upcaster or dual reader.
- F0 adds no retry, error-category policy, Provider/model fallback, SQLite,
  TUI, Skill, Memory, second Benchmark runner or Product-specific AgentLoop.
- Raw Provider exception normalization in durable events remains an explicit
  F2 boundary; only the terminal rendering injection path is closed here.
  F2 subsequently implemented that boundary in
  [ADR-0037](0037-typed-provider-failures-and-bounded-model-retry.md) without
  changing this ADR's dispatch-permit ownership.
