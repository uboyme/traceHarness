# ADR-0026: One append-only hierarchical Budget ledger

- Status: Accepted and implemented
- Date: 2026-08-23
- Stage: v0.7-A

## Context

v0.6 persisted a caller-supplied `Budget` inside `agent/created`, but no owned
execution boundary reserved or charged it.  That shape was descriptive data,
not conserved authority.  ADR-0025 therefore requires a pre-1.0 breaking
cutover rather than a compatibility wrapper around an unenforced DTO.

Stage A needs a durable fact plane that later enforcement can trust without
putting balance state in `AgentRuntime`, `AgentLoop`, `ProcessAgentSupervisor`
or model arguments.  It must also survive concurrent writers, cancellation at
the EventStore commit point and an unreadable reconciliation attempt.

## Decision

### 1. There is one global Budget fact stream

One `EventStore` has one `budgets:ledger` stream and one schema.  Its only
facts are:

- `budget/root-granted`;
- `budget/child-reserved`;
- `budget/reservation-committed`;
- `budget/reservation-released`;
- `budget/usage-charged`;
- `budget/account-closed`.

`BudgetLedger` is rebuilt from those immutable facts plus a fresh
`AgentDirectory`.  There is no mutable balance table, per-Agent Budget stream,
second writer/projector or Runtime cache.  Every operation id, reservation id,
child id and creation request id is stable and bounded.  Operation ids are
globally unique; child and creation-request correlation identities cannot be
reused even after release.

The reader always obtains the dependent Budget prefix first and the Agent
Directory prerequisite second.  Budget root/commit facts can only depend on
Directory facts appended earlier, so this order cannot combine a new Budget
fact with an older Directory snapshot.  A newer Directory with an older
Budget prefix is safe and conservative: an unrelated Agent fact grants no
Budget authority until the corresponding ledger fact is visible.

### 2. The host issues limits; models do not carry authority

`BudgetLimits` has seven required fields.  Every durable number is an exact
built-in `int` in the JSON range `0..2**53-1`; booleans and caller-defined
integer subclasses are rejected before comparison, so hostile comparison
methods cannot escape the stable Budget input/protocol boundary.  Wall time is
measured in milliseconds.  `None` means the host deliberately did not activate
that dimension, not an omitted permissive default.

- tokens, Steps, Tool calls and wall milliseconds are conserved cumulative
  capacity.  A child grant is permanently carved from the parent, and direct
  usage consumes the same balance;
- `max_children` is cumulative direct-child capacity.  Exactly one unit is
  held by each child reservation; generic usage charges cannot count children;
- `max_depth` is a non-consumptive decreasing child constraint;
- `max_processes` is persisted as a monotonic child constraint, but process
  slots are process-local leases and are not durably consumed in Stage A.

All limit fields are required at construction.  `BudgetAmounts` contains only
the four durable usage quantities, so child creation has one accounting path.

### 3. Reserve before identity; Directory is the commit proof

A child reservation immediately holds the parent's capacity.  A reservation
is pending until the exact durable Directory fact matches child id, creation
request id and owner id.  That Directory fact is the sole child-creation
commit point and opens the child Budget account.  The optional
`budget/reservation-committed` fact is an audit acknowledgement, never a second
identity fact.

A release refunds a pending hold only after a trusted host explicitly reports
that the creation operation and cleanup converged and a fresh Directory read
contains neither the child nor its request.  Stage A deliberately does not
claim atomic absence across the separate Directory and Budget streams: an
unmanaged writer that creates the Agent after that check makes the history
contradictory and replay fails closed.  Stage B must serialize the managed
creation saga through the D0 control-plane boundary; external code that bypasses
that boundary is outside the enforced v0.7 contract.

### 4. Writes use CAS, exact idempotency and three-state reconciliation

Every mutation replays the ledger, validates the complete transition and
appends with the current Budget stream sequence.  Concurrent writers therefore
have one linearization winner.  Retrying the same operation id succeeds only
when event type and canonical JSON payload are identical; reusing it for
anything else fails.

After an append error or cancellation, reconciliation reads the durable stream
and returns exactly one of committed, absent or unknowable.  Exact canonical
JSON comparison prevents Python equality from treating `true`, `1` and `1.0`
as the same fact.  Repeated cancellation cannot release the caller while the
reconciliation read is still running.  `Exception` from untrusted event reads
is normalized; `BaseException` is not converted into a protocol verdict.

### 5. The old identity shape is deleted, not migrated

`AgentSpec.budget`, `AgentRecord.budget` and the old public `Budget` DTO are
removed.  New `agent/created` facts use schema version 2 and contain no Budget
field.  A schema-version-1 Agent history fails explicitly with
`agent-budget-history-unsupported`; there is no alias, upcaster, dual reader,
automatic grant inference or deletion of the old data.

### 6. Stage A is a fact layer, not enforcement

`BudgetLedgerService` is a host control surface.  Stage A does not change
`AgentLoop`, `AgentRuntime`, `PluginManager`, Tool schemas, the Supervisor
scheduler or CLI.  It does not reserve around managed create, charge real model
usage, stop Steps, lease process slots or expose Budget mutation to a model.
Those integrations belong to Stage B at the existing owned boundaries.
Stage B subsequently implemented those adapters and extended the usage
reservation vocabulary; see ADR-0027.  This paragraph remains the historical
Stage A boundary rather than a claim about the current repository.

## Rejected alternatives

### Keep the v0.6 DTO and add a second authoritative balance

Rejected because identity data and balance data would disagree and every
caller would have to decide which meaning of Budget to trust.

### Store mutable remaining balances

Rejected because a crash or concurrent writer can lose a decrement.  Facts
are appended; balances are projections.

### Treat `budget/reservation-committed` as child identity

Rejected because it would duplicate the Agent Directory and permit a Budget
writer to assert an Agent that the control plane never created.

### Charge child count through generic usage events

Rejected because reservation and usage would become two legal paths for the
same unit.  Child count belongs only to reservations.

### Claim atomic release against arbitrary Directory writers

Rejected because two EventStore streams do not provide a cross-stream
transaction.  The trusted managed saga is the authority boundary; replay
fails closed if an unmanaged writer violates it.

### Read Directory before the dependent Budget stream

Rejected because concurrent legal writes can then produce an old Directory
snapshot paired with a new root grant or commit, falsely classifying valid
history as corrupt.  Dependency-ordered reads provide the conservative pair
without a cache, retry loop or cross-stream mutable snapshot.

### Put enforcement branches in `AgentLoop` or state in `AgentRuntime`

Rejected because Budget is a host policy service spanning create, model, Step,
Tool and process boundaries.  The single-Agent runtime remains thin.

## Consequences and boundaries

- Stage A supplies one replayable ledger, one mutation service and stable
  public Budget values/errors.
- Pending reservations conservatively hold capacity; failed or unknown
  creation cannot silently refund it.
- Directory identity and Budget authority remain separate facts joined by
  exact stable ids.
- Existing v0.6 Agent history is preserved as evidence but is unsupported by
  the new reader.
- Version remains `0.6.0`; v0.7-A is implemented but unreleased.
- Stage B is the only next Budget stage: it composes this ledger around the D0
  managed control surface and real execution boundaries.
