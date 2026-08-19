# ADR-0012: Bind four-layer Service Scope to Composition Generations

## Status

Accepted.

## Context

The repository already had a small `Scope` prototype, but the default Runtime and plugin
candidate path used one flat `ServiceRegistry`. The prototype also allowed an ancestor to be
shadowed without an explicit override. It therefore did not satisfy the planned Application
→ Workspace → Preset → Agent contract and was not connected to a Step Lease.

Adding scoped Tool, Prompt, Policy and Provider contributions in one change would combine
several independent trust, persistence and ownership decisions. The first mainline boundary
needed is deterministic Service resolution: it exercises hierarchy, explicit override,
candidate isolation and Generation freezing without changing the model-visible request.

## Decision

Keep `ServiceRegistry` as the only Service registration mechanism. A Registry may reference
one parent and resolves locally before reading the parent. Build exactly four layers with
`ScopeChain`:

1. Application;
2. Workspace;
3. Preset;
4. Agent.

Assembly accepts immutable `ScopedServiceBinding` values. Bindings are processed in hierarchy
order regardless of caller order. The rules are:

- a second binding in the same layer fails with `service-already-bound` unless it explicitly
  replaces that layer's value;
- shadowing an ancestor requires `replace=True`, otherwise it fails with
  `service-override-requires-replace`;
- an explicit override must target the same `ServiceKey`, including API major; a same-name,
  different-major target fails with `service-override-api-major-mismatch`;
- different API majors may coexist when the new binding is not represented as an override.

`replace` must be an actual boolean; truthy strings or integers are not override authority.
`ScopeChain.build()` preflights the complete four-layer assembly against an isolated Registry
fork before committing Application bindings to the caller-owned Registry, so a later-layer
conflict cannot leave partial state behind.

Each conflict exposes a stable code, key, destination scope and existing source scope.

`PluginGenerationBuilder` forks the application Registry for every candidate and constructs a
new ScopeChain. `PluginActivationSet` owns that chain. `CompositionGeneration` captures the
effective Agent Scope and read-only `ServiceView`, and `ActiveComposition` returns the same
captured values to a Step. Published Scope objects reject further assembly-time `provide()`
calls; the public Service view has no registration method. Plugin registrations still mutate
only the candidate's application Registry through reversible Registrations, and cleanup waits
for the last Generation Lease.

The public `PluginManager.prepare_activation_set()` carries the existing chain's
Workspace/Preset/Agent binding blueprint into its Builder. D1 Scope is optional for custom
ActivationSet implementations that satisfy the earlier ownership/cleanup protocol; an
implementation that opts in must provide a matching Scope and ServiceView pair.

Application plugin Services publish after the child layers are assembled. The manager therefore
revalidates child override intent before Activations publish. This closes the timing gap without
creating a second Service registration path, and any conflict keeps its structured code and
responsible plugin identity.

Application-scoped plugin setup resolves only from the application Registry. It cannot depend
on Workspace, Preset or Agent overrides that are conceptually downstream.

## Consequences

- Default and plugin Runtime paths share one scoped Service implementation; there is no
  separate scoped Runtime or second Service fact source.
- A replacement cannot mutate the Scope or Service view of a Step already in progress.
- Two Runtime instances may assemble different Agent-local Services without sharing local
  registries.
- `ScopedServiceBinding` values are borrowed from the assembler; the Scope resolves them but
  does not acquire cleanup ownership. Plugin-provided application Services remain owned by
  their reversible Activation registrations.
- Service Scope identity is not persisted and does not enter Composition revision or Request
  Fingerprint because a Service binding is not itself model-visible.
- `PluginManifest.allowed_scopes` remains application-only. This decision does not add scoped
  plugin setup, Tool/Prompt/Policy overlays, AgentSupervisor behavior, Wheel installation,
  module reload, isolated plugins or new persistent events.
- Future model-visible scoped contributions must be assembled by the existing candidate →
  ActivationSet → Generation → Lease path and represented in the Composition Snapshot.

## Rejected alternatives

- **Keep `Scope` as a standalone dictionary:** leaves a tested prototype outside the Runtime
  mainline and creates an ability island.
- **Flatten all four layers before publication:** loses source-scope diagnostics and makes
  reversible overrides unable to reveal their ancestor naturally.
- **Expose mutable Registries from `AgentRuntime` and Step Lease:** allows a running Step to
  observe in-place composition changes, defeating Generation freezing.
- **Enable all Manifest scopes immediately:** conflates Service resolution with scoped plugin
  lifecycle, Tool/Policy composition and model-visible Snapshot decisions.
- **Persist Scope names in the Event Log now:** invents a new durable fact with no current
  reconstruction consumer; model-visible changes remain governed by Composition Snapshot.
