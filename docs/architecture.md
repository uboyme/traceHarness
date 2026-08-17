# Architecture

## Goal

TraceHarness v0.3 is a small coding-agent runtime whose current implementation can grow
without turning `AgentLoop` into a feature switchboard. The stable center is protocol
semantics, not a specific provider, tool set or UI.

## Layers

```text
Applications
  CLI, SDK, evaluation, inspector

Control
  AgentRuntime, AgentLoop, ContinuationRuntime

Capabilities
  CompositionRuntime, PromptAssembler, LlmRegistry, ToolRuntime, CompletionVerifier

State
  SessionService, EventStore, SessionEventFeed, SurfaceProjector, RecoveryService

Kernel
  Scope, HookDispatcher, Activation, Lifespan, OwnedTaskSet
```

Dependencies point downward. `traceh.api` contains structural protocols and frozen value
types that future third-party packages should import. Runtime internals are not plugin
API.

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

Telemetry is intentionally absent from the durable protocol in v0.3. A future
OpenTelemetry plugin should subscribe to NOTIFY hooks and may sample or lose data without
changing recovery semantics.

## Composition

Every Step obtains an `ActiveComposition` through `CompositionRuntime.lease()` and creates a `CompositionSnapshot` containing provider, model, final system
prompt, visible tool schemas, policies and plugin identities. The snapshot receives a
canonical SHA-256 revision.

The lease retains the exact provider and Tool Runtime objects while the model/tool cycle runs. Step-local freezing prevents a future plugin update from changing tools halfway through
one model/tool cycle. v0.3 creates a new immutable snapshot directly; v0.5 can replace
that operation with a generation lease without changing the request or event schema.

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
