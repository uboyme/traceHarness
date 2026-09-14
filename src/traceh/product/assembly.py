"""Resolve a confirmed task into an exact plan without executing it.

Re-resolve Profile, source, verification and target bindings on every call;
refuse any drift from the confirmed preflight. The execution mode is the
confirmed request. Compute the Workflow hash from the actual definition.
This owner produces no model calls, new Product progress events, Artifacts,
approvals or promotions. ProductTaskService remains the only Product writer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from traceh.api.product import (
    ProductAssemblyReceipt,
    ProductPreflightBinding,
    ResolvedTaskMode,
)
from traceh.api.promotion import PromotionTarget, PromotionTargetResolver
from traceh.api.workflow import WorkflowDefinition
from traceh.api.workspaces import WorkspaceSourceSnapshot
from traceh.product.errors import (
    ProductInputError,
    ProductProfileError,
    ProductStateError,
)
from traceh.product.events import require_hex_digest, require_product_identifier
from traceh.product.registry import ProductProfileRegistry, ResolvedProductProfile
from traceh.product.service import ProductTaskService
from traceh.product.topology import PRODUCT_MODE_ROLES, product_workflow_definition
from traceh.promotion.models import require_target_ref
from traceh.workflow.models import workflow_definition_hash


class ProductSourceResolver(Protocol):
    """The host seam from a registered source id to an exact commit.

    Narrow on purpose. Provisioning a worktree, inspecting it and removing it are
    the Workspace domain's job; assembling a binding needs only the snapshot, so
    this asks for only that. ``LocalGitWorkspaceProvider`` already satisfies it.
    """

    async def resolve_source(
        self, source_id: str, revision: str
    ) -> WorkspaceSourceSnapshot:
        ...


@dataclass(frozen=True, slots=True)
class ProductPreflight:
    """One Profile resolved against the world, before any mode is chosen.

    It carries the snapshot as well as the binding because a host that is about
    to provision worktrees needs the exact commit, and re-resolving it would let
    the plan and the checkout disagree. It deliberately does **not** carry the
    resolved :class:`PromotionTarget`: that value holds a repository path, and
    the only things this stage owes anyone about the target are its fingerprint
    and expected revision, both of which are in the binding.
    """

    profile: ResolvedProductProfile
    source: WorkspaceSourceSnapshot
    binding: ProductPreflightBinding

    @property
    def digest(self) -> str:
        return self.binding.digest


@dataclass(frozen=True, slots=True)
class ProductAssembly:
    """A receipt and the exact definition its hash was taken from."""

    preflight: ProductPreflight
    receipt: ProductAssemblyReceipt
    definition: WorkflowDefinition

    @property
    def resolved_mode(self) -> ResolvedTaskMode:
        return self.receipt.resolved_mode


class ProductAssemblyService:
    """Resolve, bind and assemble. Nothing here executes what it plans."""

    __slots__ = ("_registry", "_sources", "_targets", "_tasks")

    def __init__(
        self,
        tasks: ProductTaskService,
        *,
        registry: ProductProfileRegistry,
        sources: ProductSourceResolver,
        targets: PromotionTargetResolver,
    ) -> None:
        # The concrete writer, not a duck-typed stand-in. It is the only Event
        # Store-backed seam here, and it already proves its Session and Workflow
        # readers share its log - so requiring it is what makes "one fact
        # universe" a structural property instead of a comparison this service
        # would have to repeat and could forget.
        if type(tasks) is not ProductTaskService:
            raise ProductInputError("product-task-service-invalid", "tasks")
        if type(registry) is not ProductProfileRegistry:
            raise ProductInputError("product-registry-invalid", "registry")
        self._tasks = tasks
        self._registry = registry
        self._sources = sources
        self._targets = targets

    @property
    def tasks(self) -> ProductTaskService:
        return self._tasks

    async def preflight(self, profile_id: str) -> ProductPreflight:
        """Resolve one named Profile against the world, right now.

        Deterministic for one input and one state of the world: the same registry
        entry, the same source commit and the same target revision produce an
        equal binding and an equal digest. Nothing is cached, so a second call
        after the world moved produces a *different* binding rather than a stale
        one - which is precisely what lets :meth:`assemble` detect drift.
        """

        resolved = await self._registry.resolve(profile_id)
        profile = resolved.profile
        snapshot = await self._sources.resolve_source(
            profile.source_id, profile.source_revision
        )
        source = _require_snapshot(snapshot, profile.source_id, profile.source_revision)
        target = _require_target(
            await self._targets.resolve(profile.promotion_target_id),
            profile.promotion_target_id,
        )
        binding = ProductPreflightBinding(
            profile_digest=profile.digest,
            role_assembly_digest=resolved.role_assembly_digest,
            repository_fingerprint=source.repository_fingerprint,
            base_revision=source.base_revision,
            verification_plan_digest=resolved.verification_plan_digest,
            promotion_target_fingerprint=target.repository_fingerprint,
            promotion_target_ref=target.target_ref,
            promotion_expected_revision=target.expected_revision,
        )
        return ProductPreflight(profile=resolved, source=source, binding=binding)

    async def assemble(
        self,
        *,
        task_id: str,
        profile_id: str,
    ) -> ProductAssembly:
        """Re-resolve a confirmed task and derive a receipt, refusing settled tasks or drift."""

        task_id = require_product_identifier(task_id, field="task_id")
        summary = await self._tasks.load(task_id)
        if summary is None:
            raise ProductStateError("product-task-unknown", task_id)
        if summary.settled:
            raise ProductStateError("product-task-settled", task_id)
        preflight = await self.preflight(profile_id)
        # Checked here rather than after routing: an assembly that cannot bind
        # must fail before execution, and ``binds()`` compares exactly these two
        # digests once the receipt exists. The F1 writer applies the same
        # comparison again when the task actually starts.
        if preflight.binding.profile_digest != summary.profile_digest:
            raise ProductStateError("product-profile-drifted", task_id)
        if preflight.digest != summary.preflight_digest:
            raise ProductStateError("product-preflight-drifted", task_id)
        mode = ResolvedTaskMode(summary.requested_mode.value)
        definition = require_assemblable(preflight, mode)
        receipt = ProductAssemblyReceipt(
            preflight=preflight.binding,
            resolved_mode=mode,
            workflow_definition_hash=workflow_definition_hash(definition),
        )
        return ProductAssembly(
            preflight=preflight, receipt=receipt, definition=definition
        )



def require_assemblable(
    preflight: ProductPreflight, mode: ResolvedTaskMode
) -> WorkflowDefinition:
    """Prove one mode could really run, and return the definition it would use.

    Two things are checked, and both are about authority rather than shape. Every
    role the mode runs must have a resolved assembly, and each one's workspace
    access must be the access its *slot* defines - so a resolver cannot hand back
    a writable investigator and a Profile has no field with which to ask for one.
    """

    if type(preflight) is not ProductPreflight:
        raise ProductInputError("product-preflight-invalid", "preflight")
    roles = PRODUCT_MODE_ROLES.get(mode) if type(mode) is ResolvedTaskMode else None
    if roles is None:
        raise ProductInputError("product-resolved-mode-invalid", "mode")
    for role in roles:
        assembly = preflight.profile.execution_assembly(role, mode)
        if assembly.workspace_access is not role.workspace_access:
            raise ProductProfileError("product-assembly-access-mismatch", role.value)
    return product_workflow_definition(
        mode, promotion_target_id=preflight.profile.profile.promotion_target_id
    )




def _require_snapshot(
    snapshot: object, source_id: str, revision: str
) -> WorkspaceSourceSnapshot:
    """Accept a source resolution only when it answers the question asked."""

    if type(snapshot) is not WorkspaceSourceSnapshot:
        raise ProductProfileError("product-source-invalid", "source")
    if snapshot.source_id != source_id or snapshot.requested_revision != revision:
        raise ProductProfileError("product-source-mismatch", "source")
    require_hex_digest(
        snapshot.repository_fingerprint,
        lengths=(64,),
        field="repository-fingerprint",
    )
    require_hex_digest(
        snapshot.base_revision, lengths=(40, 64), field="base-revision"
    )
    return snapshot


def _require_target(target: object, target_id: str) -> PromotionTarget:
    if type(target) is not PromotionTarget:
        raise ProductProfileError("product-target-invalid", "promotion_target")
    if target.target_id != target_id:
        raise ProductProfileError("product-target-mismatch", "promotion_target")
    require_hex_digest(
        target.repository_fingerprint,
        lengths=(64,),
        field="promotion-target-fingerprint",
    )
    require_hex_digest(
        target.expected_revision,
        lengths=(40, 64),
        field="promotion-expected-revision",
    )
    try:
        require_target_ref(target.target_ref)
    except Exception:
        raise ProductProfileError(
            "product-target-ref-invalid", "promotion_target"
        ) from None
    return target


__all__ = [
    "ProductAssembly",
    "ProductAssemblyService",
    "ProductPreflight",
    "ProductSourceResolver",
    "require_assemblable",
]
