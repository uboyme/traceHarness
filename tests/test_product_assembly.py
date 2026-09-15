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
from dataclasses import fields, replace
from pathlib import Path

import pytest
from product_fixtures import (
    PROFILE_ID,
    Gate,
    RecordingAssemblies,
    RecordingSources,
    RecordingTargets,
    build_plan,
    open_for_plan,
    profile,
    registry,
    resolved_role,
    verification_plan,
)
from promotion_fixtures import build_source_repository, make_bare_target

from traceh.api.product import (
    PRODUCT_TASK_STARTED,
    ProductRole,
    RequestedTaskMode,
    ResolvedTaskMode,
)
from traceh.api.promotion import PromotionTargetBinding
from traceh.product import (
    ProductAssemblyService,
    ProductInputError,
    ProductProfileError,
    ProductStateError,
    product_definition_hash,
    product_role_node_id,
    product_task_stream,
)
from traceh.promotion.local_git import LocalBareGitPromotionTargets
from traceh.workflow.models import workflow_definition_hash
from traceh.workspaces.local_git import LocalGitWorkspaceProvider


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
        type(getattr(preflight.binding, item.name)) is str for item in fields(preflight.binding)
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
    task_profile = replace(profile(), source_id="trusted-source", promotion_target_id="main-target")
    plan = await build_plan(task_profile=task_profile)
    service = ProductAssemblyService(
        plan.tasks,
        registry=registry(assemblies=plan.assemblies, task_profile=task_profile),
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
    )
    preflight = await service.preflight(PROFILE_ID)
    assert len(preflight.binding.base_revision) == 40
    assert preflight.binding.base_revision == (preflight.binding.promotion_expected_revision)
    assert preflight.binding.repository_fingerprint != (
        preflight.binding.promotion_target_fingerprint
    )
    await plan.aclose()


# ------------------------------------------------------------ explicit modes


async def test_both_modes_assemble_without_model_calls_or_progress_events() -> None:
    for requested, resolved in (
        (RequestedTaskMode.SINGLE, ResolvedTaskMode.SINGLE),
        (RequestedTaskMode.MULTI, ResolvedTaskMode.MULTI),
    ):
        plan = await build_plan()
        await open_for_plan(plan, requested_mode=requested)
        assembly = await plan.service.assemble(task_id="task-1", profile_id=PROFILE_ID)
        assert assembly.resolved_mode is resolved
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
    assert [node.node_id for node in assembly.definition.nodes][:1] == [
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


async def test_an_unavailable_role_prevents_assembly() -> None:
    """Both execution templates must be resolved before assembly."""

    plan = await build_plan()
    await open_for_plan(plan, requested_mode=RequestedTaskMode.MULTI)
    plan.assemblies.unavailable = frozenset({ProductRole.INVESTIGATOR})
    with pytest.raises(ProductProfileError) as caught:
        await plan.service.assemble(task_id="task-1", profile_id=PROFILE_ID)
    assert caught.value.code == "product-assembly-unavailable"
    assert await event_types(plan) == ["product/task-opened"]
    await plan.aclose()


# ----------------------------------------------------------------------- drift


async def test_a_moved_source_revision_fails_closed() -> None:
    sources = RecordingSources()
    plan = await build_plan(sources=sources)
    await open_for_plan(plan, requested_mode=RequestedTaskMode.MULTI)
    sources.revision = "e5" * 20
    with pytest.raises(ProductStateError) as caught:
        await plan.service.assemble(task_id="task-1", profile_id=PROFILE_ID)
    assert caught.value.code == "product-preflight-drifted"
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


async def test_a_changed_verification_plan_fails_closed() -> None:
    plan = await build_plan()
    await open_for_plan(plan)
    moved = ProductAssemblyService(
        plan.tasks,
        registry=registry(assemblies=plan.assemblies, plan=verification_plan(plan_version=2)),
        sources=plan.sources,
        targets=plan.targets,
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
            role: resolved_role(role, tools=("read-file", "apply-patch")) for role in ProductRole
        }
    )
    moved = ProductAssemblyService(
        plan.tasks,
        registry=registry(assemblies=rebound),
        sources=plan.sources,
        targets=plan.targets,
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
                roles={role: resolved_role(role, model_id="another-model") for role in ProductRole},
            ),
            task_profile=other,
        ),
        sources=plan.sources,
        targets=plan.targets,
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

    plan = await build_plan()
    await open_for_plan(plan, requested_mode=RequestedTaskMode.MULTI)
    await plan.service.assemble(task_id="task-1", profile_id=PROFILE_ID)
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
        )
    assert caught.value.code == "product-task-service-invalid"
    await plan.aclose()


# --------------------------------------------------------------- concurrency


async def test_a_failing_external_read_leaves_no_half_receipt() -> None:
    class _Broken:
        reads = 0
        revision = ""

        async def resolve_source(self, source_id: str, revision: str):
            del source_id, revision
            raise RuntimeError("the source repository is unreachable")

    plan = await build_plan()
    await open_for_plan(plan, requested_mode=RequestedTaskMode.MULTI)
    broken = ProductAssemblyService(
        plan.tasks,
        registry=plan.registry,
        sources=_Broken(),  # type: ignore[arg-type]
        targets=plan.targets,
    )
    with pytest.raises(RuntimeError):
        await broken.assemble(task_id="task-1", profile_id=PROFILE_ID)
    assert await event_types(plan) == ["product/task-opened"]
    await plan.aclose()


async def test_cancelled_source_resolution_leaves_only_the_opened_fact() -> None:
    plan = await build_plan()
    await open_for_plan(plan, requested_mode=RequestedTaskMode.MULTI)
    gate = Gate()

    class WaitingSource:
        async def resolve_source(self, source_id: str, revision: str):
            await gate.wait()
            return await plan.sources.resolve_source(source_id, revision)

    service = ProductAssemblyService(
        plan.tasks, registry=plan.registry, sources=WaitingSource(), targets=plan.targets
    )
    caller = asyncio.create_task(service.assemble(task_id="task-1", profile_id=PROFILE_ID))
    await gate.entered.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert caller.done()
    assert await event_types(plan) == ["product/task-opened"]
    await plan.aclose()
