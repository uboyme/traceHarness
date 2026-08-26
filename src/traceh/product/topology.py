"""The two fixed Workflow shapes the product surface runs, and nothing else.

There are exactly two, they are functions of the resolved mode, and no Profile,
task, Router answer or configuration file can add a node, an edge, an Agent or a
fan-out. That is the whole point: the router chooses between two values, and the
two values are written here.

``single`` is ``coder -> verification -> approval``.
``multi`` is ``parent -> reviewer -> coder -> verification -> approval``.

``single`` is a *shorter* Workflow, not a shortcut past one. Both end in the same
safety tail - the same frozen verification plan, the same immutable Artifact, the
same human Approval barrier - because a second "fast path" is exactly where a
check would later be skipped.

The reviewer runs *before* the coder. A reviewer after the coder produces an
opinion nothing consumes: the fixed verifier does not read it, the Approval node
does not read it and no later node runs. Placing it first makes it part of the
work rather than commentary on it.

Only the coder captures an Artifact, because only the coder may write. Every
identifier here is derived from the role and the node kind, so two hosts running
the same mode against the same promotion target produce the same definition and
the same hash.
"""

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

PRODUCT_MODE_ROLES: Mapping[ResolvedTaskMode, tuple[ProductRole, ...]] = (
    MappingProxyType(
        {
            ResolvedTaskMode.SINGLE: (ProductRole.CODER,),
            ResolvedTaskMode.MULTI: (
                ProductRole.PARENT,
                ProductRole.REVIEWER,
                ProductRole.CODER,
            ),
        }
    )
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
    target_id = require_product_identifier(
        promotion_target_id, field="promotion_target_id"
    )
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


def product_definition_hash(
    mode: ResolvedTaskMode, *, promotion_target_id: str
) -> str:
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
