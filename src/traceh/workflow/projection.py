"""The one Projector that rebuilds a Workflow run from its own stream.

There is no status file, no in-memory result cache and no second store. Which
nodes are ready, which are still running and whether the run is waiting at a
human barrier are all recomputed from the append-only stream on every load,
which is also what makes the recovery rule checkable rather than assumed.
"""

from __future__ import annotations

from dataclasses import dataclass

from traceh.api.events import EventEnvelope
from traceh.api.json_types import JsonValue
from traceh.api.workflow import (
    NodeOutcome,
    NodeStatus,
    WorkflowDefinition,
    WorkflowNode,
    WorkflowNodeKind,
    WorkflowRun,
    WorkflowStatus,
)
from traceh.session.event_store import EventStore
from traceh.workflow.errors import WorkflowProtocolError
from traceh.workflow.events import (
    WORKFLOW_APPROVAL_AWAITED,
    WORKFLOW_MAP_EXPANDED,
    WORKFLOW_NODE_COMPLETED,
    WORKFLOW_NODE_FAILED,
    WORKFLOW_NODE_STARTED,
    WORKFLOW_RUN_STARTED,
    expected_keys,
    workflow_event_header,
    workflow_stream_id,
)
from traceh.workflow.models import (
    agent_identity,
    freeze_workflow_definition,
    map_child_node_id,
    node_kind,
    workflow_definition_hash,
)


@dataclass(frozen=True, slots=True)
class MapExpansion:
    node_id: str
    map_keys: tuple[str, ...]
    child_node_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _NodeState:
    node_id: str
    kind: WorkflowNodeKind
    map_key: str | None
    status: NodeStatus
    agent_id: str | None = None
    message_id: str | None = None
    artifact_id: str | None = None
    review_id: str | None = None
    approval_digest: str | None = None
    failure_code: str | None = None


class WorkflowProjection:
    """Immutable view of one run, derived only from its durable facts."""

    __slots__ = (
        "_awaiting",
        "_definition_hash",
        "_definition_id",
        "_expansions",
        "_failure_code",
        "_head_seq",
        "_nodes",
        "_run_id",
        "_terminal",
    )

    def __init__(
        self,
        *,
        run_id: str,
        definition_id: str,
        definition_hash: str,
        nodes: dict[str, _NodeState],
        expansions: dict[str, MapExpansion],
        awaiting: tuple[str, ...],
        terminal: str | None,
        failure_code: str | None,
        head_seq: int,
    ) -> None:
        self._run_id = run_id
        self._definition_id = definition_id
        self._definition_hash = definition_hash
        self._nodes = nodes
        self._expansions = expansions
        self._awaiting = awaiting
        self._terminal = terminal
        self._failure_code = failure_code
        self._head_seq = head_seq

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def definition_hash(self) -> str:
        return self._definition_hash

    @property
    def head_seq(self) -> int:
        return self._head_seq

    @property
    def expansions(self) -> dict[str, MapExpansion]:
        return dict(self._expansions)

    @property
    def awaiting_approval(self) -> tuple[str, ...]:
        return self._awaiting

    def state(self, node_id: str) -> _NodeState | None:
        return self._nodes.get(node_id)

    def status_of(self, node_id: str) -> NodeStatus:
        state = self._nodes.get(node_id)
        return NodeStatus.PENDING if state is None else state.status

    def running_nodes(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                node_id
                for node_id, state in self._nodes.items()
                if state.status is NodeStatus.RUNNING
            )
        )

    def failed_nodes(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                node_id
                for node_id, state in self._nodes.items()
                if state.status is NodeStatus.FAILED
            )
        )

    def is_terminal(self, node_id: str) -> bool:
        return self.status_of(node_id) in (NodeStatus.COMPLETED, NodeStatus.FAILED)

    def run(self, definition: WorkflowDefinition) -> WorkflowRun:
        definition = freeze_workflow_definition(definition)
        self._require_definition_agrees(definition)
        outcomes = tuple(
            NodeOutcome(
                node_id=state.node_id,
                kind=state.kind,
                status=state.status,
                agent_id=state.agent_id,
                message_id=state.message_id,
                artifact_id=state.artifact_id,
                review_id=state.review_id,
                approval_digest=state.approval_digest,
                map_keys=self._map_keys_for(state.node_id),
                failure_code=state.failure_code,
            )
            for state in sorted(self._nodes.values(), key=lambda item: item.node_id)
        )
        if self._terminal == NodeStatus.COMPLETED.value:
            status = WorkflowStatus.COMPLETED
        elif self._terminal == NodeStatus.FAILED.value:
            status = WorkflowStatus.FAILED
        elif self._awaiting:
            status = WorkflowStatus.AWAITING_APPROVAL
        else:
            status = WorkflowStatus.RUNNING
        return WorkflowRun(
            run_id=self._run_id,
            definition_id=self._definition_id,
            definition_hash=self._definition_hash,
            status=status,
            outcomes=outcomes,
            awaiting_approval=self._awaiting,
            failure_code=self._failure_code,
            head_seq=self._head_seq,
        )

    def _require_definition_agrees(self, definition: WorkflowDefinition) -> None:
        """The stream may only describe nodes this definition actually declares.

        Start/terminal agreement alone is not enough: a node can be internally
        consistent and still be a kind the definition never contained, or a map
        child of a node that is not a Map.
        """

        by_id = {node.node_id: node for node in definition.nodes}
        declared = {node_id: node_kind(node) for node_id, node in by_id.items()}
        for node_id, state in self._nodes.items():
            node = by_id.get(node_id)
            # A node's role is decided by its identity, and its recorded map key
            # has to agree with that role. Letting the two disagree gives the
            # evidence rule two different answers for the same node: a Map
            # parent that records a key looks like a child and may then carry
            # Agent evidence it never produced.
            if node is not None:
                expected_key: str | None = None
            else:
                origin = self._origin_of_child(node_id)
                node = by_id.get(origin[0]) if origin is not None else None
                if node is None or node_kind(node) is not WorkflowNodeKind.MAP:
                    raise WorkflowProtocolError("workflow-node-undeclared", node_id=node_id)
                expected_key = origin[1]
            if state.map_key != expected_key:
                raise WorkflowProtocolError("workflow-node-map-key-invalid", node_id=node_id)
            if state.kind is not node_kind(node):
                raise WorkflowProtocolError("workflow-node-kind-mismatch", node_id=node_id)
            if state.status is NodeStatus.COMPLETED:
                self._require_declared_evidence(node, node_id, state, expected_key)
        if self._terminal == NodeStatus.COMPLETED.value:
            self._require_dag_completed(definition, declared)

    def _require_declared_evidence(
        self,
        node: WorkflowNode,
        node_id: str,
        state: _NodeState,
        expected_key: str | None,
    ) -> None:
        """The definition-aware half of the evidence rule.

        `rebuild()` has already enforced everything decidable from the node kind
        alone. What only the definition can decide is added here: whether an
        AgentTask yields an Artifact is `capture_artifact` on that particular
        node, and a Map child follows its parent's setting.
        """

        kind = node_kind(node)
        # The caller has already bound the recorded key to this role, so a Map
        # child is exactly a node with an expansion key behind it.
        runs_an_agent = kind is WorkflowNodeKind.AGENT_TASK or (
            kind is WorkflowNodeKind.MAP and expected_key is not None
        )
        # Only an Agent-running node's Artifact is a definition question; every
        # other kind is already settled by the base rule.
        capture = node.capture_artifact if runs_an_agent else None
        _require_evidence(self._run_id, node_id, state, capture_artifact=capture)

    def _require_dag_completed(
        self,
        definition: WorkflowDefinition,
        declared: dict[str, WorkflowNodeKind],
    ) -> None:
        """A completed run must have actually completed the whole DAG.

        Checking only that the nodes present are declared leaves the opposite
        hole open: a `run-finished(completed)` with no node facts at all, or
        with a whole branch missing, would still report success. A run may only
        claim completion when every declared node - and every child of every
        expanded Map - is durably completed.
        """

        for node_id in declared:
            state = self._nodes.get(node_id)
            if state is None or state.status is not NodeStatus.COMPLETED:
                raise WorkflowProtocolError(
                    "workflow-run-completion-incomplete", node_id=node_id
                )
        for node in definition.nodes:
            if node_kind(node) is not WorkflowNodeKind.MAP:
                continue
            expansion = self._expansions.get(node.node_id)
            if expansion is None:
                raise WorkflowProtocolError(
                    "workflow-run-completion-incomplete", node_id=node.node_id
                )
            for child in expansion.child_node_ids:
                child_state = self._nodes.get(child)
                if child_state is None or child_state.status is not NodeStatus.COMPLETED:
                    raise WorkflowProtocolError(
                        "workflow-run-completion-incomplete", node_id=child
                    )

    def _origin_of_child(self, node_id: str) -> tuple[str, str] | None:
        """Which Map and which key actually produced this child id.

        Returning the key as well as the parent is what lets a caller check that
        a child's recorded ``map_key`` is the one its identity was derived from,
        rather than any key the payload felt like naming.
        """

        for parent, expansion in self._expansions.items():
            for key, child_id in zip(
                expansion.map_keys, expansion.child_node_ids, strict=True
            ):
                if child_id == node_id:
                    return parent, key
        return None

    def _map_keys_for(self, node_id: str) -> tuple[str, ...]:
        expansion = self._expansions.get(node_id)
        return () if expansion is None else expansion.map_keys

    @classmethod
    def rebuild(
        cls, run_id: str, events: tuple[EventEnvelope, ...]
    ) -> WorkflowProjection:
        stream_id = workflow_stream_id(run_id)
        nodes: dict[str, _NodeState] = {}
        expansions: dict[str, MapExpansion] = {}
        child_keys: dict[str, str] = {}
        awaiting: list[str] = []
        definition_id = ""
        definition_hash = ""
        terminal: str | None = None
        failure_code: str | None = None
        expected_seq = 1
        for event in events:
            event_type, data, _, seq = workflow_event_header(event, stream_id)
            if seq != expected_seq:
                raise WorkflowProtocolError("workflow-sequence-invalid", seq)
            expected_seq += 1
            if set(data) != expected_keys(event_type):
                raise WorkflowProtocolError("workflow-payload-keys-unexpected", seq)
            if terminal is not None:
                raise WorkflowProtocolError("workflow-run-already-finished", seq)

            if event_type == WORKFLOW_RUN_STARTED:
                if seq != 1:
                    raise WorkflowProtocolError("workflow-run-start-unexpected", seq)
                if _text(data, "run_id", seq) != run_id:
                    raise WorkflowProtocolError("workflow-run-id-invalid", seq)
                definition_id = _text(data, "definition_id", seq)
                definition_hash = _digest(data, "definition_hash", seq)
                if _integer(data, "workflow_protocol_version", seq) != 1:
                    raise WorkflowProtocolError(
                        "workflow-protocol-version-unsupported", seq
                    )
                continue
            if seq == 1:
                raise WorkflowProtocolError("workflow-run-start-missing", seq)

            if event_type == WORKFLOW_NODE_STARTED:
                node_id = _text(data, "node_id", seq)
                if node_id in nodes:
                    raise WorkflowProtocolError("workflow-node-restarted", seq)
                map_key = _optional(data, "map_key", seq)
                # A key is only meaningful if an expansion already produced this
                # exact child id from that exact key. Anything else - a key on a
                # node nobody expanded, or a key that is not the one this id came
                # from - is refused here, without needing a definition.
                if map_key != child_keys.get(node_id):
                    raise WorkflowProtocolError("workflow-node-map-key-invalid", seq)
                nodes[node_id] = _NodeState(
                    node_id=node_id,
                    kind=_kind(data, seq),
                    map_key=map_key,
                    status=NodeStatus.RUNNING,
                )
            elif event_type == WORKFLOW_MAP_EXPANDED:
                node_id = _text(data, "node_id", seq)
                state = nodes.get(node_id)
                if state is None or state.status is not NodeStatus.RUNNING:
                    raise WorkflowProtocolError("workflow-map-expansion-unexpected", seq)
                # Only a running Map *parent* fans out. Without this, a Join
                # could record an expansion and carry map keys it never produced,
                # and the stream alone would never object.
                if state.kind is not WorkflowNodeKind.MAP or state.map_key is not None:
                    raise WorkflowProtocolError("workflow-map-expansion-unexpected", seq)
                if node_id in expansions:
                    raise WorkflowProtocolError("workflow-map-expansion-duplicate", seq)
                keys = _identifier_list(data, "map_keys", seq)
                children = _identifier_list(data, "child_node_ids", seq)
                if len(keys) != len(children) or len(set(keys)) != len(keys):
                    raise WorkflowProtocolError("workflow-map-expansion-invalid", seq)
                # Child identity is derived, never trusted from the payload.
                if tuple(map_child_node_id(node_id, key) for key in keys) != children:
                    raise WorkflowProtocolError("workflow-map-child-id-invalid", seq)
                for key, child_id in zip(keys, children, strict=True):
                    if child_id in child_keys or child_id in nodes:
                        # One child belongs to one expansion, and cannot appear
                        # before the fan-out that created it.
                        raise WorkflowProtocolError("workflow-map-child-duplicate", seq)
                    child_keys[child_id] = key
                expansions[node_id] = MapExpansion(node_id, keys, children)
            elif event_type in (WORKFLOW_NODE_COMPLETED, WORKFLOW_NODE_FAILED):
                node_id = _text(data, "node_id", seq)
                state = nodes.get(node_id)
                if state is None or state.status is not NodeStatus.RUNNING:
                    raise WorkflowProtocolError("workflow-node-terminal-unexpected", seq)
                # A terminal fact reports how a node ended; it cannot redefine
                # what the node was. Letting it carry its own kind would allow a
                # started AgentTask to finish as a Join holding a foreign
                # Artifact, and every later reader would believe it.
                if (
                    _kind(data, seq) is not state.kind
                    or _optional(data, "map_key", seq) != state.map_key
                ):
                    raise WorkflowProtocolError("workflow-node-terminal-mismatch", seq)
                completed = event_type == WORKFLOW_NODE_COMPLETED
                settled = _NodeState(
                    node_id=node_id,
                    kind=state.kind,
                    map_key=state.map_key,
                    status=NodeStatus.COMPLETED if completed else NodeStatus.FAILED,
                    agent_id=_optional(data, "agent_id", seq) if completed else None,
                    message_id=_optional(data, "message_id", seq) if completed else None,
                    artifact_id=(
                        _optional(data, "artifact_id", seq) if completed else None
                    ),
                    review_id=_optional(data, "review_id", seq) if completed else None,
                    approval_digest=(
                        _optional_digest(data, "approval_digest", seq)
                        if completed
                        else None
                    ),
                    failure_code=(
                        None if completed else _text(data, "failure_code", seq)
                    ),
                )
                if completed:
                    # Base layer: everything a stream alone can decide. The
                    # public Projector must fail closed on a malformed terminal
                    # even when no definition is supplied.
                    _require_evidence(run_id, node_id, settled, capture_artifact=None)
                nodes[node_id] = settled
                if node_id in awaiting:
                    awaiting.remove(node_id)
            elif event_type == WORKFLOW_APPROVAL_AWAITED:
                node_id = _text(data, "node_id", seq)
                state = nodes.get(node_id)
                if state is None or state.status is not NodeStatus.RUNNING:
                    raise WorkflowProtocolError("workflow-approval-wait-unexpected", seq)
                if node_id in awaiting:
                    raise WorkflowProtocolError("workflow-approval-wait-duplicate", seq)
                awaiting.append(node_id)
            else:
                status = _text(data, "status", seq)
                if status not in (NodeStatus.COMPLETED.value, NodeStatus.FAILED.value):
                    raise WorkflowProtocolError("workflow-run-status-invalid", seq)
                terminal = status
                failure_code = _optional(data, "failure_code", seq)
        if events and not definition_hash:
            raise WorkflowProtocolError("workflow-run-start-missing", expected_seq)
        return cls(
            run_id=run_id,
            definition_id=definition_id,
            definition_hash=definition_hash,
            nodes=nodes,
            expansions=expansions,
            awaiting=tuple(awaiting),
            terminal=terminal,
            failure_code=failure_code,
            head_seq=expected_seq - 1,
        )


def ready_nodes(
    definition: WorkflowDefinition, projection: WorkflowProjection
) -> tuple[tuple[WorkflowNode, str | None], ...]:
    """Nodes whose predecessors are all durably terminal and that have not run.

    A Map's successors wait for its children, not merely for the expansion, so
    the fan-out is genuinely joined before anything downstream starts. Ordering
    is by node id so two processes derive the same list from the same facts.
    """

    by_id = {node.node_id: node for node in definition.nodes}
    ready: list[tuple[WorkflowNode, str | None]] = []
    for node in sorted(definition.nodes, key=lambda item: item.node_id):
        if projection.status_of(node.node_id) is not NodeStatus.PENDING:
            continue
        if not all(
            _predecessor_settled(by_id, projection, predecessor)
            for predecessor in node.predecessors
        ):
            continue
        if any(
            projection.status_of(predecessor) is NodeStatus.FAILED
            or _map_children_failed(projection, predecessor)
            for predecessor in node.predecessors
        ):
            continue
        ready.append((node, None))

    for parent_id, expansion in sorted(projection.expansions.items()):
        parent = by_id.get(parent_id)
        if parent is None or node_kind(parent) is not WorkflowNodeKind.MAP:
            continue
        for key, child_id in zip(expansion.map_keys, expansion.child_node_ids, strict=True):
            if projection.status_of(child_id) is NodeStatus.PENDING:
                ready.append((parent, key))
    return tuple(ready)


def _predecessor_settled(
    by_id: dict[str, WorkflowNode],
    projection: WorkflowProjection,
    predecessor: str,
) -> bool:
    if not projection.is_terminal(predecessor):
        return False
    node = by_id.get(predecessor)
    if node is not None and node_kind(node) is WorkflowNodeKind.MAP:
        expansion = projection.expansions.get(predecessor)
        if expansion is None:
            return False
        return all(
            projection.is_terminal(child) for child in expansion.child_node_ids
        )
    return True


def _map_children_failed(projection: WorkflowProjection, predecessor: str) -> bool:
    expansion = projection.expansions.get(predecessor)
    if expansion is None:
        return False
    return any(
        projection.status_of(child) is NodeStatus.FAILED
        for child in expansion.child_node_ids
    )


class WorkflowStreamReader:
    __slots__ = ("_store",)

    def __init__(self, store: EventStore) -> None:
        self._store = store

    @property
    def store(self) -> EventStore:
        return self._store

    async def read_events(self, run_id: str) -> tuple[EventEnvelope, ...]:
        return await self._store.read(workflow_stream_id(run_id))

    async def load(self, run_id: str) -> WorkflowProjection:
        return WorkflowProjection.rebuild(run_id, await self.read_events(run_id))


def _text(data: dict[str, JsonValue], key: str, seq: int) -> str:
    from traceh.agents.identity import is_agent_identifier

    value = data.get(key)
    if not is_agent_identifier(value):
        raise WorkflowProtocolError(f"workflow-{key.replace('_', '-')}-invalid", seq)
    assert isinstance(value, str)
    return str(value)


def _optional(data: dict[str, JsonValue], key: str, seq: int) -> str | None:
    if data.get(key) is None:
        return None
    return _text(data, key, seq)


def _kind(data: dict[str, JsonValue], seq: int) -> WorkflowNodeKind:
    value = data.get("kind")
    try:
        return WorkflowNodeKind(value)  # type: ignore[arg-type]
    except ValueError:
        raise WorkflowProtocolError("workflow-node-kind-invalid", seq) from None


def _digest(data: dict[str, JsonValue], key: str, seq: int) -> str:
    value = data.get(key)
    if (
        type(value) is not str
        or len(value) != 64
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise WorkflowProtocolError(f"workflow-{key.replace('_', '-')}-invalid", seq)
    return value


def _optional_digest(data: dict[str, JsonValue], key: str, seq: int) -> str | None:
    if data.get(key) is None:
        return None
    return _digest(data, key, seq)


def _integer(data: dict[str, JsonValue], key: str, seq: int) -> int:
    value = data.get(key)
    if type(value) is not int:
        raise WorkflowProtocolError(f"workflow-{key.replace('_', '-')}-invalid", seq)
    return value


def _identifier_list(
    data: dict[str, JsonValue], key: str, seq: int
) -> tuple[str, ...]:
    from traceh.agents.identity import is_agent_identifier

    value = data.get(key)
    if type(value) is not list or not value:
        raise WorkflowProtocolError(f"workflow-{key.replace('_', '-')}-invalid", seq)
    for item in value:
        if not is_agent_identifier(item):
            raise WorkflowProtocolError(
                f"workflow-{key.replace('_', '-')}-invalid", seq
            )
    return tuple(str(item) for item in value)


def workflow_run_definition_hash(definition: WorkflowDefinition) -> str:
    return workflow_definition_hash(definition)


__all__ = [
    "MapExpansion",
    "WorkflowProjection",
    "WorkflowStreamReader",
    "ready_nodes",
    "workflow_run_definition_hash",
]




def _require_evidence(
    run_id: str,
    node_id: str,
    state: _NodeState,
    *,
    capture_artifact: bool | None,
) -> None:
    """Check one completion's evidence against what its node can produce.

    This is one rule used at two depths, not two rules. `rebuild()` calls it with
    ``capture_artifact=None`` because a stream alone cannot say whether a task
    was asked to capture; every other expectation follows from the node kind and
    is enforced there, so the public Projector still fails closed on a malformed
    terminal. `run(definition)` calls it again with the real setting, adding the
    one constraint only the definition can supply.
    """

    kind = state.kind
    is_map_child = state.map_key is not None
    if kind is WorkflowNodeKind.MAP and not is_map_child:
        # A Map parent completes on its expansion, never on work of its own.
        expected_agent = expected_message = None
        expects_artifact: bool | None = False
        expects_review = expects_approval = False
    elif kind in (WorkflowNodeKind.AGENT_TASK, WorkflowNodeKind.MAP):
        expected_agent, _, _, expected_message = agent_identity(run_id, node_id)
        expects_artifact = capture_artifact
        expects_review = expects_approval = False
    else:
        expected_agent = expected_message = None
        expects_artifact = kind is not WorkflowNodeKind.JOIN
        expects_review = kind is not WorkflowNodeKind.JOIN
        expects_approval = kind is WorkflowNodeKind.APPROVAL

    if state.agent_id != expected_agent or state.message_id != expected_message:
        raise WorkflowProtocolError(
            "workflow-node-result-identity-invalid", node_id=node_id
        )
    for present, expected in (
        (state.artifact_id is not None, expects_artifact),
        (state.review_id is not None, expects_review),
        (state.approval_digest is not None, expects_approval),
    ):
        if expected is None:  # undecidable without the definition
            continue
        if present != expected:
            raise WorkflowProtocolError(
                "workflow-node-result-incomplete"
                if expected
                else "workflow-node-result-unexpected",
                node_id=node_id,
            )
