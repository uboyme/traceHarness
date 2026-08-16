# Changelog

## Unreleased

- Add shared `AGENTS.md` / `CLAUDE.md` development instructions and continuously
  maintained formal/plain-language project context documents under `docs/note/`.
- Load OpenAI-compatible provider settings and secrets from `.env` without adding a
  runtime dependency.
- Add `--env-file` and `TRACEH_*` configuration with explicit CLI/environment/file
  precedence.
- Extend `traceh doctor` with non-secret provider and API-key-presence diagnostics.
- Guard every `JsonlEventStore` critical section with a real cross-process file lock on
  both POSIX (`fcntl`) and Windows (`msvcrt` byte-range locking), with an optional
  `lock_timeout`, and cover it with independent-process tests plus a Windows CI job.
- Close unfinished Model Attempts during crash recovery: an attempt with a durable
  `assistant/message` in the same turn and step, written after the attempt started, is closed as
  `succeeded`, anything else as `unknown_after_crash`, without ever calling the provider again,
  merging chunks or inventing usage and finish reasons. Starts whose `attempt_id` is not a
  non-blank string are skipped instead of being coerced into an invented identity.
- Report the repair as `closed_model_attempts` in `RecoveryReport` and `runtime/recovered`, and
  add Model Attempt identity, pairing, duplication and real-lifecycle-scope invariants to
  `CoreInvariantChecker`.
- Make cancelling `JsonlEventStore.append`/`read`/`head` abandon the lock wait and
  converge its worker thread before `CancelledError` reaches the caller, so a cancelled
  operation can no longer write to the stream in the background. Convergence absorbs
  repeated cancellation instead of treating it as an early exit.

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
