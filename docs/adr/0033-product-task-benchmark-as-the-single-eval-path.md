# ADR-0033: The ProductTask benchmark is the only `traceh eval` path

- Status: Accepted and implemented
- Date: 2026-08-27
- Stage: v0.7-F4

## Context

ADR-0032 §15 decided that when the benchmark arrived it would **reuse and rework**
`traceh eval` rather than grow a second command, and that the v0.6 `*/case.json`
manifests would be refused explicitly rather than upcast. It did not decide how
the measurement itself stays honest, and that turns out to be the harder half.

The v0.6 runner discovered `*/case.json`, copied a directory, built a default
Runtime with a scripted provider, ran one Agent and reported a success rate. None
of the v0.7 mainline existed in it: no confirmation, no Budget account, no
managed worktree, no immutable Artifact, no frozen Review, no human-or-host
approval and no Git ref compare-and-swap. Reading those manifests under the new
pipeline would mean inventing every one of those facts.

The measurement question v0.7 actually needs answered is narrow: *for the same
requirement, the same source revision, the same frozen verifier, the same Budget
rules and the same promotion rules, what changes when the confirmed mode changes
from `single` to `multi`?* Everything below exists to keep that question from
quietly becoming a different, easier one.

## Decision

### 1. One benchmark path, assembled from the product mainline

`traceh eval` is the only benchmark command. `traceh.evaluation` is a composition
root, like `traceh.cli`: it builds the same `ProductChatHost` that
`traceh chat --product-config` builds and drives the same
`ProductTaskControlPlane`. It owns no task state machine, no scheduler, no second
Workflow and no second definition of "did this work".

`ProductChatHost` therefore exposes its `control` property. `ProductChatSurface`
is the *console rendering* of those operations, not an authority over them, and a
non-console host needs the operations without the console. Nothing model-facing
moves: the two chat Tools still hold only `ProductTurnActions`.

The shared half of the host configuration schema is parsed by one function,
`traceh.product.config.parse_product_host_settings`. A second, weaker parser
would eventually accept a Profile the Chat host refuses.

### 2. The manifest cannot name a repository, a model or a graph

The benchmark creates a throwaway source repository and a one-shot local **bare**
promotion target for every attempt, from the task's `initial/` tree. No manifest
key names a repository path, so there is no value to point at a real remote: the
"never touch a real remote" property is structural rather than a rule somebody
has to remember.

Provider and model are also arguments rather than keys, supplied by `--provider`
/ `--model`. A checked-in manifest naming a model would either pin one vendor
into this repository or ship a placeholder that must be edited. One run resolves
one provider and one model and uses it for every arm, which makes "single and
multi used the same model family" true by construction.

The manifest likewise carries no node, edge, fan-out, Agent count or approval
digest, for the same reason ADR-0031 refused a workflow DSL and ADR-0032 §16
refused one in the Chat host file.

### 3. The candidate cannot author the question

The requirement and the requested mode come from the manifest and are passed
directly to `ProductTaskControlPlane.offer()`. The chat surface exists so a
*person* can turn a conversation into a task; a benchmark already knows the task,
and a model that could propose it could rewrite what it is scored on.

`product/task-opened` still requires real Session evidence, so the benchmark runs
two real user Turns through a host-frozen, **tool-free** requester provider. That
provider makes no decisions, never sees the managed Workspace, and its Session is
not part of the measured Agent subtree.

### 4. Programmatic immediate approval, labelled everywhere

The benchmark host approves its own one-shot local target as soon as the Approval
barrier is reached. This is recorded as `approval_policy:
programmatic-immediate` in both outputs and grants no authority anywhere else:
ordinary Chat still requires a human `/task approve`.

It exists so `active elapsed` measures work. Approval wait is timed as its own
interval and subtracted, because a number that includes how long a person was
away from the keyboard is not a latency measurement of `multi`.

### 5. `auto` is a routing observation, not a third quality arm

Quality aggregates are keyed by **resolved** mode and contain every attempt that
resolved to it, including `auto` attempts. `auto` appears separately only as
strict-parse outcome, routing tokens and routing elapsed.

Treating `auto` as a third arm would compare `multi` against itself and then
report the difference as a finding.

### 6. Every metric comes from a fact source or is unavailable

| metric | source |
|---|---|
| success | ProductTask terminal + Workflow terminal + Review `passed` + a Promotion receipt whose new revision is what the target ref actually holds |
| routing tokens/elapsed | the Session of the Agent `product/task-routed` names |
| execution tokens/steps/tool calls | the Sessions of the Agents the Workflow's node outcomes name |
| cumulative work duration | durable `turn/start` → `turn/end` intervals |
| budget outcome | the Budget Ledger, scoped to this task's ownership subtree |
| active / approval wait / wall | the runner's own monotonic clock |

Phase boundaries are the one exception and are labelled as such: no durable fact
records when a host decided.

Counting events is not the same as reading a Session. Every measured Session is
checked with the existing `CoreInvariantChecker` before any number is taken from
it, so a stream that merely *looks* like a Session - an attempt-end with no
start, an unpaired Tool call - makes the metric refuse rather than inflate. The
benchmark does not own a second, weaker definition of a valid lifecycle.

A **failed** AgentTask node records no `agent_id`; its terminal payload carries
only a failure code. Reading the outcome alone therefore drops every token a role
spent before it failed and reports a confident zero for work that really
happened. The Agent identity is instead derived from run and node by the same
rule the executor used, and cross-checked against the outcome whenever the
outcome does carry one.

The Workflow Verification outcome, the ProductTask `review_id` and the Promotion
receipt must describe **one** Review, and the promotion's approval digest must be
the digest of that Review's content. Each is well-formed alone, so reading them
independently lets a report say "verified, approved and promoted" from three
unrelated records.

The Promotion projector proves that a Review is internally coherent, but the
benchmark host also owns the frozen `VerificationPlan`. Before using
`review.passed`, the evidence collector reuses Promotion's shared frozen-plan
matcher to bind every result, in order, to the exact command id and argv digest
from that plan. Recomputing a Review's internal evidence and approval digests is
therefore not enough to substitute a different verifier result.

`product/task-routed` names both the Router Agent and its Session, but only the
Agent Directory decides which Session that Agent owns, so the routing Session is
resolved there and required to match the recorded one. Taking the pair on trust
lets a routing identity point at a role Session of the same task - it parses
cleanly and passes the invariant check - and the same tokens are then counted
once as routing and once as execution, collapsing the separation those two
metrics exist to keep.

Two token totals are reported and they are different measurements. The Session
total is what the provider said it used; the ledger total is what the Budget
authority actually consumed, which is conservative by design. Presenting one as
the other would misstate whichever it replaced.

A metric the facts cannot support is reported as **unavailable**. In particular a
`UsageQuality.UNKNOWN` report makes that Session's token total unavailable rather
than zero: `unknown` is this repository's own word for "this count is not
evidence", and a `0` in a token column reads as "no tokens were used".

### 7. Comparability is proved, not assumed

For each task the report records the shared `requirement_digest`,
`profile_digest`, `source_base_revision` and `verifier_definition_digest` across
all arms, and names any field that diverged. Deterministic commit construction
(fixed tree, identity, timestamps and message) is what makes the source revision
comparable in the first place.

The verifier is proved from the **frozen manifest**, not inferred from whichever
attempts reached a Review. An arm that failed before its Review has no verifier
digest at all, and filtering that absence away is how a task where only `single`
survived ended up claiming a shared verifier one arm never demonstrated. Every
attempt that did establish a digest must match the frozen plan.

Absence is likewise not agreement in the other columns: an attempt that never
established a field is counted in `unproven_fields`. That is reported beside
coherence rather than folded into it, because an attempt that failed before it
started legitimately never had a source revision, and calling the whole task
incoherent for that would report a normal failure as a broken experiment.

Workspace **quarantine is a converged terminal**, not an unconverged one. The
Product resource contract quarantines a dirty worktree on failure or
cancellation precisely so the captured bytes survive; convergence excludes only a
record still `provisional` or `attached`, which the report exposes as `live`.

A run with a divergent condition, or with an attempt that could not be measured,
is `complete: false` and exits 4. The exit code answers "did the measurement
complete", not "did the coding tasks succeed": a failed task is a result, and a
benchmark that exits non-zero on one is reporting data as a tool error.

### 8. Small n stays small

Aggregates are counts, totals, minima, maxima and a mean. There is no variance,
no confidence interval and no significance claim, and an arm with one observation
says `single observation` in both outputs.

### 9. Convergence, not deletion

Failure and cancellation converge the existing owners - the ownership tree, the
Budget accounts and the managed worktree - through the same control plane a
person would use, and then record an honest terminal. Nothing under the output
directory is deleted: an attempt is clean because its owners converged, not
because its evidence was removed.

## Consequences

- There is exactly one answer to "did this work", and it is the same pipeline
  users run.
- A benchmark run cannot reach a real remote or a real model without an explicit
  `--provider` / `--base-url`, and cannot reach a real repository at all.
- Attempt directories are numbered rather than descriptive. A managed worktree is
  named by a 67-character `ws-<full SHA-256>` identity. The compact prefix keeps
  Git for Windows' linked-worktree administration path below its fixed internal
  limit without truncating the identity digest; descriptive attempt names can
  still exceed the platform boundary. The readable `attempt_id` and the numbered
  directory are connected in the report.
- The frozen verifier proves the declared checks passed on the reviewed bytes. It
  does **not** prove a candidate left those checks as strong as it found them.
  That gap is stated in the report rather than closed here; the human/host
  approval gate remains the place it is caught.
- Three tasks with a handful of repetitions is a sanity measurement. Nothing in
  this ADR claims it ranks models.

## Alternatives rejected

- **A second benchmark command beside `traceh eval`.** Two definitions of "did
  this work", one of which rots. This restates ADR-0032 §15.
- **Reading `*/case.json` under the new pipeline.** It would mean inventing the
  confirmation, Budget, Workspace, Artifact, Review and promotion target it never
  had. An explicit refusal is the honest cutover, and the user's old data is
  never rewritten or deleted.
- **Letting the chat model author the benchmark requirement.** The candidate
  would be able to change the question it is scored on.
- **Letting the manifest name the source and target repositories.** The one
  property worth having structurally - no real remote - would become a rule.
- **Pinning a provider and model in the checked-in manifest.** Either a vendor in
  this repository or a placeholder somebody must remember to edit.
- **Reporting `auto` as its own quality arm.** It would compare `multi` with
  itself.
- **Folding an unavailable measurement in as zero.** A mean silently dragged down
  by measurements that never happened.
- **Treating `UsageQuality.UNKNOWN` as a token count.** The Budget domain already
  refuses to believe it; a report that believed it would contradict the ledger
  beside it.
- **Deleting an attempt's directory after a failure.** That is the appearance of
  convergence, not convergence.
- **Counting a quarantined worktree as unconverged.** It would report the
  failure/cancellation lifecycle as broken at the exact moment it did what the
  resource contract requires.
- **Reading a failed node's Agent from its terminal payload.** There is none, so
  a role that worked and then failed would aggregate as a confident zero.
- **Trusting Verification, Review and Promotion as three independent facts.**
  Three well-formed unrelated records would still read as one verified,
  approved, promoted result.
- **Taking Session counts without checking the Session's own invariants.** Any
  stream shaped like a Session would produce numbers, including a forged one.
- **Trusting the Agent/Session pair a routing fact carries.** The Directory owns
  that relationship; without it one Session can be billed to both arms.
- **Aborting the whole run when one attempt cannot be measured.** One unmeasurable
  attempt would destroy the evidence for every other one; it is reported as
  unmeasured and the run stays `complete: false`.
