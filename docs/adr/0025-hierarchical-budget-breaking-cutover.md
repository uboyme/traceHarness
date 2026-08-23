# ADR-0025: Hierarchical Budget uses a breaking pre-1.0 cutover

- Status: Accepted
- Date: 2026-08-23
- Stage: v0.7 Budget architecture / D0 decision, realized by Stage A

## Context

The v0.6 `AgentSpec.budget`/`AgentRecord.budget` value is persisted in
`agent/created`, but nothing reserves, charges or enforces it. Treating that
descriptive value as if it were an active balance would be unsafe: two children
could each observe the same parent limit, and a crash between an external
effect and an in-memory decrement would lose the charge.

TraceHarness is still pre-1.0. The project explicitly prefers one current
protocol over preserving an unused historical shape through aliases, fallback
projectors or silent migration.

## Decision

### 1. v0.7 Budget is an append-only hierarchical ledger

The Stage A protocol now defines explicit durable facts for:

- a host-issued root grant;
- a child reservation from a parent account;
- reservation commit or release;
- usage charges for tokens, Steps, Tool calls and wall time where the
  corresponding execution boundary can prove them; direct child count belongs
  only to reservations, while process slots remain process-local leases;
- terminal account state and reconciliation after an unknown append outcome.

Every mutation uses a stable host/request identity and optimistic concurrency.
Projectors derive balances; callers never mutate a balance object. A child may
consume only committed capacity reserved from its parent, so total committed
and available capacity cannot exceed the root grant.

The host chooses limits and which dimensions are active. Models may report a
need or request less capacity, but they cannot mint, raise, transfer, approve or
forgive Budget.

### 2. Reserve before crossing an owned effect boundary

Managed child creation reserves Budget before Workspace/Activation creation.
The reservation commits only after durable Agent identity can be reconciled;
otherwise it is released after candidate cleanup. Runtime enforcement will be
placed at existing owned boundaries (model attempt, Step, Tool dispatch,
process slot), not as branches in `AgentLoop`.

An unknown append result is not permission to repeat an effect. The ledger must
re-read and distinguish committed, absent and unknowable outcomes before a
caller proceeds or releases capacity.

### 3. No v0.6 Budget compatibility layer

Stage A is a breaking cutover for managed multi-Agent Budget data:

- no `LegacyBudget`, `BudgetV2`, compatibility mode, old-field alias, dual
  writer, dual projector, upcaster or automatic adoption path;
- an old v0.6 `agent/created.budget` is not an enforceable v0.7 grant;
- managed v0.7 operation uses a fresh data directory/ledger, or fails closed
  with an explicit unsupported-history error;
- the program never silently guesses limits from old data and never deletes an
  old `.traceh` directory automatically.

This decision is narrow: it does not authorize unrelated breakage to plugin,
Session, EventStore or single-Agent Runtime APIs.

### 4. D0 recorded the contract; Stage A replaced the old path

D0 added no Budget event, ledger, projector, reservation or enforcement.
Stage A has now removed `AgentSpec.budget`, `AgentRecord.budget` and the old
public DTO, advanced `agent/created` to schema version 2, and implemented the
single ledger/projector/service described in [ADR-0026](0026-append-only-hierarchical-budget-ledger.md).
Runtime enforcement still belongs to Stage B.

## Rejected alternatives

### Start enforcing the v0.6 creation DTO in place

Rejected because a copied limit is not a conserved hierarchical balance and
has no reservation, charge or crash-reconciliation protocol.

### Read both old and new Budget protocols indefinitely

Rejected because two meanings of Budget would create two fact paths, multiply
every invariant and retain unused semantics solely for compatibility the user
did not request.

### Automatically migrate or delete existing data

Rejected because guessing a grant changes authority, while deletion destroys
evidence. The safe pre-1.0 boundary is explicit refusal plus a fresh managed
data directory.

### Let a child choose its own Budget in `spawn_agent`

Rejected because model input is not authority. Budget comes from host policy
and a durable parent reservation.

### Keep balances only in Supervisor memory

Rejected because restart, concurrent processes and unknown commit outcomes
would make enforcement disagree with durable Agent identity and real effects.

## Consequences and boundaries

- Stage A may delete/replace the superseded Budget DTO path rather than wrap
  it, while preserving unrelated public contracts.
- v0.6 managed Agent histories are unsupported by the v0.7 Budget
  control plane; operators retain them as read-only evidence or choose a fresh
  data directory.
- Budget implementation has one event vocabulary, one projector and one host
  mutation service; Stage B will reuse it rather than add a second balance.
- Version remains `0.6.0`; Stage A is implemented and unreleased, not yet
  execution enforcement or a v0.7 release.
