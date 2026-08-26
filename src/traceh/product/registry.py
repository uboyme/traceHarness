"""The one host Profile Registry, and what "resolved" actually means.

A Profile is a list of *names*: a preset, a provider, a model, a source, a
verification plan, a promotion target. What those names currently resolve to is a
different fact, and it is the one that decides what a task can really do. A
registry may keep every name spelled identically while rebinding ``preset`` to a
different ``AgentSpec``, different grants or a different Tool/Prompt/Policy
composition - and neither ``profile_digest`` nor ``workflow_definition_hash()``
would notice, because both cover names and binding ids rather than resolution
results.

So this module resolves both halves and derives a digest over what it actually
got. :class:`ProductProfileRegistry` is the only place a ``profile_id`` becomes a
runnable Profile. There is no default: an unknown id, a duplicate id, a plan that
does not match the id the Profile named, or an assembly that disagrees with the
slot it was resolved for are all failures, never fall-throughs.

Two invariants are enforced rather than recorded, because recording them would
only prove the disagreement later:

* **write authority follows the slot.** ``ProductRole.workspace_access`` is the
  single definition, so a resolver that hands back a writable reviewer is
  refused. A Profile has no field with which to grant it and a resolver has no
  standing to;
* **the router holds no Tool.** ``ProductRouterProfile`` has no
  ``capability_grants`` field, and a router assembly carrying a Tool id or a
  grant is refused here, which is what makes "the router was granted no tool" a
  checked fact rather than a claim in a docstring.

Nothing here reads or writes an Event Store, starts anything, or performs Git or
model I/O. It resolves host configuration and stops.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Protocol

from traceh.agents.errors import AgentIdentityError
from traceh.agents.identity import agent_spec_request_fingerprint, freeze_agent_spec
from traceh.api.agents import AgentSpec
from traceh.api.json_types import fingerprint
from traceh.api.product import (
    PRODUCT_TASK_PROTOCOL_VERSION,
    ProductRole,
    ProductRoleProfile,
    ProductRouterProfile,
    ProductTaskProfile,
    RequestedTaskMode,
)
from traceh.api.promotion import VerificationPlan
from traceh.api.workspaces import WorkspaceAccess
from traceh.budgets.errors import BudgetInputError
from traceh.budgets.events import freeze_limits
from traceh.product.errors import ProductProfileError
from traceh.product.events import require_product_identifier
from traceh.promotion.models import freeze_verification_plan, verifier_definition_digest

MAX_ASSEMBLY_COMPONENTS = 64
"""Bound on each resolved composition list.

A composition is host configuration, so this is a sanity ceiling rather than a
policy: it exists so a runaway resolver produces a stable failure instead of an
unbounded digest input.
"""

MAX_ROLE_GRANTS = 64
MAX_PROFILE_VERSION = 1_000_000


@dataclass(frozen=True, slots=True)
class ResolvedAgentAssembly:
    """What one preset actually resolved to, right now.

    This is the value a name-only binding cannot see. ``spec`` is the resolved
    ``AgentSpec`` - preset, workspace intent, ownership, lineage and the
    effective capability grants; ``provider_id`` and ``model_id`` are the
    resolved model identity; the three id tuples are the effective
    Tool/Prompt/Policy composition.

    Order is preserved in all three tuples rather than sorted. A policy chain in
    a different order is a different chain, and flattening that into a set would
    make two materially different compositions share one digest.
    """

    spec: AgentSpec
    provider_id: str
    model_id: str
    tool_ids: tuple[str, ...]
    prompt_ids: tuple[str, ...]
    policy_ids: tuple[str, ...]
    workspace_access: WorkspaceAccess


class ProductAssemblyResolver(Protocol):
    """The host seam from a configured preset to what it currently resolves to.

    Deliberately not implemented here. Turning a preset into a Provider, a model,
    a prompt, a Tool set and a policy chain is a deployment's own decision, and
    baking any of it into this package would make one deployment's choices the
    product's defaults.

    The ``role`` is passed so a host can *see* which slot it is resolving, never
    so it can decide what that slot may do: the returned ``workspace_access`` is
    checked against ``ProductRole.workspace_access`` and a disagreement is
    refused.
    """

    async def role_assembly(
        self,
        *,
        role: ProductRole,
        profile: ProductRoleProfile,
        provider_id: str,
        model_id: str,
    ) -> ResolvedAgentAssembly:
        ...

    async def router_assembly(
        self, *, profile: ProductRouterProfile, provider_id: str, model_id: str
    ) -> ResolvedAgentAssembly:
        ...


@dataclass(frozen=True, slots=True)
class ProductProfileBinding:
    """One registry entry: the names, and the frozen plan those names refer to.

    The verification plan is held rather than named a second time because
    ``verification_plan_id`` alone cannot say what "verified" proved. The plan's
    ``plan_id`` must equal the id the Profile named, so an entry cannot quietly
    pair one Profile with another Profile's checks.
    """

    profile: ProductTaskProfile
    verification_plan: VerificationPlan


@dataclass(frozen=True, slots=True)
class ResolvedProductProfile:
    """One Profile, fully resolved, with every digest derived from the result.

    Every digest here is a computed property. A supplied one would be a second
    place the same fact could disagree with itself, and could silently omit a
    field added later - the same rule ``ProductPreflightBinding.digest`` follows.
    """

    profile_id: str
    profile: ProductTaskProfile
    verification_plan: VerificationPlan
    roles: Mapping[ProductRole, ResolvedAgentAssembly]
    router: ResolvedAgentAssembly

    def assembly(self, role: ProductRole) -> ResolvedAgentAssembly:
        resolved = self.roles.get(role)
        if resolved is None:
            raise ProductProfileError("product-role-assembly-missing", role.value)
        return resolved

    @property
    def role_assembly_digest(self) -> str:
        return role_assembly_digest(self.roles)

    @property
    def router_assembly_digest(self) -> str:
        return router_assembly_digest(self.router)

    @property
    def verification_plan_digest(self) -> str:
        return verifier_definition_digest(self.verification_plan)


def agent_assembly_digest(assembly: ResolvedAgentAssembly) -> str:
    """Cover the resolved identity, model and composition of one Agent.

    ``AgentSpec`` identity goes through the repository's own
    ``agent_spec_request_fingerprint()`` rather than a second field list written
    here, so "which Agent is this" has one definition in the codebase.
    """

    if type(assembly) is not ResolvedAgentAssembly:
        raise ProductProfileError("product-assembly-invalid", "assembly")
    return fingerprint(
        {
            "protocol": PRODUCT_TASK_PROTOCOL_VERSION,
            "purpose": "product-agent-assembly",
            "spec": agent_spec_request_fingerprint(assembly.spec),
            "provider_id": assembly.provider_id,
            "model_id": assembly.model_id,
            "tools": list(assembly.tool_ids),
            "prompts": list(assembly.prompt_ids),
            "policies": list(assembly.policy_ids),
            "workspace_access": assembly.workspace_access.value,
        }
    )


def role_assembly_digest(
    roles: Mapping[ProductRole, ResolvedAgentAssembly],
) -> str:
    """Cover all three roles, in a fixed order, or refuse to produce a digest.

    All three participate even when a mode runs only one of them. A digest that
    covered "whichever roles this mode happens to use" would change meaning with
    the mode, and the binding a person confirmed is made before any mode is
    chosen.
    """

    entries = []
    for role in sorted(ProductRole, key=lambda member: member.value):
        resolved = roles.get(role)
        if resolved is None:
            raise ProductProfileError("product-role-assembly-missing", role.value)
        entries.append({"role": role.value, "assembly": agent_assembly_digest(resolved)})
    return fingerprint(
        {
            "protocol": PRODUCT_TASK_PROTOCOL_VERSION,
            "purpose": "product-role-assembly",
            "roles": entries,
        }
    )


def router_assembly_digest(router: ResolvedAgentAssembly) -> str:
    return fingerprint(
        {
            "protocol": PRODUCT_TASK_PROTOCOL_VERSION,
            "purpose": "product-router-assembly",
            "assembly": agent_assembly_digest(router),
        }
    )


class ProductProfileRegistry:
    """The single host-owned resolution surface from a profile id to a Profile.

    It takes an iterable of pairs rather than a mapping on purpose: a mapping
    cannot express a duplicate id, and silently keeping the last entry for a
    repeated id is exactly the kind of quiet decision a registry must not make.

    It holds no run state. Resolving twice with unchanged configuration produces
    equal values and equal digests; changing a binding changes them.
    """

    __slots__ = ("_assemblies", "_bindings")

    def __init__(
        self,
        bindings: Iterable[tuple[str, ProductProfileBinding]],
        *,
        assemblies: ProductAssemblyResolver,
    ) -> None:
        try:
            entries = tuple(bindings)
        except Exception:
            raise ProductProfileError("product-registry-invalid", "bindings") from None
        resolved: dict[str, ProductProfileBinding] = {}
        for entry in entries:
            if type(entry) is not tuple or len(entry) != 2:
                raise ProductProfileError("product-registry-invalid", "bindings")
            profile_id = _profile_identifier(entry[0])
            binding = entry[1]
            if type(binding) is not ProductProfileBinding:
                raise ProductProfileError("product-profile-invalid", profile_id)
            if profile_id in resolved:
                raise ProductProfileError("product-profile-duplicate", profile_id)
            _require_profile(binding.profile, profile_id)
            _require_plan(binding, profile_id)
            resolved[profile_id] = binding
        self._bindings = MappingProxyType(resolved)
        self._assemblies = assemblies

    @property
    def profile_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._bindings))

    async def resolve(self, profile_id: str) -> ResolvedProductProfile:
        """Resolve one explicit id completely, or fail closed.

        There is no default id and no nearest match. A host that did not name a
        Profile has not made the decision this value represents.
        """

        profile_id = _profile_identifier(profile_id)
        binding = self._bindings.get(profile_id)
        if binding is None:
            raise ProductProfileError("product-profile-unknown", profile_id)
        profile = binding.profile
        roles: dict[ProductRole, ResolvedAgentAssembly] = {}
        for role in ProductRole:
            slot = profile.role_profile(role)
            assembly = await self._assemblies.role_assembly(
                role=role,
                profile=slot,
                provider_id=profile.provider_id,
                model_id=profile.model_id,
            )
            roles[role] = _require_assembly(
                assembly,
                preset=slot.preset,
                grants=slot.capability_grants,
                provider_id=profile.provider_id,
                model_id=profile.model_id,
                access=role.workspace_access,
                field=role.value,
            )
        router = await self._assemblies.router_assembly(
            profile=profile.router,
            provider_id=profile.provider_id,
            model_id=profile.model_id,
        )
        router = _require_assembly(
            router,
            preset=profile.router.preset,
            grants=(),
            provider_id=profile.provider_id,
            model_id=profile.model_id,
            # A router reads a summary and answers with one word. Write access
            # would be authority it has no use for and no way to spend.
            access=WorkspaceAccess.READ_ONLY,
            field="router",
        )
        if router.tool_ids:
            raise ProductProfileError("product-router-tool-granted", "router")
        return ResolvedProductProfile(
            profile_id=profile_id,
            profile=profile,
            verification_plan=binding.verification_plan,
            roles=MappingProxyType(roles),
            router=router,
        )


def _profile_identifier(value: object) -> str:
    try:
        return require_product_identifier(value, field="profile_id")
    except Exception:
        raise ProductProfileError("product-profile-id-invalid", "profile_id") from None


def _require_plan(binding: ProductProfileBinding, profile_id: str) -> None:
    try:
        plan = freeze_verification_plan(binding.verification_plan)
    except Exception:
        raise ProductProfileError(
            "product-verification-plan-invalid", profile_id
        ) from None
    if plan.plan_id != binding.profile.verification_plan_id:
        raise ProductProfileError("product-verification-plan-mismatch", profile_id)


def _require_profile(profile: object, profile_id: str) -> None:
    """Every name a task will run against, checked before it can be resolved."""

    if type(profile) is not ProductTaskProfile:
        raise ProductProfileError("product-profile-invalid", profile_id)
    if (
        type(profile.profile_version) is not int
        or profile.profile_version < 1
        or profile.profile_version > MAX_PROFILE_VERSION
    ):
        raise ProductProfileError("product-profile-version-invalid", profile_id)
    if type(profile.default_mode) is not RequestedTaskMode:
        raise ProductProfileError("product-default-mode-invalid", profile_id)
    for field, value in (
        ("provider_id", profile.provider_id),
        ("model_id", profile.model_id),
        ("source_id", profile.source_id),
        ("source_revision", profile.source_revision),
        ("verification_plan_id", profile.verification_plan_id),
        ("promotion_target_id", profile.promotion_target_id),
    ):
        _require_identifier(value, profile_id, field)
    for role in ProductRole:
        _require_role_profile(profile.role_profile(role), profile_id, role.value)
    router = profile.router
    if type(router) is not ProductRouterProfile:
        raise ProductProfileError("product-router-profile-invalid", profile_id)
    _require_identifier(router.preset, profile_id, "router_preset")
    _require_limits(router.budget, profile_id, "router_budget")
    _require_limits(profile.task_budget, profile_id, "task_budget")


def _require_role_profile(slot: object, profile_id: str, field: str) -> None:
    if type(slot) is not ProductRoleProfile:
        raise ProductProfileError("product-role-profile-invalid", profile_id)
    _require_identifier(slot.preset, profile_id, f"{field}_preset")
    grants = slot.capability_grants
    if type(grants) is not tuple or len(grants) > MAX_ROLE_GRANTS:
        raise ProductProfileError("product-role-grants-invalid", profile_id)
    seen: set[str] = set()
    for grant in grants:
        _require_identifier(grant, profile_id, f"{field}_grant")
        if grant in seen:
            raise ProductProfileError("product-role-grants-invalid", profile_id)
        seen.add(grant)
    _require_limits(slot.budget, profile_id, f"{field}_budget")


def _require_limits(limits: object, profile_id: str, field: str) -> None:
    """All seven dimensions must be explicit values the host actually chose.

    ``None`` is a decision - "do not enforce this dimension" - and is accepted.
    The Budget domain's own ``freeze_limits()`` owns the integer and maximum
    range contract; duplicating a weaker subset here would let a Profile promise
    an account the only Ledger writer can never create.
    """

    try:
        freeze_limits(limits, field=field)
    except BudgetInputError:
        raise ProductProfileError("product-budget-invalid", field) from None


def _require_identifier(value: object, profile_id: str, field: str) -> None:
    try:
        require_product_identifier(value, field=field)
    except Exception:
        raise ProductProfileError("product-profile-name-invalid", field) from None
    del profile_id


def _require_assembly(
    assembly: object,
    *,
    preset: str,
    grants: tuple[str, ...],
    provider_id: str,
    model_id: str,
    access: WorkspaceAccess,
    field: str,
) -> ResolvedAgentAssembly:
    """Accept a resolution only when it describes the slot it was asked about.

    A resolver is trusted to answer *what* a preset currently is; it is not
    trusted to answer *which* preset, model or role it was asked about. Those
    came from the Profile, and a resolution that disagrees with them is two facts
    about one slot rather than one resolved fact.
    """

    if type(assembly) is not ResolvedAgentAssembly:
        raise ProductProfileError("product-assembly-invalid", field)
    try:
        spec = freeze_agent_spec(assembly.spec)
    except AgentIdentityError:
        raise ProductProfileError("product-assembly-spec-invalid", field) from None
    except Exception:
        raise ProductProfileError("product-assembly-spec-invalid", field) from None
    if spec.preset != preset:
        raise ProductProfileError("product-assembly-wrong-preset", field)
    if tuple(spec.capability_grants) != tuple(grants):
        raise ProductProfileError("product-assembly-grants-mismatch", field)
    if assembly.provider_id != provider_id or assembly.model_id != model_id:
        raise ProductProfileError("product-assembly-model-mismatch", field)
    if (
        type(assembly.workspace_access) is not WorkspaceAccess
        or assembly.workspace_access is not access
    ):
        raise ProductProfileError("product-assembly-access-mismatch", field)
    return replace(
        assembly,
        spec=spec,
        tool_ids=_require_components(assembly.tool_ids, field, "tool"),
        prompt_ids=_require_components(assembly.prompt_ids, field, "prompt"),
        policy_ids=_require_components(assembly.policy_ids, field, "policy"),
    )


def _require_components(
    values: object, field: str, kind: str
) -> tuple[str, ...]:
    if type(values) is not tuple or len(values) > MAX_ASSEMBLY_COMPONENTS:
        raise ProductProfileError(f"product-assembly-{kind}-invalid", field)
    seen: set[str] = set()
    for value in values:
        try:
            identifier = require_product_identifier(value, field=kind)
        except Exception:
            raise ProductProfileError(
                f"product-assembly-{kind}-invalid", field
            ) from None
        if identifier in seen:
            raise ProductProfileError(f"product-assembly-{kind}-invalid", field)
        seen.add(identifier)
    return tuple(values)


__all__ = [
    "MAX_ASSEMBLY_COMPONENTS",
    "MAX_PROFILE_VERSION",
    "MAX_ROLE_GRANTS",
    "ProductAssemblyResolver",
    "ProductProfileBinding",
    "ProductProfileRegistry",
    "ResolvedAgentAssembly",
    "ResolvedProductProfile",
    "agent_assembly_digest",
    "role_assembly_digest",
    "router_assembly_digest",
]
