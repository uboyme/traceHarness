"""v0.7-F2: the one Profile Registry, and the two fixed shapes it can run.

A Profile is a list of names. These cases are about the difference between the
names and what they currently resolve to, and about the two topologies that are
decided here rather than by any configuration.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from product_fixtures import (
    PROFILE_ID,
    RecordingAssemblies,
    limits,
    profile,
    registry,
    resolved_role,
    resolved_router,
    verification_plan,
)

from traceh.api.product import ProductRole, ResolvedTaskMode
from traceh.api.workflow import (
    AgentTaskNode,
    ApprovalNode,
    JoinNode,
    MapNode,
    VerificationNode,
)
from traceh.api.workspaces import WorkspaceAccess
from traceh.budgets.events import MAX_BUDGET_VALUE
from traceh.product import (
    PRODUCT_APPROVAL_NODE,
    PRODUCT_MODE_ROLES,
    PRODUCT_VERIFICATION_NODE,
    ProductProfileBinding,
    ProductProfileError,
    ProductProfileRegistry,
    product_definition_hash,
    product_role_node_id,
    product_spec_binding,
    product_workflow_definition,
    role_assembly_digest,
)
from traceh.product.registry import agent_assembly_digest

TARGET = profile().promotion_target_id


async def refuse(profiles: ProductProfileRegistry, profile_id: str = PROFILE_ID) -> str:
    with pytest.raises(ProductProfileError) as caught:
        await profiles.resolve(profile_id)
    return caught.value.code


# ------------------------------------------------------------------- resolving


async def test_one_explicit_id_resolves_to_one_complete_profile() -> None:
    assemblies = RecordingAssemblies()
    resolved = await registry(assemblies=assemblies).resolve(PROFILE_ID)
    assert resolved.profile_id == PROFILE_ID
    assert resolved.profile == profile()
    assert resolved.verification_plan == verification_plan()
    assert set(resolved.roles) == set(ProductRole)
    assert sorted(assemblies.calls) == ["coder", "parent", "reviewer", "router"]


async def test_resolving_twice_produces_the_same_digests() -> None:
    profiles = registry()
    first = await profiles.resolve(PROFILE_ID)
    second = await profiles.resolve(PROFILE_ID)
    assert first.role_assembly_digest == second.role_assembly_digest
    assert first.router_assembly_digest == second.router_assembly_digest
    assert first.verification_plan_digest == second.verification_plan_digest


async def test_there_is_no_default_profile() -> None:
    """A host that named no Profile has not made this decision."""

    empty = ProductProfileRegistry((), assemblies=RecordingAssemblies())
    assert empty.profile_ids == ()
    assert await refuse(empty) == "product-profile-unknown"
    assert await refuse(registry(), "profile-beta") == "product-profile-unknown"
    for bad in ("", "  ", None, 7, True):
        assert await refuse(registry(), bad) == "product-profile-id-invalid"  # type: ignore[arg-type]


def test_a_repeated_profile_id_is_refused_rather_than_overwritten() -> None:
    """A mapping cannot express this, which is why the registry takes pairs."""

    binding = ProductProfileBinding(
        profile=profile(), verification_plan=verification_plan()
    )
    with pytest.raises(ProductProfileError) as caught:
        ProductProfileRegistry(
            ((PROFILE_ID, binding), (PROFILE_ID, binding)),
            assemblies=RecordingAssemblies(),
        )
    assert caught.value.code == "product-profile-duplicate"


def test_a_plan_from_another_profile_cannot_be_paired_with_this_one() -> None:
    binding = ProductProfileBinding(
        profile=profile(), verification_plan=verification_plan(plan_id="other-plan")
    )
    with pytest.raises(ProductProfileError) as caught:
        ProductProfileRegistry(
            ((PROFILE_ID, binding),), assemblies=RecordingAssemblies()
        )
    assert caught.value.code == "product-verification-plan-mismatch"


def test_an_incomplete_or_ill_typed_profile_never_becomes_a_registry_entry() -> None:
    base = profile()
    for broken in (
        replace(base, profile_version=0),
        replace(base, provider_id=""),
        replace(base, source_revision="  main"),
        replace(base, promotion_target_id=None),  # type: ignore[arg-type]
        replace(base, task_budget=limits(max_tokens=-1)),
        replace(base, coder=replace(base.coder, preset="")),
        replace(base, coder=replace(base.coder, capability_grants=["a"])),  # type: ignore[arg-type]
        replace(base, coder=replace(base.coder, capability_grants=("a", "a"))),
        replace(base, router=replace(base.router, budget=None)),  # type: ignore[arg-type]
        replace(base, default_mode="auto"),  # type: ignore[arg-type]
    ):
        with pytest.raises(ProductProfileError):
            ProductProfileRegistry(
                (
                    (
                        PROFILE_ID,
                        ProductProfileBinding(
                            profile=broken, verification_plan=verification_plan()
                        ),
                    ),
                ),
                assemblies=RecordingAssemblies(),
            )


def test_profile_budgets_use_the_ledger_domain_limit_contract() -> None:
    """A Profile cannot authorize capacity the only Budget writer rejects."""

    base = profile()
    too_large = replace(
        base,
        router=replace(
            base.router,
            budget=limits(max_tokens=MAX_BUDGET_VALUE + 1),
        ),
    )
    with pytest.raises(ProductProfileError) as caught:
        ProductProfileRegistry(
            (
                (
                    PROFILE_ID,
                    ProductProfileBinding(
                        profile=too_large,
                        verification_plan=verification_plan(),
                    ),
                ),
            ),
            assemblies=RecordingAssemblies(),
        )
    assert caught.value.code == "product-budget-invalid"


# ------------------------------------------------- names versus what they mean


async def test_a_registry_rebinding_changes_the_assembly_digest_alone() -> None:
    """Every name stays identical; only what they resolved to moved."""

    before = await registry().resolve(PROFILE_ID)
    rebound = RecordingAssemblies(
        roles={
            role: (
                resolved_role(role, tools=("read-file", "apply-patch"))
                if role is ProductRole.CODER
                else resolved_role(role)
            )
            for role in ProductRole
        }
    )
    after = await registry(assemblies=rebound).resolve(PROFILE_ID)
    assert after.profile.digest == before.profile.digest
    assert after.role_assembly_digest != before.role_assembly_digest
    assert after.router_assembly_digest == before.router_assembly_digest


def test_a_reordered_composition_is_a_different_composition() -> None:
    """A policy chain in another order runs differently, so it digests differently."""

    forward = resolved_role(ProductRole.CODER, policies=("policy-a", "policy-b"))
    reversed_chain = replace(forward, policy_ids=("policy-b", "policy-a"))
    assert agent_assembly_digest(forward) != agent_assembly_digest(reversed_chain)


def test_a_role_digest_needs_all_three_roles() -> None:
    """The binding is made before a mode is chosen, so all three participate."""

    with pytest.raises(ProductProfileError) as caught:
        role_assembly_digest(
            {
                role: resolved_role(role)
                for role in ProductRole
                if role is not ProductRole.REVIEWER
            }
        )
    assert caught.value.code == "product-role-assembly-missing"


# -------------------------------------------------------- enforced authorities


async def test_write_authority_comes_from_the_slot_not_the_resolver() -> None:
    resolved = await registry().resolve(PROFILE_ID)
    assert resolved.assembly(ProductRole.CODER).workspace_access is (
        WorkspaceAccess.WRITABLE
    )
    for role in (ProductRole.PARENT, ProductRole.REVIEWER):
        assert resolved.assembly(role).workspace_access is WorkspaceAccess.READ_ONLY

    writable_reviewer = RecordingAssemblies(
        roles={
            role: resolved_role(
                role,
                access=(
                    WorkspaceAccess.WRITABLE
                    if role is ProductRole.REVIEWER
                    else role.workspace_access
                ),
            )
            for role in ProductRole
        }
    )
    assert (
        await refuse(registry(assemblies=writable_reviewer))
        == "product-assembly-access-mismatch"
    )


async def test_the_router_is_granted_no_tool_and_no_capability() -> None:
    """This is what makes "the router holds nothing" a checked fact."""

    with_tool = RecordingAssemblies(router=resolved_router(tools=("read-file",)))
    assert await refuse(registry(assemblies=with_tool)) == "product-router-tool-granted"
    with_grant = RecordingAssemblies(router=resolved_router(grants=("read-workspace",)))
    assert (
        await refuse(registry(assemblies=with_grant))
        == "product-assembly-grants-mismatch"
    )
    writable = RecordingAssemblies(
        router=resolved_router(access=WorkspaceAccess.WRITABLE)
    )
    assert (
        await refuse(registry(assemblies=writable))
        == "product-assembly-access-mismatch"
    )


async def test_a_resolution_must_answer_the_question_it_was_asked() -> None:
    """A resolver says what a preset *is*, never which preset it was asked about."""

    wrong_preset = RecordingAssemblies(
        roles={
            role: resolved_role(role, preset="preset-other")
            for role in ProductRole
        }
    )
    assert (
        await refuse(registry(assemblies=wrong_preset))
        == "product-assembly-wrong-preset"
    )
    wrong_model = RecordingAssemblies(
        roles={role: resolved_role(role, model_id="other-model") for role in ProductRole}
    )
    assert (
        await refuse(registry(assemblies=wrong_model))
        == "product-assembly-model-mismatch"
    )
    wrong_grants = RecordingAssemblies(
        roles={role: resolved_role(role, grants=("write-anything",)) for role in ProductRole}
    )
    assert (
        await refuse(registry(assemblies=wrong_grants))
        == "product-assembly-grants-mismatch"
    )
    not_an_assembly = RecordingAssemblies(
        roles=dict.fromkeys(ProductRole, object())  # type: ignore[arg-type]
    )
    assert await refuse(registry(assemblies=not_an_assembly)) == (
        "product-assembly-invalid"
    )


async def test_a_composition_list_is_bounded_and_duplicate_free() -> None:
    duplicated = RecordingAssemblies(
        roles={
            role: resolved_role(role, tools=("read-file", "read-file"))
            for role in ProductRole
        }
    )
    assert (
        await refuse(registry(assemblies=duplicated)) == "product-assembly-tool-invalid"
    )
    listed = RecordingAssemblies(
        roles={
            role: replace(resolved_role(role), prompt_ids=["prompt-role"])  # type: ignore[arg-type]
            for role in ProductRole
        }
    )
    assert (
        await refuse(registry(assemblies=listed)) == "product-assembly-prompt-invalid"
    )


# ---------------------------------------------------------- the two topologies


def test_single_is_a_shorter_workflow_and_not_a_shortcut_past_one() -> None:
    single = product_workflow_definition(
        ResolvedTaskMode.SINGLE, promotion_target_id=TARGET
    )
    multi = product_workflow_definition(
        ResolvedTaskMode.MULTI, promotion_target_id=TARGET
    )
    assert [node.node_id for node in single.nodes] == [
        product_role_node_id(ProductRole.CODER),
        PRODUCT_VERIFICATION_NODE,
        PRODUCT_APPROVAL_NODE,
    ]
    assert [node.node_id for node in multi.nodes] == [
        product_role_node_id(ProductRole.PARENT),
        product_role_node_id(ProductRole.REVIEWER),
        product_role_node_id(ProductRole.CODER),
        PRODUCT_VERIFICATION_NODE,
        PRODUCT_APPROVAL_NODE,
    ]
    for definition in (single, multi):
        verification, approval = definition.nodes[-2:]
        assert type(verification) is VerificationNode
        assert type(approval) is ApprovalNode
        assert verification.artifact_node_id == product_role_node_id(ProductRole.CODER)
        assert verification.target_id == TARGET
        assert approval.review_node_id == PRODUCT_VERIFICATION_NODE
        assert approval.predecessors == (PRODUCT_VERIFICATION_NODE,)


def test_the_reviewer_runs_before_the_coder_reads_its_report() -> None:
    multi = product_workflow_definition(
        ResolvedTaskMode.MULTI, promotion_target_id=TARGET
    )
    by_id = {node.node_id: node for node in multi.nodes}
    assert by_id[product_role_node_id(ProductRole.REVIEWER)].predecessors == (
        product_role_node_id(ProductRole.PARENT),
    )
    assert by_id[product_role_node_id(ProductRole.CODER)].predecessors == (
        product_role_node_id(ProductRole.REVIEWER),
    )


def test_only_the_one_role_that_may_write_captures_anything() -> None:
    for mode in (ResolvedTaskMode.SINGLE, ResolvedTaskMode.MULTI):
        definition = product_workflow_definition(mode, promotion_target_id=TARGET)
        capturing = {
            node.node_id
            for node in definition.nodes
            if type(node) is AgentTaskNode and node.capture_artifact
        }
        assert capturing == {product_role_node_id(ProductRole.CODER)}
        writable = {
            role for role in PRODUCT_MODE_ROLES[mode]
            if role.workspace_access is WorkspaceAccess.WRITABLE
        }
        assert writable == {ProductRole.CODER}


def test_neither_mode_uses_map_or_join() -> None:
    """Stage E keeps its fan-out; the product surface simply does not need it."""

    for mode in (ResolvedTaskMode.SINGLE, ResolvedTaskMode.MULTI):
        definition = product_workflow_definition(mode, promotion_target_id=TARGET)
        assert not any(type(node) in (MapNode, JoinNode) for node in definition.nodes)


def test_a_definition_carries_binding_ids_rather_than_values() -> None:
    definition = product_workflow_definition(
        ResolvedTaskMode.MULTI, promotion_target_id=TARGET
    )
    for node in definition.nodes:
        if type(node) is not AgentTaskNode:
            continue
        assert node.spec_binding.startswith("product-spec-")
        assert node.message_binding.startswith("product-message-")
    assert product_spec_binding(ProductRole.CODER) == "product-spec-coder"


def test_the_definition_hash_is_deterministic_and_names_the_target() -> None:
    first = product_definition_hash(
        ResolvedTaskMode.SINGLE, promotion_target_id=TARGET
    )
    assert first == product_definition_hash(
        ResolvedTaskMode.SINGLE, promotion_target_id=TARGET
    )
    assert first != product_definition_hash(
        ResolvedTaskMode.MULTI, promotion_target_id=TARGET
    )
    assert first != product_definition_hash(
        ResolvedTaskMode.SINGLE, promotion_target_id="other-target"
    )


def test_the_mode_table_cannot_be_rewritten_by_an_importer() -> None:
    with pytest.raises(TypeError):
        PRODUCT_MODE_ROLES[ResolvedTaskMode.SINGLE] = ()  # type: ignore[index]
