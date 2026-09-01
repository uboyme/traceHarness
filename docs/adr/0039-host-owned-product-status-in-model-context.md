# ADR-0039: Host-owned ProductTask status in requester model context

- Status: Accepted, implemented, and amended
- Date: 2026-08-31
- Amended: 2026-09-01
- Stage: v0.8-F5 release-candidate repair

## Context

ProductTask, Workflow and Promotion already had one canonical append-only fact
source each. The Textual and Line adapters could therefore show a completed,
failed or cancelled task by replaying those streams. The requester model in a
later ordinary Chat Turn could not: `RequestBuilder` reconstructed its Surface
only from the requester Session, while Product control facts deliberately did
not enter that Session.

This produced a concrete contradiction during hands-on acceptance. The right
pane proved `product/task-completed` and a Promotion receipt, but the next model
response said that the task had not actually run. The model was faithfully
answering from an incomplete request, not disputing the durable facts.

The first repair copied only the public status. A second hands-on acceptance
proved that this was still underspecified: the request contained
`status: completed`, yet the requester model treated its own lack of write-tool
calls and its requester-workspace view as proof that the ProductTask had not
run. The missing facts were not another Patch or Promotion copy. They were the
canonical relationship and actor semantics already established by the host:
this exact task was proposed and confirmed in this requester Session, while
separate host-managed Product agents execute it in managed workspaces.

The format-3 relationship/actor repair restored freshness and lifecycle
agreement in a later acceptance run: the requester model named the exact task
and its completed state. That same answer exposed a smaller epistemic gap. It
treated the one selected task as a complete task inventory, combined the
requester's visible Workspace root with the generic managed-workspace sentence
to invent a Product execution path, and repeated the internal XML wrapper.
This was neither stale state nor missing Product evidence. The model-facing
canonical message lacked selection coverage, workspace-mapping and
fact-versus-inference guidance.

Format 4 closed that epistemic gap, but a later frozen request exposed a
different conflict. The request contained the canonical completed context, an
older assistant statement that the task was still waiting for START, and the
current user question. Because the Surface kept the context at its Session
sequence position after the stale assistant text, the model repeated the old
state. The context was present and the durable state was correct; the request
had no structural precedence rule between current host evidence and historical
model prose.

Copying a widget's prose into the request would fix only one adapter and would
not survive restart or request-snapshot verification. Joining arbitrary control
streams inside `RequestBuilder` would make historical request reconstruction
depend on their current heads rather than on what the model saw at dispatch
time. Exposing Review, Approval, Patch or Promotion evidence would also give the
model data and apparent authority it does not need.

## Decision

### 1. Domain streams remain the only authorities

`product-task:<task_id>` remains the only authority for ProductTask state.
Workflow and Promotion remain authoritative for their own orchestration and
promotion facts. No control decision reads the new Session event, and no
Product state is reconstructed from it.

The new event is perception evidence: it records the small, exact status
message that the host placed on a requester model Surface. This is the same
kind of evidentiary role that `request/snapshot` plays for a dispatched request;
it is not a second Product projector or mutable status cache.

### 2. The Product host synchronizes before every requester Turn

`ProductChatSurface.prepare_turn()` asks one `ProductModelContext` bridge to do
a fresh read before `AgentLoop` records the new user Turn. The bridge receives
the same `SessionService` and the same durable-log identity as the Product host;
assembly rejects mismatched Stores.

The bridge replays ProductTask streams and binds a task to the requester
Session only through its canonical origin/confirmation identities and the
durable `inbox/accepted` evidence for the confirmation message. A single live
task wins. With no live task, the unique terminal task with the greatest
confirmation accepted sequence wins. Multiple live tasks, conflicting equal
orders, missing confirmation evidence or mixed Session identity fail closed.
Task ids, per-stream head sequence numbers, timestamps and stream enumeration
order are not cross-task clocks.

### 3. One exact Session event freezes what the model saw

When the selected Product head changed, the host appends schema-1, format-5
`product/context-snapshot` to `session:<session_id>`. Its key set is exact:

- deterministic `context_id`;
- format version;
- Product task id;
- canonical Product source stream and head sequence;
- confirmation-message accepted sequence used as cross-task order;
- public durable Product status;
- the exact `system` message.

The Product head event id is the Session event's `causation_id` and participates
in `context_id`. The parser rebuilds the complete payload and message and
requires canonical equality before the message may reach the Surface.

The bounded message uses natural-language Facts, Limits and Use sections. It
contains the task id, durable source handle and public status, plus fixed host
semantics derived from that already-validated status:

- this exact ProductTask was proposed and confirmed in this requester Session;
- host-managed Product workflow agents perform Product execution; requester
  Tool history neither performs nor refutes that execution;
- each public status has one canonical, Product-stream-bounded meaning. A
  `started` ProductTask proves that START was durably accepted, but does not by
  itself assert that `workflow/run-started` is already durable. A `completed`
  ProductTask is durable terminal and carries a Promotion reference; the normal
  control path records it after promotion, but this bounded Product-only
  context neither resolves nor exposes the Promotion receipt. Meanings for
  failed/cancelled/rejected/abandoned never imply successful completion;
- this is one task selected for the request, not a complete task inventory;
  missing task ids prove neither uniqueness nor absence of other tasks;
- no Product execution Workspace path or requester-to-Product Workspace mapping
  is supplied. A requester Session Workspace path is not Product execution-path
  evidence;
- omitted file, command, test, output, Patch, Review and Promotion identities
  do not mean no work occurred, and requester Tool history or Workspace files
  must not be used to contradict the host status;
- earlier conversation claims about this task's status cannot override the
  current host facts. The old messages remain intact as history;
- the model may answer naturally, summarize and make reasonable inferences, but
  must distinguish host facts from inference rather than invent omitted
  specifics. The provider-visible text contains no XML wrapper and asks the
  model not to quote the internal block or its labels;
- the observation is evidence only and grants no START, approval, Promotion,
  retry or other control authority.

It does not claim that a particular file changed: a valid completed task may be
a no-op. It never contains requirement text, model reports, paths, Patch bytes,
Review or Promotion ids, digests, revisions, verifier arguments/output, failure
detail or Provider data.

Format 1's status-only wording, format 2's earlier cross-owner wording,
format 3's XML/underspecified evidence wording and format 4's missing conflict
precedence are deliberately unsupported. Candidate data containing any of
them fails closed and
requires a new data directory; there is no multi-version parser, migration,
silent rewrite or fallback. The format participates in `context_id`, so changing
the canonical message cannot alias an older durable event. The event envelope
remains schema 1 because only this message protocol changed.

### 4. Session replay alone reconstructs each request

`SurfaceProjector` selects at most one validated Product context by the logical
pair `(confirmation accepted seq, Product head seq)`. It projects and sorts the
complete conversation exactly as before, then places the selected current-state
system instruction before that conversation. A late append of an older read
cannot roll the Surface backwards, and stale assistant prose cannot
structurally precede the current host evidence. No historical message is
filtered or rewritten. Existing `RequestBuilder`,
`request/snapshot` and fingerprint verification then reconstruct the exact
request using only Session events through the recorded `source_seq`; there is no
request-time cross-stream join.

Manual `surface/replace` compaction may summarize conversation prose but cannot
hide this host status evidence. `CompactionService` therefore never offers
`product/context-snapshot` as a replacement source. Older snapshots remain in
the append-only log but do not accumulate on the current model Surface.

### 5. Write ownership and cancellation keep the existing contracts

The bridge writes through the existing `SessionService`, uses Session
`expected_seq` CAS and retries a bounded number of times after a real conflict.
Every retry repeats the fresh Product and Session reads instead of appending a
payload frozen before the conflict. Identical heads are idempotent across
process restart.

Append cancellation converges the owned worker before returning cancellation.
Ordinary append failure performs full, JSON-type-sensitive payload and
causation reconciliation; an unknown commit outcome stays unknown. A stale
reader that loses to a logically newer snapshot does not append its older
observation behind it.

## Rejected alternatives

### Put the TUI or Line host sentence back into the model prompt

Rejected because adapter output is ephemeral presentation, differs by UI and
cannot be reconstructed from a Session request snapshot. The existing local
host notices remain presentation only.

### Make `RequestBuilder` read Product, Workflow and Promotion streams

Rejected because replaying an old request would then observe today's domain
heads, not the facts visible when the request was dispatched. It would also
couple the generic Runtime request builder to Product control domains.

### Give the requester model a status, approval or promotion Tool

Rejected because the model does not need control authority to receive a host
observation. Approval and Promotion remain human/host actions, and ordinary
requester Chat remains read-only before `START`.

### Copy raw control-plane events or evidence into the Session

Rejected because it would leak unnecessary identities and duplicate domain
state. The snapshot is a strict bounded whitelist with one purpose.

### Delete or rewrite stale assistant messages

Rejected because those messages are part of the exact requester conversation
and request-replay evidence. Removing them would hide how the contradiction
arose and would turn conflict handling into response censorship. The Surface
keeps every historical message and gives the one canonical current-state
instruction structural precedence instead.

### Copy changed paths or joined Review/Promotion evidence into the snapshot

Rejected for this defect because the public Product status already owns the
needed lifecycle conclusion, while safely proving a detailed joined outcome
would add Workflow, Artifact, Review, Approval and Promotion read-side
invariants. The contradiction is resolved by stating the existing task
relationship, execution owner and status meaning; raw or duplicated outcome
evidence remains outside the requester model context.

### Keep the latest task in TUI memory or another cache

Rejected because restart, Line Chat and request reconstruction would disagree.
Selection is recomputed from the one EventStore and only the exact perception
evidence is appended to the requester Session.

## Consequences and verification

- After a ProductTask head changes, the next requester model request contains
  the host-recorded durable Product status together with its relationship,
  conditional execution owner, Product-bounded status meaning, selected-not-
  inventory scope and Workspace/evidence limits. It expressly permits natural
  summaries and reasonable inferences while requiring attribution of inference.
  The request
  no longer omits the fact that a completed ProductTask is terminal and must not
  be treated as waiting for START merely because the requester model itself made
  no write Tool call. Deterministic tests prove this request contract, not that
  every external Provider will follow the instruction. The canonical context
  leads the requester conversation while that conversation remains complete.
- Restoring the format-1 status-only message makes the semantic unit and real
  local-Git next-request assertions fail while the canonical Product task still
  completes, directly reproducing the defect. Reusing format 2 for the changed
  canonical message makes the first legacy-format isolation assertion fail.
  Removing selected-not-inventory makes all nine status contract cases fail;
  reusing format 3 makes the old legal format-3 context and the new canonical
  message share one identity, which the isolation test rejects. Restoring the
  old chronological Surface ordering makes the conflict unit and real Product
  E2E lose the leading canonical context.
- Request-snapshot reconstruction remains exact after the new system message.
- Deterministic tests cover every status meaning, completed/failed/cancelled
  terminal boundaries, the positive permission to summarize and infer,
  selection/workspace/omission limits, absence of the old XML wrapper,
  format-1/2/3/4 rejection, no task, restart
  idempotence, new-task and new-head supersession, late stale append, malformed
  payload rejection, Session CAS conflict, read failure, cancellation before a
  write and cancellation after a may-have-committed write.
- Tests also prove that Review, Promotion, failure detail, digests and revisions
  do not enter the requester model request.
- This decision narrows the earlier ADR-0032 statement that Product control
  facts do not enter the model: raw Product/Workflow/Promotion streams still do
  not, while this bounded host-owned Session observation now does.
