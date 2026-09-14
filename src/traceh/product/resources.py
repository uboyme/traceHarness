"""Host resource binding for ProductTask-owned Agents.

The Product profile already decides every grant.  This module translates that
single F2 result into the existing Budget and Workspace adapter policies, then
creates one non-model root Agent whose identity anchors the task's ownership
tree and aggregate Budget account.  It adds no balance, path or lifecycle fact
of its own.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

from traceh.agents.directory import AgentDirectoryReader
from traceh.agents.identity import (
    agent_spec_request_fingerprint,
    creation_matches,
    freeze_agent_spec,
)
from traceh.api.agents import AgentRecord, AgentSpec, AgentSupervisor
from traceh.api.budgets import BudgetLimits, ChildBudgetGrant
from traceh.api.json_types import fingerprint
from traceh.api.product import ProductRole
from traceh.api.workspaces import WorkspaceAccess, WorkspaceProvisioningRequest
from traceh.artifacts.catalog import PatchArtifactCatalogReader
from traceh.budgets.events import freeze_limits
from traceh.budgets.service import BudgetLedgerService
from traceh.product.assembly import ProductPreflight
from traceh.product.errors import (
    ProductInputError,
    ProductProfileError,
    ProductStateError,
)
from traceh.product.execution import product_task_owner_id
from traceh.product.registry import (
    ResolvedAgentAssembly,
    agent_assembly_digest,
)
from traceh.session.event_store import EventStore
from traceh.supervision.delegation import InvestigationBinding
from traceh.supervision.lifecycle import AgentOwnershipGraph
from traceh.supervision.writable_work import WritableBinding
from traceh.workspaces.errors import WorkspaceDirtyError
from traceh.workspaces.service import WorkspaceService

_OWNER_PRESET = "traceh-product-owner"


@dataclass(frozen=True, slots=True)
class ProductRuntimeBinding:
    """The actual runtime composition one managed Agent must receive."""

    assembly: ResolvedAgentAssembly | None
    budget: BudgetLimits | None
    max_output_tokens: int | None
    max_turn_wall_milliseconds: int | None = None
    retained_parent_tokens: int = 0
    role: ProductRole | None = None
    initial_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class _InvestigationTemplate:
    assembly: ResolvedAgentAssembly
    runtime: ProductRuntimeBinding
    source_id: str
    revision: str


@dataclass(frozen=True, slots=True)
class _WritableTemplate:
    task_id: str
    assembly: ResolvedAgentAssembly
    runtime: ProductRuntimeBinding
    source_id: str
    revision: str


class ProductResourceBindings:
    """One checked translation from resolved assemblies to adapter policies."""

    __slots__ = ("_budgets", "_runtime", "_workspaces", "_investigations", "_patch_authors")

    def __init__(self) -> None:
        self._budgets: dict[str, ChildBudgetGrant] = {}
        self._workspaces: dict[str, WorkspaceProvisioningRequest] = {}
        self._runtime: dict[str, ProductRuntimeBinding] = {}
        self._investigations: dict[str, _InvestigationTemplate] = {}
        self._patch_authors: dict[str, _WritableTemplate] = {}

    def register(self, task_id: str, preflight: ProductPreflight) -> AgentSpec:
        if type(preflight) is not ProductPreflight:
            raise ProductInputError("product-preflight-invalid", "preflight")
        owner_id = product_task_owner_id(task_id)
        owner = AgentSpec(
            preset=_OWNER_PRESET,
            workspace_id=f"product-owner-{task_id}",
        )
        self._bind_spec(
            owner,
            source_id=preflight.source.source_id,
            revision=preflight.source.base_revision,
            access=WorkspaceAccess.READ_ONLY,
            runtime=ProductRuntimeBinding(None, None, None),
        )
        profile = preflight.profile.profile
        for role in ProductRole:
            if role is not ProductRole.CODER:
                continue  # Template only: never a sibling execution node.
            resolved = preflight.profile.assembly(role)
            spec = replace(resolved.spec, owner_agent_id=owner_id)
            self._bind_spec(
                spec,
                source_id=preflight.source.source_id,
                revision=preflight.source.base_revision,
                access=role.workspace_access,
                runtime=ProductRuntimeBinding(
                    resolved,
                    freeze_limits(profile.role_profile(role).budget),
                    profile.role_profile(role).max_output_tokens,
                    profile.role_profile(role).max_turn_wall_milliseconds,
                ),
            )
        multi = preflight.profile.multi
        multi_spec = replace(multi.spec, owner_agent_id=owner_id)
        self._bind_spec(
            multi_spec,
            source_id=preflight.source.source_id,
            revision=preflight.source.base_revision,
            access=WorkspaceAccess.WRITABLE,
            runtime=ProductRuntimeBinding(
                multi,
                freeze_limits(profile.coder.budget),
                profile.coder.max_output_tokens,
                profile.coder.max_turn_wall_milliseconds,
            ),
        )
        investigator = preflight.profile.assembly(ProductRole.INVESTIGATOR)
        self._investigations[_runtime_key(multi_spec)] = _InvestigationTemplate(
            investigator,
            ProductRuntimeBinding(
                investigator,
                freeze_limits(profile.investigator.budget),
                profile.investigator.max_output_tokens,
                profile.investigator.max_turn_wall_milliseconds,
                profile.retained_tokens,
                ProductRole.INVESTIGATOR,
                profile.investigator_initial_tokens,
            ),
            preflight.source.source_id,
            preflight.source.base_revision,
        )
        if profile.patch_author is not None:
            patch = profile.patch_author
            assembly = preflight.profile.assembly(ProductRole.PATCH_AUTHOR)
            self._patch_authors[_runtime_key(multi_spec)] = _WritableTemplate(
                task_id,
                assembly,
                ProductRuntimeBinding(
                    assembly,
                    freeze_limits(patch.budget),
                    patch.max_output_tokens,
                    patch.max_turn_wall_milliseconds,
                    profile.retained_tokens,
                    ProductRole.PATCH_AUTHOR,
                ),
                preflight.source.source_id,
                preflight.source.base_revision,
            )
        return owner

    def has_patch_author(self, spec: AgentSpec) -> bool:
        return _runtime_key(spec) in self._patch_authors

    def prepare_patch_author(self, owner: AgentRecord) -> WritableBinding:
        template = self._patch_authors.get(_runtime_key(_record_spec(owner)))
        if template is None or owner.owner_agent_id != product_task_owner_id(template.task_id):
            raise ProductProfileError("product-patch-owner-unbound", owner.agent_id)
        spec = replace(template.assembly.spec, owner_agent_id=owner.agent_id)
        self._bind_spec(
            spec,
            source_id=template.source_id,
            revision=template.revision,
            access=WorkspaceAccess.WRITABLE,
            runtime=template.runtime,
        )
        return WritableBinding(
            spec,
            template.task_id,
            template.source_id,
            template.revision,
            agent_assembly_digest(template.assembly),
            fingerprint(
                ChildBudgetGrant(
                    template.runtime.budget,
                    template.runtime.retained_parent_tokens,
                    template.runtime.initial_tokens,
                )
            ),
        )

    def workspace_for_agent(self, spec: AgentSpec) -> WorkspaceProvisioningRequest:
        frozen = freeze_agent_spec(spec)
        request = self._workspaces.get(agent_spec_request_fingerprint(frozen))
        if request is None:
            raise ProductProfileError("product-workspace-binding-missing", frozen.preset)
        return request

    def grant_for_child(self, *, parent: AgentRecord, child: AgentSpec) -> ChildBudgetGrant:
        frozen = freeze_agent_spec(child)
        if frozen.owner_agent_id != parent.agent_id:
            raise ProductProfileError("product-budget-owner-mismatch", frozen.preset)
        grant = self._budgets.get(agent_spec_request_fingerprint(frozen))
        if grant is None:
            raise ProductProfileError("product-budget-binding-missing", frozen.preset)
        return grant

    def prepare_investigation(self, owner: AgentRecord) -> InvestigationBinding:
        template = self._investigations.get(_runtime_key(_record_spec(owner)))
        if template is None:
            raise ProductProfileError("product-investigation-owner-unbound", owner.agent_id)
        spec = replace(template.assembly.spec, owner_agent_id=owner.agent_id)
        self._bind_spec(
            spec,
            source_id=template.source_id,
            revision=template.revision,
            access=WorkspaceAccess.READ_ONLY,
            runtime=template.runtime,
        )
        return InvestigationBinding(spec, template.source_id, template.revision)

    def runtime_for(self, spec: AgentSpec) -> ProductRuntimeBinding:
        frozen = freeze_agent_spec(spec)
        binding = self._runtime.get(_runtime_key(frozen))
        if binding is None:
            raise ProductProfileError("product-runtime-binding-missing", frozen.preset)
        return binding

    def _bind_spec(
        self,
        spec: AgentSpec,
        *,
        source_id: str,
        revision: str,
        access: WorkspaceAccess,
        runtime: ProductRuntimeBinding,
    ) -> None:
        frozen = freeze_agent_spec(spec)
        exact = agent_spec_request_fingerprint(frozen)
        workspace = WorkspaceProvisioningRequest(
            source_id=source_id,
            revision=revision,
            access=access,
        )
        existing_workspace = self._workspaces.get(exact)
        if existing_workspace is not None and existing_workspace != workspace:
            raise ProductProfileError("product-workspace-binding-conflict", frozen.preset)
        self._workspaces[exact] = workspace
        if runtime.budget is not None:
            grant = ChildBudgetGrant(
                runtime.budget, runtime.retained_parent_tokens, runtime.initial_tokens
            )
            existing_budget = self._budgets.get(exact)
            if existing_budget is not None and existing_budget != grant:
                raise ProductProfileError("product-budget-binding-conflict", frozen.preset)
            self._budgets[exact] = grant
        key = _runtime_key(frozen)
        existing_runtime = self._runtime.get(key)
        if existing_runtime is not None and not _same_runtime(existing_runtime, runtime):
            raise ProductProfileError("product-runtime-binding-conflict", frozen.preset)
        self._runtime[key] = runtime


class ProductInvestigationPolicy:
    """Resolve only the approved main's readonly template and current catalog."""

    def __init__(self, bindings: ProductResourceBindings, workspaces: WorkspaceService):
        self._bindings = bindings
        self._workspaces = workspaces

    def prepare(self, owner: AgentRecord) -> InvestigationBinding:
        return self._bindings.prepare_investigation(owner)

    def planned_grant(self, owner: AgentRecord) -> ChildBudgetGrant:
        return self._bindings.grant_for_child(parent=owner, child=self.prepare(owner).spec)

    async def validate_child(self, owner: AgentRecord, child: AgentRecord) -> InvestigationBinding:
        binding = self.prepare(owner)
        if _runtime_key(_record_spec(child)) != _runtime_key(binding.spec):
            raise ProductProfileError("product-investigation-child-mismatch", child.agent_id)
        workspace = await self._workspaces.resolve_for_agent(child.agent_id)
        if (
            workspace.source_id != binding.source_id
            or workspace.base_revision != binding.revision
            or workspace.access is not WorkspaceAccess.READ_ONLY
        ):
            raise ProductProfileError("product-investigation-source-mismatch", child.agent_id)
        return binding


class ProductWritablePolicy:
    def __init__(self, bindings: ProductResourceBindings, workspaces: WorkspaceService):
        self._bindings = bindings
        self._workspaces = workspaces

    def prepare(self, owner: AgentRecord) -> WritableBinding:
        return self._bindings.prepare_patch_author(owner)

    def planned_grant(self, owner: AgentRecord) -> ChildBudgetGrant:
        return self._bindings.grant_for_child(parent=owner, child=self.prepare(owner).spec)

    async def validate_child(self, owner: AgentRecord, child: AgentRecord) -> WritableBinding:
        binding = self.prepare(owner)
        if _runtime_key(_record_spec(child)) != _runtime_key(binding.spec):
            raise ProductProfileError("product-patch-child-mismatch", child.agent_id)
        catalog = await self._workspaces.catalog()
        workspace = catalog.for_agent(child.agent_id)
        parent = catalog.for_agent(owner.agent_id)
        if (
            workspace is None
            or parent is None
            or workspace.workspace_id == parent.workspace_id
            or workspace.owner_agent_id != owner.agent_id
            or workspace.session_id != child.session_id
            or workspace.source_id != binding.source_id
            or parent.source_id != binding.source_id
            or workspace.base_revision != binding.revision
            or parent.base_revision != binding.revision
            or workspace.repository_fingerprint != parent.repository_fingerprint
            or workspace.access is not WorkspaceAccess.WRITABLE
        ):
            raise ProductProfileError("product-patch-source-mismatch", child.agent_id)
        return binding


def _record_spec(record: AgentRecord) -> AgentSpec:
    return AgentSpec(
        preset=record.preset,
        workspace_id=record.workspace_id,
        owner_agent_id=record.owner_agent_id,
        forked_from_session_id=record.forked_from_session_id,
        capability_grants=record.capability_grants,
        metadata=record.metadata,
    )


class ManagedProductTaskProvisioner:
    """Create the task root, grant its Budget, and settle its descendants."""

    __slots__ = (
        "_bindings",
        "_budgets",
        "_artifacts",
        "_closed",
        "_directory",
        "_lock",
        "_owned",
        "_supervisor",
        "_workspaces",
    )

    def __init__(
        self,
        supervisor: AgentSupervisor,
        budgets: BudgetLedgerService,
        workspaces: WorkspaceService,
        bindings: ProductResourceBindings,
        artifacts: PatchArtifactCatalogReader,
    ) -> None:
        self._supervisor = supervisor
        self._budgets = budgets
        self._workspaces = workspaces
        self._bindings = bindings
        self._artifacts = artifacts
        self._directory = AgentDirectoryReader(supervisor.store)
        self._owned: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def store(self) -> EventStore:
        return self._supervisor.store

    async def prepare(self, task_id: str, preflight: ProductPreflight) -> str:
        owner_id = product_task_owner_id(task_id)
        owner_spec = self._bindings.register(task_id, preflight)
        request_id = f"product-owner-create-{task_id}"
        session_id = f"product-owner-session-{task_id}"
        async with self._lock:
            if self._closed:
                raise ProductStateError("product-resources-closed", task_id)
            directory = await self._directory.load()
            existing = directory.get(owner_id)
            if existing is None:
                await self._supervisor.create(
                    owner_spec,
                    request_id=request_id,
                    agent_id=owner_id,
                    session_id=session_id,
                )
            else:
                expected = {
                    "agent_id": owner_id,
                    "session_id": session_id,
                    "request_id": request_id,
                    "preset": owner_spec.preset,
                    "workspace_id": existing.workspace_id,
                    "owner_agent_id": None,
                    "forked_from_session_id": None,
                    "capability_grants": [],
                    "metadata": {},
                }
                if not creation_matches(existing, expected):
                    raise ProductStateError("product-task-owner-conflict", task_id)
                await self._supervisor.resume(session_id)
            await self._budgets.grant_root(
                operation_id=f"product-budget-grant-{task_id}",
                agent_id=owner_id,
                limits=freeze_limits(preflight.profile.profile.task_budget),
            )
            self._owned[task_id] = owner_id
        return owner_id

    async def release(self, task_id: str, *, reason: str) -> None:
        owner_id = product_task_owner_id(task_id)
        workspace_reason = reason if reason in {"merged", "rejected"} else "explicit-release"
        async with self._lock:
            directory = await self._directory.load()
            postorder = AgentOwnershipGraph(directory).subtree_postorder(owner_id)
            if not postorder:
                self._owned.pop(task_id, None)
                return
            await self._supervisor.dispose(owner_id)
            catalog = await self._workspaces.catalog()
            artifacts = await self._artifacts.load()
            failures: list[BaseException] = []
            for agent_id in postorder:
                record = catalog.for_agent(agent_id)
                if record is None or record.status.value == "released":
                    continue
                try:
                    captures = tuple(
                        manifest
                        for manifest in artifacts
                        if manifest.workspace_id == record.workspace_id
                        and manifest.workspace_generation == record.updated_seq
                    )
                    if captures:
                        if len(captures) != 1:
                            raise ProductStateError("product-workspace-capture-conflict", task_id)
                        if reason in {"merged", "rejected"}:
                            await self._workspaces.release_captured(
                                record.workspace_id,
                                candidate_tree=captures[0].candidate_tree,
                                reason=reason,
                            )
                        else:
                            # Failure/cancellation has no authority to discard
                            # captured bytes.  The ordinary release path proves
                            # the worktree is dirty, records quarantine and
                            # preserves it for inspection.
                            try:
                                await self._workspaces.release(
                                    record.workspace_id,
                                    reason=workspace_reason,
                                )
                            except WorkspaceDirtyError:
                                continue
                    else:
                        await self._workspaces.release(
                            record.workspace_id,
                            reason=workspace_reason,
                        )
                except WorkspaceDirtyError as error:
                    if reason not in {"failed", "cancelled"}:
                        failures.append(error)
            ledger = await self._budgets.ledger()
            for agent_id in postorder:
                account = ledger.account(agent_id)
                if account is None or account.status.value == "closed":
                    continue
                try:
                    await self._budgets.close_account(
                        operation_id=f"product-budget-close-{task_id}-{agent_id}",
                        agent_id=agent_id,
                    )
                except BaseException as error:
                    failures.append(error)
            if failures:
                raise BaseExceptionGroup("Product resource release failed", failures)
            self._owned.pop(task_id, None)

    async def interrupt(self, task_id: str) -> None:
        """Stop this task's live ownership tree without discarding its worktree."""

        owner_id = product_task_owner_id(task_id)
        # Supervisor already owns the durable Directory/cached-Activation
        # reconciliation, including unknown/inactive identities and malformed
        # history fallback.  Re-reading it here would put a weaker gate in
        # front of the component that can actually converge the resources.
        await self._supervisor.dispose(owner_id)

    async def aclose(self) -> None:
        async with self._lock:
            self._closed = True
            self._owned.clear()
        failures: list[BaseException] = []
        try:
            await self._supervisor.aclose()
        except BaseException as error:
            failures.append(error)
        if failures:
            raise BaseExceptionGroup("Product resource close failed", failures)


def _runtime_key(spec: AgentSpec) -> str:
    # WorkspaceManagedAgentSupervisor replaces only workspace_id before the
    # activation factory sees the spec.  Normalize that delegated field while
    # retaining preset, owner, grants, lineage and metadata.
    return agent_spec_request_fingerprint(replace(spec, workspace_id="product-managed-workspace"))


def _same_runtime(left: ProductRuntimeBinding, right: ProductRuntimeBinding) -> bool:
    if left.budget != right.budget:
        return False
    if left.max_output_tokens != right.max_output_tokens:
        return False
    if (
        left.max_turn_wall_milliseconds != right.max_turn_wall_milliseconds
        or left.retained_parent_tokens != right.retained_parent_tokens
        or left.initial_tokens != right.initial_tokens
        or left.role != right.role
    ):
        return False
    if left.assembly is None or right.assembly is None:
        return left.assembly is right.assembly
    return agent_assembly_digest(left.assembly) == agent_assembly_digest(right.assembly)


__all__ = [
    "ManagedProductTaskProvisioner",
    "ProductResourceBindings",
    "ProductRuntimeBinding",
]
