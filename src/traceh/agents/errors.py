"""Stable, non-echoing errors for the durable Agent control plane.

Every message here is written in this repository and identified by a fixed
``code``. Rejected identifiers are never echoed: an ``agent_id`` or
``session_id`` arrives from a caller and may be a mis-pasted credential, and a
message that reprints it turns a validation error into a disclosure. Callers
branch on ``code``, not on message text.
"""

from __future__ import annotations


class AgentControlPlaneError(Exception):
    """Base class for durable Agent identity failures.

    ``code`` is the stable discriminator. ``message`` is fixed text from this
    repository and never contains a caller-supplied value.
    """

    code = "agent-control-plane-error"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class AgentIdentityError(AgentControlPlaneError, ValueError):
    """A caller-supplied identity or specification field is not usable.

    Raised before anything is read or appended, so a rejected input cannot
    reach the event log.
    """

    def __init__(self, code: str, field: str) -> None:
        self.code = code
        self.field = field
        # The field name is ours; the value is the caller's and is withheld.
        super().__init__(f"agent {field} is not a usable identity")


class AgentDirectoryProtocolError(AgentControlPlaneError, ValueError):
    """A persisted Agent fact is malformed, duplicated or contradictory.

    Replay fails closed rather than repairing history: a directory that
    silently drops a broken record would answer questions about an Agent set
    that never existed.
    """

    def __init__(self, code: str, seq: int) -> None:
        self.code = code
        self.seq = seq
        super().__init__(_directory_message(code))


def _directory_message(code: str) -> str:
    messages = {
        "agent-event-type-unknown": "the agent directory stream contains an unknown event",
        "agent-stream-unexpected": "an agent creation fact is on the wrong stream",
        "agent-schema-version-unsupported": (
            "an agent creation fact uses an unsupported schema version"
        ),
        "agent-payload-keys-unexpected": "an agent creation fact has unexpected payload keys",
        "agent-payload-invalid": "an agent creation fact is malformed",
        "agent-identity-invalid": "an agent creation fact has an unusable identity",
        "agent-budget-invalid": "an agent creation fact has a malformed budget",
        "agent-grants-invalid": "an agent creation fact has malformed capability grants",
        "agent-metadata-invalid": "an agent creation fact has malformed metadata",
        "agent-id-duplicate": "two agent creation facts claim the same agent id",
        "agent-session-duplicate": "two agent creation facts claim the same session id",
        "agent-request-duplicate": "two agent creation facts share one request id",
        "agent-owner-unknown": "an agent creation fact names an owner that does not exist",
        "agent-owner-self": "an agent creation fact names itself as its owner",
    }
    return messages.get(code, "the agent directory protocol is invalid")


class AgentIdentityConflictError(AgentControlPlaneError):
    """The requested ``agent_id`` is already a durable identity."""

    code = "agent-id-taken"

    def __init__(self) -> None:
        super().__init__("agent_id is already registered")


class AgentSessionConflictError(AgentControlPlaneError):
    """The requested ``session_id`` already belongs to another Agent.

    One Session has exactly one owning Agent. Allowing a second claim would
    let two identities append to one history.
    """

    code = "agent-session-taken"

    def __init__(self) -> None:
        super().__init__("session_id already belongs to another agent")


class AgentOwnerNotFoundError(AgentControlPlaneError):
    """``owner_agent_id`` does not name an existing durable Agent."""

    code = "agent-owner-unknown"

    def __init__(self) -> None:
        super().__init__("owner_agent_id does not name a registered agent")


class AgentRequestConflictError(AgentControlPlaneError):
    """A stable ``request_id`` was reused for a different identity.

    Retrying with the same ``request_id`` is how a caller makes creation
    idempotent, so reusing it to mean something else must fail rather than
    quietly create a second Agent.
    """

    code = "agent-request-reused"

    def __init__(self) -> None:
        super().__init__("request_id was already used for a different agent identity")


class AgentDirectoryConflictError(AgentControlPlaneError):
    """The directory advanced between the read and the append.

    Nothing was written. Retrying with the same ``request_id`` is safe.
    """

    code = "agent-directory-changed"

    def __init__(self) -> None:
        super().__init__("the agent directory changed before this creation could be recorded")


class AgentCreationError(AgentControlPlaneError):
    """The creation append failed.

    ``committed`` reports what a re-read of the stream actually proved about
    the append, and has three states rather than two:

    * ``False`` - the record is provably not in the stream;
    * ``True`` - the record *is* in the stream even though the call failed;
    * ``None`` - **unknown.** The reconciling read could not answer, so nothing
      may be claimed either way.

    The third state is not pedantry. Reporting an unreadable stream as
    ``False`` would state that nothing was written at the exact moment the code
    has the least evidence for it, and a caller acting on that would create a
    second Agent for a request that had already committed. Retrying with the
    same ``request_id`` stays safe under all three.

    None of them is a disguised success; this is always a failure.
    """

    code = "agent-creation-failed"

    def __init__(self, *, committed: bool | None) -> None:
        self.committed = committed
        if committed is None:
            message = "agent creation failed and whether it was recorded is unknown"
        elif committed:
            message = "agent creation was recorded but the call failed"
        else:
            message = "agent creation could not be recorded"
        super().__init__(message)


__all__ = [
    "AgentControlPlaneError",
    "AgentCreationError",
    "AgentDirectoryConflictError",
    "AgentDirectoryProtocolError",
    "AgentIdentityConflictError",
    "AgentIdentityError",
    "AgentOwnerNotFoundError",
    "AgentRequestConflictError",
    "AgentSessionConflictError",
]
