# ADR-0023: Supervisor-backed subagent tools

- Status: Accepted
- Date: 2026-08-22
- Stage: v0.6 Stage E

> v0.7 D0 amendment: the Stage E behaviour and five Tool schemas remain, but
> `SupervisorToolset` now targets the public `AgentSupervisor` protocol,
> authorization is owned by a fresh-reader `AgentToolAuthority`, and child
> intent must pass an explicit host `ChildProvisioningPolicy`. The new decision
> and dependency boundary are recorded in [ADR-0024](0024-v07-managed-agent-control-plane-and-threat-boundary.md);
> the original sections below remain the historical Stage E decision.

## Context

Stages A–D established durable Agent identity, FIFO acceptance, claim/terminal
delivery, one process-local Activation per Agent, and child-first lifecycle
disposal. A host could use those APIs, but a model could not: no Tool exposed
the control plane.

Putting subagent syntax into `AgentLoop`, copying Supervisor state into a Tool,
or letting the model declare its own owner would each create a second
scheduler or authority source. Returning an in-memory `TurnResult` would also
make collection disagree with a fresh process reading the durable logs.

Stage E must expose useful model operations while preserving the existing
identity, ownership, communication, cancellation and fact-source boundaries.

## Decision

### 1. The five operations are ordinary bound Tools

`SupervisorToolset` creates `spawn_agent`, `send_agent_message`, `wait_agent`,
`stop_agent` and `collect_agent_artifact`. They call the existing
`ProcessAgentSupervisor`; they do not own an Inbox, queue, Activation table or
cleanup graph. A host injects them through the existing Tool Registry or
`additional_tools` assembly seam. `AgentLoop`, `AgentRuntime` and
`PluginManager` remain unaware.

### 2. Authority is host-bound and fail-closed

The Toolset constructor binds three facts:

1. the process-local Supervisor;
2. the caller's durable `owner_agent_id`;
3. the EventStore used by the caller's Runtime.

The two stores must resolve to the same durable object identity. At execution,
the Tool context Session must equal the owner's durable Session. Targeted
operations rebuild the ownership graph and accept only strict descendants.
The model cannot supply or override `owner_agent_id`, and cannot operate self,
ancestors, siblings or another tree.

Preset and workspace identifiers remain explicit model inputs, but their
meaning is resolved by the host's `AgentActivationFactory`. The control plane
does not guess a model, path, example preset or workspace. Capability grants
and Budget are not exposed as enforced permissions because this Stage does not
enforce them.

### 3. Acceptance, waiting and collection remain separate

`send_agent_message` returns a `MessageReceipt`: durable acceptance only.
`wait_agent` waits for one explicit message identity; cancelling that waiter
does not cancel the Agent. It registers an in-process notification for that
message and immediately re-reads after registration to close the race. That
notification is only the low-latency path for a terminal written by the same
Activation. While waiting, the Supervisor also re-reads the durable report at
a bounded interval, because another supported Supervisor may write the
terminal without sharing the local Future. On wake it still returns only the
durable report. It never substitutes the broader Activation `wait_idle()`,
because a later message must not delay or fault an already completed earlier
message. `stop_agent` is the explicit lifecycle operation.

`collect_agent_artifact` loads an `AgentRunReport`. In Stage E “artifact” means
the child run result and durable evidence references. Its workspace
`artifact_refs` remain empty: a final text is not a `PatchArtifact`, and Git
branching/diff/merge belongs to v0.7.

### 4. Run reports are replay-derived

`AgentRunReportReader` joins Directory, Inbox, Delivery and Session events.
For a completion it requires:

- the claim Session equals the Agent's Session;
- one terminal delivery outcome for the requested message;
- exactly one matching `turn/start` and `turn/end` in valid order;
- the Session Turn uses the same message identity and terminal reason;
- at least one valid `assistant/message`, whose last content is the final text.

Missing, open or contradictory evidence fails closed. Failed/cancelled
outcomes may report status and stable reason but do not invent final text.
No live Activation or cached `TurnResult` is a fact source. Envelope access
and sequence comparisons share one hostile-evidence boundary, and missing vs
unsettled have distinct stable error codes.

### 5. Tool-call identity provides idempotency

Spawn request ids and send message ids are derived deterministically from the
bound owner and the current Session/Turn/Step/Tool Call identities. Replaying
the same Tool Call joins the same creation/acceptance fact rather than minting
a second child or message.

The Supervisor's shared create registers one waiter receipt per caller under
the same lock that owns the request single-flight. A freshly installed local
Activation begins in an unretained state; this is a revocable abandonment
opportunity, not a permanent `compensable` value captured at construction.
Returning a child handle from `create()`, or handing that same Activation to a
public `resume()`/wakeup path, atomically retains it under the Supervisor lock.
A cancelled caller may select abandonment only when it releases the final
waiter, no create caller has received the handle, the shared task's own outcome
proves that it installed a fresh local Activation, and no other public path has
retained that Activation. If abandonment wins the same lock race, later public
reuse fails closed instead of receiving a handle already committed to cleanup.
Reusing an identity found by the task's authoritative Directory re-read is
retained from the outset, even when this caller entered with an older valid
snapshot from before another create generation committed. This distinguishes
an orphaned first create from concurrent waiters, later replay and independent
public handoff: cancellation cannot dispose a child already delivered by any
supported path. The Tool does not infer provenance from a caller-local
Directory snapshot. The pending receipt remains registered until compensation
finishes; a retry arriving in that window leaves admission, joins the cleanup,
then reloads the durable identity instead of receiving the handle being
disposed. Compensation begins only after create admission has been released,
so subtree disposal cannot wait on the admission held by its own caller.

The Supervisor also owns each public `create()` invocation from entrance
through that post-admission compensation tail. The registry stores an
operation-level state and a method-return receipt; the caller Task is only the
temporary cancellation carrier while that exact invocation remains
registered **and has not entered its synchronous exit boundary**. As the first
step of `finally`, `create()` records that it is returning or raising; from that
no-await point onward, cancelling the caller could land only in work after the
public call. `aclose()` atomically stops new calls, cancels pre-admission calls
and shared candidates through their existing owners, completes forest cleanup,
then joins the observed invocation receipts. The return path schedules a
completion Task that cannot run before the public coroutine has returned to its
caller. That Task removes the invocation registration and resolves its receipt
under one Supervisor-lock acquisition, so close observes either the still-live
call or an already-published completion -- never a post-unregister/pre-return
gap. It never waits for unrelated work the caller performs after `create()`
returns, and a delayed post-return receipt after early validation failure does
not make that caller cancellable again. A close/compensation hand-off
event prevents a cycle: compensation that starts after close linearizes waits
for close's resource phase instead of awaiting the close Task that is itself
waiting for the public invocation. Therefore close cannot return while an
unreturned create or its cleanup is still live, and a caller may safely await
the same close after its create call has returned.

Repeated cancellation during compensation keeps waiting for the same cleanup
Task. Cleanup failure remains the cause of a bare `CancelledError`, and the
Supervisor retains the failed disposal Task for `aclose()` to report. A
`BaseExceptionGroup` containing cancellation is deliberately rejected here:
it would bypass the existing ToolRuntime/AgentLoop/Activation cancellation
boundaries and could leave the parent delivery claim permanently open. A child
already returned to the model is intentional; a later Turn interrupt does not
silently become subtree shutdown. Parent `dispose()`/`aclose()` retains Stage
D's lifecycle guarantee.

## Rejected alternatives

### Put subagent operations in `AgentLoop`

Rejected because the loop would gain identity, scheduling and lifecycle
ownership responsibilities and become the multi-agent fact source.

### Let the model pass `owner_agent_id`

Rejected because a prompt argument is not authority. It would allow self,
sibling or ancestor control and make lifecycle ownership mutable at call time.

### Make `send()` wait and return final text

Rejected because acceptance and completion are different durable facts. It
would make retries, cancellation and failure reporting ambiguous.

### Implement `wait_agent` as `wait_idle(agent_id)`

Rejected because idle is an Activation-wide property. A later message can run
forever or fault after the requested message already has a valid terminal
report; neither may change the requested message's join result.

### Treat a local message Future as the only progress signal

Rejected because Stage C supports more than one Supervisor over the same
durable Inbox and Delivery streams. A different Supervisor can write the
terminal without waking this process-local Future. Local notification is only
an optimization; bounded durable re-read preserves the actual wait contract.

### Infer spawn compensation ownership from a pre-call Directory read

Rejected because two concurrent first callers can both observe no durable
identity and then join the same create transaction. A valid snapshot can also
become stale while a prior pending generation commits and leaves the table.
Cleanup ownership must come from the actual shared create outcome together
with waiter and delivery state, not from a volatile boolean observed before
joining.

### Let close wait only for the shared candidate Task

Rejected because cancellation compensation deliberately runs after lifecycle
admission is released. A candidate can be finished while the public create
call is still converging subtree cleanup. Close must own and join that complete
operation without creating a close-waits-compensation-waits-close cycle.

### Use the caller Task as the create operation receipt

Rejected because one Task may call `create()` and then continue with unrelated
work or await `aclose()` itself. A close snapshot that awaits the whole Task can
therefore over-wait or form a self-cycle. The exact invocation needs its own
registration and return receipt; the caller Task is usable only to deliver
cancellation while that registration is still live.

### Unregister before publishing the method-return receipt

Rejected because close can linearize after registry removal but before the
public coroutine resumes and returns. It would then clean the new Activation,
return successfully and let `create()` hand its caller an already-disposed
handle. Registry removal and receipt publication occur together only after the
method has returned.

### Treat `owned_work is None` as permission to cancel the caller

Rejected because absence of an owned candidate describes both a create still
performing early validation and a create that has already raised to its caller
while the post-return receipt is waiting for the Supervisor lock. Only the
former may receive shutdown cancellation through its caller Task. The explicit
synchronous exit marker separates those states without waiting for a lock or
turning the caller Task into the operation receipt.

### Treat fresh-install provenance as permanent compensation authority

Rejected because another supported public path can retain the Activation after
installation but before the original create waiter records delivery. Resume
and wakeup handoff must revoke abandonment under the same lock used to select
cleanup, otherwise cancellation can destroy a handle already returned by a
different API.

### Group spawn cancellation and cleanup failure

Rejected because a group containing `CancelledError` is a `BaseExceptionGroup`,
not a cancellation signal understood by the existing execution path. Cleanup
failure is retained as cause and as the Supervisor's failed disposal Task
without sacrificing the durable cancelled terminal.

### Cache the child `TurnResult`

Rejected because a restart would lose the result while the logs remain. The
same child run must yield the same report to a fresh reader.

### Treat final text as a workspace patch

Rejected because no isolated branch, base revision, diff or merge evidence
exists in Stage E. Such a claim would make the Tool name stronger than its
contract.

### Cascade every parent Turn interrupt to all children

Rejected because `interrupt()` means one Turn cancellation in the current
control plane. Lifecycle ownership is enforced by `dispose()`/`aclose()`;
silently conflating the two would destroy intentionally persistent child work.

## Consequences and boundaries

- A suitably assembled parent model can create a child with its own durable
  Session, send work, wait, collect its durable final text and stop its subtree.
- Host policy still decides presets, workspaces, models, tools and Scope.
- The tools run in the same process and user security context; they are not an
  OS sandbox.
- No cross-process lease, cold recovery, stale-claim takeover, retry attempt,
  hierarchical Budget, workspace branch, Patch Artifact, Workflow or
  `NEXT_STEP` delivery is added.
- Version remains `0.5.0`; Stage E is not itself a v0.6 release.
