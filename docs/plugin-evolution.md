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
and reversible, and `Runtime.dispose()` converges active turns, drains retired Composition
Generations, and only then unloads plugins in reverse activation order.

## Stage A: Generation-backed Composition Runtime — shipped as infrastructure

Both default runtime factories now create the same Generation-backed Composition Runtime;
the no-plugin path also uses it. A Step acquires one Lease, and that Lease binds the
Provider, Prompt, ToolRuntime, plugin identities, policies/middleware and Composition
Snapshot to one immutable Generation. `publish()` has a lock-protected linearization
point; the old Generation is retired and new Leases see only the new one. Cleanup starts
only after the last old Lease releases, runs at most once, and `drain()` waits for both
Lease count zero and cleanup completion. Repeated cancellation is absorbed until the
shared drain task converges. Cleanup errors are returned as a bounded deterministic
structured failure after other generations have also been attempted; a failed cleanup
poisons the runtime and future publication is rejected.

Generation identity is an internal lifecycle number. It is not put into the persisted
Composition Snapshot or Request Fingerprint. `CompositionSnapshot.revision` remains a
fingerprint of model-visible content, so distinct Generations with identical content may
share a revision.

The initial Generation freezes Tool metadata and schemas together with flat, idempotent
execution adapters, Prompt and Plugin Identity inputs. The Tool adapter exposes read-only
metadata properties and recursively frozen JSON schema data; it never offers a mutation
method. Policy and middleware names used by the Snapshot are captured at construction.
Runtime initialization fixes the identity of the main `ToolRuntime.sessions` object; a
candidate using another `SessionService` is rejected before publication, preserving the
Session Event Log as the only fact source. Generation-object publication ownership and
resource cleanup ownership are separate states. Cleanup is carried by one explicit,
one-shot `CompositionResourceOwner` handle created by the assembly layer. Raw
`LlmRegistry`, `ToolRuntime`, `PromptAssembler`, Provider/Tool/Policy/Middleware components
and frozen/compatibility wrappers propagate its binding directly; there is no global `id()`
catalog and no object-graph scan to infer ownership. A public cleanup-bearing Generation
without an explicit owner is rejected, and a raw slotted capability that cannot retain the
binding marker is also rejected for cleanup ownership. Such a capability must first pass
through a binding-capable controlled assembly; otherwise the same raw object could be put
in two fresh containers with no way to prove that they share ownership. Generation
construction performs all provider lookup and freezing before it commits the owner/binding.
The binding is stored directly in the actual instance dictionary or declared slot and is
read back for verification, so a custom setter cannot silently discard it. A partial commit
restores the exact prior attribute state, including the difference between an absent field
and a present `None`, so a failed candidate does not poison a retry. Runtime compatibility
views are built from the frozen initial Generation before the owner claim; there is no
second caller-controlled Prompt/Registry read after ownership becomes one-shot. A used binding cannot receive a new cleanup
owner, and the same owner handle cannot be claimed by two Runtimes. Runtime initialization
and `publish()` use the same validation/claim entry: frozen cleanup inputs, multi-hop
derivations, wrapper aliases and cleanup added to an already-used binding are rejected
before current changes. Stage A has no resource-level refcount, so a cleanup-bearing
Generation must use an unclaimed exclusive capability assembly. Service values remain application-level registrations owned by
PluginManager/AgentRuntime and are not yet bound to Generation lifecycle. Stage A also
rejects a publication whose plugin identity tuple differs from the startup composition;
Session migration is intentionally deferred.

The two product capabilities the earlier draft listed as part of the loader remain future
work, because they change the meaning of a Step rather than the loader:

- a user-facing **hot-reload command** that builds, validates and publishes a candidate
  Generation;
- dynamically installing or uninstalling a Wheel while the process is running.

The plugin set is still fixed between startup and `dispose()`, and PluginManager remains a
startup activation owner. Also absent: Workspace/Preset/Agent Scope Overlay, isolated
(out-of-process) plugins, and any new Provider, Policy, Middleware, EventStore or Verifier
plugin contribution. The version remains `0.4.0`; Stage A is not a v0.5 release.

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
