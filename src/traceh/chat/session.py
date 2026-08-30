"""UI-neutral opening and recovery for one interactive Chat Session.

Both terminal adapters must cross the same durable boundary before accepting
input.  This module creates a new Session or verifies and recovers an existing
one; it deliberately renders nothing and keeps no conversation state.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from traceh.runtime.agent_runtime import AgentRuntime
from traceh.session.recovery import RecoveryReport


@dataclass(frozen=True, slots=True)
class ChatSession:
    session_id: str
    workspace: Path


@dataclass(frozen=True, slots=True)
class OpenedChatSession:
    session: ChatSession
    recovery: RecoveryReport | None

    @property
    def created(self) -> bool:
        return self.recovery is None


async def open_chat_session(
    runtime: AgentRuntime,
    *,
    workspace: Path | None,
    session_id: str | None,
) -> OpenedChatSession:
    """Create or safely resume exactly one Session.

    Callers validate the mutually-exclusive CLI arguments.  A resumed Session
    verifies its frozen plugin composition before recovery is allowed to append
    any closing facts.
    """

    if session_id is None:
        if workspace is None:
            raise ValueError("chat-session-target-missing")
        created = await runtime.create_session(workspace, metadata={"cli": "chat"})
        persisted_workspace = await runtime.sessions.workspace_for(created)
        return OpenedChatSession(ChatSession(created, persisted_workspace), None)

    if workspace is not None:
        raise ValueError("chat-session-target-ambiguous")
    persisted_workspace = await runtime.sessions.workspace_for(session_id)
    session = ChatSession(session_id, persisted_workspace)
    await runtime.verify_session_plugins(session_id)
    recovery = await runtime.recovery.recover(session_id)
    return OpenedChatSession(session, recovery)


__all__ = ["ChatSession", "OpenedChatSession", "open_chat_session"]
