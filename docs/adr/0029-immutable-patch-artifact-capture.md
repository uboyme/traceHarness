# ADR-0029: Immutable Patch Artifact capture outside the execution kernel

- Status: Accepted and implemented
- Date: 2026-08-24
- Stage: v0.7-D1

## Context

v0.7-C gives a managed Agent an exact commit-pinned Git worktree, but a final
answer is not a reviewable code deliverable. A Patch must include committed,
staged, unstaged, untracked, deleted, binary and mode changes while remaining
bound to the exact Agent, Session, message, Turn and Workspace generation that
produced it. Reading only `git diff`, copying the final text, or asking the
model which files changed cannot provide that proof.

Capture also crosses independent boundaries: durable Agent/Session evidence,
the physical worktree, Git's object database, a byte CAS and the EventStore.
They cannot be made one atomic transaction. Cancellation and concurrent
writers therefore require explicit ownership, repeated observation and
fail-closed reconciliation without adding Artifact state to `AgentLoop`,
`AgentRuntime`, `ProcessAgentSupervisor` or `PluginManager`.

## Decision

### 1. Artifact is an independent domain with a narrow public seam

`traceh.api.artifacts` defines frozen Patch Blob/Manifest/Artifact values,
explicit capture limits, an `ArtifactCas` protocol and a
`WorkspaceCaptureGate` protocol. `traceh.artifacts` owns capture, Git plumbing,
the local CAS, Manifest replay and read-only report decoration.

The Workspace wrapper implements only the generic capture gate. It exposes one
already-validated `WorkspaceHandle` while holding its existing writer/close
lock; it imports no Artifact implementation or type. The Artifact domain does
not import the execution Runtime, concrete Supervisor or PluginManager. There
is still one Activation table, one Workspace Catalog and one EventStore.

### 2. Bytes live in CAS; source binding lives in one append-only stream

Raw `--binary --full-index` Patch bytes are addressed by SHA-256 in an explicit
CAS. Event Log facts never contain a local CAS or Workspace path. One global
`artifacts:catalog` stream records `artifact/patch-captured` schema 1 with:

- Artifact/capture identity and exact Blob digest, size, address and protocol;
- Agent, Session, message and Turn identity;
- Workspace id and Catalog generation;
- repository fingerprint, base revision, capture-time HEAD and candidate tree;
- Git-derived changed paths and capture protocol version.

The event timestamp supplies `captured_at`; its canonical payload plus that
timestamp supplies the Manifest digest. `capture_key` is recomputed from the
Agent/message/Workspace/generation tuple and `artifact_id` is recomputed from
that key at both the public builder and replay boundary. Shape-valid but
incorrect derived identities are rejected rather than becoming a second fact.
Replay also rejects gaps, unknown schema, extra/missing keys and duplicate
identities. A fresh reader always replays the catalog and re-hashes CAS bytes
before returning a reference.

### 3. A temporary index captures the complete candidate tree

The Git transaction is:

```text
validate exact managed worktree identity
  -> fingerprint original index
  -> temporary GIT_INDEX_FILE
  -> read-tree current HEAD
  -> git add -A
  -> write-tree candidate
  -> validate the complete candidate tree
  -> diff base revision to candidate with --binary --full-index --no-renames
  -> repeat the complete snapshot
  -> require identical identity, index fingerprint and snapshot
```

The original index, HEAD, refs and worktree bytes are never modified. Git may
write unreachable blobs/trees to the repository object database, which is an
unavoidable property of `git add`/`write-tree`; no ref makes them reachable.
Capture uses the same repository Git configuration source as worktree
materialization so line-ending normalization does not manufacture changes.
Every inherited `GIT_*` environment variable is removed before the capture
process adds only its controlled prompt, credential, optional-lock and
temporary-index settings. This closes `GIT_CONFIG_PARAMETERS`, dynamic
`GIT_CONFIG_COUNT` keys and future Git environment injection without relying
on a finite denylist; prompts and hooks remain disabled.

The complete candidate tree and filesystem are rejected for symbolic links,
Windows Junction/reparse points, gitlinks/submodules, `.gitmodules`, control
paths, non-UTF-8 or non-NFC paths, case-folding collisions, unsupported modes
and bounded entry/path/file/total/Patch limits. Changed paths come only from
the base-to-candidate Git tree diff.

### 4. Durable terminal evidence and the Workspace gate define capture admission

Capture requires one attached writable Workspace and an exact completed
message whose durable Directory/Inbox/Delivery/Session facts agree. The target
must be the latest accepted message, all accepted messages must have terminal
outcomes, no claim/Turn/Step remains open, the target Turn must be the latest
closed Turn, and Session/effect invariants must pass.

The service records an evidence receipt before Git capture and reloads it after
the second snapshot. Any Workspace generation, Session head, Effect head,
Inbox/Delivery head, Turn or Git snapshot drift fails closed. Managed create,
resume, send and close cannot cross the Workspace capture gate. External or
cross-process writers are not claimed to be locked; observable drift is
rejected rather than silently merged.

### 5. Capture is idempotent and cancellation converges

The capture key is the canonical digest of `(agent_id, message_id,
workspace_id, workspace_generation)`; the Artifact id is derived from it. One
process shares concurrent calls for the same key through one owned Task.
After the first success, the same key resolves the same recorded Artifact even
if the physical worktree later changes; new work needs a new terminal message
and capture identity.

CAS writes are atomic and verify an existing digest against exact bytes. Before
creating a directory, writing or reading a Blob, the implementation walks the
configured root-to-parent chain and rejects every symlink, Junction or other
reparse point. It creates at most one already-validated child at a time and
rechecks the chain, so a post-initialization replacement cannot create outside
directories or supply outside bytes through the CAS namespace.
Manifest append uses stream CAS and the existing three-state commit
reconciliation. A cancelled caller waits for the owned Git/CAS/Event work to
converge and then receives its original cancellation. If CAS succeeded but a
later check or append failed, an unreachable content-addressed Blob may remain;
it is never exposed as a verified Artifact. Closing the capture service waits
all already-admitted captures and admits no new one.

### 6. Collection remains read-only

`ArtifactReportingAgentSupervisor` decorates durable reports by fresh-reading
already-recorded Manifest references. The existing `collect_agent_artifact`
Tool remains `PURE_READ`: it can return those references but never invokes
capture, Git or CAS. Hosts or a later Workflow decide when to call
`PatchCaptureService`; no model-visible self-capture Tool is added.

## Consequences

- A code result has immutable bytes and enough durable identity to reproduce
  exactly which Agent operation and candidate tree produced it.
- The concurrency-heavy Supervisor and Runtime gain no Artifact state or
  branch; only the existing Workspace adapter gains a generic lease.
- Full Git status is captured without rewriting a user's original index.
- Same-process cancellation cannot expose a half-finished Artifact.
- CAS garbage collection and cross-process Workspace leasing remain future
  operations because neither can be inferred safely from one failed call.

## Rejected alternatives

- **Treat final model text as the Artifact.** Text neither proves filesystem
  bytes nor includes committed/staged/binary/deleted changes.
- **Run `git diff` against the live index.** It misses untracked files and can
  omit either staged or unstaged state.
- **Stage into the original index.** That mutates the child deliverable while
  trying to observe it.
- **Store Patch bytes in the Event Log.** Large binary deltas do not belong in
  the control-plane history; the log stores an immutable digest and source
  binding instead.
- **Capture from `collect_agent_artifact`.** A `PURE_READ` model Tool must not
  create CAS or Git effects.
- **Cache report-to-Artifact links in memory.** The Manifest stream is already
  the durable relation; another cache would be a second truth.
- **Trust shape-valid `capture_key` and `artifact_id` fields during replay.**
  Both are deterministic derivations, not independent caller facts.
- **Check only the CAS root and final Blob.** A parent Junction can redirect
  both directory creation and reads before either endpoint check detects it.
- **Maintain a finite denylist of Git environment variables.** Git supports
  multiple and evolving configuration injection channels; capture instead
  removes the whole inherited `GIT_*` namespace and adds only owned values.
- **Add capture state to AgentRuntime or ProcessAgentSupervisor.** Neither owns
  Artifact retention, review or Git promotion.

## Explicit boundaries

D1 does not verify or approve a Patch, create an integration commit, update a
target ref, merge/promote, expose a capture CLI/Tool, garbage-collect orphan CAS
bytes, provide a distributed Workspace lease or sandbox same-user processes.
Those belong to D2/later stages. The package version remains `0.6.0`; D1 is
implemented but unreleased.
