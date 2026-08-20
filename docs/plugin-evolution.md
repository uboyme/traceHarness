# Plugin and multi-agent evolution

This page tracks what is **shipped** versus what remains planned. For the author-facing
contract see [`plugins.md`](plugins.md); for the v0.4 transaction reasoning see
[ADR-0007](adr/0007-transactional-plugin-activation.md), for the Generation-owned
ActivationSet decision see [ADR-0009](adr/0009-generation-owned-plugin-activation-set.md),
and for the Session migration protocol see
[ADR-0010](adr/0010-session-plugin-composition-migration.md). The control-plane ownership
split is recorded in
[ADR-0011](adr/0011-plugin-composition-control-plane-coordinator.md).

TraceHarness `0.5.0` ships the Stage A-D3 path described below. Its release gate includes the
independently packaged
[`traceh-python-quality-plugin`](../examples/plugins/traceh-python-quality-plugin/), built and
installed as a real Wheel through the public SDK rather than compiled into the core Runtime.

## v0.4 plugin manager — shipped

`PluginManager` discovers Python entry points in the `traceh.plugins` group, imports only
the plugins the operator explicitly enabled, and activates them as one transaction:

1. validate the explicit selection, before discovery;
2. discover from distribution metadata **without importing anything**;
3. import the enabled plugins and validate every manifest field;
4. resolve dependencies into a deterministic topological order;
5. run each `setup()` against **private staged registries**;
6. close every Composition contribution method;
7. check every conflict against the core registries;
8. run `health_check()`;
9. atomically publish the staged capabilities into the existing mainlines.

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
ordering, manifest validation and setup all happen privately; setup then freezes the
contribution surface before conflict checking and health. A failed setup, health check,
Generation construction or publish
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
(out-of-process) plugins remain absent; D3 below adds Provider, Policy, Middleware and
Verifier without adding EventStore replacement.

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
D0 is the structural boundary needed before Scope Overlay work; it is not Scope Overlay
itself.

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

D2 by itself did not widen plugin authority. Plugin setup remained application-only and
host-provided child bindings remained borrowed rather than owned
by plugin cleanup. Two independently assembled Runtimes may now have different Agent-level
Tool/Prompt/Policy compositions, but creating and supervising two Agents remains v0.6 work.

## Stage D3: Provider, Policy, Middleware and Verifier contributions — shipped

`PluginContext` now exposes reversible registrations for `LlmProvider`, `ToolPolicy`,
`ToolMiddleware` and named `CompletionVerifier` values. Setup remains trusted,
application-scope and in-process. The manager stages all four categories privately, checks
Provider/Policy/Middleware conflicts and selected Provider/Verifier existence before health
checks, then publishes them into the same candidate `LlmRegistry`, Policy/Middleware tuple
and `PluginActivationSet`. Failure or cancellation unwinds their registrations with every
other Activation resource.

Setup is the only phase allowed to change the candidate Composition. After every setup has
returned, the manager closes all Tool/Prompt/Service/Provider/Policy/Middleware/Verifier
registration methods before conflict checks and health. Health may inspect configuration and
Services and retain lifecycle cleanup/Owned Tasks, but a late registration fails health and
rolls the candidate back. Tool, Provider, Policy and Middleware names are captured when they
are registered; setup completion, every health return and the final awaited publication
boundary verify that the original object still has that identity. Drift fails with
`plugin-contribution-identity-changed`. Conflict checks and attribution use the captured name,
and Tool/LLM reversal handles use their original registry key. Policy child-overlay failures
retain the responsible plugin id.

`prepare_activation_set()` is also a public asynchronous ownership boundary. The transferred
set keeps an immutable receipt of its Registry containers, member objects, registered names,
Prompt, ordered Policy/Middleware values, Verifier and plugin identities. Generation
construction revalidates that receipt before claim, while frozen schemas and runtime lookup
both use the registered Registry key. A caller may therefore await after preparation without
letting an Owned Task make one Generation advertise a Tool name it cannot execute.
Activation and receipt construction remain one transaction: until `PluginActivationSet`
construction succeeds, the temporary Manager is the only cleanup owner. A receipt or Scope
failure therefore cancels and waits for Owned Tasks, performs reverse cleanup exactly once and
only then returns the original hand-off error. Repeated cancellation cannot cut convergence
short; a concurrent cleanup failure is reported alongside the transfer failure through
`BaseExceptionGroup`. When both members are ordinary `Exception` values Python derives the
existing `ExceptionGroup`; direct `BaseException` interruptions remain representable instead
of being masked by a new grouping `TypeError`.

Provider and Verifier selection is deliberately explicit. A custom Provider name is accepted
only alongside an explicitly enabled plugin and an explicit Model; a named Verifier is
selected by `verifier_name`, `--plugin-verifier` or `TRACEH_PLUGIN_VERIFIER`. Enabling a
plugin never silently takes over either role, and a named plugin Verifier is mutually
exclusive with a direct/command Verifier. Stable pre-health failures include
`provider-not-provided`, `verifier-not-provided`, `provider-publish-conflict`,
`policy-publish-conflict` and `middleware-publish-conflict`.

The selected Provider, ordered Policy and Middleware objects, and Verifier are transferred by
identity with the ActivationSet and captured by one `CompositionGeneration`. AgentLoop reads
the Verifier from the same Step Lease as Provider and ToolRuntime, so a replacement cannot
make one in-progress Step verify with a newer generation. Existing Snapshot fields continue
to record Provider, policies and middleware; Verifier remains execution evidence through the
existing `verification/result` event rather than a second configuration fact.

When an ActivationSet exposes an LLM registry, the selected Provider must both exist there and
be the exact object used by the Generation; a same-named object from another registry is not a
fallback. The D0 replacement contract remains compatible only for custom ActivationSets that
do not expose D3 `llms` (or expose `None`): those borrow the coordinator's existing core
registry rather than being forced to implement a new optional field.

EventStore is intentionally excluded. SessionService, recovery, inspection and every event
append share it as a process-lifetime fact source, while a D3 ActivationSet may retire after a
Step Lease. Making it hot-reloadable would allow an old Session to retain a cleaned-up Store
or split one Runtime across two ledgers. A future EventStore plugin boundary first needs a
separate pinned owner, construction/disposal order, Session compatibility rules and Store
contract tests. See [ADR-0014](adr/0014-generation-scoped-plugin-execution-capabilities.md).

### v0.5.0 release acceptance

The release does not rely only on in-repository fixture objects. The public `traceh.plugins`
surface exports the author contracts needed by a separate distribution, and the Python
Quality plugin consumes only that surface. It contributes:

- `python_project_info`, a bounded read-only Tool over fixed workspace-root evidence;
- deterministic Python quality guidance;
- `python-environment-safety`, a monotonic deny Policy for direct environment removal and
  external pip installation targets (a guardrail, not a sandbox);
- `python-tests`, a named Verifier that runs only a project-declared test command or explicit
  pytest configuration and otherwise fails closed.

The clean-environment acceptance copies only each project's declared build inputs, builds core,
Skill example, Python Quality and Plugin Creator Wheels, audits every archive for bytecode,
caches, old build trees and egg metadata, then installs them offline with `packaging`. It
discovers all three Entry Points, runs plugin doctor, and drives Python Quality's Tool, Policy
and Verifier through one real Session. The resulting Composition Snapshot, event invariants and
request reconstruction must all remain clean.

### Post-v0.5 L1: source-only Plugin Creator Skill

The first controlled capability-evolution step remains outside the runtime control plane.
`traceh-plugin-creator-skill-plugin` is an independent Entry Point distribution that adds one
short Prompt and one `PURE_READ` Tool over packaged workflow, SDK contract, package template
and static checklist resources. It relies on the existing coding Tools to write a candidate
inside a dedicated Workspace, so it adds no writer, installer, loader, registry, event or
Generation path.

L1 stops at source. The candidate includes package metadata, Entry Point, Manifest,
implementation, tests, README and a human-readable card marked
`UNVALIDATED (L1 SOURCE ONLY)`, but is not imported, built, tested, installed, enabled or
promoted. This is a workflow boundary rather than a sandbox. See
[ADR-0015](adr/0015-source-only-plugin-candidate-authoring-skill.md).

The packaged SDK contract distinguishes lifecycle from persistence: a named Verifier is fixed
by the Generation and Step Lease, but is not a `CompositionSnapshot` field. Its observed result
is durable only through `verification/result`.

### Post-v0.5 L2: independent candidate validation

`traceh plugins validate` is a separate development control plane under `traceh.evolution`; it
does not add work to `AgentLoop`, `AgentRuntime`, `PluginManager` or the Event Log. The caller
must provide a source-only candidate, a trusted TraceHarness Git repository, a new output
directory and an explicit dependency source (`--allow-index` or `--wheelhouse`).

The validator clones the trusted repository's `HEAD`, builds core and candidate Wheels, audits
the candidate archive, and creates separate candidate-contract and core-regression virtual
environments. Host-owned metadata checks and pytest configuration prevent the candidate from
redefining the evaluator. The candidate's own tests are one gate; the trusted core suite is a
different gate with the candidate installed but not enabled. Candidate stdout/stderr never
becomes report evidence.

The selected core clone, not the running CLI, supplies the compatibility version. Source reparse
points and host-control import roots are rejected. The host anchors audited Wheel bytes before
candidate execution, rechecks disk identity afterward, and publishes Wheel/reports/diagnostics as
one directory transaction. Ordinary gate failure yields a complete report-only bundle; report or
commit failure leaves no output path. Virtual environments are not an OS sandbox and candidate
code retains current-user authority; downstream consumers must recheck the SHA-256 before use.
L2 still makes no quality or approval claim. See
[ADR-0016](adr/0016-independent-plugin-candidate-validation.md).

### Post-v0.5 L3: host-owned baseline/candidate comparison

`traceh plugins compare` is the next development-control-plane step. It accepts only the exact
successful L2 evidence bundle, checks the canonical 13 gates and artifact digest again, clones the
core commit named by that report, and loads a fixed suite from a relative path inside that trusted
commit. It never rebuilds the candidate.

Dependencies are resolved once into a bounded all-Wheel set whose members are fixed by filename,
size and SHA-256. Baseline and candidate receive separate venvs installed offline from that same
set, and their Distribution receipts must match before execution and remain unchanged afterwards.
Nested Tool and Verifier pip processes receive that set as one canonical percent-encoded local
`file://` URI; raw paths, multiple values, remote hosts, queries and fragments are not propagated.
The baseline keeps the target plugin disabled; the candidate enables the exact L2 plugin identity.
A host-owned probe drives the public Runtime and persisted Session/Verifier paths. It requires the
matching durable Turn/Step lifecycle to be closed, durable reason and Step count to agree with the
method return, and every in-Turn Composition Snapshot to contain the expected arm identity before
recording success, failure codes, Step/model/tool counts, verification, invariants, request
reconstruction and duration. Candidate code cannot replace the tasks or evaluator, and the report,
Wheel, dependency, receipt and suite digests are rechecked after candidate execution.

The output classification is only `improved`, `regressed`, `mixed` or `no-change`. It carries no
approval or promotion bit. The first fixed suite under `benchmarks/evolution/python_quality_v1`
contains one capability-difference case, one ordinary repair that must not regress and one honest
verification-failure case. This is a deterministic capability contract, not a general coding or
real-model benchmark. The venvs are not an OS sandbox. See
[ADR-0017](adr/0017-host-owned-baseline-candidate-comparison.md).

## Extension categories

Shipped in v0.5.0 — a plugin may register:

- **Tool**: register `Tool` objects; they join the normal admission, policy, middleware and
  effect-ledger pipeline.
- **Prompt**: register deterministic `PromptSection` values.
- **Service**: provide typed `ServiceKey` values other plugins can `require()`.
- **LLM provider**: register `LlmProvider`; the host must select its name explicitly.
- **Policy / middleware**: register `ToolPolicy` and `ToolMiddleware`; they join the existing
  ToolRuntime admission and execution chain.
- **Verification**: register a named `CompletionVerifier`; the host must select it explicitly.

Still planned, not reachable from `PluginContext` today:

- Persistence: implement `EventStore` and pass the contract tests.
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
