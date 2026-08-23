"""Process-local Agent supervision: claims, Turns and delivery outcomes.

This layer sits *above* `AgentRuntime`, never inside it. `traceh.agents` keeps
the durable facts - identity, acceptance, and now the delivery lifecycle - and
this package turns them into work: claim the next accepted message, run it as a
real Turn on that Agent's own Session, record how it ended.

Four things are kept apart:

* **Identity** (`traceh.agents`) - which Agents exist;
* **Acceptance** (`traceh.agents`) - what arrived and in what order;
* **Delivery lifecycle** (here, durable) - what was claimed and how it ended;
* **Activation** (here, in memory) - the live worker and its exclusive
  execution runtime, which can be disposed and rebuilt without changing any of
  the three above.

v0.6 Stage D also projects lifecycle ownership from the durable Agent
Directory. Subtree disposal closes admission, waits admitted work to quiesce,
then releases descendants before owners. Stage E adds a host-wired five-Tool
facade over that same Supervisor and a durable report reader; it does not add a
parallel worker, scheduler or result cache. There is still **no** cold recovery,
stale-claim takeover, automatic retry, managed Workspace or budget enforcement;
`MessageTarget.NEXT_STEP` is refused rather than reinterpreted. See
[ADR-0021](../../../docs/adr/0021-process-local-agent-supervisor-and-delivery-lifecycle.md)
and
[ADR-0022](../../../docs/adr/0022-agent-lifecycle-ownership-and-quiescent-disposal.md),
plus
[ADR-0023](../../../docs/adr/0023-supervisor-backed-subagent-tools.md).
"""

from __future__ import annotations

from traceh.supervision.authority import (
    AgentToolAuthority,
    AgentToolAuthorizationError,
    AgentToolBindingError,
)
from traceh.supervision.delivery import (
    AgentDeliveryLog,
    AgentDeliveryReader,
    DeliveryIssue,
    MessageClaim,
    MessageOutcome,
    validate_agent_delivery_events,
)
from traceh.supervision.delivery_identity import (
    AGENT_DELIVERY_SCHEMA_VERSION,
    AGENT_DELIVERY_STREAM_PREFIX,
    AGENT_MESSAGE_CANCELLED,
    AGENT_MESSAGE_CLAIMED,
    AGENT_MESSAGE_COMPLETED,
    AGENT_MESSAGE_FAILED,
    agent_delivery_stream,
    cancelled_data,
    claimed_data,
    completed_data,
    failed_data,
    parse_delivery_event,
)
from traceh.supervision.delivery_service import AgentDeliveryService
from traceh.supervision.errors import (
    ActivationConflictError,
    ActivationFaultedError,
    AgentMessageNotFoundError,
    AgentMessageNotSettledError,
    AgentNotActiveError,
    AgentOwnerNotActiveError,
    AgentRunEvidenceError,
    DeliveryAppendError,
    DeliveryConflictError,
    DeliveryInputError,
    DeliveryProtocolError,
    ExecutionSessionMismatchError,
    ExecutionStoreMismatchError,
    MessageWakeError,
    SupervisionError,
    SupervisorDisposedError,
    UnsupportedMessageTargetError,
)
from traceh.supervision.execution import (
    AgentActivationFactory,
    AgentExecution,
    AgentRuntimeExecution,
)
from traceh.supervision.lifecycle import (
    AgentLifecycleCoordinator,
    AgentOwnershipGraph,
    AgentOwnershipGraphError,
)
from traceh.supervision.provisioning import (
    ChildProvisioningPolicy,
    ChildProvisioningProposal,
)
from traceh.supervision.reports import AgentRunReportReader
from traceh.supervision.supervisor import (
    AgentNotFoundError,
    ProcessAgentSupervisor,
    SupervisedAgentHandle,
)
from traceh.supervision.tools import (
    CollectAgentArtifactTool,
    SendAgentMessageTool,
    SpawnAgentTool,
    StopAgentTool,
    SupervisorToolset,
    WaitAgentTool,
)

__all__ = [
    "AGENT_DELIVERY_SCHEMA_VERSION",
    "AGENT_DELIVERY_STREAM_PREFIX",
    "AGENT_MESSAGE_CANCELLED",
    "AGENT_MESSAGE_CLAIMED",
    "AGENT_MESSAGE_COMPLETED",
    "AGENT_MESSAGE_FAILED",
    "ActivationConflictError",
    "ActivationFaultedError",
    "AgentActivationFactory",
    "AgentDeliveryLog",
    "AgentDeliveryReader",
    "AgentDeliveryService",
    "AgentExecution",
    "AgentNotActiveError",
    "AgentMessageNotFoundError",
    "AgentMessageNotSettledError",
    "AgentOwnerNotActiveError",
    "AgentRunEvidenceError",
    "AgentRunReportReader",
    "AgentToolAuthority",
    "AgentToolAuthorizationError",
    "AgentToolBindingError",
    "AgentLifecycleCoordinator",
    "AgentOwnershipGraph",
    "AgentOwnershipGraphError",
    "AgentNotFoundError",
    "AgentRuntimeExecution",
    "DeliveryAppendError",
    "DeliveryConflictError",
    "DeliveryInputError",
    "DeliveryIssue",
    "DeliveryProtocolError",
    "ExecutionSessionMismatchError",
    "ExecutionStoreMismatchError",
    "MessageClaim",
    "MessageOutcome",
    "MessageWakeError",
    "CollectAgentArtifactTool",
    "ChildProvisioningPolicy",
    "ChildProvisioningProposal",
    "ProcessAgentSupervisor",
    "SendAgentMessageTool",
    "SpawnAgentTool",
    "StopAgentTool",
    "SupervisedAgentHandle",
    "SupervisorToolset",
    "SupervisionError",
    "SupervisorDisposedError",
    "UnsupportedMessageTargetError",
    "WaitAgentTool",
    "agent_delivery_stream",
    "cancelled_data",
    "claimed_data",
    "completed_data",
    "failed_data",
    "parse_delivery_event",
    "validate_agent_delivery_events",
]
