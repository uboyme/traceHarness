"""The fixed outer Workflow shared by single and multi execution.

Both run coder -> verification -> human approval. Multi delegation occurs
inside the coder execution owner and cannot bypass this tail. Only the coder
captures an Artifact after its owned children converge. Definitions retain
strategy-specific identities and exact promotion-target binding."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from traceh.api.product import ProductRole, ResolvedTaskMode
from traceh.api.workflow import (
    AgentTaskNode,
    ApprovalNode,
    VerificationNode,
    WorkflowDefinition,
)
from traceh.product.errors import ProductInputError
from traceh.product.events import require_product_identifier
from traceh.workflow.models import freeze_workflow_definition, workflow_definition_hash

PRODUCT_MODE_ROLES: Mapping[ResolvedTaskMode, tuple[ProductRole, ...]] = MappingProxyType(
    {
        ResolvedTaskMode.SINGLE: (ProductRole.CODER,),
        ResolvedTaskMode.MULTI: (ProductRole.CODER,),
    }
)
"""Which roles each mode runs, in execution order, as a read-only mapping.

The coder is last in both, which is what makes the verification tail able to name
one Artifact-producing node without knowing which mode it is in. A mapping any
importer could rewrite would let one import silently change what every later
caller builds.
"""

PRODUCT_VERIFICATION_NODE = "product-verification"
PRODUCT_APPROVAL_NODE = "product-approval"


def product_role_node_id(role: ProductRole) -> str:
    return f"product-role-{_role_value(role)}"


def product_spec_binding(role: ProductRole) -> str:
    return f"product-spec-{_role_value(role)}"


def product_message_binding(role: ProductRole) -> str:
    return f"product-message-{_role_value(role)}"


def product_workflow_definition(
    mode: ResolvedTaskMode, *, promotion_target_id: str
) -> WorkflowDefinition:
    """Build and fully validate the one definition ``mode`` selects.

    ``promotion_target_id`` participates because the Verification node really
    does name a target, and a definition hash that ignored it would say two runs
    against two different repositories were the same plan.

    Bindings are ids, never values: the durable definition carries no prompt, no
    ``AgentSpec``, no repository path and no credential. What each id resolves to
    is the host resolver's answer at run time, and what it resolved to *at
    binding time* is covered by ``role_assembly_digest``.
    """

    roles = PRODUCT_MODE_ROLES.get(mode) if type(mode) is ResolvedTaskMode else None
    if roles is None:
        raise ProductInputError("product-resolved-mode-invalid", "mode")
    target_id = require_product_identifier(promotion_target_id, field="promotion_target_id")
    agents: list[AgentTaskNode] = []
    predecessors: tuple[str, ...] = ()
    for role in roles:
        node = AgentTaskNode(
            node_id=product_role_node_id(role),
            predecessors=predecessors,
            spec_binding=product_spec_binding(role),
            message_binding=product_message_binding(role),
            # Exactly one role may write, so exactly one node has candidate
            # bytes to freeze. A capturing reviewer would produce an Artifact
            # nobody could attribute.
            capture_artifact=role is ProductRole.CODER,
        )
        agents.append(node)
        predecessors = (node.node_id,)
    coder_node = product_role_node_id(ProductRole.CODER)
    verification = VerificationNode(
        node_id=PRODUCT_VERIFICATION_NODE,
        predecessors=(coder_node,),
        artifact_node_id=coder_node,
        target_id=target_id,
    )
    approval = ApprovalNode(
        node_id=PRODUCT_APPROVAL_NODE,
        predecessors=(PRODUCT_VERIFICATION_NODE,),
        review_node_id=PRODUCT_VERIFICATION_NODE,
    )
    return freeze_workflow_definition(
        WorkflowDefinition(
            definition_id=f"product-{mode.value}",
            nodes=(*agents, verification, approval),
        )
    )


def product_definition_hash(mode: ResolvedTaskMode, *, promotion_target_id: str) -> str:
    """The hash a receipt records, taken from the definition that will run.

    Recomputed from the built definition rather than stored beside it, so a
    receipt cannot name a plan other than the one it carries.
    """

    return workflow_definition_hash(
        product_workflow_definition(mode, promotion_target_id=promotion_target_id)
    )


def _role_value(role: object) -> str:
    if type(role) is not ProductRole:
        raise ProductInputError("product-role-invalid", "role")
    return role.value


__all__ = [
    "PRODUCT_APPROVAL_NODE",
    "PRODUCT_MODE_ROLES",
    "PRODUCT_VERIFICATION_NODE",
    "product_definition_hash",
    "product_message_binding",
    "product_role_node_id",
    "product_spec_binding",
    "product_workflow_definition",
]
