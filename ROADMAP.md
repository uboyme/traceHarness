# Roadmap

The order below preserves the current protocol and keeps orchestration
boundaries narrow. v0.8.0 is released: M3 host-owned Surface compaction and M4
context transparency close the v0.8 line without moving Product state into
`AgentLoop` or adding a second fact source. The final candidate passed the public
real L2 gate and `2758 passed, 7 skipped` full suite; its complete real-Provider
grid measured all 18 attempts and reports quality failures separately from gate
completion. The working tree now implements the v0.9-F0-B minimal Context request
path and completed its targeted gates. F0-C History disclosure and its final
targeted gates are also complete; F0-A/B/C's authorized implementation is closed,
with F1-F4 implemented and F5 governance/evaluator implemented. The retrieval precision correction
and unchanged-input verification are complete: all 11 attempts meet the original thresholds with zero
scope violations. The semantic query still only meets its predeclared lexical floor of zero.
Stop C and release gates remain pending.

## v0.4: Plugin SDK and discovery — done

- ✅ `traceh.plugins` SDK with a `PluginContext` exposing only tools, prompts, services,
  cleanups, owned tasks and configuration.
- ✅ Discovery through `importlib.metadata.entry_points(group="traceh.plugins")`,
  metadata-only: listing never imports a plugin.
- ✅ Explicit enablement (`--plugin` / `TRACEH_PLUGINS`); installing never enables.
- ✅ Manifest, TraceHarness compatibility range and plugin dependencies validated before
  setup; selection validated before import.
- ✅ Setup inside a private `Activation` against staged registries; conflicts checked, then
  health checks, then atomic publish; reverse-order rollback on failure or cancellation.
- ✅ `traceh plugins list/inspect/doctor`.
- ✅ Plugin lifecycle tests, plus a built and installed example plugin wheel exercised in a
  clean virtual environment.

Definition of done **met**: installing a separate wheel adds a tool and a prompt section
without editing this repository or `AgentLoop`. See
[`docs/plugins.md`](docs/plugins.md) and
[ADR-0007](docs/adr/0007-transactional-plugin-activation.md).

Explicitly deferred out of v0.4: user-facing hot reload, isolated
(out-of-process) plugins, and plugin-supplied providers, policies, middleware, event stores
or verifiers.

## v0.5: Composition generations, scoped overlays and execution plugins — released

- **Stage A — completed:** Generation-backed Composition Runtime is used by both default
  factories and the no-plugin path. Each Step acquires a generation-bound Lease;
  Publish/Retire, last-Lease cleanup, structured Drain failure reporting and repeated
  cancellation convergence are implemented. Model-visible Tool, Prompt, Provider,
  Policy/Middleware names and Plugin Identity inputs are frozen per Generation. Generation
  identity remains internal and is not part of Snapshot revision or Request Fingerprint.
- **Stage B — completed:** `PluginGenerationBuilder` constructs
  each explicit candidate in private Tool, Prompt and Service registry views. Discovery,
  dependency ordering, Manifest validation, setup, conflict checking and health checks
  complete before a `PluginActivationSet` can be published. Successful Activation
  ownership transfers to the new Composition Generation; an old Lease keeps its old
  plugin Tool, Prompt, Service and Owned Task alive until the last Lease exits, then
  cleanup runs in reverse dependency order. Candidate failures roll back immediately,
  repeated cancellation converges before re-raising, and cleanup failures are bounded and
  poison the runtime without skipping other plugins or Generations.
- **Stage B default-mainline result:** both default factories, startup plugins, real
  AgentLoop Steps and `AgentRuntime.dispose()` use the same ActivationSet/Generation/Lease/
  Drain path. SessionService, EventStore, core Provider and built-in Tools are borrowed
  core resources; plugin Activation, Tool, Prompt, Service, Owned Task and cleanup are
  generation-owned. Runtime disposal first cancels and waits for in-flight replacement Tasks;
  internal replacement does not authorize an existing Session to migrate across plugin
  identities.
- **Stage C — completed:** idle `traceh chat` exposes `/plugins`,
  `/plugins reload`, `/plugins use ID [ID ...]` and `/plugins use --none`. All four commands
  use the Stage B Builder → private registries → ActivationSet → Composition Generation →
  publish/Drain path. Same-identity reloads do not append a migration event; identity changes
  append `composition/migration-authorized` with `migration_id`, `source_seq`, `from_plugins`
  and `to_plugins` after candidate validation and Session-head CAS. A shared Runtime Gate
  blocks Turn admission while a migration confirms global quiescence and publishes. Append
  cancellation is reconciled by migration id; durable authorization followed by publish
  failure is fail-closed.
- **Stage D0 — completed as a structural checkpoint:** plugin candidate replacement,
  Session identity verification/migration, the shared admission Gate and in-flight
  replacement/admission convergence live in `PluginCompositionCoordinator`.
  `AgentRuntime` remains the public facade, active-Turn owner and overall shutdown owner;
  `AgentLoop` is unchanged. D0 adds no Scope Overlay and no new user command.
- **Stage D1 — completed as the Service Scope foundation:** default Runtime assembly and
  every plugin candidate now build Application → Workspace → Preset → Agent Service layers.
  Resolution prefers the nearest layer; same-layer conflicts, missing explicit replacement
  and API-major mismatch have stable structured diagnostics. Published Scope/Service views
  are read-only and captured by the Composition Generation and Step Lease, so a replacement
  cannot mutate a running Step. Child overrides are revalidated after application plugin
  Services publish, so setup order cannot bypass the same rules. Plugin setup still runs at
  application scope only. `replace` is a strict boolean; failed assembly is transactional;
  public candidate preparation preserves child-layer bindings; and D0 custom ActivationSets
  remain compatible when they do not opt into the D1 Scope view.
- **Stage D2 — completed as the model-visible composition overlay:** both default factories
  and every plugin candidate accept explicit Application → Workspace → Preset → Agent
  Tool, Prompt and Policy bindings. Same-layer duplicates and cross-layer overrides require
  strict `replace=True`; plugin application Tool/Prompt contributions are revalidated before
  health checks; and one effective ToolRegistry, PromptAssembler and Policy tuple enters the
  existing ActivationSet → Generation → Lease → Snapshot path. There is no parallel scoped
  runtime, no new persistent fact and no `AgentLoop` change. These are host assembly bindings,
  not child-scope plugin setup or a new Policy contribution method on `PluginContext`.
- **Stage D3 — completed as the execution-capability contribution layer:** application
  plugins may register Provider, Policy, Middleware and named Verifier contributions. They
  are staged during setup, then the contribution surface is frozen before conflict and health
  phases; mutable capability objects cannot change their captured registration names during
  health or after a public candidate hand-off. ActivationSet retains a capability receipt and
  Generation revalidates it before claim. Activation and hand-off construction are one
  transaction: a receipt/Scope failure before ownership transfer disposes the temporary
  Manager and all Owned Tasks before returning; simultaneous hand-off/cleanup failures use
  `BaseExceptionGroup` so direct `BaseException` interruptions remain visible. They are
  transferred through the existing ActivationSet → Generation → Step Lease
  path; provider identity is checked against the candidate registry, while legacy custom
  ActivationSets without a D3 registry retain their D0 replacement fallback. Provider and
  Verifier selection is explicit; merely enabling a plugin cannot replace either default.
  Verifier execution remains inside the same Step Lease as model and tools. EventStore is deliberately not a
  hot-reloadable contribution: it is the Runtime/Session process-lifetime fact source and
  first needs a separately pinned ownership design.
- **Release candidate and `0.5.0` release — completed:** the author-facing Tool, Policy,
  Middleware and Verifier contracts are exported from `traceh.plugins`; the independently
  packaged `traceh-python-quality-plugin` contributes a read-only project Tool, guidance,
  environment Policy and explicit named test Verifier. Clean-venv Wheel E2E builds the core,
  the Skill example and Python Quality as separate distributions, installs them offline, and
  verifies discovery, diagnosis, Policy denial, Tool execution, Verifier evidence, Snapshot,
  invariants and request reconstruction. Chat `/help` now points operators from installation
  through discovery and doctor.

Still deferred after v0.5.0: running `pip install`/`uninstall` for Wheels, forced Python module
reload, file watching, scoped plugin setup, EventStore plugin contribution, isolated plugins,
multi-agent, Workflow, MCP, TUI and model streaming.

The D2/D3 assembly contract now proves that two independently built Runtime/Agent scopes can
see different Tool/Prompt/Policy compositions, and the existing Lease contract prevents a
published Generation from changing an in-progress Step's Provider, tools or Verifier. A
product-level two-Agent demo still depends on the v0.6 `AgentSupervisor`; D3 does not claim
that capability.

## Controlled capability evolution — L1-L4 released in v0.6, L5 planned

- **L1 — released in v0.6:** `traceh-plugin-creator-skill-plugin` is a real external
  Wheel that supplies a short Prompt and packaged workflow/contract/template/checklist through
  one `PURE_READ` Tool. The Agent writes source only in a dedicated Candidate Workspace using
  existing coding Tools. It does not build, test, install, enable or approve its own output,
  and the core Runtime/AgentLoop/PluginManager are unchanged. Wheel acceptance builds every
  project from an isolated declared-source copy and audits archive members before installation;
  the packaged contract also states that Verifier uses Generation/Step Lease but not
  `CompositionSnapshot`, with results recorded as `verification/result`.
- **L2 — released in v0.6:** `traceh plugins validate` takes an explicit source-only
  candidate, trusted core Git repository, new evidence directory and dependency source. It
  clones the trusted `HEAD`, reads that clone's compatibility version, rejects source reparse
  points and host-control namespaces, builds and anchors exact audited Wheel bytes, uses separate
  candidate/regression venvs, runs host-owned metadata/doctor/test/core gates, then rechecks the
  Wheel and atomically commits the complete evidence directory after all 13 gates pass. This is
  filesystem/Python environment isolation, not an OS sandbox; see ADR-0016.
- **L3 — released in v0.6:** `traceh plugins compare` consumes the exact successful L2
  evidence bundle, clones the core commit named by that evidence and runs a fixed suite from that
  trusted commit in two otherwise identical venvs. Dependencies resolve once into a frozen
  SHA-256-addressed Wheel set; both arms install offline from it and must retain identical
  Distribution receipts. Only the candidate arm enables the exact target plugin identity. A
  host-owned probe requires a closed durable Turn/Step lifecycle, agreeing reason/Step count and
  matching Composition Snapshot plugin identity before it reads Verifier/request-reconstruction
  evidence and reports `improved`, `regressed`, `mixed` or `no-change` without approving or
  installing anything. The first three-case Python Quality
  suite checks one capability gain, one ordinary repair with no regression and one honest
  verification failure; see ADR-0017.
- **L4 — released in v0.6:** `traceh plugins promote` revalidates the exact L2/L3
  evidence and explicitly selected target environment. Its first invocation writes a Chinese
  capability/risk/evidence card and SHA-256 approval digest without changing the Registry or
  target. Only a second invocation carrying that exact digest can install the audited Wheel. The
  digest binds canonical L3 Case/Gate/Wheel evidence, artifact, Registry, interpreter, complete
  installed-package content and current receipts; known regressions,
  unmanaged installs, dependency drift and stale approval fail closed. A shallow locked Registry
  records `stable / installing / rollbacking` state, immutable artifacts and content receipts;
  one target-environment owner/lock prevents Registry, interpreter-alias, plugin-id and
  Distribution split ownership; L4 v1 permits one active managed Distribution chain per target
  until a complete first-version rollback releases the environment for handoff;
  `traceh plugins rollback` restores the previous exact Wheel or uninstalls a first promotion,
  including recovery from an interrupted transition. See ADR-0018.
- **L5:** derive repeated weakness evidence from existing Sessions/Evaluation and propose a
  candidate; the Agent may propose and implement, but cannot approve or promote itself.

All five levels are a development control plane outside the execution plane. They must reuse
the public Plugin SDK, Verifier/Evaluation, Generation/Lease/Drain and Session evidence rather
than adding build/test/approval logic to `AgentRuntime` or a second plugin loader. L1 is
recorded in [ADR-0015](docs/adr/0015-source-only-plugin-candidate-authoring-skill.md), L2 in
[ADR-0016](docs/adr/0016-independent-plugin-candidate-validation.md), L3 in
[ADR-0017](docs/adr/0017-host-owned-baseline-candidate-comparison.md), and L4 in
[ADR-0018](docs/adr/0018-human-approved-exact-plugin-promotion.md).

## v0.6: AgentSupervisor and subagents — released

- **Stage A — completed:** durable Agent identity is separated from live Activation.
  `traceh.agents` records `agent/created` on its own `agents:directory` control-plane stream
  in the existing `EventStore`, and rebuilds `AgentRecord` from that stream alone. A fresh
  process holding only a store recovers every identity; an `AgentRuntime`, Task or
  `AgentHandle` is an Activation that may stop and restart without changing it. Lookup by
  `agent_id` and by `session_id` is supported, two Agents cannot own one Session, and replay
  fails closed on duplicate, malformed or contradictory facts rather than repairing them —
  there is no last-write-wins registry semantics. Creation is a transaction whose
  linearization point is the append's `expected_seq`, carried from the directory read; a
  caller-supplied `request_id` makes retries idempotent; and a failed or cancelled append is
  reconciled by re-reading the stream rather than assumed to have written nothing, reporting
  committed/not-committed/unknown rather than collapsing the third case into the second.
  Write-side and replay-side validation share one definition, so the writer cannot append a
  fact replay would reject; payloads are gated on stream, schema version and an exact key
  set; direct `BaseException` interrupts propagate unrewritten; and every directory lookup
  returns a detached record so a caller cannot write through the shared projector.
  History lineage (`forked_from_session_id`), lifecycle ownership (`owner_agent_id`) and
  communication stay separate relations, and communication has no field in the creation fact
  at all. `AgentLoop`, `AgentRuntime` and `PluginManager` are unchanged. See
  [ADR-0019](docs/adr/0019-durable-agent-identity-and-activation-boundary.md).
- **Stage A explicitly excludes**, and no part of it may be described as delivering: a live
  `AgentSupervisor`, single-activation enforcement, Inbox, message delivery or wakeup,
  subagent tools, parent/child disposal, and Agent cold recovery. `AgentSupervisor`'s methods
  had no implementation in Stage A; Stage C now supplies the process-local implementation.
- **Stage B — completed as the durable Inbox fact layer:** one append-only FIFO acceptance
  history per Agent, on its own `agent-inbox:<agent_id>` stream in the existing `EventStore`.
  `AgentInboxService.accept()` freezes the request before its first `await`, requires the
  target Agent to exist in the Stage A directory, and appends `agent/message-accepted` with
  `expected_seq` carried from the Inbox read; a caller-supplied `message_id` makes retries
  idempotent and reusing one for a different message is rejected. `AgentInbox` rebuilds the
  same order from the log alone and fails closed on duplicate ids, unknown event types,
  unsupported schema versions, inexact payload key sets, wrong-stream events and malformed
  fields. `content` is prose rather than an identifier - multi-line is legal, but it is
  bounded and must be UTF-8 encodable; `target` is checked against `MessageTarget` and
  `wakeup` is strictly `bool`. Both control-plane transactions now share one commit
  reconciliation with `True`/`False`/unknown, and `AgentRegistrar`'s contract is unchanged.
  See [ADR-0020](docs/adr/0020-durable-agent-inbox-acceptance.md).
- **Stage B explicitly excluded**, and nothing in that layer may be described as delivering:
  **accepted is not processed.** The Inbox protocol cannot express delivery, claim,
  completion, failure or retry, and `wakeup` records the sender's request rather than waking
  anything. Stage C supplies the consumer; the Inbox stream itself is unchanged.
- **Stage C — completed as the process-local Supervisor and delivery lifecycle:** a durably
  accepted `NEW_TURN` message now becomes exactly one claim, one Turn on that Agent's own
  Session, and one terminal fact. `ProcessAgentSupervisor` owns at most one Activation per
  Agent and per Session, consumes the Inbox in strict FIFO by re-reading the log every round,
  and reaches an `AgentRuntime` only through a four-method execution protocol.
  `AgentDeliveryService` appends `agent/message-claimed` and exactly one of
  `agent/message-completed`, `agent/message-failed` or `agent/message-cancelled` to a separate
  `agent-delivery:<agent_id>` stream, and `AgentDeliveryLog` fails closed against the Inbox it
  references. It also rejects a claim that skips the FIFO head; an open claim blocks every
  later message. Claim/terminal transactions re-read and prove the authoritative Agent,
  Acceptance, delivery view and open Claim before append, so cross-Agent or fabricated DTOs
  write nothing. Nothing calls a model or a tool before the claim is provably in the log; an
  unknown claim outcome faults the Activation rather than retrying. `TurnInput` lets the
  control plane's `message_id` and source reach `turn/start`, so a completion can carry the
  real `turn_id`. `create()`, `resume()`, `send()`, `interrupt()`, `wait_idle()` and
  `dispose()`/`aclose()` have real semantics. Create single-flight compares the complete
  request rather than `request_id` alone; disposal owns in-flight create/resume candidates as
  well as installed Activations; worker failures become stable faults; runtime cleanup failure
  is replayed rather than hidden. Repeated cancellation cannot release shutdown early.
  See [ADR-0021](docs/adr/0021-process-local-agent-supervisor-and-delivery-lifecycle.md).
- **Stage C explicitly excludes**, and no part of it may be described as delivering: Agent
  cold recovery and stale-claim takeover, automatic retry, `MessageTarget.NEXT_STEP` delivery
  (it is refused before acceptance), cross-process activation uniqueness, subagent tools,
  parent/child disposal, workspaces, hierarchical budgets and Workflow. Version remains
  `0.5.0`; Stage C is not a v0.6 release.
- **Stage D — completed as lifecycle ownership and quiescent subtree disposal:**
  `AgentOwnershipGraph` is projected from the durable Directory rather than maintained as a
  second registry. Create, resume and wakeup take a lineage admission lease; subtree disposal
  closes intersecting admission, converges matching candidate builds, re-reads the Directory,
  and releases descendants before their owner. Unknown or inactive owners are rejected before
  child provisioning; concurrent parent/child disposal joins one cleanup Task per Agent; one
  cleanup failure does not skip the rest of the tree; repeated cancellation cannot escape the
  shared disposal Task. Existing unpinned retries are matched by durable request identity;
  `aclose()` still releases known process-local resources when the Directory is malformed and
  reports each independent cleanup failure once. Starting close atomically retains every
  registered tree-disposal Task until shutdown has observed its result, so cancellation of a
  public disposer cannot erase an in-flight failure. Close otherwise applies the same
  post-order to the durable forest. History
  lineage and communication do not participate. See
  [ADR-0022](docs/adr/0022-agent-lifecycle-ownership-and-quiescent-disposal.md).
- **Stage D explicitly excludes:** model-visible subagent tools, cold recovery, cross-process
  Activation leases, stale-claim takeover, retry policy, Workspace and hierarchical budgets.
  It is a process-local explicit lifecycle guarantee and does not run after a hard process
  crash. Version remains `0.5.0`; Stage D is not a v0.6 release.
- **Stage E — completed as host-bound model tools and durable run reports:**
  `SupervisorToolset` exposes `spawn_agent`, `send_agent_message`, `wait_agent`,
  `stop_agent` and `collect_agent_artifact` as ordinary tools backed by the same
  Supervisor. The host binds owner identity and EventStore; targets must be strict
  owned descendants. Spawn/message ids derive from Tool Call identity; Supervisor-owned
  waiter receipts and an Activation-level retained/abandonment handoff ensure that cancellation
  cleans only an otherwise-unreturned child. Cleanup provenance comes from the actual shared
  create outcome rather than a pre-admission Directory snapshot, while `create`, `resume` and
  wakeup all revoke abandonment when they publicly hand off the Activation. A stale retry or
  overlapping public reuse therefore cannot destroy an already delivered child. Public create
  invocations remain close-owned through post-admission compensation using an operation-level
  return receipt. Registration removal and receipt publication happen atomically under the
  Supervisor lock from a post-return completion Task, so close cannot miss a call that has not
  actually returned. A synchronous exit marker ends caller-Task cancellation permission before
  either successful return or early validation failure reaches the caller, so a delayed receipt
  cannot expose unrelated caller work to shutdown cancellation. `aclose()` joins that complete
  tail without waiting for unrelated caller work. `wait_agent` joins one
  message terminal, not whole-Agent idle, and bounded durable re-read observes terminals
  written by another supported Supervisor. Collection joins durable
  Directory/Inbox/Delivery/Session facts instead of caching `TurnResult`. Repeated
  cancellation cannot escape cleanup, and cleanup failure cannot replace the parent's
  cancellation terminal. History lineage, communication and ownership remain separate. See
  [ADR-0023](docs/adr/0023-supervisor-backed-subagent-tools.md).
- **Stage E explicitly excludes:** default CLI product wiring, Patch Artifacts, Workspace
  branching, hierarchical Budget enforcement, cold recovery, cross-process Activation
  leases, retry/takeover and Workflow. `collect_agent_artifact` currently returns a durable
  run report; it does not claim a workspace diff exists.

Definition of done: a suitably assembled parent can create a child with its own Session and
host-resolved Agent Scope, send and collect one durable run, and dispose its subtree without
orphaned process-local work. **Met and released as `0.6.0`.** The release candidate also ran
this complete sequence against a real OpenAI-compatible model, then explicitly resumed the
same durable child identity for a second real Turn and proved a separately gated cancellation
converged to durable `cancelled` evidence. Both Sessions had closed lifecycle state, zero core
invariant violations and zero request-reconstruction violations.

## v0.7: Budgets, workspaces and workflows

The authoritative execution order, non-goals and intended product effect are
kept in [the v0.7 stage plan](docs/plan/TRACEHARNESS_V0.7_STAGE_PLAN.md). The
plan coordinates stages; source, tests, ADRs and the two context documents
remain the authority for implemented facts.

- **D0 — architecture seams (complete, unreleased):** make `SupervisorToolset`
  depend on the public `AgentSupervisor` protocol, move durable Tool authority
  into a fresh-reader `AgentToolAuthority`, require an explicit host
  `ChildProvisioningPolicy`, and freeze the v0.7 dependency/threat boundary.
- **A — hierarchical Budget protocol (complete, unreleased):** replaced the
  unenforced v0.6 Budget DTO path with one append-only reservation/charge
  ledger and schema-v2 Agent identity. Root grants, child holds,
  Directory-backed commit, release, charges, terminal accounts, CAS,
  idempotency and three-state reconciliation now exist. This is an intentional
  pre-1.0 breaking cutover: no legacy/V2 mode, aliases, dual projector or
  automatic migration; old managed histories fail closed and remain untouched.
- **B — Budget enforcement (complete, unreleased):** thin host adapters now
  reserve before managed child/model/wall work, reconcile child grants against
  the durable Directory, terminalize reserve/START cancellation windows,
  enforce Step and ordered Tool admission, and hold process-local ancestor
  slots. The single ledger remains the only durable balance source;
  `AgentLoop`, `AgentRuntime` and `ProcessAgentSupervisor` have no Budget state
  or orchestration branch.
- **C — managed Git workspaces (complete, unreleased):** one append-only
  Workspace Catalog now owns provisional/attached/quarantined/released facts;
  a host-mapped Git provider pins clean sources to exact commits beneath one
  managed root; and a public-Supervisor wrapper reconciles Agent/Session
  identity without adding another scheduler. Dirty, unsafe or uncertain paths
  are quarantined rather than force-deleted. Worktree markers are bound in both
  directions to one exact Git admin registry entry, and wrapper-owned resume
  post-validation converges before close. Read-only is enforced at explicit
  Tool admission and is not claimed as an OS sandbox.
- **D1 — immutable Patch Artifacts (complete, unreleased):** one terminal
  message can now be captured from its exact managed worktree as a full Git
  candidate tree, binary Patch, SHA-256 CAS blob and append-only Manifest.
  Capture uses a temporary index, terminal durable evidence, the Workspace
  lifecycle gate, before/after drift checks and cancellation-safe
  reconciliation. Derived identities are recomputed during replay, CAS owns its
  complete non-reparse parent chain, and inherited Git configuration injection
  is removed. Final text is not a Patch; the read-only collect Tool only reports
  refs that a host already captured.
- **D2 — verification and human promotion (complete, unreleased):** one
  append-only `patch-promotions:ledger` now records Review, Approval and
  Promotion. Review clones a host-configured **bare** target into a temporary
  directory, applies the exact CAS-verified Patch to the exact expected revision
  with `git apply --cached` (never `--3way`), builds a deterministic integration
  tree and single-parent commit, and runs the host's frozen verification plan by
  argv with a positive-list environment and bounded, digest-only evidence.
  Approval requires the exact content digest of a freshly replayed passing
  Review; promotion rebuilds the identical tree and commit inside the target's
  own object database and moves the ref only through
  `git update-ref <ref> <new> <expected-old>`, reconciling the three possible
  ref states against a failed, cancelled or unknown Event append. No model gains
  an approve, merge, promote or `update-ref` Tool, and D2 adds no CLI, Workflow,
  automatic approval, non-bare target or cross-process lease.
- **E — typed Workflow (complete, unreleased):** one append-only
  `workflow:<run_id>` stream and one projector now compose the public
  Supervisor, capture and promotion services as AgentTask, Map, Join,
  Verification and Approval nodes. The DAG is fixed and fully validated before
  anything runs; definitions carry host binding ids rather than prompts, paths
  or policy, and are bound to a canonical definition hash. Agent, message,
  review and map-child identities derive from the run and node, so re-entry is a
  fresh read rather than a repeat. Approval is a human barrier the coordinator
  cannot cross, and the only continuable interrupted state is a clean stop at
  that barrier - every other in-between state fails closed. Agent FIFO, Turn
  execution and Activation lifetime remain owned by the existing Supervisor.
- **F — product gate.** One unified `traceh chat` entry point above everything
  above, split into a contract stage and five implementation stages.
  - **F0 — product contract (complete, unreleased):** `traceh.api.product`
    freezes the ProductTask event vocabulary (nine types, exact key sets, five
    distinct terminals rather than one optional-field blob), its allowed status
    transitions *and the cross-event value consistency order alone cannot
    express*, the requested and resolved mode enums, a durable status
    enum plus a derived view whose three extra answers are `resumable`,
    `unreconciled`, and `interrupted`, the fixed
    host Profile, the preflight binding and Assembly Receipt with computed
    digests over host-resolved assemblies, the temporary Proposal and its
    confirmation rule, one read model and two narrow protocols. Plain chat stays
    plain, a Proposal is temporary and must be confirmed from the same Session
    by a distinct message accepted after the Turn that offered it ended, opening a task binds the
    preflight the person actually confirmed,
    `workflow_run_id == task_id`, both modes reuse one `WorkflowService`, the
    role slot alone decides write authority so only the coder may write, and
    approval and promotion stay host operations the model never sees the
    evidence for. **Contract only:** no implementation package, no event writer,
    projector, service, router, chat command, CLI or default assembly;
    `cli/chat.py` is unchanged and no ProductTask event has ever been written.
  - **F1 — ProductTask fact layer (complete, unreleased):** `traceh.product`
    turns the frozen contract into a durable fact: a strict parser and single
    projector enforcing shape, order and cross-event values; a fresh reader
    returning `None` for an unopened task; host-owned writes for all nine facts
    with exact-payload idempotency, replay-sourced compare-and-swap, shared
    three-state reconciliation and owned-task cancellation convergence;
    confirmation proven by replaying a structurally valid, ordered, human-authored
    Session from the same EventStore and comparing the confirmation acceptance
    sequence with the proposing Turn's durable end, with the Workflow status source bound to
    that same Store; preflight and receipt inputs validated
    before the first append; and a view derived from three fresh reads so
    `unreconciled`, `resumable` and `interrupted` are distinct.
    It records what happens to a task and executes none of it.
  - **F2 — strict Router, Profile Registry and Product Assembly (complete,
    unreleased):** the concrete `TaskRoutingParser` accepts exactly one JSON
    answer shape (`mode`/`reason`) and refuses unknown modes, extra keys,
    over-long, malformed or doubled answers without retrying or mining prose;
    the host `ProductModeRouter` runs inside bounds that come only from an
    explicit `ProductRouterProfile`, derives identity from its actual resolved
    assembly and must match fresh preflight before routing. The single `ProductProfileRegistry` has no
    default and enforces both authority invariants - write access comes from
    the `ProductRole` slot, and the router assembly carries no Tool or grant -
    while `role_assembly_digest`, `router_assembly_digest` and
    `verification_plan_digest` cover what presets actually resolved to, so a
    registry rebinding that keeps every name spelled the same changes the
    binding. `ProductAssemblyService` re-resolves source commit, verification
    plan and the promotion target's repository/ref/revision on every preflight, refuses any drift from the
    confirmed `preflight_digest` *before* spending a routing call, records the
    one durable `product/task-routed` through the F1 writer, and takes the
    receipt's `workflow_definition_hash` from the exact fixed definition that
    would run: `single` is `coder → verification → approval`, `multi` is
    `parent → reviewer → coder →` the same safety tail, neither uses Map/Join
    and only the coder captures. It writes no `product/task-started` and starts
    nothing: no Workflow execution, capture, verification, approval,
    promotion, real model call or chat surface.
  - **F3 — implemented:** the optional `traceh chat --product-config` surface,
    its `/task inspect|approve|reject|cancel|abandon` host commands, Workflow
    execution driven from the product layer and Promotion invoked explicitly by
    the host after a human approval.
  - **F4 — implemented:** `traceh eval` reworked into the ProductTask
    benchmark, replacing the v0.6 runner and refusing old `*/case.json`
    manifests explicitly. It is a second composition root over the same host and
    control plane, creates a throwaway source repository and a one-shot local
    bare target per attempt, and derives every metric from a durable fact source
    or its own monotonic clock. See
    [ADR-0033](docs/adr/0033-product-task-benchmark-as-the-single-eval-path.md).
  - **F5 — in progress:** four corrected OpenAI-compatible ProductTask grids
    are complete as measurements (`18/18` each). They progressed from `11/18`
    with 6 TLS EOF failures, to a proxy-confounded `3/18` with 14 TLS EOFs, then
    to `13/18` with no TLS EOF after a process-local `NO_PROXY` bypass. That run
    still had two strict Router rejections, one Budget exhaustion and two DNS
    lookup errors.
    All resources converged and no retry/fallback was added. The run exposed and
    root-fixed the shared D1/D2 non-recursive tree-diff defect for ordinary files
    in new directories. It also exposed that the production Router request did
    not disclose the parser's existing reason bound; the prompt now states that
    shared contract and a deterministic public-path reverse verification covers
    it without relaxing the parser. A fresh post-fix grid then produced `15/18`:
    auto parsed 6/6 with zero reason rejection, and all three failures were DNS
    lookup errors. Manual Chat then exposed two release blockers: the approval
    barrier was technically complete but opaque, and one cumulative Token
    Budget was also being used as a per-request output ceiling. Product Chat now
    renders bounded durable Workflow/Session/Patch/verifier evidence and
    optional progress without a new fact source; ADR-0034 gives every role and
    Router an explicit request output limit separate from cumulative Budget.
    The exact old Profile shape is rejected. This changed the benchmark Profile,
    so the old `15/18` remains historical. A fresh post-change grid also
    measured 18/18 and produced 15 full successes; its three failures were
    durable Windows DNS errors, with no TLS, Budget, Router or Verifier failure,
    and all Budget/Workspace owners converged. DNS-only probes then identified
    the WLAN's preferred DHCP resolver as the fault. After replacing the DNS
    pair, the Windows resolver passed 200/200 and the Provider-equivalent,
    proxy-free Python admission path passed 50/50. A seventh fresh grid measured
    18/18 and produced 16 full successes with zero DNS/TLS failure; the two
    remaining durable failures were one remote disconnect and one cumulative
    Budget fail-closed after a stochastic coder overrun. A first independent review also
    found one approval-chain P1: an
    internally coherent Review could carry an argv digest not belonging to the
    frozen command. One Promotion-owned frozen-plan validator now protects
    inspect, Review reuse, approve, promote and F4 evidence collection; its two
    public reverse verifications reproduce the old bare-ref movement and the
    false successful measurement when disabled. Independent re-review then
    found the Product recovery branch for an already-durable Promotion was
    returning before that owner check; it now re-enters idempotent promotion,
    and restoring the early return reproduces an incorrect durable Product
    completion. Independent re-review cleared P0/P1/P2 and the single final full
    gate passed 2402 tests with 5 skips. The F5 security scan then found no
    real credential shape, current-machine path or benchmark/provider fixture
    embedded in production. The single package version and validation record
    are now `0.7.0`; clean-input packaging, archive audit and a fresh offline
    installation from the candidate also passed. Tag, push and release remain
    separately gated.

Definition of done: parallel coding children do not share one mutable directory and the
host can promote only an immutable, fixed-suite-verified, human-approved patch while the
existing Agent Runtime, Supervisor concurrency kernel and Event Log fact sources stay singular.
See [ADR-0024](docs/adr/0024-v07-managed-agent-control-plane-and-threat-boundary.md),
[ADR-0025](docs/adr/0025-hierarchical-budget-breaking-cutover.md),
[ADR-0028](docs/adr/0028-managed-git-workspace-lifecycle.md),
[ADR-0029](docs/adr/0029-immutable-patch-artifact-capture.md) and
[ADR-0030](docs/adr/0030-verified-approved-git-ref-promotion.md) and
[ADR-0031](docs/adr/0031-fixed-typed-workflow-above-public-services.md) and
[ADR-0032](docs/adr/0032-unified-chat-product-task-surface.md) and
[ADR-0033](docs/adr/0033-product-task-benchmark-as-the-single-eval-path.md) and
[ADR-0034](docs/adr/0034-separate-product-token-budget-and-request-output-limit.md).

## v0.7.1: Maintenance corrections — released

- Require an exact host-terminal `START` action before a model-suggested
  Product confirmation can create any durable task or allocate resources.
- Converge one AgentLoop-owned Attempt → Step → Turn cancellation finalizer
  before the public Turn returns, even under repeated cancellation.
- Inspect an explicitly selected target venv with an explicit `venv` sysconfig
  scheme under `-I -S`, independent of distro default-scheme patches.
- Repair the two independently built example plugins' Distribution/Manifest
  compatibility ranges exposed by the 0.7.1 offline Wheel gate; their patch
  releases support the existing 0.7 SDK without promising v0.8 compatibility.
- Keep v0.8/v0.9 planning out of this patch release. Frozen review, final full
  regression, clean packaging, offline install, annotated tag and GitHub Release
  are complete.

## v0.8: Reliable local ProductTask host — released

- F0 closes the terminal-error injection path and makes one durable Model
  Attempt correspond to one admitted Provider dispatch. Attempt-scoped cost
  holds are independent; the Session/Step CAS owner now preserves ADR-0027's
  anti-duplicate-dispatch property. The exact composed and Budget-bounded
  dispatch requests are frozen together under ADR-0035. The concrete admission
  must also bind the exact Composition Provider object and host Attempt; Budget
  accounting cannot override or substitute the actual dispatch.
- F1 replaces JSONL with one SQLite production EventStore and rejects legacy data
  explicitly: no migration, dual reader or fallback. CLI commands, Evaluation
  attempts and Evolution comparison cases own explicit store scopes. Same-process
  multi-stream serialization, cross-process CAS, busy ownership, backup and close
  convergence are implemented; Release Stop A is closed.
- ✅ Add bounded retry only for the same Provider, model and frozen dispatch request,
  with typed sanitized failures and independent per-Attempt Budget evidence. No
  Provider/model fallback is added.
- ✅ Refactor the existing Line Chat into one UI-neutral driver and one ephemeral
  activity projection. The Line adapter now consumes typed updates;
  Product/Workflow observation remains read-only and displays cross-stream
  divergence instead of advancing facts from a heartbeat. Partial observation
  startup rolls back its subscribers/watchers, and host assembly binds the
  observer to the exact Feed owned by the injected Publishing EventStore.
- ✅ Add `traceh chat --tui` as an optional Textual adapter over the same driver,
  Session open/recovery and Product control/observation seams. It keeps START
  and Approval host-owned, reconstructs current state from durable facts, and
  combines process-local dirty hints with bounded periodic durable refresh. The
  current single light-theme implementation separates transient operations, durable facts
  and model self-report; renders only legal typed-confirmation gates; shows
  task-bound stream/operation ages and visible shutdown; keeps Cancel reachable
  while START is converging; recovers initial observation reads on a bounded
  cadence; top-aligns the Product summary,
  bottom-anchors the legal gate, fixes fact columns and short-chat growth; and has no legacy TUI
  branch. Its renewed review found two P1s and two P2s; all four are fixed with
  deterministic reverse checks and cleared by short re-review. Later real TUI
  use found the Product-configured requester Chat could still dirty the source
  before START through ordinary coding Tools. The one CLI composition now
  exposes only read/proposal/confirmation Tools and monotonically denies
  declared effectful plugin Tools; real Git Line/Textual counter-examples pass,
  and the correction passes its focused evidence. A later real Provider response
  exposed non-JSON double-triple-quoted multiline Tool strings; ADR-0038 now permits
  only exact schema-declared top-level string normalization and rejects neighboring
  malformed forms, including `NaN` and positive/negative `Infinity`. Widths below
  110 columns use the single-column layout so the fixed 52-column facts row cannot
  wrap. Failure inspection binds Agent/Session/create-request and the deterministic
  Workflow message, then reads only that Turn's stable leaf code beside the Workflow
  wrapper; a later unrelated Turn cannot replace it. One fresh real `qwen-plus` TUI
  ProductTask completed Proposal, typed START, Review, typed Approval and bare-target
  Promotion with clean source and converged Session/Budget/Workspace owners; the
  Provider, TUI, failure-evidence, and cross-owner re-reviews are clear with no
  P0/P1/P2; the latest adjacent-owner gate is `251 passed`. The current single
  adapter subsequently gained the missing output view in place: `Ctrl+T` fresh
  reads Product observation and exact Router/fixed-role Sessions, validates each
  Session before `SurfaceProjector`, groups roles without caching or writes, and
  reports unknown Usage as unavailable while hiding shell arguments and tool
  result bodies. `Ctrl+P` fresh reads full identities and offers explicit copy/
  temporary-file fallback. The Review block again shows bounded changed paths,
  verifier evidence and patch preview. `Ctrl+I`/`Ctrl+R` are not current TUI
  commands; reconciliation remains on the existing Line control path.
- Keep `traceh eval` as the only benchmark path and run one full real-Provider grid
  only as final release evidence.
- F5 has switched the single version source to `0.8.0`, aligned the two independent
  example plugin Wheels with their tested v0.8 SDK ranges, and completed the frozen
  pre-review gates. The global independent review cleared P0/P1, and its two
  non-blocking stale-context P2s were corrected. The pre-replacement candidate's
  full gate was `2496 passed, 7 skipped`, but the TUI replacement and its repaired
  review findings reopened Release Stop C and the current-candidate full gate.
  The replacement candidate subsequently completed `2555 passed, 7 skipped` in
  `3078.80s`, exit 0; later output-visibility, M2 task memory, M3 compaction and
  M4 transparency work made that result historical. The final v0.8.0 candidate
  collected `2765` tests with Textual, passed the public real L2 gate, and
  completed `2758 passed, 7 skipped` in `2712.46s`, exit 0. Clean-input archives,
  core/`[tui]` offline installs and the separately authorized 18-attempt Provider
  grid also completed; v0.8.0 is released.

See the [frozen v0.8 stage plan](docs/plan/TRACEHARNESS_V0.8_STAGE_PLAN.md).

## v0.9: Host-owned long context — F1-F4 and F5 governance/evaluator implemented; frozen precision verification complete

- F0-B implements one request-scoped Context Input path on the existing
  Session/Lease/Request owners. Each Step freezes Context before Composition and
  reuses it for same-request retry. Its one user reference message precedes all
  Surface messages, including an empty wrapper under the explicit empty policy.
  Context is durable and exactly reconstructable, never projected into Surface.
- F0-C introduced Session `context_protocol=2`,
  `f0-c-context-policy-v1` and renderer `context-json-v2`; old F0-B Sessions are
  rejected without migration or fallback. The narrow policy retains seven fields
  and adds nullable `history`, whose seven explicit limits govern the pure reader.
  Default selection remains empty. Raw reads require directory/summary mode plus
  explicit History configuration; there is no raw-only mode or automatic Tool grant.
- F0-C adds bounded current-Session M3 expansion and sequential cursor disclosure.
  Section/chunk use the same closed-Turn pages; oversized Turns retain a stable
  rejected page slot and never receive an accepted receipt. A previously disclosed
  block remains addressable after a wider replacement hides it. A normal PURE_READ
  `request_history_page` Tool returns only a receipt; typed host requests pass
  through `TurnInput.history_requests` and ChatDriver to the existing Session CAS
  owner. Authorized raw pages receive atomic budget before automatic references.
- F0-C fixes Workspace observation to null and freshness to unknown: existing
  base revisions are not execution-time revision facts bound to historical Tool
  results. Real matched/stale observations belong to F3/F4 source integration.
  F2 adds Skill selection/disclosure and exact+FTS ranking; Memory, cross-source configuration and
  governance UI remain later-stage work. F0-C implementation and its final targeted gates are complete.
- F1 implements typed Skill contributions on the existing trusted
  Activation/Generation/Lease lifecycle. Explicit host limits and identity-bound
  roots govern bounded immutable resource snapshots. Catalog metadata/digest enter
  Composition revisions; Context binds the exact catalog without injecting Skill
  messages or granting Tools. Old Leases retain old bytes through reload; existing
  Activation cleanup releases retired resources. F2 owns durable selection,
  retrieval and model disclosure.
- F2 implements host-only durable selection, eligible-corpus BM25 and exact-lane RRF,
  atomic reference budgets, and receipt-only four-tier disclosure for the immediate next Step.
  Index rebuild uses the existing Store worker/transaction/close/backup owner. Historical requests
  reconstruct without consulting current indexes or resources; same-Step retries never retrieve again.
  At the F2 checkpoint the protocol was Session 3, Context 2, f2-context-policy-v1, context-json-v3 and
  SQLite schema 2. Old versions are refused; no migration. Local semantic/reranker work remains deferred.
  The 40-file targeted gate passed 893 cases with 3 Windows symlink skips; six reverse guards passed.
  Release Stop A independently cleared P0/P1; both P2 findings have since been fixed. Context uses the
  shared tier-aware Skill identity, and exact matching preserves complete literal paths with identifier
  boundaries. The 12-file repair gate passed all 273 cases, including 19 new cases; restoring the old
  implementations reproduced eight expected failures before the fixes were restored. See the
  [review record](docs/plan/TRACEHARNESS_V0.9_RELEASE_STOP_A_REVIEW.md). No full suite or L2 was run.
- **F3 authority implemented; B-P1-01 closed, Stop B re-review passed:** host-approved, project-scoped,
  append-only Memory now shares the existing EventStore.
  Models may propose bounded candidates but cannot approve, supersede or revoke
  them. Host-owned durable project bindings connect requester Sessions, later
  Sessions and derived worktrees; temporary workspace ids or matching paths are
  not project authority. Each host-confirmed fact slot has at most one active
  value, with exact-predecessor CAS for replacement and revocation. Runtime host decisions
  borrow the existing Lease; Product worktrees bind after attachment and before dispatch.
  Historical evidence survives workspace release without reviving access. No Memory Context
  injection, retrieval, new SQLite schema, migration or governance UI is included in F3.
  B-P1-01 is fixed: source, consumer and mapping-only reads now share Git registry/admin
  validation. Linked source marker swaps fail before Memory reads or approvals; valid primary
  consumers no longer depend on which checkout is configured as source. Thirteen new cases
  pass; the fix-stage 11-file gate reports 189 passed / 1 skipped. Post-fix independent
  review found P0=0/P1=0/P2=0; the 11 repository files plus four independent probes
  report 193 passed / 1 skipped. Stop B passed; F4 was not part of that review checkpoint. See the [Stop B review](docs/plan/TRACEHARNESS_V0.9_RELEASE_STOP_B_REVIEW.md).
- **v0.9 F4 implemented:** active project Memory now shares exact/eligible BM25/RRF and one
  final Context budget with Skill/History; each source keeps its own authority and corpus statistics.
  Memory disclosure is receipt-only; admitted bodies may remain within the same Turn under current authority and budgets. Frozen requests
  replay historical prefixes/bytes. Opt-in real Git/Tool observations derive History freshness.
   The F4 checkpoint used Session 4 / Context 3 / f4-context-policy-v1 / context-json-v4;
   C2 now uses Session 9 / Context 8 / f5-context-policy-v5,
   renderer context-json-v8 and tokenizer v3; SQLite remains 2. Semantic/reranker are explicitly disabled;
   no download or alternate parser.
  See [ADR-0045](docs/adr/0045-qualified-reference-retrieval-and-history-observations.md).
  F5 governance and frozen evaluation are implemented below; Stop C is passed in the focused authorized scope; final release gates remain outstanding.
- Use exact and SQLite FTS retrieval as the core path, with progressive disclosure,
  two host filters and explicit Context Budget. Local embeddings/rerankers remain
  optional, offline and derived.
- Freeze Context failure prefixes and distinguish runtime Lease identity from
  durable catalog content identity. Same-Step Provider retries reuse the frozen
  input. A History Tool request expires if its same-Turn immediate successor
  Step cannot consume it; it never carries raw history into a later Turn.
- Treat FTS as a Store-owned schema change with explicit rebuild, cancellation,
  shutdown and backup/restore contracts, not tables privately added by a plugin.
- Evaluate retrieval through the existing Product Benchmark runner. Its existing
  attempt owner loads frozen host-only corpus inputs; no second runner, external
  vector database or model self-evaluation is allowed. Create the requester
  Session/project binding before production-service seeding and host assembly;
  freeze sampling, relevance labels, thresholds and resource limits before
  candidate results, including Chinese and mixed code-identifier cases.

The v0.8.0 prerequisite and the authorized F0-A design freeze are complete.
[ADR-0043](docs/adr/0043-step-scoped-context-input-and-retrieval.md),
[ADR-0044](docs/adr/0044-host-owned-project-scope-and-memory-authority.md), and the
[F0 design contract](docs/plan/TRACEHARNESS_V0.9_F0_DESIGN_CONTRACT.md) define the
decisions, fields and owner acceptance criteria. F0-A's nineteen existing-seam
checks are design evidence. F0-B's final 36-file targeted gate collected 1079 cases
and finished with `1076 passed, 3 skipped in 20.22s`; the three Windows skips are
two symlink privilege cases and one invalid NUL path. Its 59 new Context/protocol/
Runtime cases (24/24/11) are included in that result, not added to it. That stage
introduced `context_protocol=1`; F0-C replaced it with 2 at that stage while keeping Context
outer format 1, the empty Composition catalog/digest, ten Request snapshot fields
and SQLite schema 1. F0-C's final 38-file gate collected 1104 cases and finished
with `1100 passed, 4 skipped in 31.98s`. Its 81 new History cases (reader 37,
requests 12, Tool 16, Runtime 16) are included, not added. The Windows skips were
two SQLite file symlink privilege cases, one Tool directory symlink privilege
case and one CLI invalid NUL path. Compileall, changed-Python Ruff (40 files),
bounded hardcoding checks (19 production files) and two independent review
partitions passed; seven reverse-protection checks failed for the intended causes
before restoring the guards. F0-A/B/C's authorized implementation is closed,
F1 and F2 are also implemented with targeted validation; Stop A independently cleared P0/P1 and both recorded P2 findings are now fixed.
Release-level full-suite/L2 gates have not run.
Tests ran from an isolated empty temporary working directory without loading the
repository's real `.env`. Compileall, changed-Python Ruff and bounded independent
reviews passed; the existing TUI inspector now counts Context reference separately
from Product and conversation using the shared request rebuild. No full suite, L2,
build, network or real Provider ran for this stage. F2-F5 and the
three release stops remain in order. Daily owner checks do not automatically run full/L2 gates;
an unfiltered final suite already includes real L2. See the
[frozen v0.9 stage plan](docs/plan/TRACEHARNESS_V0.9_STAGE_PLAN.md).

F1's final explicit 31-file related gate collected 767 cases and finished with
`764 passed, 3 skipped in 32.06s`; the skips were Windows symlink privilege limits.
Discovery/list/inspect now explicitly reports Skill visibility after successful activation,
without importing disabled plugins. Five inverse checks verified prompt isolation, catalog
receipts, resource digests, Lease validity and refusal of Tool grant fields. Compileall and
changed-file Ruff passed; no full/L2 gate, wheel build, network or real Provider ran.
See the [F1 contract and evidence](docs/plan/TRACEHARNESS_V0.9_F0_DESIGN_CONTRACT.md#13-f1-实现与限定验证证据).

## v0.10: Host-owned Sandbox

- Add a real, fail-closed execution boundary for filesystem, network, secrets,
  resources, output and process-tree convergence.
- Route effectful Tools and isolated local processes through one host-owned
  Sandbox contract and durable receipt; a missing backend must never silently
  degrade to an ordinary subprocess.
- Keep `READ_ONLY`, virtual environments, temporary directories and Git
  worktrees honest: they remain useful policies or isolation units, but are not
  advertised as an OS Sandbox.
- Separate trusted adapters managing sandboxed external processes (S3-A) from
  the isolated Plugin protocol (S3-B). Freeze pre-import isolation, contribution
  subsets and cross-process lifecycle semantics before claiming isolated Plugin
  support; otherwise keep that mode explicitly rejected.

## v0.11: Official MCP Client plugin

- Implement TraceHarness as an MCP client through an independent official
  plugin owned by the existing Activation/Generation/Lease/Drain lifecycle.
- Map MCP Tools, Resources and Prompts into the existing Tool/resource/typed
  Skill boundaries; server annotations never grant effects or authority.
- Apply host Policy, Budget, Sandbox/network controls, redaction and EventStore
  audit to every external call. MCP server mode is outside the v1.0 scope.

## v0.12: Controlled dynamic concurrent Workflow

- Let models select host-approved templates first, then propose a restricted DAG
  inside a frozen capability envelope.
- Reuse the existing DAG/Map/Join scheduler and ready-node concurrency while
  giving each child Agent an independent identity, Session, Context and worktree.
- Add immutable child artifacts, deterministic Join/Merge and integrated
  Verification plus post-code Review before human Approval and Git CAS Promotion.
- W1 starts with parallel read-only analysis and one coder. Multiple-coder
  templates open only after W3 supplies integrated artifacts and a review result
  consumed by the safety tail; the current Join barrier alone does not merge code.
- Do not accept arbitrary executable workflow code or allow models to bypass the
  mandatory safety tail.

## v1.0: Stable local coding-agent host

- Freeze a deliberately small compatibility policy for public API/SDK, plugins,
  persistent protocols and CLI; decide migration versus explicit pre-1.0 data
  rejection per protocol instead of promising a universal upcaster.
- Run the combined context/memory, Sandbox, MCP, dynamic Workflow, replay,
  packaging and supported-platform release gates without adding new features.
- Publish the migration guide and third-party Plugin/MCP contract test kit.
- Keep OpenTelemetry productization, real-time token streaming, silent
  Provider/model fallback, multi-host runtime and cloud/multi-user control planes
  outside the v1.0 completion boundary.

The authoritative sequencing, ownership boundaries, stage stops and acceptance
journeys are defined in the
[v1.0 master plan](docs/plan/TRACEHARNESS_V1.0_MASTER_PLAN.md).

### v0.9 F5 governance and frozen evaluation

Both chat adapters share fresh host governance and explicit Context configuration. The sole Product
benchmark runner now owns real requester scope and production Skill/Memory seeding before host assembly.
Root manifest schema 2 rejects schema 1; no second reader, registry, Store or runner was introduced.
The first 11-query frozen baseline completed every Product attempt with zero scope violations, while
five queries failed precision/zero-hit thresholds. The correction and unchanged-input verification are
complete: Product 11/11, quality_passed 11/11 under the original thresholds, scope violations 0.
The four precision failures improve from 0.5 to 1 for precision@K and full Context precision while
Recall/MRR stay at 1; the retired-generation query improves from zero-hit 0 to 1. The semantic query
retains Recall/MRR/precision 0 and only meets its predeclared lexical floor; this is no generalization
or semantic-performance claim. Sixteen named test files pass 303 cases, including the frozen grid,
19 non-corpus Runtime regressions and seed/Product index failure/cancellation paths. Three root-cause
reverse checks fail as intended before exact restoration. Stop C is now passed within the focused authorized scope;
no full suite, L2-L4, packaging or real Provider ran in this stage.
See [F5 validation](docs/validation-v0.9-f5.md) and [ADR-0046](docs/adr/0046-shared-context-governance-and-frozen-retrieval-evaluation.md).

The shared ranker now preserves complete punctuated identifiers/paths as query terms; queries containing
these literals require at least one complete literal match. Ordinary prose retains partial OR retrieval.
Per-source receipts freeze matching query coverage; global fusion orders coverage count before the
original rational RRF score and content identity. Only automatically selected blocks admitted by the
final Context budget can exclude later strict-subset coverage as `query-dominated`. Equal/complementary
coverage and explicit disclosure remain eligible. This is a lexical rule, not a semantic guarantee.
Current C2 uses Session 9 / Context 8 / source receipt 2, tokenizer v3 and ranker v2;
old Sessions 1–8 are rejected without migration. No query-id exceptions, stopword list, new threshold or
configuration field was added. See [ADR-0047](docs/adr/0047-literal-query-coverage-admission.md).


F5 Skill navigation now supplies author-owned section/resource/chunk titles and summaries through the existing catalog and disclosure path. Real Provider tests cover autonomous chapter selection and next-Step reads, with model-specific failures retained; this is not a universal reliability claim. The retrieval corpus changes only its catalog binding and dependent hash for the new descriptor protocol, preserving all quality judgments and thresholds. See [ADR-0048](docs/adr/0048-skill-navigation-and-real-provider-disclosure.md) and [validation](docs/validation-v0.9-skill-navigation.md). Release Stop C is now passed within the authorized focused scope; release gates remain outstanding. No full suite, L2 or Wheel was run.


C1 implementation now places the current reference after complete Tool groups and derives bounded same-Turn
retention for admitted Skill, Memory and History bodies. Source authority and shared budgets are rechecked each
Step; eviction cannot revive an old grant. C1 third-round frozen Skill acceptance passed 24/24 for each of three models; earlier failed grids remain recorded. The ordered
[Stop C repair plan](docs/plan/TRACEHARNESS_V0.9_RELEASE_STOP_C_EXECUTION.md) keeps full-suite, L2–L4,
Wheel/install and Git publication gates prohibited for this task; the limited Stop C acceptance is complete.

C2 now derives a compact model view and explicit Memory/History read actions from the same complete
Context proofs. Fresh format-2 core grids pass 27/28, 28/28 and 28/28; original Skill regression passes
24/24, 23/24 and 24/24 against the predeclared thresholds. SQLite recalculation retains the remaining
model-behavior failures. Adoption checks are complete. C3's four frozen local retrieval candidates
failed the declared multilingual quality and gain thresholds; the original 11-query Runtime baseline
still passes. Semantic/reranker lanes remain disabled, with no production protocol or dependency
change. See [C3 evidence](docs/validation-v0.9-stop-c-c3.md). C4 preserves 24 controlled attempts:
21 parsed and three transport failures, with no confirmed Adapter defect. All 22 received bodies
including a separate control pass offline byte replay; the historical malformed bytes remain unknown.
See [C4 evidence](docs/validation-v0.9-stop-c-c4.md). C5 independent review and focused fixes are complete with zero production P0/P1/P2 findings. Product fixture and UI synchronization regressions pass; see [Stop C acceptance](docs/validation-v0.9-stop-c.md).
