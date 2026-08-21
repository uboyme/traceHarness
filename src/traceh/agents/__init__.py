"""Durable Agent identity: the multi-agent control plane's fact layer.

This package owns one question - *which Agents exist, and which Session does
each one own* - and answers it from the event log alone. It is deliberately
separate from `traceh.runtime`:

* `AgentLoop` executes one Turn and knows nothing about this package;
* `AgentRuntime` is an **Activation** - a live, in-process object that can be
  built, stopped and built again. It is not an identity, and this package never
  holds one;
* a future `AgentSupervisor` will hold Activations through a narrow interface
  and use this package for identity. The dependency only points that way.

Stage A of v0.6 implements identity and the creation transaction. There is no
Supervisor, no Inbox, no message delivery, no subagent tool and no parent/child
disposal here; `owner_agent_id` records lifecycle responsibility only. See
[ADR-0019](../../../docs/adr/0019-durable-agent-identity-and-activation-boundary.md).
"""

from __future__ import annotations

from traceh.agents.directory import (
    AgentDirectory,
    AgentDirectoryIssue,
    AgentDirectoryReader,
    validate_agent_directory_events,
)
from traceh.agents.errors import (
    AgentControlPlaneError,
    AgentCreationError,
    AgentDirectoryConflictError,
    AgentDirectoryProtocolError,
    AgentIdentityConflictError,
    AgentIdentityError,
    AgentOwnerNotFoundError,
    AgentRequestConflictError,
    AgentSessionConflictError,
)
from traceh.agents.identity import (
    AGENT_CREATED,
    AGENT_DIRECTORY_STREAM,
    agent_created_data,
    is_agent_identifier,
    parse_agent_created,
)
from traceh.agents.registrar import AgentRegistrar

__all__ = [
    "AGENT_CREATED",
    "AGENT_DIRECTORY_STREAM",
    "AgentControlPlaneError",
    "AgentCreationError",
    "AgentDirectory",
    "AgentDirectoryConflictError",
    "AgentDirectoryIssue",
    "AgentDirectoryProtocolError",
    "AgentDirectoryReader",
    "AgentIdentityConflictError",
    "AgentIdentityError",
    "AgentOwnerNotFoundError",
    "AgentRegistrar",
    "AgentRequestConflictError",
    "AgentSessionConflictError",
    "agent_created_data",
    "is_agent_identifier",
    "parse_agent_created",
    "validate_agent_directory_events",
]
