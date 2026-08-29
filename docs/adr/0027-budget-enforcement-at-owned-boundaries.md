# ADR-0027: Budget enforcement at existing owned boundaries

- Status: Accepted and implemented
- Date: 2026-08-23
- Stage: v0.7-B

## Context

ADR-0026 replaced the unenforced v0.6 identity DTO with one append-only
hierarchical Budget ledger.  A ledger alone does not stop work: child creation,
model invocation, Step continuation, Tool dispatch, active-Turn time and live
Activation slots are owned by different existing components.  Putting all of
those decisions in `AgentLoop`, `AgentRuntime` or `ProcessAgentSupervisor`
would duplicate ownership and make the single-Agent kernel or concurrency
kernel responsible for host policy.

Stage B must enforce all seven limits without creating a second scheduler,
mutable balance cache or parallel execution path.  It also has to preserve the
repository's cancellation rule: once stateful work starts, cancellation may be
reported only after that work and its reconciliation/cleanup have converged.

## Decision

### 1. One host bundle installs thin adapters

`BudgetEnforcement` owns one `BudgetLedgerService` and exposes three read-only,
identity-bound adapters:

- `BudgetedLlmRuntime` at the existing model invocation seam;
- `BudgetContinuationRuntime` at the existing continuation seam;
- `BudgetToolAdmissionGate` between ordinary Tool Policy and dispatch.

`BudgetedAgentExecution` wraps the existing four-method `AgentExecution` for
Turn preflight, active wall time and final Step reconciliation.
`BudgetedAgentSupervisor` wraps the public `AgentSupervisor` for child grants,
while `BudgetedActivationFactory` and `ProcessSlotAuthority` wrap the existing
Activation factory for process-local descendant slots.

The Runtime and Budget service must identify the same durable EventStore and
Session.  A mismatched Runtime is rejected before a Turn; a mismatched
Activation is disposed and its slot is released before the error returns.
The bundle itself is read-only after assembly.  `AgentLoop`, `AgentRuntime`,
`ProcessAgentSupervisor` and `PluginManager` own no Budget state or branch.

### 2. Child grants are one managed cross-stream saga

For a child, the wrapper freezes the request and host-resolved grant, reserves
parent capacity, calls the existing Supervisor with a deterministic child id,
then re-reads the Agent Directory.  An exact child/request/owner fact commits
the reservation; proven absence after the create operation converges releases
it; ambiguity or conflicting identity fails closed.  The wrapper serializes
this saga and close with one host lock, in addition to each stream's own CAS.
It never treats `budget/reservation-committed` as Agent identity.

The reserve append is itself owned by the saga.  Cancellation waits for that
write to reach a durable verdict.  If the reservation committed before the
inner Supervisor was called, a fresh Directory absence releases it before the
original cancellation is re-raised; repeated cancellation cannot detach this
compensation.  A child that was never provisioned therefore cannot leave a
permanent parent hold merely because cancellation raced the append return.

Fact idempotency is not a reusable creation permit.  The managed boundary
examines the reservation returned by the reserve operation before invoking the
inner Supervisor: `PENDING` permits the first creation attempt, `COMMITTED`
permits only the existing exact durable-child retry path, and `RELEASED` is a
terminal refusal.  Replaying a released reserve fact therefore fails with
`budget-reservation-state-invalid` before any Session, Activation or Directory
side effect can occur.  Otherwise a retry could persist a child after its
delegation had already been refunded and make the Ledger permanently
unreplayable.

`max_children` is cumulative and never refunded by dispose.  Token, Step,
Tool and wall grants are permanently carved from the parent.  `max_depth`
must decrease.  The same request and same grant remain idempotent; another
request cannot reuse released child or request correlation identity.

### 3. Process capacity is an explicit process-local lease

A descendant Activation acquires one slot from every ancestor whose
`max_processes` is active.  The current Agent itself is not counted.  The
authority is constructed and shared by one host composition root; there is no
module singleton and no durable claim that it is a distributed lock.
Provision/activation failure, cancellation, direct dispose and child-first
subtree/host close all converge the same lease release exactly once.

### 4. Step and wall enforcement use durable evidence and owned finalizers

Before a Turn, `BudgetContinuationRuntime` replays the Session and records each
previously unaccounted `step/start` under a deterministic operation id.  A
zero remaining Step balance refuses the Turn before `turn/start`.  After every
Step decision and in the outer Turn finalizer, the same reconciliation records
new durable Step facts; an admitted Step is never refunded.

For active wall time, the execution wrapper reserves the current remaining
milliseconds, appends a one-shot `budget/usage-started` fact, runs the existing
Turn under `asyncio.timeout()` using a monotonic clock, then settles the actual
elapsed amount capped by the reservation.  Timeout cancels the real Turn but
does not return until Provider, Tool, Turn and Budget finalizers converge.
PENDING reservations may be released before work starts; STARTED reservations
cannot be released.  A crash after STARTED therefore holds the full amount and
fails closed instead of guessing that work did not run.

The START append is also an owned Task.  If same-process cancellation arrives
after START committed but before the Turn begins, the wrapper settles the full
wall hold before re-raising the first cancellation.  This is ordinary
cancellation convergence, not crash recovery: a hard process failure can
still leave STARTED without a terminal fact.

### 5. Token calls reserve once and settle by evidence quality

> **Forward decision:** v0.8-F0 [ADR-0035](0035-two-stage-model-admission-and-session-dispatch-permit.md)
> supersedes this section's Step-scoped reservation as the anti-duplicate
> Provider-dispatch guard. Token capacity, usage quality, START/SETTLED
> lifecycle and cancellation convergence below remain in force; the current
> cost hold is Attempt-scoped and Session CAS grants dispatch authority.

One model Step derives one stable reservation and one non-idempotent START
claim.  Concurrent or recovered attempts cannot call the Provider a second
time after another owner reached STARTED or SETTLED.  A trusted `TokenCounter`
may count input and cap output before the call.  Without one, the wrapper caps
the requested output by remaining capacity, reserves the whole remainder and
does not pretend character length is a Token count.

- `EXACT` settles the reported amount when it is valid and within the hold;
- `ESTIMATED` settles the estimate only when the host explicitly opts in;
- missing, hostile, untrusted or over-reservation Usage settles the full hold
  as `UNKNOWN`;
- Provider failure, callback failure or cancellation consumes the full hold.

If cancellation races the START append return, the Provider is not invoked.
The wrapper first converges the append and, when this call acquired START,
settles the full Token hold as `UNKNOWN` before re-raising cancellation.  A
still-PENDING hold is released instead.  A host must pass an exact built-in
`bool` to opt into `ESTIMATED`; truthy strings or integers are invalid.  A
caller-supplied `LlmRuntime` is absent only when it is `None`, so a valid falsey
implementation cannot be silently replaced by the default runtime.

Settlement must use the reservation's one active dimension and may not exceed
it.  The service validates that before append and the Projector validates it
again during replay, so an invalid public settlement cannot first corrupt the
ledger and only fail on the subsequent read.  As the plan states, without a
trusted tokenizer Stage B cannot promise that one external response never
slightly exceeds a Token cap; it can consume the full hold and block the next
call honestly.

### 6. Tool admission is ordered, host-owned and conservative

`ToolRuntime` first writes `tool/call`, then performs lookup, Schema validation
and ordinary Policy.  Only the surviving calls reach `ToolAdmissionGate` as a
detached tuple in model order.  The Budget gate uses one ledger CAS to admit
and charge the largest available prefix.  Unknown, invalid, Policy-denied and
Budget-denied calls consume nothing.  An admitted call is never refunded after
failure, timeout or cancellation.

The ledger charge is the capacity linearization point; the subsequent
`tool/admitted` Session fact is the dispatch authorization and durable runtime
evidence.  If that Session append fails, no Tool is dispatched and the already
linearized capacity remains conservatively spent.  This avoids a second
cross-stream reservation protocol and is safer than refunding after an
ambiguous Session append.  Cancellation during admission or admitted-event
append converges the owned task, persists admitted evidence when it landed,
writes terminal `tool/result` facts for every uncompleted call and only then
rethrows cancellation.  A Tool already in dispatch likewise owns its
`effect/outcome` cancellation finalizer as an explicit Task; repeated
cancellation cannot detach that append before the outer batch finalizer reads
Effect evidence and writes `tool/result`.  Parallel-safe dispatch starts only
after the entire ordered admission phase is durable.

### 7. Usage reservation lifecycle is explicit

Stage B extends the Budget vocabulary with:

- `budget/usage-reserved`;
- `budget/usage-started`;
- `budget/usage-settled`;
- `budget/usage-released`.

The state machine is:

```text
PENDING -> STARTED -> SETTLED
   |
   +---------------> RELEASED
```

START is intentionally not an idempotent permission.  The exact same START
fact observed after a CAS race still means another execution owner won; a
retry receives `budget-reservation-state-invalid` rather than permission to
repeat external work.  All facts use the same global operation-id uniqueness,
canonical JSON idempotency and three-state append reconciliation as ADR-0026.

Same-process cancellation is not allowed to manufacture the hard-crash state:
the Task that owns reserve/START also owns the matching release or conservative
settlement until a terminal fact exists.  Only process death can prevent that
Python finalization path from running.

## Rejected alternatives

### Put Budget into ordinary Tool Policy

Rejected because Policy runs per call and parallel scheduling could decide who
gets the final slot.  Policy remains a pure admission decision; the dedicated
gate owns ordered, stateful capacity.

### Add Budget branches to `AgentLoop`

Rejected because child, process and wall ownership do not belong to the
single-Turn loop.  Existing seams can enforce each dimension without changing
its orchestration.

### Keep a mutable balance or Runtime state bag

Rejected because it would diverge after recovery and become a second fact
source.  Every durable verdict comes from a fresh ledger projection.

### Retry a START fact idempotently

Rejected because fact idempotency is not execution ownership.  Returning the
same START as success would authorize a second Provider call or Turn after
recovery.

### Treat a released child reserve as a fresh create permit

Rejected because release is terminal and has already returned the delegated
capacity to the parent.  The original reserve operation may still be read
idempotently, but it cannot authorize another external create; a new attempt
must use fresh child and request correlation identities.

### Refund admitted Tools or started work after failure

Rejected because external work may already have happened.  Only a PENDING
reservation, before START, may be released.

### Leave a committed START or child reserve for later recovery on cancellation

Rejected because the process is still alive and owns enough evidence to finish
the saga.  Deferring that terminalization would turn an ordinary cancellation
window into a permanent hold and make account close and deterministic retry
fail until a future crash-recovery feature exists.

### Truth-test injected Runtime and policy values

Rejected because Python truthiness is not an absence marker or a configuration
schema.  Dependency injection uses explicit `None`; evidence-policy switches
require exact booleans before any await or ledger write.

### Count Tokens from characters

Rejected because character count is neither provider Usage nor a tokenizer.
Unknown evidence consumes the reservation conservatively.

### Treat process slots as durable distributed leases

Rejected because Stage B has no cross-process Activation ownership protocol.
`ProcessSlotAuthority` states and tests only its process-local guarantee.

## Consequences and boundaries

- All seven dimensions now have explicit behavior at real owned boundaries.
- The single ledger remains the only durable balance fact source; Session and
  Directory facts are evidence joined by stable identities, not copied state.
- Host assembly is explicit.  The default CLI does not silently invent root
  grants, child grants, tokenizer policy or process authority.
- Cross-process simultaneous execution can still race Step/Tool/Token limits;
  Stage B guarantees one managed host/process, not a distributed scheduler.
- STARTED-but-unsettled usage remains held after a hard process crash and
  requires a future explicit recovery decision.  Same-process cancellation,
  including repeated cancellation around append return, converges to a
  terminal settlement before returning.
- No Workspace, Patch Artifact, Workflow, cold recovery, stale-claim takeover
  or v0.7 product CLI has been added.
- Version remains `0.6.0`; v0.7-B is implemented but unreleased.
