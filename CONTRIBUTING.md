# Contributing

## Setup

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
pytest
```

A no-install workflow is also supported:

```bash
PYTHONPATH=src pytest
```

## Architectural rules

- Do not mutate Session history or model messages in place.
- Do not add provider, tool, plugin or subagent names to `AgentLoop` conditionals.
- Persist any behavior-changing model input in the Composition or Request Snapshot.
- Route model-visible side effects through `ToolRuntime` and the Effect Stream.
- Every registration or background task must have an owner and cleanup path.
- Add or update an invariant when introducing a new durable lifecycle protocol.
- Verify real files, commands or artifacts in E2E tests; do not trust final model text.

## Pull request checklist

- Tests cover the failure path, not only success.
- New events include a schema version and documentation.
- Public API changes include an ADR or migration note.
- Cancellation leaves no open Step, Turn, subprocess or owned task.
- The deterministic scripted suite remains key-free.
