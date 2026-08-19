# ADR-0009: Generation-owned Plugin ActivationSet

- Status: accepted for v0.5 Stage B infrastructure
- Date: 2026-08-19
- Scope: trusted, in-process Tool / Prompt / Service plugins only

## Context

v0.4 made plugin activation transactional, but one `PluginManager` still owned the
application registries and all plugin cleanup from startup until process shutdown. That
ownership is incompatible with Composition Generations: after publishing a new
composition, an old Step may still hold a Lease and may still call an old plugin Tool or
Service. Unloading one application-level manager would remove resources from under that
Step.

Stage A established the immutable Composition Generation and the Lease/Retire/Drain
boundary. Stage B moves the plugin activation boundary into the same lifecycle without
making `AgentLoop` aware of plugins or replacement control planes.

## Decision

### Candidate transaction

`PluginGenerationBuilder` accepts an explicit, validated tuple of enabled plugin ids. Each
call forks independent Tool, Prompt and Service registry views. The existing
`PluginManager` transaction then performs discovery, dependency ordering, Manifest
validation, setup, conflict checking and health checks against those private views.

Nothing is visible to the current Generation while the candidate is being prepared.
Failure at setup, health check, Generation construction or publication reverses every
Activation immediately, including owned tasks, Service registrations, Tool registrations
and Prompt registrations. Candidate cancellation is converged before the original
`CancelledError` is re-raised when rollback succeeds. If rollback cleanup genuinely fails,
the remaining Activations are still attempted and a bounded `PluginDisposeError` is the
terminal result; cancellation cannot hide an incomplete cleanup.

### ActivationSet ownership

The successful result is a one-shot `PluginActivationSet`. It owns the candidate
registries, Activation objects, plugin identities, reverse activation order and cleanup
state. A Composition Generation claims the set at the same synchronous publication claim
used for Generation and Stage A resource ownership. The set cannot be claimed by another
Generation or Runtime, and the temporary PluginManager clears its Activation references
after transfer. Before publication, the candidate transaction owns rollback; after
publication, the Generation owns cleanup. There is no second owner.

The set's lifecycle is:

```text
candidate --claim/publish--> owned by Generation
candidate --failure/cancel--> reverse rollback --> disposed or cleanup_failed
owned + retired + leases > 0 --> waiting
owned + retired + leases == 0 --> cleaning --> disposed or cleanup_failed
```

An ActivationSet cleanup Task is shared by all waiters and is shielded from caller
cancellation. Repeated cancellation waits for the same internal task to converge and
then re-raises the first cancellation only when cleanup succeeded; the cleanup Task's real
failure otherwise wins. Cleanup runs Owned Tasks first, then closes
registrations in reverse plugin/dependency order. A plugin failure does not skip later
cleanup. Raw plugin exception objects and tracebacks are not retained; only bounded,
repository-authored structured failure summaries are exposed.

### Borrowed core versus generation-owned resources

The ownership boundary is explicit rather than inferred from object graphs or wrapper
types:

- borrowed core: `SessionService`, `EventStore`, the core LLM Provider, built-in Tools,
  base policies/middleware and base configuration;
- generation-owned: plugin `Activation`, plugin Tool registrations, Prompt sections,
  plugin Services, Owned Tasks and plugin cleanup callbacks.

Borrowed core objects may appear in more than one forked candidate and are never closed by
an ActivationSet cleanup. Plugin resources are created by that candidate's setup and are
not accepted by another Generation. Shared plugin resource reference counting is not
implemented in Stage B; an ActivationSet is therefore the exclusive lifecycle unit.

### Generation identity and Snapshot revision

Generation identity is an internal lifecycle number used for Lease accounting, retirement,
cleanup and diagnostics. It is not persisted in an event, Composition Snapshot or Request
Fingerprint. `CompositionSnapshot.revision` remains the fingerprint of model-visible
content. Two Generations with identical model-visible content therefore have the same
revision even when their internal identities differ.

### Publish and Lease linearization

`publish()`, Lease acquisition, Lease release and retirement are serialized by the
Composition Runtime's single `asyncio.Lock`. The publication linearization point is the
lock-protected installation of the new Generation record after candidate validation and
ActivationSet claim. The old record is marked `retired` before the new record becomes
`current`; no await runs plugin setup or cleanup while this lock is held. A Lease stores
the exact record it acquired, so a later publish cannot replace any of its Provider,
Prompt, ToolRuntime, Service, Policy/Middleware or Snapshot references.

The current record and retired records have these states:

- `current`: new leases may acquire it;
- `retired`: no new lease may acquire it; cleanup waits for the lease count to reach zero;
- `cleaning`: exactly one cleanup Task is running;
- `cleaned`: cleanup succeeded and the runtime record is removed;
- `cleanup_failed`: cleanup converged, a bounded failure was recorded, the record is
  removed, and the Runtime is poisoned for future publication.

### Session durability

`session/created.metadata.traceh_plugins` remains the Session's initial plugin
composition. Each Step's `composition/snapshot` records the complete PluginIdentity tuple
actually used. On recovery, the latest legal durable Composition Snapshot wins; if no
Snapshot exists, recovery falls back to `session/created`, including v0.3 sessions that
lack the field. Explicit null, malformed values and duplicate ids remain invalid. An
internal Generation replacement does not authorize an existing Session to cross the
composition boundary: the Session must still match that latest durable identity. Stage B
does not use a process-wide bypass; callers that need the new combination create a new
Session until Stage C defines an explicit per-Session migration protocol.

Publishing a Generation is an in-memory lifecycle operation and does not append an event.
If the process stops before a Step uses the new Generation, recovery may legitimately
return to the last durable Snapshot. The next Step after a successful replacement writes
the new composition, request snapshot and fingerprint through the existing AgentLoop
path.

### Runtime disposal

`AgentRuntime.dispose()` first converges active Turns, then cancels and waits for every
in-flight candidate replacement Task, including its rollback, and only then calls
Composition Drain. Drain retires the current Generation and waits for every
Generation-owned ActivationSet to finish its cleanup. Only after that does a custom legacy
assembly release application-level builders or a separately supplied legacy PluginManager.
The default Stage B factories do not retain a PluginManager cleanup owner, so startup
plugin cleanup occurs exactly once through the initial Generation. Shutdown retrieves every
replacement Task's outcome. A normally cancelled candidate is not a shutdown error, but a
candidate rollback `PluginDisposeError` is included in shutdown's stable result, so the first
and every later `dispose()` report failure instead of success.

## Consequences

- Startup plugin activation and internal replacement use one real Generation/Lease/Drain
  mainline; there is no plugin-specific AgentLoop or ToolRuntime.
- Old plugin Services and Tools remain usable inside old Leases while a new composition is
  current.
- Candidate failures cannot mutate or partially publish the current registries.
- Cleanup errors remain observable as bounded structured results, but their arbitrary
  exception text and traceback are intentionally not retained or displayed.
- Cancellation controls convergence, not truth: it cannot overwrite a cleanup Task's real
  failure or make Runtime shutdown report success.
- Plugin identity may change between Generations, while Session initial metadata remains
  immutable historical context.

## Not implemented in Stage B

Stage B itself provided no user-facing reload command. Stage C later added only the idle
Chat commands `/plugins reload`, `/plugins use` and `/plugins use --none`; it does not add a
generic `/reload`, running Wheel installation or uninstallation, file watcher, forced module
re-import, Scope Overlay, Provider/Policy/Middleware/EventStore/Verifier contribution surface,
isolated process plugin host, Session automatic migration, multi-Agent, Workflow, MCP, TUI or
model streaming. The package version remains `0.4.0`; neither Stage B nor Stage C is a v0.5
release.
