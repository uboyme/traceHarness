# ADR-0008: Composition Generation, Step Lease and Drain

- Status: accepted for v0.5 Stage A infrastructure
- Date: 2026-08-18
- Scope: lifecycle foundation only; the package version remains `0.4.0`

## Context

The v0.4 `StaticCompositionRuntime` assembled a fresh `CompositionSnapshot` for
each Step from the runtime's current registries. It did not have a lifecycle identity,
reference count or a way to keep old Provider/ToolRuntime/Prompt objects alive while a
Step was still using them. That would make a future plugin replacement able to produce a
mixed Step: a Prompt or tool schema from one composition and a Provider or ToolRuntime
from another.

The persisted protocol already treats `CompositionSnapshot.revision` as a fingerprint of
the model-visible Composition. Stage A must add lifecycle identity without changing that
protocol or putting an internal object identity into Request Fingerprints.

## Decision

### Generation is the immutable capability bundle

`CompositionGeneration` binds the LLM registry and resolved Provider, Provider name,
Model, Prompt assembler, ToolRuntime, plugin identities, policy/middleware names exposed
by the ToolRuntime, temperature, max output tokens and an optional explicit
`CompositionResourceOwner` handle. A bare cleanup callback is not accepted by a public
Generation. Tool
metadata (`name`, description, `input_schema` and `effect_kind`) is copied into a flat,
read-only adapter; its public metadata properties have no assignment/deletion path, nested
JSON Schema values are frozen, and `execute()` delegates to the captured Tool object.
Freezing an already frozen Tool is idempotent, so candidates derived from a Generation do
not create recursive adapter chains. Snapshot schema data and execution validation
therefore cannot diverge when the source Tool is later mutated. The runtime never mutates
a published Generation.

Each Generation object carries an internal one-time publication claim. The first Runtime
that publishes it owns that lifecycle identity; a retired or cleaned Generation cannot be
published into this or another Runtime. Resource cleanup ownership is a separate explicit
`CompositionResourceOwner` handle. The assembly layer binds that one-shot handle to the raw
`LlmRegistry`, `ToolRuntime`, `PromptAssembler`, Provider/Tool/Policy/Middleware components;
frozen Generations and compatibility projections propagate the binding directly. There is
no process-wide `id()` catalog and no object-graph scan. A raw slotted Provider, Tool,
Policy or Middleware that cannot retain the binding marker is rejected from a
cleanup-bearing Generation; it must first pass through a binding-capable controlled
assembly. This conservative rejection is necessary because putting the same slotted raw
object into two fresh containers provides no observable ownership marker. Generation
construction performs Provider lookup and all freezing before committing owner/binding
state. Binding storage bypasses arbitrary component setters: it writes to the real instance
dictionary or declared slot and verifies the postcondition. The commit records whether each
field was absent or present (including `None`) and restores that exact state on failure, so a
failed candidate leaves its Owner and raw capabilities retryable. Runtime construction also
builds compatibility views from the frozen initial Generation before claiming the Owner;
caller-controlled Prompt/Registry sources are not read again after the one-shot claim. A used binding
cannot accept a new cleanup owner, a second claim of the same handle is rejected, and
wrapping a frozen Provider or Tool in a new raw registry cannot erase its binding. Runtime
initialization and `publish()` call the same validation/claim entry. Stage A has no
resource-level refcount, so a cleanup-bearing Generation must be built from an unclaimed,
exclusive capability assembly. Cleanup-free derivations remain allowed only when they do
not introduce a cleanup owner. A replacement is otherwise a distinct bundle/record.

`GenerationCompositionRuntime` always has one current record until disposal. The no-plugin
path and the startup-plugin path both construct this same runtime. Startup PluginManager
activation remains the source of the initial Tool, Prompt and Plugin Identity composition.
Service remains an application-level PluginManager/AgentRuntime registration and is not
bound to Generation lifecycle in Stage A.

### Every Step holds a Lease

`CompositionRuntime.lease()` returns a single-use context manager. Its `__aenter__` increments
the current record's Lease count while holding the runtime's `asyncio.Lock`, then builds the
snapshot and returns Provider, ToolRuntime and Snapshot from that exact record. The record
reference is retained by the Lease until `__aexit__` completes. Normal exit, body exception,
body cancellation, snapshot-build failure and repeated cancellation of exit all release the
reference; concurrent exits share one release completion, so the reference cannot be released
twice and the Lease cannot be entered again.

This is necessary because the Step must keep Provider, Prompt, tool schemas, ToolRuntime,
plugin identity, policy/middleware and Snapshot coherent for its entire lifetime. The
AgentLoop remains unaware of generations: it only enters and exits `lease()` and uses the
returned `ActiveComposition`.

### Generation identity is not Snapshot revision

The runtime assigns a monotonically increasing internal `generation_id` at publication.
It is used for lifecycle diagnostics, Lease counts and cleanup reports only. It is not
persisted in `composition/snapshot`, is not copied into `Request Fingerprint`, and is not
needed by replay.

`CompositionSnapshot.revision` remains the canonical fingerprint of model-visible
content. Therefore two different Generation identities with identical Provider/Model,
Prompt, tools, plugins, policies/middleware and parameters have the same revision. A
different lifecycle identity alone is not a model-visible change.

### Publish is a lock-protected linearization point

`publish(candidate)` holds the same `asyncio.Lock` used by Lease acquire and release. The
runtime fixes the identity of the initial `ToolRuntime.sessions` object. Before the
publication transition, a candidate using a different `SessionService` is rejected, as is
any already-claimed Generation (including a retired/cleaned one), any frozen cleanup
lineage, any cleanup candidate sharing a published lineage, and any raw alias already
claimed by a cleanup owner; the current Generation object is therefore also rejected. The
initial Runtime construction uses this same candidate validation and claim path. None of
these failures changes current state. At the linearization point the runtime claims the
candidate, marks the previous current record `retired`, schedules cleanup only if its Lease
count is already zero, assigns the next identity and installs the candidate as current.
After the lock is released, new Lease acquisition can see only the candidate. A Lease that
acquired before the point retains the old record. This keeps every Tool event on the
Runtime's one Session Event Log, prevents cleanup-bearing resources from being shared
across generations, and prevents a just-published current object from also being retired
and cleaned.

Candidate construction and validation happen before `publish`; a failure before the
linearization point leaves the current record unchanged. Cleanup callbacks are never
awaited while the internal lock is held. Stage A also requires the candidate's plugin
identity tuple to equal the startup tuple. This preserves the v0.4 Session identity
contract; deliberate Session migration is not part of this stage.

### Retire and cleanup are reference-counted and at-most-once

The record states are:

```text
current -> retired -> cleaning -> cleaned
                              \-> cleanup_failed
```

`retired` records with active Leases cannot clean. The last release transitions the
record to `cleaning` and creates exactly one named cleanup Task. The same transition is
also used for a retired record that already has zero Leases when Drain observes it.
Cleanup catches and retrieves every `BaseException`, stores only the safe pair
`(generation_id, error_type)`, and transitions to `cleanup_failed` or `cleaned`; it never
leaves an unobserved Task exception. After the cleanup outcome is recorded, the Generation
record is removed from the runtime table. At most one safe structured failure summary is
retained, so repeated Drain calls report a bounded deterministic result without retaining
the Generation object. A cleanup failure poisons the runtime and later `publish()` calls
are rejected; already scheduled cleanup Tasks still run, so one failure cannot prevent
other Generations from converging.

### Drain is a shared, cancellation-resistant convergence operation

`drain()` creates or reuses one internal drain Task. That Task waits until every retired
record has zero Leases and its cleanup Task has completed. It raises
`CompositionDrainError.failures` only after all eligible records have attempted cleanup.
The failure result is bounded and deterministic. One failed Generation therefore cannot
prevent other Generations from converging, and the poisoned runtime cannot accumulate an
unconsumed error list through repeated publication.

Callers await the shared Task through `asyncio.shield`. If the caller is cancelled,
`await_worker_convergence()` absorbs the first and every repeated cancellation while
waiting for the same internal Task; once the Task converges, the original
`CancelledError` is re-raised. Cancellation is an intent signal, not an escape from
resource convergence. `drain()` and `dispose()` reuse their internal Task and are
idempotent; no lifecycle correctness depends on `__del__` or garbage collection.

### Ownership and disposal order

Stage A does not turn PluginManager into a multi-generation manager. The default
Generation has no plugin Activation cleanup callback. `PluginManager` remains the sole
owner of startup plugin Activation cleanup, including reverse activation order. The
runtime shutdown order is:

1. cancel and gather active Turns;
2. dispose the Composition Runtime and Drain retired/current Generations;
3. dispose PluginManager and reverse-unload its Activations.

If Generation cleanup fails, Runtime still gives PluginManager its cleanup opportunity and
then reports the structured Generation failure (or a deterministic `ExceptionGroup` if
both owners fail). Thus one resource does not have two cleanup owners. Service registrations
follow the same application-level ownership and are not Generation cleanup resources.

## Consequences

- A Step can never combine a snapshot from one Generation with Provider or ToolRuntime
  references from another through the supported Lease path.
- The runtime's SessionService identity is part of candidate validation, so a Generation
  cannot silently redirect Tool events to another EventStore.
- Generation publication is a one-time ownership claim across Runtimes; cleaned or retired
  objects cannot be republished. Resource cleanup ownership is independently claimed by an
  explicit `CompositionResourceOwner` handle: used bindings, multi-hop frozen derivations,
  wrapper aliases and a second owner claim cannot create another cleanup owner. Slotted raw
  capabilities without binding storage are rejected from cleanup-bearing candidates rather
  than guessed to be fresh. Without resource-level refcounting, cleanup-bearing candidates
  must be built from an unclaimed exclusive capability assembly.
- A current Generation cannot be retired by publishing itself, and repeated replacements
  of frozen no-cleanup sources remain executable because Tool adapters stay flat.
- The existing Composition event and Request reconstruction protocol remains unchanged;
  Generation identity is intentionally absent from persisted facts.
- Cleanup failures are observable without exposing untrusted exception text or traceback.
- The runtime now owns asynchronous lifecycle Tasks explicitly and retrieves their
  outcomes.
- A user-facing hot-reload command is still required to build/validate candidates and
  call `publish`; Stage A does not provide it.

## Not implemented in Stage A

This ADR does not implement or authorize:

- a plugin hot-update CLI or runtime `pip install`/`uninstall`;
- Workspace, Preset or Agent Scope Overlay;
- Provider, Policy, Middleware, EventStore or Verifier plugin contributions;
- isolated plugin processes;
- multi-Agent, Workflow, MCP, TUI or model streaming;
- a v0.5 version bump, tag or release.

The package remains `0.4.0`, and PluginManager remains startup-only.
