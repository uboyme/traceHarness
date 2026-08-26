"""The host-owned ProductTask state machine above existing durable services.

This is intentionally an orchestration layer, not another runtime.  Product
facts stay in :class:`ProductTaskService`, node progress stays in the Workflow
stream, review/approval/promotion stay in the promotion ledger, and Agent,
Workspace, Artifact and Budget facts stay in their existing domains.

The only process-local product state is an unanswered Proposal.  It is removed
when the process exits and is never used to resume a durable task.  Every
operation after confirmation starts from a fresh ProductTask replay by task id.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from traceh.api.json_types import fingerprint
from traceh.api.product import (
    ProductTaskProposal,
    ProductTaskStatus,
    ProductTaskSummary,
    ProductTaskView,
    ProposalConfirmation,
    RequestedTaskMode,
    TaskModeSource,
)
from traceh.api.promotion import PatchReviewReport
from traceh.api.workflow import WorkflowRun, WorkflowStatus
from traceh.concurrency import await_worker_convergence, combine_failures
from traceh.product.assembly import (
    ProductAssembly,
    ProductAssemblyService,
    ProductPreflight,
)
from traceh.product.errors import (
    ProductInputError,
    ProductServiceClosedError,
    ProductStateError,
)
from traceh.product.events import require_product_identifier
from traceh.product.router import MAX_ROUTER_SUMMARY_CHARS
from traceh.product.service import ProductTaskService
from traceh.product.topology import PRODUCT_VERIFICATION_NODE
from traceh.promotion.models import expected_approval_digest
from traceh.promotion.service import PatchPromotionService
from traceh.session.event_store import EventStore
from traceh.supervision.execution import durable_log_identity


class ProductExecutionBoundary(Protocol):
    """The existing Workflow plus the resources it needs for one task.

    A concrete implementation may own a Supervisor, Budget service and managed
    Workspace service, but none of those handles reaches the chat action parser
    or the Proposal.  The Product control plane asks for lifecycle outcomes and
    the boundary keeps the lower-level sagas in their owning domains.
    """

    @property
    def store(self) -> EventStore:
        ...

    def owns_task(self, task_id: str) -> bool:
        ...

    async def prepare(self, task_id: str, preflight: ProductPreflight) -> None:
        ...

    async def start(
        self, task_id: str, assembly: ProductAssembly, *, requirement: str
    ) -> WorkflowRun:
        ...

    async def resume(self, task_id: str, assembly: ProductAssembly) -> WorkflowRun:
        ...

    async def state(self, task_id: str, assembly: ProductAssembly) -> WorkflowRun:
        ...

    async def cancel(self, task_id: str) -> None:
        ...

    async def release(self, task_id: str, *, reason: str) -> None:
        ...

    async def aclose(self) -> None:
        ...


@dataclass(frozen=True, slots=True)
class PendingProductProposal:
    """The one unanswered offer in a chat Session.

    ``task_id`` is the one identity this Proposal may create after a valid
    confirmation.  Showing it does not create a durable task; only
    ``product/task-opened`` does that.

    ``requirement`` is retained only until confirmation.  The durable source of
    that text is the origin Session message; a restart reconstructs execution
    from that stream rather than from this value.
    """

    task_id: str
    proposal: ProductTaskProposal
    profile_id: str
    requirement: str


@dataclass(frozen=True, slots=True)
class ProductInspection:
    """Fresh host rendering inputs; no field is a second stored fact."""

    view: ProductTaskView
    review: PatchReviewReport | None = None
    approval_digest: str | None = None


@dataclass(frozen=True, slots=True)
class ProductAdvanceResult:
    """What a host should render after one Product operation."""

    summary: ProductTaskSummary
    workflow: WorkflowRun | None = None
    review: PatchReviewReport | None = None
    approval_digest: str | None = None


class ProductTaskControlPlane:
    """Confirm, run, pause, inspect and settle ProductTasks.

    This class owns no scheduler.  Its pending Tasks are only single-flight
    receipts for public calls; execution remains owned by ``ProductExecution``
    and ``WorkflowService``.  Cancellation waits for that boundary to converge
    before ``product/task-cancelled`` is allowed to claim a terminal state.
    """

    __slots__ = (
        "_assembly",
        "_closed",
        "_execution",
        "_lock",
        "_pending",
        "_profile_id",
        "_promotions",
        "_proposals",
        "_tasks",
    )

    def __init__(
        self,
        tasks: ProductTaskService,
        assembly: ProductAssemblyService,
        execution: ProductExecutionBoundary,
        promotions: PatchPromotionService,
        *,
        profile_id: str,
    ) -> None:
        if type(tasks) is not ProductTaskService:
            raise ProductInputError("product-task-service-invalid", "tasks")
        if type(assembly) is not ProductAssemblyService:
            raise ProductInputError("product-assembly-service-invalid", "assembly")
        if type(promotions) is not PatchPromotionService:
            raise ProductInputError("product-promotion-service-invalid", "promotions")
        if assembly.tasks is not tasks:
            raise ProductInputError("product-task-service-mismatch", "assembly")
        expected = durable_log_identity(tasks.store)
        if (
            durable_log_identity(execution.store) is not expected
            or durable_log_identity(promotions.store) is not expected
        ):
            raise ProductInputError("product-store-mismatch", "store")
        self._tasks = tasks
        self._assembly = assembly
        self._execution = execution
        self._promotions = promotions
        self._profile_id = require_product_identifier(
            profile_id, field="profile_id"
        )
        self._proposals: dict[str, PendingProductProposal] = {}
        self._pending: dict[str, asyncio.Task[ProductAdvanceResult]] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def store(self) -> EventStore:
        return self._tasks.store

    # --------------------------------------------------------------- proposal

    async def offer(
        self,
        *,
        session_id: str,
        origin_turn_id: str,
        origin_message_id: str,
        proposed_turn_id: str,
        requirement: str,
        requested_mode: RequestedTaskMode | None = None,
    ) -> PendingProductProposal:
        """Replace this Session's unanswered Proposal with one fresh preflight.

        A supplied mode is part of the host-rendered offer and becomes binding
        only when a later human message confirms that exact Proposal.  Omitting
        it uses the Profile default; neither path accepts topology input.
        """

        session_id = require_product_identifier(session_id, field="session_id")
        origin_turn_id = require_product_identifier(
            origin_turn_id, field="origin_turn_id"
        )
        origin_message_id = require_product_identifier(
            origin_message_id, field="origin_message_id"
        )
        proposed_turn_id = require_product_identifier(
            proposed_turn_id, field="proposed_turn_id"
        )
        requirement = _require_requirement(requirement)
        if requested_mode is not None and type(requested_mode) is not RequestedTaskMode:
            raise ProductInputError("product-requested-mode-invalid", "requested_mode")
        preflight = await self._assembly.preflight(self._profile_id)
        mode = (
            preflight.profile.profile.default_mode
            if requested_mode is None
            else requested_mode
        )
        mode_source = (
            TaskModeSource.PROFILE
            if requested_mode is None
            else TaskModeSource.CONFIRMED_PROPOSAL
        )
        proposal_id = "proposal-" + fingerprint(
            {
                "purpose": "product-task-proposal",
                "session_id": session_id,
                "origin_turn_id": origin_turn_id,
                "origin_message_id": origin_message_id,
                "proposed_turn_id": proposed_turn_id,
                "requirement_digest": _requirement_digest(requirement),
                "preflight_digest": preflight.digest,
                "requested_mode": mode.value,
                "mode_source": mode_source.value,
            }
        )
        proposal = ProductTaskProposal(
            proposal_id=proposal_id,
            origin_session_id=session_id,
            origin_turn_id=origin_turn_id,
            origin_message_id=origin_message_id,
            proposed_turn_id=proposed_turn_id,
            requirement_digest=_requirement_digest(requirement),
            requested_mode=mode,
            mode_source=mode_source,
            preflight=preflight.binding,
        )
        pending = PendingProductProposal(
            task_id=_task_id(proposal),
            proposal=proposal,
            profile_id=self._profile_id,
            requirement=requirement,
        )
        async with self._lock:
            self._require_open()
            self._proposals[session_id] = pending
        return pending

    async def pending_proposal(
        self, session_id: str
    ) -> PendingProductProposal | None:
        session_id = require_product_identifier(session_id, field="session_id")
        async with self._lock:
            return self._proposals.get(session_id)

    async def confirm(
        self,
        *,
        session_id: str,
        confirming_turn_id: str,
        confirming_message_id: str,
    ) -> ProductAdvanceResult:
        """Open and advance the one Proposal this Session currently owns."""

        session_id = require_product_identifier(session_id, field="session_id")
        confirming_turn_id = require_product_identifier(
            confirming_turn_id, field="confirming_turn_id"
        )
        confirming_message_id = require_product_identifier(
            confirming_message_id, field="confirming_message_id"
        )
        async with self._lock:
            self._require_open()
            pending = self._proposals.get(session_id)
        if pending is None:
            raise ProductStateError("product-proposal-missing", session_id)
        confirmation = ProposalConfirmation(
            proposal_id=pending.proposal.proposal_id,
            confirming_session_id=session_id,
            confirming_turn_id=confirming_turn_id,
            confirming_message_id=confirming_message_id,
        )
        task_id = pending.task_id

        async def advance() -> ProductAdvanceResult:
            summary = await self._tasks.open_task(
                task_id=task_id,
                operation_id=_operation_id("open", task_id),
                proposal=pending.proposal,
                confirmation=confirmation,
            )
            try:
                preflight = await self._assembly.preflight(pending.profile_id)
                if preflight.digest != summary.preflight_digest:
                    raise ProductStateError("product-preflight-drifted", task_id)
                await self._execution.prepare(task_id, preflight)
                assembly = await self._assembly.assemble(
                    task_id=task_id,
                    profile_id=pending.profile_id,
                    routing_summary=pending.requirement,
                )
                await self._tasks.start_task(
                    task_id=task_id,
                    operation_id=_operation_id("start", task_id),
                    receipt=assembly.receipt,
                )
            except Exception as error:
                return await self._fail_opened_task(task_id, error)
            try:
                run = await self._execution.start(
                    task_id, assembly, requirement=pending.requirement
                )
            except Exception as error:
                # Workflow records a failed terminal before it reports node
                # failures to its caller.  Re-read that durable outcome and
                # settle ProductTask instead of turning a correctly recorded
                # task failure into a Chat traceback.  Cancellation remains a
                # BaseException and is never rewritten as failure here.
                try:
                    durable = await self._execution.state(task_id, assembly)
                except Exception as state_error:
                    combined = combine_failures(
                        error,
                        state_error,
                        "workflow failed and its durable state could not be read",
                    )
                    assert combined is not None
                    return await self._fail_opened_task(task_id, combined)
                if durable.status is not WorkflowStatus.FAILED:
                    return await self._fail_opened_task(task_id, error)
                run = durable
            return await self._after_workflow(task_id, run)

        try:
            result = await self._owned(
                task_id,
                advance,
                cancel_execution_on_caller_cancel=True,
            )
        finally:
            # A durable task, a durable error, or a retry all make this offer no
            # longer the unanswered question.  A failure before open_task is
            # safely repeatable through the same deterministic task identity.
            async with self._lock:
                current = self._proposals.get(session_id)
                if current is pending:
                    self._proposals.pop(session_id, None)
        return result

    async def _fail_opened_task(
        self, task_id: str, error: BaseException
    ) -> ProductAdvanceResult:
        """Release every allocated owner before recording one failed terminal.

        Routing and assembly happen after ``product/task-opened`` because their
        cost and result belong to that durable identity.  Consequently an
        ordinary failure in either phase is a task outcome, not a reason to
        leave an opened task with attached resources.  The terminal is written
        only after release succeeds, preserving the existing retry boundary.
        """

        try:
            await self._execution.release(task_id, reason="failed")
        except BaseException as cleanup_error:
            combined = combine_failures(
                error,
                cleanup_error,
                "Product task failed and its resources could not be released",
            )
            assert combined is not None
            raise combined from None
        try:
            failed = await self._tasks.fail_task(
                task_id=task_id,
                operation_id=_operation_id("fail", task_id),
                failure_code=_failure_code(error),
            )
        except BaseException as write_error:
            combined = combine_failures(
                error,
                write_error,
                "Product task failed after cleanup but its terminal could not be recorded",
            )
            assert combined is not None
            raise combined from None
        return ProductAdvanceResult(summary=failed)

    # --------------------------------------------------------------- durable

    async def inspect(self, task_id: str) -> ProductInspection:
        task_id = require_product_identifier(task_id, field="task_id")
        await self._reconcile_started(task_id)
        view = await self._tasks.view(task_id)
        if view is None:
            raise ProductStateError("product-task-unknown", task_id)
        review = None
        approval_digest = None
        if view.summary.review_id is not None:
            ledger = await self._promotions.ledger()
            review = ledger.review(view.summary.review_id)
            if review is None:
                raise ProductStateError("product-review-missing", task_id)
            approval_digest = expected_approval_digest(review)
        return ProductInspection(
            view=view, review=review, approval_digest=approval_digest
        )

    async def approve(self, task_id: str, *, approver_id: str) -> ProductAdvanceResult:
        task_id = require_product_identifier(task_id, field="task_id")
        approver_id = require_product_identifier(approver_id, field="approver_id")

        async def advance() -> ProductAdvanceResult:
            await self._reconcile_started(task_id)
            summary = await self._require_status(
                task_id, ProductTaskStatus.AWAITING_APPROVAL
            )
            if summary.review_id is None:
                raise ProductStateError("product-review-missing", task_id)
            ledger = await self._promotions.ledger()
            review = ledger.review(summary.review_id)
            if review is None or not review.passed:
                raise ProductStateError("product-review-not-approvable", task_id)
            digest = expected_approval_digest(review)
            existing_promotion = ledger.promotion_for_approval(digest)
            if existing_promotion is not None:
                await self._execution.release(task_id, reason="merged")
                completed = await self._tasks.complete_task(
                    task_id=task_id,
                    operation_id=_operation_id("complete", task_id),
                    promotion_id=existing_promotion.promotion_id,
                )
                return ProductAdvanceResult(summary=completed)
            assembly = await self._assembly.assemble(
                task_id=task_id, profile_id=self._profile_id
            )
            await self._execution.prepare(task_id, assembly.preflight)
            await self._promotions.approve(
                review_id=review.review_id,
                approval_digest=digest,
                approver_id=approver_id,
                operation_id=_operation_id("approve", task_id),
            )
            run = await self._execution.resume(task_id, assembly)
            if run.status is not WorkflowStatus.COMPLETED:
                raise ProductStateError("product-workflow-not-completed", task_id)
            promotion = await self._promotions.promote(approval_digest=digest)
            await self._execution.release(task_id, reason="merged")
            completed = await self._tasks.complete_task(
                task_id=task_id,
                operation_id=_operation_id("complete", task_id),
                promotion_id=promotion.promotion_id,
            )
            return ProductAdvanceResult(summary=completed, workflow=run)

        return await self._owned(task_id, advance)

    async def reject(self, task_id: str) -> ProductAdvanceResult:
        task_id = require_product_identifier(task_id, field="task_id")

        async def settle() -> ProductAdvanceResult:
            await self._reconcile_started(task_id)
            summary = await self._require_status(
                task_id, ProductTaskStatus.AWAITING_APPROVAL
            )
            if summary.review_id is None:
                raise ProductStateError("product-review-missing", task_id)
            await self._execution.release(task_id, reason="rejected")
            rejected = await self._tasks.reject_task(
                task_id=task_id,
                operation_id=_operation_id("reject", task_id),
                review_id=summary.review_id,
            )
            return ProductAdvanceResult(summary=rejected)

        return await self._owned(task_id, settle)

    async def cancel(self, task_id: str) -> ProductAdvanceResult:
        task_id = require_product_identifier(task_id, field="task_id")

        async def settle() -> ProductAdvanceResult:
            summary = await self._tasks.load(task_id)
            if summary is None:
                raise ProductStateError("product-task-unknown", task_id)
            if summary.settled:
                return ProductAdvanceResult(summary=summary)
            await self._execution.cancel(task_id)
            await self._execution.release(task_id, reason="cancelled")
            cancelled = await self._tasks.cancel_task(
                task_id=task_id,
                operation_id=_operation_id("cancel", task_id),
                reason_code="user-cancelled",
            )
            return ProductAdvanceResult(summary=cancelled)

        return await self._owned(task_id, settle)

    async def abandon(self, task_id: str) -> ProductAdvanceResult:
        task_id = require_product_identifier(task_id, field="task_id")
        summary = await self._tasks.abandon_task(
            task_id=task_id,
            operation_id=_operation_id("abandon", task_id),
            reason_code="user-abandoned",
        )
        # Deliberately no release: an unowned interrupted task has not proven
        # that its resources converged.  Abandonment is an honest product end,
        # not permission to erase evidence another process may still own.
        return ProductAdvanceResult(summary=summary)

    # ------------------------------------------------------------- lifecycle

    async def _after_workflow(
        self, task_id: str, run: WorkflowRun
    ) -> ProductAdvanceResult:
        if run.status is WorkflowStatus.AWAITING_APPROVAL:
            evidence = run.outcome(PRODUCT_VERIFICATION_NODE)
            if evidence is None or evidence.review_id is None:
                raise ProductStateError("product-review-missing", task_id)
            summary = await self._tasks.record_awaiting(
                task_id=task_id,
                operation_id=_operation_id("awaiting", task_id),
                review_id=evidence.review_id,
            )
            ledger = await self._promotions.ledger()
            review = ledger.review(evidence.review_id)
            if review is None:
                raise ProductStateError("product-review-missing", task_id)
            return ProductAdvanceResult(
                summary=summary,
                workflow=run,
                review=review,
                approval_digest=expected_approval_digest(review),
            )
        if run.status is WorkflowStatus.FAILED:
            await self._execution.release(task_id, reason="failed")
            failed = await self._tasks.fail_task(
                task_id=task_id,
                operation_id=_operation_id("fail", task_id),
                failure_code=run.failure_code or "workflow-failed",
            )
            return ProductAdvanceResult(summary=failed, workflow=run)
        raise ProductStateError("product-workflow-ended-without-approval", task_id)

    async def _reconcile_started(self, task_id: str) -> ProductTaskSummary | None:
        """Bring ProductTask level with a durable Workflow approval barrier.

        The Workflow append and ProductTask append are separate CAS streams.
        A process may die after the first one, so every task-id operation must
        re-read both and append the one missing Product fact before deciding
        whether approval is allowed.  No node is re-executed here.
        """

        summary = await self._tasks.load(task_id)
        if summary is None or summary.status is not ProductTaskStatus.STARTED:
            return summary
        assembly = await self._assembly.assemble(
            task_id=task_id, profile_id=self._profile_id
        )
        run = await self._execution.state(task_id, assembly)
        if run.status is WorkflowStatus.FAILED:
            await self._execution.release(task_id, reason="failed")
            return await self._tasks.fail_task(
                task_id=task_id,
                operation_id=_operation_id("fail", task_id),
                failure_code=run.failure_code or "workflow-failed",
            )
        if run.status not in {WorkflowStatus.AWAITING_APPROVAL, WorkflowStatus.COMPLETED}:
            return summary
        evidence = run.outcome(PRODUCT_VERIFICATION_NODE)
        if evidence is None or evidence.review_id is None:
            raise ProductStateError("product-review-missing", task_id)
        return await self._tasks.record_awaiting(
            task_id=task_id,
            operation_id=_operation_id("awaiting", task_id),
            review_id=evidence.review_id,
        )

    async def _require_status(
        self, task_id: str, status: ProductTaskStatus
    ) -> ProductTaskSummary:
        summary = await self._tasks.load(task_id)
        if summary is None:
            raise ProductStateError("product-task-unknown", task_id)
        if summary.status is not status:
            raise ProductStateError("product-task-state-invalid", task_id)
        return summary

    async def _owned(
        self,
        task_id: str,
        factory,
        *,
        cancel_execution_on_caller_cancel: bool = False,
    ) -> ProductAdvanceResult:
        async with self._lock:
            self._require_open()
            task = self._pending.get(task_id)
            if task is None:
                task = asyncio.create_task(
                    factory(), name=f"traceh-product-control-{task_id}"
                )
                self._pending[task_id] = task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if cancel_execution_on_caller_cancel:
                cancellation = asyncio.create_task(
                    self._execution.cancel(task_id),
                    name=f"traceh-product-cancel-{task_id}",
                )
                await await_worker_convergence(cancellation)
                if not cancellation.cancelled() and cancellation.exception() is not None:
                    raise error from cancellation.exception()
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            if task.done() and not task.cancelled() and task.exception() is not None:
                raise error from task.exception()
            raise
        finally:
            if task.done():
                async with self._lock:
                    if self._pending.get(task_id) is task:
                        self._pending.pop(task_id, None)

    def _require_open(self) -> None:
        if self._closed:
            raise ProductServiceClosedError

    async def aclose(self) -> None:
        async with self._lock:
            self._closed = True
            pending = tuple(self._pending.values())
            self._proposals.clear()
        failures: list[BaseException] = []
        for task in pending:
            try:
                await asyncio.shield(task)
            except BaseException as error:
                failures.append(error)
        try:
            await self._execution.aclose()
        except BaseException as error:
            failures.append(error)
        if failures:
            raise BaseExceptionGroup("Product control close failed", failures)


def _require_requirement(value: object) -> str:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > MAX_ROUTER_SUMMARY_CHARS
    ):
        raise ProductInputError("product-requirement-invalid", "requirement")
    return value


def _requirement_digest(requirement: str) -> str:
    return fingerprint(
        {"purpose": "product-task-requirement", "content": requirement}
    )


def _task_id(proposal: ProductTaskProposal) -> str:
    """Return the one task identity this Proposal may create if confirmed.

    Confirmation remains mandatory evidence in ``product/task-opened``.  It is
    deliberately not part of identity: two later messages cannot turn one
    Proposal into two tasks, and the host can show the identity before any
    fallible allocation begins.
    """

    return "product-task-" + fingerprint(
        {
            "purpose": "confirmed-product-task",
            "proposal_id": proposal.proposal_id,
        }
    )


def _failure_code(error: BaseException) -> str:
    code = getattr(error, "code", None)
    if type(code) is str:
        try:
            return require_product_identifier(code, field="failure_code")
        except ProductInputError:
            pass
    return "product-execution-failed"


def _operation_id(purpose: str, task_id: str) -> str:
    return "pt-op-" + fingerprint(
        {"purpose": purpose, "task_id": task_id}
    )


__all__ = [
    "PendingProductProposal",
    "ProductAdvanceResult",
    "ProductExecutionBoundary",
    "ProductInspection",
    "ProductTaskControlPlane",
]
