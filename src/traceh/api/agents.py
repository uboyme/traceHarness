"""Multi-agent control-plane values and protocols.

Two different things live here and must not be confused.

**Durable identity.** `AgentRecord` is a fact rebuilt from the event log by
:mod:`traceh.agents`: this Agent exists and owns this Session. v0.6 Stage A
implements it, so it is no longer forward-looking.

**Activation.** `AgentHandle` and `AgentSupervisor` describe a *live*, in-process
Agent - something that can be created, stopped and created again.  The shipped
`traceh.supervision.ProcessAgentSupervisor` implements this boundary through a
durable Inbox and delivery lifecycle while keeping scheduling out of the
single-Agent Runtime.

An `AgentHandle` is therefore never an identity. Stopping or rebuilding one
cannot change an `AgentRecord`, and losing every handle in a process does not
remove an Agent from the durable directory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from traceh.api.json_types import JsonValue
from traceh.session.event_store import EventStore


class MessageTarget(str, Enum):
    NEW_TURN = "new_turn"
    NEXT_STEP = "next_step"


@dataclass(frozen=True, slots=True)
class AgentMessage:
    message_id: str
    content: str
    source: str
    correlation_id: str | None = None
    causation_id: str | None = None


@dataclass(frozen=True, slots=True)
class AgentSpec:
    preset: str
    workspace_id: str
    owner_agent_id: str | None = None
    forked_from_session_id: str | None = None
    capability_grants: tuple[str, ...] = ()
    metadata: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentRecord:
    """One durable Agent identity, as rebuilt from ``agent/created``.

    This is what a fresh process reading only an `EventStore` recovers. It
    holds no Runtime, Task or Handle, which is precisely why an Activation can
    stop and restart without changing it.

    Three relations are recorded separately and mean different things:

    * ``session_id`` - the Session this Agent owns. Exactly one Agent owns a
      Session;
    * ``forked_from_session_id`` - *history lineage*: which Session this
      Agent's starting context came from. It confers no authority;
    * ``owner_agent_id`` - *lifecycle ownership*: which Agent is responsible
      for disposing this one. It is not a lineage claim and not a message
      route.

    Communication has no field here at all: a message's source is a per-message
    fact, so it cannot be inferred from a creation record.

    Budget is deliberately absent. Capacity is host authority reconstructed
    from the independent append-only Budget ledger; an Agent creation DTO
    cannot mint or rewrite it.
    """

    agent_id: str
    session_id: str
    request_id: str
    preset: str
    workspace_id: str
    owner_agent_id: str | None
    forked_from_session_id: str | None
    capability_grants: tuple[str, ...]
    metadata: dict[str, JsonValue]
    created_seq: int


@dataclass(frozen=True, slots=True)
class MessageReceipt:
    message_id: str
    agent_id: str
    accepted_seq: int


@dataclass(frozen=True, slots=True)
class AcceptedMessage:
    """One message durably accepted into an Agent's Inbox.

    **Accepted is not processed.** This records that the message was received
    and where it sits in that Agent's FIFO order. It does not mean the message
    was delivered to an Activation, claimed, executed, completed or failed:
    those are recorded separately, on a delivery stream, and no field here
    should ever be read as one of them.

    Every field is an immutable scalar, so a projector may hand the same object
    to two callers without either being able to write through it. That is a
    property of the current message shape, not a permanent guarantee: adding a
    mutable content block or attachment list would reintroduce shared state and
    this boundary would then owe each caller its own copy.
    """

    agent_id: str
    message: AgentMessage
    target: MessageTarget
    wakeup: bool
    accepted_seq: int

    def receipt(self) -> MessageReceipt:
        """The receipt replay reconstructs for this acceptance."""

        return MessageReceipt(
            message_id=self.message.message_id,
            agent_id=self.agent_id,
            accepted_seq=self.accepted_seq,
        )


@dataclass(frozen=True, slots=True)
class AgentRunReport:
    agent_id: str
    session_id: str
    reason: str
    final_text: str
    artifact_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    status: str = "completed"
    message_id: str | None = None
    turn_id: str | None = None


class AgentHandle(Protocol):
    agent_id: str
    session_id: str


@runtime_checkable
class AgentSupervisor(Protocol):
    """The public contract implemented by the process-local Supervisor."""

    @property
    def store(self) -> EventStore:
        """The durable Event Store used by every control-plane operation.

        This is an identity-validation surface for host adapters, not an
        invitation to bypass the Supervisor and append control facts directly.
        """
        ...

    async def create(
        self,
        spec: AgentSpec,
        *,
        request_id: str,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> AgentHandle:
        ...

    async def resume(self, session_id: str) -> AgentHandle:
        ...

    async def send(
        self,
        agent_id: str,
        message: AgentMessage,
        *,
        target: MessageTarget,
        wakeup: bool,
    ) -> MessageReceipt:
        ...

    async def interrupt(self, agent_id: str, reason: str = "interrupted") -> bool:
        ...

    async def wait_idle(self, agent_id: str) -> None:
        ...

    async def wait_message(self, agent_id: str, message_id: str) -> AgentRunReport:
        ...

    async def report(self, agent_id: str, message_id: str) -> AgentRunReport:
        ...

    async def dispose(self, agent_id: str) -> None:
        ...

    async def aclose(self) -> None:
        ...
