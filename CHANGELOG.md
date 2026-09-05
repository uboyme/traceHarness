# Changelog

## 0.8.0 - 2026-09-05

### M4: Context transparency in the optional Textual TUI

- Added a full-width Context status row under the topbar and a read-only
  `Ctrl+X` detail screen. Both answer "how much can the model see, how close is
  it to being compacted, and what was actually frozen into the last request".
- Added `traceh.tui.context_inspection`, a stateless read-only projector. It
  re-reads the requester Session once and derives everything with the existing
  owners' parsers (`surface_conversation`, `parse_surface_replacement`,
  `latest_product_context`, `ModelRequest.from_dict`,
  `dispatch_request_matches_composed`, `CoreInvariantChecker`). No durable
  event, fact source, cache, index or state machine is added, and no model
  message, prompt, fingerprint or ProductTask behaviour changes.
- **Bytes are shown as bytes.** With no trusted general tokenizer and no
  canonical per-model input ceiling, a context-window percentage cannot be
  computed honestly, so none is shown. The only denominator used is the
  configured compaction trigger, and only while compaction is enabled; the
  detail page labels it explicitly as a share of that threshold.
- The current projection and the latest frozen request are reported separately.
  The frozen request is read from `request/snapshot` rather than recomputed, and
  the Product context it carried is selected within its own `source_seq`, so a
  newer ProductTask head can never rewrite what an older request contained.
- Durable compaction count and currently visible summaries are reported as two
  different numbers, and a historical replacement is only described as matching
  the current policy when its stored `policy_digest` actually matches.
- Read failures, malformed replacements and malformed request snapshots fail
  closed with a stable code; the Context view never blocks a Turn, a ProductTask
  or shutdown.
- The status row is fitted to the cells it actually has rather than left to
  Textual's clipping: candidate renderings are measured from richest to
  shortest and the first that fits is used. In the failure state the stable
  code is the payload, so it is the last thing dropped.
- The detail screen wraps to the viewport. `RichLog` lays out at
  `max(min_width, viewport)` and defaults `min_width` to 78, so a narrow
  terminal previously overflowed horizontally with no Footer key to scroll it.
- Fixed two real defects found while building this: naming the display snapshot
  `self._context` shadowed `textual.app.App._context`, which stopped the app
  from ever starting and hung every TUI test instead of raising; and passing an
  explicit `width=` to `RichLog.write` pinned the virtual line width and
  disabled `wrap=True`.

### M3: host-owned automatic Surface compaction and exact replay

- **Breaking (pre-1.0): `surface/replace` uses format 2 only.** Format 1 is
  rejected explicitly, with no second parser, migration, alias, dual projector,
  fallback or silent rewrite; older data requires a new data directory. The new
  exact key set binds the source sequences, a content digest, the cut boundary,
  the method, the compaction policy digest, the summarizer identity and the
  exact replacement bytes, and the parser requires canonical equality before a
  replacement may reach the Surface. `session/surface_replacement.py` is the one
  place that protocol is defined; the projector, the compaction service and the
  invariant checker all read it.
- **Breaking: a replacement is projected at the logical position of the history
  it replaced**, not at its own append sequence. A summary of older turns can no
  longer appear behind newer conversation, including behind the current user
  message. Repeated compaction converges into one summary instead of stacking.
- **Breaking: `traceh compact --through-seq` must now be exactly the sequence of
  a closed Turn's `turn/end`.** A sequence inside a Turn, or past the end of the
  log, is refused with exit code 3 and one stable code rather than silently
  sliding back to an earlier Turn and compacting a range the caller did not ask
  for.
- Added host-owned automatic compaction. `AgentLoop` consults the single
  `CompactionService` once before a Turn opens - the only point with one Turn
  owner, no open Turn and a replacement still preceding every request the Turn
  will freeze. A failed compaction leaves history untouched, records a
  `surface/compaction-failed` notice carrying only a stable code, and lets the
  Turn proceed.
- The trigger is `history_utf8_bytes`, the canonical UTF-8 size of model-visible
  conversation. It is a byte count, not a token count: this runtime has no
  trusted general tokenizer. Enabling compaction requires stating all four
  values explicitly (`--auto-compact on|off`, `--auto-compact-bytes`,
  `--auto-compact-summary-bytes`, `--auto-compact-keep-turns`, or their
  `TRACEH_AUTO_COMPACT*` variables); a partial configuration is refused before
  any Runtime or Session exists, and an absent configuration means off.
- `product/context-snapshot` remains impossible to compact, now enforced by the
  invariant checker as well as by selection (ADR-0039/0041 unchanged).
- Summaries are untrusted history: scrubbed, byte-bounded with an explicit
  truncation flag, and embedded as a JSON string, so a summary cannot forge a
  header, a closing tag or a second message. The `<compacted-summary>` XML
  wrapper is gone.
- **No model-backed summarizer ships, deliberately.** The only auditable,
  budgeted and cancellable model dispatch is the Session dispatch permit, and
  every `request/snapshot` must reconstruct as the Surface projection - which a
  summarization request is not. The default `BoundedHistorySummarizer` is
  deterministic and model-free; a host may inject its own, under the same rule.
  A `SummaryRequest` carries no Store, Session service, Tool registry, Runtime
  or approval handle. See
  [ADR-0042](docs/adr/0042-host-owned-automatic-surface-compaction.md).
- Selection, summarizing and the append are one compare-and-swap: the head
  observed during selection is the append's `expected_seq`, a conflict re-reads
  and re-selects instead of resubmitting a stale payload, cancellation converges
  the owned append worker and reconciles a may-have-committed write, and unknown
  commit status stays unknown.
- `CoreInvariantChecker` recomputes a replacement's `source_seqs`,
  `source_digest`, `source_utf8_bytes` and `history_utf8_bytes` from the history
  preceding it and requires exact equality, so a canonical-looking event cannot
  bind fabricated derived facts or hide part of a prefix. `source_seqs` is
  ascending by sequence while selection is by logical position; the two orders
  differ once an earlier summary is appended after messages a wider cut covers.
- Line and the Textual adapter render the same durable event and keep no
  compaction state: counts and provenance only, never the summary body, the
  replaced messages, the digests or a prompt. The failure notice reports all
  three commit answers separately - only an exact `false` is shown as "history
  unchanged", and an unknown commit stays unknown.
- The printed resume command carries `--auto-compact` and its three thresholds
  when a policy is enabled, so copying it cannot silently disable compaction.

### v0.8-F5: v0.8.0 release candidate integration

- Advanced the single package/core/plugin-API version source to `0.8.0` without
  changing any durable protocol version. Distribution metadata, package export,
  `traceh.core` identity, CLI banner and source-archive naming remain derived
  from `traceh.version.__version__`.
- Advanced the independent Plugin Creator and Python Quality example
  distributions to `0.2.2`. Their Distribution dependencies and runtime
  Manifests now explicitly admit the tested v0.8 SDK while retaining their
  earlier lower bounds; the Creator authoring template generates the current
  `traceharness-py>=0.8,<0.9` candidate contract. Core discovery and validation
  rules were not relaxed.
- Updated the public plugin-author contract for v0.8 and configured all three
  CI matrix jobs to install the optional `tui` extra so Textual Pilot coverage is
  not silently skipped. The release gate still proves core-only and `[tui]`
  installs separately from clean offline inputs.
- Completed the frozen F5 pre-review gates and global independent review. The
  review found no P0/P1; its two non-blocking stale-context P2s were corrected
  in the formal and plain-language project contexts. That candidate passed a
  full-pytest gate (`2496 passed, 7 skipped`, exit 0), but the TUI replacement
  below materially changed the candidate afterward, so the historical result
  no longer counts as the current final gate. Renewed independent review and
  the replacement-era full run are recorded below; clean-input Wheel/sdist/
  source ZIP audit, offline installs, the separately authorized real-Provider
  grid, commit, tag and release remain pending and are not claimed by this
  entry.
- Replaced the first F4 TUI presentation in place, with no legacy layout or
  compatibility switch. The one current adapter separates transient host work,
  durable Product/Workflow/Session facts and model self-report; renders only
  legal typed-confirmation gates; reports operation and per-stream ages; keeps
  reconciliation explicitly write-labelled; offers compact narrow-terminal and
  full-identity views; and shows owner-by-owner shutdown convergence.
- Completed the same TUI's missing output-visibility surface without adding a
  second adapter or fact source. `Ctrl+T` now opens a fresh, read-only snapshot
  grouped by exact Router and fixed Workflow role Sessions. Router ownership is
  checked through Agent Directory; fixed roles retain their deterministic
  Agent/Session/create-request binding. Every Session passes
  `CoreInvariantChecker` before one canonical-sequence pass combines user/model
  speech with paired Tool call/result rows. Tool rows expose only bounded safe
  labels, statuses and exact sequence ranges; unknown Usage is unavailable
  rather than zero, and shell arguments, Tool-result bodies and raw payloads
  stay hidden. The projection scans no streams, caches nothing and writes no
  facts.
- Closed the post-approval feedback gap found during hands-on acceptance. After
  approve, reject, cancel or abandon returns and the existing observation is
  freshly read, the left conversation now renders the typed operation and
  durable Product status as a host notice. The notice is process-local UI only:
  it is not appended to the Chat Session, SQLite or any model request.
- Closed the separate requester-context gap exposed by the next Chat Turn.
  Before a Product-configured requester model is dispatched, the host now
  fresh-selects the canonical ProductTask head and freezes one exact, bounded
  `product/context-snapshot` in that requester Session. The event records the
  exact model-visible messages plus replay/identity metadata, so request
  snapshots remain replayable; ProductTask,
  Workflow and Promotion streams remain the only control authorities. The
  superseded format-5 message contains task/head/status plus fixed same-requester-
  Session, host-managed execution-owner, per-status meaning, selection scope,
  Workspace/evidence limits and non-authorization semantics. A `started`
  ProductTask is not presented as an
  independently resolved Workflow start, and `completed` is presented as a
  durable Product terminal carrying a Promotion reference, not as a newly
  resolved or exposed Promotion receipt. The next request explicitly says not
  to treat that terminal as waiting for START; deterministic tests prove this
  request contract, not external-model compliance. Review, Promotion, Patch,
  revision, path, verifier and failure identities/details remain excluded;
  the requirement digest stays in the replayable receipt identity but is not
  rendered into the provider-visible messages. Those messages no longer carry
  the old XML wrapper; they explicitly permit natural summaries and reasonable inferences while
  requiring the model to distinguish host facts from inference. After a later
  frozen request proved that stale assistant prose could still conflict with
  the current completed evidence, the Surface now places the one canonical
  current-state instruction before the complete, otherwise unchanged
  conversation and states that earlier status claims cannot override it. This
  is request conflict handling, not response filtering or a new fact source;
  external-model compliance remains best effort. The earlier status-only
  format 1, cross-owner format 2, underspecified XML format 3 and missing-
  precedence format 4 are rejected without migration or fallback. The version
  participates in the context identity, preventing a changed canonical message from aliasing an old
  durable event. Deterministic coverage includes restart idempotence, new-task
  supersession, stale append order, strict bool/int protocol rejection, CAS,
  read failure, may-have-committed cancellation and a real local-Git next-request
  E2E. At that format-5 checkpoint, the renewed full gate was still pending;
  the later format-7 M2 result is recorded below.
- Extended that host-owned requester context in place to format 6 so later
  turns retain a bounded memory of ProductTasks from the same requester
  Session. One atomic `product/context-snapshot` now carries the current focus,
  up to five additional recent tasks, exact total/omitted counts and two
  ordered model messages: authoritative current facts as `system`, followed by
  a clearly historical `user` reference. Source-request excerpts are copied
  from validated Session evidence, JSON-escaped and bounded; they are not
  canonical requirements, current instructions or control authorization.
  ProductTask streams and Session events remain the only durable authorities;
  no Memory stream, cache, RAG, cross-Session recall or model-authored summary
  was added. Format 1 through 5 are rejected without migration or fallback.
- Advanced the same Session-scoped projection to format 7 and completed M2's
  progressive-disclosure path. The default context keeps the atomic six-task
  catalog and adds only a verified execution summary for a current focus in
  `awaiting_approval`, `completed`, `rejected`, `cancelled` or `failed`:
  Workflow status, managed Tool-call count, changed-path count, verification
  result, verifier count and whether Promotion is recorded. Historical tasks,
  `opened`/`routed`/`started` and `abandoned` do not receive an execution
  summary. Detailed paths, bounded Tool outcomes, verifier results, Review and
  Promotion identities remain out of the default request.
- Added the `PURE_READ` requester Tool `read_product_task_evidence`. It accepts
  exactly one ProductTask id already related to the calling requester Session,
  freshly proves both Product origin and confirmation against that Session,
  and returns bounded Product, Workflow/node, role/Tool, Review, approval,
  Promotion and Budget usage evidence. Missing, foreign, corrupt and unreadable
  tasks deliberately share one `product-task-evidence-unavailable` result.
  Raw Patch content, Tool arguments/output, model prose and Product workspace
  paths are never exposed, and the Tool grants no lifecycle or approval power.
  Its ordinary Session Tool call/result events use the existing Runtime path;
  the evidence reader itself writes no Product or control facts.
- Kept the shared Product activity projection aligned with every durable Tool
  Runtime/Recovery result: `succeeded`, `failed`, `cancelled`, `invalid`,
  `denied`, `aborted_before_dispatch` and `unknown_after_crash`. Unknown durable
  values still fail closed; `pending` remains only the projection of an
  unpaired in-flight call.
- The CLI constructs two separate stateless `ProductReadModels` bundles: one
  before Runtime construction for the frozen requester Tool surface, and one
  over the Runtime's publishing Store wrapper for host context/observation.
  They are not the same Python instances. The CLI supplies both constructions
  with the same underlying durable log and the same Profile/CAS/
  VerificationPlan/target/report-bound inputs, so the production composition
  guarantees those properties by construction. The host does not compare the
  two bundle instances; it validates only the second bundle against the host
  and rejects an internally mixed reader chain. All projections remain fresh
  joins over existing EventStore streams, with no Memory stream, cache, RAG or
  second fact source.
  Formats 1 through 6 are rejected without migration, fallback or dual reader.
- Made `Ctrl+P` fresh-read the current Product observation before showing full
  identities and explicit copy actions; clipboard failure exports only the
  selected value to a named temporary text file and reports its path. The default
  Review block now shows exact full-Artifact-derived per-file summaries instead
  of a bounded diff preview. `Ctrl+D` fresh-validates the Review-to-CAS identity
  chain and opens a full-width, line-numbered change view with file navigation,
  terminal-safe manual wrapping and byte-exact `Ctrl+E` export. `Ctrl+I` and
  `Ctrl+R` remain unadvertised and unimplemented; exact reconciliation remains
  available through the existing Line `/task inspect` path.
- Recorded the earlier output-visibility and host-feedback checkpoint with `54`
  focused TUI/optional tests, `215` adjacent Product/Chat tests and `2575`
  collected tests. A separate short review before the host-feedback repair
  found no P0/P1/P2; removing the Router Agent-to-Session binding
  makes the tampered unrelated-Session counter-example fail with `DID NOT
  RAISE`, and restoring the binding returns it to green. The earlier
  `2555 passed, 7 skipped` full run remains historical. The host-feedback counter-example also
  fails precisely when its single render call is absent while the right pane is
  already completed; restoring it returns the test to green without adding a
  Session event. This checkpoint predates the requester-context bridge and is
  no longer current release evidence. The first status-only format-1 bridge
  checkpoint had `30` protocol/Surface tests, `15` context/Compaction tests,
  `24` Product F3 E2E tests and `2573` collected tests; those numbers and its
  review conclusion are historical. The format-3 checkpoint's `51` Product
  context/Compaction/F3 E2E tests and `2590` collected tests are historical too.
  The format-4 checkpoint's `52` Product context/Compaction/F3 E2E tests, `33`
  Surface/Core-invariant/Product-architecture tests and `2591` collected tests
  are now historical as well. Superseded format-5 and format-6 validation is
  recorded in the validation document. M2's current format-7 candidate passes
  the 172-test direct/adjacent Product group, the 82-test complete TUI group,
  the 26-test Product F3 E2E module and 2665-test collection. A repository-
  external, 33-file exact candidate commit passed the real L2 gate, then the
  unfiltered final suite completed with `2658 passed, 7 skipped` (exit 0) in
  `3226.49s`. Independent code and corrected-document review ended at
  `P0=0 / P1=0 / P2=0`. Release asset, offline-install, real-Provider, commit,
  push, tag and release gates remain unrun.
- Serialized Product observation refreshes so a slower old read cannot overwrite
  newer facts. A real deterministic TUI path using the actual Product host, auto
  Router, fixed multi topology, managed local Git, Verifier and Review reached
  the Approval barrier and caught a stale transient Proposal suppressing the
  valid Approval gate. Counter-examples also reproduce missing START feedback,
  out-of-order observation overwrite and the former Textual `_closing` collision.
- Fixed the renewed TUI review findings without adding another UI or control
  path. A START caller that is still active no longer hides typed Cancel after
  durable Product/Workflow facts prove RUNNING; the App first converges that
  caller and then invokes the existing Product cancel command. Initial
  observation failures are shown honestly and retried on the bounded refresh
  cadence, while a successful fresh read clears only the observation error.
  Task age ignores global-stream events that cannot be bound to the current
  task, and details visibility now stays synchronized with narrow-layout
  expansion across toggles and resize.
- Added deterministic coverage for a real Product host reaching durable
  CANCELLED from an in-flight START, initial observation recovery, stale-error
  clearing, cross-task age isolation and bidirectional details layout. Six
  reverse checks each reproduced its root defect before restoring the
  protection. The focused replacement group was 38 passed and the short
  independent re-review cleared those findings; the later Product Chat
  authority correction below reopened only its focused review and the
  current-candidate full gate at that checkpoint.
- Closed a real pre-START authority leak found during TUI use. A
  Product-configured requester Chat no longer inherits `apply_patch` or `shell`:
  its complete core surface is workspace reads, Product proposal/confirmation
  and the same-Session `PURE_READ` ProductTask evidence Tool above; a monotonic
  policy denies any registered effectful Tool by declared `EffectKind`. Plain
  Coding Chat and the Product coder remain unchanged. A real
  Git counter-example using the configured source as the Chat workspace now
  reaches Approval without dirtying source; removing the boundary reproduces
  `workspace-source-invalid`, and a registered write probe is denied without an
  Effect.
- Closed the actual START-path deadlock at the plugin ActivationSet lifecycle
  boundary. `PluginActivationSet` previously called `asyncio.create_task()`
  while holding its non-reentrant ownership lock. Under Python 3.12 eager task
  scheduling, used by the Textual path, an empty/core cleanup could run
  synchronously and re-enter that same lock before `create_task()` returned,
  blocking the entire event-loop thread. Line Chat normally used lazy task
  scheduling, which explained the misleading mode-specific symptom.
- Disposal now freezes `disposing` under the synchronous ownership lock, starts
  cleanup only after releasing it, and uses one async start lock to preserve the
  existing exactly-once Task and repeated-cancellation contract. Router cleanup
  remains on its original Supervisor path; the earlier responder-local removal
  was reverted because it treated a symptom as an ownership change. A
  deterministic eager-task counter-example turns same-thread lock re-entry into
  an immediate failure: the old implementation fails through real Runtime
  disposal, while the corrected implementation and Textual Product path
  converge normally.
- Fixed the single Product pane's task-identity handoff after a terminal task.
  A newly offered or confirmed proposal now closes and awaits the old observer,
  clears only its in-memory projection, and selects the exact new task before
  rendering its START gate. Old terminal evidence remains durable but can no
  longer mix with the new requirement or suppress its authorization. Removing
  the handoff reproduces the public "new title + old failure + no START"
  counter-example; the restored path and the 17-test Textual Pilot pass.
- Replaced the presentation in place with the one current light-theme layout;
  no old-TUI or theme compatibility path remains. The Product summary starts at
  the top, only the legal gate stays at the bottom, typed confirmation preserves
  the summary, gate buttons use one outlined accent, facts have an exact fixed
  width, short conversations grow upward from the input, and model prose uses a
  compact dim/italic `模型 ·` marker. Real screenshot verification caught and
  corrected a 58-column facts row being rendered in a 52-column pane; 100–109
  columns now use the single-column layout, and 110 columns is the first width
  that proves the complete facts row fits.
- Completed the first readability batch without changing Product authority,
  observation or masking. The task-conversation page now uses one ordered event
  pass for speech and Tool activity, reports Tool sequence ranges and explicit
  omitted counts, and is four physical lines smaller across its reader and
  screen rendering. The main conversation uses three stable left edges (user,
  teal host and indented dim/italic model), while the Product pane uses exactly
  four semantic groups separated by three rules and Chinese evidence headings.
  The direct Textual/presentation/conversation group passes 47 tests, its
  adjacent Product/CLI owners pass 161 tests, and the repository collects 2575
  tests. Follow-up N10/N11 now replace the default patch preview with a summary
  parsed from exact validated Artifact bytes and add the fresh, read-only
  `Ctrl+D` change page without a cache, second fact source or durable write.
  N12/R4 then complete the same read-only task-conversation path: expanded
  roles no longer have screen, message or RichLog content caps; all retained
  text remains terminal-safe and scrollable. Role headers now use an inline
  full-width hierarchy without reverse video, preserve complete facts through
  manual narrow-width wrapping, shorten only the displayed Session handle,
  collapse redundant model blank lines, and remove duplicate instructions.
  A 2,105-message Pilot retained both ends and remained scrollable without an
  incremental-rendering state machine.
- Project stable leaf failure evidence only from the exact failed Workflow
  message Turn after Agent/Session/create-request binding and core invariant
  validation. Identity-conflict nodes never adopt a foreign Directory record.
  The Workflow message/Turn is unique, its single runtime error must belong to
  the Turn actually open at that event and the Turn must end as failed; later
  unrelated or forged Turns cannot replace the original failure. Missing
  reliable evidence is explicit `unavailable`. The TUI distinguishes that
  code/category/type from the Workflow wrapper without displaying raw Provider
  bodies, headers, exception messages or tracebacks.
- Closed the repeated real-provider failure at the existing OpenAI-compatible
  response boundary. Strict JSON remains first; only a top-level double-triple-
  quoted value whose exact frozen Tool schema declares it as a string may be
  lexically normalized, and the complete result must still parse as a JSON
  object. Nearby malformed forms remain non-retryable protocol failures; there
  is no JSON5/eval/fallback or model, task, file or Tool-name hardcoding. One
  strict decoder also rejects Python's non-JSON `NaN` and positive/negative
  `Infinity` extensions before any Tool can run. See ADR-0038.
- Completed one targeted real `qwen-plus` TUI acceptance from Proposal through
  typed START, auto-resolved single execution, Review, typed Approval and
  one-shot bare-target Promotion. The source stayed clean, four promoted tests
  passed, and all measured Session, Budget and Workspace owners converged. At
  that checkpoint this focused acceptance did not replace either the still-
  pending 18-attempt grid or a current full-suite gate; the later format-7 M2
  full result is recorded above.
- Cleared the Provider, TUI, failure-evidence, and final cross-owner re-reviews
  with no P0/P1/P2. The latest adjacent-owner regression is `251 passed`; the
  renewed current-candidate full suite then ran as described below.
- Fixed the Textual gate focus handoff at its presentation owner. A gate Button
  click can finish its own focus processing after the handler; immediate
  `focus()` could therefore be overwritten and swallow a user's following
  typed confirmation. The single current App now uses Textual's public
  `call_after_refresh(field.focus)` without adding state, authority or a second
  path. Pilot confirmation flows wait for and assert real `Input.has_focus`
  rather than guessing one event-loop pause.
- Kept both complete red gates as evidence. The first was
  `2553 passed, 7 skipped, 2 failed` and exposed a legal transient/durable START
  observation race plus the production focus handoff. The second was
  `2554 passed, 7 skipped, 1 failed` and exposed two remaining Pilot assertions
  that read state before queued click handling. After the focused test passed
  in 10 independent processes, three complete TUI runs each passed 34 tests,
  and independent re-review cleared P0/P1/P2, a fresh complete run finished
  **`2555 passed, 7 skipped in 3078.80s (0:51:18)`**, exit 0. The subsequent
  output-visibility change above materially changed the TUI and tests, so this
  full run is retained as historical evidence rather than claimed as the
  current final gate. At that checkpoint a renewed focused review and final full
  run were pending; the later format-7 M2 result is recorded above. Clean-input
  archives/offline installs and the separately authorized real-Provider grid
  remain pending.

### v0.8-F4: optional Textual adapter on the existing Chat/Product mainline

- Added `traceh chat --tui` as an optional presentation adapter, not a second
  command or Product path. Line and Textual now share UI-neutral Session
  opening/recovery, the existing `ChatDriver`, SQLite conversation facts and
  the same Product host/control plane.
- Added the `tui` extra (`textual>=8.2.8,<9`). Core imports, Line Chat and Eval
  remain independent of Textual; selecting `--tui` without the extra fails
  before Store/Runtime/Session assembly and never silently falls back to Line.
- Added a bounded two-column interface for conversation/activity and the
  current ProductTask. Restart reconstructs conversation and the unique
  unsettled task from durable facts, never widget state; ambiguous live tasks
  fail closed. The original fixed-button presentation recorded here has since
  been replaced in place by the current F5 usability correction above.
- Preserved human authority: model confirmation only exposes the exact typed
  start request, and a separate START button is required. Approval is enabled
  only for reconciled fresh durable Review/evidence with no existing receipt
  and sends the existing task-id command to the original owner; digest
  calculation, idempotency, stale protection and
  Promotion remain outside the UI and model context.
- Rendered every model/Patch/path/error-derived value as bounded plain text,
  with terminal-control escaping and Textual/Rich markup disabled. App close,
  Ctrl+C and terminal teardown cancel and await the existing Runtime/Product
  owners before observer and watcher cleanup.
- Added a bounded periodic durable refresh beside the process-local Feed dirty
  hint. Positive heartbeat configuration supplies its interval; disabling the
  activity heartbeat still preserves the default 10-second Product refresh, so
  another process cannot leave the TUI permanently stale.
- Added core-only optional-dependency/presentation tests and Textual headless
  Pilot tests for resize, durable turns, active cancellation, Line/TUI start
  identity, START/Approval authority, duplicate clicks, durable restart and
  markup/control/Unicode bounds, plus refresh without any Feed notification.
  Reverse verification proved automatic start, enabled markup and removal of
  the periodic waiter are caught. No F5 full suite, package/release,
  version bump, real Provider, fallback, token streaming or second fact source
  is included.
- Release Stop C and its focused periodic-refresh re-review both completed with
  no remaining P0/P1/P2. The focused review ran 302 offline checks; its
  core-only interpreter did not install Textual and did not claim to repeat the
  separately recorded seven headless Pilot tests.

### v0.8-F3: UI-neutral Chat driver and read-only Product observation

- Added one UI-neutral `ChatDriver` that submits Turns to the existing
  `AgentRuntime` and emits typed Session event, activity and terminal outcome
  updates. It never reads stdin, renders terminal text or stores conversation
  state; cancellation still uses the Runtime owner and converges before return.
- Moved the sole ephemeral `ActivityTracker` into `traceh.chat`. Line rendering
  consumes those typed updates, so a future TUI cannot silently invent a second
  in-flight interpretation. Timeline and heartbeat remain non-durable and do
  not change model context or request fingerprints.
- Split Product Chat coordination from the Line-terminal adapter. Proposal,
  exact `START`, inspect/approve/reject/cancel/abandon and Promotion still call
  the original control-plane owners; model Tools retain only their low-authority
  ephemeral Turn actions.
- Added pure Product observation that fresh-reads ProductTask, Workflow,
  Directory, Artifact and Promotion facts, keeps Product/Workflow status
  separate, and never reconciles or appends. Exact-stream subscribe-before-read
  closes discovered-stream races; Feed payloads are only dirty hints, with
  periodic, action and final durable refresh covering dropped notifications.
- Closed the first independent review's observation-lifecycle P1: a failed
  initial fresh read now rolls back every subscription and watcher inside the
  observation owner, while the Line adapter also retains cleanup ownership
  across partial start. Product host assembly now requires the exact Feed owned
  by its `PublishingEventStore`; missing or mismatched Feed wiring fails before
  any Product resources are assembled instead of creating a silent observer.
- Independent short re-review found no P0, P1 or P2. Its cancellation probe
  confirmed the original `CancelledError` propagates while all five initial
  subscriptions and every watcher converge, and a closed observer cannot
  restart.
- Migrated the existing Line CLI and its tests without a compatibility alias for
  removed private display helpers. Added deterministic no-stdin, Feed-loss,
  stream-divergence, cancellation and architecture tests plus reverse
  verification for re-read, periodic refresh, pure-read, shared-tracker,
  failed-start rollback and Store/Feed identity protections. No TUI, second
  Product state/command, Runtime, Event bus,
  Provider call, version bump or release action is included.

### v0.8-F2: typed Provider failures and bounded same-request retry

- Added stable, sanitized Provider failure codes/categories. The
  OpenAI-compatible adapter classifies approved transport and HTTP failures by
  type/status without exposing response bodies, headers, transport text,
  secrets or local paths; untyped Provider/plugin failures become non-retryable
  `provider-failure-unclassified`.
- Added one explicit host `ModelRetryPolicy` with finite Attempt/elapsed/delay,
  Retry-After and jitter bounds. Only temporary DNS, timeout/408, TLS EOF,
  disconnect, 429 and selected 5xx categories are candidates; permanent and
  unknown categories cannot be added. Exponential delay applies the finite cap
  without materializing an unbounded integer, so even a very large valid
  ordinal cannot overflow before the decision. The adapter itself has no
  hidden retry.
- Kept retry inside the existing AgentLoop Step: every later ordinal reuses the
  same Composition-resolved Provider/model and exact request frozen by ordinal
  one. AgentLoop and Session CAS both reject drift before dispatch, and later
  Attempt starts bind the immediately preceding typed failure. Session also
  reuses the full core invariant checker under its Stream lock before granting
  a later dispatch permit, so an already-invalid history cannot authorize a
  paid call and is preserved rather than folded, repaired or deleted.
- Gave every retry ordinal an independent Budget reserve/start/settle lifecycle.
  Known failure Usage is charged and reported; unknown Usage remains
  conservative, and insufficient balance refuses a retry rather than shrinking
  the frozen request.
- Made cancellation converge without a later paid call across retry delay,
  reservation, Attempt start/end, Provider and settlement windows. Cancellation
  after a failed Provider outcome now returns to the AgentLoop lifecycle owner
  after Budget convergence instead of escaping as a `BaseExceptionGroup` with
  open Attempt/Step/Turn evidence.
- Extended `traceh eval` JSON/Markdown with one recorded policy, Attempt counts,
  retry wait, Provider-active time, stable failure categories and final model
  results for routing and execution. Product success, auto arm attribution and
  quality semantics are unchanged; retry-assisted success is reliability data,
  not a quality claim.
- Added deterministic clock/Gate tests and reverse verification for permanent
  failure storms, uncharged later calls, cancellation ownership and request
  drift. Public counterexamples also reject missing response messages and
  explicit null Usage as protocol failures, and prove a large valid retry
  ordinal still returns a finite bounded delay. No Provider/model fallback,
  proxy/TLS weakening, second Runner, driver/TUI, v0.9 capability, version bump
  or release action is included.
- Closed Release Stop B after independent re-review found no P0/P1/P2. The first
  full F1+F2 suite exposed one stale CLI Activity assertion that expected a raw
  Provider `RuntimeError`; the production-safe typed failure was retained and
  only that assertion was synchronized. A complete rerun from a new short
  external pytest directory passed with `2472 passed, 7 skipped`, exit code 0;
  no `--lf`, test filtering, network, real Provider or F3 work was used.

### v0.8-F1: single production SQLite EventStore

- Replaced the production JSONL backend with one stdlib SQLite EventStore and
  removed `JsonlEventStore` plus `Durability.BATCHED`. Runtime factories now
  require an explicit borrowed Store; CLI commands, each Evaluation attempt and
  each Evolution comparison case own and close their Store scope.
- Added a version-1 schema with canonical full-envelope JSON, transactional
  `expected_seq` CAS, cross-process primary-key enforcement, WAL,
  `synchronous=FULL` and a documented five-second default busy timeout. Writers
  across different Streams serialize within that bound; timeout is the stable
  `event-store-busy` error and is never retried by the Store.
- Made open/read fail closed on unknown, older or newer schema, integrity
  failures, sequence gaps, malformed/non-canonical Envelopes and linked Store
  paths. The exact-schema gate covers every persistent schema object and the
  normalized table DDL, so extra triggers/views/indexes cannot alter accepted
  appends. Existing databases first pass an immutable read-only exact-schema
  authority probe that cannot recover a hot rollback journal; only a proven
  current schema may be opened read-write for SQLite crash recovery, full
  integrity/history validation and WAL setup. A database rejected by that
  authority probe therefore keeps its original bytes, journal mode and rollback
  journal evidence. Old `.jsonl`/`.lock` and mixed roots are refused without reading,
  moving, deleting, importing or masking their evidence.
- Added explicit idempotent close and cancellation convergence: new operations
  are rejected once closing starts, admitted Worker threads finish before the
  caller returns, and fresh replay remains the authority for a cancelled append
  that may already have committed.
- Preserve independent Runtime/attempt failures together with Store shutdown
  failures at CLI, Evaluation and comparison ownership boundaries instead of
  letting cleanup mask the primary error.
- Added validated, non-overwriting SQLite backup/restore and deterministic tests
  for same-stream cross-process CAS, different-stream bounded wait/timeout,
  process death, authorized current-schema hot-journal recovery, refusal of an
  unknown hot journal without evidence loss, concurrent Stream/Feed traffic, commit cancellation,
  close-cancellation, backup during an active writer and all legacy/schema/data
  refusal paths. No retry, Provider fallback, TUI or v0.9 capability is included.

### v0.8-F0: two-stage Model admission and dispatch evidence

- Split the generic model boundary into side-effect-free admission and one-shot
  dispatch. Budget admission now freezes the exact provider-bound request and
  holds an Attempt-scoped PENDING Token reservation before any Session Attempt
  evidence or Provider call exists.
- Made the Session stream CAS the only Provider-dispatch permit. The first
  Attempt atomically persists one exact composed/dispatch request snapshot and
  its start; competing owners use independent Attempt/reservation identities,
  and the loser releases its hold without calling the Provider.
- Bound the admitted capability back to the exact Composition-resolved Provider
  object and host Attempt before Session evidence is written. `AgentLoop`
  accepts only the concrete host admission; Budget contributes accounting hooks
  but no longer owns an overridable dispatch method, so an injected Runtime
  cannot declare one Provider/request and dispatch another through that handle.
- Bound Attempt start/end to ordinal, request snapshot seq, dispatch
  fingerprint, provider/model and reservation identity. Request replay verifies
  the composed request independently and permits only a non-increasing
  `max_output_tokens` difference in the exact dispatch request.
- Converge pre-dispatch cancellation, unavailable append returns, Provider
  outcomes and Budget settlement without detached work. A zero or insufficient
  Token Budget now records no false Model Attempt.
- Sanitize the final `traceh chat` exception type and message through the
  existing bounded single-line terminal safety boundary, preventing control,
  newline and bidirectional text from forging Product or Approval output.
- Added deterministic public-path tests and reverse verification for the old
  Step-scoped reservation collision, Budget-after-Attempt ordering and missing
  Session CAS, plus Provider swapping and dispatch-time request rewriting. No
  retry, SQLite, Provider fallback, TUI or v0.9 capability is included.

## 0.7.1 - 2026-08-29

### Host authorization, convergence and supported-platform portability

- A chat model's `confirm_product_task` Tool Call now only requests a
  host-owned authorization prompt. The exact pending ProductTask starts only
  after the terminal user types the fixed `START` control token; EOF, any other
  input, or undecodable input leaves the Proposal pending and creates no
  ProductTask, task Budget, managed Workspace or Workflow. This is a structural
  host capability boundary, not an English/Chinese yes/no keyword parser.
- `AgentLoop` now owns one cancellation finalizer that durably closes the
  current Model Attempt, Step and Turn in that order. Repeated caller
  cancellation is absorbed until the same finalizer converges, commit ambiguity
  is resolved from a fresh Session replay, and an independent finalizer failure
  remains visible beside the original cancellation.
- L4 target inspection keeps `-I -S` but explicitly selects the standard
  `venv` sysconfig scheme when the selected interpreter has an adjacent
  `pyvenv.cfg`. Resolved package roots must remain inside that venv, preventing
  distro-specific Python 3.13 default schemes from redirecting inspection to a
  nonexistent `local/.../dist-packages` tree.
- Managed Workspace identities now use `ws-<full SHA-256>` instead of the
  redundant `ws-workspace-<full SHA-256>`. The full 256-bit identity and all
  ownership checks remain unchanged, while the ten-character reduction keeps
  nested Candidate L2 Product Benchmark worktrees below Git for Windows'
  fixed `$GIT_DIR` boundary. A real long-path worktree test reproduces
  `workspace-git-failed` with the old label and passes with the compact identity;
  no retry, path-specific exception or shortened digest was added.
- The release gate found that the independently built Plugin Creator and Python
  Quality examples still excluded every 0.7 core in both Wheel metadata and
  `PluginManifest`. They are now `0.2.1`, with matching `<0.8` runtime ranges;
  the Creator package remains installable on 0.6/0.7 while its authoring guide
  generates new candidates for the current `>=0.7,<0.8` contract.
- Added deterministic public-path counter-examples for a model confirming a
  user's refusal, three separately gated cancellation terminals plus finalizer
  failure, and a distro-biased sysconfig default exercised through
  `CandidatePromoter.run()`. Each root protection has been reverse-verified.

## 0.7.0 - 2026-08-28

### v0.7-F5 RC: ProductTask acceptance and release stabilization

- Advanced the package's single version source to `0.7.0`; pyproject metadata,
  package exports, core plugin identity, plugin API compatibility and CLI output
  continue to derive from `traceh.version.__version__`. Added the dedicated
  [v0.7.0 validation record](docs/validation-v0.7.0.md). The release commit
  passed clean-input packaging, archive audit and a fresh `--no-index` install
  before the annotated tag and GitHub Release were published.
- Built the `0.7.0` Wheel, sdist and Git-index source ZIP from a clean clone of
  the release commit. Archive audit found 190 Wheel entries, 306 sdist entries
  and an exact 388-file source ZIP, with no env file, cache, bytecode, Git data
  or undeclared working-tree note. A fresh venv installed the core and its
  locally reconstructed `packaging 24.1` dependency exclusively from a local
  wheelhouse; package/distribution versions, CLI banner, scripted doctor,
  `eval --help` and plugin discovery all passed without index or Provider use.

- Made the existing Product Chat approval barrier readable without adding a
  state machine or event stream. Confirmation now prints the task id before
  execution, the existing optional heartbeat reports fresh durable Product and
  Workflow progress, and `/task inspect` plus the automatic approval screen join
  the fixed Workflow, Agent Directory, immutable Artifact and Review facts to
  show node/Session replay identities, changed paths, bounded inert Patch text
  and verifier outcomes. Missing or tampered evidence is explicitly unavailable
  and the screen says not to approve. None of the projection enters a model
  request. All CLI commands now apply the UTF-8 stdio policy before rendering,
  so `replay` and `inspect` cannot crash on a valid Unicode scalar under a
  legacy Windows code page.
- Root-fixed an independent-review P1 in the approval evidence chain. A durable
  Review could replace one verifier result's `argv_digest`, recompute its
  internal evidence digest, still render as passed, and then be directly
  approved and promoted. Promotion now owns one frozen-plan Review validator
  covering definition digest, result count/order, command ids and argv digests,
  evidence digest and `passed`; inspection, existing-Review reuse, approval and
  promotion all reuse it, including idempotent reads. F4 evidence collection
  now also requires the manifest's frozen VerificationPlan and reuses that same
  matcher before treating `review.passed` as a metric. The public-path
  counter-example now renders unavailable/do-not-approve, rejects direct
  approval and leaves the bare ref unchanged; a second counter-example keeps
  every Product/Workflow/Promotion identity coherent while substituting a
  result outside the plan, and the collector refuses it. Short-circuiting the
  shared rule reproduced both the old ref movement and the false successful
  measurement before the protection was restored.
- Fixed the remaining Product recovery path found by independent re-review.
  When a Promotion fact was durable but `product/task-completed` was not, the
  control plane previously trusted a ledger lookup and completed the task
  without re-entering Promotion's frozen-plan validation. Recovery now calls
  the idempotent `promote()` operation first and uses its returned receipt. A
  real Chat/Promotion crash-prefix counter-example remains awaiting approval
  and reports `promotion-review-verification-mismatch`; restoring the old early
  return made it durably `completed` again.
- Separated cumulative Product Token authority from each provider request's
  output ceiling in [ADR-0034]. Every role and Router now requires an explicit
  `max_output_tokens` in addition to `budget.max_tokens`; the exact schema
  rejects the old shape, the resource binding passes the request bound through
  the existing `RuntimeConfig`, and the Budget ledger remains unchanged. The
  shipped benchmark restores cumulative role accounts of 60000/60000/120000,
  uses 8192 per role request, and uses 8000 cumulative/256 per request for the
  Router. All arms still share one frozen Profile. Because this changes the
  Profile digest, the fifth 18-attempt grid remains historical and did not
  certify the current manifest; the next item records its replacement gate.
- Completed that post-change grid from a fresh repository-external directory
  with process-local proxy variables disabled and `NO_PROXY=*`. The unchanged
  public CLI measured all 18 attempts with no unavailable metrics and produced
  15 full successes: requested single 5/6, multi 4/6 and auto 6/6; all auto
  routes parsed strictly and resolved to single. The three failures were
  durable Windows DNS `getaddrinfo failed` errors at one single coder, one
  multi parent and one multi coder; there was no TLS EOF, Budget exhaustion,
  Router rejection or Verifier failure. All 52 Budget accounts terminalized;
  51 of 52 Workspaces released and the one dirty Provider failure quarantined
  its evidence with `live=0`. JSON and Markdown agree across all attempt and
  aggregate rows and contain no credential shape or current-machine path.
- Isolated the remaining lookup failures to the WLAN's preferred DHCP DNS: its
  uncached UDP and TCP queries failed 50/50 and 10/10 while the healthy
  resolver passed. After the user replaced the WLAN pair with
  `223.5.5.5`/`223.6.6.6`, the Windows resolver passed 200/200 and the same
  proxy-free Python `urllib`/OpenSSL admission path used by the Provider passed
  50/50 without a key. A seventh fresh, unchanged 18-attempt grid then measured
  all attempts and produced 16 full successes: requested single 5/6, multi
  5/6 and auto 6/6, with zero DNS or TLS EOF failure. The two remaining durable
  failures were one remote disconnect and one multi coder stopped by its
  cumulative Budget after 126312 measured tokens. All 54 Budget accounts and
  54 Workspaces converged; two dirty failures were quarantined with `live=0`.
  No retry, fallback, report replacement, proxy special case or SSL relaxation
  was added.
- Aligned the production Router request with the strict response contract it
  already enforces. The host now tells the model that `reason` is `null` or
  non-empty single-line-safe display text bounded by the shared
  `MAX_REASON_DISPLAY_CHARS`, with no surrounding whitespace or extra prose;
  the parser remains fail closed with no truncation, retry or fallback. A
  deterministic Chat-to-ProductTask counter-example returns 257 characters
  when that contract is absent: the old prompt reproduces
  `product-router-reason-invalid`, while the corrected public path resolves to
  `single`. A fresh 18-attempt real-model grid then measured every attempt and
  produced 15 full successes: auto strictly parsed 6/6 with no reason rejection
  and resolved all six to single. The remaining three failures were coder-side
  Windows DNS `getaddrinfo failed`; there was no TLS EOF or verifier failure.
  All Budget accounts and Workspaces converged, and the prior 13/18 grid remains
  intact as pre-fix historical evidence.
- Identified the prior TLS instability as the Windows user-level loopback proxy
  path used automatically by Python `urllib`: the proxied path reproduced 4 TLS
  EOFs in 20 credential-free probes, while the same Python/OpenSSL stack was
  20/20 when direct. A process-local `NO_PROXY` also proved 20/20 without
  changing the system proxy. After a 50/50 clean admission probe, a third
  corrected 18-attempt grid using that bypass measured every attempt and had 13
  full successes with no TLS EOF. Its five failures were two strict Router
  reason rejections, one coder Budget exhaustion and two transient Windows DNS
  `getaddrinfo failed` errors. Resolved single was 8/10 and multi 5/6; these are
  descriptive small-n results. No production proxy special case or retry was
  added, and all three corrected reports remain intact outside the repository.
- Repeated the corrected 18-attempt grid through the unchanged public CLI after
  a credential-free TLS admission probe briefly returned 20/20 HTTP responses.
  The follow-up again measured all 18 attempts and converged every Budget and
  Workspace, but only 3 met the full success definition: 14 durable model
  attempts failed with TLS `UNEXPECTED_EOF_WHILE_READING` (coder 10, parent 3,
  Router 1), and one Router answer failed the strict reason contract. The
  preceding Python/OpenSSL probe had observed 14 TLS EOFs in 50 requests while
  Windows Schannel/curl reached all four resolved IPs 32/32; the later clean
  20-request window therefore did not predict POST stability. No retry,
  fallback, attempt replacement, report splicing or SSL relaxation was added.
  Both corrected reports remain evidence; the transport-confounded 1/10
  resolved-single versus 2/6 resolved-multi result is not a quality claim.
- Ran the shipped ProductTask benchmark through its only production entry with
  one explicit OpenAI-compatible model: three tasks by single, multi and auto,
  repeated twice. The corrected run measured all 18 attempts with no unavailable
  attempt and coherent experiment conditions; 11 attempts met the full Product,
  Workflow, Review and Promotion success definition. Resolved single was 9/10
  and resolved multi 2/6. Auto strictly parsed 4/6, resolved those four to
  single and kept routing cost separate.
- Classified the seven quality failures from durable facts instead of model
  prose: six were provider TLS `UNEXPECTED_EOF_WHILE_READING` failures and one
  was the strict `product-router-reason-invalid` contract. `complete=true`
  therefore means the measurement and evidence chain completed, not that every
  coding attempt succeeded. Every Budget account and Workspace converged with
  no live record.
- As an interim RC measure, the fifth grid used `32768` for each role's
  cumulative account so the provider would not see a larger request ceiling.
  A later real Chat showed why that was not a final design: one Coder consumed
  38454 exact tokens across eleven legal responses and exhausted the cumulative
  account before its summary. ADR-0034 replaces that coupling with the explicit
  two-limit contract above. Each task's tracked initial tree also ignores
  ordinary Python bytecode caches, keeping verifier-generated files out of
  candidate Patches without adding a runner fallback or changing evaluation.
- Fixed a D1/D2 Git boundary defect exposed by the real model: non-recursive
  `git diff-tree --raw` reported a newly added directory container as mode
  `040000`, causing Artifact capture and Promotion to reject an ordinary first
  file below that directory. Both existing readers now request recursive leaf
  entries. Two deterministic public-path tests use real Git to prove capture and
  promotion of a new regular directory; removing the recursive flag reproduced
  `artifact-git-mode-rejected` and `promotion-git-mode-rejected` before the
  sources were restored.
- A corrected one-attempt smoke completed the full model/tool/Patch/verifier/
  approval/promotion chain. The 18-attempt JSON and Markdown reports agree and
  all evidence remains outside the repository. Targeted adjacent regression is
  `111 passed, 2 skipped`; the Router/F3, Product contract/architecture and
  Product Benchmark E2E correction gate is `141 passed`. The current Chat/Token/
  Unicode stabilization gates are Product `257 passed`, Evaluation `52 passed`,
  Budget/Workspace/Artifact/Promotion/Workflow `397 passed, 3 skipped`, and CLI
  `521 passed, 1 skipped`; collect-only is 2407. Seven classes of critical
  protection were reverse-verified individually. Compileall, changed-scope Ruff, the generic
  example-hardcoding scan, `git diff --check` and the four protected-core zero
  diff check pass. Independent re-review cleared P0/P1/P2; the one final full
  suite then passed `2402 passed, 5 skipped` from 2407 collected tests with exit
  code 0. The F5 security scan covered 377 tracked or intended-new text files:
  no real credential shape, current-machine user path, or benchmark/provider
  name appeared in production code; broad key-like matches were synthetic test
  identities. The post-change real-model grid, packaging/offline-install gates,
  version bump, validation record, tag and release remain outstanding.

### v0.7-F4: `traceh eval` is the ProductTask benchmark

- Replaced the v0.6 single-Agent scripted benchmark with one that drives the
  real ProductTask mainline: a confirmed proposal, the fixed Workflow, a managed
  Git worktree, an immutable Patch Artifact, the frozen verifier, a Review and a
  Git ref compare-and-swap promotion. `traceh.evaluation` assembles the same
  `ProductChatHost` the Chat host assembles and drives the same control plane;
  there is no second runner, task state machine or definition of success.
- The v0.6 `*/case.json` layout is now refused with a stable
  `benchmark-legacy-manifest-rejected`, without reading any of it. There is no
  adapter, no dual reader and no automatic rewrite or deletion of old data.
- Added a schema-1 `benchmark.json` with an exact key set. It names the Profile,
  role slots and Budgets, Router bounds, the aggregate task Budget, the frozen
  VerificationPlan, capture limits, arms and tasks. It cannot name a repository,
  a promotion target, a provider, a model, a node, an edge, an Agent count or an
  approval digest: each attempt's source repository and one-shot local bare
  target are created by the runner, so the command structurally cannot reach a
  real remote.
- The requirement and the requested mode come from the manifest and are passed
  to the host control plane directly. A host-frozen, tool-free requester
  provider produces only the durable Session evidence `product/task-opened`
  requires, so a candidate can never author the question it is scored on.
- Metrics are derived from the fact source that owns each one: success from a
  ProductTask terminal plus a Workflow terminal plus a passed Review plus a
  Promotion receipt whose new revision is what the target ref actually holds;
  routing and execution tokens from the Sessions the routing fact and the
  Workflow node outcomes name; steps, tool calls and cumulative work duration
  from durable Session facts; Budget outcome from the ledger scoped to the
  task's ownership subtree. Active, approval-wait and wall intervals come from
  the runner's own monotonic clock and are labelled as the one non-durable
  measurement.
- A metric the facts cannot support is reported as unavailable rather than zero,
  including a `UsageQuality.UNKNOWN` usage report, which makes that Session's
  token total unavailable instead of contributing a misleading `0`.
- A **failed** AgentTask node records no `agent_id`, so the role's Agent is
  derived from run and node by the executor's own rule and cross-checked against
  the outcome. Reading the terminal payload alone dropped every token a role
  spent before it failed and aggregated a confident zero for real work.
- Every measured Session is checked with the existing `CoreInvariantChecker`
  before any number is taken from it, so a stream that merely looks like a
  Session cannot inflate a token count.
- The Workflow Verification outcome, the ProductTask `review_id` and the
  Promotion receipt must describe one Review, and the promotion's approval
  digest must be that Review's expected digest. Read independently, three
  unrelated well-formed records still read as "verified, approved, promoted".
- The routing Session is resolved from the Agent Directory record of the Router
  Agent and must match the one `product/task-routed` recorded. Trusting the pair
  in the payload let a routing identity point at a role Session of the same
  task, billing one set of tokens to both the routing and execution metrics.
- Workspace quarantine is reported as a converged terminal, not as failure to
  converge: the Product resource contract quarantines a dirty worktree on
  failure or cancellation to preserve the captured bytes. Convergence now
  excludes only records still `provisional` or `attached`, exposed as `live`.
- The shared verifier is proved from the frozen manifest instead of inferred
  from whichever attempts reached a Review, and a field no attempt established
  is listed in `unproven_fields` rather than filtered into apparent agreement.
- `auto` is not a third quality arm: each auto attempt is aggregated into the arm
  its Router resolved to, and auto is reported separately only as strict-parse
  outcome, routing tokens and routing elapsed. An arm with one observation is
  labelled `single observation`, and aggregation claims no significance.
- Approval is `programmatic-immediate` and labelled in both outputs, so active
  elapsed measures work rather than how long a person was away. It grants no
  authority outside the benchmark; ordinary Chat still requires `/task approve`.
- Each task's shared `requirement_digest`, `profile_digest`,
  `source_base_revision` and `verifier_definition_digest` are recorded across
  arms and any divergence is named, so "single and multi ran the same
  experiment" is checked rather than assumed.
- `traceh eval` now requires `--output` and refuses an existing directory, takes
  `--provider`/`--model`/`--base-url`/`--api-key-env`/`--script`, and exits `4`
  when the measurement is incomplete rather than when a coding task fails.
  Failure and cancellation converge the existing owners and record an honest
  terminal; no evidence directory is deleted.
- `parse_product_host_settings` now owns the shared half of the Product host
  schema so the Chat host file and the benchmark manifest cannot disagree about
  what a Profile is; `ProductChatHost.control` exposes the host-side operations
  the console surface renders. Design decisions are recorded in
  [ADR-0033](docs/adr/0033-product-task-benchmark-as-the-single-eval-path.md).
- Independent F4 review is complete: two review rounds found five P1 and two P2
  defects, all fixed with deterministic public-path counter-examples, including
  six per-fix reverse verifications. The single final full gate collected 2395
  tests and finished with 2390 passed, 5 skipped, exit code 0 in 28:04;
  compileall, changed-scope Ruff, documentation QA and `git diff --check` also
  passed, and the four protected core files have zero diff. Real external-model
  acceptance was still outstanding at the F4 checkpoint and was completed later
  in the F5 RC work recorded above; the remaining release work is still open.

### v0.7-F3: unified Chat ProductTask execution and approval

- A Proposal may now carry an explicit `single`, `multi` or `auto` mode when
  the user asked for one. The host renders the mode and its source before a
  later human confirmation; omission still uses the Profile default, and no
  topology or Agent count becomes model-controlled.
- Derive one prospective task identity from the exact Proposal and render it
  before confirmation. Confirmation still creates the first durable task fact,
  but Router failures and caller interruption can no longer leave the user
  without the identity needed for inspect/cancel/abandon.
- Treat every ordinary failure after `product/task-opened` and before Workflow
  execution as a task failure: release the already allocated ownership tree,
  Budget accounts and Workspaces first, then append `product/task-failed` with
  the stable error code. Cleanup or terminal-write failure retains the original
  error and does not claim convergence.
- Added an opt-in `traceh chat --product-config` host assembly. Plain Chat is
  unchanged when the flag is absent. The exact schema-1 file selects a Profile,
  source, managed Workspace root, CAS, fixed VerificationPlan and bare target;
  it cannot contain a DAG, prompt, approval digest or Agent count.
- Added low-authority proposal/confirmation Tools that retain only a current
  Turn action. The host opens a ProductTask only after the Turn closes and the
  existing Session evidence reader proves a later human confirmation.
- Connected the fixed single/multi/auto Product Assembly to the existing
  Supervisor, hierarchical Budget, managed Git Workspace, Patch capture,
  Workflow, Review and Promotion services. Workflow still does not promote;
  `/task approve TASK_ID` is the explicit host authority.
- Added fresh task-id commands for inspect/approve/reject/cancel/abandon and
  restart continuation at the proven Approval barrier. Product/Workflow
  cross-stream reconciliation fills only a missing awaiting/failed fact and
  never re-runs a partial node.
- Resource cleanup now precedes Product terminal facts. Exact captured trees
  may be removed after merge/reject; later drift fails closed, while failed or
  cancelled dirty work is quarantined. Confirmation interruption cancels and
  converges the live ownership tree instead of waiting forever.
- Added deterministic real-Git end-to-end coverage for ordinary Chat, explicit
  single/multi, auto routing, restart approval/promotion, rejection, model
  secrecy and cancellation. No external credential or real remote is used.
- Final F3 confirmation collected 2344 tests and passed 2339 with 5 existing
  platform skips. Promotion architecture guards now pin the exact F3 Product
  orchestration and CLI composition-root imports by file, module and symbol.

### v0.7-F2: strict Router, Profile Registry and Product Assembly

- Added the router, registry, topology and assembly stages to `traceh.product`
  so a confirmed ProductTask can be turned into one exact
  `ProductAssemblyReceipt` beside the fixed Workflow definition its hash was
  taken from - or refused with a stable error. It deliberately stops there: no
  Workflow run, capture, verification, approval, promotion, real model call or
  chat surface, and no `product/task-started` is written.
- `StrictTaskRoutingParser` accepts exactly one JSON object whose key set is
  exactly `mode`/`reason`; unknown modes, extra keys, code fences, prose,
  arrays, doubled answers and over-long or non-text bodies are all stable
  `ProductRoutingError`s, never retried and never guessed from free text. The
  enum beside the display-only `reason_display` is the decision; a reason that
  contradicts it changes nothing. `ProductModeRouter` takes every bound from an
  explicit `ProductRouterProfile` (no code defaults) and converges its owned
  responder on timeout, cancellation and close, so a deadline never leaves a
  router still talking to a provider. The live router receives the actual
  resolved assembly and derives its identity; before a new `auto` decision the
  assembler checks both its Profile bounds and assembly digest against the
  fresh preflight, so a paper Router cannot cover another live configuration.
- `ProductProfileRegistry` resolves one explicit profile id and has no default:
  unknown, empty, duplicated or ill-typed ids fail. Entries are
  `(profile_id, binding)` pairs rather than a mapping, so a repeated id is a
  construction error instead of a silent overwrite. The resolved
  `ResolvedAgentAssembly` must answer the slot it was asked about - preset,
  grants, provider and model - and two authorities are enforced rather than
  recorded: `workspace_access` must equal `ProductRole.workspace_access` (a
  writable reviewer is refused), and the router assembly carries no Tool, no
  grant and read-only access. `agent_assembly_digest` covers the resolved
  `AgentSpec` fingerprint (the repository's own definition), provider/model and
  the order-preserving Tool/Prompt/Policy composition; `role_assembly_digest`
  covers all three roles or refuses to produce one. A registry rebinding that
  keeps every name spelled the same changes `role_assembly_digest` while
  `profile_digest` stays put, which is exactly the drift a name-only binding
  cannot see. Profile Budget validation reuses the Budget domain's
  `freeze_limits()` contract, including `MAX_BUDGET_VALUE`, rather than keeping
  a weaker Product-local range.
- `ProductAssemblyService.preflight()` re-resolves on every call and caches
  nothing: the source snapshot's exact commit, the frozen
  `verifier_definition_digest` and the promotion target's fingerprint, exact
  branch ref and expected revision all come from the real resolvers (`LocalGitWorkspaceProvider`
  and `LocalBareGitPromotionTargets` already satisfy the two narrow seams). The
  resulting binding carries identities and revisions only - no repository path,
  prompt, credential or verifier argv.
- `assemble()` checks the freshly resolved `profile_digest` and preflight digest
  against the values recorded at opening *before* any routing call, so source
  drift, a changed verification plan, an advanced promotion target ref or a
  registry rebinding all fail closed with no routing token spent and no second
  receipt minted. An explicit `single`/`multi` never reaches the router; an
  `auto` task reuses its one durable `product/task-routed` fact, or asks exactly
  once and records the result through the F1 writer with a task-derived
  `operation_id`, so a retry is the same write and two racing assemblies leave
  exactly one routing fact. The receipt's `workflow_definition_hash` is
  computed from the definition that would actually run.
- The two fixed topologies live in `product/topology.py` and are not
  configurable: `single` is `coder → verification → approval`, `multi` is
  `parent → reviewer → coder →` the same safety tail, neither uses Map/Join,
  only the coder captures an Artifact, and the reviewer runs before the coder so
  its report is part of the work. No Profile, task or router answer has a field
  for nodes, edges, Agent counts or fan-out.
- Tightened the architecture guards rather than loosening them: the product
  domain may import exactly five named pure functions
  (`freeze_workflow_definition`/`workflow_definition_hash` from
  `traceh.workflow.models`, `freeze_verification_plan`/
  `verifier_definition_digest`/`require_target_ref` from
  `traceh.promotion.models`) so a receipt records *the* definition hash, *the*
  verifier digest and Promotion's exact ref rule rather than a second
  computation of any; everything else in every executing domain remains
  unreachable, and `tests/test_promotion_architecture.py` was extended the same
  way. The assembly service takes the concrete `ProductTaskService` by type, so
  the one fact universe is structural rather than a remembered comparison.
- Added `tests/test_product_router.py` (18), `tests/test_product_registry.py`
  (21) and `tests/test_product_assembly.py` (30); the cross-stage
  `tests/test_product_architecture.py` grew from 11 to 15, and the F0 contract
  guard about "no `router.py`/`registry.py`/`assembly.py` yet" became "they
  exist, `chat.py` and any CLI import still do not". Seventeen reverse
  validations, each through the public path and each failing for its own root
  cause before restoration: unknown-key tolerance, prose salvaging, an ignored
  response bound, timeout/cancellation without convergence, resolver-decided
  access, a dropped no-Tool check, name-only role digests, dropped drift
  comparison, re-deciding an already durable routing, routing before preflight,
  hashing a definition other than the one that would run, a duck-typed writer,
  and routing explicit modes, plus live Router profile/assembly binding, exact
  promotion target-ref binding and reuse of the Budget domain's range contract.
- Final post-review evidence: product-targeted gate `233 passed`, current
  adjacent Budget/Workflow/Promotion/Product regression `375 passed`, and the
  one final full gate `2321 passed, 5 skipped`; `compileall`, scoped `ruff` and
  document QA clean. The independent review found no P0/P1/P2. The four protected files are
  byte-identical and the version stays `0.6.0`.

### v0.7-F1: ProductTask as a durable fact

- Added `traceh.product`, an independent domain implementing the F0 contract:
  a strict parser and single projector, a fresh reader, host-owned writes for
  all nine facts, Session-replayed confirmation evidence, and a derived view.
  It records what happens to a task and drives none of it - an architecture
  test asserts it imports no Workflow, Promotion, Artifact, Workspace,
  Supervisor, Runtime, plugin, CLI or Provider module.
- `rebuild_product_task()` replays the whole stream every time: no status file,
  cache or second store. It enforces shape (stream, sequence, schema 1, exact
  key set), order (`PRODUCT_TASK_TRANSITIONS`) and value (`product_required_values()`),
  cross-checks the payload's `task_id` against the stream name, refuses a
  duplicate `operation_id`, and returns `None` - not an invented summary - for a
  task that was never opened.
- Stated the projector's reach honestly: `mode`, `workflow_run_id` and
  `preflight_digest` are replay-checkable, while `definition_hash`,
  `assembly_digest` and `source_base_revision` need the Receipt and are checked
  by the service through `binds()` and `product_started_values()`.
- `ProductTaskService` reuses the existing rules rather than reinventing weaker
  ones: the projection that validated the history supplies the compare-and-swap
  expectation, idempotency binds an `operation_id` to its exact canonical
  payload (a different payload under the same id is a conflict refused *before*
  the append), failed or cancelled appends go through the shared three-state
  `committed_after_failure()`, and one owned single-flight task per task means a
  cancelled caller re-raises its own `CancelledError` after convergence.
- Confirmation is proven, not trusted: opening a task replays a schema-1,
  contiguous, structurally valid `session/created` → `inbox/accepted` →
  `inbox/claimed` history for both messages plus the proposing Turn's durable
  `turn/start`/`turn/end`. Both claimed message Turns must have a real durable
  `turn/start`, and the whole Session must pass the shared
  `CoreInvariantChecker`; a `turn/end` written over an open Step is not valid
  authorization. Only a distinct plain `source="user"` message whose acceptance
  sequence follows the Proposal Turn end can authorize the task; an older
  origin message or one queued while the offer was still being produced cannot
  be relabelled as consent. Acceptance `source`, `content` and `target` are
  detached as exact built-in strings before comparison, and `target` must be
  exactly `new_turn`; a `str` subclass cannot impersonate that value. The
  evidence reader must share the exact EventStore object with the ProductTask
  writer. Backend-specific Session read failures become the single stable
  `product-session-unreadable` outcome, while caller-control `BaseException`s
  remain unchanged.
- Normalize an opening exactly once before policy, Session evidence and
  persistence. `proposal_confirmable()` compares detached built-in strings,
  and the service uses the same normalized Proposal/Confirmation that produced
  the payload, so a hostile `str` subclass cannot authorize one Session while
  writing another.
- Parse each Product event once into a detached `ParsedProductEvent`; replay,
  cross-event checks and operation idempotency share that representation.
  Caller-defined equality or a stateful `str.__str__` can no longer forge a
  decided value, make an operation disappear between replays or append a
  duplicate fact that poisons the stream.
- Preserve known commit state after a normal append return: if the final result
  reload fails, the public error is `ProductWriteError(committed=True)`, not a
  raw Store exception or an unknown outcome.
- The Workflow status source is bound to that same EventStore at construction
  and checked again before a view read, so another fact universe cannot decide
  `resumable`/`interrupted` or authorize `abandon_task()` for this task.
- Every field in `ProductPreflightBinding` and `ProductAssemblyReceipt` is
  validated before the first append. A malformed definition hash, revision,
  registry binding or derived digest is therefore an input error with a clean
  stream, not a durable fact that later poisons replay.
- `view()` reads the ProductTask, the Workflow state and ownership fresh on
  every call and caches none of them, so `unreconciled`, `resumable` and
  `interrupted` are all genuinely reachable. `abandon_task()` writes only where
  the derived view really is `interrupted`.
- Added `tests/test_product_task_stream.py`, `tests/test_product_service.py` and
  `tests/test_product_architecture.py` (`87 passed`; F0+F1 `160 passed`). Eight original reverse validations
  ran through the public path; one of them - the compare-and-swap source - did
  **not** go red at first, which exposed a missing test, and a case gating
  between replay and append was added before it did.
- Added four hardening counterexample groups for human Session evidence,
  malformed Session protocol, cross-Store fact sources and pre-append Receipt
  validation; all first failed on the old public path and pass after the fix.
- Added three review counterexamples for an older message reused as approval, a
  stateful string identity changing across replays, and a successful append
  followed by a failed result read. Removing each corresponding guard makes
  only its counterexample fail; all three guards were restored.
- Added three Session-authorization counterexamples: origin and confirmation
  claims cannot name Turns that never started, and a Turn ending over an open
  Step cannot authorize. Removing the durable-start checks makes both ghost
  Turns open a task; removing the shared invariant check makes the invalid
  closure open one. All guards were restored.
- Added four Session payload/read counterexample cases: plain and hostile
  non-`new_turn` targets are equally refused without invoking subclass
  comparison, a Store read failure has one non-reflective Product error, and
  `SystemExit` remains caller control. Restoring the hostile comparison and
  removing the read boundary each makes only its own counterexample fail.
- Bound every claimed Turn to exactly one message during Session replay, so a
  confirmation cannot borrow a Turn another message started. Product replay
  now also normalizes its public query identity once and uses that plain value
  for stream selection, payload identity checks and the returned Summary.
  Both public-path counterexamples failed before the fixes and pass afterwards.
- Added repository-wide review admission and stopping rules to `AGENTS.md`:
  blocking findings require a deterministic current public path and concrete
  contract impact; P2 is non-blocking by default; offline semantic review is
  separated from the single final full/network gate; and review stops expanding
  the threat model once P0/P1 are clear.
- After the independent review cleared P0/P1, the single final full gate passed:
  `2253 collected / 2248 passed / 5 skipped`, exit code 0.
- No Router, chat surface, default Profile/Registry/Assembly, Workflow
  execution, Promotion call or benchmark rework. `cli/chat.py` is unchanged, the
  four protected files are byte-identical, and the version stays `0.6.0`.
- Added `docs/plan/TRACEHARNESS_V0.7_STAGE_PLAN.md` as the single execution
  plan for D0 through F5, including the target Chat experience, architectural
  invariants, evidence-derived measurements and explicit v0.7 non-goals. It
  coordinates work but does not replace source, tests, ADRs or context as the
  authority for implemented facts.

### v0.7-F0: the unified chat product contract, frozen but not implemented

- Added `traceh.api.product`: a **contract-only** public module for the unified
  `traceh chat` product surface. It defines the ProductTask event vocabulary
  (nine types, each with an exact key set), the requested/resolved mode enums,
  the durable status enum with its allowed transitions, the derived view
  status, the fixed host Profile, the preflight binding and Assembly Receipt,
  the temporary Proposal and its confirmation rule, one read model, one derived
  view and two narrow protocols. It
  performs no I/O, holds no mutable state and imports only `traceh.api`.
- **Nothing is implemented.** There is no `src/traceh/product/` package, no
  event writer, no parser, no projector, no service, no router, no chat
  controller, no CLI command, no default assembly and no benchmark rework.
  `cli/chat.py` is unchanged, no build has ever written a ProductTask event,
  and the version stays `0.6.0`.
- Froze `product-task:<task_id>` as one append-only stream per task inside the
  existing Event Store: no database, status file, cache or second scheduler.
  It carries product identity, host control decisions, digests and references
  into the Agent Directory, Session, Workspace catalog, Artifact catalog and
  promotion ledger - never copies of their state.
- Froze five terminals as five distinct event types rather than one `settled`
  event with optional fields, so "completed without a promotion" and "cancelled
  carrying a review id" are not expressible shapes. `cancelled` and `abandoned`
  are separate facts, because a process that died proves no convergence.
- Froze `workflow_run_id == task_id` as a derived property of the read model,
  and both modes onto one `WorkflowService`: `single` is
  `coder -> verification -> approval`, `multi` is
  `parent -> reviewer -> coder -> verification -> approval`. The reviewer runs
  before the coder so its opinion is actually consumed. Write authority follows
  the role: `ProductRoleProfile` carries no role or access field of its own, so
  the slot it occupies is the only thing that makes it the parent, the reviewer
  or the coder, and `ProductRole.workspace_access` is the single definition of
  what that role may do. Stage E's Map/Join is neither used nor weakened.
- Froze `interrupted` as a derived view status only. The durable enum has no
  such member and no event can carry it: recording `cancelled` would claim a
  convergence nobody performed, and recording `interrupted` would freeze a guess
  the next read may contradict. An honest `product/task-abandoned` stays
  available and explicitly does not claim resources were released.
- Froze the routing seam honestly. `ResolvedTaskMode` has no `auto` member, so
  an unresolved mode cannot reach execution. `TaskRoutingParser.parse` is named
  for what it does: it parses a bounded response, it does not obtain one, and
  the caller owns the router Agent's Session, Budget account and bounds. A
  synchronous Protocol does **not** prove an implementation performs no I/O or
  holds no service handle - only that no handle arrives through the seam. That
  the router Agent holds no Tool is evidenced by `router_assembly_digest` and
  must be proven by the implementing stage. Routing runs after
  `product/task-opened`, because routing costs tokens and only an Agent Session
  enters the Budget ledger.
- Froze approval and promotion as host operations. No model-visible approve,
  promote, update-ref or capture capability exists, and the approval digest,
  Patch SHA-256 and exact revisions are absent from every product value.
  Promotion is called by the product service after the run completes, not from
  inside the Workflow.
- Froze the Profile as a fixed-length schema with no graph fields, no raw
  verifier command, no path and no credential; every field is required, and all
  seven Budget dimensions must be stated explicitly on all five accounts. Both
  `digest` values are computed properties rather than stored fields, so a
  supplied digest cannot disagree with what it describes or silently omit a
  field added later.
- Froze the binding in two layers, and made it cover more than names. A Profile
  may say `main`; `ProductPreflightBinding` records which commit that was, and
  `ProductAssemblyReceipt` adds the resolved mode and definition hash that only
  choosing a mode produces. Because a registry can rebind a preset without
  changing any name - and `workflow_definition_hash()` covers binding ids, not
  resolved specs - the binding also carries host-supplied `role_assembly_digest`
  and `router_assembly_digest` over what was actually resolved. Because the
  promotion target's expected revision is part of the binding, a task whose
  target ref moved fails closed and must be re-opened rather than silently
  re-basing.
- Froze what a person confirmed into the stream: `product/task-opened` and
  `product/task-started` both record `preflight_digest`, opening also records the
  exact confirming Session, Turn and message, and
  `ProductAssemblyReceipt.binds()` requires a started task to rest on that exact
  binding. `binds()` needs the Receipt, so only a Service can evaluate it; the
  repeated digest is what lets a pure projector compare the two facts. Without it the Proposal could show one commit and one promotion
  target, the world could move, and `product/task-started` could record a
  different receipt with nothing able to contradict it.
- Froze cross-event value consistency, not only order: `ProductTaskFacts`,
  `product_required_values()` and `product_started_mode()`. Where an earlier fact
  decided a value there is exactly one legal value for the later one, derived
  rather than proposed-and-checked - an explicit request is its own started mode,
  `auto` has none until routing produced one and cannot start before then, and a
  rejection must name the awaited review. `ProductTaskSummary.facts()` is the
  single assembly point. `product_started_values()` derives *every* value the
  started fact carries from one Receipt, so a payload cannot half-describe one
  binding and half-describe another; freezing only `mode` had left the run id,
  definition hash, assembly digest and base revision free.
- `ProductTaskView.status` is a computed property rather than a field, so a view
  can no longer carry a status that contradicts its own summary, and it is
  derived from all three fresh reads: `PRODUCT_TASK_COHERENT_WORKFLOW` freezes
  when the ProductTask and Workflow streams agree, yielding `resumable` for a
  clean Approval barrier, `unreconciled` for a lagging product stream and
  `interrupted` only where a person really has to look, and
  `PRODUCT_TASK_TRANSITIONS` is a read-only mapping rather than a `dict` an
  importer could rewrite. `ProductTaskSummary` carries `reason_display`, so the
  one thing written for a person to read actually reaches them.
- Froze the durable status *order*, not only the event shapes:
  `PRODUCT_TASK_TRANSITIONS` and `product_transition_allowed()` fix the allowed
  predecessors, forbid repeats and close every terminal. An explicit mode is
  never routed and `auto` can never skip routing; `completed` and `rejected` are
  reachable only from `awaiting_approval`, which obliges a resuming writer to
  reconcile a missing `product/task-awaiting` rather than skip it.
  `product_view_status()` derives `interrupted`, which is the only condition
  under which writing `product/task-abandoned` is legitimate.
- Froze the temporary Proposal: `ProductTaskProposal`, `ProposalConfirmation`
  and `proposal_confirmable()`. One active Proposal per Session, confirmation
  names the exact id plus the confirming Session, Turn and message, the
  confirmation must come from the Proposal's own Session, and the confirming
  Turn must differ from `proposed_turn_id` - the Turn that made the offer, which
  is routinely *not* the Turn the requirement was stated in, so comparing the
  latter still let a model propose and confirm in one breath. A Proposal carries a `ProductPreflightBinding` rather than a
  full receipt, because an `auto` Proposal has no resolved mode yet.
- Added `tests/test_product_contract.py` (`72 passed`), including a byte pin on
  `agent_loop.py`, `agent_runtime.py`, `supervisor.py` and `manager.py`. Six
  reverse validations confirmed each guard fails for its own root cause; none of
  them touched a protected file, and two were re-done after the first attempt
  produced only a fixture `TypeError` and a collection `ImportError` rather than
  the real root cause. A third review round added six more guards and six more
  reverse validations; one of them showed a mutable transition table leaking a
  rewrite into unrelated cases.
- See [ADR-0032](docs/adr/0032-unified-chat-product-task-surface.md).

### v0.7-E: fixed typed Workflow above the public services

- Added `traceh.api.workflow` (frozen host values, five node kinds and the host
  binding-resolver seam) and `traceh.workflow` (definition freezing and derived
  identities, the orchestration event vocabulary, one projector, the five node
  executors and a single-flight coordinator). `AgentLoop`, `AgentRuntime`,
  `ProcessAgentSupervisor` and `PluginManager` are unchanged, and an
  architecture test asserts the domain reads no private Supervisor state.
- Added one append-only `workflow:<run_id>` stream per run carrying seven
  schema-1 orchestration facts. One projector rebuilds the run on every load;
  there is no status file, result cache or second store, and the stream keeps
  identities pointing at the Directory, Session, Artifact Catalog and promotion
  ledger rather than copies of their state. Map child ids are re-derived during
  replay, so a forged child id is refused.
- Added a fixed DAG that is fully validated before anything runs: duplicate
  ids, unknown predecessors, self-edges, cycles, unreachable nodes, bounded
  node/predecessor/fan-out counts and typed cross-references. Definitions carry
  host binding ids rather than specs, prompts, paths or policy, and bind to a
  canonical definition hash that distinguishes `True` from `1`.
- Added stable identity for every side-effecting call: Agent, session, create
  request, message, review request and map child all derive from the run and
  node, never from scheduling order. Re-entry replays the Directory and Inbox to
  decide whether to create or resume and whether the message still needs
  sending, instead of repeating the side effect.
- Added the five node contracts: AgentTask converges its Activation, Workspace
  and process slot in a `finally`; Map appends its expansion before any child
  starts; Join waits for every map child; Verification binds the exact Artifact,
  target and Review; Approval is a human barrier the Workflow cannot cross and
  requires an approval covering that exact review and artifact.
- Added narrow recovery: only a run stopped cleanly at a human Approval barrier
  may continue. A node with a start fact and no terminal fact fails closed,
  because it could have left an Agent claim, an open Turn, a Budget hold, a
  provisional Workspace, a running capture or a running Review behind.
- Cancellation converges on one owned task per run, independent nodes run with
  structured concurrency, failures are collected per node so two nodes raising
  the same exception object are two failures, and stream appends reuse the
  existing three-state may-have-committed reconciliation. The multi-failure
  composition rule moved to `traceh.concurrency` so D2 and E share one copy.
- Every composed service must write to the one durable log the run uses,
  resolved through the same `durable_log_identity` helper the Budget, Artifact
  and Promotion domains already use; two logs cannot check each other.
- Re-entry matches complete durable facts, not just derived ids: an existing
  Agent must carry the create fact this node would have written (session,
  request, preset, not owned by or forked from another Agent), and an accepted
  message must match content, source and both correlation fields.
  `workspace_id` is excluded, because a workspace-managing Supervisor legitimately
  rewrites it and the Workflow does not own that decision.
- A terminal fact can no longer redefine the node it ends: its kind and map key
  must equal the started ones, and `run(definition)` additionally requires every
  node in the stream to be declared by that definition (or be a map child of a
  real Map) with the kind the definition gives it.
- A failed run records `run-finished(failed)` before node errors reach the
  caller, so a later `resume()` cannot supply the missing terminal itself and
  look like a legitimate continuation. A cancelled node still writes no run
  terminal and stays un-continuable.
- `state()` now refuses a definition whose hash the run never recorded, matching
  `start()` and `resume()`.
- Re-entry comparison goes through the protocols' own `creation_matches()` and
  `acceptance_matches()` rather than a hand-written subset, so
  `capability_grants`, `target` and `wakeup` all participate: an Agent with
  different grants, or a message accepted without the required wake-up, is no
  longer adopted as this node's own work.
- A completion must carry the evidence its node kind produces and only that, and
  its Agent and message ids are recomputed from the run and node - an AgentTask
  can no longer complete with no Agent evidence while holding a foreign
  Artifact.
- A run may report `completed` only when every declared node, plus every child of
  every expanded Map, is durably completed; a lone `run-finished(completed)` can
  no longer claim success for a DAG that never ran.
- If the failed-run terminal cannot be written, the node failures and the write
  failure are composed through the shared rule instead of the bookkeeping error
  replacing the root cause.
- Fixed a v0.7-D2 verifier defect found while investigating a recurring
  full-suite failure: the output capture accounted whole read chunks, so the
  recorded size and digest depended on how the pipe split the stream, and at the
  largest legal bound the crossing chunk pushed `stdout_bytes` past what a
  `VerifierOutcome` may carry - turning "output exceeded" into a leaked input
  error. The capture now accounts exactly the first `max_output_bytes` of each
  stream and keeps draining past that so the child can still exit, making the
  evidence chunk-invariant and always within the recordable range.
- Completion evidence is checked in two layers by one shared rule. `rebuild()`
  enforces everything a stream can decide alone - a Join carrying no Artifact,
  Review or digest, a Verification carrying both, an Approval adding the digest,
  and Agent/message ids recomputed from the run and node - so the public
  Projector still fails closed with no definition in hand. `run(definition)`
  then adds the constraints only the definition supplies: `capture_artifact`,
  with a Map child following its parent's setting.
- Map fan-out is closed at the replay boundary too. `rebuild()` now requires
  that only a running Map parent records an expansion - a Join can no longer
  record one and report map keys it never produced - that a child id and its key
  correspond exactly, that a node nobody expanded carries no key, and that one
  child belongs to one expansion and cannot appear before it. The definition
  layer keeps what only it can decide: whether the expanding node is a Map in
  that definition, and whether a separately declared node may carry a key.
- Recorded the decision and boundaries in ADR-0031. v0.7-E adds no CLI, no
  model-visible workflow/approve/promote/capture Tool, no retry policy, no
  conditional or loop node, no cross-process lease, no cold Activation recovery
  and no OS sandbox; version remains `0.6.0`. The three dedicated Stage E files
  contain 85 tests (all passing). After committing the verifier fix, the
  recursive L2 gate passed from the new core commit and the complete suite is
  `2093 collected / 2088 passed / 5 skipped`.

### v0.7-D2: fixed verification, human approval and Git ref compare-and-swap promotion

- Added an independent promotion control plane outside the execution kernel:
  `traceh.api.promotion` (frozen host values and the target-resolver seam) and
  `traceh.promotion` (identities/digests, events, one projector, the fixed
  verifier runner, all Git effects and the three transactions). `AgentLoop`,
  `AgentRuntime`, `ProcessAgentSupervisor` and `PluginManager` are unchanged and
  no module outside the domain imports it.
- Added one append-only `patch-promotions:ledger` carrying exactly
  `patch/review-recorded`, `patch/approval-recorded` and
  `patch/promotion-committed` at schema 1. A single projector rebuilds Review,
  Approval and Promotion on every load and recomputes the review id, evidence
  digest, `passed` flag, approval digest and promotion id instead of trusting the
  payload. Sequence gaps, unknown schema/event types, unexpected keys, duplicate
  identities, approvals of failed reviews and promotions that contradict their
  Review are refused. No repository path, verifier output or environment value
  enters the log.
- Added fixed host verification: a frozen `VerificationPlan` validated once at
  the public boundary (exact int/bool types, bounded argv/timeouts/output, unique
  command ids, no `GIT_*` in the environment policy). Commands run by argv with
  no shell, in a positive-list environment, and record only command identity,
  status, exit code and the SHA-256 plus byte size of each output stream.
- Added deterministic integration: review clones a host-configured **bare**
  target into a temporary directory, pins HEAD to the exact expected revision,
  applies the exact CAS-verified Patch with `git apply --cached` (never
  `--3way`), and produces an integration tree and single-parent commit whose
  parent, tree, message, author, committer and timestamp are protocol constants
  or approved inputs. Review never updates a target ref and leaves no object in
  the target repository; a failing verifier still produces a durable
  `passed=False` report that can never be approved.
- Added an explicit human approval API bound to the exact content digest of a
  freshly replayed Review — Manifest, target identity/ref/expected revision,
  integration tree/commit, verifier definition digest, evidence digest and merge
  policy version. There is no `approved=True` form, no CLI and no model-visible
  approve/merge/promote/`update-ref`/capture Tool. `operation_id` is exactly
  idempotent and any different payload conflicts.
- Added safe promotion: fresh replay, fresh Artifact verification, fresh target
  resolution, reconstruction of the identical tree and commit through a
  temporary index inside the target's own object database, and a single
  linearization point at `git update-ref <ref> <new> <expected-old>`. Git
  mutation and Event append are reconciled across three ref states
  (approved-new, expected-old, anything else); a failed, cancelled or unknown
  append never implies that Git did not move, and a bounded retry keeps an
  already-durable ref update from becoming an unrecorded one.
- Hardened the boundary: every inherited `GIT_*` variable is removed before the
  host's own Git controls are added, targets must be bare repositories with no
  reparse component, refs are restricted to validated `refs/heads/*`, owned tasks
  converge under repeated cancellation, and scratch directories converge on
  success, failure, cancellation and cleanup failure without masking the primary
  error.
- Bound the evidence to what was actually verified: the integration worktree is
  proved by hashing the filesystem, so neither an unstaged edit, a
  candidate-supplied `.gitignore` rule nor an `--assume-unchanged` index flag
  can hide what a verifier really executed. The checkout is additionally proved
  to *be* the integration tree - each file hashed as a Git blob and compared
  against the tree's own object ids - because `checkout-index` converts line
  endings under `core.autocrlf`/`core.eol` or a candidate-supplied
  `.gitattributes`, which would otherwise let a review approve LF bytes a
  verifier never ran. Configuration-driven conversion is suppressed and
  attribute-driven conversion fails closed; verifiers are granted owned scratch
  space outside the checkout, and a failure to remove it is reported rather than
  silently ignored. When several things fail at once - the work itself, the
  removal, and a cancellation - the caller sees its own `CancelledError` with
  every other failure retained as the cause, both together as a
  `BaseExceptionGroup` when both occurred. That composition lives in one shared
  helper used by both the integration and verifier scratch lifetimes. Git's executable
  bit is compared where the platform can store it; on Windows, which cannot, the
  tree's own mode is carried through and a mode change is still caught by the
  Git-side tree re-derivation. The
  output limit is enforced while a command runs, and the deadline covers the
  output readers as well as the direct child, so an orphaned grandchild holding
  an inherited pipe cannot extend or remove the bound the host set. Every
  returned result is matched in order against its frozen command before the
  evidence digest is recomputed, review/approval idempotency binds the complete
  operation definition instead of the identity alone, and reading a hostile
  event envelope raises the stable protocol error without swallowing
  `BaseException`.
- Recorded the decision, threat boundary and rejected alternatives in ADR-0030.
  D2 adds no CLI, Workflow, automatic approval, automatic target selection,
  non-bare target, multi-parent merge, object/CAS garbage collection,
  cross-process lease or OS sandbox; version remains `0.6.0`. The four dedicated
  D2 files contain 130 tests (`129 passed, 1 skipped`); the expanded
  Artifact/Workspace gate is `172 passed, 2 skipped`, and the complete gate is
  `2005 collected / 2000 passed / 5 skipped`.

### v0.7-D1: immutable Patch Artifact capture

- Added an independent Artifact domain with one append-only
  `artifacts:catalog`, immutable schema-1 Patch Manifests and an explicit
  SHA-256 content-addressed store for raw Patch bytes. Manifests bind exact
  Agent, Session, message, Turn, Workspace generation, repository fingerprint,
  base/head/candidate tree and changed paths without persisting host paths.
- Added full managed-worktree capture through a temporary Git index. Staged,
  unstaged, untracked, deleted, binary and executable-mode changes become one
  candidate tree; capture rejects symlink/junction/reparse paths, gitlinks,
  `.gitmodules`, control paths, invalid modes, unsafe Unicode/case collisions
  and explicit size-limit violations without modifying the user's index.
- Added terminal durable-evidence validation, a Workspace-owned capture gate,
  before/after Git and evidence receipts, shared per-message capture tasks and
  cancellation-safe Catalog reconciliation. Drift fails closed and no Manifest
  is appended; a prewritten but unreferenced CAS blob is the only allowed
  residue after a later failure.
- Added fresh Catalog/CAS reading and an optional reporting adapter that only
  attaches already-recorded Artifact refs. `collect_agent_artifact` remains a
  pure read and never captures or modifies a Workspace.
- Hardened the trust boundary: Catalog builders and replay recompute both
  derived identities, CAS validates the complete root-to-Blob parent chain
  before directory creation/read/write, and Git capture removes every inherited
  `GIT_*` variable before adding only its controlled settings.
- Recorded the ownership and threat boundaries in ADR-0029. D1 adds no Patch
  Verifier, Review Report, approval, integration tree/ref promotion, CLI,
  model capture Tool, cross-process lease or OS sandbox; version remains
  `0.6.0`. The four dedicated D1 files contain 40 tests (`39 passed,
  1 skipped`); the expanded Workspace/Supervisor/Tool gate is `82 passed,
  1 skipped`, and the complete gate is `1875 collected / 1871 passed /
  4 skipped`.

### v0.7-C: managed Git workspace lifecycle

- Added one append-only `workspaces:catalog` lifecycle with provisional,
  attached, quarantined and released facts. Workspace identity is correlated
  with the exact Agent creation request and Session workspace id; paths remain
  host-only and never become model-supplied authority.
- Added `LocalGitWorkspaceProvider`, which maps an explicit source id to a
  clean top-level repository, resolves one immutable commit and materializes a
  detached worktree beneath one managed root. Symlinks, junctions/reparse
  points, occupied paths, registry/HEAD mismatches and dirty cleanup are
  rejected or quarantined; each marker's absolute admin directory must also
  equal the unique registry entry that points back to that exact worktree, so
  swapping two otherwise valid sibling markers is rejected. No force removal
  or broad prune is used.
- Added a cancellation-safe `WorkspaceService` and
  `WorkspaceManagedAgentSupervisor`. The wrapper delegates execution and
  lifecycle to the existing public Supervisor, then reconciles the Directory
  and Session before attach. Resume validation remains wrapper-owned through
  its post-check, so close cannot return before it converges; Agent disposal
  preserves the worktree until an explicit host release/reject/merged decision.
- Added `ManagedWorkspaceAccessPolicy`: read-only Agents can use only pure and
  workspace-read Tools, while writes, processes, network and external effects
  are denied. This is an explicitly installed Tool capability boundary, not an
  operating-system sandbox.
- Moved generic direct-child process convergence to `traceh.process_control`;
  Tool output capture remains in `traceh.tools.process_control`. The old
  location is not retained as a compatibility alias.
- Recorded the ownership, path, Git, reconciliation and explicit threat
  boundaries in ADR-0028. Stage C adds no Patch Artifact, diff/merge,
  promotion, Workflow, Workspace CLI or distributed lease; version remains
  `0.6.0`. The five dedicated Stage C files contain 60 tests, with 10 more
  regressions added to existing files; the expanded
  Stage C/tool/cancellation gate is 84 passed, 2 skipped, and the complete gate
  is 1835 collected / 1832 passed / 3 Windows platform skips. Catalog operation
  receipts retain protocol constants rather than hostile envelope objects.

### v0.7-B: Budget enforcement at owned boundaries

- Added explicit host adapters for managed child creation, model/token usage,
  Step continuation, ordered Tool admission, active Turn wall time and
  process-local descendant slots. Existing Supervisor, Runtime and Tool paths
  remain the only execution paths; `AgentLoop` owns no Budget branch.
- Extended the single Budget ledger with reserve/start/settle/release usage
  facts. A START is a one-shot execution claim rather than an idempotent retry
  permission; failure, cancellation and uncertain Usage conservatively retain
  or consume capacity instead of repeating external work.
- Made the reserve/START append itself owned work. Cancellation after a child
  hold commits but before provision now releases it; cancellation after a
  Token/wall START commits but before Provider/Turn entry conservatively
  settles the full hold. Repeated cancellation cannot return before either
  terminal fact is durable.
- Added deterministic child-grant reconciliation against the Agent Directory,
  ancestor process leases, trusted/estimated/unknown token evidence, ordered
  batch Tool admission, durable Step reconciliation and monotonic wall-time
  finalization. Store/Session mismatches fail before work or clean the candidate
  before returning.
- Separated reserve-fact idempotency from child-creation permission. A
  `RELEASED` child reservation is now rejected before the inner Supervisor can
  create durable identity; `PENDING` remains the first-attempt permit and
  `COMMITTED` remains the exact durable-child retry path.
- Preserved falsey caller-supplied LLM runtimes through explicit `None`
  handling and required an exact boolean for the estimated-Usage policy, so
  Python truthiness cannot replace an injected mainline or weaken evidence.
- Recorded the ownership, cancellation and explicit-host boundaries in
  ADR-0027. At the Stage B checkpoint, default CLI grants, cross-process
  leases, hard-crash recovery for STARTED reservations, Workspace/Patch and
  Workflow remained future work; Stage C subsequently adds Workspace only,
  without changing the other Budget boundaries.
  Version stays `0.6.0`. The Budget suite is 79 passed, the expanded
  Composition/plugin set is 168 passed, and the complete gate is 1770
  collected / 1769 passed / 1 skipped.

### v0.7-A: append-only hierarchical Budget ledger

- Removed the unenforced v0.6 `Budget` field from Agent identity instead of
  retaining a compatibility DTO. New Agent facts use schema version 2; old
  schema-version-1 Budget histories fail explicitly and remain untouched.
- Added one global `budgets:ledger` fact vocabulary, immutable projector and
  host mutation service for root grants, child reservations, exact
  Directory-backed commit, converged release, usage charges and terminal
  accounts. Tokens, Steps, Tool calls, wall milliseconds and direct-child
  capacity are conserved without a mutable balance or Runtime cache.
- Added CAS, globally unique operation ids, immutable child/request
  correlation ids, canonical-JSON idempotency and three-state append
  reconciliation. Child count has one accounting path through reservations;
  process slots remain a Stage B process-local lease.
- Ordered cross-stream replay as Budget prefix then fresh Directory, preventing
  legal concurrent root/commit writes from being mistaken for corrupt history.
  Exact built-in integers are required so hostile numeric subclasses cannot
  leak caller-controlled comparison failures.
- Recorded the implemented protocol and its cross-stream/trusted-host boundary
  in ADR-0026. Stage A does not change `AgentLoop`, `AgentRuntime`,
  `PluginManager`, Supervisor scheduling, Tool schemas or the CLI; real
  enforcement remains Stage B. The Budget Ledger suite is 41 passed, the
  expanded identity/lifecycle/D0 set is 290 passed, and the complete gate is
  1732 collected / 1731 passed / 1 skipped.

### v0.7 D0: managed control-plane seams

- Changed `SupervisorToolset` to depend on the public `AgentSupervisor`
  protocol and added the store identity surface required to prove that Tool
  authority and control operations share one durable Event Log.
- Added a cache-free `AgentToolAuthority` that replays the Directory for each
  caller/strict-descendant decision, plus a mandatory host
  `ChildProvisioningPolicy` whose proposal is limited to preset, workspace
  intent and descriptive metadata. Task delivery remains a separate Tool and
  concrete Provider/model/prompt/runtime resolution remains in
  `AgentActivationFactory`.
- Recorded the v0.7 dependency/threat model and the intentional breaking
  hierarchical-Budget cutover in ADR-0024/0025. D0 adds no Budget event,
  Workspace, Patch, Workflow, Tool schema or CLI capability; version remains
  `0.6.0`.
- Added five architecture guards. The D0 + existing Tool/Supervisor targeted
  gate is 96 passed; the repository collects 1712 tests and the complete gate
  is 1711 passed, 1 skipped. Reverse checks prove the guards fail if the public
  Supervisor seam, mandatory policy call or fresh Directory replay is removed.

## 0.6.0 - 2026-08-23

### Release candidate acceptance

- Released the complete v0.6 Stage A-E Agent control plane: durable identity, FIFO Inbox,
  process-local Supervisor and delivery lifecycle, child-first ownership disposal, durable
  run reports and five host-bound model Tools. The implementation stays above the existing
  `AgentRuntime`/`AgentLoop` mainline and adds no parallel scheduler or mutable fact source.
- Ran a real OpenAI-compatible model through parent `spawn_agent` → `send_agent_message` →
  `wait_agent` → `collect_agent_artifact` → `stop_agent`. The child owned a distinct Session;
  the same durable identity was then explicitly resumed for another real model Turn, and a
  separately gated model wait converged through `interrupt()` to durable `cancelled` evidence.
  Directory, Inbox and Delivery protocol validation passed; both Sessions had closed
  Turn/Step lifecycles, zero invariant violations and zero request-reconstruction violations.
- Promoted the post-v0.5 L1-L4 controlled capability-evolution pipeline into the released
  surface. The Plugin Creator and Python Quality distributions are now `0.2.0`; the creator
  contract targets `traceharness-py>=0.6,<0.7`, while Python Quality declares its verified
  backwards-compatible `>=0.5,<0.7` window.
- The repository collects 1707 tests; the complete release gate is 1706 passed with the one
  documented Windows NUL-path skip, including recursive L2 validation and clean Wheel E2E.

### v0.6 Stage E: supervisor-backed subagent tools

- Added a host-wired `SupervisorToolset` with exactly five ordinary Tool Runtime
  capabilities: `spawn_agent`, `send_agent_message`, `wait_agent`, `stop_agent` and
  `collect_agent_artifact`. The tools delegate to the existing Supervisor, durable Agent
  Directory, Inbox, delivery ledger and per-Agent Session; they do not add a second scheduler,
  worker queue or lifecycle registry. See
  [ADR-0023](docs/adr/0023-supervisor-backed-subagent-tools.md).
- Bound every toolset to one owner Agent, one Supervisor and the same authoritative
  `EventStore`. At execution time the caller's Session must still be the owner's durable
  Session, and target operations authorize only strict durable ownership descendants. The
  model cannot choose an owner id, grants, budget or hidden Runtime object.
- Made mutating calls retry-safe from existing Tool context. Spawn and send derive stable
  request/message ids from the owner and durable Session/Turn/Step/Tool-call identity. The
  Supervisor records one waiter per shared create. A freshly installed Activation remains
  abandonable only until `create()`, `resume()` or wakeup publicly retains it under the same
  lock; cancellation can select cleanup only when the final waiter leaves and no supported
  public path has delivered the Activation. An authoritative task re-read that reuses an
  already durable identity is retained from the outset, even if the caller entered with a
  stale but valid Directory snapshot. Cancelling concurrent, later idempotent or overlapping
  resume/wakeup calls therefore cannot destroy an already delivered child. The pending receipt
  stays registered through compensation, so a late retry waits outside admission and reloads
  only after cleanup instead of receiving the handle being disposed. Public create invocations
  remain Supervisor-owned through this post-admission tail using an operation-level state and
  method-return receipt. A post-return completion Task atomically removes the registration and
  publishes that receipt under the Supervisor lock, so shutdown cannot miss an invocation that
  has not actually returned. Entering the synchronous return/raise boundary explicitly revokes
  permission to cancel the caller Task, even if early validation failed before owned work existed;
  shutdown therefore cannot inject cancellation into the caller's later error handling while it
  waits for the post-return receipt. `aclose()` joins those receipts without waiting for unrelated
  work in the caller Task. An explicit resource hand-off avoids a close/compensation wait cycle. Repeated
  cancellation waits the same cleanup Task; cleanup failure stays attached to a bare
  cancellation and remains reportable by Supervisor close rather than escaping as a
  `BaseExceptionGroup` and leaving an open delivery claim.
- Added `AgentRunReportReader`, which reconstructs join results from the durable Inbox,
  delivery and Session streams rather than a cached worker result. A completed report is
  accepted only when its message, claim, terminal outcome, Turn boundaries, reason and final
  assistant text agree. Failed, cancelled, unsettled and malformed evidence remain distinct
  outcomes. `collect_agent_artifact` currently returns this durable final text and evidence
  references; there is no invented patch-artifact store. `wait_agent` uses a per-message
  notification as a fast path and bounded durable re-read as the authority, so a later message
  cannot block an earlier completed join and a terminal written by another supported
  Supervisor cannot leave the waiter asleep. Hostile Session sequence fields are normalized
  to a stable evidence error, and missing/unsettled messages expose distinct fixed error codes.
- Kept orchestration above the core Runtime. `AgentLoop`, `AgentRuntime` and `PluginManager`
  have no Stage E changes; default CLI construction still does not silently enable subagents.
  Added `tests/test_agent_tools.py` (30), including a real
  AgentLoop -> ToolRuntime -> Supervisor -> child Session path and reverse checks for caller
  identity, message-scoped waiting across Supervisors, concurrent idempotent-create ownership
  and cancelled-spawn cleanup convergence, public resume/wakeup handoff, and operation-level
  close ownership, including the post-return registration handoff and an early-failure return that
  must not cancel later caller work. The repository now collects 1707 tests; Stage E is 30/30,
  the Stage A-E control-plane set is 545/545, and the complete suite is 1706 passed with 1
  platform skip, including the real recursive L2 validation and Wheel E2E gates.
- Released with `0.6.0`. Cold recovery, stale-claim takeover, hierarchical budget enforcement,
  managed Workspace isolation, retry policy, Workflow and a default product-level subagent
  configuration remain outside this release.

### v0.6 Stage D: lifecycle ownership and quiescent subtree disposal

- Added `AgentOwnershipGraph`, projected from the durable Agent Directory rather than kept as
  another mutable parent/child registry. It uses `owner_agent_id` only: message source and
  `forked_from_session_id` remain communication and history lineage, not cleanup authority.
  Deterministic post-order visits descendants before owners. See
  [ADR-0022](docs/adr/0022-agent-lifecycle-ownership-and-quiescent-disposal.md).
- Added process-local lineage admission. Child create, resume and wake cannot race through an
  intersecting subtree disposal; a new child requires a durable and live owner before any
  candidate Runtime is provisioned, and installation rechecks under the Supervisor lock.
- Changed `dispose(agent_id)` to own the complete lifecycle subtree. It registers the disposal
  scope first, cancels and joins matching in-flight create/resume candidates, waits older
  admissions to leave, re-reads the Directory to catch an identity that committed during
  convergence, then cleans the subtree child-first. Durable identity, Inbox and delivery facts
  remain untouched.
- Made overlapping parent/child disposal join one per-Agent cleanup Task, so cleanup happens
  once. One failed child cleanup does not skip siblings or the owner; failures are reported
  after every node has been attempted. Repeated cancellation still waits for convergence and
  then re-raises the original cancellation.
- Made `aclose()` permanently close admission, converge candidates and in-flight subtree
  disposal, and then release the complete durable ownership forest child-first. `interrupt()`
  remains Turn-only and does not silently become subtree shutdown.
- Hardened failure and retry edges found in independent review: an unpinned idempotent create
  retry is matched to subtree disposal by its durable `request_id`; malformed final Directory
  projection is reported only after every known process-local Activation/cleanup Task has been
  released; and close deduplicates repeated observations by cleanup Task source rather than by
  exception-object identity. The same Task is reported once, while two independent Tasks that
  raise the same object remain two failures. Creating the close Task now also atomically claims
  every registered tree disposal: a cancelled public disposer cannot remove that registration
  before close observes its result and therefore cannot make a real shutdown failure disappear.
- Kept the lifecycle control plane in `traceh.supervision`; `AgentRuntime`, `AgentLoop` and
  `PluginManager` have no Stage D changes or ownership state. Added
  `tests/test_agent_lifecycle.py` (20); the repository now collects 1677 tests, with 1676 passed
  and 1 platform skip. Reverse checks prove owner-first cleanup, late owner validation,
  fail-fast cleanup, malformed-directory leakage, unpinned retry escape and duplicate failure
  reporting, over-broad exception-object deduplication and close-period tree deregistration are
  each caught by the new tests.
- Version remains `0.5.0`. Stage D is not a v0.6 release: there is still no model-visible
  subagent tool, cold recovery, cross-process Activation lease, retry policy, Workspace,
  hierarchical budget or Workflow.

### v0.6 Stage C: process-local Agent Supervisor and delivery lifecycle

- Added `traceh.supervision`: a durably accepted `NEW_TURN` message now becomes exactly one
  claim, one Turn on that Agent's own Session, and one terminal fact. See
  [ADR-0021](docs/adr/0021-process-local-agent-supervisor-and-delivery-lifecycle.md).
- Added a separate `agent-delivery:<agent_id>` stream holding `agent/message-claimed` and
  exactly one of `agent/message-completed`, `agent/message-failed` or
  `agent/message-cancelled`. It is not the Inbox stream, so acceptance history stays a plain
  answer to "what was received" and claims do not contend for the `expected_seq` senders use.
  A claim carries the Inbox `accepted_seq` as well as the `message_id`, so replay can prove
  the two streams agree about which message is running.
- **Nothing executes before the claim is durable.** `AgentDeliveryService.claim()` returns only
  when the claim is provably in the log; every other outcome raises, including unknown. An
  unknown claim faults the Activation instead of retrying, because retrying is what could
  double-execute and no ledger correction undoes a tool that already wrote to a workspace.
- Kept no in-memory queue: the worker re-reads the Inbox and the delivery log every round and
  takes the earliest unclaimed message. FIFO is strict and a bad or undeliverable message is
  never skipped. A claim without a terminal outcome blocks every later message rather than
  being treated as completed or eligible for an undeclared stale-claim takeover.
- Made `AgentDeliveryLog` fail closed against the Inbox it references - unknown event types,
  wrong schema version, inexact key sets, wrong stream, wrong Agent, a claim on a message that
  was never accepted, an `accepted_seq` that disagrees with the Inbox, a second claim on one
  message, a duplicate `claim_id`, a terminal fact with no claim, and a second terminal fact on
  one claim. Reading an event is itself untrusted: type, stream, schema version and payload sit
  inside one exception boundary that catches `Exception` and never `BaseException`.
- Claim and terminal transactions now re-read the authoritative Inbox and delivery stream
  before append and prove the complete Acceptance/delivery view/open Claim belongs to the
  requested Agent. Fabricated DTOs, cross-Agent views, stale or fabricated delivery facts and
  foreign terminal claims fail before writing anything.
- Recorded stable repository codes on failure and cancellation, plus the real `turn_id` on
  completion. Exception text, tracebacks and model output stay in the Session Event Log; they
  are arbitrary third-party output that may quote a request, a path or a credential.
- Added `TurnInput` to `traceh.api` (content, `message_id`, source) so a Turn is addressable.
  `AgentLoop.run_turn()` previously minted its own `message_id` and stamped `source="user"`,
  which left no way to say which Session Turn ran which durable message except by comparing
  text. Passing a plain `str` keeps the previous behaviour exactly, and `AgentLoop` still
  imports nothing about Agents, Inboxes or Supervisors.
- Kept the Supervisor out of `AgentRuntime`. It reaches a runtime through a four-method
  protocol - run one message, cancel the current Turn, dispose, expose Session and
  `EventStore` identity - never reads runtime internals, and neither `AgentRuntime` nor
  `AgentLoop` knows the package exists. The store is compared by object identity, resolving
  only `PublishingEventStore`, since two identically configured stores are still two logs.
- Enforced one Activation per Agent and per Session within a Supervisor: concurrent `resume()`
  calls join one in-flight build, so the factory runs once. Wake and idle are set under one
  lock and the worker clears its wake flag *before* draining, so a request that lands during a
  drain cannot be lost.
- Documented that `create()` spans `session:<id>` and `agents:directory` without a transaction.
  It provisions the Session first and appends identity second, because an unreferenced Session
  is detectable and inert while an `AgentRecord` pointing at a missing Session is unusable; any
  failure or cancellation, including an unknown identity append, disposes the candidate
  runtime. The log is never rewritten to fake atomicity.
- Bound create single-flight to the complete frozen request rather than `request_id` alone.
  Existing durable requests are reconciled again through `AgentRegistrar`; concurrent calls
  with a different preset, scope, budget or pinned identity are rejected instead of receiving
  another caller's Agent, and the factory receives a detached spec it cannot use to mutate the
  identity request across an await.
- Gave `send()`, `interrupt()`, `wait_idle()` and `dispose()` real semantics. `wakeup=False`
  accepts durably and starts nothing; a wake that fails after acceptance raises
  `MessageWakeError` carrying the `MessageReceipt`, so a retry does not append the message
  again. `interrupt()` cancels only the current Turn and the Activation keeps draining.
  `wait_idle()` waits for what was scheduled and reports a faulted Activation instead of
  hanging. Worker/store exceptions become stable Activation faults rather than successful
  idle. `dispose()` and `aclose()` own in-flight create/resume candidates as well as installed
  Activations, run through shared internal Tasks, converge through repeated cancellation and
  preserve cleanup failures; `AgentRuntimeExecution` replays one failed cleanup instead of
  silently succeeding on retry. Shutdown deletes nothing durable.
- Reconciled the exported `AgentSupervisor` Protocol with the concrete implementation:
  explicit `request_id`, optional explicit Agent/Session ids, `interrupt() -> bool`, and
  `aclose()` now form one public contract.
- Refused `MessageTarget.NEXT_STEP` in `send()` before acceptance, writing no events, since a
  Step has a frozen Composition and an in-flight model call. A `NEXT_STEP` message written
  directly through `AgentInboxService` is claimed and recorded as `failed`/`unsupported-target`
  rather than skipped, which would silently reorder the FIFO.
- Added `tests/test_agent_delivery.py` (73) and `tests/test_agent_supervisor.py` (61). Stage A
  (214) and Stage B (147) suites still pass; the repository now collects 1657 tests, with
  1656 passed and 1 platform skip. Concurrency tests use `asyncio.Event`,
  gates and real append latches rather than `sleep()`.
- Version remains `0.5.0`. Stage C is not a v0.6 release: there is still no cold recovery,
  stale-claim takeover, retry policy, subagent tool, parent/child disposal, workspace,
  hierarchical budget or Workflow.

### v0.6 Stage B: durable Agent Inbox acceptance

- Added a durable, append-only FIFO acceptance history per Agent. `AgentInboxService.accept()`
  appends `agent/message-accepted` to that Agent's own `agent-inbox:<agent_id>` stream in the
  existing `EventStore`, and `AgentInbox` rebuilds the same order from the log alone. No
  in-memory queue, no second fact source. See
  [ADR-0020](docs/adr/0020-durable-agent-inbox-acceptance.md).
- **Accepted is not processed.** The protocol deliberately cannot express delivery, claiming,
  execution, completion, failure or retry; `wakeup` records the sender's request rather than
  waking anything. There is still no `AgentSupervisor`, Activation, Turn scheduling or cold
  recovery, and `AgentLoop`, `AgentRuntime` and `PluginManager` hold no Inbox state.
- Gave writer and replay one rule set in `inbox_identity.py`: an exact eight-key payload,
  `schema_version == 1`, and a stream id built forward from the payload's `agent_id` by the
  single constructor rather than parsed back out of the stream name.
- Treated `content` as prose, not an identifier: multi-line text is legal, but it is bounded by
  `MAX_MESSAGE_CONTENT_CHARS` and must be UTF-8 encodable, because a lone surrogate survives
  `json.dumps` and then raises `UnicodeEncodeError` inside `JsonlEventStore.append()`.
- Required `target` to be a real `MessageTarget` value and `wakeup` to be strictly `bool`.
  Truthiness would read `1`, `"false"` or `[]` as a decision that will later start an
  Activation.
- Made acceptance idempotent by caller-supplied `message_id`, comparing every field against the
  frozen payload; the same id with a different message is rejected rather than treated as an
  update. The target Agent must already exist in the Stage A directory.
- Used one lock per Agent rather than one per service, since each Agent has its own stream and
  therefore its own compare-and-swap; the append carries `expected_seq` from the Inbox read.
- Extracted `commit_reconciliation.py` so both control-plane transactions share one answer to
  the `EventStore` commit-point question, with `True`/`False`/unknown preserved. Each keeps its
  own error mapping. `AgentRegistrar` moved onto it with no behaviour change and its full
  contract suite unchanged.
- Matched commit reconciliation against the complete frozen fact rather than the id alone, in
  both control-plane transactions. Two writers racing on one `message_id`/`request_id` write
  different facts, and matching on the id told the loser its own event had been recorded when
  what landed was the winner's. The candidate is now parsed through the projector - which also
  re-checks stream, schema version and key set - and compared field by field; a malformed
  unrelated event is skipped rather than making the answer unknown.
- Compared reconciliation candidates as canonical JSON rather than with `==`. Python equality is
  not JSON identity - `True == 1`, `1 == 1.0` and `[True] == [1]` all hold - so `{"flag": 1}`
  matched a racing writer's `{"flag": True}` and was reported as our own committed fact.
- Kept the third commit state reachable through the comparison itself. Only a protocol error
  justifies "not ours"; a canonical-encoding failure means the comparison could not be made, so
  it now propagates and the shared reconciler reports `None` instead of being swallowed into
  `False` - which had claimed "provably not committed" about an event already in the stream.
- Extended the protocol-error boundary to the whole event, not only its payload. `EventEnvelope`
  is a public DTO, so `event.type`, `event.stream_id` and `event.schema_version` are as
  untrusted as `event.data`; a `str` subclass with a raising `__ne__` leaked a bare exception out
  of all four public replay entry points. Neither `_scan()` pre-checks the event type any more,
  since that read would sit outside the parser's boundary.
- Put the whole persisted-payload read inside one protocol-error boundary in both projectors.
  `set(data)`, `data.get()` and `data[key]` run against a container the store handed back, so a
  `dict` subclass raising from any of them leaked a bare exception out of `rebuild()` and out of
  `validate_agent_inbox_events()`/`validate_agent_directory_events()`, whose contract is to
  return issues rather than raise. `SystemExit` and `KeyboardInterrupt` stay uncaught.
- Added `AcceptedMessage` to `traceh.api.agents`; `AgentMessage`, `MessageTarget` and
  `MessageReceipt` already expressed the contract and were not modified.

### v0.6 Stage A: durable Agent identity and the Activation boundary

- Added `traceh.agents`, a multi-agent control-plane fact layer that records and rebuilds
  durable Agent identity from the existing `EventStore`. `agent/created` is appended to one
  `agents:directory` control-plane stream, separate from every Session Stream; no JSON file,
  SQLite table or process-global registry is introduced. See
  [ADR-0019](docs/adr/0019-durable-agent-identity-and-activation-boundary.md).
- Added `AgentRecord` to `traceh.api.agents` as the durable identity DTO, and corrected the
  stale "v0.3 does not ship an AgentSupervisor" module docstring. `AgentHandle` and
  `AgentSupervisor` are now documented as the Activation-side protocols they are, still with
  no implementation behind any method.
- Added `AgentDirectory`, a read-only projector supporting lookup by `agent_id`, by
  `session_id` and by `request_id`, plus an ownership-only `children_of()`. Duplicate
  `agent_id`/`session_id`/`request_id`, malformed payloads, unknown event types on this
  stream, self-ownership and dangling `owner_agent_id` all fail closed with stable codes;
  broken records are never silently skipped and there is no last-write-wins semantics. Error
  messages are fixed repository text and never echo a rejected value.
- Added `AgentRegistrar`, the creation transaction. Its linearization point is the append's
  `expected_seq`, carried from the directory read rather than re-read at append time; a
  registrar-local lock only closes the read-then-append window and never decides whether a
  write succeeded. A required caller-supplied `request_id` makes retries idempotent, and
  reusing one for a different identity is rejected.
- Made the uncertain-append outcome explicit rather than assumed. A failed or cancelled
  append re-reads the stream by `request_id` instead of presuming nothing was written;
  cancellation is always re-raised unchanged, `AgentCreationError` reports whether the append
  committed, and `AgentDirectoryConflictError` promises nothing was. The reconciliation read
  converges through `await_worker_convergence()`, so repeated cancellation cannot release the
  caller early and no Task outlives the call.
- Kept history lineage (`forked_from_session_id`), lifecycle ownership (`owner_agent_id`) and
  communication as separate relations. Communication has no field in the creation fact at all.
- Gave the write path exactly the rules replay applies. `validate_spec()` validates the budget
  mapping that would be persisted, not merely that the value is a `Budget`, and rejects
  booleans, negatives and NaN/±Infinity. A looser writer is not a weaker check but a way to
  append a fact that can never be read back: one bad budget previously committed and then
  failed every later rebuild *and* every later creation for that store.
- Gated `agent/created` on `stream_id`, `schema_version == 1` and an exact payload key set, so
  an event from a newer writer, or an identity fact read out of a Session Stream, fails closed
  instead of being read as a complete v1 identity.
- Made `AgentCreationError.committed` three-state (`True`/`False`/`None`). A reconciling read
  that cannot answer now reports unknown instead of asserting nothing was written, and
  `AgentDirectoryConflictError` is used only when the re-read proved that.
- Stopped rewriting direct `BaseException` interrupts into domain errors. Only `CancelledError`
  gets convergence handling; `SystemExit` and `KeyboardInterrupt` propagate untouched.
- Made every `AgentDirectory` lookup return a detached record. `frozen=True` is shallow, so
  returning the retained object let a caller write through `metadata` and change what every
  later query on that directory answered.
- Closed the other entrance to the same boundary: parsing `agent/created` now deep-copies and
  normalizes the metadata graph, so a directory owns its graph from the input events onward.
  Copying on the way out cannot repair a reference kept on the way in.
- Froze the complete creation payload before `create_agent()`'s first suspension point.
  `AgentSpec` is frozen but its `metadata` is not, so a caller mutating it while the
  transaction awaited the directory read previously decided what got persisted; conflict
  checks, the append and `request_id` reconciliation now read only that snapshot.
- Validated the whole metadata graph before the append. A `set` nested inside metadata used to
  pass validation and fail later inside the store, surfacing as `AgentCreationError` instead of
  a pre-write `AgentIdentityError`.
- Bounded metadata graphs. `to_json_value()` recurses, so cyclic or extremely deep metadata
  raised a bare `RecursionError` out of both `create_agent()` and `AgentDirectory.rebuild()`,
  at a depth decided by `sys.getrecursionlimit()`. Normalization now walks the graph within
  `MAX_METADATA_DEPTH`, rejects containers that reappear among their own ancestors, and also
  encodes, with the key scan, the bounded walk and the encode all inside one boundary that
  catches `Exception`. Traversal is itself untrusted work - a `dict` subclass overriding only
  `values()` or `__iter__` breaks the walk while remaining encodable through `items()` - so a
  guard outside that boundary leaked a bare exception on both entrances. `KeyboardInterrupt`,
  `SystemExit` and `CancelledError` are deliberately not caught.
- Made the exported `agent_created_data()` raise on metadata it cannot carry. Its `or {}`
  fallback conflated "rejected" with "legitimately empty" and silently dropped the caller's
  data; an empty mapping still passes through unchanged.
- Bounded budget numbers to `2**53 - 1`. `10**10000` is a valid non-negative `int` that raised
  a bare `OverflowError` out of `float()`/`math.isfinite()` on both the write and replay paths,
  escaping the stable `agent-budget-invalid` outcome.
- Not included, and not to be described as delivered: a live `AgentSupervisor`,
  single-activation enforcement, Inbox, `send`/wakeup, subagent tools, parent/child disposal,
  workspace providers, Workflow, budget reservation and Agent cold recovery. `AgentLoop`,
  `AgentRuntime`, `PluginManager` and every Composition path are unchanged.

### Controlled capability evolution L4

- Added Runtime-external `traceh plugins promote` review/apply control. Review mode writes a
  Chinese capability/risk/evidence card and approval SHA-256 without changing the Registry or
  target. Apply mode requires that exact digest and rechecks the L2/L3 report bytes, audited Wheel,
  selected Registry, target Python identity, Distribution receipt and current managed state.
- Restricted promotion to internally consistent `improved` evidence with at least one improvement
  and zero regressions. The target must match L3's core and non-candidate Distribution receipt;
  apply installs only the exact SHA-256-addressed Wheel with index/dependency resolution disabled,
  rechecks the complete receipt, runs plugin doctor, and rechecks the receipt again so plugin
  import/health code cannot mutate the target before L4 records it as stable.
- Hardened isolated target inspection for virtual environments. The probe keeps the explicitly
  selected venv executable path (including POSIX `bin/python` symlinks), derives its prefix from
  adjacent `pyvenv.cfg`, and supplies that prefix to `sysconfig` under `-I -S`; it therefore reads
  the selected environment without running candidate startup hooks or leaking into the base Python.
- Added a shallow cross-process-locked promotion Registry with fsync/atomic state updates,
  immutable Wheel/record/receipt storage and `stable / installing / rollbacking` transitions.
  Unmanaged installs, stale approval, target drift, duplicate current artifacts and corrupted
  records fail closed.
- Added explicit `traceh plugins rollback`. It restores the previous exact managed Wheel or
  uninstalls a first promotion, requires the exact current/unfinished promotion id, and can
  converge hard-crash states. Ordinary failure, report failure and cancellation during apply
  restore the previous state before the caller returns; repeated cancellation cannot abandon the
  rollback task.
- Hardened L4 evidence and target ownership: promotion reconstructs the complete canonical L3
  Case/gate/dependency report, uses one canonical target-environment lock and Owner across
  Registry paths, interpreter aliases, plugin ids and Distributions, rejects control paths inside
  the target, and hashes all installed-package files around doctor so same-version or unrecorded
  file drift rolls back. Because each package state stores the full environment receipt, L4 v1
  admits one active managed Distribution chain per target until complete rollback releases it.
- Validate L3 `failure_codes`, `improvements` and `regressions` member types before duplicate
  detection, so malformed but valid JSON produces stable evidence errors instead of leaking an
  unhandled `TypeError` from `set(...)`.
- Made the package coordination lane independent of `TEMP` by placing its owner/lock namespace
  beside the canonical target environment. Explicit rollback can also converge the first-install
  crash window after owner/record persistence but before the initial `installing` state.
- Kept all package-management and approval logic under `traceh.evolution`; AgentRuntime,
  AgentLoop, PluginManager, Session/Event facts and the running plugin-composition path are
  unchanged. See ADR-0018.

### Controlled capability evolution L3

- Added `traceh plugins compare` as a Runtime-external baseline/candidate comparison control
  plane. It accepts only a successful canonical L2 evidence bundle, reuses its exact audited Wheel
  bytes and core commit, and requires an explicit fixed suite and dependency source.
- Added one bounded, all-Wheel dependency freeze before the two temporary environments. Both arms
  install offline from the same SHA-256-addressed Wheel set, must expose identical Distribution
  receipts before execution, and are rechecked after candidate code; only the candidate arm enables
  the exact target plugin identity. The local offline policy also reaches nested Tool/Verifier pip
  calls as one canonical percent-encoded local `file://` URI. The sanitizer rejects raw paths,
  whitespace-separated values, remote hosts, queries and fragments, so a Wheelhouse path containing
  spaces remains one pip source and cannot be extended with a remote find-links value.
- Hardened the host-owned probe so a normal return is not enough evidence: the matching durable
  Turn and Step lifecycle must be closed, durable reason/Step count must agree with the call result,
  and every in-Turn Composition Snapshot must contain the expected arm plugin identity.
- Added bounded deterministic metrics and the classifications `improved`, `regressed`, `mixed`
  and `no-change`. Reports are atomically committed and deliberately contain no approval,
  promotion, installation or rollback authority; those remain L4 responsibilities.
- Added the repository-owned three-case Python Quality v1 suite and negative coverage for invalid
  L2 gates, digest drift, open durable lifecycle, reason disagreement, arm identity mismatch,
  dependency/receipt drift, regressions, report commit failure and repeated cancellation
  convergence. A real L2-to-L3 CLI acceptance run classified the candidate as improved (baseline
  2/3, candidate 3/3) with no regressions or protocol violations; it froze three Wheels and proved
  matching four-Distribution receipts.
- Propagated L2 wheelhouse settings into nested candidate/core subprocesses so an explicitly
  offline validation cannot silently return to a package index.

### Controlled capability evolution L2

- Added `traceh plugins validate` as a development control plane outside Runtime/AgentLoop. It
  requires explicit candidate/core/output paths and an explicit dependency source, clones the
  trusted core Git `HEAD`, and never treats dirty core files or candidate-authored reports as
  evaluator facts.
- Added clean candidate copying and strict Wheel audit for links, secrets, caches, bytecode,
  path hooks, Python startup hooks, unsafe members and undeclared top-level packages. Core and
  candidate contract checks run in separate temporary virtual environments.
- Added host-owned installed-metadata, plugin doctor, candidate pytest and trusted full-core
  regression gates. Candidate stdout/stderr is withheld; reports contain stable bounded Chinese
  evidence, and only all-gates success atomically publishes the exact audited Wheel and SHA-256.
- Added cancellation convergence for validator subprocesses and deterministic negative coverage
  for ambiguous identity, `.env`, stale build input, startup hooks, failed tests and premature
  artifact publication. A repository-external temporary Git snapshot completed all 13 gates.
- Hardened the L2 transaction after independent review: source copies reject Windows Junctions
  and other reparse points; Wheels cannot own standard-library, `traceh` or pytest control
  namespaces; compatibility uses the selected core clone's version; initial audit bytes are
  memory-anchored and rechecked after candidate execution; and Wheel/reports/diagnostics are
  exposed only by one atomic directory commit, so report failure leaves no orphan artifact.
- Fixed two environment-sensitive tests exposed by the clean venv: Windows may report a venv
  launcher PID different from the child interpreter's own PID, and a random temporary path may
  contain the generic substring `-q`; the tests now assert the actual ownership/security facts.
- Documented that virtual environments are not an OS sandbox and L2 is not comparison,
  approval, promotion, installation or rollback. See ADR-0016.

### Controlled capability evolution L1

- Added `traceh-plugin-creator-skill-plugin` as an independently buildable external Wheel. It
  exposes a short authoring Prompt and a `PURE_READ` guide Tool backed by packaged workflow,
  v0.5 SDK contract, generic package template and static candidate checklist resources.
- Kept candidate generation outside the Runtime control plane: the existing coding Tools write
  source in a dedicated Candidate Workspace; `AgentLoop`, `AgentRuntime`, `PluginManager`,
  EventStore and Generation semantics are unchanged. L1 never imports, builds, tests, installs,
  enables, commits or pushes the candidate, and marks its review card
  `UNVALIDATED (L1 SOURCE ONLY)`.
- Extended clean-venv Wheel acceptance to install and discover the creator distribution, run
  `inspect`/`doctor`, and call its guide through the normal AgentLoop, ToolRuntime, Event/Effect,
  Composition Snapshot, invariant and request-reconstruction paths while proving its workspace
  remains unchanged.
- Isolated every Wheel build from ignored worktree artifacts by copying only declared source
  inputs, then auditing archive members for bytecode, caches, old build trees and egg metadata.
- Corrected the packaged SDK contract: a named Verifier is captured by Generation and Step
  Lease, but is not a `CompositionSnapshot` field; observed results persist as
  `verification/result` evidence.

## 0.5.0 - 2026-08-20

### Release candidate and external Python Quality plugin

- Promoted the Stage A-D3 Generation, scoped overlay and execution-capability work to the
  released `0.5.0` version contract.
- Expanded the author-facing `traceh.plugins` SDK with Tool call, Policy, Middleware and
  Verifier contracts needed by independent distributions; added contract tests that pin the
  public import surface.
- Added `traceh-python-quality-plugin` as an independently buildable release-acceptance
  distribution. It contributes the read-only `python_project_info` Tool, Python quality
  guidance, the monotonic `python-environment-safety` Policy and the explicitly selected
  `python-tests` Verifier. Test execution is resolved from explicit project evidence and
  fails closed instead of guessing a runner.
- Extended the clean-venv Wheel acceptance to build the core, Skill example and Python Quality
  plugin as separate Wheels, install them offline, and prove Entry Point discovery, doctor,
  Policy denial, Tool execution, Verifier evidence, Composition Snapshot, invariants and
  request reconstruction through the existing runtime mainline.
- Added plugin installation/discovery guidance to Chat `/help` and the operator docs. Source
  archives now enumerate committed files through `git ls-files`, then byte-verify every ZIP
  member, so unrelated untracked notes and local artifacts cannot enter a release archive.

### v0.5 Stage D3 plugin execution capabilities

- Added reversible `PluginContext` registrations for Provider, Policy, Middleware and named
  Verifier capabilities. They use the existing private setup/conflict/health/publish
  transaction and transfer through `PluginActivationSet` into one Composition Generation;
  cancellation and failure use the same reverse rollback and Drain ownership as other plugin
  resources.
- Kept authority explicit: enabling a plugin does not select its Provider or Verifier. Custom
  Provider selection requires an explicit plugin and Model; named Verifiers use
  `verifier_name`, `--plugin-verifier` or `TRACEH_PLUGIN_VERIFIER` and remain mutually
  exclusive with direct/command verification. Missing selections and Provider/Policy/
  Middleware conflicts fail before health checks with stable structured codes.
- Moved Verifier execution inside the Step Composition Lease. Provider, ToolRuntime and
  Verifier now come from one Generation, so an in-flight Step cannot switch to a newer
  Verifier after plugin replacement.
- Deliberately kept EventStore outside `PluginContext`: it is the Runtime/Session
  process-lifetime fact source and cannot safely be owned by a retireable Step Generation.
  A future plugin boundary requires a separately pinned owner and Store lifecycle contract.
- Added real AgentLoop, tool admission, verifier event, replacement isolation, rollback,
  conflict and CLI selection tests.
- Closed the candidate contribution surface after setup, before conflict checks and health,
  so health code cannot add a late Policy, Middleware or other Composition capability. Policy
  overlay failures now retain the responsible plugin id.
- Captured Tool, Provider, Policy and Middleware names at registration and reject any later
  identity drift with `plugin-contribution-identity-changed`. Conflict checks and attribution
  use the captured transaction identity, while Tool/LLM reversal handles retain the original
  registry key so a mutable capability cannot bypass validation or strand cleanup by renaming
  itself during health.
- Added an immutable capability receipt at the public `PluginActivationSet` transfer boundary.
  `CompositionGeneration` revalidates the candidate before claiming it, so an Owned Task that
  mutates a Tool after `prepare_activation_set()` returns cannot split model-visible schemas
  from ToolRuntime lookup keys; frozen Tool/Provider names use registered Registry keys.
- Made activation and `PluginActivationSet` construction one transaction. If receipt or Scope
  validation rejects the hand-off before the caller receives a candidate, the temporary
  `PluginManager` converges Owned Tasks and reverse cleanup before returning. Repeated
  cancellation cannot escape that cleanup. Simultaneous transfer/cleanup failures are grouped
  with `BaseExceptionGroup`: ordinary `Exception` members still derive `ExceptionGroup`, while
  a direct `BaseException` cannot be replaced by a secondary grouping `TypeError`.
- Strengthened Generation identity validation: an ActivationSet-provided LLM registry must
  contain the selected Provider object. Legacy custom ActivationSets that do not expose a D3
  LLM registry continue to borrow the coordinator's core registry during replacement.

### v0.5 Stage D2 Tool, Prompt and Policy overlays

- Added explicit Application, Workspace, Preset and Agent bindings for Tool, Prompt and
  Policy composition. Resolution uses private forks and feeds one effective ToolRegistry,
  PromptAssembler and Policy tuple into the existing ActivationSet/Generation/Lease/Snapshot
  mainline; no parallel scoped runtime or new persistent fact was added.
- Added strict boolean replacement intent and stable same-layer/cross-layer conflict codes.
  Scope order is deterministic regardless of input order, failed resolution leaves caller
  inputs unchanged, and reversible Prompt replacement restores the previous section.
- Revalidate child Tool/Prompt overlays against staged application plugin contributions before
  health checks, preserving responsible plugin identity and transactional rollback. Plugin
  replacement retains the child composition blueprint and uses the candidate ActivationSet's
  resolved Policy tuple. Generation validation compares each ordered Policy by object identity
  so a caller-controlled `__eq__` cannot substitute different admission behavior.
- Added real AgentLoop Tool/Policy admission, request reconstruction, cross-Runtime isolation,
  public candidate preservation and reverse-validation tests. Plugin setup remains
  application-only and `PluginContext` was not widened to Policy or other deferred categories.

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
watching, scoped plugin setup, EventStore contribution, isolated plugins, multi-agent,
Workflow, MCP, TUI or streaming output.

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

### Additional v0.4 hardening

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
