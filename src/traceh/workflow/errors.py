"""Stable, non-echoing failures for the typed Workflow."""

from __future__ import annotations


class WorkflowError(Exception):
    code = "workflow-error"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class WorkflowInputError(WorkflowError, ValueError):
    def __init__(self, code: str, field: str) -> None:
        self.code = code
        self.field = field
        super().__init__(f"workflow {field} is not usable")


class WorkflowDefinitionError(WorkflowError, ValueError):
    def __init__(self, code: str, node_id: str | None = None) -> None:
        self.code = code
        self.node_id = node_id
        super().__init__("the workflow definition is not a valid fixed DAG")


class WorkflowProtocolError(WorkflowError, ValueError):
    """A durable Workflow fact this build refuses to interpret.

    ``seq`` points at the offending event when one event is at fault. Checks
    that compare the whole stream against a definition have no single guilty
    event, so they report ``seq=0`` and name the node instead.
    """

    def __init__(self, code: str, seq: int = 0, node_id: str | None = None) -> None:
        self.code = code
        self.seq = seq
        self.node_id = node_id
        super().__init__("the workflow stream protocol is invalid")


class WorkflowStateError(WorkflowError):
    def __init__(self, code: str, node_id: str | None = None) -> None:
        self.code = code
        self.node_id = node_id
        super().__init__("the workflow is not in a state that allows this")


class WorkflowNodeFailedError(WorkflowError):
    """One node failed; the run stops and records why."""

    def __init__(self, code: str, node_id: str) -> None:
        self.code = code
        self.node_id = node_id
        super().__init__("a workflow node did not complete")


class WorkflowRecoveryError(WorkflowError):
    """The stream stopped somewhere this stage refuses to continue from."""

    def __init__(self, code: str, node_id: str | None = None) -> None:
        self.code = code
        self.node_id = node_id
        super().__init__("this workflow cannot be safely continued")


class WorkflowOperationConflictError(WorkflowError):
    code = "workflow-operation-reused"

    def __init__(self) -> None:
        super().__init__("the operation identity was already used for other content")


class WorkflowLedgerConflictError(WorkflowError):
    code = "workflow-stream-changed"

    def __init__(self) -> None:
        super().__init__("the workflow stream changed before this fact was recorded")


class WorkflowWriteError(WorkflowError):
    code = "workflow-write-failed"

    def __init__(self, *, committed: bool | None) -> None:
        self.committed = committed
        if committed is None:
            message = "workflow write failed and whether it was recorded is unknown"
        elif committed:
            message = "workflow write was recorded but the call failed"
        else:
            message = "workflow write could not be recorded"
        super().__init__(message)


class WorkflowServiceClosedError(WorkflowError):
    code = "workflow-service-closed"

    def __init__(self) -> None:
        super().__init__("the workflow service is closed")


__all__ = [
    "WorkflowDefinitionError",
    "WorkflowError",
    "WorkflowInputError",
    "WorkflowLedgerConflictError",
    "WorkflowNodeFailedError",
    "WorkflowOperationConflictError",
    "WorkflowProtocolError",
    "WorkflowRecoveryError",
    "WorkflowServiceClosedError",
    "WorkflowStateError",
    "WorkflowWriteError",
]
