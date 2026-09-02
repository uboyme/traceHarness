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
from traceh.llm.retry import NO_MODEL_RETRY, ModelRetryPolicy
from traceh.product.assembly import ProductAssemblyService
from traceh.product.chat import (
    ProductChatSurface,
    ProductChatTurn,
    ProductTurnActions,
)
from traceh.product.context import ProductModelContext
from traceh.product.control import ProductTaskControlPlane
from traceh.product.errors import ProductInputError
from traceh.product.evidence import SessionEvidenceReader
from traceh.product.execution import (
    ProductExecutionHost,
    ProductWorkflowBindingResolver,
)
from traceh.product.inspection import ProductInspectionEvidenceReader
from traceh.product.memory import ProductTaskMemoryReader
from traceh.product.observation import (
    ProductObservationReader,
    ProductObservationSession,
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
from traceh.promotion.models import freeze_verification_plan, verifier_definition_digest
from traceh.promotion.service import PatchPromotionService
from traceh.session.event_feed import EventFeed, PublishingEventStore
from traceh.session.event_store import EventStore
from traceh.session.service import SessionService
from traceh.supervision.execution import durable_log_identity
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


@dataclass(frozen=True, slots=True)
class ProductReadModels:
    """One immutable pure-reader bundle over an explicitly supplied store."""

    store: EventStore
    artifact_reader: PatchArtifactReader
    evidence: ProductInspectionEvidenceReader
    observation: ProductObservationReader
    memory: ProductTaskMemoryReader
    verification_plan_digest: str
    promotion_target_id: str
    max_report_chars: int


def build_product_read_models(
    *,
    store: EventStore,
    host_profile: ProductHostProfile,
    artifact_cas: ArtifactCas,
    max_report_chars: int,
) -> ProductReadModels:
    """Build one internally bound chain of stateless, fresh-read projections."""

    if (
        type(host_profile) is not ProductHostProfile
        or type(host_profile.profile) is not ProductTaskProfile
    ):
        raise ProductInputError("product-host-profile-invalid", "host_profile")
    try:
        plan = freeze_verification_plan(host_profile.verification_plan)
    except Exception:
        raise ProductInputError(
            "product-verification-plan-invalid", "verification_plan"
        ) from None
    if plan.plan_id != host_profile.profile.verification_plan_id:
        raise ProductInputError(
            "product-verification-plan-mismatch", "verification_plan"
        )
    digest = verifier_definition_digest(plan)
    artifact_reader = PatchArtifactReader(store, artifact_cas)
    evidence = ProductInspectionEvidenceReader(
        store,
        artifact_reader,
        verification_plan=plan,
        verification_plan_digest=digest,
        promotion_target_id=host_profile.profile.promotion_target_id,
        max_patch_chars=max_report_chars,
    )
    observation = ProductObservationReader(
        store,
        evidence,
        promotion_target_id=host_profile.profile.promotion_target_id,
    )
    return ProductReadModels(
        store=store,
        artifact_reader=artifact_reader,
        evidence=evidence,
        observation=observation,
        memory=ProductTaskMemoryReader(store, observation),
        verification_plan_digest=digest,
        promotion_target_id=host_profile.profile.promotion_target_id,
        max_report_chars=max_report_chars,
    )


class ProductChatHost:
    """The UI-neutral Chat coordinator plus the services it exclusively owns."""

    __slots__ = (
        "_capture",
        "_closed",
        "_control",
        "_event_feed",
        "_promotion",
        "_observation",
        "_router",
        "_surface",
        "_tasks",
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
        observation: ProductObservationReader,
        event_feed: EventFeed,
    ) -> None:
        self._surface = surface
        self._control = control
        self._tasks = tasks
        self._router = router
        self._capture = capture
        self._promotion = promotion
        self._observation = observation
        self._event_feed = event_feed
        self._closed = False

    @property
    def control(self) -> ProductTaskControlPlane:
        """The host-side control plane this host already owns.

        ``ProductChatSurface`` coordinates typed operations without rendering
        them and is not a second authority.  The Line adapter and TUI
        both drive this same object, as does the non-interactive benchmark, so
        there remains one confirm/approve/reject/cancel path.  Nothing
        model-facing reaches it: requester Tools hold only ephemeral Turn
        actions or the pure read model.
        """

        return self._control

    @property
    def observation(self) -> ProductObservationReader:
        """Pure durable observation for Line/TUI adapters."""

        return self._observation

    def observe(self, task_id: str) -> ProductObservationSession:
        """Open an exact-stream observation handshake for a UI adapter."""

        return ProductObservationSession(self._observation, self._event_feed, task_id)

    async def prepare_turn(self, session_id: str, text: str) -> ProductChatTurn:
        return await self._surface.prepare_turn(session_id, text)

    async def resolve_turn(self, *args, **kwargs):
        return await self._surface.resolve_turn(*args, **kwargs)

    async def start(self, *args, **kwargs):
        return await self._surface.start(*args, **kwargs)

    async def discard_turn(self, *args, **kwargs) -> None:
        await self._surface.discard_turn(*args, **kwargs)

    async def execute_command(self, *args, **kwargs):
        return await self._surface.execute_command(*args, **kwargs)

    async def inspect(self, *args, **kwargs):
        return await self._surface.inspect(*args, **kwargs)

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
    sessions: SessionService,
    data_dir: Path,
    host_profile: ProductHostProfile,
    providers: Mapping[str, LlmProvider],
    workspace_provider: WorkspaceProvider,
    artifact_cas: ArtifactCas,
    promotion_targets: PromotionTargetResolver,
    capture_limits: PatchCaptureLimits,
    approver_id: str,
    max_report_chars: int,
    event_feed: EventFeed,
    actions: ProductTurnActions | None = None,
    read_models: ProductReadModels | None = None,
    model_retry_policy: ModelRetryPolicy = NO_MODEL_RETRY,
) -> ProductChatHost:
    """Build one explicit F3 host without inventing deployment defaults."""

    if event_feed is None:
        raise ProductInputError("product-event-feed-required", "event_feed")
    if not isinstance(store, PublishingEventStore):
        raise ProductInputError("product-event-store-not-publishing", "store")
    if store.feed is not event_feed:
        raise ProductInputError("product-event-feed-mismatch", "event_feed")
    if (
        type(sessions) is not SessionService
        or durable_log_identity(sessions.store) is not durable_log_identity(store)
    ):
        raise ProductInputError("product-context-store-mismatch", "sessions")

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
    if read_models is None:
        read_models = build_product_read_models(
            store=store,
            host_profile=host_profile,
            artifact_cas=artifact_cas,
            max_report_chars=max_report_chars,
        )
    if (
        type(read_models) is not ProductReadModels
        or read_models.store is not store
        or durable_log_identity(read_models.artifact_reader.store)
        is not durable_log_identity(store)
        or read_models.artifact_reader.cas is not artifact_cas
        or durable_log_identity(read_models.evidence.store)
        is not durable_log_identity(store)
        or read_models.evidence.artifact_reader is not read_models.artifact_reader
        or read_models.observation.store is not store
        or read_models.observation.evidence_reader is not read_models.evidence
        or durable_log_identity(read_models.memory.store)
        is not durable_log_identity(store)
        or read_models.memory.observation_reader is not read_models.observation
        or read_models.evidence.verification_plan_digest
        != resolved.verification_plan_digest
        or read_models.evidence.promotion_target_id
        != resolved.profile.promotion_target_id
        or read_models.observation.promotion_target_id
        != resolved.profile.promotion_target_id
        or read_models.evidence.max_patch_chars != max_report_chars
        or read_models.verification_plan_digest
        != resolved.verification_plan_digest
        or read_models.promotion_target_id != resolved.profile.promotion_target_id
        or read_models.max_report_chars != max_report_chars
    ):
        raise ProductInputError("product-read-models-mismatch", "read_models")
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
        retry_policy=model_retry_policy,
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
    artifact_reader = read_models.artifact_reader
    promotion = PatchPromotionService(
        store,
        artifact_reader,
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
    session_evidence = SessionEvidenceReader(store)
    tasks = ProductTaskService(
        store,
        sessions=session_evidence,
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
    evidence = read_models.evidence
    observation = read_models.observation
    surface = ProductChatSurface(
        control,
        actions,
        evidence,
        ProductModelContext(sessions, store, read_models.memory),
        approver_id=approver_id,
    )
    return ProductChatHost(
        surface=surface,
        control=control,
        tasks=tasks,
        router=router,
        capture=capture,
        promotion=promotion,
        observation=observation,
        event_feed=event_feed,
    )


__all__ = [
    "ProductChatHost",
    "ProductHostProfile",
    "ProductReadModels",
    "build_product_chat_host",
    "build_product_read_models",
]
