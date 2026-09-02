# ADR-0041: Session-scoped ProductTask memory and on-demand evidence

- Status: Accepted and implemented
- Date: 2026-09-02
- Stage: M2 task memory completion
- Partially supersedes: ADR-0039 evidence/tool exclusions and ADR-0040 format-6 projection

## Context

ADR-0039 made the current ProductTask status authoritative in later requester
model calls. ADR-0040 then replaced the single-task format with an atomic,
bounded catalog of ProductTasks proven to belong to the same requester Session.
Those changes fixed stale-status and multi-task continuity, while deliberately
keeping execution details out of the request.

Hands-on use exposed the remaining progressive-disclosure gap. A model could
see that a task was durably `completed`, but the default context did not say
whether managed agents had called tools, changed files, passed verification or
recorded Promotion. The model could therefore infer from its own requester-Chat
tool history that no execution had happened, even though the right-hand Product
view was backed by complete durable evidence. Copying every path, tool outcome,
Verifier and Promotion identity into every request would avoid that one error at
the cost of large, mostly irrelevant context and broader disclosure.

The missing information already exists in ProductTask, Workflow, Agent,
Session, Artifact, Review, Approval, Promotion and Budget streams plus the
Artifact CAS. It must not be copied into a new Memory stream, mutable cache,
model-authored summary or second state machine. The design therefore needs two
levels: the smallest verified execution summary required on every relevant
request, and a bounded read-only evidence operation for questions that need
specifics.

In this ADR, “task memory” means a fresh, Session-scoped read model over those
existing facts. It does not name a durable Memory domain and is unrelated to a
future cross-Session Workspace Memory design.

## Decision

### 1. Existing streams and CAS remain the only authorities

`EventStore` remains the only durable fact log. ProductTask, Workflow, Agent,
Session, Artifact, Review/Approval/Promotion and Budget owners keep their
existing streams, while raw Patch bytes remain in the existing content-addressed
Artifact store. No new stream, database table, cache, index, RAG layer or
model-authored memory is introduced.

`ProductTaskMemoryReader` and `ProductTaskActivityReader` are stateless join
readers despite their names. Each call freshly replays the exact task and its
related streams, validates identities through the existing readers, and returns
immutable values. Neither reader writes facts or makes a control decision.

The existing schema-1 `product/context-snapshot` in the requester Session still
records the exact model-visible messages together with the identity and replay
metadata that binds them to the selected Product facts. It is perception
evidence for exact request replay; it cannot reconstruct or advance Product state. A call to the
new requester Tool is recorded by the ordinary Session Tool lifecycle, as every
model Tool call is, but the evidence reader itself adds no Product or control
event.

### 2. Format 7 keeps the catalog and adds one minimal focus summary

Product context format 7 preserves ADR-0040's atomic catalog:

- the current focus is first;
- at most six same-Session ProductTasks are shown;
- exact `total_tasks` and `omitted_tasks` remain part of the payload;
- a leading `system` message carries current host facts;
- a following `user` message carries only bounded historical requester-text
  references and is not a current request or authorization.

Only the current focus may receive an `execution_summary`, and only when its
Product status is exactly one of:

- `awaiting_approval`;
- `completed`;
- `rejected`;
- `cancelled`;
- `failed`.

The summary has exactly six host-derived fields:

- `workflow_status`;
- `managed_tool_call_count`;
- `changed_path_count`;
- `verification_passed`;
- `verifier_count`;
- `promotion_recorded`.

Historical catalog entries never carry an execution summary. Neither do focus
entries in `opened`, `routed`, `started` or `abandoned`. Review-derived
`changed_path_count`, `verification_passed` and `verifier_count` are all absent
or all present; a partial triple is invalid. A `completed` summary must have
Promotion evidence, while every other summarized status must not claim
Promotion. These constraints prevent the small summary from combining facts
from different lifecycle points.

The provider-visible system message identifies this as a minimal execution
summary. It explicitly tells the model that changed-path details, bounded Tool
outcomes, Verifier results and Review/Promotion metadata are omitted by default
and may be obtained with `read_product_task_evidence` when needed. Raw Patch
content, Tool arguments/output, model prose and Product workspace paths remain
outside both the default summary and that Tool.

### 3. Detailed evidence is an exact, same-Session pure read

Product-configured requester Chat adds one core Tool:
`read_product_task_evidence`. Its effect kind is `PURE_READ`, it accepts exactly
one `task_id`, and it grants no START, approve, reject, cancel, abandon,
Promotion, retry or workspace-write authority. The complete requester core Tool
surface is therefore:

- `list_files`;
- `read_file`;
- `search_text`;
- `propose_product_task`;
- `confirm_product_task`;
- `read_product_task_evidence`.

The existing monotonic Product Chat policy continues to deny every registered
Tool declared as workspace write, process, network write or external
transaction. Adding this
pure read does not weaken the pre-START effect boundary.

Possession of a task id is not authorization. Before reading detailed evidence,
the reader proves from durable facts that both the Product origin and Product
confirmation belong to the calling requester Session. It validates the exact
accepted messages, their claims and Turn starts, the origin Turn end, and that
the confirmation was accepted after that end. Cross-Session tasks fail the same
way as missing tasks.

For public Tool behavior, a missing, foreign, corrupt or unreadable task always
returns:

```json
{"available":false,"code":"product-task-evidence-unavailable"}
```

The response does not reveal which check failed, so the Tool cannot be used as
a ProductTask identity oracle across Sessions. Caller control signals are not
reinterpreted as task availability.

On success, the bounded JSON projection can contain:

- Product head, status, requested/resolved mode and requirement digest;
- Workflow status, stream-divergence flag and fixed node outcomes/failure
  categories;
- per-role Turn counts plus managed Tool names, call/result sequence, outcome
  status and exit code;
- Review identity, pass result, Patch digest/size/totals, changed paths and
  Verifier command identities/status/exit codes;
- whether Approval is recorded;
- Promotion identity, Review/target linkage, target ref and resulting revision;
- durable Budget usage metrics when available.

The activity projection accepts exactly the durable result statuses emitted by
the current Tool Runtime and Recovery paths: `succeeded`, `failed`,
`cancelled`, `invalid`, `denied`, `aborted_before_dispatch` and
`unknown_after_crash`. Any other durable value fails closed. `pending` is only
the in-memory projection of an admitted call that has no paired `tool/result`;
it is not an eighth durable result status.

Changed paths are capped at eight with an omitted count. Each role retains its
latest eight Tool calls with an omitted count. Verifiers are capped at eight
with exact shown/total counts. The complete canonical JSON is capped at 20,000
characters and fails closed if it cannot fit. This is detailed operational
evidence, not a raw event dump: Agent/Session internals not in the projection,
raw Tool arguments/results, model responses, Patch bytes and workspace paths
are not exposed.

### 4. One validated read-model chain, two distinct CLI bundles

`build_product_read_models()` constructs an internally bound, stateless reader
chain containing the Artifact reader, inspection evidence reader, Product
observation reader and Product task-memory reader. Construction freezes and
checks the VerificationPlan digest, Promotion target, Artifact CAS and report
bound. The Product host receives only the post-Runtime bundle. It validates that
bundle against its own Store, CAS and frozen Product configuration, and rejects
an internally mixed Artifact/evidence/observation/memory chain.

The CLI intentionally constructs this bundle twice:

1. before Runtime construction, one bundle over the supplied Store provides the
   memory reader captured in the requester's frozen Tool registry;
2. after Runtime construction, another bundle over the Runtime's publishing
   Store wrapper provides Product host observation and automatic model context
   while retaining the in-process Event Feed.

These are separate Python objects and are not compared with each other by the
Product host. The CLI calls the builder twice with the same underlying durable
log and the same explicit Product Profile, Artifact CAS, VerificationPlan,
Promotion target and report bound; the second call wraps that log with the
Runtime's publishing Store. Their shared authority and configuration are
therefore guaranteed by the exact production composition, not by a cross-bundle
host comparison. Thus the requester Tool, automatic context, Line/TUI
observation and task-conversation projection may use separate reader instances
without creating separate facts.

`ProductTaskActivityReader` is also the shared implementation for bounded
managed role/Tool activity. The task-memory path and the TUI task-conversation
path may each instantiate it; they still read the exact role Sessions selected
from the same Product/Workflow/Agent identity chain and the same durable log.

### 5. Freshness and fail-closed joins

Automatic context first captures a related Product head. When the focus needs
an execution summary, it loads evidence for that exact captured head. If the
Product head changes during the join, the reader returns the existing
`product-memory-product-head-changed` signal and the bounded context writer
restarts all fresh reads rather than combining an old status with newer
execution facts.

Detailed evidence applies the same principle. Product state, Workflow topology,
Agent ownership, Session invariants, Artifact identity/CAS bytes, frozen
VerificationPlan, Review target, Approval and Promotion Review/target linkage
must all agree. Missing or mismatched evidence fails closed; no layer fills a
gap from model prose, widget state, timestamps or a guessed path.

The context append retains ADR-0039/0040's Session CAS, logical ordering,
may-have-committed reconciliation, request replay and compaction-preservation
rules. The evidence Tool performs no parallel append or reconciliation path.

### 6. Breaking protocol cutover

The Product context parser accepts format 7 only. Formats 1 through 6 are
rejected explicitly. There is no compatibility reader, migration, alias, dual
projector, fallback, automatic rewrite or silent deletion. Testing and use
after this pre-1.0 cutover require a new data directory.

ADR-0040 remains the historical rationale for the bounded same-Session catalog,
but its format-6 payload and “no execution evidence/new requester Tool”
non-goals are superseded here. ADR-0039's rejection of model-visible Product
control Tools still applies; a same-Session `PURE_READ` evidence Tool is not a
status writer, approval command or Promotion command.

## Explicit non-goals

This decision does not add:

- cross-Session or cross-Workspace recall;
- a durable Memory domain, cache, search index, RAG, embeddings, reranking or
  model-authored summary;
- automatic chat compaction or model-controlled context selection;
- raw Event, Patch, Tool argument/output, model prose or Product workspace-path
  disclosure;
- requester write/process/external-transaction capability;
- Product lifecycle, Workflow, approval, Promotion, retry or reconciliation
  control;
- another TUI state, Product projector or EventStore.

The Tool is not a general audit-query language. It returns one fixed, bounded
projection for one exact related task.

## Rejected alternatives

### Put all execution evidence into every request

Rejected because it would consume context on details irrelevant to most turns,
increase disclosure and still require a precise cross-domain join. The default
summary should answer “did managed execution happen and how did it settle?”;
the pure-read Tool handles narrower follow-up questions.

### Let the model infer execution from requester Tool history

Rejected because requester Chat and host-managed Product agents have different
owners and Sessions. Absence of requester writes is not evidence that Product
execution did not occur.

### Authorize evidence by task-id possession

Rejected because task ids can appear in logs, screenshots or copied messages.
The existing durable origin/confirmation relationship is the correct
Session-scoped authorization boundary.

### Persist a memory catalog or evidence cache

Rejected because existing streams already own every fact and can be freshly
joined. A duplicate cache would require invalidation and reconciliation and
could disagree after restart or cross-process updates.

### Reuse one Python reader instance everywhere

Rejected as an architectural requirement. Runtime construction freezes its Tool
registry before the publishing Store wrapper is available, while host
observation needs that wrapper for Feed notifications. Stateless readers over a
proven common durable log provide consistency without artificial object-identity
coupling.

## Consequences

- A later requester turn receives a small, authoritative indication of managed
  execution for the current relevant status without carrying every detail.
- The model can answer evidence-specific follow-up questions by invoking one
  bounded, read-only Tool for a same-Session task.
- More than six completed ProductTasks remain represented by exact total and
  omitted counts; detailed evidence for a task still requires its exact id and
  Session relationship.
- The design gives the model better facts but does not promise perfect Provider
  instruction following. Historical prose remains visible and host facts retain
  structural precedence.
- Existing Product, Workflow, Session, Artifact, Review, Promotion and Budget
  evidence stays authoritative and independently auditable.
- Format-6 data is intentionally incompatible with this runtime and must not be
  mixed into the new data directory.

## Verification obligations

The implementation must cover:

- canonical format-7 payload/message reconstruction and formats 1–6 rejection;
- all five summary-bearing statuses, all summary-free statuses, focus-only
  placement, Review triple consistency and Promotion consistency;
- same-Session success plus indistinguishable missing/foreign/corrupt/unreadable
  Tool results;
- exact Tool schema, `PURE_READ` classification, no Product/control writes and
  continued denial of effectful Product Chat Tools;
- bounded paths, per-role latest Tool calls, Verifiers and whole-report size,
  with the exact shown/omitted or shown/total counts defined by the projection
  and prohibited-field absence;
- all seven durable Tool result statuses, an unpaired in-flight call projected
  as `pending`, and fail-closed rejection of unknown durable statuses;
- Product/Workflow/Agent/Session/Artifact/Review/Approval/Promotion identity and
  frozen-plan/target mismatch rejection;
- separate CLI reader bundles constructed from the same durable-log identity
  and frozen configuration inputs, plus host validation of the post-Runtime
  bundle's internal chain, without cross-bundle or Python object-identity
  comparison;
- head-change retry, Session CAS retry from fresh reads,
  may-have-committed convergence, request reconstruction and compaction
  preservation.

Removing the format-7 execution summary must reproduce the default-context
contradiction. Removing the Session relationship check must make a foreign task
readable. Either reverse check is necessary evidence that the production paths,
not isolated helper shapes, are under test.
