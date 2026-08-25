"""Definition freezing, DAG validation and derived Workflow identities.

Every identity here is a deterministic function of the run and the definition,
never of scheduling order. That is what makes re-entry safe: the second attempt
computes the same Agent, message, capture and review identity as the first, so
the underlying services recognise it as the same operation instead of doing the
work twice.

Replay recomputes these values rather than reading them back out of an event,
so a shape-valid but wrong derived field in a payload is rejected.
"""

from __future__ import annotations

from traceh.agents.identity import is_agent_identifier
from traceh.api.json_types import JsonValue, fingerprint
from traceh.api.workflow import (
    AgentTaskNode,
    ApprovalNode,
    JoinNode,
    MapNode,
    VerificationNode,
    WorkflowDefinition,
    WorkflowNode,
    WorkflowNodeKind,
)
from traceh.workflow.errors import WorkflowDefinitionError, WorkflowInputError

WORKFLOW_PROTOCOL_VERSION = 1
"""The only Workflow protocol this build reads or writes."""

MAX_NODES = 256
MAX_PREDECESSORS = 32
MAX_FAN_OUT = 64
MAX_MAP_KEY_LENGTH = 256

_NODE_TYPES: dict[type, WorkflowNodeKind] = {
    AgentTaskNode: WorkflowNodeKind.AGENT_TASK,
    MapNode: WorkflowNodeKind.MAP,
    JoinNode: WorkflowNodeKind.JOIN,
    VerificationNode: WorkflowNodeKind.VERIFICATION,
    ApprovalNode: WorkflowNodeKind.APPROVAL,
}


def require_workflow_identifier(value: object, *, field: str) -> str:
    try:
        valid = is_agent_identifier(value)
        normalized = str(value) if valid else ""
    except Exception:
        valid = False
        normalized = ""
    if not valid or not is_agent_identifier(normalized):
        raise WorkflowInputError("workflow-identity-invalid", field)
    return normalized


def require_bounded_int(value: object, *, minimum: int, maximum: int, field: str) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        raise WorkflowInputError(f"workflow-{field}-invalid", field)
    return value


def require_exact_bool(value: object, *, field: str) -> bool:
    """``1`` and ``"yes"`` are not booleans; a durable flag must be exact."""

    if type(value) is not bool:
        raise WorkflowInputError(f"workflow-{field}-invalid", field)
    return value


def freeze_map_keys(value: object, *, limit: int) -> tuple[str, ...]:
    """A bounded, duplicate-free, canonically ordered key set.

    Order is imposed here rather than taken from the resolver so that two runs
    of the same definition expand to the same child identities regardless of
    how the host happened to build the collection.
    """

    if type(value) not in (list, tuple):
        raise WorkflowInputError("workflow-map-keys-invalid", "map_keys")
    if not value:
        raise WorkflowInputError("workflow-map-keys-empty", "map_keys")
    if len(value) > limit:
        raise WorkflowInputError("workflow-map-fan-out-exceeded", "map_keys")
    seen: set[str] = set()
    for item in value:
        if (
            type(item) is not str
            or not item
            or item != item.strip()
            or len(item) > MAX_MAP_KEY_LENGTH
        ):
            raise WorkflowInputError("workflow-map-keys-invalid", "map_keys")
        if not is_agent_identifier(item):
            raise WorkflowInputError("workflow-map-keys-invalid", "map_keys")
        if item in seen:
            raise WorkflowInputError("workflow-map-keys-duplicate", "map_keys")
        seen.add(item)
    return tuple(sorted(value))


def node_kind(node: object) -> WorkflowNodeKind:
    kind = _NODE_TYPES.get(type(node))
    if kind is None:
        raise WorkflowDefinitionError("workflow-node-kind-unknown")
    return kind


def freeze_workflow_definition(definition: object) -> WorkflowDefinition:
    """Validate one fixed DAG completely, before anything can run."""

    if type(definition) is not WorkflowDefinition:
        raise WorkflowInputError("workflow-definition-invalid", "definition")
    require_workflow_identifier(definition.definition_id, field="definition_id")
    nodes = definition.nodes
    if type(nodes) is not tuple:
        # A list would let a caller keep a mutable handle on the definition.
        raise WorkflowDefinitionError("workflow-definition-nodes-invalid")
    if not nodes:
        raise WorkflowDefinitionError("workflow-definition-empty")
    if len(nodes) > MAX_NODES:
        raise WorkflowDefinitionError("workflow-definition-too-large")

    by_id: dict[str, WorkflowNode] = {}
    for node in nodes:
        kind = node_kind(node)
        node_id = require_workflow_identifier(node.node_id, field="node_id")
        if node_id in by_id:
            raise WorkflowDefinitionError("workflow-node-duplicate", node_id)
        predecessors = node.predecessors
        if type(predecessors) is not tuple:
            raise WorkflowDefinitionError("workflow-node-predecessors-invalid", node_id)
        if len(predecessors) > MAX_PREDECESSORS:
            raise WorkflowDefinitionError("workflow-node-predecessors-exceeded", node_id)
        seen: set[str] = set()
        for predecessor in predecessors:
            predecessor = require_workflow_identifier(predecessor, field="predecessor")
            if predecessor == node_id:
                raise WorkflowDefinitionError("workflow-node-self-edge", node_id)
            if predecessor in seen:
                raise WorkflowDefinitionError(
                    "workflow-node-predecessor-duplicate", node_id
                )
            seen.add(predecessor)
        _freeze_node_fields(node, kind, node_id)
        by_id[node_id] = node

    for node in nodes:
        for predecessor in node.predecessors:
            if predecessor not in by_id:
                raise WorkflowDefinitionError(
                    "workflow-node-predecessor-unknown", node.node_id
                )
        _require_reference(node, by_id)

    _require_acyclic(by_id)
    _require_reachable(by_id)
    return definition


def _freeze_node_fields(
    node: WorkflowNode, kind: WorkflowNodeKind, node_id: str
) -> None:
    if kind is WorkflowNodeKind.AGENT_TASK:
        require_workflow_identifier(node.spec_binding, field="spec_binding")
        require_workflow_identifier(node.message_binding, field="message_binding")
        require_exact_bool(node.capture_artifact, field="capture-artifact")
    elif kind is WorkflowNodeKind.MAP:
        require_workflow_identifier(node.keys_binding, field="keys_binding")
        require_workflow_identifier(node.child_spec_binding, field="spec_binding")
        require_workflow_identifier(node.child_message_binding, field="message_binding")
        require_exact_bool(node.capture_artifact, field="capture-artifact")
        require_bounded_int(
            node.max_fan_out, minimum=1, maximum=MAX_FAN_OUT, field="fan-out"
        )
    elif kind is WorkflowNodeKind.VERIFICATION:
        require_workflow_identifier(node.artifact_node_id, field="artifact_node_id")
        require_workflow_identifier(node.target_id, field="target_id")
    elif kind is WorkflowNodeKind.APPROVAL:
        require_workflow_identifier(node.review_node_id, field="review_node_id")
    del node_id


def _require_reference(node: WorkflowNode, by_id: dict[str, WorkflowNode]) -> None:
    """A node that names another node must name one that can precede it."""

    kind = node_kind(node)
    if kind is WorkflowNodeKind.VERIFICATION:
        referenced = node.artifact_node_id
        expected = WorkflowNodeKind.AGENT_TASK
    elif kind is WorkflowNodeKind.APPROVAL:
        referenced = node.review_node_id
        expected = WorkflowNodeKind.VERIFICATION
    else:
        return
    target = by_id.get(referenced)
    if target is None or node_kind(target) is not expected:
        raise WorkflowDefinitionError("workflow-node-reference-invalid", node.node_id)
    if not _reaches(by_id, referenced, node.node_id):
        raise WorkflowDefinitionError("workflow-node-reference-unordered", node.node_id)


def _reaches(by_id: dict[str, WorkflowNode], source: str, target: str) -> bool:
    """Whether ``source`` is an ancestor of ``target`` in the definition."""

    pending = [target]
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        node = by_id.get(current)
        if node is None:
            continue
        for predecessor in node.predecessors:
            if predecessor == source:
                return True
            pending.append(predecessor)
    return False


def _require_acyclic(by_id: dict[str, WorkflowNode]) -> None:
    state: dict[str, int] = {}
    for start in by_id:
        stack: list[tuple[str, bool]] = [(start, False)]
        while stack:
            node_id, leaving = stack.pop()
            if leaving:
                state[node_id] = 2
                continue
            current = state.get(node_id, 0)
            if current == 2:
                continue
            if current == 1:
                raise WorkflowDefinitionError("workflow-definition-cycle", node_id)
            state[node_id] = 1
            stack.append((node_id, True))
            for predecessor in by_id[node_id].predecessors:
                stack.append((predecessor, False))


def _require_reachable(by_id: dict[str, WorkflowNode]) -> None:
    """Every node must be reachable from a root, or it is dead definition."""

    roots = [node_id for node_id, node in by_id.items() if not node.predecessors]
    if not roots:
        raise WorkflowDefinitionError("workflow-definition-rootless")
    successors: dict[str, list[str]] = {node_id: [] for node_id in by_id}
    for node_id, node in by_id.items():
        for predecessor in node.predecessors:
            successors[predecessor].append(node_id)
    reached: set[str] = set()
    pending = list(roots)
    while pending:
        node_id = pending.pop()
        if node_id in reached:
            continue
        reached.add(node_id)
        pending.extend(successors[node_id])
    missing = set(by_id) - reached
    if missing:
        raise WorkflowDefinitionError(
            "workflow-node-unreachable", sorted(missing)[0]
        )


def node_definition_data(node: WorkflowNode) -> dict[str, JsonValue]:
    """The exact canonical shape one node contributes to the definition hash."""

    kind = node_kind(node)
    data: dict[str, JsonValue] = {
        "node_id": node.node_id,
        "kind": kind.value,
        "predecessors": list(node.predecessors),
    }
    if kind is WorkflowNodeKind.AGENT_TASK:
        data["spec_binding"] = node.spec_binding
        data["message_binding"] = node.message_binding
        data["capture_artifact"] = node.capture_artifact
    elif kind is WorkflowNodeKind.MAP:
        data["keys_binding"] = node.keys_binding
        data["child_spec_binding"] = node.child_spec_binding
        data["child_message_binding"] = node.child_message_binding
        data["capture_artifact"] = node.capture_artifact
        data["max_fan_out"] = node.max_fan_out
    elif kind is WorkflowNodeKind.VERIFICATION:
        data["artifact_node_id"] = node.artifact_node_id
        data["target_id"] = node.target_id
    elif kind is WorkflowNodeKind.APPROVAL:
        data["review_node_id"] = node.review_node_id
    return data


def workflow_definition_hash(definition: WorkflowDefinition) -> str:
    """Bind a run to the complete definition, not to its identifier.

    ``canonical_json`` distinguishes ``True`` from ``1``, so a definition that
    only differs by a boolean-shaped flag is a different definition.
    """

    definition = freeze_workflow_definition(definition)
    return fingerprint(
        {
            "protocol": WORKFLOW_PROTOCOL_VERSION,
            "purpose": "workflow-definition",
            "definition_id": definition.definition_id,
            "nodes": [
                node_definition_data(node)
                for node in sorted(definition.nodes, key=lambda item: item.node_id)
            ],
        }
    )


def workflow_operation_id(
    *, run_id: str, node_id: str, purpose: str, map_key: str | None = None
) -> str:
    """One stable identity for one side-effecting call from one node."""

    run_id = require_workflow_identifier(run_id, field="run_id")
    node_id = require_workflow_identifier(node_id, field="node_id")
    purpose = require_workflow_identifier(purpose, field="purpose")
    if map_key is not None:
        map_key = require_workflow_identifier(map_key, field="map_key")
    return fingerprint(
        {
            "protocol": WORKFLOW_PROTOCOL_VERSION,
            "purpose": purpose,
            "run_id": run_id,
            "node_id": node_id,
            "map_key": map_key,
        }
    )


def agent_identity(run_id: str, node_id: str) -> tuple[str, str, str, str]:
    """The Agent, Session, create request and message identity for one node.

    All four derive from the same run/node pair, so a retry addresses exactly
    the Agent and message the first attempt used.
    """

    create = workflow_operation_id(run_id=run_id, node_id=node_id, purpose="create")
    message = workflow_operation_id(run_id=run_id, node_id=node_id, purpose="message")
    return (
        f"wf-agent-{create}",
        f"wf-session-{create}",
        f"wf-create-{create}",
        f"wf-message-{message}",
    )


def review_request_identity(run_id: str, node_id: str) -> str:
    return "wf-review-" + workflow_operation_id(
        run_id=run_id, node_id=node_id, purpose="review"
    )


def map_child_node_id(parent_node_id: str, map_key: str) -> str:
    """Child identity comes from the parent and the key, never from ordering."""

    parent_node_id = require_workflow_identifier(parent_node_id, field="node_id")
    map_key = require_workflow_identifier(map_key, field="map_key")
    return "wf-child-" + fingerprint(
        {
            "protocol": WORKFLOW_PROTOCOL_VERSION,
            "purpose": "workflow-map-child",
            "parent_node_id": parent_node_id,
            "map_key": map_key,
        }
    )


__all__ = [
    "MAX_FAN_OUT",
    "MAX_NODES",
    "MAX_PREDECESSORS",
    "WORKFLOW_PROTOCOL_VERSION",
    "agent_identity",
    "freeze_map_keys",
    "freeze_workflow_definition",
    "map_child_node_id",
    "node_definition_data",
    "node_kind",
    "require_bounded_int",
    "require_exact_bool",
    "require_workflow_identifier",
    "review_request_identity",
    "workflow_definition_hash",
    "workflow_operation_id",
]
