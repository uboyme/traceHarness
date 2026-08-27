"""Explicit F3 host assembly over the existing durable domains.

This module wires services; it does not create a second runtime or store.  Every
dependency that can change authority or external effects is supplied by the
host: Profile, Provider, Git source provider, Artifact CAS and promotion target
resolver.  The fixed Product topology remains in :mod:`traceh.product.topology`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from traceh.api.artifacts import ArtifactCas, PatchCaptureLimits
from traceh.api.llm import LlmProvider
from traceh.api.product import ProductTaskProfile
from traceh.api.promotion import PromotionTargetResolver, VerificationPlan
from traceh.api.workspaces import WorkspaceProvider
from traceh.artifacts.capture import PatchCaptureService
from traceh.artifacts.catalog import PatchArtifactCatalogReader
from traceh.artifacts.reader import PatchArtifactReader
from traceh.budgets.service import BudgetLedgerService
from traceh.budgets.supervision import (
    BudgetedActivationFactory,
    BudgetedAgentSupervisor,
    ProcessSlotAuthority,
)
from traceh.product.assembly import ProductAssemblyService
from traceh.product.chat import (
    ConfirmProductTaskTool,
    ProductChatSurface,
    ProductChatTurn,
    ProductTurnActions,
    ProposeProductTaskTool,
)
from traceh.product.control import ProductTaskControlPlane
from traceh.product.evidence import SessionEvidenceReader
from traceh.product.execution import (
    ProductExecutionHost,
    ProductWorkflowBindingResolver,
)
from traceh.product.registry import ProductProfileBinding, ProductProfileRegistry
from traceh.product.resources import ManagedProductTaskProvisioner, ProductResourceBindings
from traceh.product.router import ProductModeRouter, StrictTaskRoutingParser
from traceh.product.runtime import (
    BuiltinProductAssemblyResolver,
    ProductAgentRuntimeFactory,
    ProductRouterAgentResponder,
)
from traceh.product.service import ProductTaskService
from traceh.promotion.service import PatchPromotionService
from traceh.session.event_store import EventStore
from traceh.supervision.supervisor import ProcessAgentSupervisor
from traceh.workflow.execution import WorkflowServices
from traceh.workflow.service import WorkflowService
from traceh.workspaces.service import WorkspaceService
from traceh.workspaces.supervision import WorkspaceManagedAgentSupervisor


@dataclass(frozen=True, slots=True)
class ProductHostProfile:
    """One explicit registry entry selected for this Chat host."""

    profile_id: str
    profile: ProductTaskProfile
    verification_plan: VerificationPlan


class ProductChatHost:
    """The optional Chat surface plus the services it exclusively owns."""

    __slots__ = (
        "_capture",
        "_closed",
        "_control",
        "_promotion",
        "_router",
        "_surface",
        "_tasks",
        "confirm_tool",
        "propose_tool",
    )

    def __init__(
        self,
        *,
        surface: ProductChatSurface,
        control: ProductTaskControlPlane,
        tasks: ProductTaskService,
        router: ProductModeRouter,
        capture: PatchCaptureService,
        promotion: PatchPromotionService,
        actions: ProductTurnActions,
    ) -> None:
        self._surface = surface
        self._control = control
        self._tasks = tasks
        self._router = router
        self._capture = capture
        self._promotion = promotion
        self.propose_tool = ProposeProductTaskTool(actions)
        self.confirm_tool = ConfirmProductTaskTool(actions)
        self._closed = False

    @property
    def tools(self) -> tuple[object, object]:
        return (self.propose_tool, self.confirm_tool)

    @property
    def control(self) -> ProductTaskControlPlane:
        """The host-side control plane this host already owns.

        ``ProductChatSurface`` is the console rendering of these operations, not
        a second authority over them.  A non-console host - the benchmark in
        :mod:`traceh.evaluation` - drives the same object so there is one
        confirm/approve/reject/cancel path rather than two.  Nothing model-facing
        reaches it: the two Tools still hold only ``ProductTurnActions``.
        """

        return self._control

    async def prepare_turn(self, session_id: str, text: str) -> ProductChatTurn:
        return await self._surface.prepare_turn(session_id, text)

    async def finish_turn(self, *args, **kwargs) -> None:
        await self._surface.finish_turn(*args, **kwargs)

    async def discard_turn(self, *args, **kwargs) -> None:
        await self._surface.discard_turn(*args, **kwargs)

    async def handle_command(self, *args, **kwargs) -> bool:
        return await self._surface.handle_command(*args, **kwargs)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        failures: list[BaseException] = []
        for close in (
            self._control.aclose,
            self._router.aclose,
            self._tasks.aclose,
            self._promotion.aclose,
            self._capture.aclose,
        ):
            try:
                await close()
            except BaseException as error:
                failures.append(error)
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup("Product Chat host close failed", failures)


async def build_product_chat_host(
    *,
    store: EventStore,
    data_dir: Path,
    host_profile: ProductHostProfile,
    providers: Mapping[str, LlmProvider],
    workspace_provider: WorkspaceProvider,
    artifact_cas: ArtifactCas,
    promotion_targets: PromotionTargetResolver,
    capture_limits: PatchCaptureLimits,
    approver_id: str,
    max_report_chars: int,
    actions: ProductTurnActions | None = None,
) -> ProductChatHost:
    """Build one explicit F3 host without inventing deployment defaults."""

    resolver = BuiltinProductAssemblyResolver()
    registry = ProductProfileRegistry(
        (
            (
                host_profile.profile_id,
                ProductProfileBinding(
                    profile=host_profile.profile,
                    verification_plan=host_profile.verification_plan,
                ),
            ),
        ),
        assemblies=resolver,
    )
    resolved = await registry.resolve(host_profile.profile_id)
    bindings = ProductResourceBindings()
    workspaces = WorkspaceService(store, workspace_provider)
    budgets = BudgetLedgerService(store)
    runtime_factory = ProductAgentRuntimeFactory(
        store,
        workspaces,
        bindings,
        budgets,
        data_dir=data_dir,
        providers=providers,
    )
    slots = ProcessSlotAuthority(budgets)
    process = ProcessAgentSupervisor(
        store=store,
        factory=BudgetedActivationFactory(runtime_factory, slots),
    )
    workspace_supervisor = WorkspaceManagedAgentSupervisor(
        process,
        workspaces,
        workspace_policy=bindings,
    )
    supervisor = BudgetedAgentSupervisor(
        workspace_supervisor,
        budgets,
        child_budget_policy=bindings,
    )
    capture = PatchCaptureService(
        workspace_supervisor,
        workspaces,
        artifact_cas,
        limits=capture_limits,
    )
    promotion = PatchPromotionService(
        store,
        PatchArtifactReader(store, artifact_cas),
        promotion_targets,
        plan=resolved.verification_plan,
    )
    workflow_resolver = ProductWorkflowBindingResolver(
        supervisor, max_report_chars=max_report_chars
    )
    workflow = WorkflowService(
        store,
        WorkflowServices(
            supervisor=supervisor,
            capture=capture,
            promotion=promotion,
        ),
        workflow_resolver,
    )
    provisioner = ManagedProductTaskProvisioner(
        supervisor,
        budgets,
        workspaces,
        bindings,
        PatchArtifactCatalogReader(store),
    )
    execution = ProductExecutionHost(workflow, workflow_resolver, provisioner)
    tasks = ProductTaskService(
        store,
        sessions=SessionEvidenceReader(store),
        workflow=workflow,
        ownership=execution,
    )
    responder = ProductRouterAgentResponder(supervisor, bindings)
    router = ProductModeRouter(
        responder,
        StrictTaskRoutingParser(),
        profile=resolved.profile.router,
        assembly=resolved.router,
    )
    assembly = ProductAssemblyService(
        tasks,
        registry=registry,
        sources=workspace_provider,
        targets=promotion_targets,
        router=router,
    )
    control = ProductTaskControlPlane(
        tasks,
        assembly,
        execution,
        promotion,
        profile_id=host_profile.profile_id,
    )
    actions = ProductTurnActions() if actions is None else actions
    surface = ProductChatSurface(control, actions, approver_id=approver_id)
    return ProductChatHost(
        surface=surface,
        control=control,
        tasks=tasks,
        router=router,
        capture=capture,
        promotion=promotion,
        actions=actions,
    )


__all__ = [
    "ProductChatHost",
    "ProductHostProfile",
    "build_product_chat_host",
]
