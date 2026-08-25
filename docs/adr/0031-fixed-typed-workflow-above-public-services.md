# ADR-0031: A fixed typed Workflow above the public services

- Status: Accepted and implemented
- Date: 2026-08-25
- Stage: v0.7-E

## Context

v0.7 finished five independent domains: hierarchical Budgets (ADR-0026/0027),
managed Git Workspaces (ADR-0028), immutable Patch Artifacts (ADR-0029) and
verified, human-approved, compare-and-swap promotion (ADR-0030). Each owns one
fact source and one public service.

What is still missing is the sentence that joins them: *run this coder, fan out
over these areas, wait for all of them, verify the result, stop for a human, and
only then continue.* Today a host must write that by hand, and every host would
invent its own retry, its own idea of "done" and its own place to keep progress.

ADR-0024 already fixed the shape of the answer: workflow nodes call the public
services and persist orchestration facts; they do not reach into the Supervisor,
Runtime or plugin internals. This ADR implements that and records where the
boundaries are.

## Decision

### 1. A fixed typed DAG, not a workflow language

There are exactly five node kinds - AgentTask, Map, Join, Verification and
Approval - with no expressions, conditions, loops, retries or user-supplied
callables. `traceh.api.workflow` holds frozen, slotted values; `traceh.workflow`
holds identities and hashing (`models.py`), the event vocabulary (`events.py`),
the single projector (`projection.py`), the five executors (`execution.py`) and
the coordinator (`service.py`).

A general DSL was rejected deliberately. An expression language would need its
own evaluator, its own sandbox and its own threat model, and every one of those
would sit directly above services that create Agents and move Git refs. A fixed
DAG can be validated completely before anything runs: duplicate ids, unknown
predecessors, self-edges, cycles, unreachable nodes, bounded node/predecessor
counts and bounded fan-out are all decidable up front.

### 2. The definition carries no policy, only binding ids

A node names `spec_binding`, `message_binding` or `keys_binding` - host registry
keys - never an `AgentSpec`, a prompt, a repository path, a command environment
or a Python object. A host-owned `WorkflowBindingResolver` turns a key into a
value at run time.

This is what keeps the durable definition safe to read later: there is nothing
in it a future reader would have to trust. `workflow_definition_hash()` covers
every decision-bearing field through canonical JSON, so `True` and `1` are
different definitions, and a run is bound to that hash rather than to a name.

### 3. One append-only stream owns orchestration, and nothing else

`workflow:<run_id>` carries seven facts at schema 1: `run-started`,
`node-started`, `map-expanded`, `node-completed`, `node-failed`,
`approval-awaited` and `run-finished`. One projector rebuilds the run on every
load; there is no status file, no result cache and no second store.

The stream records *orchestration* only. Whether an Agent exists, what its
report said, which bytes the Patch holds, what the Review proved and who
approved it all remain owned by the Directory, the Session, the Artifact
Catalog and the promotion ledger. The Workflow keeps identities that point at
those facts, never copies of them.

Replay recomputes derived values instead of trusting the payload: map child ids
are re-derived from the parent and key, and a forged child id is rejected. An
event whose key set, schema, type, sequence or ordering is wrong is refused
rather than migrated.

A terminal fact reports how a node *ended*; it may not redefine what the node
*was*. Its kind and map key must equal the ones the node started with, and the
projector keeps the started values rather than the payload's - otherwise a
started AgentTask could finish as a Join carrying a foreign Artifact, and every
later reader, including Verification, would believe it.

A completion must also carry the evidence its own node produces, and only that.
This is one rule applied at two depths, and the second does not replace the
first.

`rebuild()` enforces everything a stream can decide by itself: a Join carries no
Artifact, Review or digest; a Verification carries both an Artifact and a Review;
an Approval adds the digest; an Agent-running node names an Agent and a message,
and those ids are recomputed from the run and node rather than merely required to
be present. This layer has to stand alone, because `rebuild()` and
`WorkflowStreamReader.load()` are public and take no definition - anyone who only
replays a stream must still be refused a malformed terminal.

`run(definition)` then adds the one thing a stream cannot know: whether a task was
asked to capture. `capture_artifact` is a property of the individual node, so a
kind-only rule would accept both a task that captured nothing while holding
someone else's Artifact and one that was asked to capture and produced none. A
Map child follows its parent's setting.

Fan-out closes the same way, and most of it belongs to replay. A stream alone can
say that only a *running Map parent* records an expansion - so a Join cannot
record one and then report map keys it never produced - that a child id and its
key correspond exactly, that a node nobody expanded carries no key at all, and
that one child belongs to one expansion and never appears before the fan-out that
created it. All four are enforced in `rebuild()`.

The definition layer keeps the part only it can decide: whether the node behind
an expansion is a Map *in this definition*, and whether a node the definition
separately declares is allowed to carry a key at all - a definition may declare a
node whose id happens to equal a real child id, and replay has no way to know
that.

Interpreting a run against a definition adds two further layers. Every node the
stream describes must be declared by that definition, or be a map child of a node
that really is a Map, with the kind the definition gives it. And the converse
must hold as well: a run may report `completed` only when every declared node,
plus every child of every expanded Map, is durably completed. Without that, a
lone `run-finished(completed)` would be enough to claim success for a DAG that
never ran.

Because that interpretation can be wrong, reads are checked too. `state()`
refuses a definition whose hash is not the one the run recorded, exactly as
`start()` and `resume()` do; pairing a real run with someone else's definition
would otherwise report node kinds and outcomes that run never agreed to.

### 4. Stable identity is what makes re-entry safe

Every side-effecting call derives its identity from the run and node:

```text
agent id / session id / create request = hash(run_id, node_id, "create")
message id                            = hash(run_id, node_id, "message")
review request id                     = hash(run_id, node_id, "review")
map child node id                     = hash(parent_node_id, canonical_key)
```

Scheduling order never contributes. A second attempt therefore addresses exactly
the Agent and message the first attempt used, and each service recognises it as
the same operation.

Re-entry is a *fresh read*, not a repeat: the executor replays the Agent
Directory to decide whether to `create` or `resume`, and replays the Inbox to
decide whether the message still needs sending. Artifact capture is already
idempotent per `(agent, message)`. Map keys are canonically sorted before
expansion, so two runs of one definition produce the same children regardless of
how the host built the collection.

Matching the identity alone is not sufficient, because a derived identity is
predictable and therefore occupiable. Before adopting an existing Agent or an
accepted message, the node requires the durable fact to be the whole request it
would itself have made. That comparison is not written here: it goes through the
protocols' own `creation_matches()` and `acceptance_matches()`, so every field
that defines the operation participates - including `capability_grants`, which
decides what the adopted Agent may do, and `target`/`wakeup`, which are delivery
semantics an `AgentMessage` comparison cannot see. A message with identical text
accepted without the required wake-up is not this node's operation. A record that
merely occupies the identity is refused rather than adopted.

`workspace_id` is deliberately excluded from that comparison. A
workspace-managing Supervisor rewrites the spec's intent id into the managed
catalog id, so the durable value is not the one the node asked for. Workspace
assignment belongs to that layer, and the Workflow does not assert facts it does
not own.

### 5. Not a second scheduler

The coordinator derives ready nodes from durable facts, calls one public service
per node, runs independent ready nodes concurrently, and appends what happened.
Agent FIFO order, Turn execution, Activation lifetime and close all stay in the
existing Supervisor. There is no second Activation table, Inbox, delivery log or
Agent queue, and the Workflow reads no private Supervisor state - an
architecture test asserts that by name.

A Map's successors wait for its *children*, not merely for its expansion, so a
fan-out is genuinely joined. The expansion is appended before any child starts,
so a later reader derives the same children this run executed.

### 6. Approval is a barrier the Workflow cannot cross by itself

When an Approval node becomes ready, the Workflow appends `approval-awaited` and
stops. It never calls `approve()`; an architecture test asserts that no call to
`approve`, `promote` or `compare_and_swap` exists anywhere in the domain.

Continuing requires a human to have recorded the approval through the promotion
service. The node then re-reads the promotion ledger and requires the approval to
cover *this* review, whose digest is recomputed from the review's own content and
whose artifact matches what this run captured. Finding *an* approval is not
enough. Nothing after the barrier may start until that check passes.

### 7. One narrow recovery point, everything else fails closed

v0.7-E implements no general crash recovery. Exactly one interrupted run may be
continued: one that stopped cleanly at a human Approval barrier.

A node with a start fact and no terminal fact is refused
(`workflow-node-still-running`). That state could mean an open Agent delivery
claim, an open Turn or Step, a pending or started Budget usage, an unreleased
process slot, a provisional Workspace, a running capture or a running Review -
and nothing in the stream distinguishes them. Guessing would risk repeating
external work. Stale-claim takeover, automatic retry, cold Activation recovery,
cross-process leases and retry policy are explicitly out of scope.

### 8. Cancellation, failure and cleanup keep every fact

Every composed service must write to the one durable log the run uses, resolved
through the same `durable_log_identity` helper the Budget, Artifact and Promotion
domains already use. Splitting them would produce two histories that cannot be
checked against each other - the Workflow recording that a node created an Agent
while the Agent facts live somewhere this run can never replay.

A run that fails records `run-finished(failed)` *before* the node errors reach
the caller. Leaving the run durably `running` would let a later `resume()`
supply the missing terminal itself, which is indistinguishable from a legitimate
continuation and would quietly widen the recovery rule above. A *cancelled* node
is different: it keeps its start fact and gains no terminal, so the run stays
un-continuable and no run terminal is written.

If that terminal cannot be written, the caller still learns why the nodes failed.
Both failures are composed through the shared rule rather than letting the
bookkeeping error replace the root cause - a run whose real failure is invisible
behind an append error is worse than one that simply failed.

One run has one owned single-flight task. A cancelled caller waits for that same
task and receives its original `CancelledError`; repeated cancellation cannot
release it early. Independent ready nodes run concurrently and every one of them
is converged before the caller returns, so one node's failure never leaves a
sibling still touching an Agent, a Workspace or a Git repository.

Failures are collected *per node*, so two nodes raising the same exception
object still count as two independent failures, and a node observed through two
join paths is reported once. The composition rule for "several things failed at
once" is the one already written for D2, now shared in `traceh.concurrency`
(`combine_failures`, `informative_failure`) rather than copied with variations.

Stream appends use the existing three-state `committed_after_failure()`
reconciliation: a failed or cancelled append is never assumed absent.

## Consequences

- A host can express a Reviewer/Coder pipeline once, and the durable record of
  what happened is replayable and bounded.
- The v0.6 concurrency kernel and the four protected files gain nothing.
- A failed Verification is a durable fact but cannot flow onward as success.
- A node's Activation is closed when it ends, but its managed Workspace is not:
  a worktree outlives the Agent that used it because the Patch it holds is still
  evidence. Releasing it stays an explicit host decision, and Stage E does not
  make it one.
- Because recovery is narrow, an interrupted mid-node run requires human
  inspection. That is the honest cost of not guessing.

## Rejected alternatives

- **A general workflow DSL.** An evaluator above Agent creation and Git
  promotion needs its own threat model; a fixed DAG can be fully validated first.
- **Put the resolved spec, prompt or path in the definition.** The durable
  record would then carry policy a later reader must trust.
- **Let the Workflow own an Activation table or queue.** Two schedulers could
  both claim one durable Agent; ADR-0024 already refused this.
- **Derive child identity from scheduling order.** Re-entry would address
  different children than the first attempt.
- **Start map children before the expansion is durable.** A later reader would
  derive children this run never executed.
- **Let the Workflow approve, or treat any approval as sufficient.** Approval is
  a human fact about exact content, not a step a coordinator may take.
- **Recover any interrupted run.** A started node with no terminal fact may have
  left an Agent claim, a Budget hold or a Git operation half done.
- **Keep node results in memory as the source of truth.** That is a second fact
  source that diverges after any restart.
- **Compose services that write to different stores.** The two histories could
  never be checked against each other.
- **Match re-entry on identity alone.** A derived identity is predictable, so it
  is occupiable; the full durable fact has to agree.
- **Let a terminal fact carry its own node kind.** A node could then change what
  it was after the fact, and carry a foreign Artifact with it.
- **Report node failures without recording the run terminal.** A later resume
  would write that terminal itself and look like a legitimate continuation.
- **Let a failed terminal write replace the node failures.** The real reason the
  run failed would disappear behind a bookkeeping error.
- **Hand-write a subset of the identity comparison.** The subset omitted
  `capability_grants`, `target` and `wakeup`; the protocols already own the
  complete comparison.
- **Trust `run-finished(completed)` on its own.** One event would otherwise be
  able to claim success for a DAG that never ran.
- **Validate completion evidence by node kind alone.** `capture_artifact` is a
  property of the individual node, so the whole definition has to decide.
- **Move that check entirely to `run(definition)`.** The public Projector takes
  no definition, so replay would stop refusing malformed terminals; the
  definition layer adds to the base layer rather than replacing it.
- **Infer a node's Map role from its recorded key alone.** Identity decides the
  role; the key has to be checked against it, or the two layers can disagree
  about what the same node is.
- **Accept an expansion from any running node.** A Join could then record a
  fan-out and carry map keys it never produced, and the stream alone would never
  object.
- **Write a second cancellation/cleanup composition rule.** D2 already has one;
  a slightly different copy is how the two D2 paths drifted apart.

## Explicit boundaries

v0.7-E adds no CLI, no model-visible Workflow/approve/promote/capture Tool, no
retry policy, no conditional or loop node, no cross-process lease, no cold
Activation recovery and no OS sandbox. Verifier commands still run with the host
user's authority. The package version remains `0.6.0`; v0.7-E is implemented but
unreleased, and Stage F (CLI, a real Reviewer–Coder chain, packaging checks and
the v0.7.0 release) has not started.
