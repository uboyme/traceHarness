# Architecture

## Goal

TraceHarness v0.6 is a small coding-agent runtime whose current implementation can grow
without turning `AgentLoop` into a feature switchboard. The stable center is protocol
semantics, not a specific provider, tool set or UI.

That rule is why the plugin system sits in the runtime *assembly* layer rather than
inside the loop: plugin tools, prompt sections and services join the existing registries,
and `AgentLoop` has no reference to `PluginManager`. See
[`plugins.md`](plugins.md) and [ADR-0007](adr/0007-transactional-plugin-activation.md).

## Layers

```text
Applications
  CLI, SDK, evaluation, inspector

Control
  AgentRuntime, PluginCompositionCoordinator, AgentLoop, ContinuationRuntime
  AgentRegistrar, AgentDirectory (durable Agent identity)
  AgentInboxService, AgentInbox (durable accepted-message facts)
  ProcessAgentSupervisor, AgentDeliveryService, AgentDeliveryLog
    (process-local Activations; durable claim and outcome facts)
  SupervisorToolset -> AgentSupervisor protocol
  AgentToolAuthority, ChildProvisioningPolicy
    (fresh durable authorization; explicit host provisioning decision)
  WorkspaceManagedAgentSupervisor -> AgentSupervisor protocol
  WorkspaceService, ManagedWorkspaceAccessPolicy
    (managed Git lifecycle; durable attach; explicit Tool capability)

Capabilities
  CompositionRuntime, PromptAssembler, LlmRegistry, ToolRuntime, CompletionVerifier

State
  SessionService, EventStore, SessionEventFeed, SurfaceProjector, RecoveryService
  WorkspaceCatalog; LocalGitWorkspaceProvider (host filesystem/Git effects)

Kernel
  ScopeChain, ServiceRegistry/View, CompositionOverlayPlan, HookDispatcher, Activation,
  Lifespan, OwnedTaskSet
```

Dependencies point downward. `traceh.api` contains structural protocols and frozen value
types that third-party packages import, re-exported for plugin authors through
`traceh.plugins`. Runtime internals are not plugin API: `PluginContext` exposes reversible
Tool, Prompt, Service, Provider, Policy, Middleware and named Verifier registrations plus
cleanups, owned tasks and configuration, and no `AgentRuntime`, `AgentLoop`, `EventStore`,
`ToolRegistry` or `PromptAssembler` object.

Default Runtime assembly builds one Application → Workspace → Preset → Agent Service chain
for every plugin composition candidate. The effective read-only Service view is captured by
the Composition Generation and Step Lease; plugin setup still writes only to the candidate's
application Registry. Assembly preflights the complete chain before mutating the caller-owned
Application Registry, and public candidate preparation preserves existing child-layer
bindings. D1 Scope remains optional for custom ActivationSets that implement the earlier D0
ownership contract.

D2 applies explicit Tool, Prompt and Policy bindings in the same four-layer order during
candidate assembly. It resolves them on private forks and passes one effective ToolRegistry,
PromptAssembler and Policy tuple through the existing ActivationSet → Generation → Lease →
Snapshot path. Application plugin Tool/Prompt contributions are revalidated against child
overlays before health checks. This is host assembly, not child-scope plugin setup, and it
does not add a second scoped runtime or an `AgentLoop` dependency.

D3 stages application-plugin Provider, Policy, Middleware and named Verifier contributions
in the same candidate transaction. Provider and Verifier selection is explicit. The selected
Verifier is carried by the Composition Generation and remains under the same Step Lease as
Provider and ToolRuntime. Public candidate preparation also transfers an immutable capability
receipt; Generation construction revalidates it so an await between preparation and claim
cannot split Snapshot names from executable Registry keys. Activation and receipt construction
are one transaction: if the hand-off cannot produce an ActivationSet, the temporary Manager
retains ownership and converges reverse cleanup before returning. A simultaneous cleanup
failure is grouped through `BaseExceptionGroup`, retaining direct `BaseException` interrupts
without changing the ordinary `ExceptionGroup` result. EventStore remains a process-
lifetime Runtime dependency rather than a retireable Generation-owned plugin contribution.

"Frozen" is the dataclass guarantee - fields cannot be rebound - not deep immutability. An
`EventEnvelope.data` graph stays a mutable JSON structure, so an `EventStore` must hand out
detached copies rather than references into its own history; see
[`event-protocol.md`](event-protocol.md).

`SessionEventFeed` sits in the State layer but points upward: it is an in-process
notification channel that applications may subscribe to, never a store the runtime reads
from. Consumers receive the read-only `EventFeed` interface - publication is private to the
`PublishingEventStore` that owns the feed, so an observer cannot announce an event the store
never accepted. Notification means "the store accepted this for the `Durability` requested",
not "this is fsynced"; the feed adds no persisted fact and no crash durability of its own.
Dependencies still point downward - the CLI timeline depends on the feed, and nothing in
`runtime/` depends on the CLI. See [`event-feed.md`](event-feed.md).

## Thin loop

`AgentLoop.run_turn()` owns only these transitions:

1. Accept and claim an Inbox message.
2. Open a Turn.
3. Open a Step.
4. persist pending user messages;
5. freeze a Composition Snapshot;
6. build and persist a request;
7. execute one model attempt;
8. execute the returned tool batch;
9. close the Step;
10. ask `ContinuationRuntime` whether to continue;
11. close the Turn.

Provider retry, prompt sections, policies, verification and persistence are delegated to
services. Subagents are ordinary tools backed by `AgentSupervisor`, not branches in this
loop; v0.7 Budget enforcement and managed Workspace lifecycle already remain above this
boundary, and Artifact, Promotion and Workflow services must do the same.

## State planes

### Session Stream

The Session Stream contains user/model/tool/lifecycle facts. It is the source for runtime
state, replay and model Surface.

### Effect Stream

The Effect Stream records side-effect intent, dispatch and outcome. It closes the
otherwise invisible crash window between performing an operation and saving its model-
visible result.

### Agent Directory Stream

`agents:directory` is one control-plane stream per `EventStore`, not a per-session stream.
It records which Agents exist and which Session each owns. A Session Stream is one Agent's
execution history; this stream is the identity plane above it. They are kept apart so that
enumerating Agents does not require reading every Session and so that one Agent's history
cannot assert facts about another. It adds no model-visible content: it never enters the
Surface, recovery, invariants or the request fingerprint.

`traceh.agents` depends only on `traceh.api`, the `EventStore` and the shared convergence and
text-safety helpers. It does not import `AgentRuntime`, `AgentLoop` or `PluginManager`, and
only `traceh.supervision` imports it. The Supervisor holds Activations and reads identity from
it; the dependency never points back. `AgentRecord` is durable identity, `AgentHandle` is an
Activation, and stopping or rebuilding the latter cannot change the former. See
[ADR-0019](adr/0019-durable-agent-identity-and-activation-boundary.md).

Stage D projects `owner_agent_id` into a process-local `AgentOwnershipGraph`. Create/resume/wake
take lineage admission leases; subtree disposal closes that admission, re-reads the Directory
after candidate convergence, and cleans descendants before owners. Unpinned retries are matched
through their durable request identity rather than a fresh provisional UUID. The graph is not persisted
or mutable identity, and it ignores message source and `forked_from_session_id`. This logic
stays in `traceh.supervision`, so `AgentRuntime` and `AgentLoop` remain unaware. See
[ADR-0022](adr/0022-agent-lifecycle-ownership-and-quiescent-disposal.md). A malformed final
Directory is reported but cannot prevent release of known process-local Activations. Starting
`aclose()` atomically retains every registered tree Task until close has observed its result, so a
cancelled public disposer cannot erase shutdown failure evidence. There is still no
cross-process lifecycle lease or cold recovery.

Stage E exposes that same control plane through a host-wired `SupervisorToolset`. Its five
ordinary tools create, send, wait, stop and collect a durable report for strict ownership
descendants; the owner identity, authoritative Store and caller Session are host-bound rather
than model arguments. Deterministic Tool-call identities make spawn/send retries idempotent,
and a cancelled spawn converges both the create transaction and any committed child subtree
before returning. Reports are reconstructed from Inbox, delivery and Session facts, not from a
second result cache. This facade remains in `traceh.supervision`; it does not change
`AgentLoop`, `AgentRuntime` or `PluginManager`. See
[ADR-0023](adr/0023-supervisor-backed-subagent-tools.md). Default CLI wiring and cold recovery
remain future work. v0.7-B offers explicit host-wired Budget enforcement and v0.7-C wraps the
same public Supervisor with managed Git workspace provisioning without moving either policy
into the concurrency kernel. Patch Artifacts remain future work. Child reserve and
Token/wall START writes are themselves owned tasks: same-process cancellation waits for release
or conservative settlement before returning, while recovery of STARTED facts left by a hard
process crash remains future work.

v0.7 D0 narrows that adapter without changing its five schemas or any durable event. The
Toolset now accepts the public `AgentSupervisor` protocol rather than the concrete process
implementation. A separate `AgentToolAuthority` replays a fresh `AgentDirectory` for every
caller/descendant decision, while a mandatory `ChildProvisioningPolicy` converts only
preset/workspace intent into an approved proposal. Owner, Budget, grants and executable
Provider/model/prompt/runtime choices are not proposal fields; the existing
`AgentActivationFactory` remains their runtime-resolution boundary. Future managed
orchestration must wrap one `ProcessAgentSupervisor` and compose domain services rather than
add state to it. See [ADR-0024](adr/0024-v07-managed-agent-control-plane-and-threat-boundary.md)
and [ADR-0025](adr/0025-hierarchical-budget-breaking-cutover.md).

### Budget Ledger Stream

v0.7-A adds one `budgets:ledger` stream above Agent identity. It records
host-issued root grants, child reservations, optional commit acknowledgements,
converged release, usage charges, usage reservation lifecycles and terminal
accounts. Remaining capacity is always projected from immutable facts; it is
not stored in `AgentRuntime`, the Supervisor or a mutable balance table. The
exact `agents:directory` child fact is the only creation commit proof, so a
Budget writer cannot invent an Agent.

The v0.6 `AgentSpec.budget`/`AgentRecord.budget` shape was removed rather than
kept as a second meaning of authority. New Agent identity uses schema version
2, while old schema-version-1 Budget history fails closed and is never
auto-migrated or deleted. Stage B composes thin, explicit host adapters around
the existing create/model/Step/Tool/process boundaries: child creation remains
one Supervisor saga, process capacity is a process-local ancestor lease, and
model/wall usage uses reserve/start/settle rather than a repeatable permission.
Tool admission happens in model order between ordinary Policy and dispatch.
`AgentLoop`, `AgentRuntime` and `ProcessAgentSupervisor` still own no Budget
state or Budget branch. See
[ADR-0026](adr/0026-append-only-hierarchical-budget-ledger.md) and
[ADR-0027](adr/0027-budget-enforcement-at-owned-boundaries.md).

### Workspace Catalog Stream

v0.7-C adds one `workspaces:catalog` stream per EventStore. It records a
framework-generated workspace identity, host-approved source id, exact Git base
commit and access capability before materialization, then one attachment,
quarantine or release lifecycle. The catalog is the only durable workspace
lifecycle view; concrete filesystem paths stay inside the host provider and do
not enter model input, Session Surface or request fingerprints.

`LocalGitWorkspaceProvider` creates detached, commit-pinned worktrees beneath
one explicit managed root. It validates the source/common-dir/worktree registry,
HEAD and regular `.git` marker. The registry's unique admin entry must point
back to this target marker, and Git must resolve the marker back to that exact
admin directory; swapping sibling markers therefore fails identity validation.
It rejects symlink/junction/reparse traversal and occupied paths, disables Git
hooks and prompts, and never force-removes or broadly prunes. Only an exact
registered worktree that is clean at its base can be removed. Dirty, unsafe or
unknowable state is quarantined for inspection.

`WorkspaceManagedAgentSupervisor` wraps the public `AgentSupervisor`; it does
not own Activations, Inbox, Delivery or Directory state. Its create saga
provisions first, delegates one ordinary Supervisor create, then uses fresh
Directory and Session facts to attach. Failure or cancellation converges Agent
cleanup and workspace release/quarantine before returning. Agent stop/host
close preserves worktrees because review and later artifacts outlive a live
Activation. Its shared wrapper lock also keeps `resume()` admission, inner
resume, post-validation and failure cleanup visible to `aclose()` until the
public operation returns. `ManagedWorkspaceAccessPolicy` can explicitly restrict a
read-only Agent to pure/workspace-read Tools, but it is not an OS sandbox and
does not constrain arbitrary code running as the host user. See
[ADR-0028](adr/0028-managed-git-workspace-lifecycle.md).

### Agent Inbox Streams

`agent-inbox:<agent_id>` is one stream per Agent, holding the append-only order in which that
Agent's messages were **accepted**. One stream per Agent rather than a shared one, because
FIFO order is a property of an Agent's Inbox: a shared stream would make one Agent's traffic
advance another's `expected_seq`. Stream ids come from one constructor and are never parsed
back into an `agent_id`.

**Accepted is not processed.** These events record that a message was durably received and
where it sits in that Agent's order. Claiming, execution and outcome live in a separate
delivery stream and have no representation here, and `wakeup` stores the sender's request
rather than an action taken. Like the directory stream, none of this enters the model
Surface, recovery, invariants or the request fingerprint. See
[ADR-0020](adr/0020-durable-agent-inbox-acceptance.md).

### Agent Delivery Streams

`agent-delivery:<agent_id>` records what a live Activation did with an accepted message:
`agent/message-claimed` and then exactly one of `agent/message-completed`,
`agent/message-failed` or `agent/message-cancelled`. It is a separate stream from the Inbox so
that acceptance history stays a plain answer to "what was received" and so that claims do not
contend for the `expected_seq` senders use. A claim carries the Inbox `accepted_seq` as well
as the `message_id`, so replay can prove the two streams agree about which message is running.

The claim append's `expected_seq` is the linearization point that makes a durably accepted
message become exactly one Turn. Nothing may call a model or a tool before that claim is
provably in the log - an unknown outcome faults the Activation rather than retrying, because
retrying is the thing that could double-execute and no ledger correction undoes a tool that
already wrote to a workspace. There is no in-memory queue: the worker re-reads the Inbox and
the delivery log every round and takes the earliest unclaimed message. An open claim blocks
every later FIFO item; Stage C has no stale-claim takeover. Before writing, the service also
re-reads the authoritative Inbox/delivery views and proves the complete Acceptance or open
Claim belongs to that Agent, so a frozen but fabricated/cross-Agent DTO cannot corrupt the log.

Terminal facts carry a stable repository code, plus the real `turn_id` on completion. Model
output, tool results and exception text belong to the Session Event Log and are never copied
here.

`traceh.supervision` reaches an `AgentRuntime` through a four-method protocol - run one
message, cancel the current Turn, dispose, expose Session and `EventStore` identity - and
`AgentRuntime` and `AgentLoop` do not know the package exists. `TurnInput` (in `traceh.api`,
carrying content, a `message_id` and a source) is what lets the control plane's message
identity reach `turn/start` in the Session; a plain `str` keeps the previous behaviour.
Activations are process-local and are not rebuilt after a crash. See
[ADR-0021](adr/0021-process-local-agent-supervisor-and-delivery-lifecycle.md); there is still
no cold recovery, stale-claim takeover or retry policy. Stage E's host-wired subagent
tools are described above; they are not installed by the default CLI. Create/resume
single-flights and all installed Activations are owned by Supervisor disposal: `aclose()`
closes admission once and converges candidates, rollback and runtime cleanup through one
shared Task, while worker exceptions become stable faults rather than idle success.

All three control-plane transactions share `commit_reconciliation.py` for the `EventStore`
commit-point question - did our event land, and can we even tell - while each keeps its own
error mapping.

### Telemetry

Telemetry is intentionally absent from the durable protocol in v0.4. A future
OpenTelemetry plugin should subscribe to NOTIFY hooks and may sample or lose data without
changing recovery semantics. Note that v0.4's `PluginContext` does not yet expose hook
subscription, so such a plugin is not writable today.

## Composition

Every Step obtains an `ActiveComposition` through `CompositionRuntime.lease()` and creates a `CompositionSnapshot` containing provider, model, final system
prompt, visible tool schemas, policies and plugin identities. The snapshot receives a
canonical SHA-256 revision.

`plugin identities` are real data as of v0.4: `traceh.core` at the single source version,
plus each activated external plugin's true id and version. Replay rebuilds them from the
event, so a plugin-affected request stays reconstructable.

The lease retains the exact Provider, Tool Runtime and Verifier objects while the Step runs.
Step-local freezing prevents a plugin composition change from changing model, tools or
completion evidence halfway through one Step. The current runtime publishes immutable Generations; old
Leases retain the old Generation until their last release, after which Drain owns cleanup.
Generation identity stays internal and does not change the request or event schema.

`AgentRuntime` is the public facade and owns the active-Turn table plus overall shutdown.
`PluginCompositionCoordinator` owns the plugin candidate transaction, the shared
Turn-admission/migration gate, durable Session plugin-identity checks and in-flight
replacement and pre-registration admission convergence. It does not execute a Turn and it
does not create a second Session fact source. `AgentLoop` still sees only
`CompositionRuntime.lease()`.

## Extension rule

A feature belongs in the loop only when it changes the meaning of Session, Turn or Step.
Otherwise it should be one of:

- a service implementation;
- a registry entry;
- a prompt section;
- a tool policy;
- continuation rule;
- verifier;
- projection;
- observer hook;
- orchestration above the loop.
