# ADR-0022: Agent lifecycle ownership and quiescent subtree disposal

## Status

Accepted. Implements v0.6 Stage D only.

## Context

Stage A records `owner_agent_id` as lifecycle responsibility, and Stage C owns
process-local Activations. Until this Stage those two facts were not joined:
`dispose(parent)` stopped only the parent's Activation, so a child could retain
its worker and exclusive Runtime after its declared owner had gone away.

The fix cannot be a mutable parent/child registry. Durable identity already
lives in `agents:directory`, and another map would disagree after restart or a
concurrent create. It also cannot infer ownership from message source or
`forked_from_session_id`; communication, history lineage and lifecycle
responsibility are intentionally different relations.

## Decision

### 1. Project ownership from the durable Directory

`AgentOwnershipGraph` is rebuilt from `AgentDirectory`. It stores Agent ids and
the `owner_agent_id` edges only. It validates duplicate ids, self ownership,
unknown owners and cycles even though the Directory already rejects those
histories. Its deterministic post-order traversal returns every descendant
before its owner while preserving durable creation order among siblings.

The graph is a projection, not a new fact source. Nothing is appended when an
Activation starts or stops, and disposal never deletes an `AgentRecord`, Inbox
or delivery fact.

### 2. Admission and disposal share one process-local coordinator

Create, resume and wakeup operations take an admission lease over the target's
complete ownership lineage. A subtree disposal first registers its affected
ids, blocking new intersecting admissions. It then cancels and joins matching
in-flight create/activation candidates, waits older admissions to leave, and
rebuilds the Directory before cleanup. The second read captures a child whose
identity committed immediately before its create was cancelled.

An unpinned idempotent create retry may generate a fresh provisional Agent id
even though its `request_id` already belongs to a durable Agent. Pending-create
matching therefore also uses the durable request ids in the affected subtree;
the provisional id alone is not lifecycle identity.

Unrelated ownership trees do not share a global disposal lock. Overlapping
parent and child disposals serialize at the subtree boundary and join the same
per-Agent cleanup Task, so one Activation is never released twice.

### 3. Owners must be live at child admission

A new child requires both a durable owner record and a live, non-stopping,
non-faulted owner Activation. This check happens before provisioning and again
at Activation installation. Resuming an owned Agent has the same rule. The
lineage admission prevents its owner being disposed between those checks.

Stopping a subtree does not erase identity. A host may explicitly resume the
root and then resume its children in ownership order. A child cannot resume
first and become an orphan.

### 4. Cleanup is child-first, convergent and exhaustive

`dispose(agent_id)` now means dispose that Agent's lifecycle subtree. It uses a
shared internal tree Task; repeated caller cancellation cannot release the
caller until the Task converges, after which the original `CancelledError` is
re-raised. Each Agent still has one shared cleanup Task, so concurrent parent
and child disposal performs cleanup once.

One cleanup failure does not skip siblings or owners. Every node is attempted
in deterministic child-first order and failures are reported together only
after traversal. `aclose()` permanently closes admission, converges pending
candidate work, joins in-flight subtree disposals, then disposes the complete
durable forest child-first.

Creating the shared close Task under the Supervisor lock is also the ownership
transfer point for every tree-disposal Task still in the registry. A public
`dispose()` waiter may be cancelled after that point, but it cannot remove its
tree registration or failure evidence before close has joined the exact Task.
Close removes those registrations only after their results have been observed.
This closes the interval between shutdown admission and its later tree-task
snapshot without making the public waiter a second shutdown owner.

If the final Directory projection fails, that protocol error is retained but
does not short-circuit process-local cleanup. Close releases every known live
Activation and existing cleanup Task in a deterministic fallback order, then
reports the projection error together with any cleanup failures. The fallback
order is not described as ownership evidence because the durable graph was not
trustworthy. Close records which cleanup Tasks each in-flight tree Task joined.
It removes only those repeated Task observations from the tree aggregate, then
reports every per-Agent Task result once. Exception-object identity is not a
failure identity: two independent cleanup Tasks may raise the same object and
both failures remain visible.

`interrupt()` remains Turn cancellation only. It does not silently acquire the
stronger meaning of subtree disposal.

### 5. Keep the execution core unchanged

The ownership graph and coordinator live in `traceh.supervision`. `AgentLoop`,
`AgentRuntime`, `PluginManager`, Session projection and model request building
do not import or store lifecycle state. Future subagent tools must call the
Supervisor rather than add branches to the execution loop.

## Rejected alternatives

- **Keep a mutable owner-to-children registry in the Supervisor.** It becomes a
  second identity fact source and misses identities written by another process.
- **Dispose only children currently present in the first Directory read.** A
  child creation already admitted before disposal can commit during quiescence
  and escape.
- **Use one global read/write gate.** It is simpler but makes disposal in one
  independent ownership tree block creation and wakeup in every other tree.
- **Infer children from `forked_from_session_id` or message source.** Those are
  history and communication relations and confer no cleanup authority.
- **Stop at the first cleanup failure.** It strands siblings and owners and
  turns one bad third-party cleanup into an ownership leak.
- **Abort close when the final Directory cannot be projected.** It reports a
  real protocol failure but leaks the very process-local resources close owns.
- **Match an unpinned retry by its newly proposed Agent id only.** Its durable
  identity is the existing request fact, so a fresh UUID hides it from child
  disposal.
- **Report both a tree aggregate and its cached per-Agent failure.** They are
  two observations of one cleanup Task, not two independent failures.
- **Deduplicate globally by exception-object identity.** Third-party cleanup
  implementations can deliberately or accidentally reuse one exception object
  across independent Tasks; object identity would erase real failures.
- **Let each public `dispose()` waiter always remove its tree registration.** A
  cancelled waiter can finish after close starts but before close snapshots the
  registry, permanently erasing an in-flight failure that shutdown owns.
- **Cascade `interrupt()`.** An interrupt ends one Turn while its Activation is
  intentionally reusable; changing it into subtree shutdown would break the
  Stage C public contract.

## Consequences and boundaries

- The guarantee is process-local. Another process can still activate the same
  durable Agent; cross-process leases and stale-claim takeover remain absent.
- There is no automatic disposal on process crash, worker fault or arbitrary
  host task cancellation. Explicit `dispose()`/`aclose()` own this lifecycle.
- No model-visible `spawn_agent`, send/wait/stop/collect tool is added in this
  Stage. A host can use the Supervisor API, but the model still cannot create a
  child.
- Workspaces, hierarchical budgets, Workflow, `NEXT_STEP`, cold recovery and
  automatic retry remain later work. Version remains `0.5.0`; Stage D is not a
  v0.6 release.
