# ADR-0011: Extract the plugin composition control plane from AgentRuntime

## Status

Accepted.

## Context

Stages A through C added Generation publication, Generation-owned plugin ActivationSets,
Session plugin-composition migration, Turn-admission exclusion and shutdown convergence.
The behavior was correct, but `AgentRuntime` had become the owner of three separate state
machines:

1. the public Runtime and active-Turn lifecycle;
2. plugin candidate setup, publication and rollback;
3. durable Session plugin-identity verification and migration.

Keeping those concerns in one facade made later Scope work likely to add more locks and
transaction branches to the same class. Moving them into `AgentLoop` would be worse:
plugin composition is an assembly/control-plane concern and does not change the meaning of
Session, Turn or Step execution.

## Decision

Introduce `PluginCompositionCoordinator` as the owner of:

- the shared Turn-admission and Session-migration gate;
- candidate replacement serialization and in-flight replacement tasks;
- pre-registration admission tasks;
- candidate prepare, publish and rollback;
- durable Session plugin-identity verification;
- migration authorization CAS, may-have-committed reconciliation and fail-closed outcomes;
- convergence of replacement and admission work before Composition Drain.

Keep the following in `AgentRuntime`:

- the public API facade;
- dynamic dispatch between its public composition methods, including reload delegating
  through the public migration method and current public plugin-id view;
- the active-Turn table and its lock;
- final Turn admission and duplicate-Session checks;
- the one shared overall shutdown task;
- shutdown ordering across active Turns, the coordinator, Composition Drain and optional
  legacy cleanup.

The coordinator receives narrow callbacks for Runtime disposal state, active-Turn state and
the current Generation's external plugin identities. It reads durable Session identity only
through `SessionService`; it does not introduce another state or fact source.

`AgentLoop` is unchanged and continues to depend only on `CompositionRuntime.lease()`.

## Linearization and shutdown order

Turn admission holds the coordinator gate through durable identity verification and the
facade's final active-Turn registration. Session migration holds the same gate through
candidate validation, authorization CAS and Generation publication.

Shutdown order remains:

1. mark the Runtime disposed and converge active Turns;
2. cancel and converge in-flight replacement/migration work and wait pre-registration
   admissions;
3. retire and Drain Composition Generations;
4. run optional legacy application-level cleanup.

Repeated cancellation cannot release a caller before the same underlying work converges.

## Consequences

- `AgentRuntime` becomes a smaller facade without changing its public behavior.
- Existing public override and instrumentation seams remain observable through the facade;
  extraction does not redirect one public method around another into a private coordinator
  shortcut.
- Candidate and Session migration rules have one focused owner before Scope Overlay work.
- Existing private test seams move under the coordinator; no new public API is promised.
- The coordinator is still application-scoped. Workspace/Preset/Agent overlays are not
  implemented by this decision.
- This decision does not add Wheel installation, module reload, new plugin contribution
  categories, multi-agent behavior or persistent events.

## Rejected alternatives

- **Keep adding branches to `AgentRuntime`:** preserves file locality but mixes unrelated
  state machines and makes future Scope changes harder to reason about.
- **Move replacement into `AgentLoop`:** violates the thin-loop boundary and couples Step
  semantics to plugin product controls.
- **Create a generic orchestration manager:** hides ownership behind an abstract bucket and
  would be a new capability island without a concrete protocol boundary.
