# Changelog

## Unreleased

- Add `SessionEventFeed`, an in-process channel for observing events an `EventStore` has
  accepted, plus `PublishingEventStore`, a decorator that wraps any `EventStore` and
  announces what it just accepted. The decorator is the boundary because `store.append()`
  in `session/service.py` is the only append call site in `src/`, so every writer is
  observable through one place and announcing a non-accepted event is not expressible.
  The feed persists nothing, adds no event type, replays no history, keeps no state and is
  visible only inside one process; recovery, the inspector and the invariant checks keep
  reading the store. See [`docs/event-feed.md`](docs/event-feed.md).
- Publish only after a successful append, and in sequence order. A conflict, a failure or a
  cancellation publishes nothing - including on the may-have-committed path, since the feed
  may miss events and the log may not. Publication means the store *accepted* the event for
  the `Durability` its caller requested, not that it is fsynced: `durability` is passed
  through unchanged, `BATCHED` is never upgraded to `SYNC`, and crash durability stays
  entirely the store's contract. The feed adds no persisted fact of its own. One lock per stream spans append and publish, so two
  concurrent writers cannot invert: the store serializes the writes, but the callers resume
  independently, so seq 10's writer could otherwise publish after seq 11's.
- Detach once per subscriber rather than once per publish, extending the event ownership
  contract from the store boundary to fan-out. A subscriber mutating its own event changes
  neither stored history, nor another subscriber's event, nor a later subscriber's.
- Show a live activity timeline in `traceh chat` while a turn runs: step boundaries, model
  attempts, the tool lifecycle, verification results, runtime errors, cancellation and
  recovery. Every line carries the event's real persisted `seq`, so a line can be found
  again with `traceh inspect`; the numbers are intentionally non-contiguous because hidden
  events keep their sequence numbers. `--no-timeline` turns it off for scripts and quiet
  output, and is a startup flag rather than a chat command.
- Keep the timeline conservative about what it prints. Prompts, request and composition
  snapshots, assistant text, file contents, patches and command output are never shown, and
  an unrecognised event type renders as nothing instead of a raw payload dump. Tool argument
  summaries are limited to a per-tool allowlist, clipped to one bounded line, and suppressed
  entirely when the value looks like a credential; an unknown tool shows only its name and
  call id.
- Keep the loop free of presentation: timeline wording lives in `cli/timeline.py` as a pure
  projection from `EventEnvelope` to one line or nothing, and `AgentLoop` imports no CLI,
  console or timeline code. The timeline never enters the model surface and cannot change a
  request fingerprint.
- Give consumers a read-only `EventFeed` interface - `subscribe()` and `subscriber_count()`
  only - and keep publication private to the store that owns the feed. A public `publish`
  would let any holder inject an envelope the store never accepted, which a subscriber could
  not tell from a real event, so the timeline would faithfully display a step that never
  happened. `AgentRuntime.events` is now required and must be the same feed its
  `PublishingEventStore` publishes to: defaulting it handed callers a subscribable object
  that stayed silent forever.
- Treat every timeline string as untrusted input. A tool name comes from a model response and
  an error type from an arbitrary exception, so rendering one raw let a newline forge an extra
  timeline row and an ESC byte emit a live terminal control sequence. All payload text now
  passes through one `sanitize()`: control and format characters - including bidirectional
  overrides - become spaces, whitespace collapses to exactly one line, and length is bounded.
- Stop displaying a `shell` command entirely, and stop displaying a `runtime/error` message.
  A command line is the most likely place for a credential and no keyword scan recognises
  every secret shape, so the command is withheld unconditionally rather than filtered; an
  exception message is arbitrary text that can quote a request or a credential. Shell shows
  its name and call id, `runtime/error` shows its type. Displayable read-tool paths are still
  checked for credential shapes and dropped whole when one matches.
- Converge the timeline printer when a drain is cancelled. `asyncio.shield` protected the
  printer but did not keep the drain waiting, so a cancelled drain returned while the printer
  was still writing to the console - the same detached-worker shape already fixed for store
  and provider workers. The drain now waits through `await_worker_convergence()`, absorbing
  repeated cancellation, and re-raises the original `CancelledError` only once the printer is
  done. A printer that raises is dropped as a display bug: it can neither fail a turn that
  succeeded nor mask the caller's own exception or cancellation.
- Stop `InMemoryEventStore` from handing out the very events it stores. `append()` and
  `read()` returned the objects held in the stream, and `EventEnvelope` being frozen only
  prevents rebinding its fields - the nested dicts and lists inside `data` stayed mutable.
  A caller editing its own event therefore rewrote stored history, silently and in the past.
  Both methods now return detached copies; `head()` still copies nothing. `JsonlEventStore`
  needed no change, because its history lives in the file and both directions already pass
  through the shared `EventEnvelope` serialization boundary - which does rebuild the payload,
  so this is "no store-specific detach call", not "no copying".
- Detach the nested references that `EventEnvelope.to_dict()` and `from_dict()` shared with
  their input. `to_dict()` handed out the envelope's own payload, and `from_dict()` rebuilt
  only the top level, so editing either dict reached back into the event. `from_dict()`
  still rejects a non-object payload rather than coercing it.
- Add `detach_event()`, built on the existing `to_json_value()` so that one rule decides both
  what an event payload may contain and how it is normalized. That rule reaches wider than
  `JsonValue`: `Path`, `UUID`, `datetime`, `Enum`, dataclasses, mappings and `Sequence` values
  other than `str`, `bytes` and `bytearray`, such as `tuple`, are converted into their JSON
  form, and only genuinely unsupported values (`set`, `bytes`, arbitrary objects) raise
  `TypeError`. No JSON text round trip is involved, so `UUID`
  and `datetime` metadata on the envelope itself survive intact.
- Add `tests/test_event_store_contract.py`, an ownership contract parametrised over both
  stores: mutating the pending input, the append result or a read result never changes
  stored history, two reads share nothing, `to_dict()`/`from_dict()` detach in both
  directions, and `expected_seq`/`ConcurrencyConflict` semantics are untouched. Both sides of
  the payload rule are pinned too: unsupported values raise, while `Path` and `tuple` are
  normalized rather than rejected. The tests really mutate nested structures and read back,
  rather than asserting non-identity.
- Document the ownership contract on the `EventStore` protocol itself, not only on
  `InMemoryEventStore`, since a replaceable backend must not change what callers may safely
  do with a returned event.
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
