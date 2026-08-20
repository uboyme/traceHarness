# TraceHarness v0.5 Plugin Contract

This guide is pinned to `traceharness-py>=0.5,<0.6`. Import author-facing values
only from `traceh.plugins`; do not reach into `traceh.runtime`, `traceh.session`,
`traceh.kernel` or manager implementation modules.

## Packaging identity

- The Python distribution declares a dependency on
  `traceharness-py>=0.5,<0.6`.
- The Entry Point group is exactly `traceh.plugins`.
- The Entry Point name is the plugin id and exactly matches
  `PluginManifest.plugin_id`.
- Plugin ids are lowercase and contain only `a-z`, `0-9`, `.`, `_` and `-`.
- Installation makes a plugin discoverable; it never enables it.

## Supported contribution surface

During `setup(context, config)`, a v0.5 application-scoped trusted plugin may
use:

- `context.register_tool(tool)`;
- `context.register_prompt(PromptSection(...))`;
- `await context.provide(ServiceKey(...), value)`;
- `context.register_provider(provider)`;
- `context.register_policy(policy)`;
- `context.register_middleware(middleware)`;
- `context.register_verifier(name, verifier)`;
- `context.add_cleanup(callback)`;
- `context.spawn_owned(coroutine, name=...)`.

All Composition contributions happen during `setup()`. A health check may
inspect state and attach cleanup or owned tasks, but may not add late
contributions. Registering a Provider or Verifier does not select it; the
operator must select it explicitly.

Every manifest for this skill uses the currently supported boundary:

```python
PluginManifest(
    plugin_id=PLUGIN_ID,
    version=PLUGIN_VERSION,
    requires_traceh=">=0.5,<0.6",
    allowed_scopes=("application",),
    trust_mode="trusted",
    provides=(...),
)
```

Use only the requested contribution types. Do not manufacture a Tool, Service
or background task merely to make a candidate look substantial.

## Lifecycle and evidence

- Registrations and cleanup belong to the existing plugin Activation and
  Generation lifecycle. Do not create another registry or loader.
- A background task must be created with `spawn_owned`; never use a detached
  `asyncio.create_task()`.
- Cleanup must be deterministic, bounded where possible and safe to call once
  through the activation owner.
- A Tool declares the correct `EffectKind`; write/process/network effects must
  not masquerade as reads.
- Model-visible Tool schemas, Prompt sections, Provider selection, Policy and
  Middleware names, and plugin identity flow through the existing
  Generation/Lease/Composition Snapshot path.
- A named Verifier is captured by the same Generation and Step Lease, but
  `CompositionSnapshot` does not include Verifier identity. Its observed result
  is persisted as `verification/result` evidence.
- Do not create `runtime.state`, a mutable message history, a second EventStore,
  or any other fact source beside the existing Session/Event protocol.

## Explicitly unavailable in v0.5

Plugins cannot replace `AgentLoop` or provide EventStore. There is no isolated
process host, untrusted sandbox, running Wheel install/uninstall, forced module
reload, file watcher, multi-agent surface, Workflow surface, MCP bridge, TUI or
model streaming. Do not claim or emulate those features inside a candidate.

The complete released author contract is `docs/plugins.md` in the TraceHarness
source repository. The Python Quality plugin is a useful real-world reference,
but its Python-specific names and behaviour are never defaults for another
candidate.
