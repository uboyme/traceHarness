"""Public immutable values and host seams for the typed Workflow.

This is a **fixed** typed DAG, not a general workflow language. There are five
node kinds and no expression, condition, loop or user-supplied callable. A
definition therefore says only *what* to compose; every decision about which
Agent, which prompt and which keys to fan out over is resolved at run time by a
host-owned resolver, so the durable definition never carries a repository path,
a secret, a command environment or a Python object.

Nothing here schedules anything. Agent FIFO order, Turn execution and Activation
lifetime remain owned by the existing Supervisor; a Workflow only records which
orchestration steps have happened and calls the public services in order.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from traceh.api.agents import AgentSpec


class WorkflowNodeKind(StrEnum):
    """The five node kinds this stage supports, and no others."""

    AGENT_TASK = "agent_task"
    MAP = "map"
    JOIN = "join"
    VERIFICATION = "verification"
    APPROVAL = "approval"


class WorkflowStatus(StrEnum):
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"


class NodeStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AgentTaskNode:
    """Run one message on one managed Agent and take its durable report.

    ``spec_binding`` and ``message_binding`` are host registry keys, never the
    values themselves: a durable definition must not carry a prompt, a
    workspace path or anything else a later reader would have to trust.
    """

    node_id: str
    predecessors: tuple[str, ...]
    spec_binding: str
    message_binding: str
    capture_artifact: bool = False

    @property
    def kind(self) -> WorkflowNodeKind:
        return WorkflowNodeKind.AGENT_TASK


@dataclass(frozen=True, slots=True)
class MapNode:
    """Fan one bounded, host-resolved key set out into sibling Agent tasks."""

    node_id: str
    predecessors: tuple[str, ...]
    keys_binding: str
    child_spec_binding: str
    child_message_binding: str
    max_fan_out: int
    capture_artifact: bool = False

    @property
    def kind(self) -> WorkflowNodeKind:
        return WorkflowNodeKind.MAP


@dataclass(frozen=True, slots=True)
class JoinNode:
    """Wait for every predecessor's durable terminal fact."""

    node_id: str
    predecessors: tuple[str, ...]

    @property
    def kind(self) -> WorkflowNodeKind:
        return WorkflowNodeKind.JOIN


@dataclass(frozen=True, slots=True)
class VerificationNode:
    """Review one already captured Patch Artifact against one host target."""

    node_id: str
    predecessors: tuple[str, ...]
    artifact_node_id: str
    target_id: str

    @property
    def kind(self) -> WorkflowNodeKind:
        return WorkflowNodeKind.VERIFICATION


@dataclass(frozen=True, slots=True)
class ApprovalNode:
    """A human barrier.

    The Workflow never approves anything. It records that it is waiting and
    stops; a person records the approval through the promotion service, and a
    later run reads that exact durable fact.
    """

    node_id: str
    predecessors: tuple[str, ...]
    review_node_id: str

    @property
    def kind(self) -> WorkflowNodeKind:
        return WorkflowNodeKind.APPROVAL


type WorkflowNode = (
    AgentTaskNode | MapNode | JoinNode | VerificationNode | ApprovalNode
)


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    """One fixed DAG. Its hash is what a run binds to, not its object identity."""

    definition_id: str
    nodes: tuple[WorkflowNode, ...]


@dataclass(frozen=True, slots=True)
class NodeOutcome:
    """What one node durably produced.

    Every field is an identity another domain owns, never a copy of that
    domain's state: the Agent report, the Patch bytes, the Review evidence and
    the Approval all stay in their own fact sources.
    """

    node_id: str
    kind: WorkflowNodeKind
    status: NodeStatus
    agent_id: str | None = None
    message_id: str | None = None
    artifact_id: str | None = None
    review_id: str | None = None
    approval_digest: str | None = None
    map_keys: tuple[str, ...] = ()
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class WorkflowRun:
    """The whole orchestration state, rebuilt from the run's own stream."""

    run_id: str
    definition_id: str
    definition_hash: str
    status: WorkflowStatus
    outcomes: tuple[NodeOutcome, ...]
    awaiting_approval: tuple[str, ...]
    failure_code: str | None
    head_seq: int

    def outcome(self, node_id: str) -> NodeOutcome | None:
        for item in self.outcomes:
            if item.node_id == node_id:
                return item
        return None


class WorkflowBindingResolver(Protocol):
    """Host seam turning a definition's binding id into a concrete value.

    Keeping this out of the definition is what lets the durable record stay free
    of prompts, paths and policy while the host still decides everything that
    matters.
    """

    async def agent_spec(
        self, binding_id: str, *, run_id: str, node_id: str, map_key: str | None
    ) -> AgentSpec:
        ...

    async def message_content(
        self, binding_id: str, *, run_id: str, node_id: str, map_key: str | None
    ) -> str:
        ...

    async def map_keys(
        self, binding_id: str, *, run_id: str, node_id: str
    ) -> tuple[str, ...]:
        ...


__all__ = [
    "AgentTaskNode",
    "ApprovalNode",
    "JoinNode",
    "MapNode",
    "NodeOutcome",
    "NodeStatus",
    "VerificationNode",
    "WorkflowBindingResolver",
    "WorkflowDefinition",
    "WorkflowNode",
    "WorkflowNodeKind",
    "WorkflowRun",
    "WorkflowStatus",
]
