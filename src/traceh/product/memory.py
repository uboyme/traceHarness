"""Fresh ProductTask memory and bounded model-facing evidence.

This module does not store memory.  It joins the existing Product, Workflow,
Session, Review, Artifact and Promotion facts on demand, validates their
identities through the established readers, and returns immutable read values.
"""

from __future__ import annotations

from dataclasses import dataclass

from traceh.api.events import EventEnvelope
from traceh.api.json_types import JsonValue, canonical_json
from traceh.api.product import ProductTaskSummary
from traceh.product.activity import ProductTaskActivity, ProductTaskActivityReader
from traceh.product.errors import ProductEvidenceError, ProductStateError
from traceh.product.evidence import ProductTaskSessionRelation, SessionEvidenceReader
from traceh.product.observation import ProductObservation, ProductObservationReader
from traceh.product.projection import ProductTaskStreamReader, replay_product_task
from traceh.session.event_store import EventStore
from traceh.supervision.execution import durable_log_identity

MAX_PRODUCT_EVIDENCE_CHANGED_PATHS = 8
MAX_PRODUCT_EVIDENCE_TOOLS_PER_ROLE = 8
MAX_PRODUCT_EVIDENCE_VERIFIERS = 8
MAX_PRODUCT_EVIDENCE_CONTENT_CHARS = 20_000


@dataclass(frozen=True, slots=True)
class ProductTaskMemoryHead:
    """One Product head plus proven requester-Session relation."""

    summary: ProductTaskSummary
    source_event: EventEnvelope
    relation: ProductTaskSessionRelation


@dataclass(frozen=True, slots=True)
class ProductTaskMemory:
    """One fresh, cross-domain evidence view for a related ProductTask."""

    head: ProductTaskMemoryHead
    observation: ProductObservation
    activity: ProductTaskActivity


class ProductTaskMemoryReader:
    """Read task memory without adding a cache, stream or control operation."""

    __slots__ = ("_activity", "_observation", "_relations", "_store", "_tasks")

    def __init__(
        self,
        store: EventStore,
        observation: ProductObservationReader,
    ) -> None:
        if durable_log_identity(observation.store) is not durable_log_identity(store):
            raise ValueError("product memory readers use different durable logs")
        self._store = store
        self._tasks = ProductTaskStreamReader(store)
        self._relations = SessionEvidenceReader(store)
        self._observation = observation
        self._activity = ProductTaskActivityReader(store)

    @property
    def store(self) -> EventStore:
        return self._store

    @property
    def observation_reader(self) -> ProductObservationReader:
        return self._observation

    async def load_head(
        self, session_id: str, task_id: str
    ) -> ProductTaskMemoryHead:
        head = await self.load_head_if_related(session_id, task_id)
        if head is None:
            raise ProductStateError("product-memory-task-unavailable", task_id)
        return head

    async def load_head_if_related(
        self, session_id: str, task_id: str
    ) -> ProductTaskMemoryHead | None:
        events = await self._tasks.read_events(task_id)
        summary, _ = replay_product_task(task_id, events)
        if summary is None or not events:
            return None
        related = session_id in {
            summary.origin_session_id,
            summary.confirmation_session_id,
        }
        if not related:
            return None
        relation = await self._relations.task_relation(session_id, summary)
        source_event = events[-1]
        if source_event.seq != summary.head_seq:
            raise ProductStateError("product-memory-product-history-invalid", task_id)
        return ProductTaskMemoryHead(summary, source_event, relation)

    async def load(self, session_id: str, task_id: str) -> ProductTaskMemory:
        return await self.load_for_head(
            session_id,
            await self.load_head(session_id, task_id),
        )

    async def load_for_head(
        self,
        session_id: str,
        head: ProductTaskMemoryHead,
    ) -> ProductTaskMemory:
        if type(head) is not ProductTaskMemoryHead:
            raise ProductEvidenceError("product-memory-head-invalid")
        # Re-prove the relation for callers that retained a head across an
        # await.  Session facts are append-only, but this keeps the public read
        # method independently fail-closed.
        relation = await self._relations.task_relation(session_id, head.summary)
        if relation != head.relation:
            raise ProductStateError(
                "product-memory-session-history-changed", head.summary.task_id
            )
        observation = await self._observation.load(head.summary.task_id)
        if observation.summary != head.summary:
            raise ProductStateError(
                "product-memory-product-head-changed", head.summary.task_id
            )
        activity = await self._activity.load(observation)
        latest_events = await self._tasks.read_events(head.summary.task_id)
        latest_summary, _ = replay_product_task(head.summary.task_id, latest_events)
        if (
            latest_summary != head.summary
            or not latest_events
            or latest_events[-1].event_id != head.source_event.event_id
        ):
            raise ProductStateError(
                "product-memory-product-head-changed", head.summary.task_id
            )
        return ProductTaskMemory(head, observation, activity)


def product_task_evidence_data(memory: ProductTaskMemory) -> dict[str, JsonValue]:
    """Return the bounded, non-prose evidence shape exposed by the read Tool."""

    if type(memory) is not ProductTaskMemory:
        raise ValueError("product task memory is invalid")
    summary = memory.head.summary
    observation = memory.observation
    evidence = observation.evidence
    nodes: list[dict[str, JsonValue]] = []
    if evidence is not None:
        for node in evidence.nodes:
            nodes.append(
                {
                    "failure_code": node.failure_code,
                    "kind": node.kind,
                    "leaf_error_type": node.leaf_error_type,
                    "leaf_failure_category": node.leaf_failure_category,
                    "leaf_failure_code": node.leaf_failure_code,
                    "node_id": node.node_id,
                    "status": node.status,
                }
            )

    role_activity: list[dict[str, JsonValue]] = []
    for role in memory.activity.roles:
        selected = role.tools[-MAX_PRODUCT_EVIDENCE_TOOLS_PER_ROLE:]
        role_activity.append(
            {
                "omitted_tool_calls": role.tool_call_count - len(selected),
                "role": role.role,
                "shown_tool_calls": len(selected),
                "tool_call_count": role.tool_call_count,
                "tools": [
                    {
                        "call_seq": tool.call_seq,
                        "exit_code": tool.exit_code,
                        "name": tool.name,
                        "result_seq": tool.result_seq,
                        "status": tool.status,
                    }
                    for tool in selected
                ],
                "turns_completed": role.turns_completed,
                "turns_started": role.turns_started,
            }
        )

    review_data: dict[str, JsonValue] | None = None
    review_evidence = None if evidence is None else evidence.review
    if (observation.review is None) != (review_evidence is None):
        raise ProductStateError(
            "product-memory-review-evidence-incomplete", summary.task_id
        )
    if observation.review is not None and review_evidence is not None:
        paths = review_evidence.changed_paths[:MAX_PRODUCT_EVIDENCE_CHANGED_PATHS]
        verifiers = review_evidence.verifiers[:MAX_PRODUCT_EVIDENCE_VERIFIERS]
        diff = review_evidence.patch_summary
        review_data = {
            "changed_paths": list(paths),
            "omitted_changed_paths": len(review_evidence.changed_paths) - len(paths),
            "passed": observation.review.passed,
            "patch_sha256": observation.review.patch_sha256,
            "patch_size_bytes": review_evidence.patch_size_bytes,
            "patch_totals": (
                None
                if diff is None
                else {
                    "additions": diff.additions,
                    "complete": diff.complete,
                    "deletions": diff.deletions,
                }
            ),
            "review_id": observation.review.review_id,
            "shown_changed_paths": len(paths),
            "shown_verifiers": len(verifiers),
            "verifier_count": len(review_evidence.verifiers),
            "verifiers": [
                {
                    "command_id": verifier.command_id,
                    "exit_code": verifier.exit_code,
                    "status": verifier.status,
                }
                for verifier in verifiers
            ],
        }

    promotion_data: dict[str, JsonValue] | None = None
    if observation.promotion is not None:
        promotion = observation.promotion
        promotion_data = {
            "new_revision": promotion.new_revision,
            "promotion_id": promotion.promotion_id,
            "review_id": promotion.review_id,
            "target_id": promotion.target_id,
            "target_ref": promotion.target_ref,
        }
    usage = observation.usage
    data: dict[str, JsonValue] = {
        "activity": {
            "roles": role_activity,
            "tool_call_count": memory.activity.tool_call_count,
        },
        "approval_recorded": observation.approval is not None,
        "product": {
            "head": f"{memory.head.source_event.stream_id}@{summary.head_seq}",
            "requirement_digest": summary.requirement_digest,
            "requested_mode": summary.requested_mode.value,
            "resolved_mode": (
                None if summary.resolved_mode is None else summary.resolved_mode.value
            ),
            "status": summary.status.value,
            "task_id": summary.task_id,
        },
        "promotion": promotion_data,
        "review": review_data,
        "usage": (
            None
            if usage is None
            else {
                "steps": usage.steps,
                "token_quality": (
                    None if usage.token_quality is None else usage.token_quality.value
                ),
                "tokens": usage.tokens,
                "wall_milliseconds": usage.wall_milliseconds,
            }
        ),
        "workflow": {
            "nodes": nodes,
            "status": None if evidence is None else evidence.workflow_status,
            "streams_diverged": observation.streams_diverged,
        },
    }
    if len(canonical_json(data)) > MAX_PRODUCT_EVIDENCE_CONTENT_CHARS:
        raise ProductStateError("product-memory-output-too-large", summary.task_id)
    return data


__all__ = [
    "MAX_PRODUCT_EVIDENCE_CHANGED_PATHS",
    "MAX_PRODUCT_EVIDENCE_CONTENT_CHARS",
    "MAX_PRODUCT_EVIDENCE_TOOLS_PER_ROLE",
    "MAX_PRODUCT_EVIDENCE_VERIFIERS",
    "ProductTaskMemory",
    "ProductTaskMemoryHead",
    "ProductTaskMemoryReader",
    "product_task_evidence_data",
]
