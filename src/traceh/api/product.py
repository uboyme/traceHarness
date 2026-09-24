"""Public contract for the unified ``traceh chat`` product surface.

This module is a **frozen contract only**. v0.7-F0 defines what a ProductTask
is, what its append-only stream may say, in what order it may say it, what a
host Profile must decide and what a binding must prove. It implements no
parser, no projection, no service, no router, no chat controller and no CLI,
and it performs no I/O.

Three boundaries are load-bearing and are what every later stage inherits.

**A ProductTask is not a second fact source.** It records product identity,
host control decisions, digests and *references* into other domains. Whether an
Agent exists, what a Session cost, what a Workspace holds, which bytes a Patch
carries, what a Review proved, who approved it and where a ref now points all
stay owned by the Agent Directory, the Session stream, the Workspace catalog,
the Artifact catalog and the promotion ledger. A reader resolves a reference by
replaying those sources afresh, never by trusting a copy stored here.

**Approval and promotion are host decisions.** Nothing in this module exposes an
``approve`` or ``promote`` capability, and nothing here is shaped for a model to
call. The approval digest, the Patch SHA-256 and the exact Git revisions are
host-rendered surfaces; they are deliberately absent from every value a model
could receive.

**Derived is not durable.** :class:`ProductTaskStatus` is what the stream alone
says. :class:`ProductTaskViewStatus` is what a host answers after also checking
whether it still owns the work, and it is the only place ``interrupted`` exists.
A hard interruption never becomes a durable event.

A note on what a type can and cannot enforce. Several rules here are structural:
a value a dataclass has no field for cannot be supplied, an enum member that does
not exist cannot be selected, and a digest computed from a frozen value cannot
disagree with it. Others are contract text this module can only *state* - what a
host must compute before filling in a digest, and what a concrete implementation
must not hold. Those are marked, and enforcing them belongs to the stage that
writes the implementation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from traceh.api.budgets import BudgetLimits
from traceh.api.json_types import fingerprint
from traceh.api.workflow import WorkflowStatus
from traceh.api.workspaces import WorkspaceAccess

PRODUCT_TASK_PROTOCOL_VERSION = 6
"""The only ProductTask protocol this build describes."""

PRODUCT_TASK_SCHEMA_VERSION = 5
"""Schema version carried by every ProductTask event; unknown values are refused."""

PRODUCT_TASK_STREAM_PREFIX = "product-task:"
"""One append-only stream per task inside the existing Event Store.

F0 freezes the prefix only. The validated stream-id constructor belongs with the
writer, because building one requires the identifier rule this stage has not yet
applied to hostile input.
"""


# --------------------------------------------------------------------- modes


class RequestedTaskMode(StrEnum):
    """The execution strategy named by the confirmed Proposal or explicit Profile."""

    SINGLE = "single"
    MULTI = "multi"


class ResolvedTaskMode(StrEnum):
    """The exact confirmed strategy bound into the execution receipt."""

    SINGLE = "single"
    MULTI = "multi"


class TaskModeSource(StrEnum):
    """Where the confirmed Proposal's ``requested_mode`` came from.

    ``CONFIRMED_PROPOSAL`` means the Proposal named a mode - the user said "just
    use single", or the model's proposal carried a hint the user accepted.
    ``PROFILE`` means it named none and fell through to
    ``ProductTaskProfile.default_mode``. Both are reachable, and the distinction
    is what lets a reader tell an explicit human choice from a configured
    default.
    """

    CONFIRMED_PROPOSAL = "confirmed_proposal"
    PROFILE = "profile"


class ProductRole(StrEnum):
    """Host execution templates, distinct from task-specific investigation goals.

    Both modes run a coder. Multi creates one explicitly configured readonly
    investigator or isolated patch author inside that execution node.
    Neither mode adds Workflow Map or Join nodes."""

    CODER = "coder"
    INVESTIGATOR = "investigator"
    PATCH_AUTHOR = "patch_author"

    @property
    def workspace_access(self) -> WorkspaceAccess:
        """Write authority follows the role, and this is its only definition.

        Coder and explicitly granted patch author may write their own workspace.
        :class:`ProductRoleProfile` deliberately has
        no role or access field of its own, so the only way to ask what a role
        may do is to ask the role - a Profile cannot answer differently.
        """

        return (
            WorkspaceAccess.READ_ONLY
            if self is ProductRole.INVESTIGATOR
            else WorkspaceAccess.WRITABLE
        )


# ------------------------------------------------------------------- statuses


class ProductTaskStatus(StrEnum):
    """What one ProductTask stream, read alone, says about its task.

    Every member corresponds to a durable event. ``interrupted`` is absent on
    purpose: no event records it, because no event can prove it.
    """

    OPENED = "opened"
    STARTED = "started"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    FAILED = "failed"
    ABANDONED = "abandoned"


PRODUCT_TASK_TERMINAL_STATUSES = frozenset(
    {
        ProductTaskStatus.COMPLETED,
        ProductTaskStatus.REJECTED,
        ProductTaskStatus.CANCELLED,
        ProductTaskStatus.FAILED,
        ProductTaskStatus.ABANDONED,
    }
)
"""The five durable ends. Nothing may be appended to a task after one of them."""


PRODUCT_TASK_TRANSITIONS: Mapping[ProductTaskStatus | None, frozenset[ProductTaskStatus]] = (
    MappingProxyType(
        {
            None: frozenset({ProductTaskStatus.OPENED}),
            ProductTaskStatus.OPENED: frozenset(
                {
                    ProductTaskStatus.STARTED,
                    ProductTaskStatus.CANCELLED,
                    ProductTaskStatus.FAILED,
                    ProductTaskStatus.ABANDONED,
                }
            ),
            ProductTaskStatus.STARTED: frozenset(
                {
                    ProductTaskStatus.AWAITING_APPROVAL,
                    ProductTaskStatus.CANCELLED,
                    ProductTaskStatus.FAILED,
                    ProductTaskStatus.ABANDONED,
                }
            ),
            ProductTaskStatus.AWAITING_APPROVAL: frozenset(
                {
                    ProductTaskStatus.COMPLETED,
                    ProductTaskStatus.REJECTED,
                    ProductTaskStatus.CANCELLED,
                    ProductTaskStatus.FAILED,
                    ProductTaskStatus.ABANDONED,
                }
            ),
            ProductTaskStatus.COMPLETED: frozenset(),
            ProductTaskStatus.REJECTED: frozenset(),
            ProductTaskStatus.CANCELLED: frozenset(),
            ProductTaskStatus.FAILED: frozenset(),
            ProductTaskStatus.ABANDONED: frozenset(),
        }
    )
)
"""Which durable status may follow which, keyed by the current one.

It is a read-only view, not a `dict`. An admission table that any importer can
rewrite is not a contract: mutating one entry would silently change what every
later caller is allowed to append.

``None`` is "this task has no stream yet", so the only first fact is
``product/task-opened``. Every terminal maps to the empty set, so nothing can be
appended after an end. No status maps to itself, so ``routed``, ``started`` and
``awaiting`` each happen at most once.

Two consequences are worth stating because a shape-only contract would miss
them. ``completed`` and ``rejected`` are reachable **only** from
``awaiting_approval``: a task cannot report an outcome of human review without
having durably recorded that it was waiting for one. That imposes an obligation
on the writer rather than a licence - a process that dies after the Workflow
appended ``approval-awaited`` but before the product recorded
``product/task-awaiting`` must reconcile its own stream against the Workflow's
durable state before it may continue, not skip the missing fact.

And ``cancelled``, ``failed`` and ``abandoned`` are reachable from every
non-terminal status, because work can stop at any point.
"""


def product_transition_allowed(
    current: ProductTaskStatus | None,
    following: ProductTaskStatus,
    *,
    requested_mode: RequestedTaskMode,
) -> bool:
    """Whether the next status is legal for a task with an explicit supported mode.

    The transition table owns lifecycle order; product_required_values separately
    checks that later event payloads preserve the confirmed mode and identities."""

    allowed = PRODUCT_TASK_TRANSITIONS.get(current)
    if allowed is None or following not in allowed:
        return False
    return type(requested_mode) is RequestedTaskMode


class ProductTaskViewStatus(StrEnum):
    """What a host reports after reading the stream *and* checking ownership.

    This is a *derived* answer, not a record. It repeats the durable statuses so
    a caller has one vocabulary, and adds three view-only members that no event
    can carry: ``resumable``, ``unreconciled``, and ``interrupted``.
    """

    OPENED = "opened"
    STARTED = "started"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    FAILED = "failed"
    ABANDONED = "abandoned"
    INTERRUPTED = "interrupted"
    """Unowned, mid-flight, and with no clean point to pick up from.

    A previous process that died mid-node may have left an Agent claim, an open
    Turn, a Budget hold, a provisional Workspace, a running capture or a running
    Review behind, and neither stream says which. This view deliberately does not
    claim whether Stage E would permit continuation: that depends on whether some
    node has a start fact without a terminal one, which a run-level status cannot
    show. It means "a person has to look".

    Recording ``cancelled`` here would claim a convergence nobody performed;
    recording this durably would freeze a guess the very next read might
    contradict.
    """

    RESUMABLE = "resumable"
    """Unowned, but stopped exactly where Stage E is able to continue from.

    Both streams agree the run is parked at the human Approval barrier, which is
    the one interrupted state v0.7-E continues (see ADR-0031). Collapsing this
    into ``interrupted`` would send a person to inspect a task that could simply
    be picked up.
    """

    UNRECONCILED = "unreconciled"
    """The two durable streams disagree about how far the work got.

    The Workflow moved and the ProductTask has not caught up - a run reached the
    Approval barrier, or finished, while the product stream still says
    ``started``. This is the window the transition contract already names: the
    writer must bring its own stream level with the Workflow's durable state
    before continuing, rather than skipping the missing fact. It is not about
    ownership; a live host can find its own stream lagging after a failed append.
    """


# ------------------------------------------------------------- event contract


PRODUCT_TASK_OPENED = "product/task-opened"
PRODUCT_TASK_STARTED = "product/task-started"
PRODUCT_TASK_AWAITING = "product/task-awaiting"
PRODUCT_TASK_COMPLETED = "product/task-completed"
PRODUCT_TASK_REJECTED = "product/task-rejected"
PRODUCT_TASK_CANCELLED = "product/task-cancelled"
PRODUCT_TASK_FAILED = "product/task-failed"
PRODUCT_TASK_ABANDONED = "product/task-abandoned"


@dataclass(frozen=True, slots=True)
class ProductEventContract:
    """The exact shape one ProductTask fact may take.

    ``keys`` is the complete key set, not a minimum: a payload carrying an
    unknown key, or missing a known one, is refused rather than migrated.
    ``status`` is the durable status this fact establishes, which is what ties
    the shape contract to :data:`PRODUCT_TASK_TRANSITIONS`.
    """

    event_type: str
    schema_version: int
    keys: frozenset[str]
    status: ProductTaskStatus

    @property
    def terminal(self) -> bool:
        return self.status in PRODUCT_TASK_TERMINAL_STATUSES


PRODUCT_TASK_EVENTS: tuple[ProductEventContract, ...] = (
    ProductEventContract(
        event_type=PRODUCT_TASK_OPENED,
        schema_version=PRODUCT_TASK_SCHEMA_VERSION,
        keys=frozenset(
            {
                "task_id",
                "operation_id",
                "origin_session_id",
                "origin_turn_id",
                "origin_message_id",
                "requirement_digest",
                "profile_digest",
                "preflight_digest",
                "confirmation_session_id",
                "confirmation_turn_id",
                "confirmation_message_id",
                "requested_mode",
                "mode_source",
                "product_protocol_version",
            }
        ),
        status=ProductTaskStatus.OPENED,
    ),
    ProductEventContract(
        event_type=PRODUCT_TASK_STARTED,
        schema_version=PRODUCT_TASK_SCHEMA_VERSION,
        keys=frozenset(
            {
                "task_id",
                "operation_id",
                "mode",
                "workflow_run_id",
                "definition_hash",
                "assembly_digest",
                "preflight_digest",
                "source_base_revision",
            }
        ),
        status=ProductTaskStatus.STARTED,
    ),
    ProductEventContract(
        event_type=PRODUCT_TASK_AWAITING,
        schema_version=PRODUCT_TASK_SCHEMA_VERSION,
        keys=frozenset({"task_id", "operation_id", "review_id"}),
        status=ProductTaskStatus.AWAITING_APPROVAL,
    ),
    ProductEventContract(
        event_type=PRODUCT_TASK_COMPLETED,
        schema_version=PRODUCT_TASK_SCHEMA_VERSION,
        keys=frozenset({"task_id", "operation_id", "promotion_id"}),
        status=ProductTaskStatus.COMPLETED,
    ),
    ProductEventContract(
        event_type=PRODUCT_TASK_REJECTED,
        schema_version=PRODUCT_TASK_SCHEMA_VERSION,
        keys=frozenset({"task_id", "operation_id", "review_id"}),
        status=ProductTaskStatus.REJECTED,
    ),
    ProductEventContract(
        event_type=PRODUCT_TASK_CANCELLED,
        schema_version=PRODUCT_TASK_SCHEMA_VERSION,
        keys=frozenset({"task_id", "operation_id", "reason_code"}),
        status=ProductTaskStatus.CANCELLED,
    ),
    ProductEventContract(
        event_type=PRODUCT_TASK_FAILED,
        schema_version=PRODUCT_TASK_SCHEMA_VERSION,
        keys=frozenset({"task_id", "operation_id", "failure_code"}),
        status=ProductTaskStatus.FAILED,
    ),
    ProductEventContract(
        event_type=PRODUCT_TASK_ABANDONED,
        schema_version=PRODUCT_TASK_SCHEMA_VERSION,
        keys=frozenset({"task_id", "operation_id", "reason_code"}),
        status=ProductTaskStatus.ABANDONED,
    ),
)
"""Every ProductTask fact, one entry per event type.

The five ends are five *types*, each with its own exact key set, rather than one
``settled`` event whose meaning depends on which optional fields happen to be
present. A single settled blob would make "completed without a promotion" and
"cancelled carrying a review id" expressible shapes that a projector would then
have to reject by convention; here they are not expressible at all.

Two payload fields repeat an identity the envelope already carries, and both do
so deliberately, following ``agent/message-accepted``: ``task_id`` lets a
projector prove the payload and the stream name agree instead of trusting either
alone, and ``workflow_run_id`` on ``product/task-started`` is what makes the
``run_id == task_id`` rule a checkable fact rather than an assumption.

``product/task-opened`` records ``preflight_digest`` and the exact Session,
Turn and message the person confirmed in, and ``product/task-started`` repeats
that digest. Without those, the stream could show a task that started against a
binding nobody agreed to: the Proposal screen showed one commit, one verification
plan and one promotion target, the world moved, and ``product/task-started``
recorded a different Assembly Receipt with nothing to contradict it. Repeating the
digest is what lets a reader holding *only events* compare the two, because
``assembly_digest`` is opaque to it - see :func:`product_required_values` for
which relations are checkable that way and which need the Receipt itself.

Nothing here carries an exception string, model output, a filesystem path, a
credential or a copy of another domain's state. ``reason_code`` and
``failure_code`` are stable host identifiers; ``reason_display`` is the single
bounded, sanitized, display-only string in the protocol - see
:class:`TaskRouting`.
"""

PRODUCT_TASK_EVENT_TYPES: tuple[str, ...] = tuple(
    contract.event_type for contract in PRODUCT_TASK_EVENTS
)

PRODUCT_TASK_TERMINAL_EVENT_TYPES = frozenset(
    contract.event_type for contract in PRODUCT_TASK_EVENTS if contract.terminal
)


def product_event_contract(event_type: str) -> ProductEventContract | None:
    """The contract for ``event_type``, or ``None`` if this build does not know it.

    Returning ``None`` rather than raising keeps the decision with the writer or
    projector, which owns the stable error vocabulary; this is a lookup, not a
    validator.
    """

    for contract in PRODUCT_TASK_EVENTS:
        if contract.event_type == event_type:
            return contract
    return None


@dataclass(frozen=True, slots=True)
class ProductTaskFacts:
    """Earlier facts that every later event must preserve.

    Legal status order alone cannot prevent single from starting as multi,
    or a rejection from naming another Review. product_required_values derives
    the unique values permitted by these earlier facts."""

    task_id: str
    requested_mode: RequestedTaskMode
    preflight_digest: str
    resolved_mode: ResolvedTaskMode | None = None
    awaited_review_id: str | None = None


def product_required_values(event_type: str, facts: ProductTaskFacts) -> Mapping[str, str] | None:
    """Derive values fixed by earlier events, without resolving external identities.

    The Product stream fixes the confirmed mode, task/Workflow identity and
    preflight digest. A full Receipt is needed to check definition hash, assembly
    digest and source base; product_started_values and ProductAssemblyReceipt.binds
    own those checks. None means a required earlier fact is absent; an empty
    mapping means this event carries no value fixed by the earlier Product facts."""

    if event_type == PRODUCT_TASK_STARTED:
        mode = product_started_mode(facts)
        if mode is None:
            return None
        return MappingProxyType(
            {
                "mode": mode.value,
                "workflow_run_id": facts.task_id,
                "preflight_digest": facts.preflight_digest,
            }
        )
    if event_type == PRODUCT_TASK_REJECTED:
        if facts.awaited_review_id is None:
            return None
        return MappingProxyType({"review_id": facts.awaited_review_id})
    if event_type in PRODUCT_TASK_EVENT_TYPES:
        return MappingProxyType({})
    return None


def product_started_mode(facts: ProductTaskFacts) -> ResolvedTaskMode | None:
    """Derive the execution mode from the exact confirmed request."""

    return ResolvedTaskMode(facts.requested_mode.value)


# --------------------------------------------------------------------- profile


@dataclass(frozen=True, slots=True)
class ProductContextPolicy:
    """One role's request-window governance, stated in full or not at all.

    Product previously passed no window policy at all, so its Agents had no
    request metering and therefore no compaction: a long task simply carried
    every tool result into every later request. Turning that on is a host
    decision with real cost consequences, so there is no partial form and no
    inferred value - a role either states every field or governs nothing.

    The fold marks are optional *within* a stated policy: metering without
    in-Turn folding is a legitimate choice, and `TokenBudgetPolicy` refuses a
    half-configured pair on its own.
    """

    encoding: str
    window_tokens: int
    output_reserve_tokens: int
    safety_margin_tokens: int
    trigger_percent: int
    fold_relief_percent: int | None
    fold_protect_recent_groups: int | None
    fold_protect_readback_utf8_bytes: int
    compaction_trigger_utf8_bytes: int
    compaction_max_summary_utf8_bytes: int
    compaction_keep_recent_turns: int

    def to_dict(self) -> dict[str, object]:
        # Derived from the fields rather than a literal key list, so adding a
        # field cannot leave the digest silently blind to it - and so this
        # module keeps its rule that it contains no example-shaped literals.
        return {field.name: getattr(self, field.name) for field in fields(self)}


@dataclass(frozen=True, slots=True)
class ProductWrapUpReserve:
    """Budget held back from investigation so a report can still be written."""

    steps: int
    tool_calls: int
    wall_milliseconds: int

    def to_dict(self) -> dict[str, object]:
        return {field.name: getattr(self, field.name) for field in fields(self)}


@dataclass(frozen=True, slots=True)
class ProductRoleProfile:
    """A bounded host template whose slot determines its authority.

    It has no independent role or workspace-access field. Putting a coder-shaped
    profile in the investigator slot cannot grant write access. Presets and grants
    are resolved and checked by the existing ProductProfileRegistry."""

    preset: str
    capability_grants: tuple[str, ...]
    max_output_tokens: int
    budget: BudgetLimits
    max_turn_wall_milliseconds: int
    #: ``None`` keeps the historical behaviour: no metering, no compaction. It
    #: is an explicit choice recorded in the frozen assembly digest, not a
    #: silently absent capability.
    context_policy: ProductContextPolicy | None = None
    #: How much of this role's granted budget is held back so it can still
    #: deliver. ``None`` keeps the historical behaviour: the role investigates
    #: until something cancels it, and delivers nothing.
    wrap_up_reserve: ProductWrapUpReserve | None = None


@dataclass(frozen=True, slots=True)
class ProductTaskProfile:
    """Every general value the product surface uses, stated explicitly.

    Nothing here is a code default. A missing decision is a missing field, and a
    missing field is a construction error - so a demo preset, a model name, a
    machine path or a test fixture can never become the value a real task runs
    with. It also carries no secret: provider and model are host *registry*
    identities, not endpoints or keys, and the source is a registered id plus a
    revision intent, not a filesystem path.

    ``source_revision`` may be a moving name such as a branch. A task binds to
    the exact commit that name resolved to, recorded in
    :class:`ProductPreflightBinding`.

    Every value here is a **name**. What those names currently resolve to is a
    separate fact, and one this module cannot see; that is what the resolved
    assembly digests on the preflight binding are for.
    """

    profile_version: int
    default_mode: RequestedTaskMode
    provider_id: str
    model_id: str
    coder: ProductRoleProfile
    investigator: ProductRoleProfile
    patch_author: ProductRoleProfile | None
    retained_tokens: int
    investigator_initial_tokens: int
    task_budget: BudgetLimits
    source_id: str
    source_revision: str
    verification_plan_id: str
    promotion_target_id: str

    def role_profile(self, role: ProductRole) -> ProductRoleProfile | None:
        """The profile occupying ``role``'s slot.

        This is the only mapping between a role and its configuration, and it
        runs one way: the slot decides the role. Ask
        :attr:`ProductRole.workspace_access` what that role may do; the returned
        profile has no opinion on the subject.
        """

        return {
            ProductRole.CODER: self.coder,
            ProductRole.INVESTIGATOR: self.investigator,
            ProductRole.PATCH_AUTHOR: self.patch_author,
        }[role]

    @property
    def digest(self) -> str:
        """A digest over the whole Profile, computed rather than supplied.

        Every field participates, so a decision cannot be left out of the
        binding by forgetting to add it here; and because the value is derived,
        a caller cannot record a digest that disagrees with the configuration it
        claims to describe.

        It covers the host's *names and numbers* and nothing more. Two runs whose
        profile digests agree may still have resolved ``preset`` to two different
        Agents.
        """

        return fingerprint(
            {
                "protocol": PRODUCT_TASK_PROTOCOL_VERSION,
                "purpose": "product-task-profile",
                "profile": self,
            }
        )


# ------------------------------------------------------------------- bindings


@dataclass(frozen=True, slots=True)
class ProductPreflightBinding:
    """Non-secret identities and exact revisions resolved before confirmation.

    role_assembly_digest covers the resolved coder, multi coder and investigator
    compositions, including specs, grants and ordered tools/prompts/policies. Names
    alone cannot detect registry rebinding. Source, verification and promotion
    bindings remain separate owner identities; this value stores no local paths
    or credentials and grants no execution or promotion authority."""

    profile_digest: str
    role_assembly_digest: str
    repository_fingerprint: str
    base_revision: str
    verification_plan_digest: str
    promotion_target_fingerprint: str
    promotion_target_ref: str
    promotion_expected_revision: str

    @property
    def digest(self) -> str:
        return fingerprint(
            {
                "protocol": PRODUCT_TASK_PROTOCOL_VERSION,
                "purpose": "product-preflight-binding",
                "binding": self,
            }
        )


@dataclass(frozen=True, slots=True)
class ProductAssemblyReceipt:
    """Bind the confirmed preflight to the execution mode and exact Workflow hash.

    A Proposal binds what the person reviewed. Assembly re-resolves those bindings
    and refuses drift before deriving this receipt. The receipt describes the
    Workflow that can run; it is not itself proof that execution or approval occurred."""

    preflight: ProductPreflightBinding
    resolved_mode: ResolvedTaskMode
    workflow_definition_hash: str

    def binds(self, preflight_digest: str) -> bool:
        """Whether this receipt was built on the preflight a person confirmed.

        ``product/task-opened`` records the digest of what the Proposal screen
        showed. A receipt assembled later - after a re-resolve, a restart, or a
        registry change - is only allowed to start the task if it still rests on
        that exact binding.

        This check needs the Receipt, so only a caller holding one can make it; a
        reader with just the event stream cannot rebuild a Receipt from an opaque
        digest. What that reader *can* do is compare the ``preflight_digest`` on
        ``product/task-started`` against the one on ``product/task-opened``, which
        is why the started fact repeats it.
        """

        return self.preflight.digest == preflight_digest

    @property
    def digest(self) -> str:
        """What ``assembly_digest`` records, derived from every field above.

        Provider and model identity, the three role presets and their capability
        grants, all seven Budget dimensions of all five accounts and the resolved
        Agent assemblies are covered through the preflight binding: changing any
        one of them changes its digest and therefore this one. They are not
        repeated here, because a second copy is a second place the same fact can
        disagree with itself.
        """

        return fingerprint(
            {
                "protocol": PRODUCT_TASK_PROTOCOL_VERSION,
                "purpose": "product-assembly-receipt",
                "receipt": self,
            }
        )


def product_started_values(*, task_id: str, receipt: ProductAssemblyReceipt) -> Mapping[str, str]:
    """Every value ``product/task-started`` carries, derived from one Receipt.

    This is the only place a started payload is built. ``task_id`` and
    ``operation_id`` are the task's own identity and the write's; everything else
    that fact records is a property of the Receipt, so deriving them all here
    means a writer cannot assemble a payload that half-describes one binding and
    half-describes another.

    A caller must hold the Receipt to evaluate this, which is exactly why
    :func:`product_required_values` exists beside it: a pure projector cannot,
    and must be told which subset it can still verify.
    """

    return MappingProxyType(
        {
            "mode": receipt.resolved_mode.value,
            "workflow_run_id": task_id,
            "definition_hash": receipt.workflow_definition_hash,
            "assembly_digest": receipt.digest,
            "preflight_digest": receipt.preflight.digest,
            "source_base_revision": receipt.preflight.base_revision,
        }
    )


# -------------------------------------------------------------------- proposal


@dataclass(frozen=True, slots=True)
class ProductTaskProposal:
    """One offered task, held in memory and never written down.

    A Proposal is a question. The honest durable record of a question nobody
    answered is no record at all, so this is process-local state: leaving the
    process discards it, and no event type can carry it.

    Rules a host must keep, which this shape supports but cannot enforce alone:

    * **At most one active Proposal per chat Session.** A new one replaces the
      previous, so a bare "go ahead" is never ambiguous about which offer it
      accepted;
    * **Confirmation names the exact ``proposal_id``** and carries nothing else -
      see :class:`ProposalConfirmation`;
    * **Confirmation is a different message accepted after the Turn that
      produced the Proposal ended** - the pure part is checked by
      :func:`proposal_confirmable`; the durable ordering proof belongs to the
      ProductTask writer's fresh Session replay.

    The three ``origin_*`` fields identify where the *requirement* was stated,
    and they must be a consistent triple: the Turn is the one that claimed that
    message in that Session. A writer proves that by replaying the Session, not
    by trusting the values it was handed.

    ``proposed_turn_id`` is a different fact and exists because conflating the
    two broke the rule above. The requirement may be stated in one Turn and the
    Proposal offered in a later one - a user asks a question, gets an answer,
    then says "alright, do it", and the model proposes during *that* Turn. The
    Turn a confirmation must differ from is the one that produced the offer, not
    the one that stated the need; comparing against ``origin_turn_id`` let a
    model propose and confirm inside a single Turn whenever those differed.
    """

    proposal_id: str
    origin_session_id: str
    origin_turn_id: str
    origin_message_id: str
    proposed_turn_id: str
    requirement_digest: str
    requested_mode: RequestedTaskMode
    mode_source: TaskModeSource
    preflight: ProductPreflightBinding


@dataclass(frozen=True, slots=True)
class ProposalConfirmation:
    """A trusted host attesting acceptance of one exact Proposal.

    It carries an id and the later user-message context, and nothing else. Mode,
    budgets, source, verification plan and promotion target are already bound in
    the Proposal's preflight, so a confirmation has no field with which to change
    any of them. A model may only suggest that an interactive host request this
    operation; the host's explicit task-bound authorization remains outside the
    model Tool and this value.

    The Session, Turn and message make the context and ordering checkable.
    ``confirming_message_id`` must name a real later durable user message in that
    Session; a fresh replay proves those facts, but deliberately does not parse
    natural-language consent. The trusted host owns that authorization boundary.
    """

    proposal_id: str
    confirming_session_id: str
    confirming_turn_id: str
    confirming_message_id: str


def proposal_confirmable(proposal: ProductTaskProposal, confirmation: ProposalConfirmation) -> bool:
    """Whether ``confirmation`` may open a task from ``proposal``.

    Three conditions. The ids must match exactly, so a stale confirmation cannot
    accept a Proposal that has since been replaced.

    The confirmation must come from the Session the Proposal was offered in. A
    Proposal belongs to one conversation; an acceptance arriving from another is
    not the same person agreeing to the same thing.

    The confirming message must not be the requirement's origin message, and
    the confirming Turn must differ from ``proposed_turn_id`` - the Turn that
    produced the offer. Without both checks, an earlier real user message could
    be relabelled as an acceptance, or a model could propose and confirm in one
    breath. The concrete writer additionally proves from Session sequence
    numbers that the confirming message was accepted only after the proposing
    Turn ended; identifiers alone cannot express temporal order.

    What this cannot decide is whether ``confirming_message_id`` names a real
    durable user message, or whether its prose semantically grants authority.
    The first is a fresh Session replay owned by the writer; the second is an
    explicit host capability decision, never a model classification.
    """

    if type(proposal) is not ProductTaskProposal or type(confirmation) is not ProposalConfirmation:
        return False
    values = tuple(
        _plain_proposal_text(value)
        for value in (
            proposal.proposal_id,
            proposal.origin_session_id,
            proposal.origin_message_id,
            proposal.proposed_turn_id,
            confirmation.proposal_id,
            confirmation.confirming_session_id,
            confirmation.confirming_turn_id,
            confirmation.confirming_message_id,
        )
    )
    if any(value is None for value in values):
        return False
    (
        proposal_id,
        origin_session_id,
        origin_message_id,
        proposed_turn_id,
        confirmed_proposal_id,
        confirming_session_id,
        confirming_turn_id,
        confirming_message_id,
    ) = values
    return (
        confirmed_proposal_id == proposal_id
        and confirming_session_id == origin_session_id
        and confirming_message_id != origin_message_id
        and confirming_turn_id != proposed_turn_id
    )


def _plain_proposal_text(value: object) -> str | None:
    """Detach comparison from caller-controlled ``str`` methods.

    A frozen dataclass freezes the reference, not the behaviour of an object a
    caller placed in it. Converting once to an exact built-in value prevents a
    ``str`` subclass from defining what equality means at this authorization
    boundary. Identifier shape is enforced by the concrete writer; this pure
    F0 helper only owns the comparison rule.
    """

    if not isinstance(value, str):
        return None
    try:
        # Call the built-in descriptor directly. ``str(value)`` dispatches to a
        # subclass's mutable ``__str__`` and can produce a different identity
        # each time the same frozen DTO is inspected.
        normalized = str.__str__(value)
    except Exception:
        return None
    return normalized if type(normalized) is str else None


# ------------------------------------------------------------------ read model


@dataclass(frozen=True, slots=True)
class ProductTaskSummary:
    """One ProductTask rebuilt solely from its durable stream.

    Required fields come from task-opened; optional fields remain None until their
    establishing events exist. An unopened task has no invented empty summary.
    Review and Promotion identities refer to their original ledger, and origin
    and confirmation identities refer to their original Sessions. Consumers
    resolve those owners when they need evidence beyond this Product stream."""

    def facts(self) -> ProductTaskFacts:
        """What this task has established, for checking the next fact.

        Assembling it here keeps one definition of "what is already decided",
        so a writer and a projector cannot disagree about it.
        """

        return ProductTaskFacts(
            task_id=self.task_id,
            requested_mode=self.requested_mode,
            preflight_digest=self.preflight_digest,
            resolved_mode=self.resolved_mode,
            awaited_review_id=(
                self.review_id if self.status is ProductTaskStatus.AWAITING_APPROVAL else None
            ),
        )

    task_id: str
    status: ProductTaskStatus
    requested_mode: RequestedTaskMode
    mode_source: TaskModeSource
    requirement_digest: str
    profile_digest: str
    preflight_digest: str
    origin_session_id: str
    origin_turn_id: str
    origin_message_id: str
    confirmation_session_id: str
    confirmation_turn_id: str
    confirmation_message_id: str
    head_seq: int
    resolved_mode: ResolvedTaskMode | None = None
    definition_hash: str | None = None
    assembly_digest: str | None = None
    source_base_revision: str | None = None
    review_id: str | None = None
    promotion_id: str | None = None
    reason_code: str | None = None
    failure_code: str | None = None

    @property
    def workflow_run_id(self) -> str:
        """The run id *is* the task id.

        Deriving it removes the only place the two could drift. A stored mapping
        would need its own reconciliation, and a wrong entry would point a
        product answer at somebody else's orchestration history.
        """

        return self.task_id

    @property
    def settled(self) -> bool:
        return self.status in PRODUCT_TASK_TERMINAL_STATUSES


PRODUCT_TASK_COHERENT_WORKFLOW: Mapping[ProductTaskStatus, frozenset[WorkflowStatus | None]] = (
    MappingProxyType(
        {
            ProductTaskStatus.OPENED: frozenset({None}),
            ProductTaskStatus.STARTED: frozenset({WorkflowStatus.RUNNING}),
            ProductTaskStatus.AWAITING_APPROVAL: frozenset({WorkflowStatus.AWAITING_APPROVAL}),
        }
    )
)
"""Which Workflow state agrees with each non-terminal ProductTask status.

``None`` means no run exists yet, which is the only coherent answer before
``product/task-started``. Anything outside a status' set means the two durable
streams have diverged and the product stream is behind: a run that reached the
Approval barrier or finished while the task still says ``started``, or a run that
exists before the task claims to have started one.

Terminal product statuses are absent on purpose. Once a task has durably ended,
its own stream is the answer and the Workflow cannot change it.
"""


def product_view_status(
    summary: ProductTaskSummary,
    *,
    workflow_status: WorkflowStatus | None,
    owned_by_this_host: bool,
) -> ProductTaskViewStatus:
    """The derived answer a host reports right now, from three fresh reads.

    All three participate, and an earlier version's failure to use the second is
    what this signature fixes: with only the ProductTask and ownership, a task
    parked cleanly at the Approval barrier, one whose Workflow had already
    finished, and one abandoned mid-node all collapsed into ``interrupted``. Those
    call for three different actions - resume, reconcile, and inspect - so they
    cannot share an answer.

    A settled task reports its durable end; nothing else is consulted, because a
    task that has ended owns its own conclusion.

    Otherwise the Workflow must agree with where the task says it is. If it does
    not, the answer is ``unreconciled`` regardless of ownership: the writer has to
    catch its own stream up before anything else is true.

    When they agree, ownership decides. A live owner reports the durable status.
    An unowned task parked at the Approval barrier is ``resumable`` - the one
    interrupted state Stage E can continue. Anything else unowned is
    ``interrupted``, which is the sole condition under which writing
    ``product/task-abandoned`` is legitimate.
    """

    if summary.settled:
        return ProductTaskViewStatus(summary.status.value)
    coherent = PRODUCT_TASK_COHERENT_WORKFLOW.get(summary.status)
    if coherent is None or workflow_status not in coherent:
        return ProductTaskViewStatus.UNRECONCILED
    if owned_by_this_host:
        return ProductTaskViewStatus(summary.status.value)
    if summary.status is ProductTaskStatus.AWAITING_APPROVAL:
        return ProductTaskViewStatus.RESUMABLE
    return ProductTaskViewStatus.INTERRUPTED


@dataclass(frozen=True, slots=True)
class ProductTaskView:
    """What a host reports right now, after reading every relevant source.

    This is deliberately a different type from :class:`ProductTaskSummary`. The
    summary is a replay of durable facts and is stable; this is an observation
    that includes whether the work is still owned, and the next read may answer
    differently. Keeping them apart is what stops a derived ``interrupted`` from
    being mistaken for something that was written down.

    All three fields feed :attr:`status`. ``workflow_status`` is what separates a
    task parked cleanly at the Approval barrier from one whose Workflow has moved
    on without it and one that simply lost its owner mid-flight.

    :attr:`status` is a computed property rather than a field. As a field it was
    suppliable, so a caller could hand back a view whose summary said ``opened``
    and whose status said ``completed`` - a second, contradicting copy of exactly
    the fact this type exists to derive.
    """

    summary: ProductTaskSummary
    workflow_status: WorkflowStatus | None
    owned_by_this_host: bool

    @property
    def status(self) -> ProductTaskViewStatus:
        return product_view_status(
            self.summary,
            workflow_status=self.workflow_status,
            owned_by_this_host=self.owned_by_this_host,
        )


# ------------------------------------------------------------------- protocols


class ProductTaskReader(Protocol):
    """Fresh replay of one ProductTask stream.

    ``None`` means the task has no stream. That is not an error and not an empty
    summary: with no ``product/task-opened`` there is no status, no mode and no
    origin, and returning a value shaped like a summary would require inventing
    all three.
    """

    async def load(self, task_id: str) -> ProductTaskSummary | None: ...


__all__ = [
    "PRODUCT_TASK_ABANDONED",
    "PRODUCT_TASK_AWAITING",
    "PRODUCT_TASK_CANCELLED",
    "PRODUCT_TASK_COHERENT_WORKFLOW",
    "PRODUCT_TASK_COMPLETED",
    "PRODUCT_TASK_EVENTS",
    "PRODUCT_TASK_EVENT_TYPES",
    "PRODUCT_TASK_FAILED",
    "PRODUCT_TASK_OPENED",
    "PRODUCT_TASK_PROTOCOL_VERSION",
    "PRODUCT_TASK_REJECTED",
    "PRODUCT_TASK_SCHEMA_VERSION",
    "PRODUCT_TASK_STARTED",
    "PRODUCT_TASK_STREAM_PREFIX",
    "PRODUCT_TASK_TERMINAL_EVENT_TYPES",
    "PRODUCT_TASK_TERMINAL_STATUSES",
    "PRODUCT_TASK_TRANSITIONS",
    "ProductAssemblyReceipt",
    "ProductEventContract",
    "ProductPreflightBinding",
    "ProductRole",
    "ProductRoleProfile",
    "ProductTaskFacts",
    "ProductTaskProfile",
    "ProductTaskProposal",
    "ProductTaskReader",
    "ProductTaskStatus",
    "ProductTaskSummary",
    "ProductTaskView",
    "ProductTaskViewStatus",
    "ProposalConfirmation",
    "RequestedTaskMode",
    "ResolvedTaskMode",
    "TaskModeSource",
    "product_event_contract",
    "product_required_values",
    "product_started_mode",
    "product_started_values",
    "product_transition_allowed",
    "product_view_status",
    "proposal_confirmable",
]
