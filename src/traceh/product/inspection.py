"""Human-readable ProductTask evidence projected from existing durable facts.

This module is a read model for the Chat host, not another ProductTask state.
Workflow progress, Agent/Session identity, Patch bytes and verifier outcomes stay
owned by their existing streams and CAS.  Every call rebuilds those sources and
returns a bounded display value; nothing here is persisted or fed to a model.
"""

from __future__ import annotations

from dataclasses import dataclass

from traceh.agents.directory import AgentDirectoryReader
from traceh.api.events import EventEnvelope
from traceh.api.product import ProductRole, ProductTaskSummary
from traceh.api.promotion import PatchReviewReport, VerificationPlan
from traceh.api.workflow import NodeOutcome, NodeStatus
from traceh.artifacts.reader import PatchArtifactReader
from traceh.cli.command_line import escape_for_display
from traceh.llm.failures import ProviderFailure, ProviderFailureCategory
from traceh.product.errors import ProductInputError, ProductStateError
from traceh.product.topology import (
    PRODUCT_APPROVAL_NODE,
    PRODUCT_VERIFICATION_NODE,
    product_role_node_id,
    product_workflow_definition,
)
from traceh.promotion.models import review_matches_verification_plan
from traceh.session.event_store import EventStore
from traceh.session.invariants import CoreInvariantChecker
from traceh.workflow.models import agent_identity, node_kind
from traceh.workflow.projection import WorkflowStreamReader

_SESSION_STREAM_PREFIX = "session:"
_SESSION_CREATED_KEYS = frozenset({"session_id", "workspace", "metadata"})
_RUNTIME_ERROR_KEYS = frozenset(
    {"turn_id", "step_id", "error_type", "message", "traceback"}
)
_PROVIDER_RUNTIME_ERROR_KEYS = _RUNTIME_ERROR_KEYS | frozenset(
    {"failure_code", "failure_category"}
)
_STABLE_RUNTIME_ERROR_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)


@dataclass(frozen=True, slots=True)
class ProductNodeEvidence:
    """One fixed-topology node and any durable Agent Session behind it."""

    node_id: str
    kind: str
    status: str
    agent_id: str | None
    session_id: str | None
    failure_code: str | None
    leaf_failure_code: str | None = None
    leaf_failure_category: str | None = None
    leaf_error_type: str | None = None


@dataclass(frozen=True, slots=True)
class _SessionLeafFailure:
    failure_code: str | None = None
    failure_category: str | None = None
    error_type: str | None = None


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

    @property
    def store(self) -> EventStore:
        """The durable log this pure reader joins."""

        return self._store

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
            leaf_failure = _SessionLeafFailure()
            if node.node_id in role_nodes and outcome is not None:
                (
                    expected_agent,
                    expected_session,
                    expected_request,
                    expected_message,
                ) = agent_identity(summary.workflow_run_id, node.node_id)
                if outcome.agent_id is not None and outcome.agent_id != expected_agent:
                    raise ProductStateError(
                        "product-inspection-agent-identity-mismatch", summary.task_id
                    )
                record = directory.get(expected_agent)
                if record is not None:
                    session_matches = record.session_id == expected_session
                    request_matches = record.request_id == expected_request
                    identity_conflict = (
                        status is NodeStatus.FAILED
                        and failure_code == "workflow-agent-identity-conflict"
                    )
                    if status is NodeStatus.COMPLETED and not session_matches:
                        raise ProductStateError(
                            "product-inspection-session-identity-mismatch",
                            summary.task_id,
                        )
                    if status is NodeStatus.COMPLETED and not request_matches:
                        raise ProductStateError(
                            "product-inspection-agent-identity-mismatch",
                            summary.task_id,
                        )
                    if session_matches and request_matches and not identity_conflict:
                        agent_id = expected_agent
                        session_id = record.session_id
                    if (
                        status is NodeStatus.FAILED
                        and failure_code == "workflow-agent-message-failed"
                        and session_matches
                        and request_matches
                    ):
                        leaf_failure = await self._session_leaf_failure(
                            summary.task_id,
                            record.session_id,
                            expected_message,
                        )
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
                    leaf_failure_code=leaf_failure.failure_code,
                    leaf_failure_category=leaf_failure.failure_category,
                    leaf_error_type=leaf_failure.error_type,
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

    async def _session_leaf_failure(
        self,
        task_id: str,
        session_id: str,
        expected_message_id: str,
    ) -> _SessionLeafFailure:
        try:
            events = await self._store.read(
                f"{_SESSION_STREAM_PREFIX}{session_id}"
            )
        except Exception:
            raise ProductStateError(
                "product-inspection-session-unreadable", task_id
            ) from None
        try:
            # This is the executable Session lifecycle contract.  The read
            # model never trusts a leaf-looking payload before the complete
            # associated Session has passed it.
            if CoreInvariantChecker().check(events):
                raise ValueError("invalid Session lifecycle")
            return _message_leaf_failure(
                events,
                session_id,
                expected_message_id,
            )
        except Exception:
            raise ProductStateError(
                "product-inspection-session-invalid", task_id
            ) from None

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


def _message_leaf_failure(
    events: tuple[EventEnvelope, ...],
    session_id: str,
    expected_message_id: str,
) -> _SessionLeafFailure:
    if not events:
        return _SessionLeafFailure()

    stream_id = f"{_SESSION_STREAM_PREFIX}{session_id}"
    open_turn_id: str | None = None
    target_turn_id: str | None = None
    target_failure = _SessionLeafFailure()
    target_failure_count = 0
    target_terminal_reason: str | None = None
    for expected_seq, event in enumerate(events, start=1):
        if (
            type(event.stream_id) is not str
            or event.stream_id != stream_id
            or type(event.seq) is not int
            or event.seq != expected_seq
            or type(event.schema_version) is not int
            or event.schema_version != 1
            or type(event.type) is not str
            or type(event.data) is not dict
        ):
            raise ValueError("invalid Session envelope")
        data = event.data
        if expected_seq == 1:
            if (
                event.type != "session/created"
                or set(data) != _SESSION_CREATED_KEYS
                or _plain_text(data.get("session_id")) != session_id
                or type(data.get("workspace")) is not str
                or type(data.get("metadata")) is not dict
            ):
                raise ValueError("invalid Session identity")
        elif event.type == "session/created":
            raise ValueError("duplicate Session identity")

        if event.type == "turn/start":
            turn_id = _required_plain_text(data.get("turn_id"))
            message_id = _required_plain_text(data.get("message_id"))
            if open_turn_id is not None:
                raise ValueError("overlapping Session turns")
            open_turn_id = turn_id
            if message_id == expected_message_id:
                if target_turn_id is not None:
                    raise ValueError("duplicate Workflow message turn")
                target_turn_id = turn_id
        elif event.type == "runtime/error":
            error_turn_id = _required_plain_text(data.get("turn_id"))
            if open_turn_id is None or error_turn_id != open_turn_id:
                raise ValueError("runtime error is outside its declared Turn")
            if open_turn_id == target_turn_id:
                target_failure_count += 1
                if target_failure_count != 1:
                    raise ValueError("Workflow message Turn has multiple runtime errors")
                target_failure = _runtime_error_leaf(data)
        elif event.type == "turn/end":
            end_turn_id = _required_plain_text(data.get("turn_id"))
            if open_turn_id is None or end_turn_id != open_turn_id:
                raise ValueError("Turn end does not close the open Turn")
            if end_turn_id == target_turn_id:
                target_terminal_reason = _required_plain_text(data.get("reason"))
            open_turn_id = None
    if target_failure_count and target_terminal_reason != "failed":
        raise ValueError("Workflow message failure has no failed Turn terminal")
    return target_failure


def _runtime_error_leaf(
    data: dict[str, object],
) -> _SessionLeafFailure:
    keys = set(data)
    if keys not in {_RUNTIME_ERROR_KEYS, _PROVIDER_RUNTIME_ERROR_KEYS}:
        raise ValueError("invalid runtime error payload")
    _required_plain_text(data.get("turn_id"))
    _required_plain_text(data.get("step_id"))
    error_type = _stable_runtime_error_type(data.get("error_type"))
    if type(data.get("message")) is not str or type(data.get("traceback")) is not str:
        raise ValueError("invalid runtime error text")
    if keys == _PROVIDER_RUNTIME_ERROR_KEYS:
        if error_type != "ProviderFailure":
            raise ValueError("invalid provider runtime error type")
        code, category = _provider_failure(
            data.get("failure_code"), data.get("failure_category")
        )
        return _SessionLeafFailure(code, category, None)
    if error_type == "ProviderFailure":
        raise ValueError("provider runtime error has no typed failure")
    return _SessionLeafFailure(None, None, error_type)


def _provider_failure(code: object, category: object) -> tuple[str, str]:
    if type(code) is not str or type(category) is not str:
        raise ValueError("invalid provider failure")
    parsed_category = ProviderFailureCategory(category)
    ProviderFailure(code, parsed_category)
    return code, parsed_category.value


def _stable_runtime_error_type(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 128
        or any(character not in _STABLE_RUNTIME_ERROR_CHARACTERS for character in value)
    ):
        raise ValueError("invalid runtime error type")
    return value


def _required_plain_text(value: object) -> str:
    text = _plain_text(value)
    if text is None or not text:
        raise ValueError("missing Session identity")
    return text


def _plain_text(value: object) -> str | None:
    return value if type(value) is str else None


__all__ = [
    "ProductInspectionEvidenceReader",
    "ProductNodeEvidence",
    "ProductReviewEvidence",
    "ProductTaskEvidence",
    "ProductVerifierEvidence",
]
