# ADR-0040: Session-scoped ProductTask history context

- Status: Accepted
- Date: 2026-09-02
- Stage: M2 task memory
- Partially supersedes: ADR-0039 format-5 projection

> **Partially superseded by
> [ADR-0041](0041-session-scoped-product-task-evidence-memory.md).** ADR-0041
> preserves this ADR's same-Session bounded catalog and single-source rules,
> but replaces format 6 with format 7's minimal execution summary and a
> separate, on-demand pure-read evidence Tool. The format-6 decision below is
> retained as historical rationale rather than rewritten as current behavior.

## Context

ADR-0039 made the host-owned status of one selected ProductTask visible to the
requester model. It fixed the serious case where durable Product, Workflow and
Promotion facts said that work had completed while a later requester response
claimed that it had not run.

Format 5 still exposed only one selected task. After several ProductTasks had
completed in the same requester Session, older tasks disappeared from later
model requests even though their canonical events remained intact. The result
looked like memory loss: the model knew the current task but could not answer a
simple question about other work completed earlier in that Session.

This is a projection and progressive-disclosure problem, not a missing-fact
problem. ProductTask streams remain canonical, and the accepted requester
messages already provide the Session relationship and stable ordering needed
to build a small history view. Creating a second catalog, asking a model to
summarize tasks automatically, or beginning the v0.9 Workspace Memory design
would duplicate facts or cross the current stage boundary.

## Decision

### One source and one atomic snapshot

`EventStore` remains the only durable fact source. The host freshly replays the
canonical `product-task:*` streams and the matching requester Session evidence.
It appends one schema-1 `product/context-snapshot` event to that same Session;
there is no ProductTask catalog stream, cache or mutable runtime inventory.

The snapshot uses product context format 6 and contains one atomic view:

- `focus_task_id`, identifying the current focus;
- `tasks`, containing the focus first and at most five other recent tasks;
- `total_tasks`, counting all ProductTasks related to the Session;
- `omitted_tasks`, equal to `total_tasks - len(tasks)`;
- two model messages derived from exactly that same payload.

At most six tasks are disclosed. The focus is the unique live task when one
exists; otherwise it is the latest terminal task. Remaining entries are ordered
by descending accepted confirmation sequence. Multiple live tasks, duplicate
ordering, cross-Session origin/confirmation evidence, or missing and mismatched
evidence fail closed rather than being guessed.

Each task entry carries the minimum host-derived identity and lifecycle facts
needed for later reference: task and source-stream identity, Product head,
accepted-order sequence, status, requested and resolved modes, requirement
digest, origin message identity, and a bounded source request excerpt with an
explicit truncation flag. It does not copy execution evidence.

### Current facts and historical reference have different authority

Format 6 renders two messages from the single atomic snapshot, in this order:

1. a leading `system` fact message for the current focus, including its exact
   durable status and the host-managed execution semantics established by
   ADR-0039;
2. a `user` history-reference message listing the bounded Session task catalog.

The leading system message is authoritative for current ProductTask identity
and state. Older conversation prose cannot override it. The user-role catalog
is deliberately reference data: it helps the model recognize and discuss
earlier tasks, but is not the current user request and grants no START,
approval, promotion, retry or other control authority.

The source request excerpt is derived deterministically from the exact
requester `inbox/accepted.content` identified by `origin_message_id` and is
bounded by its canonical JSON representation. It is historical requester text,
not a canonical Product requirement, a fresh instruction, or proof of what the
agents changed or verified. The model may summarize it and make reasonable
inferences, but must distinguish those inferences from host facts.

### Freshness, races and cancellation

Every requester turn performs a fresh replay before reconstructing the Surface.
The context identifier covers the complete format-6 catalog payload. An
identical snapshot is reused idempotently.

The append uses the current Session head as a compare-and-swap boundary. A CAS
conflict restarts all Product and Session reads and rebuilds the catalog; it
never retries a stale payload. Logical Product head and accepted-order keys
prevent an older reader from appending behind and replacing a newer view.
Conflicting snapshots at one logical order fail closed.

Cancellation and may-have-committed failures retain the ADR-0039 convergence
rule: the owned append is drained, the exact event is reread, and the caller
returns only after commit status has converged. This change introduces no new
worker, lock, cache or lifecycle owner.

### Replay, Surface and compaction

The requester Surface reads the latest logical format-6 snapshot from the
Session and prepends both derived messages before ordinary conversation. It
does not perform a request-time cross-domain join. Historical context snapshots
remain auditable events, but only the logically latest snapshot is projected.

Session compaction and replay preserve the latest Product context independently
of conversation replacement. A `surface/replace` event may replace older chat
prose, but it cannot erase, synthesize or supersede the latest Product context.
Thus task history disclosure and chat compaction have one deterministic replay
result without turning mutable messages into another fact source.

### Breaking format cutover

The reader accepts format 6 only. Product context formats 1 through 5 are
rejected explicitly. This pre-1.0 cutover has no migration reader, compatibility
alias, dual projector, fallback or automatic rewrite. Testing and use after the
cutover require a new data directory.

## Explicit non-goals

This decision does not add:

- Workspace Memory, cross-Session memory or a durable Memory domain;
- `ContextInputSnapshot`, RAG, FTS, vectors, embeddings, reranking or retrieval
  policy;
- automatic task summarization, model-authored memory or automatic chat
  compaction;
- changed paths, patch bodies, Review or Promotion identities, verifier
  details, commands, tool calls, outputs or failure logs;
- new requester tools, control authority, Product lifecycle states or TUI-owned
  state.

Those concerns belong to their existing evidence owners or to a separately
authorized later stage. Omission from format 6 never means that the underlying
work did not occur.

## Rejected alternatives

### Persist a separate task catalog

Rejected because it would duplicate ProductTask lifecycle facts and introduce
another reconciliation problem. A fresh deterministic replay is sufficient at
the present scale.

### Keep format 5 and append an informal summary

Rejected because two independently updated projections could disagree, and a
model-authored summary would not be canonical evidence. Current focus and
history must be one atomic payload.

### Include every task or all Product evidence

Rejected because more context is not automatically better context. A bounded
six-task view plus an explicit omitted count provides useful continuity while
keeping disclosure predictable and honest.

### Treat the requester excerpt as the requirement

Rejected because the original requester message may contain surrounding prose,
instructions or later-stale language. It is a bounded retrieval clue only; its
digest and origin identity do not upgrade it into canonical requirement text.

### Start generic Workspace Memory now

Rejected because Workspace Memory, retrieval and search are v0.9 concerns with
different identity, policy and lifecycle questions. Solving Session-scoped task
continuity must not create a premature Memory subsystem.

## Consequences

- A later turn can identify the current ProductTask and up to five other recent
  tasks from the same requester Session without replaying their full evidence
  into the model context.
- The omitted count makes bounded disclosure explicit instead of silently
  implying a complete inventory.
- The behavior does not promise recall across Sessions or Workspaces.
- Context size remains bounded, while the full audit history remains available
  from the canonical event streams.
- Format-5 data is intentionally incompatible and must not be mixed with the
  new runtime data directory.

## Verification obligations

The implementation must cover zero, one, many and more-than-six related tasks;
live-focus precedence; terminal ordering; cross-Session exclusion; missing,
mismatched and ambiguous evidence; exact `system` then `user` message order;
excerpt bounding and hostile content; canonical payload validation; formats
1-5 rejection; idempotence; CAS retry from fresh reads; stale-reader exclusion;
cancellation and may-have-committed convergence; request reconstruction; and
compaction preservation.

Removing the format-6 snapshot must make the multi-task continuity tests fail;
otherwise the tests have not exercised the production projection path.
