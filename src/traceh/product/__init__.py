"""ProductTask facts, fixed assembly and the optional F3 Chat control plane.

This domain owns two things and refuses everything between them and execution.
v0.7-F1 owns what a ProductTask durably *is*: a strict parser, one projector, a
fresh reader and the single host writer. v0.7-F2 adds what a confirmed task will
run *as*: the strict mode router, the one Profile Registry, the preflight binding
and the fixed Workflow definition a receipt is taken from.

F3 adds a thin host assembly over the existing Workflow, Agent, Budget,
Workspace, Artifact and Promotion services.  It does not move those facts or
lifecycles into this package. Model-visible actions create an ephemeral
proposal/confirmation note which the host verifies after Turn close; a separate
Session snapshot records only the canonical Product status the requester model
was shown, never control authority.
"""

from traceh.product.assembly import (
    ProductAssembly,
    ProductAssemblyService,
    ProductPreflight,
    ProductSourceResolver,
    product_routing_operation_id,
    require_assemblable,
)
from traceh.product.context import (
    MAX_PRODUCT_CONTEXT_APPEND_ATTEMPTS,
    ProductModelContext,
)
from traceh.product.errors import (
    ProductContextError,
    ProductError,
    ProductEvidenceError,
    ProductInputError,
    ProductOperationConflictError,
    ProductProfileError,
    ProductProtocolError,
    ProductRoutingError,
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
from traceh.product.host import (
    ProductChatHost,
    ProductHostProfile,
    build_product_chat_host,
)
from traceh.product.observation import (
    ObservedStreamHead,
    ProductObservation,
    ProductObservationReader,
    ProductObservationSession,
    ProductUsage,
)
from traceh.product.projection import (
    ProductTaskIssue,
    ProductTaskStreamReader,
    rebuild_product_task,
    validate_product_task,
)
from traceh.product.registry import (
    ProductAssemblyResolver,
    ProductProfileBinding,
    ProductProfileRegistry,
    ResolvedAgentAssembly,
    ResolvedProductProfile,
    agent_assembly_digest,
    role_assembly_digest,
    router_assembly_digest,
)
from traceh.product.router import (
    MAX_ROUTER_SUMMARY_CHARS,
    ProductModeRouter,
    RouterDecision,
    RouterResponder,
    RouterResponse,
    StrictTaskRoutingParser,
)
from traceh.product.service import (
    MAX_APPEND_ATTEMPTS,
    ProductTaskService,
    TaskOwnershipSource,
    WorkflowStateSource,
    converge_product_task,
)
from traceh.product.topology import (
    PRODUCT_APPROVAL_NODE,
    PRODUCT_MODE_ROLES,
    PRODUCT_VERIFICATION_NODE,
    product_definition_hash,
    product_message_binding,
    product_role_node_id,
    product_spec_binding,
    product_workflow_definition,
)

__all__ = [
    "MAX_APPEND_ATTEMPTS",
    "MAX_PRODUCT_CONTEXT_APPEND_ATTEMPTS",
    "MAX_REASON_DISPLAY_CHARS",
    "MAX_ROUTER_SUMMARY_CHARS",
    "PRODUCT_APPROVAL_NODE",
    "PRODUCT_MODE_ROLES",
    "PRODUCT_VERIFICATION_NODE",
    "MessageEvidence",
    "ObservedStreamHead",
    "ProductAssembly",
    "ProductAssemblyResolver",
    "ProductAssemblyService",
    "ProductChatHost",
    "ProductContextError",
    "ProductError",
    "ProductEvidenceError",
    "ProductInputError",
    "ProductHostProfile",
    "ProductModeRouter",
    "ProductModelContext",
    "ProductObservation",
    "ProductObservationReader",
    "ProductObservationSession",
    "ProductUsage",
    "ProductOperationConflictError",
    "ProductPreflight",
    "ProductProfileBinding",
    "ProductProfileError",
    "ProductProfileRegistry",
    "ProductProtocolError",
    "ProductRoutingError",
    "ProductServiceClosedError",
    "ProductSourceResolver",
    "ProductStateError",
    "ProductStreamConflictError",
    "ProductTaskIssue",
    "ProductTaskService",
    "ProductTaskStreamReader",
    "ProductWriteError",
    "ResolvedAgentAssembly",
    "ResolvedProductProfile",
    "RouterDecision",
    "RouterResponder",
    "RouterResponse",
    "SessionEvidenceReader",
    "StrictTaskRoutingParser",
    "TaskOwnershipSource",
    "WorkflowStateSource",
    "agent_assembly_digest",
    "build_product_chat_host",
    "converge_product_task",
    "is_product_fact",
    "product_definition_hash",
    "product_event_header",
    "product_message_binding",
    "product_role_node_id",
    "product_routing_operation_id",
    "product_spec_binding",
    "product_task_stream",
    "product_workflow_definition",
    "rebuild_product_task",
    "require_assemblable",
    "require_confirmation_evidence",
    "require_product_identifier",
    "role_assembly_digest",
    "router_assembly_digest",
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
