"""Durable Agent identity: the multi-agent control plane's fact layer.

This package owns one question - *which Agents exist, and which Session does
each one own* - and answers it from the event log alone. It is deliberately
separate from `traceh.runtime`:

* `AgentLoop` executes one Turn and knows nothing about this package;
* `AgentRuntime` is an **Activation** - a live, in-process object that can be
  built, stopped and built again. It is not an identity, and this package never
  holds one;
* `traceh.supervision` holds Activations through a narrow interface and uses
  this package for identity. The dependency only points that way - nothing here
  imports it, and nothing here holds a runtime, a Task or a live Activation.

Two fact layers live here, and both are only facts:

* **Stage A** - identity and the creation transaction: which Agents exist and
  which Session each one owns
  ([ADR-0019](../../../docs/adr/0019-durable-agent-identity-and-activation-boundary.md));
* **Stage B** - a durable per-Agent FIFO Inbox of **accepted** messages
  ([ADR-0020](../../../docs/adr/0020-durable-agent-inbox-acceptance.md)).

**Accepted is not processed - not by this package.** These streams record that a
message was received and where it sits in that Agent's order. Claiming,
execution and outcome are `traceh.supervision`'s facts on a separate delivery
stream
([ADR-0021](../../../docs/adr/0021-process-local-agent-supervisor-and-delivery-lifecycle.md));
here `wakeup` is still only a sender's request and `owner_agent_id` still records
lifecycle responsibility alone. Stage D's `traceh.supervision.lifecycle`
projects that field for process-local child-first disposal without moving the
fact or cleanup work into this package. Stage E's model-visible Tool facade also
lives in `traceh.supervision`; this package remains only the durable fact layer.
There is still no cold recovery, retry policy, managed Workspace or hierarchical
budget enforcement.
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
    AgentInboxConflictError,
    AgentInboxProtocolError,
    AgentMessageAcceptError,
    AgentMessageConflictError,
    AgentMessageError,
    AgentOwnerNotFoundError,
    AgentRequestConflictError,
    AgentSessionConflictError,
    AgentUnknownError,
)
from traceh.agents.identity import (
    AGENT_CREATED,
    AGENT_DIRECTORY_STREAM,
    agent_created_data,
    is_agent_identifier,
    parse_agent_created,
)
from traceh.agents.inbox import (
    AgentInbox,
    AgentInboxIssue,
    AgentInboxReader,
    validate_agent_inbox_events,
)
from traceh.agents.inbox_identity import (
    AGENT_INBOX_SCHEMA_VERSION,
    AGENT_INBOX_STREAM_PREFIX,
    AGENT_MESSAGE_ACCEPTED,
    agent_inbox_stream,
    is_message_content,
    message_accepted_data,
    parse_message_accepted,
)
from traceh.agents.inbox_service import AgentInboxService
from traceh.agents.registrar import AgentRegistrar

__all__ = [
    "AGENT_CREATED",
    "AGENT_DIRECTORY_STREAM",
    "AGENT_INBOX_SCHEMA_VERSION",
    "AGENT_INBOX_STREAM_PREFIX",
    "AGENT_MESSAGE_ACCEPTED",
    "AgentControlPlaneError",
    "AgentCreationError",
    "AgentDirectory",
    "AgentDirectoryConflictError",
    "AgentDirectoryIssue",
    "AgentDirectoryProtocolError",
    "AgentDirectoryReader",
    "AgentIdentityConflictError",
    "AgentIdentityError",
    "AgentInbox",
    "AgentInboxConflictError",
    "AgentInboxIssue",
    "AgentInboxProtocolError",
    "AgentInboxReader",
    "AgentInboxService",
    "AgentMessageAcceptError",
    "AgentMessageConflictError",
    "AgentMessageError",
    "AgentOwnerNotFoundError",
    "AgentRegistrar",
    "AgentRequestConflictError",
    "AgentSessionConflictError",
    "AgentUnknownError",
    "agent_created_data",
    "agent_inbox_stream",
    "is_agent_identifier",
    "is_message_content",
    "message_accepted_data",
    "parse_agent_created",
    "parse_message_accepted",
    "validate_agent_directory_events",
    "validate_agent_inbox_events",
]
