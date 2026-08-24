# ADR-0028: Managed Git workspace lifecycle outside the Agent runtime

- Status: Accepted and implemented
- Date: 2026-08-24
- Stage: v0.7-C

## Context

v0.6 can run several Agents, but an approved `workspace_id` is still only an
identity string. Giving two coding children the same mutable checkout makes
their effects race, while letting a model choose an absolute path turns an
identity field into ambient filesystem authority. The v0.7 control plane needs
one durable answer to which exact Git commit and local worktree belong to one
managed Agent operation, without moving Git, paths or cleanup into
`AgentLoop`, `AgentRuntime` or `ProcessAgentSupervisor`.

Provisioning also crosses three independent fact/effect boundaries: Git
worktree state, the Agent Directory and the Session Stream. They cannot be made
atomically transactional. Cancellation, append uncertainty and a process crash
can therefore leave a physical worktree without an attached Agent. The design
must preserve that evidence and fail closed instead of deleting an unproven or
dirty path.

## Decision

### 1. One catalog owns durable workspace identity

Each `EventStore` has one append-only `workspaces:catalog` stream. It records:

- `workspace/provisioned` with the framework-generated workspace id, stable
  creation request, host source id, requested revision, repository fingerprint,
  exact base commit, access capability and optional owner Agent;
- `workspace/attached` with the exact Agent and Session identities;
- `workspace/quarantined` with a fixed reason;
- `workspace/released` with a fixed terminal reason.

The projector is the only durable lifecycle view. It enforces exact event keys,
contiguous sequence, globally unique operation/request/workspace identities and
one Agent/Session attachment. The state machine is:

```text
PROVISIONAL -> ATTACHED
PROVISIONAL -> QUARANTINED -> ATTACHED (only the same proven Agent/Session)
PROVISIONAL -> RELEASED
ATTACHED    -> QUARANTINED
ATTACHED    -> RELEASED
QUARANTINED -> RELEASED
```

No mutable path registry or Runtime cache is a second fact source. Catalog
facts never enter a Session Surface, request fingerprint or model prompt.

### 2. Host mapping selects source; the model receives identity only

`WorkspaceProvisioningRequest` contains only a trusted source id, a revision
selector and an exact read-only/writable capability. `LocalGitWorkspaceProvider`
maps the source id through host configuration and resolves the selector to one
commit before the catalog fact is written. The catalog persists a digest of the
canonical Git common directory, not the host path. A model-visible Agent spec
contains only the resulting `workspace_id`.

`WorkspaceService.finish_agent_creation()` attaches only when a fresh Agent
Directory record proves the same creation request, owner and workspace id and
the Agent's Session `session/created` fact proves the same workspace id. A
string that merely looks equal is not sufficient to cross these boundaries.

### 3. The Git provider mutates only exact managed worktrees

The local provider creates a detached worktree at the resolved base commit
under one explicit managed root. It rejects:

- a source that is not a clean, top-level normal Git checkout;
- a source, managed root or target containing a symbolic link, junction or
  reparse component;
- an occupied target not already registered as the exact worktree;
- a `.git` entry that is not the expected regular worktree marker;
- registry, common-directory or HEAD identity disagreement;
- a marker whose Git-resolved absolute admin directory differs from the unique
  `worktrees/*/gitdir` registry entry that points back to that exact target.

The last check is deliberately bidirectional. Two sibling worktrees may belong
to the same common directory and have the same commit, so swapping their valid
marker files would otherwise borrow the sibling's index and HEAD administration
while passing every value-level check.

All Git mutation uses argument vectors, disables prompts and repository hooks,
bounds captured output and converges the direct child process after timeout or
cancellation. It never invokes force removal, `worktree prune`, ref updates or
patch application. Removal is allowed only for the exact registered, clean
worktree at its base commit. Dirty or unprovable state becomes quarantine; it
is never deleted to make a test or retry pass.

### 4. Provisioning is an owned, reconcilable saga

`WorkspaceService` serializes catalog/Git mutation within one host process,
uses Catalog sequence CAS and compares canonical facts after an uncertain
append. Provision, attach, quarantine, release and physical removal are owned
operations. Once stateful work starts, cancellation waits for the same task to
reach a durable verdict before the original cancellation is re-raised.

Provision failure is reconciled by inspecting the exact worktree and fresh
Directory/Session facts. A clean unattached worktree may be removed and marked
released. Dirty, unsafe or unknowable state is quarantined. A committed Agent
that cannot be attached is disposed through the existing public Supervisor
before the error returns. This is a same-process coordinator, not a distributed
lease; another process can still race Git/catalog mutation and will be detected
or fail closed rather than silently merged.

### 5. The public Supervisor is wrapped, not copied

`WorkspaceManagedAgentSupervisor` implements the public `AgentSupervisor`
contract and delegates to exactly one existing Supervisor. It owns the
cross-domain create order:

```text
host workspace policy
  -> provisional worktree/catalog fact
  -> public AgentSupervisor.create()
  -> fresh Directory + Session reconciliation
  -> attach, or dispose Agent and release/quarantine workspace
```

It owns no Activation table, Inbox, Delivery log, Agent Directory or worker.
`resume()` keeps the wrapper's shared lifecycle lock across its durable
pre-check, delegated resume, post-check and failure cleanup. Consequently
`aclose()` cannot close the inner Supervisor and return while a wrapper-owned
resume tail can still return an already-disposed handle. Wakeup validates the
durable workspace first. Agent
`dispose()`/Supervisor `aclose()` release Activations but intentionally preserve
the worktree; workspace deletion is an explicit host operation because Agent
lifetime and review/artifact lifetime are different.

### 6. Read-only is a Tool capability boundary, not a sandbox

`ManagedWorkspaceAccessPolicy` resolves the caller Session through the catalog
and checks that `ToolExecutionContext.workspace` is the same managed root.
A read-only workspace may invoke only `PURE_READ` and `WORKSPACE_READ` tools;
write, process, network and external effects are denied even if accidentally
registered in the Composition. A writable workspace defers to the remaining
host policies.

The policy must be explicitly included in the host Composition. The physical
directory remains writable to code running with the same operating-system
account, so this is not containment for untrusted plugins or arbitrary native
processes.

## Consequences

- Parallel coding Agents can be assigned independent commit-pinned worktrees
  without changing the single-Agent loop or Supervisor concurrency kernel.
- Workspace identity, Agent identity and Session identity remain separate
  durable facts that are reconciled explicitly.
- Dirty or uncertain workspaces remain available for inspection instead of
  being destroyed by cleanup.
- A stopped Agent does not imply that its workspace is disposable.
- The common direct-subprocess convergence helper now lives at
  `traceh.process_control`; Tool output capture remains in
  `traceh.tools.process_control`. No compatibility alias is retained.

## Rejected alternatives

- **Let the model choose a filesystem path.** This confuses a durable identity
  with host authority and makes containment unreviewable.
- **Store paths in Agent or Session identity only.** That cannot represent
  provisional, quarantined or released worktrees and creates a second mutable
  lifecycle truth.
- **Put Git lifecycle in `ProcessAgentSupervisor` or `AgentRuntime`.** Those
  components do not own source selection, artifact retention or promotion.
- **Delete a workspace when an Activation stops.** Review and later Patch
  collection outlive an in-process Activation.
- **Force-remove or broadly prune after failure.** A dirty or unregistered path
  may contain the only evidence or user work.
- **Keep the old Snapshot/Patch/Merge placeholder API beside the implemented
  contract.** Pre-1.0 keeps one current API; Patch and promotion will get their
  own implemented types in later stages.
- **Treat read-only as an OS sandbox.** Tool admission cannot constrain arbitrary
  code with the host user's filesystem permissions.

## Explicit boundaries

Stage C does not implement Patch Artifacts, diff capture, verification,
approval, merge/promotion, a Workspace CLI, cold recovery, cross-process
workspace leasing, containers or an operating-system sandbox. Those belong to
later v0.7 stages or remain explicit future work. The package version remains
`0.6.0`; Stage C is implemented but unreleased.
