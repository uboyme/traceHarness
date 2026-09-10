"""Every benchmark number, derived from durable facts and nothing else.

The rule this module exists to enforce: a metric is either read out of a fact
source that already owns it, or it is reported as **unavailable**.  It is never
estimated, never taken from a model's prose and never defaulted to zero, because
a zero is indistinguishable from "nothing happened" and that is precisely the
mistake a benchmark must not make.

Where each number comes from:

===========================  ==================================================
success                      ProductTask terminal + Workflow terminal + Review
                             ``passed`` + a Promotion receipt whose new revision
                             is what the bare target ref actually points at
routing tokens               the Session of the Agent named by
                             ``product/task-routed``
execution tokens             the Sessions of the Agents the Workflow's own node
                             outcomes name
steps / tool calls           ``step/start`` and ``tool/call`` in those Sessions
cumulative work duration     durable ``turn/start`` -> ``turn/end`` intervals
budget outcome               the Budget Ledger, scoped to this task's ownership
                             subtree
===========================  ==================================================

Wall-clock phase boundaries are **not** here.  Those belong to the runner's own
monotonic clock, because no durable fact records when a host decided to approve.

Two token totals are reported and they are not the same measurement.  The
Session total is what the provider said it used.  The ledger total is what the
Budget authority actually consumed, which is deliberately conservative: a
non-``exact`` usage report settles the whole reservation.  Presenting one as the
other would either understate the authority spent or overstate the tokens the
model reported.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from traceh.agents.directory import AgentDirectoryReader
from traceh.api.budgets import (
    BudgetAccountStatus,
    BudgetReservationStatus,
    BudgetUsageReservationStatus,
)
from traceh.api.llm import UsageQuality
from traceh.api.product import (
    ProductRole,
    ProductTaskStatus,
    ProductTaskSummary,
    RequestedTaskMode,
    ResolvedTaskMode,
    TaskModeSource,
)
from traceh.api.promotion import PatchPromotion, PatchReviewReport, VerificationPlan
from traceh.api.workflow import WorkflowStatus
from traceh.api.workspaces import WorkspaceStatus
from traceh.budgets.projection import BudgetLedgerReader
from traceh.evaluation.errors import BenchmarkEvidenceError
from traceh.product.execution import product_task_owner_id
from traceh.product.projection import ProductTaskStreamReader
from traceh.product.topology import (
    PRODUCT_APPROVAL_NODE,
    PRODUCT_MODE_ROLES,
    PRODUCT_VERIFICATION_NODE,
    product_role_node_id,
    product_workflow_definition,
)
from traceh.promotion.models import (
    expected_approval_digest,
    review_matches_verification_plan,
)
from traceh.promotion.projection import PromotionLedgerReader
from traceh.session.event_store import EventStore
from traceh.session.invariants import CoreInvariantChecker
from traceh.supervision.lifecycle import AgentOwnershipGraph
from traceh.workflow.models import agent_identity, workflow_definition_hash
from traceh.workflow.projection import WorkflowStreamReader
from traceh.workspaces.catalog import WorkspaceCatalogReader

SESSION_STREAM_PREFIX = "session:"

_QUALITY_ORDER = (UsageQuality.EXACT, UsageQuality.ESTIMATED, UsageQuality.UNKNOWN)

_TRUSTED_QUALITIES = frozenset(
    {UsageQuality.EXACT.value, UsageQuality.ESTIMATED.value}
)
"""Usage qualities a token total may be built from.

``estimated`` is reported as a value carrying its label, because it is a real
number the provider stood behind. ``unknown`` is not a weaker number; it is the
absence of one, and it makes the whole Session's total unavailable.
"""


@dataclass(frozen=True, slots=True)
class TokenTotals:
    """Provider-reported usage for one or more Sessions."""

    input_tokens: int
    output_tokens: int
    quality: str

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class SessionWork:
    """What one durable Session shows about the work that happened in it.

    ``tokens`` and ``work_duration_ms`` are ``None`` when the stream does not
    support the answer - a succeeded model attempt without a usage object, a
    Turn without a durable end, a non-monotonic pair of timestamps.  ``None``
    propagates into the report as *unavailable*; it never becomes zero.
    """

    session_id: str
    agent_id: str
    turns: int
    steps: int
    tool_calls: int
    model_attempts: int
    tokens: TokenTotals | None
    work_duration_ms: int | None
    retry_wait_milliseconds: int | None
    provider_active_milliseconds: int | None
    provider_failure_categories: tuple[str, ...]
    final_model_result: str | None


@dataclass(frozen=True, slots=True)
class SessionGroup:
    """Several Sessions summed, keeping the unavailable answers unavailable."""

    sessions: tuple[SessionWork, ...]

    @property
    def turns(self) -> int:
        return sum(item.turns for item in self.sessions)

    @property
    def steps(self) -> int:
        return sum(item.steps for item in self.sessions)

    @property
    def tool_calls(self) -> int:
        return sum(item.tool_calls for item in self.sessions)

    @property
    def model_attempts(self) -> int:
        return sum(item.model_attempts for item in self.sessions)

    @property
    def tokens(self) -> TokenTotals | None:
        parts = [item.tokens for item in self.sessions]
        if any(part is None for part in parts):
            return None
        return TokenTotals(
            input_tokens=sum(part.input_tokens for part in parts if part is not None),
            output_tokens=sum(part.output_tokens for part in parts if part is not None),
            quality=_worst_quality(
                tuple(part.quality for part in parts if part is not None)
            ),
        )

    @property
    def work_duration_ms(self) -> int | None:
        parts = [item.work_duration_ms for item in self.sessions]
        if any(part is None for part in parts):
            return None
        return sum(part for part in parts if part is not None)

    @property
    def retry_wait_milliseconds(self) -> int | None:
        parts = [item.retry_wait_milliseconds for item in self.sessions]
        if any(part is None for part in parts):
            return None
        return sum(part for part in parts if part is not None)

    @property
    def provider_active_milliseconds(self) -> int | None:
        parts = [item.provider_active_milliseconds for item in self.sessions]
        if any(part is None for part in parts):
            return None
        return sum(part for part in parts if part is not None)

    @property
    def provider_failure_categories(self) -> tuple[str, ...]:
        return tuple(
            category
            for item in self.sessions
            for category in item.provider_failure_categories
        )


@dataclass(frozen=True, slots=True)
class BudgetOutcome:
    """The Budget Ledger's own answer for this task's ownership subtree."""

    accounts: int
    accounts_closed: int
    child_reservations: int
    child_reservations_terminal: int
    usage_reservations: int
    usage_reservations_terminal: int
    settled_tokens: int
    charged_steps: int
    charged_tool_calls: int

    @property
    def converged(self) -> bool:
        return (
            self.accounts > 0
            and self.accounts == self.accounts_closed
            and self.child_reservations == self.child_reservations_terminal
            and self.usage_reservations == self.usage_reservations_terminal
        )


@dataclass(frozen=True, slots=True)
class WorkspaceOutcome:
    """Managed worktree lifecycle for this task's ownership subtree.

    ``quarantined`` is a **terminal** state, not an unconverged one. The Product
    resource contract deliberately quarantines a dirty worktree on failure or
    cancellation so the captured bytes survive for inspection; calling that
    "not converged" would report the failure/cancellation lifecycle as broken
    exactly when it behaved as designed. What convergence excludes is a record
    still ``provisional`` or ``attached`` - one nobody released or quarantined.
    """

    workspaces: int
    released: int
    quarantined: int

    @property
    def live(self) -> int:
        return self.workspaces - self.released - self.quarantined

    @property
    def converged(self) -> bool:
        return self.workspaces > 0 and self.live == 0


@dataclass(frozen=True, slots=True)
class AttemptEvidence:
    """Everything one attempt can prove about itself, and what it cannot."""

    task_id: str
    product_status: ProductTaskStatus
    requested_mode: RequestedTaskMode
    mode_source: TaskModeSource
    resolved_mode: ResolvedTaskMode | None
    requirement_digest: str
    profile_digest: str
    preflight_digest: str
    source_base_revision: str | None
    definition_hash: str | None
    workflow_status: WorkflowStatus | None
    routing: SessionWork | None
    routing_parsed: bool
    execution: SessionGroup
    unattributed: SessionGroup
    budget: BudgetOutcome
    workspaces: WorkspaceOutcome
    review_id: str | None
    review_passed: bool | None
    verifier_definition_digest: str | None
    promotion_id: str | None
    previous_revision: str | None
    new_revision: str | None
    target_ref: str
    target_revision: str | None
    failure_code: str | None
    reason_code: str | None
    unavailable: tuple[str, ...]

    @property
    def success(self) -> bool:
        """Four independent durable facts must agree; three are not enough."""

        return (
            self.product_status is ProductTaskStatus.COMPLETED
            and self.workflow_status is WorkflowStatus.COMPLETED
            and self.review_passed is True
            and self.promotion_id is not None
            and self.new_revision is not None
            and self.new_revision != self.previous_revision
            and self.target_revision == self.new_revision
        )

    @property
    def converged(self) -> bool:
        return self.budget.converged and self.workspaces.converged


async def collect_attempt_evidence(
    store: EventStore,
    *,
    task_id: str,
    promotion_target_id: str,
    target_ref: str,
    target_revision: str | None,
    verification_plan: VerificationPlan,
) -> AttemptEvidence:
    """Rebuild one attempt's measurement from the stores that own each fact."""

    summary = await ProductTaskStreamReader(store).load(task_id)
    if summary is None:
        raise BenchmarkEvidenceError("benchmark-product-task-missing", task_id)

    facts = await _workflow_facts(
        store, summary, promotion_target_id=promotion_target_id
    )
    workflow_status, role_agents = facts.status, facts.role_agents
    directory = await AgentDirectoryReader(store).load()
    owner_id = product_task_owner_id(task_id)
    # An empty subtree is a legitimate answer: a task that failed before its
    # owner was created never had one. What must not happen is a Workflow or
    # routing fact naming an Agent this task does not own, which is what the
    # containment check below decides.
    subtree = AgentOwnershipGraph(directory).subtree_postorder(owner_id)

    expected = {owner_id, *role_agents.values()}
    if summary.router_agent_id is not None:
        expected.add(summary.router_agent_id)
    if not expected - {owner_id} <= set(subtree):
        # A Product or Workflow fact naming an Agent this task does not own means
        # the two sources disagree about what ran, and measuring either alone
        # would attribute somebody else's tokens to this attempt.
        raise BenchmarkEvidenceError("benchmark-agent-set-inconsistent", task_id)
    # The reverse is *not* an inconsistency. A Router Agent is created before its
    # answer is parsed, so a rejected answer leaves a real, owned Agent that no
    # durable Product fact ever named. Its cost is reported separately rather
    # than either discarded or relabelled as routing this task never recorded.
    unattributed_ids = sorted(set(subtree) - expected)

    unavailable: list[str] = []
    routing: SessionWork | None = None
    if summary.router_agent_id is not None:
        if summary.routing_session_id is None:
            raise BenchmarkEvidenceError("benchmark-routing-session-missing", task_id)
        # ``product/task-routed`` names both the Router Agent and its Session,
        # but only the Directory decides which Session that Agent actually owns.
        # Reading the pair straight out of the Product payload lets a routing
        # identity point at some *other* Agent's Session - a role Session of the
        # same task parses cleanly and passes the invariant check - and its
        # tokens would then be counted once as routing and once as execution,
        # collapsing the separation the two metrics exist to keep.
        router_record = directory.get(summary.router_agent_id)
        if router_record is None:
            raise BenchmarkEvidenceError("benchmark-agent-record-missing", task_id)
        if router_record.session_id != summary.routing_session_id:
            raise BenchmarkEvidenceError("benchmark-routing-session-mismatch", task_id)
        routing = await _session_work(
            store,
            agent_id=summary.router_agent_id,
            session_id=router_record.session_id,
        )
        _note_unavailable(unavailable, "routing", routing)

    sessions: list[SessionWork] = []
    for role, agent_id in sorted(role_agents.items(), key=lambda item: item[0].value):
        record = directory.get(agent_id)
        if record is None:
            raise BenchmarkEvidenceError("benchmark-agent-record-missing", task_id)
        work = await _session_work(
            store, agent_id=agent_id, session_id=record.session_id
        )
        _note_unavailable(unavailable, f"execution.{role.value}", work)
        sessions.append(work)
    execution = SessionGroup(tuple(sessions))

    unattributed: list[SessionWork] = []
    for agent_id in unattributed_ids:
        record = directory.get(agent_id)
        if record is None:
            raise BenchmarkEvidenceError("benchmark-agent-record-missing", task_id)
        work = await _session_work(
            store, agent_id=agent_id, session_id=record.session_id
        )
        _note_unavailable(unavailable, "unattributed", work)
        unattributed.append(work)

    budget = await _budget_outcome(store, subtree)
    workspaces = await _workspace_outcome(store, subtree)

    ledger = await PromotionLedgerReader(store).load()
    review = None if summary.review_id is None else ledger.review(summary.review_id)
    if summary.review_id is not None and review is None:
        raise BenchmarkEvidenceError("benchmark-review-missing", task_id)
    if review is not None and not review_matches_verification_plan(
        review, verification_plan
    ):
        # The Promotion projector proves that the Review is internally
        # coherent; the benchmark owns the host-frozen VerificationPlan and
        # must additionally prove that every durable result belongs to that
        # exact plan before it can use ``passed`` as a quality fact.
        raise BenchmarkEvidenceError("benchmark-verifier-evidence-mismatch", task_id)
    promotion = (
        None if summary.promotion_id is None else ledger.promotion(summary.promotion_id)
    )
    if summary.promotion_id is not None and promotion is None:
        raise BenchmarkEvidenceError("benchmark-promotion-missing", task_id)
    if promotion is not None and promotion.target_ref != target_ref:
        raise BenchmarkEvidenceError("benchmark-promotion-target-mismatch", task_id)
    _require_one_evidence_chain(
        task_id=task_id,
        summary=summary,
        facts=facts,
        review=review,
        promotion=promotion,
    )

    return AttemptEvidence(
        task_id=task_id,
        product_status=summary.status,
        requested_mode=summary.requested_mode,
        mode_source=summary.mode_source,
        resolved_mode=summary.resolved_mode,
        requirement_digest=summary.requirement_digest,
        profile_digest=summary.profile_digest,
        preflight_digest=summary.preflight_digest,
        source_base_revision=summary.source_base_revision,
        definition_hash=summary.definition_hash,
        workflow_status=workflow_status,
        routing=routing,
        routing_parsed=summary.router_agent_id is not None
        and summary.resolved_mode is not None,
        execution=execution,
        unattributed=SessionGroup(tuple(unattributed)),
        budget=budget,
        workspaces=workspaces,
        review_id=summary.review_id,
        review_passed=None if review is None else review.passed,
        verifier_definition_digest=(
            None if review is None else review.verifier_definition_digest
        ),
        promotion_id=summary.promotion_id,
        previous_revision=None if promotion is None else promotion.previous_revision,
        new_revision=None if promotion is None else promotion.new_revision,
        target_ref=target_ref,
        target_revision=target_revision,
        failure_code=summary.failure_code,
        reason_code=summary.reason_code,
        unavailable=tuple(unavailable),
    )


@dataclass(frozen=True, slots=True)
class _WorkflowFacts:
    """What the Workflow stream itself says about this run."""

    status: WorkflowStatus | None
    role_agents: dict[ProductRole, str]
    review_id: str | None
    approval_digest: str | None


async def _workflow_facts(
    store: EventStore,
    summary: ProductTaskSummary,
    *,
    promotion_target_id: str,
) -> _WorkflowFacts:
    """Read the run through the definition the task itself recorded."""

    if summary.resolved_mode is None:
        return _WorkflowFacts(None, {}, None, None)
    definition = product_workflow_definition(
        summary.resolved_mode, promotion_target_id=promotion_target_id
    )
    if (
        summary.definition_hash is not None
        and workflow_definition_hash(definition) != summary.definition_hash
    ):
        # The task recorded a plan this build does not reproduce. Interpreting
        # its run with a different definition would report node kinds and
        # results the run never agreed to.
        raise BenchmarkEvidenceError(
            "benchmark-definition-hash-mismatch", summary.task_id
        )
    projection = await WorkflowStreamReader(store).load(summary.workflow_run_id)
    if projection.status is None:
        return _WorkflowFacts(None, {}, None, None)
    run = projection.run(definition)
    run_id = summary.workflow_run_id
    agents: dict[ProductRole, str] = {}
    for role in PRODUCT_MODE_ROLES[summary.resolved_mode]:
        node_id = product_role_node_id(role)
        outcome = run.outcome(node_id)
        if outcome is None:
            # No start fact: this node never ran, so there is no Agent and no
            # Session to attribute. That is an absence, not a zero.
            continue
        # A *failed* node records no ``agent_id`` - the terminal payload carries
        # only the failure code - so reading the outcome alone silently drops
        # every token a role spent before it failed. The identity is derivable
        # from run and node by the same rule the executor used, so the Agent is
        # still named exactly; when the outcome does carry an id the two must
        # agree, which keeps this a cross-check rather than a substitution.
        derived, _, _, _ = agent_identity(run_id, node_id)
        if outcome.agent_id is not None and outcome.agent_id != derived:
            raise BenchmarkEvidenceError(
                "benchmark-node-agent-identity-mismatch", summary.task_id
            )
        agents[role] = derived
    verification = run.outcome(PRODUCT_VERIFICATION_NODE)
    approval = run.outcome(PRODUCT_APPROVAL_NODE)
    return _WorkflowFacts(
        status=run.status,
        role_agents=agents,
        review_id=None if verification is None else verification.review_id,
        approval_digest=None if approval is None else approval.approval_digest,
    )


def _require_one_evidence_chain(
    *,
    task_id: str,
    summary: ProductTaskSummary,
    facts: _WorkflowFacts,
    review: PatchReviewReport | None,
    promotion: PatchPromotion | None,
) -> None:
    """Three domains must be describing the *same* Review, not three plausible ones.

    Each of the Workflow Verification outcome, the ProductTask ``review_id`` and
    the Promotion receipt is well-formed on its own, so reading them
    independently makes a report that says "verified, approved and promoted"
    provable from three unrelated facts. The digest ties the last link: an
    approval is only an approval *of this Review's content*, which is precisely
    the binding ADR-0030 refused to let ``review_digest`` stand in for.
    """

    if (
        facts.review_id is not None
        and summary.review_id is not None
        and facts.review_id != summary.review_id
    ):
        raise BenchmarkEvidenceError("benchmark-review-chain-broken", task_id)
    if promotion is not None:
        if review is None or promotion.review_id != review.review_id:
            raise BenchmarkEvidenceError("benchmark-promotion-chain-broken", task_id)
        if promotion.approval_digest != expected_approval_digest(review):
            raise BenchmarkEvidenceError("benchmark-approval-chain-broken", task_id)
    if facts.approval_digest is not None:
        if review is None or facts.approval_digest != expected_approval_digest(review):
            raise BenchmarkEvidenceError("benchmark-approval-chain-broken", task_id)


async def _session_work(
    store: EventStore, *, agent_id: str, session_id: str
) -> SessionWork:
    events = await store.read(f"{SESSION_STREAM_PREFIX}{session_id}")
    # Counting events without checking the lifecycle they belong to means any
    # stream that merely *looks* like a Session produces numbers. The core
    # checker is the executable definition of a valid Turn/Step/attempt
    # sequence, and reusing it keeps the benchmark from inventing a weaker one:
    # an attempt-end with no matching start, an unpaired Tool call or an
    # out-of-order Turn makes the metric refuse rather than inflate.
    if CoreInvariantChecker().check(events):
        raise BenchmarkEvidenceError("benchmark-session-invariants-violated", session_id)
    turns = 0
    steps = 0
    tool_calls = 0
    input_tokens = 0
    output_tokens = 0
    qualities: list[str] = []
    tokens_available = True
    duration_ms = 0
    duration_available = True
    model_attempts = 0
    retry_wait_milliseconds = 0
    retry_wait_available = True
    provider_active_milliseconds = 0
    provider_active_available = True
    provider_failure_categories: list[str] = []
    final_model_result: str | None = None
    started: dict[str, datetime] = {}
    for event in events:
        data = event.data if type(event.data) is dict else {}
        if event.type == "turn/start":
            turn_id = data.get("turn_id")
            if type(turn_id) is not str or event.occurred_at is None:
                duration_available = False
                continue
            started[turn_id] = event.occurred_at
        elif event.type == "turn/end":
            turns += 1
            turn_id = data.get("turn_id")
            start = started.pop(turn_id, None) if type(turn_id) is str else None
            if start is None or event.occurred_at is None:
                duration_available = False
                continue
            elapsed = (event.occurred_at - start).total_seconds()
            if elapsed < 0:
                # Durable timestamps come from a wall clock that can move. A
                # negative interval is not a small number; it is no answer.
                duration_available = False
                continue
            duration_ms += int(elapsed * 1000)
        elif event.type == "step/start":
            steps += 1
        elif event.type == "tool/call":
            tool_calls += 1
        elif event.type == "model/attempt-start":
            model_attempts += 1
            retry_wait = data.get("retry_wait_milliseconds")
            if type(retry_wait) is not int or retry_wait < 0:
                retry_wait_available = False
            else:
                retry_wait_milliseconds += retry_wait
        elif event.type == "model/attempt-end":
            status = data.get("status")
            final_model_result = status if type(status) is str else None
            active = data.get("provider_active_milliseconds")
            if type(active) is not int or active < 0:
                provider_active_available = False
            else:
                provider_active_milliseconds += active
            failure_category = data.get("failure_category")
            if isinstance(failure_category, str) and failure_category:
                provider_failure_categories.append(failure_category)
            usage = data.get("usage")
            if type(usage) is not dict:
                tokens_available = False
                continue
            raw_input = usage.get("input_tokens")
            raw_output = usage.get("output_tokens")
            quality = usage.get("quality")
            if (
                type(raw_input) is not int
                or type(raw_output) is not int
                or raw_input < 0
                or raw_output < 0
                or type(quality) is not str
                or quality not in _TRUSTED_QUALITIES
            ):
                # ``unknown`` is the repository's own word for "this count is not
                # evidence" - it is exactly why Budget settles the whole
                # reservation instead of believing the number. Reporting the
                # value anyway would put a 0 in a token column, which reads as
                # "no tokens were used" rather than "we do not know".
                tokens_available = False
                continue
            input_tokens += raw_input
            output_tokens += raw_output
            qualities.append(quality)
    if started:
        duration_available = False
    return SessionWork(
        session_id=session_id,
        agent_id=agent_id,
        turns=turns,
        steps=steps,
        tool_calls=tool_calls,
        model_attempts=model_attempts,
        tokens=(
            TokenTotals(input_tokens, output_tokens, _worst_quality(tuple(qualities)))
            if tokens_available
            else None
        ),
        work_duration_ms=duration_ms if duration_available else None,
        retry_wait_milliseconds=(
            retry_wait_milliseconds if retry_wait_available else None
        ),
        provider_active_milliseconds=(
            provider_active_milliseconds if provider_active_available else None
        ),
        provider_failure_categories=tuple(provider_failure_categories),
        final_model_result=final_model_result,
    )


async def _budget_outcome(
    store: EventStore, subtree: tuple[str, ...]
) -> BudgetOutcome:
    ledger = await BudgetLedgerReader(store).load()
    members = set(subtree)
    accounts = [item for item in ledger.accounts if item.agent_id in members]
    reservations = [
        item for item in ledger.reservations if item.parent_agent_id in members
    ]
    usage = [item for item in ledger.usage_reservations if item.agent_id in members]
    settled_tokens = 0
    for item in usage:
        if item.status is BudgetUsageReservationStatus.SETTLED:
            settled = item.settled_amounts
            settled_tokens += 0 if settled is None else settled.tokens
    charges = [item for item in ledger.charges if item.agent_id in members]
    return BudgetOutcome(
        accounts=len(accounts),
        accounts_closed=sum(
            1 for item in accounts if item.status is BudgetAccountStatus.CLOSED
        ),
        child_reservations=len(reservations),
        child_reservations_terminal=sum(
            1
            for item in reservations
            if item.status
            in {BudgetReservationStatus.COMMITTED, BudgetReservationStatus.RELEASED}
        ),
        usage_reservations=len(usage),
        usage_reservations_terminal=sum(
            1
            for item in usage
            if item.status
            in {
                BudgetUsageReservationStatus.SETTLED,
                BudgetUsageReservationStatus.RELEASED,
            }
        ),
        settled_tokens=settled_tokens,
        charged_steps=sum(item.amounts.steps for item in charges),
        charged_tool_calls=sum(item.amounts.tool_calls for item in charges),
    )


async def _workspace_outcome(
    store: EventStore, subtree: tuple[str, ...]
) -> WorkspaceOutcome:
    catalog = await WorkspaceCatalogReader(store).load()
    records = [
        record for record in catalog.workspaces if record.agent_id in set(subtree)
    ]
    return WorkspaceOutcome(
        workspaces=len(records),
        released=sum(
            1 for record in records if record.status is WorkspaceStatus.RELEASED
        ),
        quarantined=sum(
            1 for record in records if record.status is WorkspaceStatus.QUARANTINED
        ),
    )


def _note_unavailable(sink: list[str], label: str, work: SessionWork) -> None:
    if work.tokens is None:
        sink.append(f"{label}.tokens")
    if work.work_duration_ms is None:
        sink.append(f"{label}.work_duration_ms")
    if work.retry_wait_milliseconds is None:
        sink.append(f"{label}.retry_wait_milliseconds")
    if work.provider_active_milliseconds is None:
        sink.append(f"{label}.provider_active_milliseconds")


def _worst_quality(values: tuple[str, ...]) -> str:
    """The weakest evidence in a group decides how the group is labelled."""

    worst = UsageQuality.EXACT
    for value in values:
        try:
            quality = UsageQuality(value)
        except ValueError:
            return UsageQuality.UNKNOWN.value
        if _QUALITY_ORDER.index(quality) > _QUALITY_ORDER.index(worst):
            worst = quality
    return worst.value


__all__ = [
    "AttemptEvidence",
    "BudgetOutcome",
    "SessionGroup",
    "SessionWork",
    "TokenTotals",
    "WorkspaceOutcome",
    "collect_attempt_evidence",
]
