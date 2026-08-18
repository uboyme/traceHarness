# Plugin and multi-agent evolution

This page tracks what is **shipped** versus what remains planned. For the author-facing
contract see [`plugins.md`](plugins.md); for the reasoning see
[ADR-0007](adr/0007-transactional-plugin-activation.md).

## v0.4 plugin manager — shipped

`PluginManager` discovers Python entry points in the `traceh.plugins` group, imports only
the plugins the operator explicitly enabled, and activates them as one transaction:

1. validate the explicit selection, before discovery;
2. discover from distribution metadata **without importing anything**;
3. import the enabled plugins and validate every manifest field;
4. resolve dependencies into a deterministic topological order;
5. run each `setup()` against **private staged registries**;
6. check every conflict against the core registries;
7. run `health_check()`;
8. atomically publish tools, prompts and services into the existing mainlines.

Any failure unwinds every activation in reverse order. Cancellation unwinds identically and
then re-raises the original `CancelledError` - it is never reported as a plugin failure.

No plugin mutates a live registry directly. Every registration is owned by an `Activation`
and reversible, and `Runtime.dispose()` converges active turns before unloading plugins in
reverse activation order.

### Still not implemented in v0.4

The two items the earlier draft of this page listed as part of the loader remain future
work, because both change the meaning of a Step rather than the loader:

- creating a **new Composition generation** per activation;
- **draining** old generations after existing Step leases finish, i.e. hot reload.

The plugin set is fixed between startup and `dispose()`. Also absent: isolated
(out-of-process) plugins, which a manifest may declare and activation explicitly rejects.

## Extension categories

Shipped in v0.4 — a plugin may register:

- **Tool**: register `Tool` objects; they join the normal admission, policy, middleware and
  effect-ledger pipeline.
- **Prompt**: register deterministic `PromptSection` values.
- **Service**: provide typed `ServiceKey` values other plugins can `require()`.

Still planned, not reachable from `PluginContext` today:

- LLM provider: register `LlmProvider`.
- Policy: register `ToolPolicy`; middleware: register `ToolMiddleware`.
- Persistence: implement `EventStore` and pass the contract tests.
- Verification: implement `CompletionVerifier`.
- Observability: subscribe to typed NOTIFY hooks.

Behavior that changes a model request must be represented in the Composition Snapshot,
otherwise request reconstruction will correctly report a mismatch. This is why activated
plugin identities are persisted into every snapshot, and why replay rebuilds them.

## v0.6 AgentSupervisor

Multi-agent support should be built above `AgentLoop` with:

- durable Agent identity and Session;
- one FIFO Inbox per Agent;
- at most one live Activation per Session;
- explicit lifecycle ownership;
- separate history lineage, communication and workspace relationships;
- child-first cancellation and quiescent disposal;
- budget allocation and depth limits.

Subagent operations become normal tools (`spawn_agent`, `send_agent_message`,
`wait_agent`, `collect_artifact`) backed by `AgentSupervisor`. The loop remains unaware
that a tool creates another Agent.

## v0.7 workspaces and workflows

Writable coding children should receive isolated worktrees or overlay workspaces and
return Patch Artifacts plus test evidence. A workflow layer can compose Agent tasks,
parallel maps, joins, approvals and verification by calling public Supervisor and Tool
APIs.
