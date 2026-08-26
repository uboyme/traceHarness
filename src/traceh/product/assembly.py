"""Turn a confirmed ProductTask into one exact plan that has not run yet.

This is the last step before execution and it deliberately stops there. It
resolves a Profile, binds it against the world, decides a mode when the task
asked for ``auto``, and produces one :class:`ProductAssemblyReceipt` beside the
Workflow definition that receipt's hash was taken from. It starts no run,
captures nothing, verifies nothing, approves nothing and promotes nothing.

Everything it produces is re-derived, never accepted:

* the binding is re-resolved on every call, so a caller cannot hand in a
  ``ProductPreflightBinding`` assembled from somewhere else;
* the definition hash is computed from the definition that would actually run;
* the resolved mode for an explicit request is that request, and for ``auto`` it
  is the single durable ``product/task-routed`` fact - re-read rather than
  decided a second time.

Drift is a refusal, not a rebind. If re-resolving now produces a different
Profile digest or a different preflight digest than the one recorded when a
person confirmed, the task fails closed and has to be opened again against what
the world is now. That covers a moved source revision, a changed verification
plan, a rebound registry entry and a promotion target whose ref advanced, and it
is checked *before* the router is asked anything, so a task that cannot run never
spends routing tokens.

The only durable write here goes through the F1 writer. There is no second
appender, no second projector and no cached receipt: this service holds one
:class:`ProductTaskService`, which is what makes "one Event Store" structural
rather than a comparison that has to be remembered.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from traceh.api.json_types import fingerprint
from traceh.api.product import (
    ProductAssemblyReceipt,
    ProductPreflightBinding,
    ProductTaskSummary,
    RequestedTaskMode,
    ResolvedTaskMode,
)
from traceh.api.promotion import PromotionTarget, PromotionTargetResolver
from traceh.api.workflow import WorkflowDefinition
from traceh.api.workspaces import WorkspaceSourceSnapshot
from traceh.product.errors import (
    ProductInputError,
    ProductOperationConflictError,
    ProductProfileError,
    ProductRoutingError,
    ProductStateError,
)
from traceh.product.events import require_hex_digest, require_product_identifier
from traceh.product.registry import ProductProfileRegistry, ResolvedProductProfile
from traceh.product.router import ProductModeRouter
from traceh.product.service import ProductTaskService
from traceh.product.topology import PRODUCT_MODE_ROLES, product_workflow_definition
from traceh.promotion.models import require_target_ref
from traceh.workflow.models import workflow_definition_hash

PRODUCT_ROUTING_PURPOSE = "product-task-routing"
"""What the derived routing ``operation_id`` is for.

Deriving it from the task rather than taking it from the caller is what makes a
retried assembly the *same* write: F1 treats one ``operation_id`` with a
byte-identical payload as one operation, and a fresh id on every attempt would
turn a retry into a second routing fact the transition table then refuses.
"""


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

    __slots__ = ("_registry", "_router", "_sources", "_targets", "_tasks")

    def __init__(
        self,
        tasks: ProductTaskService,
        *,
        registry: ProductProfileRegistry,
        sources: ProductSourceResolver,
        targets: PromotionTargetResolver,
        router: ProductModeRouter,
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
        if type(router) is not ProductModeRouter:
            raise ProductInputError("product-router-invalid", "router")
        self._tasks = tasks
        self._registry = registry
        self._sources = sources
        self._targets = targets
        self._router = router

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
            router_assembly_digest=resolved.router_assembly_digest,
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
        routing_summary: str | None = None,
    ) -> ProductAssembly:
        """Produce the one receipt this task may start with, or refuse.

        ``routing_summary`` is required only for ``auto`` and only when nothing
        has been routed yet. An explicit ``single`` or ``multi`` task never
        reaches the router at all, so passing a summary for one is unnecessary
        and passing none for one is not an error.
        """

        task_id = require_product_identifier(task_id, field="task_id")
        summary = await self._tasks.load(task_id)
        if summary is None:
            raise ProductStateError("product-task-unknown", task_id)
        if summary.settled:
            raise ProductStateError("product-task-settled", task_id)
        preflight = await self.preflight(profile_id)
        # Checked here rather than after routing: an assembly that cannot bind
        # must not cost a router call, and ``binds()`` compares exactly these two
        # digests once the receipt exists. The F1 writer applies the same
        # comparison again when the task actually starts.
        if preflight.binding.profile_digest != summary.profile_digest:
            raise ProductStateError("product-profile-drifted", task_id)
        if preflight.digest != summary.preflight_digest:
            raise ProductStateError("product-preflight-drifted", task_id)
        mode = await self._resolved_mode(summary, preflight, routing_summary)
        definition = require_assemblable(preflight, mode)
        receipt = ProductAssemblyReceipt(
            preflight=preflight.binding,
            resolved_mode=mode,
            workflow_definition_hash=workflow_definition_hash(definition),
        )
        return ProductAssembly(
            preflight=preflight, receipt=receipt, definition=definition
        )

    async def _resolved_mode(
        self,
        summary: ProductTaskSummary,
        preflight: ProductPreflight,
        routing_summary: str | None,
    ) -> ResolvedTaskMode:
        """The single mode this task runs, decided at most once, ever."""

        if summary.requested_mode is not RequestedTaskMode.AUTO:
            # An explicit request is its own answer. Asking a router here would
            # let it overrule a person who already chose.
            return ResolvedTaskMode(summary.requested_mode.value)
        if summary.resolved_mode is not None:
            # There is exactly one durable routing fact and this is it. Routing
            # again would be a second free decision about a settled question.
            return summary.resolved_mode
        if routing_summary is None:
            raise ProductRoutingError("product-router-summary-missing")
        # The Profile Registry and the owned router are two host seams. Both
        # must name the same bounds and the same resolved Router assembly before
        # any token is spent; otherwise the preflight would only describe a
        # paper Router while another live object answered.
        if not self._router.binds(
            preflight.profile.profile.router,
            preflight.profile.router_assembly_digest,
        ):
            raise ProductProfileError("product-router-binding-mismatch", "router")
        # Both shapes are already known to be buildable, because the preflight
        # that ran before this point resolved all three role assemblies as one
        # binding and refused if any of them was unavailable. That ordering is
        # the point: a task that could not run either way never costs a routing
        # token, and a drifted binding is refused before the question is asked.
        decision = await self._router.route(
            routing_summary, task_id=summary.task_id
        )
        try:
            routed = await self._tasks.record_routing(
                task_id=summary.task_id,
                operation_id=product_routing_operation_id(summary.task_id),
                routing=decision.routing,
                router_agent_id=decision.router_agent_id,
                routing_session_id=decision.routing_session_id,
            )
        except (ProductStateError, ProductOperationConflictError):
            # Another writer recorded the one durable decision first. The answer
            # is that fact, not this call's; re-reading is how "exactly one
            # routing result" stays true under concurrency.
            current = await self._tasks.load(summary.task_id)
            if current is None or current.resolved_mode is None:
                raise
            return current.resolved_mode
        if routed.resolved_mode is None:  # pragma: no cover - replay establishes it
            raise ProductStateError("product-routing-missing", summary.task_id)
        return routed.resolved_mode


def require_assemblable(
    preflight: ProductPreflight, mode: ResolvedTaskMode
) -> WorkflowDefinition:
    """Prove one mode could really run, and return the definition it would use.

    Two things are checked, and both are about authority rather than shape. Every
    role the mode runs must have a resolved assembly, and each one's workspace
    access must be the access its *slot* defines - so a resolver cannot hand back
    a writable reviewer and a Profile has no field with which to ask for one.
    """

    if type(preflight) is not ProductPreflight:
        raise ProductInputError("product-preflight-invalid", "preflight")
    roles = PRODUCT_MODE_ROLES.get(mode) if type(mode) is ResolvedTaskMode else None
    if roles is None:
        raise ProductInputError("product-resolved-mode-invalid", "mode")
    for role in roles:
        assembly = preflight.profile.assembly(role)
        if assembly.workspace_access is not role.workspace_access:
            raise ProductProfileError("product-assembly-access-mismatch", role.value)
    return product_workflow_definition(
        mode, promotion_target_id=preflight.profile.profile.promotion_target_id
    )


def product_routing_operation_id(task_id: str) -> str:
    """One stable write identity for the one routing fact a task may record."""

    return "pt-route-" + fingerprint(
        {
            "purpose": PRODUCT_ROUTING_PURPOSE,
            "task_id": require_product_identifier(task_id, field="task_id"),
        }
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
    "PRODUCT_ROUTING_PURPOSE",
    "ProductAssembly",
    "ProductAssemblyService",
    "ProductPreflight",
    "ProductSourceResolver",
    "product_routing_operation_id",
    "require_assemblable",
]
