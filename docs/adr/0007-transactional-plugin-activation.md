# ADR-0007: Entry-point discovery with explicit enablement and transactional activation

**Status:** accepted (v0.4)

**Supersedes nothing.** It implements the direction sketched in
[`../plugin-evolution.md`](../plugin-evolution.md) and stays inside the boundary
[ADR-003](003-kernel-is-not-a-plugin.md) drew: providers, tools, prompts and policies are
replaceable; the rules that make their composition safe are not.

## Context

v0.3 shipped `PluginManifest`, `Plugin`, `PluginContext`, `Activation`, `Lifespan`,
`OwnedTaskSet`, `Scope` and `ServiceRegistry` as primitives with no loader. v0.4 has to
turn them into a real capability: an externally distributed package must be able to add a
tool and a prompt section to a running harness.

Four questions had to be answered before writing the loader.

## Decision 1: Python entry points, group `traceh.plugins`

Discovery reads installed distribution metadata through `importlib.metadata`.

**Why:** it is the mechanism the Python packaging ecosystem already has, so a plugin is an
ordinary wheel installable with ordinary tools, and "what is installed" is answerable
without a TraceHarness-specific registry, config file or directory convention. A directory
scan would have meant inventing a search path, and a search path is a thing users must
then secure.

**Consequence:** discovery is metadata-only and never imports a plugin module. Listing
what is installed is therefore not itself a code-execution step, which is what makes
`traceh plugins list` safe to run against an untrusted machine.

## Decision 2: Installation does not enable

A discovered plugin is imported and set up only when the operator names it, via
`--plugin` or `TRACEH_PLUGINS`.

**Why:** a plugin contributes to the system prompt and the tool schema list, which changes
what the model is told it can do and therefore changes the Composition revision and the
Request fingerprint. Making `pip install` change agent behaviour would mean an unrelated
dependency upgrade could silently alter what a session does. It would also make
"installed" and "trusted" the same statement, which they are not.

**Consequence:** a plugin's required dependency being *installed* is not enough - it must
also be enabled, or activation fails with `required-plugin-missing`. A plugin cannot
enable anything on the operator's behalf.

## Decision 3: Staged setup, then conflict check, then health check, then atomic publish

Activation runs as one transaction in four ordered phases:

1. each plugin's `setup()` runs against **private staged registries**;
2. **every** conflict against the core registries is checked;
3. `health_check()` runs;
4. contributions are published into the real tool, prompt and service mainlines.

**Why staged:** a plugin that fails halfway through `setup()` has already registered
whatever came before the failure. If those registrations went into the live registry, a
failed activation would leave the harness in a state no configuration describes. Private
staging makes "nothing is visible until everything succeeded" structural rather than
something each contribution type has to remember.

**Why conflicts before health:** a candidate implementation ran health checks first. A
plugin whose tool collides with a built-in is going to be rejected regardless of what its
health check reports, so checking health first only grants known-doomed third-party code
an extra opportunity to execute, consume time, or reach the network. Conflicts are decided
entirely from data the manager already holds; asking the plugin first buys nothing.

**Why atomic publish:** a Step freezes one Composition. If publication were incremental, a
Step could be composed from a half-published plugin set, and the Composition Snapshot
would describe a configuration that never coherently existed.

## Decision 4: Cancellation is cancellation, not failure

Every contribution is owned by an `Activation`. Any failure unwinds every activation in
reverse order. Cancellation unwinds identically, then re-raises the original
`CancelledError`.

**Why:** the same candidate implementation caught `CancelledError` in a bare
`except BaseException` and re-raised it as `PluginActivationError` with code
`plugin-setup-failed`. Pressing Ctrl+C during startup told the user their plugin
configuration was broken. Worse, rollback treated a repeat cancellation as a reason to
stop unwinding, so a second Ctrl+C could strand activations that had not been reached.

Rollback therefore absorbs cancellation and keeps unwinding, reusing the project's
existing convergence rule (`await_worker_convergence`, ADR-adjacent to the EventStore
cancellation semantics): repeat cancellation is an intent, not an escape hatch.

**Consequence:** when a caller receives `CancelledError` from `activate()`, every staged
registration is reversed, every owned task has been cancelled and awaited, and every
cleanup has run.

## What v0.4 deliberately does not do

**No hot reload, no Composition generation drain.** Reloading a plugin while turns are
running requires generation reference counting, draining live Step leases, and a rule for
what a mid-turn Composition change means for an already-persisted Request Snapshot. That
is a change to the meaning of a Step, not a loader feature. Plugins load at startup and
unload at `dispose()`; between those, the set is fixed.

**No isolated (out-of-process) plugins.** `trust_mode="isolated"` is a value a manifest can
declare and activation explicitly *rejects*. It is not silently downgraded to `trusted`,
because treating a request for isolation as permission to run in-process would grant the
plugin more privilege than it asked for. Real isolation needs a process boundary, a
serialisation contract for every context call, and a failure model for a crashed child -
none of which exist yet.

**No plugin-ised AgentLoop.** See "Relationship to DeepSeek Harness" in
[`../plugins.md`](../plugins.md). Plugin tools, prompts and services join the *existing*
mainlines; there is no `PluginToolRuntime` and no `PluginAgentLoop`.

**Application scope only.** `allowed_scopes` recognises the future scope names so a
manifest can state intent, but activation requires `application` and v0.4 assembles no
other layer.

## Consequences

- `packaging` becomes the project's first runtime dependency: manifest ranges, plugin
  dependency specifiers and distribution requirements are all PEP 440, and a hand-rolled
  parser guarding a trust boundary would be an incomplete PEP 440 implementation.
- The version needed a single source (`traceh.version`), because `traceh.core` now appears
  in persisted Composition Snapshots and two builders must not disagree about it.
- Sessions record the external plugin identities they were created under, and continuing a
  session under a different set is refused rather than silently permitted.
