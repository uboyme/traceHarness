"""What each of the five node kinds actually does.

Every executor calls a public service and nothing else. None of them reads a
Supervisor Activation table, an Inbox worker, a pending-create map or any other
private scheduling state: when an executor needs to know whether something has
already happened, it replays the durable facts that domain owns.

Re-entry is the normal case, not an error path. A node that already created its
Agent, already sent its message or already captured its Artifact must observe
that from durable evidence and continue, never repeat the side effect.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from traceh.agents.directory import AgentDirectoryReader
from traceh.agents.identity import agent_created_data, creation_matches
from traceh.agents.inbox import AcceptedMessage, AgentInboxReader
from traceh.agents.inbox_identity import acceptance_matches, message_accepted_data
from traceh.api.agents import (
    AgentMessage,
    AgentRecord,
    AgentSpec,
    AgentSupervisor,
    MessageTarget,
)
from traceh.api.json_types import JsonValue
from traceh.api.workflow import (
    WorkflowBindingResolver,
    WorkflowNode,
    WorkflowNodeKind,
)
from traceh.artifacts.capture import PatchCaptureService
from traceh.promotion.models import expected_approval_digest
from traceh.promotion.service import PatchPromotionService
from traceh.workflow.errors import (
    WorkflowInputError,
    WorkflowNodeFailedError,
    WorkflowStateError,
)
from traceh.workflow.events import node_completed_data
from traceh.workflow.models import (
    agent_identity,
    freeze_map_keys,
    node_kind,
    require_workflow_identifier,
    review_request_identity,
)
from traceh.workflow.projection import WorkflowProjection


@dataclass(frozen=True, slots=True)
class WorkflowServices:
    """The public services a Workflow is allowed to compose."""

    supervisor: AgentSupervisor
    capture: PatchCaptureService | None = None
    promotion: PatchPromotionService | None = None


@dataclass(frozen=True, slots=True)
class NodeExecution:
    """One node's result: a durable payload, an expansion, or a human barrier."""

    completed: dict[str, JsonValue] | None = None
    map_keys: tuple[str, ...] = ()
    awaiting_review_id: str | None = None


class NodeExecutor:
    """Run one node of one run against the public services."""

    __slots__ = ("_resolver", "_run_id", "_services")

    def __init__(
        self,
        *,
        run_id: str,
        services: WorkflowServices,
        resolver: WorkflowBindingResolver,
    ) -> None:
        self._run_id = require_workflow_identifier(run_id, field="run_id")
        self._services = services
        self._resolver = resolver

    async def execute(
        self,
        node: WorkflowNode,
        map_key: str | None,
        projection: WorkflowProjection,
    ) -> NodeExecution:
        kind = node_kind(node)
        if kind is WorkflowNodeKind.AGENT_TASK:
            return await self._agent_task(
                node.node_id,
                kind,
                spec_binding=node.spec_binding,
                message_binding=node.message_binding,
                capture_artifact=node.capture_artifact,
                map_key=None,
            )
        if kind is WorkflowNodeKind.MAP:
            if map_key is None:
                return await self._map_expansion(node)
            return await self._agent_task(
                _child_node_id(node.node_id, map_key),
                kind,
                spec_binding=node.child_spec_binding,
                message_binding=node.child_message_binding,
                capture_artifact=node.capture_artifact,
                map_key=map_key,
            )
        if kind is WorkflowNodeKind.JOIN:
            return NodeExecution(
                completed=node_completed_data(
                    node_id=node.node_id, kind=kind, map_key=None
                )
            )
        if kind is WorkflowNodeKind.VERIFICATION:
            return await self._verification(node, projection)
        return await self._approval(node, projection)

    # ------------------------------------------------------------ agent task

    async def _agent_task(
        self,
        node_id: str,
        kind: WorkflowNodeKind,
        *,
        spec_binding: str,
        message_binding: str,
        capture_artifact: bool,
        map_key: str | None,
    ) -> NodeExecution:
        supervisor = self._services.supervisor
        agent_id, session_id, request_id, message_id = agent_identity(
            self._run_id, node_id
        )
        spec = await self._agent_spec(spec_binding, node_id, map_key)

        # Durable identity decides whether this Agent already exists. Calling
        # create again and hoping it is idempotent would be a guess.
        directory = await AgentDirectoryReader(supervisor.store).load()
        existing = directory.get(agent_id)
        if existing is None:
            await supervisor.create(
                spec, request_id=request_id, agent_id=agent_id, session_id=session_id
            )
        else:
            # Matching only the id would let any record that happens to occupy
            # this identity be adopted as this node's own work. The durable
            # create fact has to be the one this node would have written.
            _require_same_agent(existing, spec, session_id, request_id, node_id)
            await supervisor.resume(session_id)

        try:
            content = await self._message_content(message_binding, node_id, map_key)
            message = AgentMessage(
                message_id=message_id,
                content=content,
                source=f"workflow:{self._run_id}",
            )
            accepted = await _accepted_message(supervisor, agent_id, message_id)
            if accepted is None:
                await supervisor.send(
                    agent_id,
                    message,
                    target=MessageTarget.NEW_TURN,
                    wakeup=True,
                )
            else:
                # Same reasoning as above: an accepted message carrying someone
                # else's content is not this node's message, and waiting on it
                # would report a foreign result as this node's outcome.
                _require_same_message(accepted, message, agent_id, node_id)
            report = await supervisor.wait_message(agent_id, message_id)
            if report.status != "completed":
                raise WorkflowNodeFailedError("workflow-agent-message-failed", node_id)

            artifact_id: str | None = None
            if capture_artifact:
                artifact_id = await self._capture(agent_id, message_id, node_id)
        finally:
            # This closes the Activation and its process slot, whatever happened
            # above. It deliberately does *not* release the Workspace: a managed
            # worktree outlives the Agent that used it, because the Patch it
            # holds is still evidence. Releasing it is the host's explicit
            # decision, not a side effect of a node finishing.
            await supervisor.dispose(agent_id)

        return NodeExecution(
            completed=node_completed_data(
                node_id=node_id,
                kind=kind,
                map_key=map_key,
                agent_id=agent_id,
                message_id=message_id,
                artifact_id=artifact_id,
            )
        )

    async def _capture(
        self, agent_id: str, message_id: str, node_id: str
    ) -> str:
        capture = self._services.capture
        if capture is None:
            raise WorkflowStateError("workflow-capture-service-missing", node_id)
        # Capture is itself idempotent per (agent, message): a re-entry resolves
        # the already recorded Manifest instead of producing a second one.
        artifact = await capture.capture(agent_id, message_id)
        return artifact.manifest.artifact_id

    # ------------------------------------------------------------------- map

    async def _map_expansion(self, node: WorkflowNode) -> NodeExecution:
        raw = await self._resolver.map_keys(
            node.keys_binding, run_id=self._run_id, node_id=node.node_id
        )
        keys = freeze_map_keys(raw, limit=node.max_fan_out)
        return NodeExecution(
            completed=node_completed_data(
                node_id=node.node_id, kind=WorkflowNodeKind.MAP, map_key=None
            ),
            map_keys=keys,
        )

    # ---------------------------------------------------------- verification

    async def _verification(
        self, node: WorkflowNode, projection: WorkflowProjection
    ) -> NodeExecution:
        promotion = self._services.promotion
        if promotion is None:
            raise WorkflowStateError("workflow-promotion-service-missing", node.node_id)
        source = projection.state(node.artifact_node_id)
        if source is None or source.artifact_id is None:
            raise WorkflowNodeFailedError("workflow-artifact-missing", node.node_id)
        report = await promotion.review(
            review_request_id=review_request_identity(self._run_id, node.node_id),
            artifact_id=source.artifact_id,
            target_id=node.target_id,
        )
        if not report.passed:
            # The Review itself stays durable in the promotion ledger; what this
            # refuses is letting a failed Review flow onward as a success.
            raise WorkflowNodeFailedError(
                "workflow-verification-not-passed", node.node_id
            )
        return NodeExecution(
            completed=node_completed_data(
                node_id=node.node_id,
                kind=WorkflowNodeKind.VERIFICATION,
                map_key=None,
                artifact_id=source.artifact_id,
                review_id=report.review_id,
            )
        )

    # -------------------------------------------------------------- approval

    async def _approval(
        self, node: WorkflowNode, projection: WorkflowProjection
    ) -> NodeExecution:
        promotion = self._services.promotion
        if promotion is None:
            raise WorkflowStateError("workflow-promotion-service-missing", node.node_id)
        source = projection.state(node.review_node_id)
        if source is None or source.review_id is None:
            raise WorkflowNodeFailedError("workflow-review-missing", node.node_id)

        ledger = await promotion.ledger()
        review = ledger.review(source.review_id)
        if review is None or not review.passed:
            raise WorkflowNodeFailedError("workflow-review-not-approvable", node.node_id)
        approval = ledger.approval_for_review(review.review_id)
        if approval is None:
            # A human has not decided yet. The Workflow records that it is
            # waiting and stops; it never approves on anyone's behalf.
            return NodeExecution(awaiting_review_id=review.review_id)

        # The approval must be for exactly this review, this artifact and this
        # target, with a digest recomputed from the review's own content.
        if (
            approval.approval_digest != expected_approval_digest(review)
            or review.artifact_id != source.artifact_id
            or approval.review_id != review.review_id
        ):
            raise WorkflowNodeFailedError("workflow-approval-stale", node.node_id)
        return NodeExecution(
            completed=node_completed_data(
                node_id=node.node_id,
                kind=WorkflowNodeKind.APPROVAL,
                map_key=None,
                artifact_id=review.artifact_id,
                review_id=review.review_id,
                approval_digest=approval.approval_digest,
            )
        )

    # ------------------------------------------------------------- bindings

    async def _agent_spec(
        self, binding: str, node_id: str, map_key: str | None
    ) -> AgentSpec:
        spec = await self._resolver.agent_spec(
            binding, run_id=self._run_id, node_id=node_id, map_key=map_key
        )
        if type(spec) is not AgentSpec:
            raise WorkflowInputError("workflow-agent-spec-invalid", "spec_binding")
        return spec

    async def _message_content(
        self, binding: str, node_id: str, map_key: str | None
    ) -> str:
        content = await self._resolver.message_content(
            binding, run_id=self._run_id, node_id=node_id, map_key=map_key
        )
        if type(content) is not str or not content:
            raise WorkflowInputError("workflow-message-invalid", "message_binding")
        return content


async def _accepted_message(
    supervisor: AgentSupervisor, agent_id: str, message_id: str
) -> AcceptedMessage | None:
    inbox = await AgentInboxReader(supervisor.store).load(agent_id)
    for item in inbox.messages:
        if item.message.message_id == message_id:
            return item
    return None


def _require_same_agent(
    existing: AgentRecord,
    spec: AgentSpec,
    session_id: str,
    request_id: str,
    node_id: str,
) -> None:
    """The durable create fact must be the whole request this node would make.

    Comparison goes through the protocol's own `creation_matches()` so every
    field that defines a creation request participates - including
    ``capability_grants``, which a hand-written subset had omitted and which
    decides what the adopted Agent is allowed to do.

    Exactly one field is delegated: `WorkspaceManagedAgentSupervisor` rewrites
    ``workspace_id`` into the managed catalog id before the inner create, so the
    durable value is that layer's decision and not the intent this node asked
    for. It is taken from the record rather than dropped from the comparison, so
    the remaining fields are still checked in full.
    """

    if existing.session_id != session_id or existing.request_id != request_id:
        raise WorkflowNodeFailedError("workflow-agent-identity-conflict", node_id)
    try:
        expected = agent_created_data(
            agent_id=existing.agent_id,
            session_id=session_id,
            request_id=request_id,
            spec=replace(spec, workspace_id=existing.workspace_id),
        )
    except Exception:
        raise WorkflowNodeFailedError(
            "workflow-agent-identity-conflict", node_id
        ) from None
    if not creation_matches(existing, expected):
        raise WorkflowNodeFailedError("workflow-agent-identity-conflict", node_id)


def _require_same_message(
    accepted: AcceptedMessage,
    expected: AgentMessage,
    agent_id: str,
    node_id: str,
) -> None:
    """The durable acceptance must be the whole delivery this node would make.

    `acceptance_matches()` also compares ``target`` and ``wakeup``, which a
    comparison of `AgentMessage` alone cannot see. Those are delivery semantics:
    a message accepted for a different target, or accepted without the wake-up
    this node requires, is not the operation this node asked for even when the
    text is identical.
    """

    try:
        payload = message_accepted_data(
            agent_id=agent_id,
            message=expected,
            target=MessageTarget.NEW_TURN,
            wakeup=True,
        )
    except Exception:
        raise WorkflowNodeFailedError("workflow-message-conflict", node_id) from None
    if not acceptance_matches(accepted, payload):
        raise WorkflowNodeFailedError("workflow-message-conflict", node_id)


def _child_node_id(parent_node_id: str, map_key: str) -> str:
    from traceh.workflow.models import map_child_node_id

    return map_child_node_id(parent_node_id, map_key)


__all__ = ["NodeExecution", "NodeExecutor", "WorkflowServices"]
