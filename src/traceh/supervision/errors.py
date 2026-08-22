"""Stable, non-echoing errors for the process-local Agent Supervisor.

Every message here is written in this repository and identified by a fixed
``code``. Nothing echoes a caller value, a message body, an exception text or a
traceback: a delivery failure records *that* something failed and under which
stable code, never what the third-party code said while failing.
"""

from __future__ import annotations


class SupervisionError(Exception):
    """Base class for Supervisor and delivery failures."""

    code = "supervision-error"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class DeliveryInputError(SupervisionError, ValueError):
    """A caller-supplied delivery field is not usable.

    Raised before anything is read or appended, so a rejected value cannot
    reach the event log.
    """

    def __init__(self, code: str, field: str) -> None:
        self.code = code
        self.field = field
        super().__init__(f"agent delivery {field} is not usable")


class DeliveryProtocolError(SupervisionError, ValueError):
    """A persisted delivery fact is malformed, duplicated or contradictory.

    Replay fails closed rather than repairing: the delivery log is what decides
    whether a message has already been claimed and run, so a projector that
    skipped a broken record could hand the same message to a second Turn.
    """

    def __init__(self, code: str, seq: int) -> None:
        self.code = code
        self.seq = seq
        super().__init__(_delivery_message(code))


def _delivery_message(code: str) -> str:
    messages = {
        "delivery-event-type-unknown": "an agent delivery stream contains an unknown event",
        "delivery-stream-unexpected": "a delivery fact is on the wrong stream",
        "delivery-schema-version-unsupported": (
            "a delivery fact uses an unsupported schema version"
        ),
        "delivery-payload-keys-unexpected": "a delivery fact has unexpected payload keys",
        "delivery-payload-invalid": "a delivery fact is malformed",
        "delivery-identity-invalid": "a delivery fact has an unusable identifier",
        "delivery-reason-invalid": "a delivery fact has an unusable reason",
        "delivery-accepted-seq-invalid": "a delivery fact has a malformed accepted sequence",
        "delivery-message-unknown": "a claim references a message this Agent never accepted",
        "delivery-inbox-agent-mismatch": "the inbox belongs to a different agent",
        "delivery-view-stale": "the supplied delivery view is not the current durable view",
        "delivery-claim-not-next": "the message is not the next claimable FIFO item",
        "delivery-claim-open": "an earlier delivery claim has no terminal outcome",
        "delivery-accepted-seq-mismatch": (
            "a claim disagrees with the accepted message it references"
        ),
        "delivery-claim-duplicate": "a message was claimed twice",
        "delivery-claim-id-duplicate": "two claims share one claim id",
        "delivery-claim-unknown": "a terminal fact references a claim that does not exist",
        "delivery-claim-already-terminal": "a claim reached a terminal state twice",
        "delivery-claim-message-mismatch": (
            "a terminal fact disagrees with the claim it references"
        ),
    }
    return messages.get(code, "the agent delivery protocol is invalid")


class DeliveryConflictError(SupervisionError):
    """The delivery stream advanced between the read and the append.

    Nothing was written; another worker won the claim. This is the ordinary,
    expected outcome of two Supervisors racing for the same message.
    """

    code = "delivery-changed"

    def __init__(self) -> None:
        super().__init__("the agent delivery log changed before this fact could be recorded")


class DeliveryAppendError(SupervisionError):
    """A delivery append failed.

    ``committed`` carries the same three states as the Stage A and Stage B
    transactions: ``True``, ``False``, or ``None`` for unknown. Unknown is never
    collapsed into "not written" - for a *claim* that distinction decides
    whether a Turn may run at all.
    """

    code = "delivery-append-failed"

    def __init__(self, *, committed: bool | None) -> None:
        self.committed = committed
        if committed is None:
            message = "a delivery fact failed and whether it was recorded is unknown"
        elif committed:
            message = "a delivery fact was recorded but the call failed"
        else:
            message = "a delivery fact could not be recorded"
        super().__init__(message)


class UnsupportedMessageTargetError(SupervisionError):
    """Only `MessageTarget.NEW_TURN` can be delivered in this Stage.

    ``NEXT_STEP`` means "inject into the Turn that is already running", and
    there is no safe seam for that: a Step has a frozen Composition and an
    in-flight model call. Silently promoting it to a new Turn would deliver
    something other than what the sender asked for, so it is refused *before*
    anything is accepted rather than accepted and then reinterpreted.
    """

    code = "message-target-unsupported"

    def __init__(self) -> None:
        super().__init__("only new_turn messages can be delivered by this supervisor")


class AgentNotActiveError(SupervisionError):
    """No Activation for this Agent exists in this Supervisor."""

    code = "agent-not-active"

    def __init__(self) -> None:
        super().__init__("this supervisor has no activation for that agent")


class AgentOwnerNotActiveError(SupervisionError):
    """A child cannot become live while its lifecycle owner is inactive."""

    code = "agent-owner-not-active"

    def __init__(self) -> None:
        super().__init__("the agent lifecycle owner is not active")


class ActivationConflictError(SupervisionError):
    """Another Agent already owns the Session an Activation asked for."""

    code = "activation-session-taken"

    def __init__(self) -> None:
        super().__init__("that session is already bound to a different activation")


class ExecutionStoreMismatchError(SupervisionError):
    """The execution runtime does not write to the Supervisor's `EventStore`.

    Compared by object identity, never by configuration or path: two stores can
    look identically configured and still be two different logs, and a Turn
    that appended to the wrong one would leave the claim pointing at a Session
    history that does not contain it.
    """

    code = "execution-store-mismatch"

    def __init__(self) -> None:
        super().__init__("the execution runtime uses a different event store")


class ExecutionSessionMismatchError(SupervisionError):
    """The execution runtime is bound to a different Session than the Agent."""

    code = "execution-session-mismatch"

    def __init__(self) -> None:
        super().__init__("the execution runtime is bound to a different session")


class ActivationFaultedError(SupervisionError):
    """The Activation stopped because it could not prove a durable fact.

    Reported rather than retried. Stage C has no retry policy, no attempt
    identity and no cold recovery, so silently trying again could run one
    message twice.
    """

    code = "activation-faulted"

    def __init__(self, fault_code: str) -> None:
        self.fault_code = fault_code
        super().__init__(f"the activation stopped: {fault_code}")


class SupervisorDisposedError(SupervisionError):
    """The Supervisor, or this Agent's Activation, is shutting down."""

    code = "supervisor-disposed"

    def __init__(self) -> None:
        super().__init__("the supervisor is disposed")


class MessageWakeError(SupervisionError):
    """The message was accepted, but waking an Activation failed.

    Carries the `MessageReceipt` because the acceptance is durable and the
    caller must not be told otherwise. Reporting a plain failure here would
    invite a retry that appends the same message under a new id, or - worse -
    leave the caller believing nothing was recorded when it was.
    """

    code = "message-wake-failed"

    def __init__(self, receipt) -> None:
        self.receipt = receipt
        super().__init__("the message was accepted but the activation could not be woken")


class AgentMessageNotFoundError(SupervisionError, LookupError):
    """The requested durable Inbox message does not exist for this Agent."""

    code = "agent-message-not-found"

    def __init__(self) -> None:
        super().__init__("the Agent has no message with that identity")


class AgentMessageNotSettledError(SupervisionError):
    """The requested message has not reached a durable terminal outcome."""

    code = "agent-message-not-settled"

    def __init__(self) -> None:
        super().__init__("the Agent message has not settled")


class AgentRunEvidenceError(SupervisionError):
    """Durable delivery and Session facts do not prove one coherent run."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("the Agent run evidence is incomplete or contradictory")


__all__ = [
    "ActivationConflictError",
    "ActivationFaultedError",
    "AgentNotActiveError",
    "AgentMessageNotFoundError",
    "AgentMessageNotSettledError",
    "AgentOwnerNotActiveError",
    "AgentRunEvidenceError",
    "DeliveryAppendError",
    "DeliveryConflictError",
    "DeliveryInputError",
    "DeliveryProtocolError",
    "ExecutionSessionMismatchError",
    "ExecutionStoreMismatchError",
    "MessageWakeError",
    "SupervisionError",
    "SupervisorDisposedError",
    "UnsupportedMessageTargetError",
]
