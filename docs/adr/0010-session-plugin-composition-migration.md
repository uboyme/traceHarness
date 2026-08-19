# ADR-0010: Session Plugin Composition Migration Authorization

- Status: accepted for v0.5 Stage C
- Date: 2026-08-19
- Scope: trusted, in-process Tool / Prompt / Service plugins; application-wide Runtime

## Context

Stage B made plugin replacement safe for Composition Generations, but an in-memory
`publish()` does not change the durable identity of an existing Session. Allowing the
Runtime's current plugin set to bypass `verify_session_plugins()` would silently migrate
every Session in the process and would make a process-local boolean a substitute for an
event-log fact. That would also allow a Session to write a new Composition Snapshot without
an explicit user decision.

Stage C adds a user control surface in idle `traceh chat`. The control surface must use the
Stage B candidate Builder and Generation lifecycle, while Session authorization remains an
append-only protocol fact. It must also handle the EventStore's may-have-committed
cancellation window and the application-wide nature of the current Composition.

## Decision

### Internal replacement is not Session migration

`AgentRuntime.replace_plugin_composition()` and a same-identity reload may publish a new
Generation without appending a Session event. This is a lifecycle operation only. Until a
Step actually uses that Generation, there is no durable evidence that a Session saw its
model-visible composition; after a crash, recovery may return to the latest durable
`composition/snapshot`.

A changed plugin identity is authorized only by an explicit control-plane operation for one
Session. No process-wide migration bypass, automatic migration of other Sessions, or
Session rewrite is permitted.

### Append-only authorization event

The event type is `composition/migration-authorized`. Its minimal payload is:

```json
{
  "migration_id": "opaque-stable-unique-id",
  "source_seq": 42,
  "from_plugins": [{"plugin_id": "example.plugin", "version": "1.2.0"}],
  "to_plugins": [{"plugin_id": "other.plugin", "version": "2.0.0"}]
}
```

Only external plugin identities are recorded; `traceh.core` and internal Generation identity
are excluded. `migration_id` is non-empty and stable for the operation. `source_seq` points
to the durable event that currently proves `from_plugins`. The shared identity projection
replays events in sequence order:

1. start with `session/created.metadata.traceh_plugins`;
2. update from each valid `composition/snapshot` containing plugin identities;
3. accept a migration only when `from_plugins` equals the current identity and
   `source_seq` equals the current identity source, then set the identity to `to_plugins`.

Missing fields retain the v0.3 compatibility meaning, while explicit `null`, malformed
structures, duplicate ids, invalid ids and invalid PEP 440 versions are rejected. Version
comparison uses `packaging.version.Version`, not string normalization or `str()` coercion.
The same helper is used by Runtime verification, invariants and request-side parsing so the
identity rules cannot drift.

### Turn and migration linearization

Because Composition is still application-wide, migration requires the Runtime to have no
active Turn. Turn admission and migration use one Composition Gate:

- Turn admission holds the Gate while it verifies the durable Session identity and registers
  the active Turn;
- migration holds the Gate while it confirms global quiescence, prepares the private
  candidate, rereads the Session head, appends authorization with expected-seq CAS and
  publishes the prepared Generation;
- a new Turn cannot pass identity verification between the idle check and publication.

The in-memory active-Turn set is not enough after a hard interruption. Before candidate
construction, and again after the candidate is ready, migration projects the durable Session
lifecycle and rejects an open Turn or Step. The second check plus expected-seq CAS closes the
window in which an external writer could append a lifecycle event while the candidate was
being prepared. `dispose()` and the final Turn-admission check also share the Runtime `_lock`,
so a verification await cannot admit a new Turn after shutdown has linearized.

The candidate is built through `PluginGenerationBuilder` and `PluginActivationSet`; Chat does
not build registries, append events or publish directly. Same-identity reload skips the
authorization append but still uses the same candidate and Generation path.

### May-have-committed cancellation

Every migration has a stable `migration_id`. If the authorization append raises or is
cancelled, the Runtime rereads the Session and searches for that id. If it is absent, the
candidate is fully rolled back and the original cancellation or append failure is re-raised.
If it is present, the write is treated as committed: the Runtime must converge toward the
authorized Generation rather than pretending nothing happened. A cancellation is re-raised
only after that convergence. If publish cannot complete after authorization is durable, the
Session is fail-closed and the Runtime reports a fixed, single-line, bounded repository
error; the old Composition may not run that Session. The authorization is never deleted or
rewritten to hide the failure.

Candidate rollback, Generation cleanup and Runtime disposal use shielded internal
convergence tasks. Repeated cancellation cannot release the caller before owned Tasks,
Activation cleanup and EventStore reconciliation have completed. A real cleanup error wins
over `CancelledError` and is reported through the existing bounded structured error path.

### Persistence and projections

The migration event is not part of the model Surface, Request Fingerprint or Composition
Snapshot. It is safe for Inspector and Replay to read as an unknown-to-model control event,
while CoreInvariantChecker reports stable rule names:

- `migration-id-present`
- `migration-source-present`
- `migration-plugins-valid`
- `migration-from-matches-current`
- `migration-source-seq-matches`
- `migration-outside-turn`

The subsequent Step's `composition/snapshot` remains the durable proof of the actual
Generation used. Internal `generation_id` is never persisted and is not part of Snapshot
revision or Request Fingerprint.

## Consequences

- Users can switch an already-discoverable plugin combination in an idle Chat without
  creating a Turn or model request.
- A user must authorize a changed composition per Session; unrelated Sessions remain
  protected and are not silently migrated.
- The Runtime may reject migration while any Session is active. This is intentional until
  Workspace/Preset/Agent Scope provides a narrower composition boundary.
- A durable authorization followed by a publish failure is visible as a fail-closed Session,
  which can be retried explicitly with the intended target or recovered by restarting with
  that target combination.
- Chat resume hints derive `--plugin` from the Session's latest durable identity rather than
  the current in-memory Generation. This keeps `/session` and exit guidance usable during a
  fail-closed authorization/publish window; if the durable identity cannot be read safely, no
  possibly misleading command is printed.
- `/plugins reload` means rediscovery, setup, conflict checking and health checking of the
  currently discoverable Entry Points. It does not install/uninstall a Wheel, force a module
  reload, watch files or promise changed source code in `sys.modules`.

## Not implemented in Stage C

There is no running `pip install/uninstall`, forced `importlib.reload()`, file watcher,
Workspace/Preset/Agent Scope Overlay, Provider/Policy/Middleware/EventStore/Verifier plugin
contribution, isolated process plugin host, automatic or batch Session migration, multi-Agent,
Workflow, MCP, TUI or model streaming. The package version remains `0.4.0`; Stage C is not a
v0.5 release. Generation identity remains an internal lifecycle detail.
