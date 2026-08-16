# Testing strategy

The default suite is deterministic and requires no model key.

## Current layers

- Unit tests for EventStore, Scope, hooks, path safety and patch behavior.
- Protocol tests for Surface and invariants.
- End-to-end scripted coding test with real files and subprocess verification.
- Cancellation test proving Turn closure and quiescent runtime disposal.
- Crash recovery tests for known and unknown Effect outcomes.
- Benchmark test producing JSON and Markdown reports.

Run:

```bash
PYTHONPATH=src pytest
```

## Later additions

- Contract test kits for third-party providers, stores and plugins.
- Property tests generating event traces and cancellation points.
- Kill-point injection around Effect Intent, dispatch, outcome and Tool Result.
- Real-provider smoke tests guarded by environment variables.
- Multi-agent deterministic tests with scripted models and virtual time.
