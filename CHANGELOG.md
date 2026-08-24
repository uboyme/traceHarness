# Changelog

## Unreleased

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
  ADR-0027. Default CLI grants, cross-process leases, hard-crash recovery for
  STARTED reservations, Workspace/Patch and Workflow remain future work;
  version stays `0.6.0`. The Budget suite is 79 passed, the expanded
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
