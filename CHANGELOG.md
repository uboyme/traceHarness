# Changelog

## Unreleased

- Load OpenAI-compatible provider settings and secrets from `.env` without adding a
  runtime dependency.
- Add `--env-file` and `TRACEH_*` configuration with explicit CLI/environment/file
  precedence.
- Extend `traceh doctor` with non-secret provider and API-key-presence diagnostics.

## 0.3.0

- Added append-only JSONL Session and Effect streams.
- Added Session, Turn, Step, Model Attempt and Tool Invocation lifecycle events.
- Added frozen Composition Snapshots and request fingerprint reconstruction.
- Added deterministic Scripted and dependency-free OpenAI-compatible providers.
- Added unified Tool Runtime, policies, barriers, timeouts and five coding tools.
- Added workspace confinement and child-process environment sanitization.
- Added generic crash recovery, invariant checks, replay and HTML inspection.
- Added external completion verification and deterministic benchmark runner.
- Added reversible Activation, Scope, typed hooks and owned-task kernel primitives for
  the plugin roadmap.
