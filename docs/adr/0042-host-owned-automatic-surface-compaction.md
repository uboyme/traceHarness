# ADR-0042: Host-owned automatic Surface compaction and exact replay

- Status: Accepted and implemented
- Date: 2026-09-04
- Stage: M3 automatic context compaction
- Supersedes: the v0.3 manual `surface/replace` format

## Context

`CompactionService.replace_through()` has existed since v0.3 as a manual,
human-authored operation. A long Session therefore grows its model request
without limit until a person notices and runs `traceh compact`. That is the
gap M3 closes.

Three properties of the existing code decided the shape of the fix.

**The old replacement protocol could not carry an automatic decision.** Format 1
recorded only `source_seqs`, a free-form message and a `through_seq`. It bound
no content digest, no policy, no summarizer and no cut semantics, so a durable
event could not answer "who decided this, against which exact history, under
which rule". It also wrapped the summary in `<compacted-summary>` XML, which an
untrusted summary can close and escape from.

**The old projection put the summary in the wrong place.** `SurfaceProjector`
sorted replacements by their own append sequence. A replacement is appended
*after* the history it replaces, so a summary of old turns landed behind newer
conversation. Manual compaction hid that because it usually ran on the whole
history at once; automatic compaction before every Turn would have placed the
summary behind the current user message on every request.

**There is no auditable model-call path a summarizer could use.** The only
auditable, budgeted, cancellable model dispatch in this runtime is the Session
dispatch permit: `SessionService.start_model_attempt()` freezes a
`request/snapshot` and `model/attempt-start` before `LlmAdmission.dispatch()`
may cross the provider boundary (ADR-0035), and `budgets/enforcement.py`
reconciles model spend against those exact Session events. Every
`request/snapshot` must also reconstruct as
`SurfaceProjector.project(events, through_seq=source_seq)` plus its Composition;
`verify_request_snapshots()` proves it. A summarization request is by
construction not that projection, so recording one would make request replay
fail for every Session that compacted.

## Decision

### 1. Compaction runs before a Turn opens, in the Turn owner

`AgentLoop.run_turn()` consults the compaction owner after `ensure_session()`
and before `inbox/accepted`. This is the only point that satisfies all three
requirements at once: `AgentRuntime` already guarantees a single active Turn per
Session there, no Turn is open so the compactable prefix is unambiguous, and the
replacement is durable before this Turn's user message and before every request
this Turn freezes.

Compaction is not part of the Turn. A failed compaction leaves history exactly
as it was and the Turn proceeds with the full conversation, which is no worse
than the behaviour before M3. Refusing a user's Turn because host maintenance
hiccuped would be the larger harm.

### 2. Only a closed prefix is compactable

A cut boundary is always the sequence of a `turn/end` that really closed an open
Turn. Consequently the current user message, an open Turn, a Step, and an
assistant Tool call together with its `tool/result` can never be split.
Automatic compaction additionally drops the most recent `keep_recent_turns`
closed Turns from its candidate boundaries.

A manual boundary must name a closed Turn **exactly**. Sliding a caller's
sequence back to the nearest earlier closed Turn would compact a different range
than the one they asked for, and a sequence past the end of the log would
succeed while meaning something else entirely; both are refused with a stable
code instead.

`product/context-snapshot` is not a model-visible conversation type, so
host-recorded ProductTask evidence can never be a compaction source. ADR-0039's
and ADR-0041's rule that a summary may replace conversation prose but not host
status evidence is unchanged, and is now also enforced by the invariant checker
rather than only by the selection code.

### 3. Format 2 binds the whole decision

`surface/replace` uses one exact key set: `format_version`, `method`, `cut_seq`,
`source_seqs`, `source_digest`, `source_utf8_bytes`, `history_utf8_bytes`,
`kept_recent_turns`, `policy_digest`, `summarizer`, `summary`,
`summary_truncated` and `replacement`.

An `automatic` replacement must name both the `CompactionPolicy` digest that
authorized it and the summarizer identity (`name`, `version`, `config_digest`)
that wrote it. A `manual` replacement is a human decision and claims neither,
and may not claim retained Turns. The parser rebuilds the complete payload,
including the message, and requires canonical equality before it may reach the
Surface, so the summary and the counts describing it cannot drift apart.

Every derived fact is **recomputed by the invariant checker, not trusted**.
`surface_prefix()` is the one place that derives which events a cut covers, in
which order, their digest and their size; the compaction service writes what it
returns and `CoreInvariantChecker` re-derives it from the history preceding the
replacement. A checker that only inspected the shape of `source_digest` and the
byte counts would let a canonical-looking event hide an arbitrary subset of
history - for instance an assistant reply while leaving its user message
visible - behind fabricated numbers, and that Surface would reach the model.

`source_seqs` is ascending by sequence, which is also the order the digest is
taken over. Selection is by logical position, and those two orders genuinely
differ: an earlier summary appended late sits logically before newer messages
that a wider cut now also covers, so writing the selection order would produce
descending sequences and be refused by the protocol itself.

The summary is untrusted history, not a host fact. It is scrubbed of control,
format, surrogate, private-use and line/paragraph separator characters, bounded
by canonical UTF-8 bytes with an explicit truncation flag, and embedded as a
JSON string inside a fixed header. A summary containing a closing tag, a forged
header or a newline therefore cannot forge a second message or a host claim.

This is a pre-1.0 cutover. Format 1 is rejected explicitly: there is no second
parser, migration, alias, dual projector, fallback or silent rewrite, and older
data requires a new data directory.

### 4. A replacement is projected at the logical position of what it replaced

`surface_conversation()` assigns every model-visible event a logical position:
its own sequence for a message, and the smallest logical position among its
sources for a replacement, computed recursively so a replacement of a
replacement keeps the original position. The Surface is ordered by that position
rather than by append order, so a summary stands where the replaced history
stood and never moves behind the current user message. Repeated compaction
converges old summaries and newer old history into one summary instead of
stacking them.

Because the projection still reads only events up to a recorded `source_seq`, a
`request/snapshot` frozen before a compaction still rebuilds the original
history byte-for-byte, while a later one rebuilds the summarized history.
Replay calls no summarizer, no provider, and reads no latest state.

### 5. The byte metric is called bytes

The trigger is `history_utf8_bytes`: the canonical UTF-8 size of model-visible
conversation, excluding the host Product context messages, which are not
compactable. This runtime has no trusted general tokenizer, and reporting bytes
as tokens would be a fabricated number. Enabling automatic compaction requires
stating all four values - on/off, trigger bytes, summary byte bound and retained
Turns - explicitly; a partially configured policy is refused, and an absent
configuration means off.

### 6. The host decides; the summarizer only writes prose

`SessionSummarizer` receives a `SummaryRequest` holding the session id, the
exact messages being replaced, the byte bound and the retained-Turn count.
There is no Store, Session service, Tool registry, Runtime or approval handle in
it. A summarizer cannot choose what is compacted, read other history or cause
any side effect, and its failure becomes a stable compaction code rather than an
exception escaping into the Turn owner.

The default `BoundedHistorySummarizer` is deterministic and model-free: a
bounded, sanitized transcript digest. **No model-backed summarizer ships**, and
that is a decision, not an omission - see section 3 of the Context. A host may
inject its own summarizer, but the same rule applies: it may not call a provider
outside the Session dispatch permit, because that would be an unaudited,
unbudgeted model call.

### 7. Selection, summarizing and the append are one CAS

The Session head observed during selection is carried into the append as
`expected_seq`. The Store's compare-and-swap is the linearization point, so a
Session that moved while the summary was being written rejects the write; the
bounded retry then re-reads and re-selects from scratch rather than resubmitting
a payload that already lost its race. Repeated automatic triggering with no new
history is a no-op rather than a summary of a summary.

Cancellation converges the owned append worker and then reconciles a
may-have-committed write before propagating. Ordinary failure performs the same
JSON-type-sensitive reconciliation and reports `True`, `False` or - honestly -
unknown. Nothing deletes history, and no failure leaves half a replacement.

### 8. One host result, two adapters

The durable `surface/replace` event and the durable `surface/compaction-failed`
notice are the single host result. The Line timeline and the Textual adapter
each render that same event; neither keeps compaction state. Both show counts
and provenance only - never the summary, the replaced messages, the digests or
a prompt.

The failure notice reports all three commit answers separately. Only an exact
`false` may be shown as "history unchanged"; `true` means a replacement
committed but could not be read back, and anything else - `null`, absent, or the
wrong type - stays unknown. Collapsing unknown into "nothing happened" is the
reading the may-have-committed reconciliation protocol exists to prevent.

Because an enabled policy has no default to fall back on, the printed resume
command carries its four values. They are non-negative integers and a fixed
literal, so they cannot carry a credential, and omitting them would silently
turn the feature off for anyone who copied the command.

## Rejected alternatives

### Give the summarizer its own Turn, Step and Attempt

Rejected because its `request/snapshot` would not reconstruct as the Surface
projection, so `verify_request_snapshots()` would report a violation for every
compacted Session, and the summarizer's own messages would pollute the
conversation the model sees.

### Call the provider directly from the summarizer

Rejected because it is exactly the unaudited, unbudgeted, uncancellable model
call the two-stage admission boundary exists to prevent. Refusing to ship a
model summarizer is the honest outcome until an auditable non-Turn model-call
protocol is separately designed and authorized.

### Relax `verify_request_snapshots()` for summarization requests

Rejected because it would add a second reconstruction rule for the same event
type, which is a second reading of what a frozen request means.

### Order replacements by append sequence and accept the drift

Rejected because on every automatic compaction the summary would sit behind the
current user message, describing a conversation ordering that never happened.

### Keep format 1 alongside format 2

Rejected as a pre-1.0 compatibility layer. Two parsers for one durable protocol
is two definitions of what a replacement means.

### Estimate tokens

Rejected because there is no trusted general tokenizer here. A fabricated token
count in a durable event is worse than an honest byte count.

## Consequences and verification

- A configured Session compacts closed history before a Turn instead of growing
  its request forever, while every original event stays in the log.
- Historical requests still reconstruct byte-for-byte, proven both by
  `verify_request_snapshots()` and by comparing a pre-compaction snapshot's
  recorded messages against a fresh reconstruction.
- Format-1 data is intentionally incompatible and must not be mixed into a new
  data directory. Manual `traceh compact --through-seq` must now be exactly the
  sequence of a closed Turn's `turn/end`; anything else is refused with
  `compaction-boundary-not-closed-turn` or `compaction-no-closed-history`.
- Removing the closed-Turn rule, the Product context exclusion, the historical
  `source_seq` in reconstruction, the Session CAS, the logical-position
  ordering, the Tool-pair rule, the summarizer capability boundary, the
  ascending write order, the recomputed derivation, the three-state commit
  outcome, the resume-command tokens or the exact manual boundary each makes the
  corresponding tests fail for its own root cause; all twelve reverse checks
  were run and restored.
- Not covered by this decision: a model-backed summarizer, cross-Session memory
  and retrieval.
