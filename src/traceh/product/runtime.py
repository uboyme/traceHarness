"""Built-in host assembly for Product Agents and the strict mode router.

This is an explicit component registry, not a default Product profile.  A
profile still names every preset, capability and Budget.  The resolver accepts
only shipped component ids whose runtime construction below is the same one
recorded in the F2 assembly digest; an unknown id fails instead of silently
falling back to a larger tool set.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from traceh.api.agents import (
    AgentMessage,
    AgentRecord,
    AgentSpec,
    AgentSupervisor,
    MessageTarget,
)
from traceh.api.llm import LlmProvider
from traceh.api.product import ProductRole, ProductRoleProfile, ProductRouterProfile
from traceh.api.prompts import PromptSection
from traceh.api.workspaces import WorkspaceAccess
from traceh.budgets.enforcement import BudgetEnforcement
from traceh.budgets.service import BudgetLedgerService
from traceh.product.errors import ProductProfileError, ProductStateError
from traceh.product.events import MAX_REASON_DISPLAY_CHARS
from traceh.product.registry import ResolvedAgentAssembly
from traceh.product.resources import ProductResourceBindings
from traceh.product.router import RouterResponder, RouterResponse
from traceh.runtime.agent_runtime import AgentRuntime, RuntimeConfig, build_default_runtime
from traceh.runtime.continuation import DefaultContinuationRuntime
from traceh.runtime.prompt import PromptAssembler, default_coding_prompt
from traceh.session.event_store import EventStore
from traceh.supervision.execution import AgentExecution, AgentRuntimeExecution
from traceh.tools.builtins import (
    ApplyPatchTool,
    ListFilesTool,
    ReadFileTool,
    SearchTextTool,
    ShellTool,
)
from traceh.tools.policy import AllowByDefaultPolicy, DangerousShellPolicy
from traceh.workspaces.policy import ManagedWorkspaceAccessPolicy
from traceh.workspaces.service import WorkspaceService

READ_TOOL_IDS = ("list_files", "read_file", "search_text")
WRITE_TOOL_IDS = (*READ_TOOL_IDS, "apply_patch", "shell")
CODING_PROMPT_IDS = default_coding_prompt().section_ids()
ROUTER_PROMPT_IDS = ("traceh.product.router",)
PRODUCT_POLICY_IDS = (
    "managed-workspace-access",
    "dangerous-shell",
    "allow-by-default",
)


class BuiltinProductAssemblyResolver:
    """Resolve a Profile only to components this host can actually construct."""

    async def role_assembly(
        self,
        *,
        role: ProductRole,
        profile: ProductRoleProfile,
        provider_id: str,
        model_id: str,
    ) -> ResolvedAgentAssembly:
        tools = tuple(profile.capability_grants)
        allowed = set(WRITE_TOOL_IDS if role is ProductRole.CODER else READ_TOOL_IDS)
        if any(tool not in allowed for tool in tools):
            raise ProductProfileError("product-capability-unsupported", role.value)
        return ResolvedAgentAssembly(
            spec=AgentSpec(
                preset=profile.preset,
                workspace_id=f"product-{role.value}-workspace",
                capability_grants=profile.capability_grants,
            ),
            provider_id=provider_id,
            model_id=model_id,
            tool_ids=tools,
            prompt_ids=CODING_PROMPT_IDS,
            policy_ids=PRODUCT_POLICY_IDS,
            workspace_access=role.workspace_access,
        )

    async def router_assembly(
        self,
        *,
        profile: ProductRouterProfile,
        provider_id: str,
        model_id: str,
    ) -> ResolvedAgentAssembly:
        return ResolvedAgentAssembly(
            spec=AgentSpec(
                preset=profile.preset,
                workspace_id="product-router-workspace",
            ),
            provider_id=provider_id,
            model_id=model_id,
            tool_ids=(),
            prompt_ids=ROUTER_PROMPT_IDS,
            policy_ids=(),
            workspace_access=WorkspaceAccess.READ_ONLY,
        )


class ProductAgentRuntimeFactory:
    """Create one exclusive, Budget-enforced Runtime per managed Agent."""

    __slots__ = (
        "_bindings",
        "_budgets",
        "_data_dir",
        "_providers",
        "_store",
        "_workspaces",
    )

    def __init__(
        self,
        store: EventStore,
        workspaces: WorkspaceService,
        bindings: ProductResourceBindings,
        budgets: BudgetLedgerService,
        *,
        data_dir: Path,
        providers: Mapping[str, LlmProvider],
    ) -> None:
        self._store = store
        self._workspaces = workspaces
        self._bindings = bindings
        self._budgets = budgets
        self._data_dir = Path(data_dir).absolute()
        self._providers = dict(providers)

    async def provision(
        self,
        spec: AgentSpec,
        *,
        agent_id: str,
        session_id: str | None,
    ) -> AgentExecution:
        if session_id is None:
            raise ProductStateError("product-agent-session-required", agent_id)
        workspace = await self._workspaces.resolve_for_creation(spec.workspace_id)
        runtime, enforcement = self._runtime(
            spec, agent_id=agent_id, session_id=session_id
        )
        created = await runtime.create_session(
            workspace.root, session_id=session_id, metadata={"product_agent": True}
        )
        execution = AgentRuntimeExecution(runtime, created)
        return execution if enforcement is None else enforcement.wrap(execution)

    async def activate(self, record: AgentRecord) -> AgentExecution:
        workspace = await self._workspaces.resolve_for_agent(record.agent_id)
        spec = AgentSpec(
            preset=record.preset,
            workspace_id=workspace.workspace_id,
            owner_agent_id=record.owner_agent_id,
            forked_from_session_id=record.forked_from_session_id,
            capability_grants=record.capability_grants,
            metadata=record.metadata,
        )
        runtime, enforcement = self._runtime(
            spec, agent_id=record.agent_id, session_id=record.session_id
        )
        execution = AgentRuntimeExecution(runtime, record.session_id)
        return execution if enforcement is None else enforcement.wrap(execution)

    def _runtime(
        self, spec: AgentSpec, *, agent_id: str, session_id: str
    ) -> tuple[AgentRuntime, BudgetEnforcement | None]:
        binding = self._bindings.runtime_for(spec)
        if binding.assembly is None:
            runtime = build_default_runtime(
                RuntimeConfig(
                    data_dir=self._data_dir,
                    provider="scripted",
                    model="product-owner",
                    max_steps=1,
                ),
                event_store=self._store,
                include_default_tools=False,
                prompt=PromptAssembler(),
                policies=(),
            )
            return runtime, None
        assembly = binding.assembly
        provider = self._providers.get(assembly.provider_id)
        if provider is None or provider.name != assembly.provider_id:
            raise ProductProfileError(
                "product-provider-binding-missing", assembly.provider_id
            )
        limits = binding.budget
        if limits is None or type(limits.max_steps) is not int or limits.max_steps < 1:
            raise ProductProfileError("product-runtime-step-bound-missing", spec.preset)
        prompt = _prompt(assembly)
        tools = _tools(assembly)
        policies = _policies(assembly, self._workspaces)
        enforcement = BudgetEnforcement(
            self._budgets,
            agent_id=agent_id,
            session_id=session_id,
            continuation=DefaultContinuationRuntime(),
        )
        runtime = build_default_runtime(
            RuntimeConfig(
                data_dir=self._data_dir,
                provider=assembly.provider_id,
                model=assembly.model_id,
                max_steps=limits.max_steps,
                max_output_tokens=binding.max_output_tokens,
            ),
            provider=provider,
            event_store=self._store,
            include_default_tools=False,
            additional_tools=tools,
            prompt=prompt,
            policies=policies,
            continuation=enforcement.continuation,
            llm_runtime=enforcement.llm_runtime,
            tool_admission_gate=enforcement.tool_admission_gate,
        )
        return runtime, enforcement


class ProductRouterAgentResponder(RouterResponder):
    """Run the F2 router as one real no-Tool managed Agent."""

    __slots__ = ("_bindings", "_supervisor")

    def __init__(
        self, supervisor: AgentSupervisor, bindings: ProductResourceBindings
    ) -> None:
        self._supervisor = supervisor
        self._bindings = bindings

    async def respond(self, summary: str, *, task_id: str) -> RouterResponse:
        from traceh.api.json_types import fingerprint

        digest = fingerprint({"purpose": "product-router", "task_id": task_id})
        agent_id = f"product-router-{digest}"
        session_id = f"product-router-session-{digest}"
        request_id = f"product-router-create-{digest}"
        message_id = f"product-router-message-{digest}"
        spec = self._bindings.router_spec(task_id)
        await self._supervisor.create(
            spec,
            request_id=request_id,
            agent_id=agent_id,
            session_id=session_id,
        )
        try:
            message = AgentMessage(
                message_id=message_id,
                source=f"product:{task_id}",
                content=(
                    "Choose the smaller safe execution shape. Reply with exactly "
                    "one JSON object with exactly the keys mode and reason; mode "
                    "must be exactly single or multi; reason must be null or a "
                    "non-empty single-line string of at most "
                    f"{MAX_REASON_DISPLAY_CHARS} characters with no leading or "
                    "trailing whitespace and no characters in Unicode categories "
                    f"Cc, Cf, Cs, Co, Zl, or Zp. Return no other text.\n\n"
                    f"Task summary:\n{summary}"
                ),
            )
            await self._supervisor.send(
                agent_id,
                message,
                target=MessageTarget.NEW_TURN,
                wakeup=True,
            )
            report = await self._supervisor.wait_message(agent_id, message_id)
            if report.status != "completed":
                raise ProductStateError("product-router-agent-failed", task_id)
            return RouterResponse(
                text=report.final_text,
                router_agent_id=agent_id,
                routing_session_id=session_id,
            )
        finally:
            await self._supervisor.dispose(agent_id)


def _tools(assembly: ResolvedAgentAssembly):
    factories = {
        "list_files": ListFilesTool,
        "read_file": ReadFileTool,
        "search_text": SearchTextTool,
        "apply_patch": ApplyPatchTool,
        "shell": ShellTool,
    }
    try:
        return tuple(factories[name]() for name in assembly.tool_ids)
    except KeyError as error:
        raise ProductProfileError("product-tool-binding-missing", str(error)) from None


def _prompt(assembly: ResolvedAgentAssembly) -> PromptAssembler:
    if assembly.prompt_ids == CODING_PROMPT_IDS:
        return default_coding_prompt()
    if assembly.prompt_ids == ROUTER_PROMPT_IDS:
        return PromptAssembler(
            (
                PromptSection(
                    "traceh.product.router",
                    "You are a bounded routing classifier. Follow the requested "
                    "JSON schema exactly and do not perform the task.",
                    10,
                ),
            )
        )
    raise ProductProfileError("product-prompt-binding-missing", assembly.spec.preset)


def _policies(
    assembly: ResolvedAgentAssembly, workspaces: WorkspaceService
):
    if assembly.policy_ids == ():
        return ()
    if assembly.policy_ids != PRODUCT_POLICY_IDS:
        raise ProductProfileError("product-policy-binding-missing", assembly.spec.preset)
    return (
        ManagedWorkspaceAccessPolicy(workspaces),
        DangerousShellPolicy(),
        AllowByDefaultPolicy(),
    )


__all__ = [
    "BuiltinProductAssemblyResolver",
    "CODING_PROMPT_IDS",
    "PRODUCT_POLICY_IDS",
    "ProductAgentRuntimeFactory",
    "ProductRouterAgentResponder",
    "READ_TOOL_IDS",
    "ROUTER_PROMPT_IDS",
    "WRITE_TOOL_IDS",
]
