"""ProductTask persistent facts: parser, projection, reader and host writer.

This domain owns one thing - what a ProductTask durably *is* - and nothing that
happens to one. It starts no Workflow, calls no model, promotes nothing, renders
no chat and exposes no model-visible capability. The contract it implements is
frozen in :mod:`traceh.api.product`; this package is the first real production
mainline built on it.
"""

from traceh.product.errors import (
    ProductError,
    ProductEvidenceError,
    ProductInputError,
    ProductOperationConflictError,
    ProductProtocolError,
    ProductServiceClosedError,
    ProductStateError,
    ProductStreamConflictError,
    ProductWriteError,
)
from traceh.product.events import (
    MAX_REASON_DISPLAY_CHARS,
    is_product_fact,
    product_event_header,
    product_task_stream,
    require_product_identifier,
    task_abandoned_data,
    task_awaiting_data,
    task_cancelled_data,
    task_completed_data,
    task_failed_data,
    task_id_from_stream,
    task_opened_data,
    task_rejected_data,
    task_routed_data,
    task_started_data,
)
from traceh.product.evidence import (
    MessageEvidence,
    SessionEvidenceReader,
    require_confirmation_evidence,
)
from traceh.product.projection import (
    ProductTaskIssue,
    ProductTaskStreamReader,
    rebuild_product_task,
    validate_product_task,
)
from traceh.product.service import (
    MAX_APPEND_ATTEMPTS,
    ProductTaskService,
    TaskOwnershipSource,
    WorkflowStateSource,
    converge_product_task,
)

__all__ = [
    "MAX_APPEND_ATTEMPTS",
    "MAX_REASON_DISPLAY_CHARS",
    "MessageEvidence",
    "ProductError",
    "ProductEvidenceError",
    "ProductInputError",
    "ProductOperationConflictError",
    "ProductProtocolError",
    "ProductServiceClosedError",
    "ProductStateError",
    "ProductStreamConflictError",
    "ProductTaskIssue",
    "ProductTaskService",
    "ProductTaskStreamReader",
    "ProductWriteError",
    "SessionEvidenceReader",
    "TaskOwnershipSource",
    "WorkflowStateSource",
    "converge_product_task",
    "is_product_fact",
    "product_event_header",
    "product_task_stream",
    "rebuild_product_task",
    "require_confirmation_evidence",
    "require_product_identifier",
    "task_abandoned_data",
    "task_awaiting_data",
    "task_cancelled_data",
    "task_completed_data",
    "task_failed_data",
    "task_id_from_stream",
    "task_opened_data",
    "task_rejected_data",
    "task_routed_data",
    "task_started_data",
    "validate_product_task",
]
