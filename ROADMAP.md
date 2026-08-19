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

## v0.5: Composition generations and scoped overlays

- **Stage A — completed:** Generation-backed Composition Runtime is used by both default
  factories and the no-plugin path. Each Step acquires a generation-bound Lease;
  Publish/Retire, last-Lease cleanup, structured Drain failure reporting and repeated
  cancellation convergence are implemented. Model-visible Tool, Prompt, Provider,
  Policy/Middleware names and Plugin Identity inputs are frozen per Generation. Generation
  identity remains internal and is not part of Snapshot revision or Request Fingerprint.
  Stage A remains lifecycle infrastructure, not a v0.5 release.
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
  failure is fail-closed. The version remains `0.4.0` and Stage C is not a v0.5 release.
- **Stage D0 — completed as a structural checkpoint:** plugin candidate replacement,
  Session identity verification/migration, the shared admission Gate and in-flight
  replacement/admission convergence live in `PluginCompositionCoordinator`.
  `AgentRuntime` remains the public facade, active-Turn owner and overall shutdown owner;
  `AgentLoop` is unchanged. D0 adds no Scope Overlay and no new user command.
- Add Application, Workspace, Preset and Agent Scope layers (v0.4 activates application
  scope only).
- Add explicit override conflict diagnostics.
- Widen `PluginContext` to providers, policies, middleware, event stores and verifiers.

Still deferred: running `pip install`/`uninstall` for Wheels, forced Python module reload,
file watching, Workspace/Preset/Agent Scope Overlay, Provider/Policy/Middleware/EventStore/
Verifier plugin contributions, isolated plugins, multi-agent, Workflow, MCP, TUI and model
streaming. The version remains `0.4.0`; completing Stage A, Stage B, Stage C or the D0
structural checkpoint does not mean v0.5 is released.

Definition of done for the later v0.5 product remains: two Agents can see different
tool/policy compositions, and updating a plugin cannot change a Step already in progress.

## v0.6: AgentSupervisor and subagents

- Separate durable Agent identity from live Activation.
- Add one FIFO Inbox per Agent and single-activation enforcement.
- Add lifecycle ownership graph and child-first quiescent disposal.
- Implement `spawn_agent`, `send_agent_message`, `wait_agent`, `stop_agent` and
  `collect_agent_artifact` as tools backed by the Supervisor.
- Keep history lineage, communication and ownership as separate relations.

Definition of done: a parent can create a reviewer child with its own Session and Scope;
parent cancellation leaves no orphan tasks.

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
