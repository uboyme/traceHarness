"""Human-readable ProductTask evidence projected from existing durable facts.

This module is a read model for the Chat host, not another ProductTask state.
Workflow progress, Agent/Session identity, Patch bytes and verifier outcomes stay
owned by their existing streams and CAS.  Every call rebuilds those sources and
returns a bounded display value; nothing here is persisted or fed to a model.
"""

from __future__ import annotations

from dataclasses import dataclass

from traceh.agents.directory import AgentDirectoryReader
from traceh.api.product import ProductRole, ProductTaskSummary
from traceh.api.promotion import PatchReviewReport, VerificationPlan
from traceh.api.workflow import NodeOutcome, NodeStatus
from traceh.artifacts.reader import PatchArtifactReader
from traceh.cli.command_line import escape_for_display
from traceh.product.errors import ProductInputError, ProductStateError
from traceh.product.topology import (
    PRODUCT_APPROVAL_NODE,
    PRODUCT_VERIFICATION_NODE,
    product_role_node_id,
    product_workflow_definition,
)
from traceh.promotion.models import review_matches_verification_plan
from traceh.session.event_store import EventStore
from traceh.workflow.models import agent_identity, node_kind
from traceh.workflow.projection import WorkflowStreamReader


@dataclass(frozen=True, slots=True)
class ProductNodeEvidence:
    """One fixed-topology node and any durable Agent Session behind it."""

    node_id: str
    kind: str
    status: str
    agent_id: str | None
    session_id: str | None
    failure_code: str | None


@dataclass(frozen=True, slots=True)
class ProductVerifierEvidence:
    """One frozen verifier command paired with its durable outcome."""

    command_id: str
    executable: str
    argument_count: int
    argv_digest: str
    status: str
    exit_code: int | None


@dataclass(frozen=True, slots=True)
class ProductReviewEvidence:
    """The exact Patch and verification evidence awaiting a human decision."""

    changed_paths: tuple[str, ...]
    patch_size_bytes: int
    patch_preview: str
    patch_preview_truncated: bool
    patch_utf8_replaced: bool
    verifiers: tuple[ProductVerifierEvidence, ...]


@dataclass(frozen=True, slots=True)
class ProductTaskEvidence:
    """Fresh, bounded evidence for one ProductTask inspection."""

    workflow_status: str | None
    nodes: tuple[ProductNodeEvidence, ...]
    review: ProductReviewEvidence | None


class ProductInspectionEvidenceReader:
    """Join Product-linked facts for display without copying their authority."""

    __slots__ = (
        "_artifacts",
        "_directory",
        "_max_patch_chars",
        "_promotion_target_id",
        "_store",
        "_verification_plan",
        "_verification_plan_digest",
        "_workflow",
    )

    def __init__(
        self,
        store: EventStore,
        artifacts: PatchArtifactReader,
        *,
        verification_plan: VerificationPlan,
        verification_plan_digest: str,
        promotion_target_id: str,
        max_patch_chars: int,
    ) -> None:
        if artifacts.store is not store:
            raise ProductInputError("product-store-mismatch", "inspection")
        if type(max_patch_chars) is not int or max_patch_chars < 1:
            raise ProductInputError("product-report-bound-invalid", "max_patch_chars")
        if (
            type(verification_plan_digest) is not str
            or len(verification_plan_digest) != 64
            or verification_plan_digest != verification_plan_digest.lower()
            or any(
                character not in "0123456789abcdef"
                for character in verification_plan_digest
            )
        ):
            raise ProductInputError(
                "product-verification-digest-invalid",
                "verification_plan_digest",
            )
        self._store = store
        self._artifacts = artifacts
        self._verification_plan = verification_plan
        self._verification_plan_digest = verification_plan_digest
        self._promotion_target_id = promotion_target_id
        self._max_patch_chars = max_patch_chars
        self._workflow = WorkflowStreamReader(store)
        self._directory = AgentDirectoryReader(store)

    async def load(
        self,
        summary: ProductTaskSummary,
        review: PatchReviewReport | None,
    ) -> ProductTaskEvidence:
        if type(summary) is not ProductTaskSummary:
            raise ProductInputError("product-summary-invalid", "summary")
        if summary.resolved_mode is None:
            return ProductTaskEvidence(None, (), None)

        definition = product_workflow_definition(
            summary.resolved_mode,
            promotion_target_id=self._promotion_target_id,
        )
        workflow = (await self._workflow.load(summary.workflow_run_id)).run(definition)
        directory = await self._directory.load()
        outcomes = {outcome.node_id: outcome for outcome in workflow.outcomes}
        nodes: list[ProductNodeEvidence] = []
        role_nodes = {product_role_node_id(role) for role in ProductRole}
        for node in definition.nodes:
            outcome = outcomes.get(node.node_id)
            status = NodeStatus.PENDING if outcome is None else outcome.status
            agent_id = None
            session_id = None
            failure_code = None if outcome is None else outcome.failure_code
            if node.node_id in role_nodes and outcome is not None:
                expected_agent, expected_session, _, _ = agent_identity(
                    summary.workflow_run_id, node.node_id
                )
                if outcome.agent_id is not None and outcome.agent_id != expected_agent:
                    raise ProductStateError(
                        "product-inspection-agent-identity-mismatch", summary.task_id
                    )
                record = directory.get(expected_agent)
                if record is not None:
                    if record.session_id != expected_session:
                        raise ProductStateError(
                            "product-inspection-session-identity-mismatch",
                            summary.task_id,
                        )
                    agent_id = expected_agent
                    session_id = record.session_id
                elif status is NodeStatus.COMPLETED:
                    raise ProductStateError(
                        "product-inspection-agent-record-missing", summary.task_id
                    )
            nodes.append(
                ProductNodeEvidence(
                    node_id=node.node_id,
                    kind=node_kind(node).value,
                    status=status.value,
                    agent_id=agent_id,
                    session_id=session_id,
                    failure_code=failure_code,
                )
            )

        review_evidence = None
        if review is not None:
            review_evidence = await self._review_evidence(
                summary, review, outcomes
            )
        return ProductTaskEvidence(
            workflow_status=workflow.status.value,
            nodes=tuple(nodes),
            review=review_evidence,
        )

    async def _review_evidence(
        self,
        summary: ProductTaskSummary,
        review: PatchReviewReport,
        outcomes: dict[str, NodeOutcome],
    ) -> ProductReviewEvidence:
        verification = outcomes.get(PRODUCT_VERIFICATION_NODE)
        coder = outcomes.get(product_role_node_id(ProductRole.CODER))
        if (
            verification is None
            or verification.review_id != review.review_id
            or coder is None
            or coder.artifact_id != review.artifact_id
        ):
            raise ProductStateError(
                "product-inspection-review-chain-broken", summary.task_id
            )
        approval = outcomes.get(PRODUCT_APPROVAL_NODE)
        if (
            approval is not None
            and approval.review_id not in {None, review.review_id}
        ):
            raise ProductStateError(
                "product-inspection-review-chain-broken", summary.task_id
            )
        if (
            review.verifier_definition_digest != self._verification_plan_digest
            or not review_matches_verification_plan(
                review, self._verification_plan
            )
        ):
            raise ProductStateError(
                "product-inspection-verifier-mismatch", summary.task_id
            )
        commands = {command.command_id: command for command in self._verification_plan.commands}
        verifiers = tuple(
            ProductVerifierEvidence(
                command_id=result.command_id,
                executable=commands[result.command_id].argv[0],
                argument_count=len(commands[result.command_id].argv) - 1,
                argv_digest=result.argv_digest,
                status=result.status,
                exit_code=result.exit_code,
            )
            for result in review.results
        )

        artifact = await self._artifacts.load(review.artifact_id)
        manifest = artifact.manifest
        if (
            manifest.manifest_digest != review.manifest_digest
            or manifest.blob.sha256 != review.patch_sha256
            or manifest.blob.size_bytes != review.patch_size_bytes
        ):
            raise ProductStateError(
                "product-inspection-artifact-mismatch", summary.task_id
            )
        preview, truncated, replaced = _patch_preview(
            artifact.content, self._max_patch_chars
        )
        return ProductReviewEvidence(
            changed_paths=manifest.changed_paths,
            patch_size_bytes=manifest.blob.size_bytes,
            patch_preview=preview,
            patch_preview_truncated=truncated,
            patch_utf8_replaced=replaced,
            verifiers=verifiers,
        )


def _patch_preview(content: bytes, limit: int) -> tuple[str, bool, bool]:
    decoded = content.decode("utf-8", errors="replace")
    replaced = "\ufffd" in decoded
    pieces: list[str] = []
    length = 0
    truncated = False
    for character in decoded:
        rendered = "\n" if character == "\n" else escape_for_display(character, limit=8)
        if length + len(rendered) > limit:
            truncated = True
            break
        pieces.append(rendered)
        length += len(rendered)
    if truncated:
        if length == limit:
            pieces[-1] = "…"
        else:
            pieces.append("…")
    return "".join(pieces), truncated, replaced


__all__ = [
    "ProductInspectionEvidenceReader",
    "ProductNodeEvidence",
    "ProductReviewEvidence",
    "ProductTaskEvidence",
    "ProductVerifierEvidence",
]
