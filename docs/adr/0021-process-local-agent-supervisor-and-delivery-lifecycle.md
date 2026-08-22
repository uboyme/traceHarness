# ADR-0021: A process-local Agent Supervisor and the delivery lifecycle

## Status

Accepted. Implements v0.6 Stage C only.

## Context

[ADR-0019](0019-durable-agent-identity-and-activation-boundary.md) made identity durable and
separated it from live Activation. [ADR-0020](0020-durable-agent-inbox-acceptance.md) made
acceptance durable and was explicit that **accepted is not processed**. Both were fact layers
with no consumer.

Stage C adds the consumer. The question it has to answer is not "how do we run a Turn" -
`AgentRuntime` already does that - but *"how does a durably accepted message become exactly
one Turn, provably, when two workers might both be looking at it"*.

That is a different problem from the previous two, because it is the first one where getting
it wrong has effects outside the log. A duplicated identity is a bad record; a duplicated
*execution* is a tool that wrote to a workspace twice.

## Decision

### 1. Four separate things

| | Where | Survives restart |
|---|---|---|
| Identity (`AgentRecord`) | `agents:directory` | yes |
| Acceptance (`AcceptedMessage`) | `agent-inbox:<id>` | yes |
| Delivery lifecycle (claim, outcome) | `agent-delivery:<id>` | yes |
| Activation (worker + runtime) | memory | **no** |

The Activation is reconstructible from the first three and never the other way round. It has
an `activation_id` that is recorded *inside* a claim, so a later reader can tell which live
instance took a message - but that id proves nothing about an Activation still existing.

### 2. Delivery gets its own stream

Not the Inbox stream. Stage B's projector accepts exactly one event type and rejects
everything else, and that contract is worth keeping: an acceptance history that could also
contain execution state stops being a plain answer to "what was received". Mixing them would
also mean every claim advanced the same `expected_seq` that senders compete for.

One stream per Agent, from one constructor, never parsed back into an `agent_id` - the same
rules as the Inbox stream, for the same reasons.

A claim carries `accepted_seq` as well as `message_id`, so replay can prove the two streams
agree about *which* accepted message is being executed rather than trusting an id match.

### 3. Nothing may execute before the claim is durable

This is the load-bearing rule of the whole Stage.

`AgentDeliveryService.claim()` returns only when the claim is provably in the log. Every
other outcome raises - including "unknown". A worker that ran a Turn on an unproven claim
could be the second worker to run it, and nothing later undoes that.

The linearization point is the claim append's `expected_seq`, carried from the delivery log
the worker actually decided from. Two workers that read the same head cannot both claim: one
gets `ConcurrencyConflict`, which is reported as `DeliveryConflictError` and simply means
"someone else is running that one".

The compare-and-swap is not a substitute for validating the decision that precedes it. Before
an append, the service re-reads the authoritative Inbox and delivery stream and proves that
the supplied Inbox, delivery view and complete `AcceptedMessage` belong to this Agent and that
the message is the next FIFO item. A terminal append likewise proves the supplied claim is the
current unique open claim. Invalid or cross-Agent views therefore write no contradictory fact.

An **unknown** claim outcome faults the Activation. It does not retry, because retrying is
exactly the thing that could double-execute, and it does not proceed, because the claim may
be invisible to the other worker. `wait_idle()` reports the fault instead of hanging or
returning as though all was well.

### 4. No in-memory queue

The worker re-reads the Inbox and the delivery log on every iteration and takes the earliest
unclaimed message. Copying accepted messages into a process-local list would create a second
answer to "what runs next" that another process cannot see, and the first thing it would get
wrong is a message someone else already claimed.

FIFO is strict: the first unclaimed message wins and a later one is never taken instead. An
open claim is not the same as a completed item: it blocks everything behind it. Stage C has no
stale-claim takeover identity, so skipping an open claim would be an undeclared retry policy.

### 5. Wakes cannot be lost

`_wake` is set and `_idle` cleared under one lock; the worker clears `_wake` **before**
draining and only sets `_idle` under that same lock after finding `_wake` clear. That pairing
removes the window between "I finished draining" and "I am now idle" in which a request could
land and be forgotten. Clearing after the drain - the obvious ordering - is the buggy one.

### 6. The Turn's message identity is the control plane's

`AgentLoop.run_turn()` minted its own `message_id` and stamped `source="user"`, which made a
Turn unaddressable: nothing could later say which Session Turn ran which durable message
except by comparing text.

`TurnInput` closes that. It is deliberately generic - content, an id, a source - and lives in
`traceh.api`, so `AgentLoop` accepts it without importing anything about Agents, Inboxes or
Supervisors. A plain `str` keeps the previous behaviour exactly.

The same `message_id` therefore appears in `inbox/accepted`, `inbox/claimed` and `turn/start`
in the Session, and in the claim and completion in the delivery log; the completion also
carries the real `turn_id`. That is what a future cold recovery will need in order to find
the Turn a stale claim belongs to, rather than guessing.

### 7. The Supervisor is not part of `AgentRuntime`

It lives in `traceh.supervision` and reaches the runtime through a four-method protocol: run
one message, cancel the current Turn, dispose, and expose Session and `EventStore` identity.
It never touches the runtime's active-Turn table, locks or plugin coordinator, and neither
`AgentRuntime` nor `AgentLoop` knows this package exists.

The public `traceh.api.AgentSupervisor` protocol describes the implementation that ships: a
stable request id is mandatory, interrupt reports whether it cancelled a Turn, and process
shutdown is explicit through `aclose()`. Keeping an incompatible sketch beside the concrete
surface would give future Tool and Workflow callers two contracts to choose from.

The store comparison is by object identity, resolving only `PublishingEventStore` - the one
transparent decorator this repository ships, which `build_default_runtime()` always applies.
Two stores can be configured identically and be two different logs; a Turn written to the
wrong one would leave a durable claim pointing at a Session history that does not contain it.

### 8. `create()` spans two streams and is not atomic

There is no transaction across `session:<id>` and `agents:directory`. One commits first, and
a failure in between leaves something behind. The order is deliberate:

1. freeze the spec and ids, with a caller-supplied stable `request_id`;
2. if that `request_id` already created an Agent, ask the Registrar to reconcile the complete
   frozen request before activating it;
3. provision - the factory creates the Session and an exclusive runtime;
4. append the identity with the same ids and the provisioned `session_id`;
5. only then install the Activation;
6. any failure or cancellation, **including an unknown identity append**, disposes the
   candidate runtime.

The in-process create single-flight is keyed by both `request_id` and a fingerprint of the
same request fields the durable identity protocol compares. A different request cannot join
an existing Task merely because the string id matches. The factory receives its own detached
spec copy, so it cannot mutate the metadata later used by identity registration.

Session-first is chosen because its failure mode is the survivable one: an unreferenced
Session is detectable and inert, while an `AgentRecord` pointing at a Session that does not
exist is a broken identity nothing can use. This is stated rather than hidden - the log is
never rewritten to fake atomicity, and `resume()` verifies the Session actually exists.

### 9. `NEXT_STEP` is refused, not reinterpreted

It means "inject into the Turn already running", and there is no safe seam: a Step has a
frozen Composition and an in-flight model call. `send()` refuses it *before* acceptance, so
nothing is written for a message that cannot be delivered.

Stage B still validates the enum, so a message written directly through `AgentInboxService`
can carry it. The worker records that as a terminal `failed` with `unsupported-target` rather
than skipping it - skipping would silently reorder the FIFO - and rather than faulting, since
one undeliverable message should not stop the rest.

### 10. `wakeup`, `interrupt`, `wait_idle`, `dispose`

- **`wakeup=False`** accepts durably and starts nothing. No Activation is created, resumed or
  woken. **`wakeup=True`** ensures an Activation exists and asks it to drain.
- If acceptance succeeded but the wake failed, `MessageWakeError` carries the `MessageReceipt`.
  Reporting a plain failure would invite a retry that appends the same message again.
- **`interrupt()`** cancels the current Turn through the runtime's existing cancellation path
  and nothing else; the Activation survives and keeps draining. The worker then records a
  `cancelled` terminal fact. The reason is bounded and single-line-safe before it can reach
  any log.
- **`wait_idle()`** waits for what was *scheduled* - claimed, run, terminal-recorded. A
  message accepted with `wakeup=False` was never scheduled and is not waited for. Cancelling
  `wait_idle()` does not cancel the Agent.
- **`dispose()`** stops admission, cancels and converges the worker, converges any terminal
  append already in flight, disposes the runtime, and removes the Activation. Its admission
  gate also owns matching create/resume Tasks, so a candidate cannot install itself after the
  caller was told disposal finished. The work belongs to one internal Task, so a caller's
  cancellation interrupts the *waiting* and never the shutdown; repeated calls await that
  same result, including cleanup failure. Nothing durable is deleted, which is what makes a
  later `resume()` meaningful.
- **`aclose()`** closes new admission once, then converges all in-flight creates and activation
  builds before draining every installed Activation. Repeated cancellation waits for the same
  internal shutdown Task; all resources get a cleanup attempt before failures are grouped.
- A worker exception while reading/projecting durable facts becomes a stable Activation fault,
  never a successful idle result. The runtime adapter also owns exactly one disposal Task, so
  an initial cleanup failure cannot become a silent success on the next call.

### 11. Failures record codes, never text

A terminal fact carries a stable code from this repository and, for a completion, the
`turn_id`. Exception messages and tracebacks are arbitrary third-party output that may quote a
request, a path or a credential; what actually happened is in the Session Event Log.

## Consequences

- A durably accepted `NEW_TURN` message becomes exactly one claim, one Turn and one terminal
  fact, and a second worker on the same store cannot duplicate any of them.
- An Agent that is disposed and resumed keeps its identity, its Inbox and its whole delivery
  history.
- A store or Session mismatch is refused before a Turn runs, rather than discovered afterwards
  in the wrong log.
- Faulting on an unproven claim means a transient store problem stops that Agent until an
  operator looks. That is the intended posture for a Stage with no retry policy.
- A crash after a claim leaves a claim with no terminal fact. Nothing in Stage C repairs it:
  the message is neither re-run nor released, and the delivery log shows exactly that.

## Explicitly not decided here

Cold recovery, stale-claim takeover, automatic retry, `spawn_agent` and the other subagent
tools, parent/child disposal, `WorkspaceProvider` and worktrees, hierarchical budgets,
Workflow, `NEXT_STEP` delivery, MCP, TUI and streaming. Version remains `0.5.0`; Stage C is
not a v0.6 release.

## Rejected alternatives

- **Put the Inbox and scheduling inside `AgentRuntime`.** Makes the single-Agent execution
  facade the multi-Agent control plane, which ADR-006, ADR-0011 and ADR-0019 each avoided.
- **Reuse the Inbox stream for claims and outcomes.** Breaks Stage B's single-event-type
  contract and makes execution state contend for the sender's sequence numbers.
- **Claim in memory and record it afterwards.** The claim would be invisible to any other
  worker for exactly the window in which it matters.
- **Treat an unknown claim as "not claimed" and retry.** The strongest possible conclusion
  from the weakest evidence, and the one that double-executes.
- **Let the Supervisor pass the message text to `run_existing()`.** The Turn stays
  unaddressable and the claim can only be matched to it by comparing prose.
- **Give `AgentLoop` a `TurnInput` that knows about Agents.** Would make the loop import the
  control plane, which is the coupling every earlier ADR exists to prevent.
- **Promote `NEXT_STEP` to a new Turn.** Delivers something other than what the sender asked
  for, silently.
- **Skip an undeliverable message in the FIFO.** Reorders what the sender queued, with no
  record that it happened.
- **Treat every claimed message as skippable when searching for FIFO work.** Lets a later
  message run while an earlier claim is still open and quietly invents stale-claim takeover.
- **Trust caller-supplied Inbox, delivery and claim objects because their dataclasses are
  frozen.** Frozen fields do not prove provenance or freshness; appending first would make a
  rejected call permanently corrupt the delivery stream.
- **Join create Tasks by `request_id` alone.** Lets a concurrent caller with a different preset,
  budget or pinned identity receive somebody else's Agent.
- **Dispose only Activations already in the live registry.** Lets a pending provision or resume
  install an Activation after disposal returned.
- **Mark cleanup attempted before awaiting it.** Turns the first cleanup failure into success
  on every later call and loses the only observable result.
- **Register identity before creating the Session.** Leaves an `AgentRecord` pointing at a
  Session that does not exist - the unrecoverable side of a boundary that cannot be atomic.
- **Delete events to fake a transaction.** The log is append-only; a control plane that
  rewrites it is no longer a fact source.
