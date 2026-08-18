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

- **Stage A — completed in the current worktree:** Generation-backed Composition Runtime
  is now used by both default factories. Each Step acquires a generation-bound Lease;
  Publish/Retire, last-Lease cleanup, structured Drain failure reporting and repeated
  cancellation convergence are implemented. Tool, Prompt and Plugin Identity inputs are
  frozen per Generation, including truly read-only flat/idempotent Tool execution adapters,
  nested Schema freezing and Snapshot policy/middleware names; each Generation object has
  one-time publication ownership. Generation-object publication ownership is separate from
  resource cleanup ownership: a cleanup-bearing Generation must carry one explicit,
  one-shot CompositionResourceOwner handle created by the assembly layer. Raw
  LlmRegistry/ToolRuntime/PromptAssembler roots, Provider/Tool/Policy/Middleware components
  and frozen/compatibility views propagate that binding directly; there is no global id()
  catalog or object-graph scan. Multi-hop replace(), wrapping frozen capabilities in new
  registries, Runtime construction from used bindings and a second owner claim are rejected;
  Runtime initialization and publish use the same validation/claim entry. Without
  resource-level refcounting, a cleanup-bearing Generation must use an unclaimed exclusive
  capability assembly rather than sharing resources that cleanup may close. Raw slotted
  Provider/Tool/Policy/Middleware objects that cannot retain the binding marker are rejected
  from cleanup-bearing Generations; they require a binding-capable controlled assembly.
  Generation construction commits owner/binding only after Provider lookup and freezing
  succeed. Binding is written to the actual instance dictionary or declared slot and
  verified after the write; a partial commit restores the exact previous attribute state,
  so a failed candidate can be retried with the same Owner and corrected source. Runtime
  compatibility views are built from the frozen initial Generation before owner claim,
  leaving no post-claim second read of caller-controlled Prompt or Registry sources.
  The Runtime fixes the main SessionService identity and rejects
  candidates using another EventStore or re-publishing any already-claimed Generation.
  Service remains application-level and is not Generationized, and publication currently
  rejects plugin identity migration. Cleanup failure is bounded and poisons the runtime for
  future publication. This is lifecycle infrastructure, not a v0.5 release.
- Add Application, Workspace, Preset and Agent Scope layers (v0.4 activates application
  scope only).
- Add a user-facing hot-reload command that builds and validates a candidate Generation;
  Stage A deliberately does not provide this command.
- Support draining old plugin generations as part of an explicit hot-reload workflow;
  the runtime primitive now exists, while PluginManager remains startup-only.
- ✅ Plugin versions already persist in the Composition Snapshot as of v0.4; provider
  identities still to follow.
- Add explicit override conflict diagnostics.
- Widen `PluginContext` to providers, policies, middleware, event stores and verifiers.

Still deferred beyond Stage A: running `pip install`/`uninstall` for Wheels, Workspace/
Preset/Agent Scope Overlay, isolated plugins, multi-agent, Workflow, MCP, TUI and model
streaming. The version remains `0.4.0`; completing Stage A does not mean v0.5 is released.

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
