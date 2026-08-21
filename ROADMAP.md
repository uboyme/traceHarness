# Roadmap

The order below preserves the current protocol and keeps `AgentLoop` stable.

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

## Controlled capability evolution — L1-L4 implemented, L5 planned

- **L1 — implemented in Unreleased:** `traceh-plugin-creator-skill-plugin` is a real external
  Wheel that supplies a short Prompt and packaged workflow/contract/template/checklist through
  one `PURE_READ` Tool. The Agent writes source only in a dedicated Candidate Workspace using
  existing coding Tools. It does not build, test, install, enable or approve its own output,
  and the core Runtime/AgentLoop/PluginManager are unchanged. Wheel acceptance builds every
  project from an isolated declared-source copy and audits archive members before installation;
  the packaged contract also states that Verifier uses Generation/Step Lease but not
  `CompositionSnapshot`, with results recorded as `verification/result`.
- **L2 — implemented in Unreleased:** `traceh plugins validate` takes an explicit source-only
  candidate, trusted core Git repository, new evidence directory and dependency source. It
  clones the trusted `HEAD`, reads that clone's compatibility version, rejects source reparse
  points and host-control namespaces, builds and anchors exact audited Wheel bytes, uses separate
  candidate/regression venvs, runs host-owned metadata/doctor/test/core gates, then rechecks the
  Wheel and atomically commits the complete evidence directory after all 13 gates pass. This is
  filesystem/Python environment isolation, not an OS sandbox; see ADR-0016.
- **L3 — implemented in Unreleased:** `traceh plugins compare` consumes the exact successful L2
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
- **L4 — implemented in Unreleased:** `traceh plugins promote` revalidates the exact L2/L3
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

## v0.6: AgentSupervisor and subagents

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
  still have no implementation behind them.
- Add one FIFO Inbox per Agent and single-activation enforcement.
- Add lifecycle ownership graph and child-first quiescent disposal.
- Implement `spawn_agent`, `send_agent_message`, `wait_agent`, `stop_agent` and
  `collect_agent_artifact` as tools backed by the Supervisor.
- Keep history lineage, communication and ownership as separate relations.

Definition of done: a parent can create a reviewer child with its own Session and Scope;
parent cancellation leaves no orphan tasks. **Not met by Stage A**, which supplies the
identity those need without implying any of them exists.

## v0.7: Budgets, workspaces and workflows

- Add hierarchical token/Step/tool/process/child budgets with reservation events.
- Add `WorkspaceProvider`, snapshots, Git worktrees or overlay branches and Patch
  Artifacts.
- Add resource claims for cross-Agent read/write coordination.
- Add Workflow Engine nodes for AgentTask, Map, Join, Approval and Verification.
- Ship a Reviewer-Coder-Parent demonstration.

Definition of done: parallel coding children do not share one mutable directory and the
parent merges evidence-backed patches.

## v1.0: Stable plugin platform

- Freeze `traceh.api` and `traceh.sdk` compatibility policy.
- Add event upcasters and opaque plugin-event handling.
- Add SQLite EventStore and projection checkpoints.
- Add isolated process plugin host and capability grants.
- Add OpenTelemetry plugin, streaming provider protocol and model retry/fallback.
- Publish migration guide and third-party contract test kit.
