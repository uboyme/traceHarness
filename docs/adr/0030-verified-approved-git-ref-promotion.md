# ADR-0030: Fixed verification, human approval and Git ref compare-and-swap promotion

- Status: Accepted and implemented
- Date: 2026-08-24
- Stage: v0.7-D2

## Context

ADR-0029 gives a terminal Agent message an immutable Patch Artifact: exact bytes
in a SHA-256 CAS plus one append-only Manifest that binds Agent, Session,
message, Turn, Workspace generation and Git provenance. That proves *what was
produced*. It proves nothing about whether the Patch still applies, whether it
passes anything, whether a human agreed to it, or whether the repository it
would land in is still where it was when the question was asked.

Turning an Artifact into a target revision crosses four independent boundaries
that cannot be one transaction: the Artifact Catalog and CAS, a physical Git
repository, an external verifier process and the Event Log. Each one can fail,
be cancelled, or be changed by another writer in between. The dangerous shapes
are specific:

- a stale approval that no longer describes what would be merged;
- an approval that binds the outcome but not the definition that produced it;
- a "verified" result whose verifier came from the candidate under review;
- two promotions that both believe they are moving the same revision;
- a successful `update-ref` whose durable record failed to append, followed by a
  retry that assumes Git never moved.

D2 must close all of these without adding a scheduler, an Activation table or a
second Session/Workspace/Artifact fact source, and without ever giving a model
the ability to approve or move a ref.

## Decision

### 1. One narrow public seam and one independent control-plane domain

`traceh.api.promotion` defines frozen, slotted host values: `VerifierCommand`,
`VerifierEnvironmentPolicy`, `VerificationPlan`, `VerifierOutcome`,
`PromotionTargetBinding`, `PromotionTarget`, `PromotionTargetResolver`,
`PatchReviewReport`, `PatchApproval` and `PatchPromotion`.

`traceh.promotion` owns identities and digests (`models.py`), the event
vocabulary (`events.py`), the single Projector (`projection.py`), the fixed
verifier runner (`verification.py`), all Git effects (`local_git.py`) and the
three transactions (`service.py`).

The domain imports `traceh.artifacts` (read-only Artifact resolution) and the
EventStore. It does not import `AgentLoop`, `AgentRuntime`,
`ProcessAgentSupervisor`, `PluginManager`, the CLI or `traceh.evolution`, and no
module outside the domain imports it. There is no new scheduler, no new
Activation set and no second Workspace or Artifact fact source.

### 2. Three facts, one append-only stream, schema 1 only

`patch-promotions:ledger` carries exactly `patch/review-recorded`,
`patch/approval-recorded` and `patch/promotion-committed`. One Projector
rebuilds Review, Approval and Promotion from that stream on every load; nothing
caches a balance, a state file or a registry. Only schema version 1 is read;
another version, another event type, a sequence gap, an unexpected key set or a
duplicate identity is refused rather than migrated. There is no legacy mode, no
alias and no upcaster.

Replay recomputes every derived value instead of trusting the payload:
`review_id` from the review request id, `verification_evidence_digest` from the
recorded results, `passed` from those results, the approval digest from the
already-replayed Review, and `promotion_id` from the approval digest. A
promotion whose target identity, ref, previous revision, new revision or tree
contradicts its Review is refused at replay, not only at write time.

The Event Log stores `target_id`, a repository fingerprint, the ref and exact
revisions. It never stores a repository path, a CAS path, a scratch path, raw
verifier output or an environment value.

### 3. Review is fixed by the host, before the Patch exists

Review inputs come only from: a fresh `PatchArtifactReader` (Manifest replay
plus CAS re-hash), a host-configured `target_id`, the host `Target Resolver`,
the host's frozen `VerificationPlan`, and an explicit `review_request_id`. The
model, the Patch content and the Workspace files supply none of the repository
path, ref, verifier argv, environment policy, timeout or decision.

`freeze_verification_plan()` validates the whole plan once at the public
boundary: exact types (a `bool` is not an `int`), bounded argv and timeouts, a
bounded output limit, unique command ids and an environment policy whose
passthrough names and overrides may never be `GIT_*`. `shell=True` and
`create_subprocess_shell` do not exist in the domain.

### 4. The integration commit is deterministic and built away from the target

Review clones the target into a temporary directory and builds there:

```text
git clone --no-checkout <bare target> <scratch>
git update-ref --no-deref HEAD <expected revision>
git read-tree <expected revision>
git apply --check --cached --binary --whitespace=nowarn <exact patch bytes>
git apply --cached ...
git write-tree                      -> integration tree
git commit-tree <tree> -p <expected revision> -m <deterministic message>
git update-ref --no-deref HEAD <integration commit>
git read-tree --reset <integration commit>
git checkout-index -a -f            -> the worktree the verifier sees
```

`--3way` is never used: a conflict is a conflict, not an invitation to
reinterpret the change. Applying `--cached` means the tree is computed from
blobs with no working-tree conversion, which is why Review and Promotion produce
byte-identical trees. Parent, tree, message, author, committer and timestamp are
all protocol constants or approved inputs, so the same Patch on the same
revision always yields the same commit id.

The worktree the verifier ran against is proved by hashing the filesystem, not
by asking Git. Every Git-side answer is influenced by state the candidate or the
verifier controls: `git write-tree` reads only the index; `git status` honours
the candidate's own `.gitignore` and skips paths marked `--assume-unchanged` or
`--skip-worktree`. A Patch can add ignore rules and a verifier can set index
flags, so neither can witness what was actually executed.

Two different claims are needed here, and only one of them is about drift.
`git checkout-index` performs line-ending conversion, driven by
`core.autocrlf`/`core.eol` on whatever machine runs the review and by a
`.gitattributes` the candidate itself can ship - and attributes outrank
configuration. Comparing the checkout only with a later copy of itself would
prove that nothing moved while proving nothing about *which* bytes the verifier
exercised, so a review could approve a tree holding LF while the verifier ran
against CRLF.

The review therefore proves both. Configuration-driven conversion is removed by
passing `core.autocrlf=false` and `core.eol=lf` to every Git call. Then,
immediately after materialisation, each file is hashed **as Git hashes a blob**
and compared against the integration tree's own blob ids and mode bits; a
checkout that is not the tree fails closed, which is what catches an
attribute-driven conversion that configuration cannot suppress. The same walk
runs again after verification and its digest must be unchanged. Only the root `.git`
administration directory is excluded; the Git-side identity that matters is
re-derived separately as HEAD, the integration tree and the commit id, so an
index a verifier rewrote changes the tree and is caught there. The walk never
follows a link and rejects symlinks, Junctions, other reparse points and
non-regular files.

Mode `100755` is compared where the platform can represent it. A Windows
filesystem cannot: a checkout of an executable blob reports `0o666`, so
demanding the bit there would refuse every repository containing a runnable
script while proving nothing. On such a platform the tree's own mode is carried
through, and the mode remains guaranteed on the Git side - `write-tree` rebuilds
the tree from the index and the result is compared with the tree under review,
so a verifier that changes a recorded mode is still caught. On POSIX the bit is
additionally compared for real. It is bounded by an explicit entry count and total byte
limit; a checkout larger than that is refused rather than left unproven.

Because *any* new file now fails the proof, a verifier is granted scratch space
outside the checkout: the runner creates one owned temporary directory per run
and points `TMPDIR`/`TEMP`/`TMP` at it. Passthrough only inherits whatever this
host process happens to have, so owned scratch outranks it; an explicit override
is a real host decision and still wins.

Review never updates a target ref and leaves no object in the target
repository: the integration objects exist only inside the temporary clone,
which is removed on every exit path.

### 5. Evidence is bounded, structured and non-echoing

Each command records its id, an argv digest, a status
(`passed`/`failed`/`timed-out`/`start-failed`/`output-exceeded`), the exit code,
and the SHA-256 and byte size of stdout and stderr. Output is streamed into a
hash and never held in memory or written to the Event Log, so a verifier cannot
push unbounded text, terminal control sequences, local paths or secrets into
durable history. `passed` is the conjunction of the individual results and is
recomputed at replay.

The output bound is enforced **while the command runs**. Both pipes are drained
continuously and the read that first crosses the configured limit kills the
writer, so overshoot is one chunk per stream rather than whatever the command
managed to produce. A limit measured only after the process exits is not a
limit.

Draining a pipe must not become a way to outlive the timeout. A pipe reaches EOF
only once *every* descendant holding it has exited, so the deadline covers the
readers as well as the direct child, and the readers are cancelled and the host's
pipe ends released before the command result is returned. An orphaned grandchild
may keep the write end as long as it likes; it can neither extend the bound the
host set nor hold a host handle open. Only direct children are owned - a
grandchild is not killed, and one sitting in the checkout can make scratch
removal fail, which is reported rather than hidden.

The returned evidence is not trusted as a set. Each result is matched, in order,
against the corresponding frozen command: same command id, same argv digest,
and a well-formed bounded outcome. The evidence digest is then recomputed from
those results. A runner that reports a command the plan never contained is
rejected instead of recorded.

Before recording, Review re-reads the Artifact (Manifest digest, blob digest and
exact bytes), re-proves the target repository identity and fingerprint, re-reads
the target ref, and re-derives HEAD, the integration tree and the commit id.
Any drift fails closed with no event. A verifier that merely *fails* is
different: it produces a durable `passed=False` Review Report, which is a fact
worth keeping and which can never be approved.

### 6. Approval binds content, not intent

`approve(review_id, approval_digest, approver_id, operation_id)` is a host API.
There is no `approved=True` form, no CLI and no model-visible Tool; the model
never gains an approve, merge, promote, `update-ref` or capture Tool.

The digest is computed from the freshly replayed Review over exactly:
review id and request id, artifact id, Manifest digest, Patch digest and size,
target id, repository fingerprint, target ref, expected revision, integration
tree, integration commit, verifier definition digest, verification evidence
digest, merge policy version and `passed`.

It deliberately does **not** reuse `review_digest`. If it did, dropping the
verifier or evidence binding would still change the digest, and the property
"an approval is invalid when the verifier definition changes" would become
untestable. Approval additionally re-resolves the target and refuses when the
target definition or current revision no longer matches the Review.

`operation_id` is exactly idempotent: the same id with an identical canonical
payload returns the same Approval; the same id with any different payload, or a
second approval of an already approved Review, is a conflict.

Idempotency is bound to the complete operation definition, not to the identity
alone. A review id is derived from its request id, so a recorded report is only
returned when the artifact, target **and** verifier definition digest also
match, and an in-flight owned task is only shared with a caller whose whole
request digest is identical. Otherwise a second, differently-defined request
would receive a receipt for work it never described.

`approver_id` is a host-supplied audit identity. D2 does not invent an
authentication system and does not claim one.

### 7. `git update-ref <ref> <new> <expected-old>` is the only linearization point

Promotion re-replays the Review and Approval, re-reads and re-verifies the
Artifact, re-resolves the target, re-proves the repository identity, then
rebuilds the tree and commit **inside the target's own object database** using a
temporary `GIT_INDEX_FILE`. The rebuilt tree and commit must equal the approved
ones exactly; otherwise nothing touches the ref.

The ref then moves only by compare-and-swap against the approved expected-old
revision. There is no force update, no merge, no rebase, no reset, no checkout
of a working directory, no last-writer-wins, no re-apply after drift under an
old approval, and no automatic rollback that would overwrite whatever a later
writer put there. `promotion_id` is derived from the approval digest, so a
retry addresses the same promotion.

### 8. Git and the Event Log reconcile in three states

The ref update and the ledger append are not one transaction, so promotion reads
the ref and treats exactly three cases:

| observed ref | meaning | action |
|---|---|---|
| approved new commit | the Git mutation converged | record or re-confirm the promotion event |
| approved expected-old | the mutation has not happened | rebuild and retry the compare-and-swap |
| anything else | target drift | fail closed |

A failed, timed-out or cancelled append never implies "Git did not move". The
ledger append uses the shared three-state `committed_after_failure()`
reconciliation: provably committed, provably absent, or **unknown**. Unknown is
reported as `PromotionWriteError(committed=None)` and a later retry reconciles
from the ref, which is why collapsing unknown into "absent" is a defect and is
covered by a reverse-validated test.

Because the ledger is a single stream shared with reviews and approvals, a lost
`expected_seq` race after a successful ref update is retried a bounded number of
times rather than abandoning an already-durable Git mutation.

### 9. Cancellation converges on the owned task

Review, approval and promotion each run in one owned Task keyed by their
identity. A cancelled caller waits for that same Task through
`await_worker_convergence()` and then receives its **original**
`CancelledError`; repeated cancellation cannot release the caller early or punch
through reconciliation. Concurrent calls for the same identity share one Task.
The work belongs to the Task, so a cancelled promotion still converges and a
later call observes the recorded fact instead of a half state.

Scratch directories are removed on success, failure, cancellation and cleanup
failure. A cleanup failure never replaces the primary error: it is grouped with
it, or reported on its own when there was no primary failure.

### 10. Replay normalises what it does not own

Envelopes come from a replaceable store, so reading an attribute can itself
fail. The promotion header boundary converts any `Exception` into the stable
`PromotionProtocolError`; `BaseException` is deliberately not caught, because
`KeyboardInterrupt` and `SystemExit` are not answers about a payload.

## Consequences

- A ref can only move to a commit that was built from an exact immutable Patch,
  verified by a host-fixed suite, and approved by a human against the complete
  content digest of that verification.
- Two candidates racing the same base cannot both land; the loser gets a stable
  target-drift failure rather than a silent overwrite.
- The concurrency-heavy Runtime, Supervisor and PluginManager gain no promotion
  state, no branch and no import.
- Failed reviews are durable evidence rather than silent deletions.
- `git write-tree`/`commit-tree` write objects into the target repository before
  the ref moves. A refused or failed promotion can therefore leave unreachable
  objects; no ref makes them reachable, and garbage collection remains a
  deliberate operator action.

## Security and trust boundary

The verifier runs as the same OS user with the same filesystem authority as the
host. This is a capability and evidence boundary, **not** an OS sandbox: a
verifier can still touch anything that user can touch. The environment is
positive-list only and every inherited `GIT_*` variable is removed before the
host's own controls are added, so Git configuration cannot be injected through
`GIT_DIR`, `GIT_INDEX_FILE`, `GIT_CONFIG_PARAMETERS` or a future variable.

Because the integration worktree must still equal the tree under review when
verification ends, verifier commands must not leave **any** file behind in the
checkout, ignored or not. They write to the granted scratch directory instead,
which the runner creates outside the checkout and removes with the run. If that
removal fails the failure is raised, not swallowed: on its own it becomes
`promotion-verifier-scratch-cleanup-failed`, alongside an ordinary error it is
grouped with it, and behind a cancellation it is chained so the original
`CancelledError` still reaches the caller.

Cancellation *during* the removal is the same rule, and it needs one extra step:
converging the cleanup task is not the same as reading it. The removal keeps
running after the caller is cancelled, so its real outcome only exists once the
task is done; the code therefore waits and then reads the task's exception.
Repeated cancellation still waits for that same task.

Three facts can be true at once - the work already failed, the removal failed,
and the caller cancelled - and each one is real, so none is dropped. The caller
sees its own `CancelledError` at the top; whatever else happened is the cause;
and when both the work and the removal failed the cause carries **both**, as a
`BaseExceptionGroup`. Promotion has two scratch lifetimes (the integration clone
and the verifier's working space), so this composition lives in one shared
helper rather than being written twice and drifting apart. Each caller supplies
only its own error vocabulary for the case where the removal is the sole
failure.

A repository that genuinely requires line-ending conversion cannot be promoted
by D2 v1: the review fails closed rather than approve bytes nobody ran. That is
a deliberate boundary, not an oversight.

D2 does not provide a cross-process lease. Another process with write access to
the target repository can still move the ref; that is detected and refused, not
prevented. D2 also does not add a CLI, a Workflow engine, automatic approval,
automatic target selection or container isolation.

## Rejected alternatives

- **Let the model call approve/promote.** Authority to change a shared
  repository is not a Tool; evidence is not consent.
- **A boolean `approved=True`.** Reusable, and it binds nothing.
- **Reuse `review_digest` as the approval digest.** It hides which fields the
  approval actually binds and makes the verifier/evidence binding untestable.
- **`git apply --3way`.** It resolves conflicts by reinterpreting the change,
  so the approved tree would no longer be the applied tree.
- **Build the integration commit directly on the target's ref.** Review would
  then mutate the repository it is only supposed to inspect.
- **Verify in the Agent's own worktree.** That worktree is the mutable
  deliverable; verification must run on the exact integration state.
- **Promote into a normal checkout.** Moving the branch of a working tree
  someone else is using is a surprise, not a compare-and-swap.
- **Force update or last-writer-wins.** Both destroy a concurrent writer's work
  and make "approved expected-old" meaningless.
- **Re-apply the Patch onto the new head after drift and reuse the approval.**
  The human approved an exact tree and commit, not an intent.
- **Automatic rollback after a later failure.** It would overwrite whatever a
  subsequent writer legitimately put on the ref.
- **Assume a failed Event append means Git did not move.** It is the exact case
  where the ref is the only fact, and re-reading it is cheap.
- **Ask Git whether the checkout is clean.** `write-tree` sees only the index;
  `status` obeys the candidate's ignore rules and index flags. Both are inputs
  the thing under review can influence.
- **Let ignore rules decide what a verifier may leave behind.** `.gitignore`
  ships inside the candidate, so that would let a Patch grant itself permission
  to run code the approved tree does not contain.
- **Bound only the direct child's lifetime.** Pipe EOF depends on every
  descendant, so the timeout would be extended, or removed, by a process the
  host never owned.
- **Compare the checkout only with a later copy of itself.** That proves the
  verifier saw *stable* bytes, not that it saw the *approved* bytes;
  `checkout-index` converts line endings, so the two can differ.
- **Suppress line-ending configuration and stop there.** A candidate's own
  `.gitattributes` outranks configuration, so conversion must be detected, not
  merely discouraged.
- **Ignore verifier scratch cleanup failures.** Leaving a directory behind is a
  real fact about the host; silencing it contradicts what this ADR promises.
- **Converge a cleanup task without reading its result.** A failure that happens
  after the caller was cancelled would be retrieved and then thrown away.
- **Report either the work's failure or the cleanup's, but not both.** They are
  independent facts; choosing one silently deletes the other.
- **Write the composition rule once per scratch lifetime.** Two copies of "which
  failure does the caller learn about" is exactly how the two paths drifted
  apart in the first place.
- **Require Git's executable bit from the filesystem on every platform.** NTFS
  cannot store it, so that refuses ordinary repositories without proving
  anything the tree comparison does not already prove.
- **Measure verifier output after the process exits.** The bound would describe
  the flood rather than stop it.
- **Trust a runner's result set because its digests are internally consistent.**
  A self-consistent set can still describe commands the plan never contained.
- **Share an in-flight task by identity alone.** The second caller would be
  handed evidence about a different artifact, target or verifier definition.
- **Collapse the unknown commit state into "not committed".** That is the
  strongest possible claim from the weakest possible evidence.
- **Store verifier stdout/stderr in the Event Log.** Unbounded, terminal-unsafe
  and a secret-leak channel; digests and sizes are the durable facts.
- **A finite denylist of `GIT_*` variables.** Git's injection surface grows; the
  whole inherited namespace is removed instead.
- **A second scheduler, Activation table or promotion state cache.** The stream
  is already the durable relation; another copy would be a second truth.
- **A CLI or Workflow node in D2.** Those belong to later stages and would
  freeze a control-plane shape before it has a real consumer.

## Explicit boundaries

D2 does not implement a CLI, a Workflow engine, automatic approval, automatic
target selection, non-bare targets, tag/note refs, multi-parent merges, CAS or
object garbage collection, cross-process leases or OS-level sandboxing. The
package version remains `0.6.0`; D2 is implemented but unreleased.
