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

Explicitly deferred out of v0.4: hot reload, Composition generation drain, isolated
(out-of-process) plugins, and plugin-supplied providers, policies, middleware, event stores
or verifiers.

## v0.5: Composition generations and scoped overlays

- Add Application, Workspace, Preset and Agent Scope layers (v0.4 activates application
  scope only).
- Replace direct snapshot creation with a generation lease.
- Drain old plugin generations after active Step leases are released — the prerequisite for
  hot reload, which v0.4 deliberately does not attempt.
- ✅ Plugin versions already persist in the Composition Snapshot as of v0.4; provider
  identities still to follow.
- Add explicit override conflict diagnostics.
- Widen `PluginContext` to providers, policies, middleware, event stores and verifiers.

Definition of done: two Agents can see different tool/policy compositions, and updating a
plugin cannot change a Step already in progress.

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
