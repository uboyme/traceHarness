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
- Capture verifier and shell output into temporary files owned by TraceHarness instead of
  pipes, so the one owner of a command's output is a file that already holds everything the
  child flushed. A verifier timeout, and a timeout a tool reports for itself, no longer
  discard the output produced before them, because nothing has to cancel a read and start
  a second one. A `ToolRuntime` budget timeout is not one of these: it cancels the tool, so
  the capture is not read and only the runtime's generic timeout is reported.
- Converge the child process on both the cancellation and the timeout path: terminate, wait
  briefly, kill, and do not return until it is gone. A component's own timeout path then
  reads the captured files; the cancellation path re-raises without reading them and reports
  no result. A cancellation arriving during the cleanup is absorbed and re-raised only after
  the direct child is confirmed dead, closing the escape where the old `kill()` plus bare
  `communicate()` could release the caller first. `ShellTool` uses the same implementation.
- Report a timed-out verifier's stdout and stderr in its summary, under the same bound as a
  normal result, so the continuation policy actually feeds that evidence to the next Step
  instead of only "Verifier timed out". The full text stays on
  `VerificationResult.stdout`/`stderr`.
- Tell a tool's own timeout apart from the runtime's budget expiring. `ToolRuntime` used one
  `except TimeoutError` for both, so a `shell` call that timed itself out lost its stdout and
  stderr from `effect/outcome` and `tool/result` and had its duration reported as the
  runtime's budget. A nested boundary now re-labels a tool-raised `TimeoutError` as
  `ToolReportedTimeout` - no error-text matching - and its own message and duration are kept;
  the runtime's budget keeps the previous generic behaviour and still converges the child.
- Drop the pipe capture that made "unclosed transport" and "Event loop is closed" possible
  after the loop closed. Without stdout/stderr pipes there are no pipe subtransports and
  `await process.wait()` finishes the cleanup, so no private asyncio attribute is touched.
  A grandchild that inherited the captured handles may still outlive the child; only the
  direct child is managed.
- Keep `SystemRoot` and the other Windows platform variables in the sanitized child
  environment. Without them any child died with WinError 10106 while importing `asyncio`,
  so a verifier command as ordinary as `python -m pytest` could not start on Windows.
- Set `PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8` for child processes, so a Python child on
  Windows no longer returns its Chinese output as CP936 bytes that decode to U+FFFD. This
  settles Python children only; native tools still follow the console code page.
- Wait for the OpenAI-compatible HTTP worker instead of detaching it when a request is
  cancelled, so no background worker outlives the turn. `urllib` cannot be aborted midway,
  so this is convergence and may wait until the provider timeout, not an instant abort.
- Let the built-in placeholder Scripted Provider answer every turn, so `traceh chat` without
  `--script` survives past the first turn. An explicit `--script` still reports exhaustion.
- Add `traceh chat`, an interactive multi-turn terminal loop over one session. It starts a
  new session from a workspace or continues an existing one by id, recovering it first and
  starting no turn until the user types one. Every line becomes a real Turn through
  `AgentRuntime.run_existing()`, so the event log stays the only conversation state.
- Add `/help`, `/session`, `/exit` and `/quit`, recognised only as whole lines, plus
  concise turn summaries, non-fatal turn errors, EOF handling and Ctrl+C that converges the
  running turn and reports the session id to resume with.
- Configure the standard streams as UTF-8 with `errors="replace"`, strip a leading
  byte-order mark from input, and refuse lines containing U+FFFD instead of guessing what
  the undecodable characters were.
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
