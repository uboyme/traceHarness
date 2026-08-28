"""Shared builders for the v0.7-F1 fact-layer and v0.7-F2 assembly tests.

Deliberately not named ``test_*`` so pytest does not collect it.

Everything here is an *example*, never a default: each value is spelled out at
the call site or defaulted here in the fixture, and no production module imports
this file. An architecture test asserts that.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from pathlib import Path

from traceh.api.agents import AgentSpec
from traceh.api.budgets import BudgetLimits
from traceh.api.events import PendingEvent
from traceh.api.product import (
    ProductAssemblyReceipt,
    ProductPreflightBinding,
    ProductRole,
    ProductRoleProfile,
    ProductRouterProfile,
    ProductTaskProfile,
    ProductTaskProposal,
    ProposalConfirmation,
    RequestedTaskMode,
    ResolvedTaskMode,
    TaskModeSource,
)
from traceh.api.promotion import (
    PromotionTarget,
    VerificationPlan,
    VerifierCommand,
    VerifierEnvironmentPolicy,
)
from traceh.api.workflow import WorkflowStatus
from traceh.api.workspaces import WorkspaceAccess, WorkspaceSourceSnapshot
from traceh.product import (
    ProductAssemblyService,
    ProductModeRouter,
    ProductProfileBinding,
    ProductProfileRegistry,
    ProductTaskService,
    ResolvedAgentAssembly,
    RouterResponse,
    SessionEvidenceReader,
    StrictTaskRoutingParser,
)
from traceh.product.errors import ProductProfileError
from traceh.session.event_store import EventStore, InMemoryEventStore

ORIGIN_SESSION = "session-alpha"
ORIGIN_TURN = "turn-1"
ORIGIN_MESSAGE = "message-1"
PROPOSED_TURN = "turn-1"
CONFIRM_TURN = "turn-2"
CONFIRM_MESSAGE = "message-2"

PROFILE_ID = "profile-alpha"
SOURCE_FINGERPRINT = "a1" * 32
SOURCE_BASE_REVISION = "b2" * 20
TARGET_FINGERPRINT = "c3" * 32
TARGET_EXPECTED_REVISION = "d4" * 20
ROUTER_AGENT = "agent-router"
ROUTER_SESSION = "session-router"
ROUTING_SUMMARY = "add a configuration check to the tracked module"


def limits(**overrides: int | None) -> BudgetLimits:
    base: dict[str, int | None] = {
        "max_tokens": 60_000,
        "max_steps": 200,
        "max_tool_calls": 400,
        "max_wall_milliseconds": 900_000,
        "max_children": 3,
        "max_depth": 1,
        "max_processes": 3,
    }
    base.update(overrides)
    return BudgetLimits(**base)  # type: ignore[arg-type]


def profile() -> ProductTaskProfile:
    def role(name: str) -> ProductRoleProfile:
        return ProductRoleProfile(
            preset=f"preset-{name}",
            capability_grants=("read-workspace",),
            max_output_tokens=4_096,
            budget=limits(max_children=0, max_depth=0),
        )

    return ProductTaskProfile(
        profile_version=1,
        default_mode=RequestedTaskMode.AUTO,
        provider_id="registered-provider",
        model_id="registered-model",
        parent=role("parent"),
        reviewer=role("reviewer"),
        coder=role("coder"),
        router=ProductRouterProfile(
            preset="preset-router",
            max_output_tokens=256,
            budget=limits(max_tokens=2_000, max_steps=2, max_tool_calls=0),
            timeout_milliseconds=30_000,
            max_response_bytes=2_048,
        ),
        task_budget=limits(),
        source_id="registered-source",
        source_revision="main",
        verification_plan_id="registered-plan",
        promotion_target_id="registered-target",
    )


def preflight(**overrides: str) -> ProductPreflightBinding:
    binding = ProductPreflightBinding(
        profile_digest=profile().digest,
        role_assembly_digest="1" * 64,
        router_assembly_digest="2" * 64,
        repository_fingerprint="3" * 64,
        base_revision="4" * 40,
        verification_plan_digest="5" * 64,
        promotion_target_fingerprint="6" * 64,
        promotion_target_ref="refs/heads/main",
        promotion_expected_revision="7" * 40,
    )
    return replace(binding, **overrides) if overrides else binding


def receipt(
    *,
    binding: ProductPreflightBinding | None = None,
    mode: ResolvedTaskMode = ResolvedTaskMode.SINGLE,
    definition_hash: str = "a" * 64,
) -> ProductAssemblyReceipt:
    return ProductAssemblyReceipt(
        preflight=binding if binding is not None else preflight(),
        resolved_mode=mode,
        workflow_definition_hash=definition_hash,
    )


def proposal(
    *,
    requested_mode: RequestedTaskMode = RequestedTaskMode.SINGLE,
    mode_source: TaskModeSource = TaskModeSource.CONFIRMED_PROPOSAL,
    binding: ProductPreflightBinding | None = None,
    origin_turn_id: str = ORIGIN_TURN,
    proposed_turn_id: str = PROPOSED_TURN,
    session_id: str = ORIGIN_SESSION,
) -> ProductTaskProposal:
    return ProductTaskProposal(
        proposal_id="proposal-1",
        origin_session_id=session_id,
        origin_turn_id=origin_turn_id,
        origin_message_id=ORIGIN_MESSAGE,
        proposed_turn_id=proposed_turn_id,
        requirement_digest="9" * 64,
        requested_mode=requested_mode,
        mode_source=mode_source,
        preflight=binding if binding is not None else preflight(),
    )


def confirmation(
    *,
    session_id: str = ORIGIN_SESSION,
    turn_id: str = CONFIRM_TURN,
    message_id: str = CONFIRM_MESSAGE,
    proposal_id: str = "proposal-1",
) -> ProposalConfirmation:
    return ProposalConfirmation(
        proposal_id=proposal_id,
        confirming_session_id=session_id,
        confirming_turn_id=turn_id,
        confirming_message_id=message_id,
    )


async def seed_session(
    store: EventStore,
    *,
    session_id: str = ORIGIN_SESSION,
    messages: tuple[tuple[str, str], ...] = (
        (ORIGIN_MESSAGE, ORIGIN_TURN),
        (CONFIRM_MESSAGE, CONFIRM_TURN),
    ),
    created: bool = True,
    claim: bool = True,
    source: str = "user",
) -> None:
    """Write the real Session facts a confirmation has to be proven against."""

    stream = f"session:{session_id}"
    seq = 0
    if created:
        await store.append(
            stream,
            expected_seq=seq,
            events=(
                PendingEvent(
                    type="session/created",
                    data={
                        "session_id": session_id,
                        "workspace": "workspace-fixture",
                        "metadata": {},
                    },
                ),
            ),
        )
        seq += 1
    for message_id, turn_id in messages:
        await store.append(
            stream,
            expected_seq=seq,
            events=(
                PendingEvent(
                    type="inbox/accepted",
                    data={
                        "message_id": message_id,
                        "source": source,
                        "content": "example requirement",
                        "target": "new_turn",
                    },
                ),
            ),
        )
        seq += 1
        if not claim:
            continue
        await store.append(
            stream,
            expected_seq=seq,
            events=(
                PendingEvent(
                    type="inbox/claimed",
                    data={"message_id": message_id, "turn_id": turn_id},
                ),
            ),
        )
        seq += 1
        await store.append(
            stream,
            expected_seq=seq,
            events=(
                PendingEvent(
                    type="turn/start",
                    data={"turn_id": turn_id, "message_id": message_id},
                ),
            ),
        )
        seq += 1
        await store.append(
            stream,
            expected_seq=seq,
            events=(
                PendingEvent(
                    type="turn/end",
                    data={"turn_id": turn_id, "reason": "completed"},
                ),
            ),
        )
        seq += 1


class RecordingWorkflow:
    """A Workflow state source whose answer a test can change between reads."""

    def __init__(
        self, store: EventStore, status: WorkflowStatus | None = None
    ) -> None:
        self.store = store
        self.status_value = status
        self.reads = 0

    async def workflow_status(self, run_id: str) -> WorkflowStatus | None:
        del run_id
        self.reads += 1
        return self.status_value


class RecordingOwnership:
    """An ownership source whose answer a test can change between reads."""

    def __init__(self, owned: bool = True) -> None:
        self.owned = owned
        self.reads = 0

    def owns_task(self, task_id: str) -> bool:
        del task_id
        self.reads += 1
        return self.owned


@dataclass(slots=True)
class Assembly:
    store: EventStore
    service: ProductTaskService
    workflow: RecordingWorkflow
    ownership: RecordingOwnership

    async def aclose(self) -> None:
        await self.service.aclose()


async def build_assembly(
    *,
    store: EventStore | None = None,
    workflow_status: WorkflowStatus | None = None,
    owned: bool = True,
    seed: bool = True,
) -> Assembly:
    store = store if store is not None else InMemoryEventStore()
    if seed:
        await seed_session(store)
    workflow = RecordingWorkflow(store, workflow_status)
    ownership = RecordingOwnership(owned)
    service = ProductTaskService(
        store,
        sessions=SessionEvidenceReader(store),
        workflow=workflow,
        ownership=ownership,
    )
    return Assembly(
        store=store, service=service, workflow=workflow, ownership=ownership
    )


async def opened(assembly: Assembly, *, task_id: str = "task-1", **kwargs: object):
    return await assembly.service.open_task(
        task_id=task_id,
        operation_id=f"{task_id}-open",
        proposal=proposal(**kwargs),  # type: ignore[arg-type]
        confirmation=confirmation(),
    )


class Gate:
    """A deterministic two-way rendezvous, so no test guesses at timing."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def wait(self) -> None:
        self.entered.set()
        await self.release.wait()


# --------------------------------------------------------------- v0.7-F2


def verification_plan(
    *, plan_id: str = "registered-plan", plan_version: int = 1
) -> VerificationPlan:
    return VerificationPlan(
        plan_id=plan_id,
        plan_version=plan_version,
        commands=(
            VerifierCommand(
                command_id="tracked-check",
                argv=("python", "-c", "raise SystemExit(0)"),
                timeout_ms=60_000,
            ),
        ),
        environment=VerifierEnvironmentPolicy(
            policy_id="verifier-env",
            passthrough=("PATH",),
            overrides=(("PYTHONIOENCODING", "utf-8"),),
        ),
        max_output_bytes=1024 * 1024,
        protocol_version=1,
    )


def resolved_role(
    role: ProductRole,
    *,
    access: WorkspaceAccess | None = None,
    preset: str | None = None,
    grants: tuple[str, ...] | None = None,
    provider_id: str = "registered-provider",
    model_id: str = "registered-model",
    tools: tuple[str, ...] = ("read-file",),
    prompts: tuple[str, ...] = ("prompt-role",),
    policies: tuple[str, ...] = ("policy-role",),
) -> ResolvedAgentAssembly:
    slot = profile().role_profile(role)
    return ResolvedAgentAssembly(
        spec=AgentSpec(
            preset=slot.preset if preset is None else preset,
            workspace_id=f"workspace-{role.value}",
            capability_grants=(
                slot.capability_grants if grants is None else grants
            ),
        ),
        provider_id=provider_id,
        model_id=model_id,
        tool_ids=tools,
        prompt_ids=prompts,
        policy_ids=policies,
        workspace_access=role.workspace_access if access is None else access,
    )


def resolved_router(
    *,
    tools: tuple[str, ...] = (),
    grants: tuple[str, ...] = (),
    access: WorkspaceAccess = WorkspaceAccess.READ_ONLY,
    preset: str | None = None,
) -> ResolvedAgentAssembly:
    router = profile().router
    return ResolvedAgentAssembly(
        spec=AgentSpec(
            preset=router.preset if preset is None else preset,
            workspace_id="workspace-router",
            capability_grants=grants,
        ),
        provider_id="registered-provider",
        model_id="registered-model",
        tool_ids=tools,
        prompt_ids=("prompt-router",),
        policy_ids=(),
        workspace_access=access,
    )


@dataclass(slots=True)
class RecordingAssemblies:
    """A host resolver whose answers a test controls role by role."""

    roles: dict[ProductRole, ResolvedAgentAssembly] = field(
        default_factory=lambda: {role: resolved_role(role) for role in ProductRole}
    )
    router: ResolvedAgentAssembly = field(default_factory=resolved_router)
    unavailable: frozenset[ProductRole] = frozenset()
    calls: list[str] = field(default_factory=list)

    async def role_assembly(self, *, role, profile, provider_id, model_id):
        del profile, provider_id, model_id
        self.calls.append(role.value)
        if role in self.unavailable:
            raise ProductProfileError("product-assembly-unavailable", role.value)
        return self.roles[role]

    async def router_assembly(self, *, profile, provider_id, model_id):
        del profile, provider_id, model_id
        self.calls.append("router")
        return self.router


@dataclass(slots=True)
class RecordingSources:
    """A source resolver whose answer a test can move between reads."""

    fingerprint: str = SOURCE_FINGERPRINT
    revision: str = SOURCE_BASE_REVISION
    source_id: str | None = None
    reads: int = 0

    async def resolve_source(
        self, source_id: str, revision: str
    ) -> WorkspaceSourceSnapshot:
        self.reads += 1
        return WorkspaceSourceSnapshot(
            source_id=self.source_id if self.source_id is not None else source_id,
            requested_revision=revision,
            repository_fingerprint=self.fingerprint,
            base_revision=self.revision,
        )


@dataclass(slots=True)
class RecordingTargets:
    """A promotion target resolver whose ref a test can advance."""

    fingerprint: str = TARGET_FINGERPRINT
    target_ref: str = "refs/heads/main"
    revision: str = TARGET_EXPECTED_REVISION
    reads: int = 0

    async def resolve(self, target_id: str) -> PromotionTarget:
        self.reads += 1
        return PromotionTarget(
            target_id=target_id,
            repository_path=Path("/product-promotion-target"),
            repository_fingerprint=self.fingerprint,
            target_ref=self.target_ref,
            expected_revision=self.revision,
        )


@dataclass(slots=True)
class ScriptedResponder:
    """A router Agent stand-in: text in, one bounded answer out."""

    text: str = '{"mode": "multi", "reason": "three roles help here"}'
    router_agent_id: str = ROUTER_AGENT
    routing_session_id: str = ROUTER_SESSION
    gate: Gate | None = None
    failure: BaseException | None = None
    calls: list[str] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)

    async def respond(self, summary: str, *, task_id: str) -> RouterResponse:
        self.calls.append(summary)
        self.task_ids.append(task_id)
        if self.gate is not None:
            await self.gate.wait()
        if self.failure is not None:
            raise self.failure
        return RouterResponse(
            text=self.text,
            router_agent_id=self.router_agent_id,
            routing_session_id=self.routing_session_id,
        )


def registry(
    *,
    assemblies: RecordingAssemblies | None = None,
    task_profile: ProductTaskProfile | None = None,
    plan: VerificationPlan | None = None,
    profile_id: str = PROFILE_ID,
    extra: tuple[tuple[str, ProductProfileBinding], ...] = (),
) -> ProductProfileRegistry:
    binding = ProductProfileBinding(
        profile=profile() if task_profile is None else task_profile,
        verification_plan=verification_plan() if plan is None else plan,
    )
    return ProductProfileRegistry(
        ((profile_id, binding), *extra),
        assemblies=RecordingAssemblies() if assemblies is None else assemblies,
    )


def mode_router(
    responder: ScriptedResponder | None = None,
    *,
    task_profile: ProductTaskProfile | None = None,
    assembly: ResolvedAgentAssembly | None = None,
) -> ProductModeRouter:
    return ProductModeRouter(
        ScriptedResponder() if responder is None else responder,
        StrictTaskRoutingParser(),
        profile=(profile() if task_profile is None else task_profile).router,
        assembly=resolved_router() if assembly is None else assembly,
    )


@dataclass(slots=True)
class Plan:
    """One complete F2 host assembly built on one F1 writer."""

    base: Assembly
    registry: ProductProfileRegistry
    assemblies: RecordingAssemblies
    sources: RecordingSources
    targets: RecordingTargets
    responder: ScriptedResponder
    router: ProductModeRouter
    service: ProductAssemblyService

    @property
    def store(self) -> EventStore:
        return self.base.store

    @property
    def tasks(self) -> ProductTaskService:
        return self.base.service

    async def aclose(self) -> None:
        await self.router.aclose()
        await self.base.aclose()


async def build_plan(
    *,
    assemblies: RecordingAssemblies | None = None,
    sources: RecordingSources | None = None,
    targets: RecordingTargets | None = None,
    responder: ScriptedResponder | None = None,
    task_profile: ProductTaskProfile | None = None,
    plan: VerificationPlan | None = None,
) -> Plan:
    base = await build_assembly()
    assemblies = RecordingAssemblies() if assemblies is None else assemblies
    sources = RecordingSources() if sources is None else sources
    targets = RecordingTargets() if targets is None else targets
    responder = ScriptedResponder() if responder is None else responder
    profiles = registry(
        assemblies=assemblies, task_profile=task_profile, plan=plan
    )
    resolved = await profiles.resolve(PROFILE_ID)
    router = mode_router(
        responder,
        task_profile=task_profile,
        assembly=resolved.router,
    )
    service = ProductAssemblyService(
        base.service,
        registry=profiles,
        sources=sources,
        targets=targets,
        router=router,
    )
    return Plan(
        base=base,
        registry=profiles,
        assemblies=assemblies,
        sources=sources,
        targets=targets,
        responder=responder,
        router=router,
        service=service,
    )


async def open_for_plan(
    plan: Plan,
    *,
    task_id: str = "task-1",
    requested_mode: RequestedTaskMode = RequestedTaskMode.SINGLE,
    profile_id: str = PROFILE_ID,
):
    """Open a task against the binding this host currently resolves.

    The preflight comes from the real resolver rather than a literal, so the
    ``preflight_digest`` a person confirmed is exactly the one a later assembly
    has to reproduce.
    """

    current = await plan.service.preflight(profile_id)
    return await plan.tasks.open_task(
        task_id=task_id,
        operation_id=f"{task_id}-open",
        proposal=proposal(requested_mode=requested_mode, binding=current.binding),
        confirmation=confirmation(),
    )


__all__ = [
    "CONFIRM_MESSAGE",
    "CONFIRM_TURN",
    "ORIGIN_MESSAGE",
    "ORIGIN_SESSION",
    "ORIGIN_TURN",
    "PROFILE_ID",
    "PROPOSED_TURN",
    "ROUTER_AGENT",
    "ROUTER_SESSION",
    "ROUTING_SUMMARY",
    "SOURCE_BASE_REVISION",
    "SOURCE_FINGERPRINT",
    "TARGET_EXPECTED_REVISION",
    "TARGET_FINGERPRINT",
    "Assembly",
    "Gate",
    "Plan",
    "RecordingAssemblies",
    "RecordingOwnership",
    "RecordingSources",
    "RecordingTargets",
    "RecordingWorkflow",
    "ScriptedResponder",
    "build_assembly",
    "build_plan",
    "confirmation",
    "limits",
    "mode_router",
    "open_for_plan",
    "opened",
    "preflight",
    "profile",
    "proposal",
    "receipt",
    "registry",
    "resolved_role",
    "resolved_router",
    "seed_session",
    "verification_plan",
]
