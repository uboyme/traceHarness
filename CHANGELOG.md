# Changelog

## Unreleased

### v0.5 Stage D1 Service Scope foundation

- Added Application, Workspace, Preset and Agent Service layers to the default Runtime and
  every plugin composition candidate. `ScopedServiceBinding` is the explicit assembly input;
  nearest-scope resolution reports the source layer.
- Added stable conflict diagnostics for same-layer duplicates, missing `replace=True` and
  API-major mismatch. Published Scope and Service views are read-only, and `ServiceKey`
  rejects blank names and invalid API major values. Child overrides are revalidated after
  application plugins publish Services, closing the setup-time shadowing gap while preserving
  the structured conflict code and transactional rollback.
- Hardened the override contract so `replace` accepts only a real boolean, Scope assembly
  preflights all four layers before mutating the caller-owned Application Registry, and plugin
  API-major failures retain both their stable code and responsible plugin id. The public
  `PluginManager.prepare_activation_set()` now preserves existing child-scope bindings.
- Preserved the D0 custom ActivationSet contract: Scope/ServiceView is an optional D1
  capability, while implementations that expose it must provide a matching pair.
- Captured the effective Scope and Service view in each Composition Generation and Step
  Lease. A replacement creates a new chain, while an old Lease keeps its old plugin Service
  alive until Generation cleanup.
- Added D1 contract tests and reverse checks for explicit override, read-only publication and
  late plugin-Service revalidation. Plugin setup remains application-scoped; scoped
  Tool/Prompt/Policy composition and other plugin contribution categories are still deferred.

### v0.5 Stage D0 control-plane structure

- Extracted plugin candidate replacement, Session plugin-identity migration, the shared
  admission Gate and in-flight control-plane convergence from `AgentRuntime` into
  `PluginCompositionCoordinator`. `AgentRuntime` remains the public facade, active-Turn
  owner and overall shutdown owner; `AgentLoop` is unchanged.
- Preserved the existing candidate rollback, Session-head CAS, may-have-committed,
  fail-closed and repeated-cancellation semantics. Runtime shutdown still converges active
  Turns, then control-plane work, then Composition Drain and optional legacy cleanup.
  Custom Generation publishers retain the previous contract: the `poisoned` diagnostic is
  optional and absence means not poisoned.
- Added structural and shutdown-order contract tests. This is a behavior-preserving D0
  extraction only; it does not add Scope Overlay, a new command or a new persistent fact.
- Preserved the public migration dispatch seam: `reload_plugin_composition()` continues to
  call the facade's `migrate_session_plugin_composition()` with the facade's current
  `enabled_plugin_ids`, so subclassing or instrumentation is not bypassed by the extraction.

### v0.5 Stage B infrastructure

- Added Generation-owned `PluginActivationSet` and `PluginGenerationBuilder`. Each
  explicit candidate uses private Tool, Prompt and Service registry views; setup,
  dependency/conflict validation and health checks complete before publication.
- Startup plugins and the internal `AgentRuntime.replace_plugin_composition()` path now
  transfer Activation ownership to the Composition Generation. Old Leases keep old
  plugin Service, Tool, Prompt and Owned Task resources alive until reverse cleanup after
  the last Lease; the default Runtime has no duplicate PluginManager cleanup owner.
- Added deterministic candidate rollback, repeated-cancellation convergence, owned-task
  joining, bounded cleanup failure reporting, Session latest durable Composition recovery,
  and real default-mainline replacement tests. Runtime disposal now also converges
  in-flight replacement candidates, and replacement never acts as a process-wide Session
  migration authorization.
- Fixed candidate cancellation so a genuine rollback cleanup failure is not discarded in
  favour of `CancelledError`. Remaining activations still unwind, only bounded structured
  diagnostics escape, and Runtime shutdown records and repeats the failed terminal result.

### v0.5 Stage C control plane

- Added idle `traceh chat` commands `/plugins`, `/plugins reload`, `/plugins use ID [ID ...]`
  and `/plugins use --none`. They use the Stage B Builder, private candidate registries,
  `PluginActivationSet`, Composition Generation publish and Lease/Drain cleanup path; they
  do not create a Turn or model request.
- Added the append-only `composition/migration-authorized` Session event and a shared strict
  plugin-identity projection. Identity changes use `migration_id`, `source_seq`,
  `from_plugins` and `to_plugins`, while same-identity reloads remain event-free. Session
  head CAS, a Runtime-wide idle Gate and per-Session verification prevent an internal
  Generation replacement from becoming an automatic migration.
- Added may-have-committed reconciliation by `migration_id` and fail-closed behavior when
  authorization is durable but publish cannot complete. Candidate rollback and Runtime
  dispose converge repeated cancellation before exposing the original cancellation or a
  bounded cleanup error.
- Hardened Stage C admission and recovery boundaries: `dispose()` and Turn creation now
  linearize on the same lock; migration rejects Sessions whose durable Turn/Step projection
  is still open; resume hints use the Session's latest durable plugin identity even during a
  fail-closed publish window; and unknown Chat commands no longer echo untrusted input.

### v0.5 Stage A infrastructure

- Added Generation-backed Composition Runtime with Step Lease, Publish/Retire, last-Lease
  cleanup, deterministic Drain, repeated-cancellation convergence and structured cleanup
  failures.
- Routed both default runtime factories and real AgentLoop Steps through the same
  Generation/Lease path; request reconstruction and Composition revision fingerprints do
  not include internal Generation identity.
- Runtime shutdown now converges active Turns, Composition Drain and then only optional
  legacy application-level cleanup in that order; Stage B's default ActivationSet cleanup
  is performed by its owning Generation during Drain.
- Added deterministic Generation contract tests and reverse validation of the lease,
  cleanup-order and repeated-cancellation boundaries.
- Corrected the Stage A contracts found in follow-up review: Generation captures immutable
  Prompt/Tool/Provider inputs instead of rereading live registries; Tool execution uses a
  frozen metadata adapter; policy/middleware Snapshot names are captured; completed records
  are removed; publication rejects plugin identity migration; Lease contexts are single-use
  with one shared release result; cleanup failures are bounded and poison future publication;
  and cleanup error type labels are terminal-safe. Service remains an application-level
  PluginManager/AgentRuntime resource rather than a Generation field.
- Follow-up review protections now fix the main `SessionService` identity for the runtime and
  reject candidates bound to another EventStore or the current Generation object itself.
  Replacing an already frozen Generation reuses one flat Tool adapter, so repeated candidate
  publication cannot build an unbounded execution recursion chain. The chat cancellation
  fixture now waits for the first Provider call to receive cancellation before releasing its
  gate; the full suite is deterministic and green.
- Final ownership hardening makes Tool metadata properties and nested schemas truly read-only.
  Each Generation object is claimed at most once across all Runtimes; retired or cleaned
  Generations cannot be republished, and candidates derived from cleanup-bearing published
  Generations are rejected instead of sharing resources with a cleanup owner.
- Follow-up ownership hardening now separates Generation-object publication claims from
  cleanup ownership. Cleanup-bearing Generations carry one explicit, one-shot
  `CompositionResourceOwner` handle created by the assembly layer; raw capability
  components and frozen/compatibility wrappers propagate that binding directly. There is
  no global `id()` catalog or object-graph scan. Bare cleanup callbacks, used bindings,
  multi-hop cleanup derivations, wrapper aliases and a second owner claim are rejected by
  the shared Runtime-construction/`publish()` validation path. The Tool contract also
  tests deletion refusal for all frozen metadata fields.
- Resource ownership is now conservative for slotted raw capabilities: a Provider, Tool,
  Policy or Middleware that cannot retain the binding marker is rejected from a
  cleanup-bearing Generation instead of being treated as verifiably fresh. Generation
  construction freezes and validates all sources before committing owner/binding state, so
  a bad Provider name leaves the same Owner and raw resources retryable.
- Ownership binding now writes directly to verifiable instance-dictionary or declared-slot
  storage, defeating custom setters that silently ignore the marker. Transaction rollback
  restores the exact prior attribute state, and Runtime compatibility views are built from
  the frozen initial Generation before the one-shot owner claim, closing the remaining
  post-claim failure window.

This still does not add running Wheel install/uninstall, forced Python module reload, file
watching, scoped plugin Tool/Prompt/Policy composition, new plugin contribution categories,
isolated plugins, multi-agent,
Workflow, MCP, TUI or streaming output. The package version remains `0.4.0`; Stage A, Stage B
and Stage C are not the v0.5 release.

## 0.4.0

### Fixes from third-party review

Five blocking defects found by an independent Codex review of the v0.4 work, all
reproduced before being fixed and reverse-verified afterwards (reverting each fix
turns its tests red).

- **Owned background task exceptions had no owner.** A task that raised *before*
  shutdown completed on its own, and the done callback only dropped it from the
  set - so `cancel_and_wait()` never saw it and never retrieved its exception.
  asyncio then reported "Task exception was never retrieved" from the garbage
  collector, at an unrelated moment. The callback now retrieves the outcome the
  instant the task finishes via `task.exception()`, and **retains nothing**: an
  earlier revision stored every failure in an unbounded list no mainline code
  read, keeping untrusted plugin exceptions and their tracebacks alive for
  nobody. Scope is stated explicitly: this is **lifecycle ownership, not a
  supervisor** - v0.4 does not restart tasks, keep exceptions, log anything, or
  escalate a background failure into a Runtime fault. Cancelled and successfully
  completed tasks are not misreported.
- **`AgentRuntime.dispose()` could strand plugins permanently.** With the
  shutdown body inline, a caller cancelled while active turns were still
  converging escaped *before* reaching `PluginManager.dispose()` - and because
  the disposed flag was already set, every later `dispose()` returned
  immediately, so the plugins were never unloaded and nothing reported it. The
  whole shutdown now lives in one reusable internal task, awaited through
  `shield` + `await_worker_convergence`: repeat cancellation cannot release the
  caller early, turns still converge before plugins unload, the original
  `CancelledError` is re-raised only after convergence, and a repeated
  `dispose()` reuses the same outcome - including re-raising a failed shutdown
  rather than silently reporting success.
- **PEP 440 equivalent versions were rejected.** The session plugin identity
  check normalised with `str(Version(...))` and compared strings, which does not
  do what it looks like: `str(Version("1.0"))` is `"1.0"` and
  `str(Version("1.0.0"))` is `"1.0.0"`. Two equivalent versions therefore read as
  a composition change and the session was refused. Comparison now uses `Version`
  objects. `1.0` vs `1.0.1` is still correctly refused, and unparseable versions
  are still rejected as malformed.
- **The reserved `traceh_plugins` metadata key was only conditionally reserved.**
  It was rejected only when the supplied value differed from the expected one, so
  `[]`, `None` and an exactly matching list all slipped through. It is now
  rejected on presence alone - the key records what the runtime itself observed,
  and no caller-supplied value can stand for that. All other caller metadata is
  still stored unchanged.
- **`traceh run` could leak a runtime.** `create_session` sat outside the
  `try/finally`, so a failure there returned without ever calling
  `runtime.dispose()` - leaking activated plugins and their owned tasks along
  with it. It is now inside the guard. Normal output, ordering and exit codes are
  unchanged.

Test suite grows from 836 to 910 (909 passing, 1 skipped by platform).

### Second-review refinements

Follow-up review confirmed four of the five fixes but required tightening:

- **The run-dispose CLI tests could still read the developer's real `.env`.**
  Clearing `TRACEH_*` variables was not enough: `--env-file` defaults to the
  relative path `.env`, so running from the repository root put the real file
  back in reach. The tests now `chdir` into the pytest tmp directory (so the
  default relative path finds nothing), `drive_run` pins a deliberately absent
  test-owned env-file path and asserts `EnvLoadReport.loaded is False`, and five
  new tests prove the isolation holds *without* any `_runtime` monkeypatch - the
  real factory builds a scripted provider, and an explicit env file outside the
  repository still works. The real file is never read or printed; removing the
  `chdir` turns the isolation tests red with the repository's `openai-compatible`
  value.
- **`OwnedTaskSet.failures` was an unconsumed, unbounded exception list.**
  Removed. The done callback still calls `task.exception()` to take ownership and
  prevent "Task exception was never retrieved", then discards the object; nothing
  is retained, and the owner's state does not grow with failures. No logging,
  restart, reporting or failure escalation was added - real observability needs a
  mainline consumer first, and would need bounded, structured, redacted records.
- **`verify_session_plugins` collapsed "key absent" and "key is null".**
  `dict.get()` answers `None` for both, so an explicitly recorded `null` was
  treated as a pre-v0.4 plugin-free session. The reader now passes a dedicated
  missing-sentinel as the `get()` default: a genuinely absent key is the v0.3
  case and continues normally, an explicit `null` is malformed data and is
  rejected - covered through real sessions written via `SessionService`
  underneath the runtime.
- **The active-turn dispose test reached its defect window by counting
  scheduler iterations.** It now uses a deterministic cancellation latch: the
  provider lights `cancellation_entered` when shutdown's cancellation arrives,
  then stays parked absorbing a second and third cancel. The test waits on the
  latch before cancelling `dispose()`, and asserts dispose is unfinished and
  plugin cleanup has not run until the test releases the latch. The only sleeps
  left are single `sleep(0)` calls delivering an already-requested cancellation;
  none is evidence of reaching the window.

### Plugin system

- Add `traceh.plugins`: entry-point discovery for the `traceh.plugins` group, an explicit
  enablement rule, and a transactional `PluginManager`. An externally built wheel can now
  add a tool and a prompt section without editing this repository or `AgentLoop`. The
  manager lives in the runtime assembly layer; plugin tools, prompt sections and services
  join the *existing* registries, so there is no separate plugin tool runtime or agent loop.
  See [`docs/plugins.md`](docs/plugins.md) and
  [ADR-0007](docs/adr/0007-transactional-plugin-activation.md).
- Discovery is metadata-only and never imports a plugin, so `traceh plugins list` is not
  itself a code-execution step. Only explicitly enabled plugins are imported.
- Installing never enables. `--plugin` (repeatable) and `TRACEH_PLUGINS` select plugins;
  any `--plugin` occurrence replaces the environment value entirely rather than adding to
  it, and `run`, `chat` and `resume` share the one rule.
- Activation is a four-phase transaction: staged setup against private registries, a full
  conflict check, health checks, then atomic publish. Conflicts are checked **before**
  health checks - a plugin already known to collide with a built-in is rejected regardless
  of what its health check would say, so running it first only gives known-doomed
  third-party code another chance to execute or reach the network.
- Any failure unwinds every activation in reverse order, converging owned tasks and
  cleanups. `Runtime.dispose()` converges active turns first, then unloads plugins in
  reverse activation order.
- Add `traceh plugins list/inspect/doctor`, all with `--json`. `list` and `inspect` import
  nothing, create no session and call no model; `doctor` sets up and health-checks against
  throwaway registries and disposes immediately. Every rendered string is escaped to one
  printable line, because it all comes from third-party metadata.
- Composition Snapshots record real plugin ids and versions, and replay rebuilds them -
  previously `composition_from_event` dropped them, so every reconstructed composition
  claimed a plugin-free runtime.
- Sessions record the external plugin identities they were created under. Continuing a
  session under a different set raises `SessionPluginMismatchError` instead of silently
  running with different tools. Sessions written before v0.4 have no such key, which reads
  as "no plugins" and continues normally.
- Add `examples/plugins/traceh-example-skill-plugin`, a real separately built distribution
  contributing one prompt section and one read-only tool. It reads no user directory, no
  environment variable and no network resource, and is never enabled by installation alone.

### Cancellation during activation is cancellation, not failure

Interrupting startup previously would have been reported as a broken plugin configuration:
`CancelledError` was caught by a bare `except BaseException` and re-raised as
`PluginActivationError`/`plugin-setup-failed`, and a repeated interrupt could cut rollback
short and strand activations that had not been reached. Cancellation now unwinds every
activation in reverse order, converges owned tasks and cleanups, and re-raises the original
`CancelledError`; repeat cancellation is absorbed rather than treated as an escape hatch,
reusing the project's existing `await_worker_convergence` rule.

### Version contract

- Bump to `0.4.0` and give the version a **single source**, `traceh.version.__version__`.
  `pyproject.toml` reads it via `[tool.setuptools.dynamic]`, so the wheel metadata and the
  imported package cannot drift. `traceh.core`'s plugin identity, the plugin API version,
  the default manifest compatibility range, the Composition Snapshot and the CLI banner all
  derive from it, and a test asserts the installed distribution agrees.
- This closes a real divergence: a runtime built without plugins and a runtime built
  through `PluginManager` would otherwise stamp different `traceh.core` versions into the
  same kind of Composition Snapshot.

### Runtime dependency

- **`packaging` is now a runtime dependency** - the project's first. Manifest ranges,
  plugin dependency specifiers and distribution requirements are all PEP 440, and a
  hand-rolled parser guarding a trust boundary would be an incomplete PEP 440
  implementation. The previous "standard library only at runtime" statement no longer holds
  and has been corrected wherever it appeared.

### Other

- `ToolRegistry.register()` and `PromptAssembler.register()` now return a reversing
  `CallbackRegistration`. Core assembly ignores it; plugin activation owns it.
- `PromptSection` moved to `traceh.api.prompts` so the plugin SDK need not import the
  runtime assembly layer. `traceh.runtime.prompt` re-exports it.
- Add `build_default_runtime_async()`. With no plugins enabled it is exactly
  `build_default_runtime()`: no discovery, no imports, an identical runtime.
- Fix a dropped import that left `traceh recover/inspect/replay/compact/sessions`
  unrunnable, and add the tests that path was missing.
- Test suite grows from 583 to 836 for the plugin system itself, and to 910 with the
  review fixes above (909 passing, 1 skipped on paths that cannot carry NUL). The new
  tests include a `slow`-marked acceptance that builds both wheels, populates an offline
  wheelhouse containing `packaging`, installs into a fresh virtual environment with
  `--no-index`, and drives the whole mainline through the real entry point.

## Unreleased

- Treat `U+2028` and `U+2029` as line breaks everywhere a value reaches one terminal line.
  The shell renderer, `escape_for_display`, the base URL check and the timeline each tested
  the Unicode `C*` categories, so all four missed the explicit line and paragraph separators
  (categories `Zl`/`Zp`): `escape_for_display("x\u2028note: forged").splitlines()` still
  returned two lines, and `is_renderable` returned True. The rule now lives in one place,
  `cli/text_safety.py`, which every caller reads. The test helpers were the reason this
  passed unnoticed - they checked only `\n`/`\r` and the `C*` categories, and now assert with
  `splitlines()` plus explicit separator checks.
- Stop reporting the rejected value when an environment variable name is invalid. Escaping
  defends against control characters, not against a printable secret, and the usual way this
  setting is got wrong is by pasting the key itself where the variable *name* belongs - so
  the invalid value is precisely what must not be echoed. Nothing derived from it is shown
  either: no length, prefix, suffix or hash. The `.env` parser likewise reports only the line
  number for a malformed name. The check validates shape, not intent: a key that happens to
  be a valid identifier is accepted, and that boundary is documented and pinned by a test.
- Decide the verifier's provenance from which value actually won, not from the env file
  merely containing the key. Configuration resolves explicit `--verify-command` over an
  existing environment variable over the file, so a file holding `TRACEH_VERIFY_COMMAND`
  alongside an explicit flag would have restored a different verifier than the one running.
  Argument resolution now records a boolean and passes only that; the verifier text has no
  field to sit in on `ResumeEnvironment`, keeping it out of the repr, the command and any log.
- Stop the base-URL credential check from crashing the chat. `urlparse` raises on inputs such
  as `https://[bad`, and only when the netloc is inspected for userinfo - exactly what the
  check does. Parsing and the userinfo access are now guarded, and an unparseable URL is
  withheld with a reason rather than echoed or surfaced as a traceback.
- Escape every value the unrenderable-command fallback shows. Printing them raw meant a data
  dir containing a newline produced a second terminal line inside the block explaining that
  no command could be shown safely; control and format characters now appear as their visible
  escape spelling, and the user still sees escaped locating information and why.
- Reject an unusable `--api-key-env` / `TRACEH_API_KEY_ENV` before any runtime or session is
  built, instead of accepting it and quietly dropping it from the resume command - which let
  the next run fall back to a different variable. The rule is shared with the `.env` parser
  and does not vary by provider: a scripted run ignoring the key does not make an unlookupable
  name valid. The rejected value is escaped in the message.
- Build the resume command as argv tokens and render it for one named shell, instead of
  quoting only values containing a space. PowerShell treats `&`, `;`, `|`, `$(...)` and the
  backtick as syntax outside quotes, so an unquoted path or model name could end the command
  and start another. PowerShell now gets single-quoted literals with its own doubling rule,
  POSIX gets `shlex`, and the block says which shell it is for. Program and flag names stay
  bare deliberately: PowerShell parses a quoted leading string as an expression, so
  `'traceh' 'chat'` would print a word rather than run anything. Values carrying control
  characters are refused rather than escaped, since a newline would produce a second command.
- Stop echoing values whose safety cannot be shown. `--verify-command` is arbitrary shell
  text that cannot be displayed and also promised credential-free, so it is omitted - restored
  by the env file when that supplied it, and otherwise reported as
  "Verifier command omitted from the displayed resume command; re-supply it manually." A base
  URL is withheld when `urllib.parse` finds userinfo, a query or a fragment. That is a
  structural rule, not a secret detector, and the documentation now states the specific
  verifiable rules instead of promising that secrets are never printed.
- Carry `--script` with its resolved absolute path, since omitting it silently substituted the
  built-in placeholder provider, and say plainly that the scripted response cursor is not
  persisted across processes - reloading the same file restarts from its first response.
- Stop advertising an API key variable to a scripted session, where naming `OPENAI_API_KEY`
  is a misleading instruction. It is shown only for a provider that sends one, only when the
  name is a valid environment variable name, and the wording distinguishes "set it in that
  shell" from "available from that env file or the shell" depending on what actually supplies
  it. The key's value is never read.
- Separate locating a session from restoring its behaviour throughout the docs, and remove the
  bare `traceh chat --session-id <id>` form from the README, which omitted the data dir and
  contradicted the full resume block further down.
- Schedule each waiting notice from the activity's own deadline instead of a fixed tick.
  Sleeping one interval at a time phase-locked the heartbeat to whenever the turn started
  rather than to the work: with a 10s interval and a tool starting at t=10.1, the t=20 wake
  saw only 9.9s and stayed silent, so the first notice landed at t=30 - nearly twenty seconds
  into the wait this feature exists to cover. The test clock now records and honours
  deadlines too; releasing every sleeper on any advance made a 0.1s wait and a 10s wait
  indistinguishable, which is how the phase bug passed a suite that looked thorough.
- Carry the non-secret configuration in the resume command. Provider and model may come from
  a `.env` in the original working directory, so a command holding only a session id and data
  dir re-resolved them wherever it ran: a session started on `custom-model` silently continued
  on the default. `--provider`, `--model`, `--max-steps` and `--verify-command` are now
  named explicitly, with the resolved absolute `--env-file` when one was actually loaded.
  Secrets are still never printed - only the *name* of the API key variable, plus a note that
  the new shell needs it set, since that is the one part a command cannot carry.
- Say only what the events prove about a running tool. `ToolRuntime` gathers a parallel-safe
  group and appends every `tool/result` once the whole group finishes, so a tool that has
  already returned is indistinguishable from one still executing. The notice now reads
  "has not reported completion" rather than "is still running", and a tool's reported duration
  is documented as `tool/admitted` to the *persisted* `tool/result` - longer than its own
  execution for a tool inside a group. A model attempt, whose end is appended as soon as the
  provider returns, still reads "is still working".
- Reprint the resume command when leaving through `/exit`, `/quit` or EOF, which the
  documentation already claimed and the code did not do.
- Record that a slow `CommandVerifier` is still silent: it has no start event - the protocol
  carries only `verification/result`, appended after the command finishes - so the heartbeat
  cannot cover it. Guessing from the absence of tool calls was rejected rather than shipped;
  adding a `verification/start` event is a protocol change left to its own design.
- Report waiting time while a model call or tool runs, so a slow provider stops looking like
  a hang. A timeline built only from events is silent exactly between
  `model/attempt-start` and `model/attempt-end`, which is the interval users worry about.
  `cli/activity.py` fills that silence from the events it already sees, keyed by `attempt_id`
  and `tool_call_id` so concurrent read tools are each tracked instead of one overwriting the
  rest, and completion lines gain a measured duration. Configured with
  `--heartbeat-seconds` (default 10; `0` disables it and keeps the timeline; `--no-timeline`
  disables both; negative, NaN and infinity are configuration errors).
- Keep the heartbeat out of the record: no event type, no append, no participation in
  recovery, replay, the surface or request fingerprints, and a `[waiting …]` prefix rather
  than `[event N]`, which stays reserved for a real persisted seq. Elapsed time comes from a
  monotonic clock - a wall clock would jump or reverse if the system time were adjusted
  mid-turn - and the clock is injectable so tests advance 10 seconds instead of waiting them.
- Show the cancellation lifecycle when a turn is interrupted. `_run_turn()` used to close the
  timeline subscription *before* `runtime.cancel()` appended `runtime/cancel-requested`, the
  cancelled model attempt, `step/end` and `turn/end`, so all of it was published to nobody and
  the output simply stopped. The subscription now stays open across convergence and is drained
  afterwards, so those events reach the console ahead of the notice.
- Make the first Ctrl+C cancel only the running turn. The session stays open, no new session
  is created, nothing is injected, and the next turn is the one the user types; an idle Ctrl+C
  at the prompt is what leaves, returning 130. Repeated interrupts cannot shorten convergence
  - it is delegated to the shared `await_worker_convergence()` - and are honoured only once
  the model, tools and subprocesses are done. A hard interrupt still runs no Python at all,
  and that boundary is documented rather than papered over.
- Print the resume command at startup instead of only on exit, since an exit-time hint cannot
  survive a hard interrupt. It carries the resolved absolute `--data-dir`, because a session
  id alone cannot locate a session whose store lives under a different data dir, and quotes
  the path so a directory with spaces survives a copy-paste. A custom `--env-file` is
  deliberately not reconstructed: guessing a path that may have moved would print a command
  that silently does something else.
- Explain the event numbers once at startup rather than renumbering them. `seq` 1-3 are
  `session/created`, `inbox/accepted` and `inbox/claimed` - persisted but not displayed - so
  the first visible line is normally `[event 4]`. Renumbering to 1 would destroy the only
  property that makes the number useful: being the seq you can look up in the JSONL. The note
  never begins with a bracket, so it cannot be mistaken for a timeline row.
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
