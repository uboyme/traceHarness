# Roadmap

The order below preserves the current protocol and keeps `AgentLoop` stable.

## v0.4: Plugin SDK and discovery

- Add `traceh.plugins.PluginContext` exposing only public registries and owned resources.
- Discover packages through `importlib.metadata.entry_points(group="traceh.plugins")`.
- Validate Plugin Manifest, TraceHarness API major and plugin dependencies before import.
- Run setup inside a private `Activation`; publish only after health checks pass.
- Add `traceh plugins list/inspect/doctor`.
- Add Plugin Lifecycle contract tests.

Definition of done: installing a separate wheel can add a tool and prompt section without
editing this repository or `AgentLoop`.

## v0.5: Composition generations and scoped overlays

- Add Application, Workspace, Preset and Agent Scope layers.
- Replace direct snapshot creation with a generation lease.
- Drain old plugin generations after active Step leases are released.
- Persist plugin versions and provider identities in Composition Snapshot.
- Add explicit override conflict diagnostics.

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
