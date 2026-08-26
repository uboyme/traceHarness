"""Shared builders for the v0.7-F1 ProductTask fact tests.

Deliberately not named ``test_*`` so pytest does not collect it.

Everything here is an *example*, never a default: each value is spelled out at
the call site or defaulted here in the fixture, and no production module imports
this file. An architecture test asserts that.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

from traceh.api.budgets import BudgetLimits
from traceh.api.events import PendingEvent
from traceh.api.product import (
    ProductAssemblyReceipt,
    ProductPreflightBinding,
    ProductRoleProfile,
    ProductRouterProfile,
    ProductTaskProfile,
    ProductTaskProposal,
    ProposalConfirmation,
    RequestedTaskMode,
    ResolvedTaskMode,
    TaskModeSource,
)
from traceh.api.workflow import WorkflowStatus
from traceh.product import ProductTaskService, SessionEvidenceReader
from traceh.session.event_store import EventStore, InMemoryEventStore

ORIGIN_SESSION = "session-alpha"
ORIGIN_TURN = "turn-1"
ORIGIN_MESSAGE = "message-1"
PROPOSED_TURN = "turn-1"
CONFIRM_TURN = "turn-2"
CONFIRM_MESSAGE = "message-2"


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


__all__ = [
    "CONFIRM_MESSAGE",
    "CONFIRM_TURN",
    "ORIGIN_MESSAGE",
    "ORIGIN_SESSION",
    "ORIGIN_TURN",
    "PROPOSED_TURN",
    "Assembly",
    "Gate",
    "RecordingOwnership",
    "RecordingWorkflow",
    "build_assembly",
    "confirmation",
    "limits",
    "opened",
    "preflight",
    "profile",
    "proposal",
    "receipt",
    "seed_session",
]
