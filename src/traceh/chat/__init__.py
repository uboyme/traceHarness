"""UI-neutral interactive Chat primitives.

The package owns no terminal, widget or product authority.  It turns durable
Session events plus a monotonic clock into typed updates that an adapter may
render, and drives the existing :class:`AgentRuntime` without keeping a second
conversation history.
"""

from traceh.chat.activity import (
    DEFAULT_HEARTBEAT_SECONDS,
    ActivityKind,
    ActivityPhase,
    ActivityTracker,
    ActivityUpdate,
    Clock,
    default_clock,
)
from traceh.chat.driver import (
    ChatDriver,
    ChatTurnOutcome,
    ChatUpdate,
    SessionEventUpdate,
    TurnCompletedUpdate,
    TurnFailedUpdate,
    TurnInterruptedUpdate,
)
from traceh.chat.session import ChatSession, OpenedChatSession, open_chat_session

__all__ = [
    "DEFAULT_HEARTBEAT_SECONDS",
    "ActivityKind",
    "ActivityPhase",
    "ActivityTracker",
    "ActivityUpdate",
    "Clock",
    "ChatDriver",
    "ChatSession",
    "ChatTurnOutcome",
    "ChatUpdate",
    "OpenedChatSession",
    "SessionEventUpdate",
    "TurnCompletedUpdate",
    "TurnFailedUpdate",
    "TurnInterruptedUpdate",
    "default_clock",
    "open_chat_session",
]
