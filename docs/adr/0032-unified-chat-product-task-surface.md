# ADR-0032: One chat entry point above a durable ProductTask

- Status: Accepted; **F0-F5 implemented; v0.7.0 and the v0.7.1 authority correction released**
- Date: 2026-08-26
- Stage: v0.7-F0

## Status, precisely

This ADR records decisions, not shipped behaviour. What existed after F0 was
`traceh.api.product`: frozen values, two mode enums, a durable status enum with
its allowed transitions, a derived view status, the exact key set of every
ProductTask event, the host Profile, the preflight binding and Assembly Receipt,
the temporary Proposal and its confirmation rule, one read model and two narrow
protocols.

At F0 none of the event writer, parser, projector or service existed. F1 has
since implemented that ProductTask fact layer. Its implementation normalizes a
Proposal/Confirmation once before policy, evidence and persistence, and parses
each durable Product event once into domain-owned built-in values shared by
projection and idempotency checks; the built-in Unicode value is extracted
without dispatching a caller-controlled ``__str__``. Its Session replay also
proves that confirmation is a distinct message accepted only after the
Proposal-producing Turn ended; different identifiers alone are not temporal
evidence.

F2 has since implemented the strict router, the Profile Registry and the
Product Assembly: a parser that accepts exactly one JSON answer shape, a host
router boundary bounded entirely by an explicit `ProductRouterProfile`, the
single registry whose resolved assemblies make "the role slot decides write
authority" and "the router holds no Tool" checked facts, and an assembly
service that turns a confirmed task into one `ProductAssemblyReceipt` beside
the fixed Workflow definition its hash was taken from. It plans a task and
deliberately stops there: it starts no run, captures nothing, verifies nothing,
approves nothing, promotes nothing and calls no model.

F3 has since implemented the optional product host above the existing Chat,
Workflow, Agent, Budget, Workspace, Artifact and Promotion services. A model
may only leave an ephemeral proposal/confirmation suggestion for the current
Turn; after that Turn closes, the host replays the durable Session evidence and
an exact task-bound terminal prompt requires the host user to type `START`.
The model Tool Call cannot supply or bypass that capability gesture. The
Product controller starts the fixed Workflow, pauses at its Approval barrier,
and only a later host command may call Promotion. Restart continuation begins
from a task id and fresh domain replays, not from the old Chat process.

The `traceh eval` benchmark rework this ADR anticipated in section 15 is now
implemented in v0.7-F4 and recorded in
[ADR-0033](0033-product-task-benchmark-as-the-single-eval-path.md).

Real-provider RC grids, final review, packaging and offline installation have
since exercised this tree; annotated `v0.7.0` and `v0.7.1` GitHub Releases exist.
[ADR-0034](0034-separate-product-token-budget-and-request-output-limit.md)
records the released cumulative-Token/per-request-output split. A later
independent review found that the model's confirmation suggestion still acted
as start authority; v0.7.1 corrects that host boundary without changing the
ProductTask event schema, Workflow, Approval or Promotion authority. The
correction and its generic cancellation/target-venv companions are released in
`v0.7.1`.

## Context

v0.7-A through v0.7-E finished five independent domains, each owning one fact
source and one public service: hierarchical Budgets (ADR-0026/0027), managed Git
Workspaces (ADR-0028), immutable Patch Artifacts (ADR-0029), verified and
human-approved compare-and-swap promotion (ADR-0030), and a fixed typed Workflow
composing them (ADR-0031).

Every one of those is reachable only from host assembly code. There is no way for
a person to sit at a prompt, describe a change, watch it happen and approve it,
and there is no durable answer to "what is this task, and where did it get to".
A host that wanted one today would invent its own idea of a task, keep progress
in a status file, and end up with one more fact source that nothing checks
against the ones that already work.

The pressure at this point is to make the model do more: let it decide the shape
of the work, let it hold the evidence, let it say when something is ready. Every
one of those is a place where a plausible sentence would substitute for a
checked fact. This ADR is mostly about refusing that.

## Decision

### 1. `traceh chat` is the only product entry point, and plain chat stays plain

One command. Asking a question and having work done are the same conversation,
not two tools.

Ordinary conversation produces a Session and nothing else. No ProductTask, no
Workflow run, no Workspace, no task Budget account. This is not an optimisation:
it is what keeps the expensive, side-effecting machinery attached to an explicit
human decision rather than to whatever the model inferred from a question.

The seam already exists. `cli/chat.py` dispatches `/exit`, `/help`, `/session`
and `/plugins ...` in `_handle_command` **before** the model sees the input, so a
deterministic host command is not something new that has to be invented - it is
the path `/plugins` already takes.

### 2. A Proposal is temporary; only a confirmed one becomes durable

Before a task exists there is a Proposal: the requested mode, the exact commit a
branch name pointed at, the verification plan, the promotion target, the
resolved role and router assemblies, and all seven Budget dimensions of every
account, rendered by the host.

Being precise about its cost, because an earlier draft of this ADR was not: a
Proposal is normally *produced by* an ordinary chat Turn, and that Turn costs
Session tokens like any other. What a Proposal does not do is run the router,
open a ProductTask Budget account, or write a ProductTask event. The host
rendering of the preflight screen itself calls no model.

It is not an event, and leaving the process discards it. That is the point. A
proposal is a question, and the honest durable record of a question nobody
answered is no record at all. Persisting it would create a class of half-tasks
that a reader has to interpret, a projector has to age out and a recovery path
has to decide about - all to describe work that never started.

`ProductTaskProposal` is therefore a public value with no event type, and it
carries a `ProductPreflightBinding` rather than a full Assembly Receipt: an
`auto` Proposal has no resolved mode and no Workflow definition hash, because
the router has not run and will not run until the task exists. Pretending
otherwise would put a fabricated decision on the screen a person is reading.

Four rules make "go ahead" unambiguous and keep it a human act. At most one
Proposal is active per chat Session, so a new one replaces the previous. The
confirmation must come from that same Session - a Proposal belongs to one
conversation, and an acceptance arriving from another is not the same person
agreeing to the same thing. The confirming message must be distinct from the
requirement's origin message. And `proposal_confirmable()` requires the
confirming Turn to differ from `proposed_turn_id`: without those identity checks,
an older message could be relabelled as approval or a model could propose and
confirm in one breath.

`proposed_turn_id` is a separate field from `origin_turn_id`, and the first
version of this decision got that wrong. `origin_turn_id` is where the
*requirement* was stated; the Proposal is often offered in a later Turn - a user
asks a question, gets an answer, then says "alright, do it", and the model
proposes during that Turn. Comparing against the requirement Turn therefore let
a model propose in Turn 2 and confirm in Turn 2. The Turn that must differ is the
one that made the offer. Even that is not enough to establish "later": the F1
writer freshly replays the Session, requires the proposing Turn's durable
`turn/start` and `turn/end`, requires both claimed message Turns to have a real
durable `turn/start`, and requires each Turn to belong to exactly one claimed
message. It rejects any history the shared `CoreInvariantChecker`
finds lifecycle-invalid, and requires the confirmation's `inbox/accepted`
sequence to follow that end. A different but older message, one queued while
the Proposal response was still running, a claim naming a nonexistent Turn, or
a `turn/end` written over an open Step is not confirmation.

`ProposalConfirmation` carries the proposal id plus the confirming Session, Turn
and message, and nothing else. Mode, budgets, source, verification plan and
promotion target are already bound in the preflight, so a confirmation cannot
change them by gaining an argument. The identities establish the later user
message's context; they do not prove the semantics of its prose. Proving
`confirming_message_id` names a real durable user message **after the Proposal
Turn closed** is a fresh Session replay the writer performs. The message's
`source`, `content` and
`target` are accepted only as exact built-in strings and detached before any
comparison; `target` must be exactly `new_turn`, so a caller-controlled string
subclass cannot turn another delivery mode into user context. Ordinary Store read
failures are normalized as unavailable evidence without reflecting backend
details, while cancellation and interpreter-control `BaseException`s retain
their original meaning.

The later user Turn is necessary Session evidence but the model's classification
of that Turn is not authority. When the model suggests confirmation, the host
renders the exact pending task identity and accepts only a separate terminal
`START` control token. EOF, undecodable input or any other token starts nothing.
This is deliberately not a language-specific yes/no parser. The prompt is bound
to the one current Session Proposal; `confirm()` freshly rechecks and consumes
that exact pending object, so stale or cross-Session gestures cannot open a task.

Only after that host-owned capability gesture does the confirmation create
`product/task-opened`.

### 3. A ProductTask is not a second fact source

`product-task:<task_id>`, one append-only stream per task, inside the Event Store
that already exists. No database, no status file, no cache, no second scheduler.

It carries product identity, host control decisions, digests and *references*.
It does not carry the Agent's report, the Session's token count, the Workspace's
contents, the Patch bytes, the Review's evidence, the approval or the resulting
revision. Those belong to the Agent Directory, the Session stream, the Workspace
catalog, the Artifact catalog and the promotion ledger, and a reader resolves a
reference by replaying that source freshly.

This is the same rule ADR-0031 applied to the Workflow stream, for the same
reason: a copy is a second answer, and two answers eventually disagree. The
difference between them is only what they orchestrate - the Workflow arranges
nodes, the ProductTask records what a person asked for and what the host decided
about it.

Two payload fields do repeat an identity the envelope already carries, and both
are deliberate, following `agent/message-accepted` in the Inbox protocol:
`task_id` on every event lets a projector prove the payload and the stream name
agree instead of trusting either alone, and `workflow_run_id` on
`product/task-started` makes decision 4 below a checkable fact rather than an
assumption.

### 4. `workflow_run_id == task_id`

Not a stored mapping - an identity. A mapping would need its own reconciliation,
and a wrong entry would point a product answer at somebody else's orchestration
history. The read model derives it rather than holding a field, so there is no
place for the two to drift.

### 5. Both modes run through the same Workflow, with the same gates

`single` is `coder -> verification -> approval`. `multi` is
`parent -> reviewer -> coder -> verification -> approval`.

`single` is a shorter Workflow, not a shortcut past one. Every safety property -
the frozen verification plan, the immutable Artifact, the human approval barrier,
the compare-and-swap promotion - applies identically. A second "fast path" would
be a second execution engine whose gates would drift from the first one's the
moment either changed, and the fast path is exactly where someone would later be
tempted to skip a check.

Neither mode uses Map or Join. v0.7-E's fan-out is not removed or weakened; the
product surface simply does not need it yet, and shipping a fan-out nobody
exercises would be shipping an untested path.

### 6. `multi` is `parent -> reviewer -> coder`, and only the coder writes

The reviewer runs **before** the coder, and reviews the plan. The coder then
reads the parent's and reviewer's durable reports and does the work.

The obvious ordering - coder first, reviewer second - was rejected because
nothing consumes the reviewer's opinion. The fixed host verifier does not read
it, the Approval node does not read it, and no later node runs. That reviewer is
an expensive spectator whose objections change nothing. Putting it before the
coder is what makes it part of the execution rather than commentary on it.

Write authority follows the role and is not configurable: `ProductRole.CODER` is
`WRITABLE`, `PARENT` and `REVIEWER` are `READ_ONLY`. Exactly one role may write,
so "who could have produced these bytes" has one answer.

Getting this right took two attempts, and the first one is worth recording.
`ProductRoleProfile` originally carried its own `role` field, so a host could
place a coder-shaped profile in the `reviewer` slot and read back `writable` for
the reviewer - two facts about one role, disagreeing. The field is gone. The slot
on `ProductTaskProfile` is what makes a profile the parent, the reviewer or the
coder; `ProductRole.workspace_access` is the single definition of what that role
may do; and `ProductTaskProfile.role_profile()` is the one mapping between them,
running only from slot to profile. A profile now has no opinion on the subject
and no field with which to hold one.

Reports reach the coder through a bounded, host-rendered injection carrying
explicit truncation evidence - original bytes, injected bytes, whether it was
cut. An unbounded report is an unbounded prompt.

### 7. The router chooses between two values; the seam only parses its answer

The router runs only for `auto`. It returns `single` or `multi`.

`ResolvedTaskMode` has no `auto` member, so "unresolved" cannot survive into
execution. That much a type does enforce.

The seam is `TaskRoutingParser.parse(response: str) -> TaskRouting`, and it is
named for what it does: it parses an answer, it does not obtain one. The caller
creates the router Agent, owns its Session and Budget account, enforces the
timeout and response bound, and passes only the resulting text.

An earlier draft called this `TaskModeRouter` and claimed its synchronous
signature proved the router performs no I/O and holds no service handle. It
proves neither, and the claim was wrong rather than merely optimistic: a
synchronous method may block on a socket, and an object satisfying a Protocol
may hold whatever its `__init__` was given - a conforming implementation with
`self.supervisor` is trivial to write. What the signature does establish is
narrower and still worth having: the seam hands the implementation nothing but a
string, so no Supervisor, Workflow, Workspace, Artifact or Promotion handle
arrives *through it*.

That the router Agent is granted no Tool, is charged to its own Budget account
and runs inside its declared bounds is a property of the concrete implementation
and its assembly. It is evidenced by the resolved `router_assembly_digest` and
must be proven by architecture tests over the stage that writes the
implementation - not asserted by a Protocol declaration.

The concrete `ProductModeRouter` therefore receives the resolved assembly, not
a caller-supplied digest, and derives that digest with the registry's one
function. Before the first `auto` decision, the assembler compares both the live
Router Profile and this derived assembly identity with the freshly resolved
preflight. This is an identity check at the existing host seam, not a second
router registry or a second source of task state.

The router runs **after** `product/task-opened`, not before. Routing costs real
tokens, and capacity is only attributable to an Agent Session: `_require_agent_session`
in `budgets/enforcement.py` means a plain read-only chat turn never enters the
Budget ledger at all. So the router is a real Agent with its own Budget account,
and the task it is routing has to exist before it can be charged to anything.

`product/task-routed` carries one bounded, sanitized, **display-only**
`reason_display`. It is the single model-influenced string the product protocol
admits. It exists so a person can see why a mode was chosen; no code may branch
on it, and the decision is the enum beside it.

### 8. Approval and promotion are host operations, and the model never sees the evidence

There is no model-visible `approve`, `promote`, `update-ref` or `capture` tool,
and `traceh.api.product` exposes no such capability at all.

Beyond that: the approval digest, the Patch SHA-256 and the exact expected-old
and new revisions never enter the model's context. Not hidden behind a check -
absent. A value the model never received is a value it cannot paraphrase
incorrectly, and paraphrasing a digest is exactly how a person ends up approving
something other than what they read. The host renders that surface; the model can
only point at it.

### 9. The Workflow waits for approval; the product service promotes

The Approval node does what ADR-0031 already decided: it appends
`approval-awaited` and stops. It never approves.

Promotion happens **after** the run completes, called explicitly by the product
service. Making the Workflow promote would put a Git ref move inside a node,
where node failure semantics, retry-free execution and the run terminal would all
have to be re-reasoned about; and it would give the orchestrator an authority
ADR-0031 spent an architecture test proving it does not have.

Consequently `product/task-completed` carries a `promotion_id`. A task that is
approved but deliberately not promoted has no terminal in this vocabulary,
because the pipeline this ADR describes always promotes after approval. That is a
scope statement, not an oversight.

### 10. Recovery is not widened, and `interrupted` is never written down

v0.7-E continues exactly one interrupted run: one stopped cleanly at a human
Approval barrier. F0 does not relax that by one state.

A hard interruption therefore produces a **derived** answer, not an event.
`ProductTaskStatus` - what the stream alone says - has no `interrupted` member.
`ProductTaskView.status` is a **property**, not a field: as a field it was
suppliable, so a caller could hand back a view whose summary said `opened` and
whose status said `completed` - a second, contradicting copy of the fact the type
exists to derive.

It is computed from three fresh reads - the ProductTask, the Workflow and
ownership - and an earlier version used only two. With the Workflow ignored, an
unowned `started` task answered `interrupted` whether its run was still going, was
parked at the Approval barrier, or had already finished. Those call for inspect,
resume and reconcile respectively, so they cannot share an answer.

`PRODUCT_TASK_COHERENT_WORKFLOW` freezes when the two streams agree: nothing
before `started`, a running run at `started`, the barrier at `awaiting_approval`.
Terminal product statuses are absent, because a task that has ended owns its own
conclusion. Three view-only answers follow. `unreconciled` - the streams disagree
and the product stream is behind - is independent of ownership, because a live
host can find its own stream lagging after a failed append. `resumable` is an
unowned task parked exactly where ADR-0031 permits continuation. `interrupted` is
everything else unowned, and it deliberately means "a person has to look" rather
than "this cannot continue": whether Stage E would allow it depends on a node
having a start fact without a terminal one, which a run-level status cannot show.
`abandoned` remains legitimate only where the view says `interrupted`.

Recording `cancelled` there would claim a convergence nobody performed - in-process
cancellation converges owned work and can prove worktrees were released and holds
settled, and a process that died proves nothing. Recording `interrupted` durably
would freeze a guess that the very next read might contradict. A user may inspect
such a task, and may write the honest `product/task-abandoned`, which says the
task is out of the active list and explicitly does **not** claim the underlying
Agent claim, Budget hold or worktree was released.

`cancelled` and `abandoned` are separate event types for exactly this reason.

### 11. Five ends are five event types

`completed`, `rejected`, `cancelled`, `failed` and `abandoned` are five distinct
types with five exact key sets, not one `settled` event with many optional
fields.

A settled blob would make "completed carrying no promotion" and "cancelled
carrying a review id" *expressible* shapes that a projector would then reject by
convention. Here they cannot be expressed. Every event's key set is exact:
unknown keys and missing keys are refused rather than migrated, and no payload
carries an exception string, model output, a path, a credential or another
domain's state.

### 12. Shape is not order: the transitions are frozen too

Nine event shapes say what a fact may look like. They say nothing about what may
follow what, and a contract that stops there hands the sequencing decisions
straight back to the stage it was supposed to constrain.

`PRODUCT_TASK_TRANSITIONS` closes that, and `product_transition_allowed()` reads
it. `None` - no stream yet - admits only `product/task-opened`. Every terminal
maps to the empty set, so nothing follows an end. No status maps to itself, so
`routed`, `started` and `awaiting` each happen at most once.

Two edges depend on the requested mode and a status-only table cannot express
them, so the function takes it: a task that named `single` or `multi` has nothing
to route, and a task that asked for `auto` must be routed before it starts.
Without that, `opened -> started` would silently accept a task whose mode nothing
ever resolved.

`completed` and `rejected` are reachable only from `awaiting_approval`. A task
must not report the outcome of human review without having durably recorded that
it was waiting for one. That is an obligation on the writer, not a licence: a
process that dies after the Workflow appended `approval-awaited` but before the
product recorded `product/task-awaiting` must reconcile its own stream against
the Workflow's durable state before continuing, rather than skipping the missing
fact. `cancelled`, `failed` and `abandoned` are reachable from every non-terminal
status, because work can stop at any point.

Sequences that were legal under a shape-only contract and are refused now include
`opened -> completed`, `started -> routed`, `awaiting -> started` and appending
anything after a terminal.

The table is a read-only mapping, not a `dict`. An admission table any importer
can rewrite is not a contract; mutating one entry silently changes what every
later caller may append, and a reverse validation confirmed that a mutation
leaks into unrelated cases.

Order alone is still not enough, because the transition rule never sees a
payload. A task that asked for `single` could record `product/task-started`
carrying `multi`, and a rejection could name a review nobody awaited.
`ProductTaskFacts` and `product_required_values()` close that: where an earlier
fact already decided a value, there is exactly *one* legal value for the later
one, so it is **derived** rather than proposed-and-checked - the same rule the
Workflow projector applies to derived identities. An explicit request is its own
started mode; `auto` has none until routing produced one, and until then the
started fact cannot be appended at all; a rejection must name the awaited review.
Facts nothing earlier decided - a promotion id - carry no requirement.
`ProductTaskSummary.facts()` is the single place "what is already decided" is
assembled, so a writer and a projector cannot disagree about it.

`abandoned` gets its own binding through `product_view_status()`: a non-terminal
task that this process does not own derives to `interrupted`, and that is exactly
the condition - the only one - under which writing `product/task-abandoned` is
legitimate. Whether such a task can instead be *continued* is a different
question with a different source, the Workflow's own durable state, which
`ProductTaskView` carries beside the status.

### 13. A Profile decides who, never what the graph is

The Profile is a fixed-length schema. It has no nodes, no edges, no fan-out and
no DAG - the topology is decided by decisions 5 and 6, not by configuration. It
carries no raw verifier command, no repository path and no credential: provider
and model are host registry identities, the source is a registered id plus a
revision intent, and the verification plan and promotion target are ids the host
resolves.

Nothing has a code default. A missing decision is a missing field and a missing
field is a construction error, so a demo preset, a model name, a machine path or
a test fixture can never become the value a real task runs with. All seven Budget
dimensions are required at construction on every account, which is a property
`BudgetLimits` already had for the same reason: an omitted host decision must not
become a permissive default.

The numeric contract is not copied into the Product Registry. Profile
validation calls the Budget domain's `freeze_limits()`, so the accepted integer
types, `None` meaning, non-negative rule and `MAX_BUDGET_VALUE` ceiling are the
same facts the only Ledger writer enforces.

### 14. An Assembly Receipt is a binding, and its digest is derived

A Profile may say `main`. A task binds to the commit `main` resolved to.

The binding splits in two, because an `auto` Proposal cannot honestly carry a
resolved mode. `ProductPreflightBinding` holds everything resolving produces
before a mode is chosen; `ProductAssemblyReceipt` is that plus the resolved mode
and the Workflow definition hash it selects.

A name-only binding is not enough, and this was the second thing the first draft
got wrong. A host registry may keep every preset, provider and model name
spelled identically while resolving them to a different `AgentSpec`, different
capability grants or a different Tool/Prompt/Policy/Provider composition. The
profile digest cannot see that, and neither can the Workflow definition hash -
`workflow_definition_hash()` covers *binding ids*, not what a resolver returns
for them. So the preflight binding carries two digests the host computes over
what it actually resolved: `role_assembly_digest` over all three roles' resolved
specs, grants and compositions, and `router_assembly_digest` over the router's.
The second is what makes "the router was granted no tool" checkable at resume
rather than merely claimed.

Those two are *supplied*, because resolving a registry is I/O this module does
not do. What is derived is the digest over them, so a resolution result cannot
be recorded and then quietly disagreed with.

Every field is a non-secret identity, digest, exact branch ref or revision, so
the whole binding is safe to show a person and safe to keep in history. The
promotion target binding includes both repository fingerprint and exact ref:
two branches in one repository may point at the same commit while granting
different compare-and-swap authority.

The binding a person confirmed also has to survive into the stream, which the
first version did not arrange. `product/task-opened` records `preflight_digest`
and the exact Session, Turn and message the confirmation happened in. Without
them the Proposal could show one commit, one verification plan and one promotion
target; the world could move or the process could die; and
`product/task-started` could record a different Assembly Receipt with nothing in
the log able to contradict it. `ProductAssemblyReceipt.binds()` is the comparison that closes it.

It closes only half, and the first version of this section claimed otherwise.
`binds()` needs a Receipt, so only a caller holding one can evaluate it; a reader
with just the event stream has an opaque `assembly_digest` and cannot rebuild a
Receipt from it. Saying a projector could make "the same check while replaying"
was simply false.

The other half is arranged by having `product/task-started` repeat
`preflight_digest`, so a pure replay can at least compare the two facts. That
splits the started payload into two explicitly different guarantees: `mode`,
`workflow_run_id` and `preflight_digest` are checkable from earlier events alone
(`product_required_values()`), while `definition_hash`, `assembly_digest` and
`source_base_revision` are properties of a Receipt and can only be checked by a
Service holding one (`product_started_values()`).

`product_started_values()` is the single place a started payload is built:
everything the fact records beyond its own write identity is derived from one
Receipt, so a writer cannot assemble a payload that half-describes one binding
and half-describes another. Freezing only `mode` left `workflow_run_id`,
`definition_hash`, `assembly_digest` and `source_base_revision` free to name a
different task, a different definition, another receipt and another commit.

`digest` is a computed property, not a stored field. A supplied digest is a
second place the same fact can disagree with itself; a derived one cannot record
something other than what it describes, and cannot silently omit a field that was
added later. Provider identity, presets, capability grants, workspace access and
all seven Budget dimensions of every account are covered transitively through
`profile_digest` rather than repeated, for the same no-second-copy reason.

Resuming re-resolves and compares. Because the target's exact ref and expected
revision are both part of the binding, neither rebinding the target id to another
branch nor moving the original branch silently changes the authority a person
confirmed: it fails closed and must be opened again against what the branch is
now. Two long-running tasks against one branch therefore cannot both promote
without a human re-opening the second. That is the same refusal ADR-0030 makes on
target drift, applied one level up, and it is a real cost of not guessing.

### 15. `traceh eval` will be replaced, not paralleled

When the benchmark arrives it reuses and reworks the existing `traceh eval`. A
second benchmark path would mean two definitions of "did this work", and the one
nobody looks at would rot.

Old `case.json` manifests will be **refused explicitly**, not upcast. This
follows the same pre-1.0 rule as ADR-0025's Budget cutover: no alias, no
adapter, no dual reader, no silent rewrite, and never automatic deletion of the
user's old data.

F0 implements none of this. v0.7-F4 implements it; the decisions that this
section left open - how the measurement itself stays honest - are recorded
separately in [ADR-0033](0033-product-task-benchmark-as-the-single-eval-path.md)
rather than added here, because they are new decisions and this ADR is the F0
record.

### 16. F3 assembles existing owners; it does not create another runtime

The optional `--product-config` host file is schema 1 with an exact key set. It
selects one Profile, source repository, managed Workspace root, local CAS,
verification plan and bare promotion target. It cannot carry Workflow nodes,
edges, prompts, approval digests or a free-form Agent count. Without the flag,
ordinary Chat constructs no Product service and behaves exactly as before.

F3 v1 reuses the directly constructed built-in Provider of that Chat process.
The Product Profile provider/model must match the Chat runtime exactly; plugin
providers are rejected because the CLI has a registered capability but no
explicit Provider object whose identity and lifetime can be handed to the
Product host. F3 does not construct a second implicit model client.

The two model-visible tools hold only a process-local current-Turn action. They
do not hold a Supervisor, Workflow, Review or Promotion service. Proposal
rendering includes the exact bounded requirement the model proposed and may
include one explicit `single`, `multi` or `auto` mode when the user requested
it. Omitting the mode uses the Profile default. The mode, its source and the
one prospective task identity are rendered before confirmation; no durable
task exists until a later human message passes Session replay. Review ids,
Patch hashes, approval digests and promotion revisions are rendered by the
host and never appended to a later model request.

The host now also makes execution and approval readable without adding a
status stream. It prints the prospective task id as soon as the later
confirmation is accepted, emits optional monotonic waiting notices using the
existing Chat heartbeat interval, and replays ProductTask/Workflow progress on
each notice. At the Approval barrier and on `/task inspect`, a read-only
projection joins the fixed Workflow, Agent Directory, Artifact CAS and Review
ledger to show node status, Agent Session replay commands, changed paths,
bounded inert Patch text, and verifier status/exit/evidence digests. Missing or
tampered evidence is shown as unavailable with an explicit do-not-approve
warning. Promotion owns one shared frozen-plan Review validation: each result's
command id, position and argv digest, the definition/evidence digests and
`passed` must all match the host plan. The projection, Review reuse, direct
approval and promotion apply the same rule, so skipping the screen cannot
bypass it. If a Promotion receipt is durable but the Product terminal is not,
the recovery branch re-enters the idempotent Promotion operation before it
writes `product/task-completed`; a ledger lookup alone is not an alternate
approval authority. None of this projection is persisted or sent to a model.

Role and Router Profiles also carry an explicit per-request
`max_output_tokens`, separate from cumulative `BudgetLimits.max_tokens`; that
decision and its rejected alternatives are recorded in
[ADR-0034](0034-separate-product-token-budget-and-request-output-limit.md).

One non-model task-root Agent anchors the ownership tree and aggregate Budget.
Role Agents use the existing `ProcessAgentSupervisor`, Budget enforcement and
managed Git Workspace adapter. The Product execution adapter only resolves the
fixed F2 bindings and invokes the existing Workflow service; Workflow still
never calls `approve()` or `promote()`.

At the Approval barrier the process may exit. A new host reconstructs the
Product Assembly and Workflow state from the same EventStore, re-resolves the
Review from the Promotion ledger, and may approve by exact task id. The product
controller invokes Promotion explicitly only after the human command. Resource
cleanup precedes the Product terminal fact, so a crash between promotion and
cleanup remains retryable. A captured dirty tree is force-removed only when its
freshly re-derived Git tree equals the Artifact manifest; failure/cancellation
preserves dirty evidence in quarantine instead of discarding it.

Cross-stream writes are reconciled in one direction: if Workflow durably reached
Approval but ProductTask is still `started`, a fresh task operation appends the
missing awaiting fact without re-running a node. A failed Workflow is likewise
settled from its durable terminal. Other partial Workflow states remain
`interrupted`; F3 does not invent cold recovery or stale-claim takeover.

Routing and assembly deliberately occur after `product/task-opened`, because
their cost and decision belong to that task. Therefore every ordinary failure
between opening and Workflow execution is settled by the Product controller:
it first releases the existing ownership tree, Budget accounts and Workspaces,
then appends `product/task-failed` with a stable code. A cleanup or terminal
write failure retains the original error and leaves a retryable non-terminal
task. Internal Product reasons such as failed/cancelled are not copied into the
Workspace protocol; a clean Workspace uses that domain's existing
`explicit-release`, while dirty failure evidence remains quarantined.

### 17. No compatibility layer

There is nothing to be compatible with. No ProductTask event has ever been
written by any build, so F0 defines protocol version 1 and schema version 1 with
no legacy branch, and later stages refuse unknown versions rather than guessing.

## Consequences

- The product surface is one command with a durable, replayable answer to "what
  is this task and where did it get to", and that answer is assembled from the
  Agent Directory, the Session stream, the Workspace catalog, the Artifact
  catalog, the promotion ledger and the Workflow run stream rather than from a
  copy of any of them.
- The Product surface adds no Product dependency or state to the four protected
  kernel files. v0.7.1 separately changes `agent_loop.py` only at the generic
  repeated-cancellation finalizer; tests pin all four current byte sequences.
- The reviewer costs real tokens and produces something the coder actually reads.
- A branch that moves under an open task invalidates that task's binding.
- A task interrupted by a hard process exit needs a human. There is no answer
  that is both automatic and honest.
- `traceh.api.product` imports only `traceh.api`, so the dependency runs
  product -> workflow and never back.

## Rejected alternatives

- **Let the model approve, promote, or hold the approval digest.** A digest the
  model never received is one it cannot paraphrase wrongly.
- **Persist the Proposal.** A durable record of an unanswered question creates
  half-tasks that projection and recovery must then interpret.
- **A separate fast path for `single`.** Two engines whose gates drift, with the
  skipping happening on the path that was built to be quick.
- **Reviewer after coder.** Nothing consumes its opinion; the fixed verifier does
  not read it and no later node runs.
- **Make workspace access a Profile field.** A host could then grant the reviewer
  write access or take it from the coder, which is the whole safety property.
- **Give the router service handles, or let it emit a DAG.** It would become a
  second planner above Agent creation and Git promotion.
- **Let the router return `auto`, or a free-form string.** "Unresolved" would
  survive into execution, and prose would become a decision.
- **Promote inside the Workflow's Approval node.** ADR-0031 has an architecture
  test proving the domain calls no `approve`, `promote` or `compare_and_swap`.
- **One `settled` terminal with optional fields.** Impossible states become
  expressible and are then rejected only by convention.
- **Record `interrupted` as an event, or call it `cancelled`.** The first freezes
  a guess the next read may contradict; the second claims a convergence that a
  dead process never performed.
- **Store `workflow_run_id` as a field.** A mapping needs reconciliation and a
  wrong entry points at another run's history.
- **Let a host supply the assembly digest.** A supplied digest can disagree with
  what it claims to describe, and silently miss a field added later.
- **Repeat provider, presets, grants and budgets in the receipt.** A second copy
  of a fact is a second place it can be wrong; `profile_digest` covers them.
- **Keep the ProductTask in a status file or a cache.** A second fact source
  beside six good ones, diverging after the first restart.
- **Add a second benchmark path beside `traceh eval`.** Two definitions of "did
  this work", one of which rots.
- **Drop `task_id` from the payload because the stream name has it.** The Inbox
  protocol repeats `agent_id` on purpose, so a projector can prove the payload
  and the stream agree rather than trusting one of them. This was considered and
  rejected on that evidence.

- **Give `ProductRoleProfile` its own `role` field.** Two facts about one role can
  disagree, and the one that disagreed handed the reviewer write access.
- **Let a reader return an empty summary for an unknown task.** Every required
  field of a summary is established by `product/task-opened`; returning one
  without that event means inventing a status, a mode and three origin
  identities. `None` is the honest answer.
- **Bind the assembly on names alone.** A registry can rebind a preset without
  changing a single name, and the Workflow definition hash covers binding ids
  rather than resolved specs.
- **Freeze the event shapes and leave the ordering to F1.** The sequencing
  decisions are exactly the ones a contract exists to make.
- **Claim a synchronous Protocol proves an implementation does no I/O and holds
  no handles.** It proves neither; a conforming class with `self.supervisor` is
  three lines.
- **Describe the Proposal in prose and ship no type for it.** F1 would have had
  to redesign the product surface rather than implement a frozen one.
- **Open a task without recording the preflight the person confirmed.** The
  binding could drift between confirmation and start with nothing in the log
  able to object.
- **Compare a confirmation against the Turn the requirement was stated in.**
  That is routinely a different Turn from the one that made the offer, so a
  model could still propose and confirm in a single Turn.
- **Treat any different Turn or any real user message as "later".** Identifiers
  are not clocks. An older requirement message then becomes reusable approval,
  and a message queued before the Proposal response finishes can cross the
  boundary. The Session sequence must place its acceptance after the proposing
  Turn's durable end.
- **Freeze the status order and leave payload values to F1.** A `single` task
  could start as `multi`, and a rejection could name a review nobody awaited.
- **Keep the derived view status as a constructor field.** It becomes a second
  copy of the fact it was supposed to derive, free to contradict it.
- **Expose the transition table as a plain `dict`.** Any importer could rewrite
  what every later caller is allowed to append.
- **Record `reason_display` and drop it from the read model.** The one thing
  written for a person to read would never reach the person.
- **Freeze only `mode` on the started fact.** The run id, definition hash,
  assembly digest and base revision stayed free to describe a different task, a
  different definition, another receipt and another commit.
- **Claim a pure projector can evaluate `binds()`.** It holds an opaque digest,
  not a Receipt; the started fact has to repeat `preflight_digest` for a replay
  to check anything at all.
- **Derive the view status without reading the Workflow.** A clean Approval
  barrier, a finished run and a broken mid-node interruption collapse into one
  answer, and the three call for different actions.

## Explicit boundaries

F3 is an opt-in host assembly, not a default Profile, general Workflow DSL,
retry engine, cross-process lease, cold Activation recovery or OS sandbox. It
does not provide a model-visible approve/promote Tool. `AgentLoop`,
`AgentRuntime`, `ProcessAgentSupervisor` and `PluginManager` remain outside the
Product state machine. v0.7.1 changes `AgentLoop` only for its generic
Attempt/Step/Turn repeated-cancellation convergence; no Product import or state
enters it. F4 benchmark work is implemented separately in
[ADR-0033](0033-product-task-benchmark-as-the-single-eval-path.md). F5 release
stabilization, ADR-0034's Token-bound split, final review, packaging and release
are complete in annotated `v0.7.0`. The bounded maintenance corrections are
complete in annotated `v0.7.1` without beginning v0.8 or v0.9.
