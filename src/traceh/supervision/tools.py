"""Model-visible subagent tools backed by the public `AgentSupervisor` seam.

This is an adapter above the control plane, not a second scheduler. Every tool
delegates to the same Supervisor methods a host caller uses, and every durable
answer is replayed from the Event Store. `AgentLoop` and `AgentRuntime` remain
unaware that one Tool may operate another Agent.

Authority is bound by the host when the tools are assembled:

* the model never supplies ``owner_agent_id``;
* the Tool execution Session must be the durable Session of that owner;
* send/wait/stop/collect may address only an owned descendant;
* the Runtime and Supervisor must share the same durable Event Store object.

The Stage E artifact is an `AgentRunReport` (final text plus durable evidence
references), not a capture command. v0.7-D1 may decorate that report with
already-recorded immutable Patch references; this Tool remains read-only and
never performs Workspace, Git or CAS mutation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import NAMESPACE_URL, uuid5

from traceh.agents import AgentDirectoryReader
from traceh.agents.identity import freeze_agent_spec, require_identifier
from traceh.agents.inbox_identity import is_message_content
from traceh.api.agents import (
    AgentMessage,
    AgentRunReport,
    AgentSpec,
    AgentSupervisor,
    MessageTarget,
)
from traceh.api.json_types import JsonValue
from traceh.api.tools import EffectKind, Tool, ToolExecutionContext, ToolOutput
from traceh.session.event_store import EventStore
from traceh.supervision.authority import (
    AgentToolAuthority,
    AgentToolAuthorizationError,
    AgentToolBindingError,
)
from traceh.supervision.execution import durable_log_identity
from traceh.supervision.provisioning import (
    ChildProvisioningPolicy,
    ChildProvisioningProposal,
)


def _operation_id(kind: str, owner_agent_id: str, context: ToolExecutionContext) -> str:
    value = "\x1f".join(
        (
            kind,
            owner_agent_id,
            context.session_id,
            context.turn_id,
            context.step_id,
            context.tool_call_id,
        )
    )
    return f"agent-tool-{kind}-{uuid5(NAMESPACE_URL, value)}"


def _required_string(arguments: dict[str, JsonValue], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


class _BoundAgentControl:
    __slots__ = ("authority", "provisioning_policy", "supervisor")

    def __init__(
        self,
        *,
        supervisor: AgentSupervisor,
        authority: AgentToolAuthority,
        provisioning_policy: ChildProvisioningPolicy,
    ) -> None:
        if durable_log_identity(supervisor.store) is not durable_log_identity(
            authority.store
        ):
            raise AgentToolBindingError(
                "the subagent tools and their Runtime use different event stores"
            )
        self.supervisor = supervisor
        self.authority = authority
        self.provisioning_policy = provisioning_policy

    @property
    def owner_agent_id(self) -> str:
        return self.authority.owner_agent_id

    async def _require_caller(self, context: ToolExecutionContext) -> None:
        await self.authority.require_caller(context.session_id)

    async def _require_owned(
        self, target_agent_id: str, context: ToolExecutionContext
    ) -> None:
        await self.authority.require_owned(target_agent_id, context.session_id)

    async def spawn(
        self, *, preset: str, workspace_id: str, context: ToolExecutionContext
    ):
        owner = await self.authority.require_caller(context.session_id)
        requested_preset = require_identifier(preset, field="preset")
        requested_workspace_id = require_identifier(
            workspace_id, field="workspace_id"
        )
        proposal = self.provisioning_policy.propose_child(
            owner=owner,
            requested_preset=requested_preset,
            requested_workspace_id=requested_workspace_id,
        )
        if not isinstance(proposal, ChildProvisioningProposal):
            raise AgentToolBindingError(
                "the child provisioning policy returned an invalid proposal"
            )
        spec = freeze_agent_spec(
            AgentSpec(
                preset=require_identifier(proposal.preset, field="preset"),
                workspace_id=require_identifier(
                    proposal.workspace_id, field="workspace_id"
                ),
                owner_agent_id=self.owner_agent_id,
                metadata=proposal.metadata,
            )
        )
        request_id = _operation_id("spawn", self.owner_agent_id, context)
        # The Supervisor single-flight owns create delivery and compensation.
        # It can atomically distinguish an unreturned child from a concurrent
        # caller's delivered handle; a Tool-side directory snapshot cannot.
        return await self.supervisor.create(spec, request_id=request_id)

    async def send(
        self,
        *,
        agent_id: str,
        content: str,
        context: ToolExecutionContext,
    ):
        await self._require_owned(agent_id, context)
        if not is_message_content(content):
            raise ValueError("content is not a persistable Agent message")
        message_id = _operation_id("message", self.owner_agent_id, context)
        return await self.supervisor.send(
            agent_id,
            AgentMessage(
                message_id=message_id,
                content=content,
                source=self.owner_agent_id,
                correlation_id=context.turn_id,
                causation_id=context.tool_call_id,
            ),
            target=MessageTarget.NEW_TURN,
            wakeup=True,
        )

    async def wait(
        self,
        *,
        agent_id: str,
        message_id: str,
        context: ToolExecutionContext,
    ) -> AgentRunReport:
        await self._require_owned(agent_id, context)
        return await self.supervisor.wait_message(agent_id, message_id)

    async def stop(self, *, agent_id: str, context: ToolExecutionContext) -> None:
        await self._require_owned(agent_id, context)
        await self.supervisor.dispose(agent_id)

    async def collect(
        self,
        *,
        agent_id: str,
        message_id: str,
        context: ToolExecutionContext,
    ) -> AgentRunReport:
        await self._require_owned(agent_id, context)
        return await self.supervisor.report(agent_id, message_id)


def _report_data(report: AgentRunReport) -> dict[str, JsonValue]:
    return {
        "agent_id": report.agent_id,
        "session_id": report.session_id,
        "message_id": report.message_id,
        "turn_id": report.turn_id,
        "status": report.status,
        "reason": report.reason,
        "artifact_refs": list(report.artifact_refs),
        "evidence_refs": list(report.evidence_refs),
    }


@dataclass(slots=True)
class SpawnAgentTool:
    control: _BoundAgentControl
    name: str = "spawn_agent"
    description: str = (
        "Create an owned child Agent with its own durable identity and Session. "
        "Preset and workspace identifiers are resolved by the host."
    )
    effect_kind: EffectKind = EffectKind.EXTERNAL_TRANSACTION
    input_schema: dict[str, JsonValue] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.input_schema = {
            "type": "object",
            "properties": {
                "preset": {"type": "string"},
                "workspace_id": {"type": "string"},
            },
            "required": ["preset", "workspace_id"],
            "additionalProperties": False,
        }

    async def execute(self, arguments, context: ToolExecutionContext) -> ToolOutput:
        handle = await self.control.spawn(
            preset=_required_string(arguments, "preset"),
            workspace_id=_required_string(arguments, "workspace_id"),
            context=context,
        )
        return ToolOutput(
            content=f"Created child Agent {handle.agent_id}.",
            data={
                "agent_id": handle.agent_id,
                "session_id": handle.session_id,
                "owner_agent_id": self.control.owner_agent_id,
            },
            evidence=(f"agent:{handle.agent_id}",),
        )


@dataclass(slots=True)
class SendAgentMessageTool:
    control: _BoundAgentControl
    name: str = "send_agent_message"
    description: str = "Send one durable FIFO message to an owned child and wake it."
    effect_kind: EffectKind = EffectKind.EXTERNAL_TRANSACTION
    input_schema: dict[str, JsonValue] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.input_schema = {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["agent_id", "content"],
            "additionalProperties": False,
        }

    async def execute(self, arguments, context: ToolExecutionContext) -> ToolOutput:
        receipt = await self.control.send(
            agent_id=_required_string(arguments, "agent_id"),
            content=_required_string(arguments, "content"),
            context=context,
        )
        return ToolOutput(
            content=f"Message {receipt.message_id} was durably accepted.",
            data={
                "agent_id": receipt.agent_id,
                "message_id": receipt.message_id,
                "accepted_seq": receipt.accepted_seq,
            },
            evidence=(f"agent-inbox:{receipt.agent_id}#{receipt.accepted_seq}",),
        )


@dataclass(slots=True)
class WaitAgentTool:
    control: _BoundAgentControl
    name: str = "wait_agent"
    description: str = (
        "Wait for one already-sent child message to settle. Cancelling this wait "
        "does not cancel the child."
    )
    effect_kind: EffectKind = EffectKind.PURE_READ
    input_schema: dict[str, JsonValue] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.input_schema = _agent_message_schema()

    async def execute(self, arguments, context: ToolExecutionContext) -> ToolOutput:
        report = await self.control.wait(
            agent_id=_required_string(arguments, "agent_id"),
            message_id=_required_string(arguments, "message_id"),
            context=context,
        )
        return ToolOutput(
            content=(
                f"Agent message settled with status={report.status}, "
                f"reason={report.reason}."
            ),
            data=_report_data(report),
            evidence=report.evidence_refs,
        )


@dataclass(slots=True)
class StopAgentTool:
    control: _BoundAgentControl
    name: str = "stop_agent"
    description: str = (
        "Stop an owned Agent subtree child-first. Durable identity and history remain resumable."
    )
    effect_kind: EffectKind = EffectKind.EXTERNAL_TRANSACTION
    input_schema: dict[str, JsonValue] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.input_schema = {
            "type": "object",
            "properties": {"agent_id": {"type": "string"}},
            "required": ["agent_id"],
            "additionalProperties": False,
        }

    async def execute(self, arguments, context: ToolExecutionContext) -> ToolOutput:
        agent_id = _required_string(arguments, "agent_id")
        await self.control.stop(agent_id=agent_id, context=context)
        return ToolOutput(
            content=f"Stopped Agent subtree {agent_id}.",
            data={"agent_id": agent_id, "stopped": True},
        )


@dataclass(slots=True)
class CollectAgentArtifactTool:
    control: _BoundAgentControl
    name: str = "collect_agent_artifact"
    description: str = (
        "Collect a settled child message's durable run report, final text, and "
        "any already-recorded artifact references. This read never captures or "
        "modifies a workspace."
    )
    effect_kind: EffectKind = EffectKind.PURE_READ
    input_schema: dict[str, JsonValue] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.input_schema = _agent_message_schema()

    async def execute(self, arguments, context: ToolExecutionContext) -> ToolOutput:
        report = await self.control.collect(
            agent_id=_required_string(arguments, "agent_id"),
            message_id=_required_string(arguments, "message_id"),
            context=context,
        )
        content = report.final_text or (
            f"The child produced no final text (status={report.status}, reason={report.reason})."
        )
        return ToolOutput(
            content=content,
            data=_report_data(report),
            evidence=report.evidence_refs,
        )


def _agent_message_schema() -> dict[str, JsonValue]:
    return {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string"},
            "message_id": {"type": "string"},
        },
        "required": ["agent_id", "message_id"],
        "additionalProperties": False,
    }


class SupervisorToolset:
    """One host-bound set of the five Stage E subagent tools."""

    __slots__ = ("_control", "_tools")

    def __init__(
        self,
        *,
        supervisor: AgentSupervisor,
        owner_agent_id: str,
        event_store: EventStore,
        provisioning_policy: ChildProvisioningPolicy,
    ) -> None:
        authority = AgentToolAuthority(
            directory_reader=AgentDirectoryReader(event_store),
            owner_agent_id=owner_agent_id,
        )
        self._control = _BoundAgentControl(
            supervisor=supervisor,
            authority=authority,
            provisioning_policy=provisioning_policy,
        )
        self._tools: tuple[Tool, ...] = (
            SpawnAgentTool(self._control),
            SendAgentMessageTool(self._control),
            WaitAgentTool(self._control),
            StopAgentTool(self._control),
            CollectAgentArtifactTool(self._control),
        )

    @property
    def tools(self) -> tuple[Tool, ...]:
        return self._tools


__all__ = [
    "AgentToolAuthority",
    "AgentToolAuthorizationError",
    "AgentToolBindingError",
    "CollectAgentArtifactTool",
    "SendAgentMessageTool",
    "SpawnAgentTool",
    "StopAgentTool",
    "SupervisorToolset",
    "WaitAgentTool",
]
