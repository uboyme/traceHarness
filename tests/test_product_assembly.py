"""v0.7-F2: one confirmed task becomes one exact plan, or nothing at all.

Assembly is the last step before execution, so most of these cases are about
refusing: a drifted binding, a mode nobody may decide twice, a router asked
before the plan was known to be buildable, and a receipt that does not rest on
the preflight a person actually confirmed.

Nothing here starts a Workflow. Several cases assert that by reading the
ProductTask stream afterwards and finding no ``product/task-started``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

import pytest
from product_fixtures import (
    PROFILE_ID,
    ROUTER_AGENT,
    ROUTER_SESSION,
    ROUTING_SUMMARY,
    Gate,
    RecordingAssemblies,
    RecordingSources,
    RecordingTargets,
    ScriptedResponder,
    build_plan,
    mode_router,
    open_for_plan,
    profile,
    registry,
    resolved_role,
    resolved_router,
    verification_plan,
)
from promotion_fixtures import build_source_repository, make_bare_target

from traceh.api.product import (
    PRODUCT_TASK_ROUTED,
    PRODUCT_TASK_STARTED,
    ProductRole,
    RequestedTaskMode,
    ResolvedTaskMode,
)
from traceh.api.promotion import PromotionTargetBinding
from traceh.product import (
    ProductAssemblyService,
    ProductInputError,
    ProductOperationConflictError,
    ProductProfileError,
    ProductRoutingError,
    ProductServiceClosedError,
    ProductStateError,
    RouterResponse,
    product_definition_hash,
    product_role_node_id,
    product_task_stream,
)
from traceh.promotion.local_git import LocalBareGitPromotionTargets
from traceh.workflow.models import workflow_definition_hash
from traceh.workspaces.local_git import LocalGitWorkspaceProvider

SINGLE_ANSWER = '{"mode": "single", "reason": "one file, one change"}'
MULTI_ANSWER = '{"mode": "multi", "reason": "worth a plan review"}'


async def event_types(plan, task_id: str = "task-1") -> list[str]:
    events = await plan.store.read(product_task_stream(task_id))
    return [event.type for event in events]


# ------------------------------------------------------------------- preflight


async def test_the_same_facts_bind_to_the_same_preflight() -> None:
    plan = await build_plan()
    first = await plan.service.preflight(PROFILE_ID)
    second = await plan.service.preflight(PROFILE_ID)
    assert first.binding == second.binding
    assert first.digest == second.digest
    assert plan.sources.reads == 2 and plan.targets.reads == 2
    await plan.aclose()


async def test_the_binding_carries_identities_and_never_a_path() -> None:
    """Everything here is safe to render to a person and to keep in history."""

    plan = await build_plan()
    preflight = await plan.service.preflight(PROFILE_ID)
    assert preflight.binding.base_revision == plan.sources.revision
    assert preflight.binding.promotion_expected_revision == plan.targets.revision
    assert preflight.binding.promotion_target_ref == plan.targets.target_ref
    assert all(
        type(getattr(preflight.binding, item.name)) is str
        for item in fields(preflight.binding)
    )
    assert "product-promotion-target" not in repr(preflight.binding)
    await plan.aclose()


async def test_a_source_or_target_that_answers_another_question_is_refused() -> None:
    plan = await build_plan(sources=RecordingSources(source_id="another-source"))
    with pytest.raises(ProductProfileError) as caught:
        await plan.service.preflight(PROFILE_ID)
    assert caught.value.code == "product-source-mismatch"
    await plan.aclose()

    plan = await build_plan(sources=RecordingSources(revision="not-a-revision"))
    with pytest.raises(ProductInputError):
        await plan.service.preflight(PROFILE_ID)
    await plan.aclose()


async def test_the_shipped_git_resolvers_satisfy_these_seams(tmp_path: Path) -> None:
    """The narrow protocols are the ones the real implementations already have."""

    source, _ = build_source_repository(tmp_path / "source")
    bare = make_bare_target(source, tmp_path / "target.git")
    task_profile = replace(
        profile(), source_id="trusted-source", promotion_target_id="main-target"
    )
    plan = await build_plan(task_profile=task_profile)
    service = ProductAssemblyService(
        plan.tasks,
        registry=registry(
            assemblies=plan.assemblies, task_profile=task_profile
        ),
        sources=LocalGitWorkspaceProvider(
            managed_root=tmp_path / "managed", sources={"trusted-source": source}
        ),
        targets=LocalBareGitPromotionTargets(
            targets={
                "main-target": PromotionTargetBinding(
                    repository_path=bare, target_ref="refs/heads/main"
                )
            }
        ),
        router=plan.router,
    )
    preflight = await service.preflight(PROFILE_ID)
    assert len(preflight.binding.base_revision) == 40
    assert preflight.binding.base_revision == (
        preflight.binding.promotion_expected_revision
    )
    assert preflight.binding.repository_fingerprint != (
        preflight.binding.promotion_target_fingerprint
    )
    await plan.aclose()


# ------------------------------------------------------------ explicit modes


async def test_an_explicit_mode_never_reaches_the_router() -> None:
    for requested, resolved in (
        (RequestedTaskMode.SINGLE, ResolvedTaskMode.SINGLE),
        (RequestedTaskMode.MULTI, ResolvedTaskMode.MULTI),
    ):
        plan = await build_plan()
        await open_for_plan(plan, requested_mode=requested)
        assembly = await plan.service.assemble(
            task_id="task-1", profile_id=PROFILE_ID
        )
        assert assembly.resolved_mode is resolved
        assert plan.responder.calls == []
        assert await event_types(plan) == ["product/task-opened"]
        await plan.aclose()


async def test_the_receipt_rests_on_the_preflight_that_was_confirmed() -> None:
    plan = await build_plan()
    summary = await open_for_plan(plan)
    assembly = await plan.service.assemble(task_id="task-1", profile_id=PROFILE_ID)
    assert assembly.receipt.binds(summary.preflight_digest)
    assert assembly.receipt.preflight == assembly.preflight.binding
    await plan.aclose()


async def test_the_recorded_hash_comes_from_the_definition_that_would_run() -> None:
    plan = await build_plan()
    await open_for_plan(plan, requested_mode=RequestedTaskMode.MULTI)
    assembly = await plan.service.assemble(task_id="task-1", profile_id=PROFILE_ID)
    assert assembly.receipt.workflow_definition_hash == workflow_definition_hash(
        assembly.definition
    )
    assert assembly.receipt.workflow_definition_hash == product_definition_hash(
        ResolvedTaskMode.MULTI, promotion_target_id=profile().promotion_target_id
    )
    assert [node.node_id for node in assembly.definition.nodes][:3] == [
        product_role_node_id(ProductRole.PARENT),
        product_role_node_id(ProductRole.REVIEWER),
        product_role_node_id(ProductRole.CODER),
    ]
    await plan.aclose()


async def test_assembling_the_same_facts_twice_gives_the_same_receipt() -> None:
    plan = await build_plan()
    await open_for_plan(plan)
    first = await plan.service.assemble(task_id="task-1", profile_id=PROFILE_ID)
    second = await plan.service.assemble(task_id="task-1", profile_id=PROFILE_ID)
    assert first.receipt == second.receipt
    assert first.receipt.digest == second.receipt.digest
    await plan.aclose()


# ------------------------------------------------------------------ auto mode


async def test_auto_routes_once_and_records_that_one_decision() -> None:
    plan = await build_plan(responder=ScriptedResponder(text=MULTI_ANSWER))
    await open_for_plan(plan, requested_mode=RequestedTaskMode.AUTO)
    assembly = await plan.service.assemble(
        task_id="task-1", profile_id=PROFILE_ID, routing_summary=ROUTING_SUMMARY
    )
    assert assembly.resolved_mode is ResolvedTaskMode.MULTI
    assert plan.responder.calls == [ROUTING_SUMMARY]
    assert await event_types(plan) == ["product/task-opened", PRODUCT_TASK_ROUTED]
    routed = (await plan.store.read(product_task_stream("task-1")))[1]
    assert routed.data["resolved_mode"] == "multi"
    assert routed.data["router_agent_id"] == ROUTER_AGENT
    assert routed.data["routing_session_id"] == ROUTER_SESSION
    assert routed.data["reason_display"] == "worth a plan review"
    await plan.aclose()


async def test_a_second_assembly_reuses_the_one_durable_routing_result() -> None:
    """Routing again would be a second free decision about a settled question."""

    plan = await build_plan(responder=ScriptedResponder(text=SINGLE_ANSWER))
    await open_for_plan(plan, requested_mode=RequestedTaskMode.AUTO)
    first = await plan.service.assemble(
        task_id="task-1", profile_id=PROFILE_ID, routing_summary=ROUTING_SUMMARY
    )
    plan.responder.text = MULTI_ANSWER
    second = await plan.service.assemble(
        task_id="task-1", profile_id=PROFILE_ID, routing_summary=ROUTING_SUMMARY
    )
    assert second.resolved_mode is first.resolved_mode is ResolvedTaskMode.SINGLE
    assert len(plan.responder.calls) == 1
    assert await event_types(plan) == ["product/task-opened", PRODUCT_TASK_ROUTED]
    await plan.aclose()


async def test_the_reason_never_overrules_the_enum_beside_it() -> None:
    plan = await build_plan(
        responder=ScriptedResponder(
            text='{"mode": "single", "reason": "definitely needs multi"}'
        )
    )
    await open_for_plan(plan, requested_mode=RequestedTaskMode.AUTO)
    assembly = await plan.service.assemble(
        task_id="task-1", profile_id=PROFILE_ID, routing_summary=ROUTING_SUMMARY
    )
    assert assembly.resolved_mode is ResolvedTaskMode.SINGLE
    assert len(assembly.definition.nodes) == 3
    await plan.aclose()


async def test_a_refused_answer_leaves_no_routing_fact_behind() -> None:
    for text in (
        '{"mode": "auto", "reason": null}',
        '{"mode": "single", "reason": null, "confidence": 1}',
        "single",
        '{"mode": "single", "reason": null}{"mode": "multi", "reason": null}',
    ):
        plan = await build_plan(responder=ScriptedResponder(text=text))
        await open_for_plan(plan, requested_mode=RequestedTaskMode.AUTO)
        with pytest.raises(ProductRoutingError):
            await plan.service.assemble(
                task_id="task-1",
                profile_id=PROFILE_ID,
                routing_summary=ROUTING_SUMMARY,
            )
        assert await event_types(plan) == ["product/task-opened"]
        await plan.aclose()


async def test_auto_without_a_question_fails_before_the_router() -> None:
    plan = await build_plan()
    await open_for_plan(plan, requested_mode=RequestedTaskMode.AUTO)
    with pytest.raises(ProductRoutingError) as caught:
        await plan.service.assemble(task_id="task-1", profile_id=PROFILE_ID)
    assert caught.value.code == "product-router-summary-missing"
    assert plan.responder.calls == []
    await plan.aclose()


async def test_an_unavailable_role_costs_no_routing_tokens() -> None:
    """All three roles are one binding, so any of them failing precedes routing."""

    plan = await build_plan()
    await open_for_plan(plan, requested_mode=RequestedTaskMode.AUTO)
    plan.assemblies.unavailable = frozenset({ProductRole.REVIEWER})
    with pytest.raises(ProductProfileError) as caught:
        await plan.service.assemble(
            task_id="task-1", profile_id=PROFILE_ID, routing_summary=ROUTING_SUMMARY
        )
    assert caught.value.code == "product-assembly-unavailable"
    assert plan.responder.calls == []
    assert await event_types(plan) == ["product/task-opened"]
    await plan.aclose()


# ----------------------------------------------------------------------- drift


async def test_a_moved_source_revision_fails_closed() -> None:
    sources = RecordingSources()
    plan = await build_plan(sources=sources)
    await open_for_plan(plan, requested_mode=RequestedTaskMode.AUTO)
    sources.revision = "e5" * 20
    with pytest.raises(ProductStateError) as caught:
        await plan.service.assemble(
            task_id="task-1", profile_id=PROFILE_ID, routing_summary=ROUTING_SUMMARY
        )
    assert caught.value.code == "product-preflight-drifted"
    assert plan.responder.calls == []
    assert await event_types(plan) == ["product/task-opened"]
    await plan.aclose()


async def test_a_promotion_target_that_moved_is_not_silently_rebased() -> None:
    """Two long tasks on one branch cannot both promote without a person."""

    targets = RecordingTargets()
    plan = await build_plan(targets=targets)
    await open_for_plan(plan)
    targets.revision = "f6" * 20
    with pytest.raises(ProductStateError) as caught:
        await plan.service.assemble(task_id="task-1", profile_id=PROFILE_ID)
    assert caught.value.code == "product-preflight-drifted"
    await plan.aclose()


async def test_a_target_id_rebound_to_another_ref_is_drift() -> None:
    """The repository and commit can agree while the branch authority differs."""

    targets = RecordingTargets()
    plan = await build_plan(targets=targets)
    await open_for_plan(plan)
    targets.target_ref = "refs/heads/release"
    with pytest.raises(ProductStateError) as caught:
        await plan.service.assemble(task_id="task-1", profile_id=PROFILE_ID)
    assert caught.value.code == "product-preflight-drifted"
    await plan.aclose()


async def test_auto_uses_only_the_router_bound_by_preflight() -> None:
    """The confirmed Router envelope cannot be paper over another live router."""

    confirmed = profile()
    plan = await build_plan(task_profile=confirmed)
    await open_for_plan(plan, requested_mode=RequestedTaskMode.AUTO)
    wider = replace(
        confirmed,
        router=replace(confirmed.router, max_response_bytes=1_000_000),
    )
    other_responder = ScriptedResponder(text=MULTI_ANSWER)
    mismatched_router = mode_router(
        other_responder,
        task_profile=wider,
        assembly=(await plan.registry.resolve(PROFILE_ID)).router,
    )
    service = ProductAssemblyService(
        plan.tasks,
        registry=plan.registry,
        sources=plan.sources,
        targets=plan.targets,
        router=mismatched_router,
    )
    with pytest.raises(ProductProfileError) as caught:
        await service.assemble(
            task_id="task-1",
            profile_id=PROFILE_ID,
            routing_summary=ROUTING_SUMMARY,
        )
    assert caught.value.code == "product-router-binding-mismatch"
    assert other_responder.calls == []
    await mismatched_router.aclose()
    await plan.aclose()


async def test_auto_refuses_another_resolved_router_assembly() -> None:
    """Matching bounds do not make another model or composition the same Router."""

    confirmed = profile()
    plan = await build_plan(task_profile=confirmed)
    await open_for_plan(plan, requested_mode=RequestedTaskMode.AUTO)
    other_responder = ScriptedResponder(text=MULTI_ANSWER)
    other_assembly = replace(resolved_router(), model_id="other-registered-model")
    mismatched_router = mode_router(
        other_responder,
        task_profile=confirmed,
        assembly=other_assembly,
    )
    service = ProductAssemblyService(
        plan.tasks,
        registry=plan.registry,
        sources=plan.sources,
        targets=plan.targets,
        router=mismatched_router,
    )
    with pytest.raises(ProductProfileError) as caught:
        await service.assemble(
            task_id="task-1",
            profile_id=PROFILE_ID,
            routing_summary=ROUTING_SUMMARY,
        )
    assert caught.value.code == "product-router-binding-mismatch"
    assert other_responder.calls == []
    await mismatched_router.aclose()
    await plan.aclose()


async def test_a_changed_verification_plan_fails_closed() -> None:
    plan = await build_plan()
    await open_for_plan(plan)
    moved = ProductAssemblyService(
        plan.tasks,
        registry=registry(
            assemblies=plan.assemblies, plan=verification_plan(plan_version=2)
        ),
        sources=plan.sources,
        targets=plan.targets,
        router=plan.router,
    )
    with pytest.raises(ProductStateError) as caught:
        await moved.assemble(task_id="task-1", profile_id=PROFILE_ID)
    assert caught.value.code == "product-preflight-drifted"
    await plan.aclose()


async def test_a_registry_rebinding_invalidates_a_confirmed_task() -> None:
    """Every name is unchanged; what they resolve to is not."""

    plan = await build_plan()
    await open_for_plan(plan)
    rebound = RecordingAssemblies(
        roles={
            role: resolved_role(role, tools=("read-file", "apply-patch"))
            for role in ProductRole
        }
    )
    moved = ProductAssemblyService(
        plan.tasks,
        registry=registry(assemblies=rebound),
        sources=plan.sources,
        targets=plan.targets,
        router=plan.router,
    )
    with pytest.raises(ProductStateError) as caught:
        await moved.assemble(task_id="task-1", profile_id=PROFILE_ID)
    assert caught.value.code == "product-preflight-drifted"
    await plan.aclose()


async def test_another_profile_cannot_stand_in_for_the_confirmed_one() -> None:
    plan = await build_plan()
    await open_for_plan(plan)
    other = replace(profile(), model_id="another-model")
    moved = ProductAssemblyService(
        plan.tasks,
        registry=registry(
            assemblies=RecordingAssemblies(
                roles={
                    role: resolved_role(role, model_id="another-model")
                    for role in ProductRole
                },
                router=replace(plan.assemblies.router, model_id="another-model"),
            ),
            task_profile=other,
        ),
        sources=plan.sources,
        targets=plan.targets,
        router=plan.router,
    )
    with pytest.raises(ProductStateError) as caught:
        await moved.assemble(task_id="task-1", profile_id=PROFILE_ID)
    assert caught.value.code == "product-profile-drifted"
    await plan.aclose()


# -------------------------------------------------------------- task lifecycle


async def test_an_unknown_or_settled_task_assembles_nothing() -> None:
    plan = await build_plan()
    with pytest.raises(ProductStateError) as caught:
        await plan.service.assemble(task_id="task-1", profile_id=PROFILE_ID)
    assert caught.value.code == "product-task-unknown"
    await open_for_plan(plan)
    await plan.tasks.cancel_task(
        task_id="task-1", operation_id="task-1-cancel", reason_code="user-stopped"
    )
    with pytest.raises(ProductStateError) as caught:
        await plan.service.assemble(task_id="task-1", profile_id=PROFILE_ID)
    assert caught.value.code == "product-task-settled"
    await plan.aclose()


async def test_this_stage_never_starts_the_task_it_planned() -> None:
    """F2 produces a plan. Starting it is a later stage's authority."""

    plan = await build_plan(responder=ScriptedResponder(text=MULTI_ANSWER))
    await open_for_plan(plan, requested_mode=RequestedTaskMode.AUTO)
    await plan.service.assemble(
        task_id="task-1", profile_id=PROFILE_ID, routing_summary=ROUTING_SUMMARY
    )
    assert PRODUCT_TASK_STARTED not in await event_types(plan)
    assert not hasattr(plan.service, "start")
    await plan.aclose()


async def test_the_writer_must_be_the_one_that_owns_the_durable_log() -> None:
    """A duck-typed writer would be a second fact universe spliced in."""

    plan = await build_plan()

    class _Elsewhere:
        store = plan.store

        async def load(self, task_id: str):
            del task_id
            return None

    with pytest.raises(ProductInputError) as caught:
        ProductAssemblyService(
            _Elsewhere(),  # type: ignore[arg-type]
            registry=plan.registry,
            sources=plan.sources,
            targets=plan.targets,
            router=plan.router,
        )
    assert caught.value.code == "product-task-service-invalid"
    await plan.aclose()


# --------------------------------------------------------------- concurrency


@dataclass(slots=True)
class _Sequenced:
    """One gate and one answer per call, so the interleaving is chosen, not hoped for."""

    gates: tuple[Gate, ...]
    answers: tuple[str, ...]
    calls: list[str] = field(default_factory=list)

    async def respond(self, summary: str, *, task_id: str) -> RouterResponse:
        del task_id
        index = len(self.calls)
        self.calls.append(summary)
        await self.gates[index].wait()
        return RouterResponse(
            text=self.answers[index],
            router_agent_id=ROUTER_AGENT,
            routing_session_id=ROUTER_SESSION,
        )


async def _racing(responder) -> tuple:
    plan = await build_plan()
    router = mode_router(responder)
    service = ProductAssemblyService(
        plan.tasks,
        registry=plan.registry,
        sources=plan.sources,
        targets=plan.targets,
        router=router,
    )
    await open_for_plan(plan, requested_mode=RequestedTaskMode.AUTO)

    def start() -> asyncio.Task:
        return asyncio.create_task(
            service.assemble(
                task_id="task-1",
                profile_id=PROFILE_ID,
                routing_summary=ROUTING_SUMMARY,
            )
        )

    return plan, router, start


async def test_a_late_assembly_adopts_the_decision_that_was_recorded_first() -> None:
    """Both loaded before anything was routed; only one answer becomes the fact."""

    gates = (Gate(), Gate())
    responder = _Sequenced(gates, (SINGLE_ANSWER, MULTI_ANSWER))
    plan, router, start = await _racing(responder)
    first = start()
    await gates[0].entered.wait()
    second = start()
    # The second caller has already replayed a task with no routing fact and is
    # now inside the router, so releasing the first is what creates the window.
    await gates[1].entered.wait()
    gates[0].release.set()
    left = await first
    gates[1].release.set()
    right = await second
    assert responder.calls == [ROUTING_SUMMARY, ROUTING_SUMMARY]
    assert left.resolved_mode is ResolvedTaskMode.SINGLE
    assert right.resolved_mode is ResolvedTaskMode.SINGLE
    assert left.receipt == right.receipt
    assert (await event_types(plan)).count(PRODUCT_TASK_ROUTED) == 1
    await router.aclose()
    await plan.aclose()


async def test_a_simultaneous_second_decision_is_refused_not_merged() -> None:
    """Two answers in flight at once: one becomes the fact, the other is a conflict."""

    gate = Gate()
    responder = _Sequenced((gate, gate), (SINGLE_ANSWER, MULTI_ANSWER))
    plan, router, start = await _racing(responder)
    first = start()
    second = start()
    await gate.entered.wait()
    gate.release.set()
    outcomes = await asyncio.gather(first, second, return_exceptions=True)
    conflicts = [item for item in outcomes if isinstance(item, BaseException)]
    assert len(conflicts) == 1
    assert isinstance(conflicts[0], ProductOperationConflictError)
    assert (await event_types(plan)).count(PRODUCT_TASK_ROUTED) == 1
    routed = (await plan.store.read(product_task_stream("task-1")))[1]
    winner = next(item for item in outcomes if not isinstance(item, BaseException))
    assert winner.resolved_mode.value == routed.data["resolved_mode"]
    await router.aclose()
    await plan.aclose()


async def test_a_cancelled_assembly_records_no_decision() -> None:
    gate = Gate()
    plan = await build_plan(responder=ScriptedResponder(text=MULTI_ANSWER, gate=gate))
    await open_for_plan(plan, requested_mode=RequestedTaskMode.AUTO)
    caller = asyncio.create_task(
        plan.service.assemble(
            task_id="task-1", profile_id=PROFILE_ID, routing_summary=ROUTING_SUMMARY
        )
    )
    await gate.entered.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert await event_types(plan) == ["product/task-opened"]
    gate.release.set()
    await plan.aclose()


async def test_a_closed_router_stops_new_assemblies_without_writing() -> None:
    plan = await build_plan()
    await open_for_plan(plan, requested_mode=RequestedTaskMode.AUTO)
    await plan.router.aclose()
    with pytest.raises(ProductServiceClosedError):
        await plan.service.assemble(
            task_id="task-1", profile_id=PROFILE_ID, routing_summary=ROUTING_SUMMARY
        )
    assert await event_types(plan) == ["product/task-opened"]
    await plan.base.aclose()


async def test_a_failing_external_read_leaves_no_half_receipt() -> None:
    class _Broken:
        reads = 0
        revision = ""

        async def resolve_source(self, source_id: str, revision: str):
            del source_id, revision
            raise RuntimeError("the source repository is unreachable")

    plan = await build_plan()
    await open_for_plan(plan, requested_mode=RequestedTaskMode.AUTO)
    broken = ProductAssemblyService(
        plan.tasks,
        registry=plan.registry,
        sources=_Broken(),  # type: ignore[arg-type]
        targets=plan.targets,
        router=plan.router,
    )
    with pytest.raises(RuntimeError):
        await broken.assemble(
            task_id="task-1", profile_id=PROFILE_ID, routing_summary=ROUTING_SUMMARY
        )
    assert plan.responder.calls == []
    assert await event_types(plan) == ["product/task-opened"]
    await plan.aclose()
