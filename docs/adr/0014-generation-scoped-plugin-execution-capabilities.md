# ADR-0014: Plugin execution capabilities follow the Generation lease

Status: Accepted

## Context

Stage D2 resolved host-supplied Tool, Prompt and Policy overlays into the existing
ActivationSet → CompositionGeneration → Step Lease path. Plugins themselves could still
contribute only Tool, Prompt and Service values. Provider, Policy, Middleware and Verifier
are executable capabilities: publishing one beside that path would let an in-progress Step
mix two compositions or leave plugin cleanup with no owner.

EventStore is different. It is not a Step capability: SessionService, recovery, inspector,
request reconstruction and every event append share it as the Runtime's process-lifetime
fact source. A Generation-owned activation may retire after its last Step Lease.

## Decision

- `PluginContext` adds reversible `register_provider()`, `register_policy()`,
  `register_middleware()` and `register_verifier(name, verifier)` methods.
- Setup stages all contributions in private candidate registries. When every setup returns,
  the manager closes every Composition registration method before conflict checks and health;
  health may inspect configuration/Services and retain lifecycle cleanup or Owned Tasks, but
  cannot mutate the candidate capability set. Tool, Provider, Policy and Middleware names are
  captured at registration; conflict/selection/attribution reads that identity, and checks
  after setup, health and awaited publication boundaries reject later drift with
  `plugin-contribution-identity-changed`. Tool and LLM reversal handles retain the same
  registration key. Conflicts and missing explicitly selected
  Provider/Verifier values fail before plugin health checks with stable structured codes.
  Cancellation and failure use the existing reverse Activation rollback.
- Enabling a plugin never selects a Provider or Verifier implicitly. Provider selection uses
  the existing provider name plus an explicit Model. Verifier selection uses a distinct name
  and is mutually exclusive with a direct or command Verifier.
- The final Provider Registry, ordered Policy/Middleware objects and selected Verifier travel
  in one `PluginActivationSet`. `CompositionGeneration` checks their object identities and a
  Step Lease exposes that exact set. If the ActivationSet exposes an LLM registry, the selected
  Provider must be present there and be the exact object used by the Generation. Custom D0
  ActivationSets that omit the optional D3 registry continue to borrow the coordinator's core
  registry during replacement.
- Returning a public `PluginActivationSet` is an asynchronous hand-off, not the end of the
  transaction. The set retains an immutable receipt of its Registry containers, members,
  registered names, Prompt, Policy/Middleware order, Verifier and plugin identities.
  `CompositionGeneration` revalidates that receipt before claiming the candidate. Frozen
  schemas and executable lookup use Registry keys, so an Owned Task that runs after prepare
  returns cannot create one Generation whose Snapshot and ToolRuntime name different Tools.
- Activation and `PluginActivationSet` construction form one transaction. Ownership transfers
  only after the set and its receipt are constructed successfully. If receipt or Scope
  validation fails first, the temporary Manager remains the sole owner and must converge Owned
  Tasks plus reverse cleanup before propagating the hand-off failure. Repeated cancellation
  cannot release the caller early; cleanup failure is preserved together with the original
  transfer failure. The pair is constructed through `BaseExceptionGroup`, which derives a
  normal `ExceptionGroup` when every member is an `Exception` but can also retain a direct
  `BaseException` such as `KeyboardInterrupt` or `SystemExit` without masking both failures.
- AgentLoop executes the selected Verifier while the Step Lease is still held. A later
  Generation publish therefore cannot change verification for an in-progress Step.
- EventStore is not added to `PluginContext`. It remains directly injectable only when the
  Runtime is constructed.

## Ownership and persistence

The plugin Activation owns its new registrations, background tasks and cleanup. The
Generation owns the ActivationSet's lifetime; cleanup starts only after the last old Lease
releases. Core/default capabilities remain borrowed when they were not registered by a
plugin.

Provider, Policy and Middleware model-visible names continue to use the existing Composition
Snapshot and request fingerprint. Verifier outcome continues to use the existing
`verification/result` event. D3 adds no new event type, mutable fact source or parallel
runtime.

## Consequences

- Plugin execution capabilities use the same rollback, publish, Lease and Drain semantics as
  plugin Tool/Prompt/Service contributions.
- An installed or enabled plugin cannot silently replace model or completion behavior.
- Old and new Generations can coexist without an old Step observing a new Verifier.
- EventStore plugins remain deferred until there is a process-lifetime pinned activation
  owner, deterministic Store construction/disposal, Session compatibility semantics and
  backend contract verification.
- Setup is still application-only, trusted and in-process. D3 does not add runtime Wheel
  installation, module reload, child-scope plugin setup, isolation, multi-agent or workflow.

## Rejected alternatives

- **Auto-select the only contributed Provider or Verifier:** rejected because installing or
  enabling code must not silently change model or completion authority.
- **Run Verifier after releasing the Step Lease:** rejected because one Step could then use a
  Provider/Tools from one Generation and completion evidence from another.
- **Keep a separate plugin Provider/Verifier lookup beside Composition:** rejected because it
  creates a parallel mutable fact source outside Generation freezing.
- **Put EventStore in the Generation-owned PluginContext:** rejected because retiring a Step
  Generation cannot safely close the process-wide ledger still referenced by Sessions.
