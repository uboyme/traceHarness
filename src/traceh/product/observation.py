"""Pure ProductTask observation for UI adapters.

An observation joins existing durable facts but advances none of them.  In
particular it never calls :meth:`ProductTaskControlPlane.inspect`, whose job is
to reconcile a lagging ProductTask stream before a human control action.  A UI
must be able to show that lag honestly: Workflow may already be waiting for
approval while ProductTask still says ``started``.

The companion subscription uses the in-process Feed only as a dirty hint.  It
subscribes exact known streams before the first read, discovers exact related
Session streams from the fresh projection, subscribes those, and reads again.
No Feed payload is projected into state.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from traceh.agents.directory import AgentDirectory
from traceh.agents.identity import AGENT_DIRECTORY_STREAM
from traceh.api.budgets import (
    BudgetAccount,
    BudgetUsageReservationStatus,
)
from traceh.api.llm import UsageQuality
from traceh.api.product import (
    PRODUCT_TASK_COHERENT_WORKFLOW,
    ProductTaskStatus,
    ProductTaskSummary,
)
from traceh.api.promotion import PatchApproval, PatchPromotion, PatchReviewReport
from traceh.api.workflow import WorkflowRun, WorkflowStatus
from traceh.artifacts.catalog import PatchArtifactCatalogReader
from traceh.artifacts.events import ARTIFACT_CATALOG_STREAM
from traceh.budgets.projection import (
    BUDGET_LEDGER_STREAM,
    BudgetLedger,
    BudgetLedgerReader,
)
from traceh.concurrency import await_worker_convergence, combine_failures
from traceh.product.errors import ProductInputError, ProductStateError
from traceh.product.events import product_task_stream, require_product_identifier
from traceh.product.execution import product_task_owner_id
from traceh.product.inspection import (
    ProductInspectionEvidenceReader,
    ProductTaskEvidence,
)
from traceh.product.projection import ProductTaskStreamReader
from traceh.product.topology import (
    PRODUCT_VERIFICATION_NODE,
    product_workflow_definition,
)
from traceh.promotion.events import PROMOTION_LEDGER_STREAM
from traceh.promotion.models import expected_approval_digest
from traceh.promotion.projection import PromotionLedgerReader
from traceh.session.event_feed import EventFeed, EventSubscription
from traceh.session.event_store import EventStore
from traceh.supervision.lifecycle import AgentOwnershipGraph
from traceh.workflow.events import workflow_stream_id
from traceh.workflow.projection import WorkflowStreamReader

SESSION_STREAM_PREFIX = "session:"


@dataclass(frozen=True, slots=True)
class ObservedStreamHead:
    stream_id: str
    seq: int
    event_type: str | None
    occurred_at: datetime | None
    task_bound: bool


@dataclass(frozen=True, slots=True)
class ProductUsage:
    """Durable Budget usage for one ProductTask ownership subtree."""

    tokens: int | None
    token_quality: UsageQuality | None
    steps: int | None
    wall_milliseconds: int | None


@dataclass(frozen=True, slots=True)
class ProductObservation:
    """One fresh UI read with Product and Workflow states kept separate."""

    task_id: str
    summary: ProductTaskSummary | None
    workflow: WorkflowRun | None
    evidence: ProductTaskEvidence | None
    review: PatchReviewReport | None
    approval: PatchApproval | None
    promotion: PatchPromotion | None
    approval_digest: str | None
    stream_heads: tuple[ObservedStreamHead, ...]
    observed_at: datetime
    usage: ProductUsage | None = None

    @property
    def product_status(self) -> ProductTaskStatus | None:
        return None if self.summary is None else self.summary.status

    @property
    def workflow_status(self) -> WorkflowStatus | None:
        return None if self.workflow is None else self.workflow.status

    @property
    def streams_diverged(self) -> bool:
        summary = self.summary
        if summary is None or summary.settled:
            return False
        coherent = PRODUCT_TASK_COHERENT_WORKFLOW.get(summary.status)
        return coherent is None or self.workflow_status not in coherent

    @property
    def related_streams(self) -> tuple[str, ...]:
        return tuple(item.stream_id for item in self.stream_heads)


class ProductObservationReader:
    """Fresh-read Product/Workflow/Directory/Artifact/Promotion projection."""

    __slots__ = (
        "_artifacts",
        "_budgets",
        "_evidence",
        "_promotion_target_id",
        "_promotions",
        "_store",
        "_tasks",
        "_workflow",
    )

    def __init__(
        self,
        store: EventStore,
        evidence: ProductInspectionEvidenceReader,
        *,
        promotion_target_id: str,
    ) -> None:
        if evidence.store is not store:
            raise ProductInputError("product-store-mismatch", "observation")
        self._store = store
        self._evidence = evidence
        self._promotion_target_id = require_product_identifier(
            promotion_target_id, field="promotion_target_id"
        )
        self._tasks = ProductTaskStreamReader(store)
        self._workflow = WorkflowStreamReader(store)
        self._budgets = BudgetLedgerReader(store)
        self._artifacts = PatchArtifactCatalogReader(store)
        self._promotions = PromotionLedgerReader(store)

    @property
    def store(self) -> EventStore:
        return self._store

    async def current_task_id(self, session_id: str) -> str | None:
        """Find the one unsettled ProductTask owned by this Chat Session.

        This is a fresh scan of the authoritative ProductTask streams, used
        only when an adapter starts or resumes.  Terminal history is not a
        "current task" and multiple live matches are refused rather than
        resolved by widget order, timestamps or an arbitrary stream name.
        """

        return await self._tasks.current_for_session(session_id)

    async def load(self, task_id: str) -> ProductObservation:
        task_id = require_product_identifier(task_id, field="task_id")
        summary = await self._tasks.load(task_id)

        # These global readers are intentionally fresh even when this task has
        # not reached the corresponding phase.  A corrupt related fact source
        # is unavailable, not something the UI may hide until a button is used.
        budget_ledger, directory = await self._budgets.load_context()
        await self._artifacts.load()
        ledger = await self._promotions.load()

        workflow = None
        evidence = None
        if summary is not None and summary.definition_hash is not None:
            if summary.resolved_mode is None:
                raise ProductStateError(
                    "product-observation-workflow-unbound", task_id
                )
            definition = product_workflow_definition(
                summary.resolved_mode,
                promotion_target_id=self._promotion_target_id,
            )
            workflow = (await self._workflow.load(task_id)).run(definition)

        review_id = None if summary is None else summary.review_id
        if workflow is not None:
            verification = workflow.outcome(PRODUCT_VERIFICATION_NODE)
            workflow_review_id = (
                None if verification is None else verification.review_id
            )
            if review_id is None:
                review_id = workflow_review_id
            elif workflow_review_id not in {None, review_id}:
                raise ProductStateError(
                    "product-observation-review-chain-broken", task_id
                )
        review = None if review_id is None else ledger.review(review_id)
        if review_id is not None and review is None:
            raise ProductStateError("product-review-missing", task_id)
        if summary is not None and workflow is not None:
            evidence = await self._evidence.load(summary, review)

        approval = (
            None if review is None else ledger.approval_for_review(review.review_id)
        )
        promotion = None
        if summary is not None and summary.promotion_id is not None:
            promotion = ledger.promotion(summary.promotion_id)
            if promotion is None:
                raise ProductStateError("product-promotion-missing", task_id)

        streams = {
            product_task_stream(task_id),
            workflow_stream_id(task_id),
            AGENT_DIRECTORY_STREAM,
            ARTIFACT_CATALOG_STREAM,
            BUDGET_LEDGER_STREAM,
            PROMOTION_LEDGER_STREAM,
        }
        if summary is not None:
            streams.add(f"{SESSION_STREAM_PREFIX}{summary.origin_session_id}")
            streams.add(f"{SESSION_STREAM_PREFIX}{summary.confirmation_session_id}")
            if summary.routing_session_id is not None:
                streams.add(f"{SESSION_STREAM_PREFIX}{summary.routing_session_id}")
        if evidence is not None:
            streams.update(
                f"{SESSION_STREAM_PREFIX}{node.session_id}"
                for node in evidence.nodes
                if node.session_id is not None
            )
        projected_heads = {
            product_task_stream(task_id): 0 if summary is None else summary.head_seq,
            workflow_stream_id(task_id): 0 if workflow is None else workflow.head_seq,
            BUDGET_LEDGER_STREAM: budget_ledger.head_seq,
        }
        heads_list: list[ObservedStreamHead] = []
        global_streams = {
            AGENT_DIRECTORY_STREAM,
            ARTIFACT_CATALOG_STREAM,
            BUDGET_LEDGER_STREAM,
            PROMOTION_LEDGER_STREAM,
        }
        for stream_id in sorted(streams):
            projected_seq = projected_heads.get(stream_id)
            task_bound = stream_id not in global_streams
            heads_list.append(
                await self._stream_head(stream_id, task_bound=task_bound)
                if projected_seq is None
                else await self._stream_at(
                    stream_id,
                    projected_seq,
                    task_bound=task_bound,
                )
            )
        heads = tuple(heads_list)
        return ProductObservation(
            task_id=task_id,
            summary=summary,
            workflow=workflow,
            evidence=evidence,
            review=review,
            approval=approval,
            promotion=promotion,
            approval_digest=(
                None if review is None else expected_approval_digest(review)
            ),
            stream_heads=heads,
            observed_at=datetime.now(UTC),
            usage=_product_usage(budget_ledger, directory, task_id),
        )

    async def _stream_head(
        self,
        stream_id: str,
        *,
        task_bound: bool,
    ) -> ObservedStreamHead:
        """Read the latest durable fact needed by a UI without caching it.

        ``head()`` keeps the normal empty-stream path cheap.  A non-empty
        stream is then read from that exact sequence.  A concurrent append may
        make the returned event newer than the first head; using the last event
        is both safe and more current.  Missing evidence for a reported head is
        corruption, never a reason to invent an event type or timestamp.
        """

        head = await self._store.head(stream_id)
        if head == 0:
            return ObservedStreamHead(stream_id, 0, None, None, task_bound)
        events = await self._store.read(stream_id, from_seq=head)
        if not events or events[0].seq != head:
            raise ProductStateError(
                "product-observation-stream-head-missing", stream_id
            )
        latest = events[-1]
        return ObservedStreamHead(
            stream_id,
            latest.seq,
            latest.type,
            latest.occurred_at,
            task_bound,
        )

    async def _stream_at(
        self,
        stream_id: str,
        seq: int,
        *,
        task_bound: bool,
    ) -> ObservedStreamHead:
        """Bind a status projection to the event version that produced it."""

        if seq == 0:
            return ObservedStreamHead(stream_id, 0, None, None, task_bound)
        events = await self._store.read(stream_id, from_seq=seq)
        if not events or events[0].seq != seq:
            raise ProductStateError(
                "product-observation-stream-head-missing", stream_id
            )
        projected = events[0]
        return ObservedStreamHead(
            stream_id,
            projected.seq,
            projected.type,
            projected.occurred_at,
            task_bound,
        )


class ProductObservationSession:
    """Exact-stream subscribe-before-read handshake for one ProductTask."""

    __slots__ = (
        "_closed",
        "_dirty",
        "_feed",
        "_reader",
        "_subscriptions",
        "_task_id",
        "_watchers",
    )

    def __init__(
        self,
        reader: ProductObservationReader,
        feed: EventFeed,
        task_id: str,
    ) -> None:
        self._reader = reader
        self._feed = feed
        self._task_id = require_product_identifier(task_id, field="task_id")
        self._subscriptions: dict[str, EventSubscription] = {}
        self._watchers: dict[str, asyncio.Task[None]] = {}
        self._dirty = asyncio.Event()
        self._closed = False

    @property
    def subscribed_streams(self) -> tuple[str, ...]:
        return tuple(sorted(self._subscriptions))

    @property
    def dirty(self) -> bool:
        return self._dirty.is_set()

    async def start(self) -> ProductObservation:
        if self._closed:
            raise ProductStateError("product-observation-closed", self._task_id)
        try:
            for stream_id in _initial_streams(self._task_id):
                self._subscribe(stream_id)
            return await self.refresh()
        except BaseException as primary:
            cleanup: BaseException | None = None
            try:
                await self.aclose()
            except BaseException as error:
                cleanup = error
            combined = combine_failures(
                primary,
                cleanup,
                "Product observation start and rollback both failed",
            )
            assert combined is not None
            raise combined from None

    async def refresh(self) -> ProductObservation:
        if self._closed:
            raise ProductStateError("product-observation-closed", self._task_id)
        while True:
            self._dirty.clear()
            observation = await self._reader.load(self._task_id)
            discovered = set(observation.related_streams).difference(
                self._subscriptions
            )
            if not discovered:
                return observation
            for stream_id in sorted(discovered):
                self._subscribe(stream_id)
            # A stream discovered by the read may have changed before its
            # subscription was installed.  Re-reading after subscribe closes
            # that gap; this loop is bounded by the finite identities the
            # durable projection can reveal.

    async def wait_dirty(self) -> None:
        if self._closed:
            return
        await self._dirty.wait()

    def _subscribe(self, stream_id: str) -> None:
        if stream_id in self._subscriptions:
            return
        subscription = self._feed.subscribe(stream_id)
        self._subscriptions[stream_id] = subscription
        self._watchers[stream_id] = asyncio.create_task(
            self._watch(subscription),
            name=f"traceh-product-observer-{self._task_id}",
        )

    async def _watch(self, subscription: EventSubscription) -> None:
        async for _event in subscription:
            # Payload deliberately ignored.  It is only evidence that a fresh
            # durable read may now produce a different answer.
            self._dirty.set()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        subscriptions = tuple(self._subscriptions.values())
        watchers = tuple(self._watchers.values())
        self._subscriptions.clear()
        self._watchers.clear()
        for subscription in subscriptions:
            subscription.close()
        failures: list[BaseException] = []
        for watcher in watchers:
            try:
                await await_worker_convergence(watcher)
            except BaseException as error:
                failures.append(error)
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup("Product observation close failed", failures)

    async def __aenter__(self) -> ProductObservationSession:
        await self.start()
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()


def _initial_streams(task_id: str) -> tuple[str, ...]:
    return (
        product_task_stream(task_id),
        workflow_stream_id(task_id),
        AGENT_DIRECTORY_STREAM,
        ARTIFACT_CATALOG_STREAM,
        BUDGET_LEDGER_STREAM,
        PROMOTION_LEDGER_STREAM,
    )


def _product_usage(
    ledger: BudgetLedger,
    directory: AgentDirectory,
    task_id: str,
) -> ProductUsage:
    """Project usage only from the task's durable ownership/Budget subtree."""

    members = AgentOwnershipGraph(directory).subtree_postorder(
        product_task_owner_id(task_id)
    )
    accounts = tuple(ledger.account(agent_id) for agent_id in members)
    if not members or any(account is None for account in accounts):
        return ProductUsage(None, None, None, None)
    complete_accounts = tuple(
        account for account in accounts if account is not None
    )
    member_ids = frozenset(members)

    tokens = _charged_dimension(
        complete_accounts,
        limit_field="max_tokens",
        amount_field="tokens",
    )
    token_quality: UsageQuality | None = None
    if tokens is not None:
        token_qualities: list[UsageQuality] = []
        unavailable = False
        for charge in ledger.charges:
            if charge.agent_id not in member_ids or not charge.amounts.tokens:
                continue
            quality = charge.usage_quality
            if quality in {None, UsageQuality.UNKNOWN}:
                unavailable = True
                break
            token_qualities.append(quality)
        if not unavailable:
            for reservation in ledger.usage_reservations:
                if (
                    reservation.agent_id not in member_ids
                    or not reservation.amounts.tokens
                ):
                    continue
                if reservation.status in {
                    BudgetUsageReservationStatus.PENDING,
                    BudgetUsageReservationStatus.STARTED,
                }:
                    unavailable = True
                    break
                if reservation.status is BudgetUsageReservationStatus.RELEASED:
                    continue
                quality = reservation.usage_quality
                if (
                    reservation.settled_amounts is None
                    or quality in {None, UsageQuality.UNKNOWN}
                ):
                    unavailable = True
                    break
                token_qualities.append(quality)
        if unavailable:
            tokens = None
        else:
            token_quality = (
                UsageQuality.ESTIMATED
                if UsageQuality.ESTIMATED in token_qualities
                else UsageQuality.EXACT
            )

    steps = _charged_dimension(
        complete_accounts,
        limit_field="max_steps",
        amount_field="steps",
    )
    wall_milliseconds = _charged_dimension(
        complete_accounts,
        limit_field="max_wall_milliseconds",
        amount_field="wall_milliseconds",
    )
    if wall_milliseconds is not None and any(
        reservation.agent_id in member_ids
        and reservation.amounts.wall_milliseconds
        and reservation.status
        in {
            BudgetUsageReservationStatus.PENDING,
            BudgetUsageReservationStatus.STARTED,
        }
        for reservation in ledger.usage_reservations
    ):
        wall_milliseconds = None
    return ProductUsage(tokens, token_quality, steps, wall_milliseconds)


def _charged_dimension(
    accounts: tuple[BudgetAccount, ...],
    *,
    limit_field: str,
    amount_field: str,
) -> int | None:
    if not accounts or any(
        getattr(account.limits, limit_field) is None for account in accounts
    ):
        return None
    return sum(getattr(account.charged, amount_field) for account in accounts)


__all__ = [
    "ObservedStreamHead",
    "ProductObservation",
    "ProductObservationReader",
    "ProductObservationSession",
    "ProductUsage",
]
