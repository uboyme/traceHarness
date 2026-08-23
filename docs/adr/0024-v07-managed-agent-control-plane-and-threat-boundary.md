# ADR-0024: v0.7 managed Agent control plane and threat boundary

- Status: Accepted
- Date: 2026-08-23
- Stage: v0.7 architecture / D0

## Context

v0.6 established one durable Agent identity plane, one Inbox and delivery
protocol, one process-local `ProcessAgentSupervisor`, child-first lifecycle
convergence and five model-visible control Tools. That kernel is deliberately
strict because create, cancellation and close have real concurrency ownership
semantics.

v0.7 must add hierarchical Budget enforcement, isolated managed Git
workspaces, immutable Patch Artifacts, verification, human approval and typed
workflows. Putting those concerns into `ProcessAgentSupervisor`,
`AgentRuntime` or `AgentLoop` would make the v0.6 concurrency kernel own policy,
filesystem and promotion state. Building a second scheduler would instead
split the durable and live facts.

Before those capabilities are implemented, D0 establishes the dependency
direction and the authority boundary they must use.

## Decision

### 1. The v0.6 Supervisor remains the only process-local scheduler

`ProcessAgentSupervisor` continues to own Activations, FIFO delivery,
single-flight creation, cancellation convergence, subtree disposal and close.
Future managed orchestration wraps one instance through the public
`AgentSupervisor` protocol; it does not subclass, mirror or replace its
Activation tables.

The future managed create saga belongs to an application control service above
that protocol:

```text
host request
  -> reserve Budget
  -> provision provisional Workspace
  -> AgentSupervisor.create(...)
  -> reconcile durable Directory identity
  -> commit/release Budget
  -> attach/release/quarantine Workspace
```

Each step delegates to one domain owner. The coordinator stores no second
Directory, Budget ledger, Workspace registry or run result cache.

### 2. D0 narrows the existing Tool adapter to public seams

`SupervisorToolset` depends on `AgentSupervisor`, not on
`ProcessAgentSupervisor`. The protocol exposes the Supervisor's `EventStore`
only so a host adapter can prove that Tool authority and control operations
share the same durable identity boundary; callers must not append control facts
through it.

`AgentToolAuthority` owns Tool authorization. It stores only a host-bound owner
id and an `AgentDirectoryReader`. Every decision replays a fresh Directory and
checks the caller Session and strict-descendant relationship from the same
snapshot. It never reads a live Activation or caches an ownership graph.

`ChildProvisioningPolicy` is a mandatory host seam. It receives a durable owner
record and the model's requested preset/workspace intent, then either rejects
or returns a `ChildProvisioningProposal` containing exactly:

- an approved preset;
- an approved workspace id;
- bounded descriptive metadata.

There is no permissive default. The proposal cannot select an owner, Budget,
capability grant, Provider, model, prompt, concrete Runtime or task. The owner
is host-bound, and `AgentActivationFactory` remains the only component that
resolves an approved preset/workspace into executable capabilities and a
directory. Work remains a separate durable `send_agent_message` operation;
`spawn_agent` does not smuggle a task into identity creation.

### 3. Future v0.7 domains retain one fact owner each

- Budget reservations, commits, releases and charges belong to one append-only
  Budget ledger and projector.
- Workspace lifecycle and path containment belong to one `WorkspaceManager`.
- Patch identity and immutable bytes belong to one Artifact service.
- Verification, approval and compare-and-swap promotion belong to one
  Promotion service.
- Workflow nodes call those public services and persist orchestration facts;
  they do not reach into Supervisor, Runtime or plugin internals.

`AgentLoop` gains no Budget, Workspace, Patch, Workflow or approval branch.
An `AgentActivationFactory` may use managed Workspace/process-slot services
before it creates the existing single-Agent Runtime, but the Runtime remains a
one-Agent execution facade.

### 4. Threat and authority boundary

Model output, Tool arguments, plugin payloads, child reports and workspace
contents are untrusted input. A model may request intent, but it cannot choose
its owner, grant itself Budget or capabilities, select an arbitrary filesystem
path, replace the Provider/model, change the verifier, approve its own Patch or
move a Git ref.

Plugins remain trusted, in-process application extensions under the v0.6
contract. A managed Workspace is a containment and ownership boundary, not an
OS sandbox. Candidate code still runs with the host user's authority unless a
future process sandbox explicitly changes that contract.

Durable events remain the fact source. Human approval is an explicit host fact,
not a model assertion. Promotion must later compare the approved immutable
artifact and expected target ref; no model-facing Tool may bypass that service.

### 5. Incremental implementation boundary

D0 changes only protocol seams, authority projection, host provisioning policy,
architecture tests and documentation. It adds no Budget event, Workspace,
Patch, Workflow, CLI command or model-visible Tool field. Subsequent stages are:

1. A: replace the unenforced v0.6 Budget DTO semantics with the v0.7 ledger;
2. B: enforce reservations and charges at owned execution boundaries;
3. C: implement managed Git workspace lifecycle;
4. D1: produce immutable Patch Artifacts;
5. D2: verify, approve and promote through Git compare-and-swap;
6. E: add a typed Workflow coordinator above the services;
7. F: add explicit CLI/product wiring and release validation.

## Rejected alternatives

### Add managed state to `ProcessAgentSupervisor`

Rejected because filesystem, Budget and promotion ownership would enlarge the
already concurrency-sensitive v0.6 kernel and make its close protocol govern
unrelated domains.

### Add Budget or Workspace branches to `AgentLoop` or `AgentRuntime`

Rejected because neither is the multi-Agent authority. The loop defines one
Session/Turn/Step execution; the Runtime is one Agent's facade.

### Provide a pass-through provisioning policy

Rejected because a hidden default would turn model strings into host policy and
make future grants, workspaces and presets fail open.

### Cache the Directory or ownership graph in the Tool adapter

Rejected because authority would become stale after durable Agent creation or
ownership changes and would compete with the Event Log fact source.

### Put a task or concrete Runtime choices in the spawn proposal

Rejected because identity creation, work acceptance and Runtime resolution are
separate contracts. Combining them would make retries and authorization
ambiguous.

### Build a second managed Supervisor

Rejected because two Activation tables and two close protocols could both
claim the same durable Agent. Managed orchestration must compose the existing
public Supervisor.

## Consequences and boundaries

- The five existing Tool schemas and durable event protocol do not change.
- Existing v0.6 concurrency and cancellation code is not refactored in D0.
- Hosts constructing `SupervisorToolset` must now supply an explicit
  `ChildProvisioningPolicy`.
- D0 creates no managed Workspace and enforces no Budget.
- Version remains `0.6.0`; D0 is not a v0.7 release.
