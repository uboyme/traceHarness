# ADR-0020: Durable Agent Inbox acceptance

## Status

Accepted. Implements v0.6 Stage B only.

## Context

[ADR-0019](0019-durable-agent-identity-and-activation-boundary.md) established that an Agent
exists exactly when its `agent/created` event is in the control-plane stream, and that a live
`AgentRuntime` is an Activation rather than an identity. It deliberately left communication
out: a message's source is a per-message fact, and folding it into a creation fact would make
"who created me" and "who is talking to me" one relation forever.

v0.6 needs an `AgentSupervisor`. Building it next would mean one component acquiring three
different kinds of complexity at once - identity, messages and Activation lifecycle - and the
message layer is the one whose mistakes are permanent: an append-only stream cannot un-accept
a message that was written with the wrong shape.

So the ordering is deliberate. Stage B establishes the *fact layer* for messages, with no
consumer. Stage C's Supervisor then consumes an already-stable protocol instead of inventing
one while also learning to run Turns.

## Decision

### 1. One durable FIFO acceptance history per Agent

`AgentInboxService.accept()` appends `agent/message-accepted` to that Agent's own stream, and
`AgentInbox` rebuilds the accepted order from it. A fresh process holding only an `EventStore`
reproduces the same order.

**Accepted is not processed.** The protocol records that a message was durably received and
where it sits in that Agent's order. It cannot express delivery, claiming, execution,
completion, failure or retry, and no field should ever be read as one of them. There is no
Supervisor in Stage B to do any of those, and `wakeup` is stored as the *sender's request*,
not as an action taken.

### 2. One stream per Agent, built by one constructor

Stream ids come from `agent_inbox_stream(agent_id)` and nowhere else. FIFO order is a property
*of an Agent's* Inbox, so a shared stream would make one Agent's traffic advance another's
`expected_seq` and serialize unrelated senders against each other.

The inverse - recovering an `agent_id` by splitting a stream name - is deliberately **not**
provided. An identifier may itself contain the separator, so parsing one back out would make
identity depend on guessing. Validation instead builds the expected name *forward* from the
payload's `agent_id` and compares, which is exact regardless of what the id contains.

### 3. One rule for writer and replay

Everything about the payload - the exact key set, the identifier rules, what content may be,
what `target` and `wakeup` may be - lives in `inbox_identity.py`, and both sides import it. A
rule the writer applies more loosely is not a weaker check but a way to append a fact that can
never be read back, and in an append-only stream that damage does not heal.

Two consequences worth naming:

- **Content is prose, not an identifier.** The single-line terminal rules that govern an
  `agent_id` must not be applied to it: a message may legitimately contain newlines, tabs and
  any script. It is bounded by `MAX_MESSAGE_CONTENT_CHARS` because one event is one JSONL
  line, and it must be UTF-8 encodable - a lone surrogate survives `json.dumps` and then
  raises `UnicodeEncodeError` inside `JsonlEventStore.append()`, which would mean the writer
  admitting content the store cannot persist.
- **`wakeup` is strictly `bool`.** Truthiness would read `1`, `"false"` or `[]` as a decision,
  and this field will later decide whether an Activation is started. `target` is likewise
  compared against `MessageTarget`'s real values, never `MessageTarget(str(value))`, which
  would coerce an unknown routing instruction into a known one.

### 4. Replay fails closed

`AgentInbox.rebuild()` raises rather than repairing. A second acceptance of a known
`message_id` is a contradiction in an append-only stream, not an update. The same applies to
an unknown event type, an unsupported `schema_version`, a payload whose key set is not exactly
the protocol's, an event on the wrong stream, and any malformed field.

Skipping a broken record would be worse here than in the directory: order *is* the answer this
projector gives, so a skipped record reports a FIFO sequence that never happened. Issue codes
are stable and carry the offending `seq`; messages are fixed repository text and never echo
content or source, which are the most likely places for a caller to have pasted something
private.

### 5. Acceptance is a transaction, mirroring Agent creation

Order: freeze the request before the first `await`, confirm the Agent exists in the durable
directory, rebuild the Inbox, reconcile a repeated `message_id`, then append with
`expected_seq = inbox.head_seq`.

- **The linearization point is the append's `expected_seq`**, carried from the Inbox read
  rather than re-read at append time. Re-reading would accept a decision whose idempotency
  check ran against a history that no longer exists.
- **One lock per Agent**, not one per service. Each Agent has its own stream and therefore its
  own compare-and-swap, so serializing unrelated Agents would be an invented constraint. The
  locks are linearization aids for callers sharing one object; the CAS is what actually
  rejects a second writer and keeps working across processes.
- **Retry is decided by the caller's `message_id`.** The same id with the same message returns
  the original receipt; the same id with a *different* message is an error. Every field
  participates in that comparison - unlike an Agent's free-form `metadata`, nothing in a
  message is merely cosmetic.
- **The Agent must already exist.** An Inbox history for an unregistered id could never be
  claimed by anyone.

### 6. Commit reconciliation is shared, not restated

A cancellation inside the store's critical section leaves the event durable, so "I was
cancelled" does not mean "nothing was written". Both control-plane transactions need that
answer and must not develop two readings of it, so `commit_reconciliation.py` holds it once:
a converged re-read returning `True`, `False`, or `None` for *unknown*.

The seam is deliberately narrow. The shared module answers only the question - did our event
land, and can we even tell - while each transaction keeps its own error mapping, because which
domain error a failure becomes is a property of that transaction. `AgentRegistrar` was moved
onto it with no behaviour change; its full contract suite still passes unchanged.

**The match is against the complete frozen fact, not the `message_id`.** Two senders racing
on one id write *different* messages, so matching on the id alone told the loser its message
had been recorded when what landed was the other one's. The candidate is parsed through the
projector - which re-checks type, stream, schema version and key set - and then compared as
**canonical JSON**, never with `==`: Python equality is not JSON identity (`True == 1`,
`1 == 1.0`, `[True] == [1]`), and both control-plane transactions share that one rule.

  Only a `AgentInboxProtocolError` justifies answering "not ours": that proves the candidate is not a
  well-formed fact at all. A *canonical-encoding* failure is not a negative answer - it means
  the comparison could not be made - so it propagates and the shared reconciler reports
  `None`. Collapsing it into `False` claimed "provably not committed" about an event that was
  sitting in the stream. A malformed unrelated event is skipped rather than making the
answer unknown.

Reading a persisted **event** is likewise untrusted work, and the boundary covers all of it -
not only the payload. `parse_message_accepted` calls `set(data)`, `data.get()` and `data[key]`
on a container the store handed back, *and* compares `event.type`, `event.stream_id` and
`event.schema_version`; `EventEnvelope` is a public DTO, so those fields are no more trusted
than the payload. The whole read sits inside one boundary that turns any `Exception` into a
fixed `AgentInboxProtocolError`, and `_scan()` does not pre-check the type outside it. `validate_agent_inbox_events` promises issues rather than
exceptions, and a hostile `dict` subclass used to break that promise. `SystemExit` and
`KeyboardInterrupt` are deliberately not caught.

`AgentInboxConflictError` promises nothing was written and is therefore used only when the
re-read positively proved that. `SystemExit` and `KeyboardInterrupt` propagate untouched;
only `CancelledError` gets convergence treatment.

## Consequences

- A fresh process recovers every Agent's accepted order from the event log alone.
- Two Agents' Inboxes are independent streams: neither ordering nor contention crosses between
  them.
- A retry after an uncertain append accepts the message once, whichever way the append went.
- Corrupt or contradictory history is visible instead of silently reinterpreted, at the cost of
  blocking reads *and* new acceptances for that Agent until it is understood. That is intended
  for a fact source whose whole answer is an ordering.
- `AgentLoop`, `AgentRuntime` and `PluginManager` are untouched and hold no Inbox state.
- Version remains `0.5.0`. Stage B is not a v0.6 release.

## Explicitly not decided here

Live `AgentSupervisor`, single-activation enforcement, claim / ack / complete / retry, wakeup
actually starting anything, Turn scheduling, cold recovery, subagent tools, parent/child
disposal, budgets, workspaces and Workflow. Stage B supplies the facts those will need without
implying any of them exists.

## Rejected alternatives

- **Build the Supervisor first.** It would acquire identity, messaging and Activation
  complexity simultaneously, and would be inventing the message protocol while also learning
  to run Turns - with the protocol's mistakes permanent in an append-only stream.
- **One shared Inbox stream for all Agents.** Makes one Agent's traffic advance another's
  sequence, serializes unrelated senders, and turns per-Agent FIFO into a filter over a global
  order.
- **Inbox events on the Session Stream.** A Session is one Agent's execution history; an
  accepted message is not yet part of any execution, and mixing them would put unclaimed
  messages into the model-visible surface.
- **Recovering `agent_id` by splitting the stream id.** Identity would depend on parsing a
  string that identifiers may contain separators in.
- **An in-memory queue with periodic persistence.** A second fact source, and the one that
  disagrees with the log after any crash.
- **Copying the commit-reconciliation protocol into the new service.** Two copies of a subtle
  cancellation and commit-point rule are two definitions of when a caller may be released.
- **Generalizing both transactions into a shared transaction framework.** Over-fits two cases
  into an abstraction, and hides the domain error mapping that is genuinely per-transaction.
- **Reconciling on `message_id` alone.** Answers "is that id present", not "did our event
  land", and misattributes a racing writer's message to us.
- **Comparing payloads with `==`.** Python equality is not JSON identity, so a racing writer's
  differently-typed but equal-looking fact is reported as ours.
- **Treating envelope protocol fields as trusted because the store produced them.**
  `EventEnvelope` is a public DTO; anything can construct one.
- **Validating a payload outside the boundary that normalizes its failures.** Traversing a
  caller-influenced container can itself raise, so the read needs the same protection as the
  fields it reads.
- **Recording `claimed`/`completed` fields now, unused.** A field that exists but means nothing
  is a field a later reader will trust.
