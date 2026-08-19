# Architecture

## Goal

TraceHarness v0.4 is a small coding-agent runtime whose current implementation can grow
without turning `AgentLoop` into a feature switchboard. The stable center is protocol
semantics, not a specific provider, tool set or UI.

That rule is why v0.4's plugin system sits in the runtime *assembly* layer rather than
inside the loop: plugin tools, prompt sections and services join the existing registries,
and `AgentLoop` has no reference to `PluginManager`. See
[`plugins.md`](plugins.md) and [ADR-0007](adr/0007-transactional-plugin-activation.md).

## Layers

```text
Applications
  CLI, SDK, evaluation, inspector

Control
  AgentRuntime, PluginCompositionCoordinator, AgentLoop, ContinuationRuntime

Capabilities
  CompositionRuntime, PromptAssembler, LlmRegistry, ToolRuntime, CompletionVerifier

State
  SessionService, EventStore, SessionEventFeed, SurfaceProjector, RecoveryService

Kernel
  Scope, HookDispatcher, Activation, Lifespan, OwnedTaskSet
```

Dependencies point downward. `traceh.api` contains structural protocols and frozen value
types that third-party packages import, re-exported for plugin authors through
`traceh.plugins`. Runtime internals are not plugin API: `PluginContext` exposes tools,
prompt sections, services, cleanups, owned tasks and configuration, and no `AgentRuntime`,
`AgentLoop`, `EventStore`, `ToolRegistry` or `PromptAssembler` object.

"Frozen" is the dataclass guarantee - fields cannot be rebound - not deep immutability. An
`EventEnvelope.data` graph stays a mutable JSON structure, so an `EventStore` must hand out
detached copies rather than references into its own history; see
[`event-protocol.md`](event-protocol.md).

`SessionEventFeed` sits in the State layer but points upward: it is an in-process
notification channel that applications may subscribe to, never a store the runtime reads
from. Consumers receive the read-only `EventFeed` interface - publication is private to the
`PublishingEventStore` that owns the feed, so an observer cannot announce an event the store
never accepted. Notification means "the store accepted this for the `Durability` requested",
not "this is fsynced"; the feed adds no persisted fact and no crash durability of its own.
Dependencies still point downward - the CLI timeline depends on the feed, and nothing in
`runtime/` depends on the CLI. See [`event-feed.md`](event-feed.md).

## Thin loop

`AgentLoop.run_turn()` owns only these transitions:

1. Accept and claim an Inbox message.
2. Open a Turn.
3. Open a Step.
4. persist pending user messages;
5. freeze a Composition Snapshot;
6. build and persist a request;
7. execute one model attempt;
8. execute the returned tool batch;
9. close the Step;
10. ask `ContinuationRuntime` whether to continue;
11. close the Turn.

Provider retry, prompt sections, policies, verification and persistence are delegated to
services. Subagents should later be tools backed by `AgentSupervisor`, not branches in
this loop.

## State planes

### Session Stream

The Session Stream contains user/model/tool/lifecycle facts. It is the source for runtime
state, replay and model Surface.

### Effect Stream

The Effect Stream records side-effect intent, dispatch and outcome. It closes the
otherwise invisible crash window between performing an operation and saving its model-
visible result.

### Telemetry

Telemetry is intentionally absent from the durable protocol in v0.4. A future
OpenTelemetry plugin should subscribe to NOTIFY hooks and may sample or lose data without
changing recovery semantics. Note that v0.4's `PluginContext` does not yet expose hook
subscription, so such a plugin is not writable today.

## Composition

Every Step obtains an `ActiveComposition` through `CompositionRuntime.lease()` and creates a `CompositionSnapshot` containing provider, model, final system
prompt, visible tool schemas, policies and plugin identities. The snapshot receives a
canonical SHA-256 revision.

`plugin identities` are real data as of v0.4: `traceh.core` at the single source version,
plus each activated external plugin's true id and version. Replay rebuilds them from the
event, so a plugin-affected request stays reconstructable.

The lease retains the exact provider and Tool Runtime objects while the model/tool cycle
runs. Step-local freezing prevents a plugin composition change from changing tools halfway
through one model/tool cycle. The current runtime publishes immutable Generations; old
Leases retain the old Generation until their last release, after which Drain owns cleanup.
Generation identity stays internal and does not change the request or event schema.

`AgentRuntime` is the public facade and owns the active-Turn table plus overall shutdown.
`PluginCompositionCoordinator` owns the plugin candidate transaction, the shared
Turn-admission/migration gate, durable Session plugin-identity checks and in-flight
replacement and pre-registration admission convergence. It does not execute a Turn and it
does not create a second Session fact source. `AgentLoop` still sees only
`CompositionRuntime.lease()`.

## Extension rule

A feature belongs in the loop only when it changes the meaning of Session, Turn or Step.
Otherwise it should be one of:

- a service implementation;
- a registry entry;
- a prompt section;
- a tool policy;
- continuation rule;
- verifier;
- projection;
- observer hook;
- orchestration above the loop.
