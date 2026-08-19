# Plugin and multi-agent evolution

This page tracks what is **shipped** versus what remains planned. For the author-facing
contract see [`plugins.md`](plugins.md); for the v0.4 transaction reasoning see
[ADR-0007](adr/0007-transactional-plugin-activation.md), for the Generation-owned
ActivationSet decision see [ADR-0009](adr/0009-generation-owned-plugin-activation-set.md),
and for the Session migration protocol see
[ADR-0010](adr/0010-session-plugin-composition-migration.md). The control-plane ownership
split is recorded in
[ADR-0011](adr/0011-plugin-composition-control-plane-coordinator.md).

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

Any failure unwinds every activation in reverse order. Cancellation unwinds identically;
when rollback succeeds it re-raises the original `CancelledError`. A real rollback cleanup
failure instead becomes a bounded `PluginDisposeError`, so cancellation cannot make a
partially cleaned candidate look successful.

No plugin mutates a live registry directly. Every registration is owned by an `Activation`
and reversible. In the Stage B default path, the manager runs against private registry
forks and transfers successful Activation ownership to a `PluginActivationSet`; it does
not retain a second cleanup owner.

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
Generation must use an unclaimed exclusive capability assembly. Stage B makes the
plugin-owned boundary explicit: SessionService, EventStore, core Provider and built-in
Tools are borrowed core resources, while each `PluginActivationSet` owns its plugin
Activations, plugin Tools, Prompt sections, Services, Owned Tasks and cleanup callbacks.
The same ActivationSet cannot be accepted by two Generations or Runtimes, and the
temporary PluginManager cannot retain a second cleanup owner. Plugin identity may change
between generations only through this ActivationSet path; an internal replacement does
not authorize an existing Session to cross that composition boundary. Stage C adds an
explicit per-Session append-only authorization for that boundary; it does not make the
internal replacement API an automatic migration.

## Stage B: Generation-owned Plugin ActivationSet — shipped as internal infrastructure

`PluginGenerationBuilder` accepts an explicit enabled-plugin id tuple and creates a fresh
candidate ToolRegistry, PromptAssembler and ServiceRegistry view. Discovery, dependency
ordering, manifest validation, setup, conflict checking and health checks all happen
before publication. A failed setup, health check, Generation construction or publish
rolls the candidate back in reverse order immediately; current Generation and its
registries do not change. Repeated cancellation is converged before the original
`CancelledError` is re-raised when rollback succeeds. If rollback cleanup fails, the
remaining activations are still attempted and the bounded structured failure wins over
cancellation.

After a successful publish, the ActivationSet is owned by the new Composition Generation.
An old Lease keeps the old plugin Tool, Prompt and Service alive. When its last Lease
exits, cleanup first cancels and waits for Owned Tasks, then reverses plugin registration
and dependency order. Cleanup failures are bounded, structured and terminal-safe; one
plugin failure does not skip other plugins, and the Composition Runtime poisons itself
after a failed Generation cleanup. Runtime disposal converges active Turns, cancels and
waits for in-flight candidate replacement Tasks, drains all Generations and ActivationSets,
and only then releases optional legacy application-level builders or discovery resources.
The default path has no second PluginManager cleanup owner. Runtime shutdown retrieves each
in-flight replacement Task's outcome: ordinary cancellation is expected, while a rollback
`PluginDisposeError` is included in the stable shutdown result and repeated `dispose()` calls
report it again.

The internal replacement API changes only the current Generation. It does not set a
process-wide Session migration bypass: an existing Session must still match its latest
durable identity. Stage C exposes the same assembly path through idle `traceh chat`
commands:

```text
/plugins                 show current external plugin ids and versions
/plugins reload          rebuild the current enabled set
/plugins use ID [ID ...] select an explicit enabled set
/plugins use --none      select no external plugins
```

`/plugins reload` is a same-identity rebuild and does not append an authorization event.
`/plugins use` with a changed identity prepares the candidate first, then appends
`composition/migration-authorized` using a Session-head CAS, and publishes only after the
authorization is durable. A shared Runtime Gate requires no active Turn and prevents a
new Turn from passing identity verification during the migration. The identity helper
rebuilds state from `session/created`, valid `composition/snapshot` and migration events;
`migration_id`, `source_seq`, `from_plugins` and `to_plugins` are checked strictly. If an
append may have committed, the Runtime rereads by `migration_id`; if authorization is
durable but publish fails, the Session is fail-closed.

This is a user-operable composition switch, not source-code hot reload. The process still
does not install or uninstall Wheels, call `importlib.reload()`, watch files, or migrate
other Sessions automatically. At the Stage C checkpoint, Workspace/Preset/Agent Scope
Overlay was still absent; D1 below adds only the Service foundation. Isolated
(out-of-process) plugins and new Provider, Policy, Middleware, EventStore or Verifier plugin
contributions remain absent. The version remains `0.4.0`; Stage C is not a v0.5 release.

## Stage D0: plugin composition control-plane extraction — shipped as structure

Stage A through C introduced three independent concerns that previously accumulated in
`AgentRuntime`: active Turn admission, plugin candidate replacement, and durable Session
composition migration. D0 moves the latter two concerns, their shared Gate and their
in-flight task convergence into `PluginCompositionCoordinator`.

The split is by ownership rather than by line count. `AgentRuntime` remains the public
facade, owns the active-Turn table, performs the final disposed/duplicate-Turn check under
its own lock and owns the overall shutdown task. The coordinator owns candidate
setup/publish/rollback, migration CAS and may-have-committed reconciliation, durable plugin
identity verification, and convergence of replacement/admission tasks before Composition
Drain. `AgentLoop` is unchanged and continues to depend only on
`CompositionRuntime.lease()`.

Public facade dispatch remains part of the behavior-preserving boundary:
`reload_plugin_composition()` reads the facade's current `enabled_plugin_ids` and calls the
facade's public `migrate_session_plugin_composition()`. The coordinator has no parallel
reload shortcut that could bypass a subclass, instrumentation or audit seam.

No Event Log fact, Session protocol, public Chat command or plugin capability was added.
D0 is the boundary needed before Scope Overlay work; it is not Scope Overlay itself and is
not a v0.5 release.

## Stage D1: four-layer Service Scope — shipped as foundation

D1 keeps `ServiceRegistry` as the only Service registration mainline and gives each layer a
read-through parent. `ScopeChain` assembles Application, Workspace, Preset and Agent in a
fixed order. A nearer binding must explicitly declare `replace=True`; same-layer duplicate,
missing replacement and API-major mismatch outcomes have stable structured codes and source
scope metadata. `replace` is a strict boolean, and complete assembly is preflighted on an
isolated Registry fork so failure does not partially mutate the caller's Application layer.

Both default factories accept explicit `ScopedServiceBinding` values. Every
`PluginGenerationBuilder` candidate forks the application registry and reconstructs the
remaining layers; `PluginActivationSet` owns that chain, and `CompositionGeneration` captures
its effective Agent Scope and read-only `ServiceView`. A Step Lease therefore observes one
generation's Service composition, while a later replacement receives a separate chain.
Application plugin setup cannot read a nearer workspace/preset/agent override.
The public `PluginManager.prepare_activation_set()` preserves an existing chain's child-layer
binding blueprint. Custom D0 ActivationSets may omit Scope entirely; implementations that opt
in must provide a matching Scope/ServiceView pair.
Because plugin Services publish after initial child-layer assembly, the manager revalidates
child override intent before Activations publish. A late ancestor cannot turn an implicit
shadow into a successful candidate; the conflict keeps its stable code and responsible plugin
id, and the candidate rolls back transactionally.

This is deliberately the Service foundation, not complete scoped plugin activation.
`PluginManifest.allowed_scopes` still requires application. No new persistent event or
model-visible fingerprint field is added because a Service binding is not itself
model-visible; the following D2 layer continues through the existing Generation and
Composition Snapshot path.

## Stage D2: Tool, Prompt and Policy overlays — shipped as host assembly

`CompositionOverlayPlan` applies explicit `ScopedToolBinding`, `ScopedPromptBinding` and
`ScopedPolicyBinding` values in the same fixed Application → Workspace → Preset → Agent
order. Resolution occurs on private Tool/Prompt forks and produces one effective
`ToolRegistry`, `PromptAssembler` and Policy tuple. Those ordinary core objects enter the
existing `PluginActivationSet` → `CompositionGeneration` → Step Lease path; Tool schemas,
Prompt content and Policy names therefore remain represented by the existing Composition
Snapshot and request fingerprint. There is no scoped ToolRuntime, parallel fact source or
`AgentLoop` branch.

Capability identity is the Tool name, Prompt section id or Policy name. Same-scope duplicates
and cross-scope overrides have stable `*-already-bound` and
`*-override-requires-replace` codes, and `replace` accepts only a real boolean. Resolution is
transactional: a late Policy conflict cannot leave earlier Tool/Prompt replacements in the
caller-owned inputs. Prompt replacement now has the same reversible-registration behavior as
Tool replacement and restores the previous section during reverse cleanup.

Application plugins publish Tool and Prompt contributions after the initial child overlay is
known. The manager therefore projects staged contributions into a private candidate and
revalidates child overlays before health checks. A missing explicit override fails with the
stable overlay code and responsible plugin id before third-party health code runs. The final
resolved Tool/Prompt/Policy composition transfers together to the ActivationSet; subsequent
plugin replacements preserve the same child binding blueprint and construct their
ToolRuntime from that ActivationSet's Policy tuple. Generation validation compares the
ordered Policy objects by identity, not `__eq__`: Policy is executable admission behavior,
so two behaviorally different objects cannot claim to be the same candidate capability by
overloading value equality.

D2 does not widen plugin authority. Plugin setup remains application-only, `PluginContext`
still cannot provide Policy, and host-provided child bindings are borrowed rather than owned
by plugin cleanup. Two independently assembled Runtimes may now have different Agent-level
Tool/Prompt/Policy compositions, but creating and supervising two Agents remains v0.6 work.

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
