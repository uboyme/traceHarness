"""Built-in host assembly for the Product main Agent and readonly investigators.

This is an explicit component registry, not a default Product profile.  A
profile still names every preset, capability and Budget.  The resolver accepts
only shipped component ids whose runtime construction below is the same one
recorded in the F2 assembly digest; an unknown id fails instead of silently
falling back to a larger tool set.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from traceh.agents.directory import AgentDirectoryReader
from traceh.api.agents import (
    AgentRecord,
    AgentSpec,
    AgentSupervisor,
)
from traceh.api.llm import LlmProvider
from traceh.api.product import ProductRole, ProductRoleProfile
from traceh.api.prompts import PromptSection
from traceh.budgets.enforcement import BudgetEnforcement
from traceh.budgets.service import BudgetLedgerService
from traceh.llm.retry import NO_MODEL_RETRY, ModelRetryPolicy
from traceh.llm.token_meter import TokenBudgetPolicy
from traceh.product.collaboration import (
    CollaborationContinuation,
    CollaborationExecutionStopped,
    CollaborationPolicy,
)
from traceh.product.errors import ProductProfileError, ProductStateError
from traceh.product.registry import ResolvedAgentAssembly
from traceh.product.resources import (
    ProductInvestigationPolicy,
    ProductResourceBindings,
    ProductWritablePolicy,
)
from traceh.runtime.agent_runtime import AgentRuntime, RuntimeConfig, build_default_runtime
from traceh.runtime.continuation import DefaultContinuationRuntime
from traceh.runtime.prompt import PromptAssembler, default_coding_prompt
from traceh.session.compaction import CompactionPolicy
from traceh.session.event_store import EventStore
from traceh.session.tool_output import OUTPUT_TOOL_IDS
from traceh.supervision.authority import AgentToolAuthority
from traceh.supervision.delegation import INVESTIGATION_TOOL_IDS, InvestigationToolset
from traceh.supervision.execution import AgentExecution, AgentRuntimeExecution, durable_log_identity
from traceh.supervision.investigation_budget import (
    INVESTIGATOR_BUDGET_TOOLS,
    InvestigationBudgetContinuation,
    InvestigatorBudgetTool,
)
from traceh.supervision.investigation_wrap_up import (
    BoundReserve,
    InvestigationStepView,
    WrapUpReserve,
)
from traceh.supervision.patch_integration import (
    PATCH_INTEGRATION_TOOLS,
    PatchIntegrationControl,
    PatchIntegrationTool,
)
from traceh.supervision.structured_collaboration import (
    SUBMIT_COLLABORATION,
    CollaborationPlanTool,
    investigator_role,
    patch_author_role,
)
from traceh.supervision.writable_collaboration import (
    COLLECT_CHILD_PATCH,
    ChildPatchCollectTool,
    WritableControl,
)
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

#: How long any single tool call of a product Agent may hold. The ToolRuntime
#: enforces it, and the collaboration plan derives its in-call wait from the same
#: number, so an `await_report` plan cannot promise a wait the runtime cuts short.
TOOL_TIMEOUT_SECONDS = 60.0
READ_TOOL_IDS = ("list_files", "read_file", "search_text")
WRITE_TOOL_IDS = (*READ_TOOL_IDS, "apply_patch", "shell")
CODING_PROMPT_IDS = default_coding_prompt().section_ids()
MULTI_PROMPT_ID = "traceh.product.multi-collaboration"
MULTI_PROMPT_IDS = (*CODING_PROMPT_IDS, MULTI_PROMPT_ID)
MULTI_GUIDANCE = (
    "The confirmed execution mode is multi. Collaboration is required "
    "in this mode;\nyou are responsible for allocating work, not choosi"
    "ng whether to collaborate.\nAfter bounded readonly scouting, submi"
    "t one collaboration plan. The host starts\nevery assistant that plan "
    "authorizes, within its granted ceiling: you decide how\nmany independently "
    "deliverable units the work holds, and each becomes one\nassignment with its own "
    "identity, budget and workspace. The plan's handoff\nfield decides whether the host "
    "waits for their exact reports inside the dispatch\ncall, or returns the accepted "
    "identities immediately so you can continue retained\nwork while they run and collect "
    "each report yourself before delivering. Do not\nseparately create, retry, or fund "
    "agents. You remain the owner of your workspace\nand own integration, final "
    "verification, and delivery. An assistant report is a\nclaim with evidence, not "
    "approval or verified truth. Existing permissions,\nbudgets, source revision, and "
    "human approval requirements remain binding."
)

INVESTIGATION_PROMPT_IDS = (*CODING_PROMPT_IDS, "traceh.product.investigation")
PATCH_PROMPT_IDS = (*CODING_PROMPT_IDS, "traceh.product.patch-author")
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
        allowed = set(
            (
                *WRITE_TOOL_IDS,
                *INVESTIGATION_TOOL_IDS,
                SUBMIT_COLLABORATION,
                *PATCH_INTEGRATION_TOOLS,
                COLLECT_CHILD_PATCH,
                *OUTPUT_TOOL_IDS,
            )
            if role is ProductRole.CODER
            else (*READ_TOOL_IDS, "apply_patch", *OUTPUT_TOOL_IDS)
            if role is ProductRole.PATCH_AUTHOR
            else (*READ_TOOL_IDS, *INVESTIGATOR_BUDGET_TOOLS, *OUTPUT_TOOL_IDS)
        )
        if any(tool not in allowed for tool in tools):
            raise ProductProfileError("product-capability-unsupported", role.value)
        return ResolvedAgentAssembly(
            spec=AgentSpec(
                preset=profile.preset,
                workspace_id=f"product-{role.value}-workspace",
                capability_grants=tools,
            ),
            provider_id=provider_id,
            model_id=model_id,
            tool_ids=tools,
            prompt_ids=(
                PATCH_PROMPT_IDS
                if role is ProductRole.PATCH_AUTHOR
                else INVESTIGATION_PROMPT_IDS
                if role is ProductRole.INVESTIGATOR
                else MULTI_PROMPT_IDS
                if set(INVESTIGATION_TOOL_IDS) <= set(tools)
                else CODING_PROMPT_IDS
            ),
            policy_ids=PRODUCT_POLICY_IDS,
            workspace_access=role.workspace_access,
            context_policy=profile.context_policy,
            wrap_up_reserve=profile.wrap_up_reserve,
        )


class MultiAgentExecution(AgentRuntimeExecution):
    """A Product execution stop is not a completed multi deliverable.

    Budget's original runtime binding and lifecycle remain untouched. The
    Supervisor records failure if a terminal budget stop bypassed continuation.
    """

    async def run_turn(self, turn_input):
        result = await super().run_turn(turn_input)
        if result.reason != "completed":
            raise CollaborationExecutionStopped(result.reason)
        return result


class PatchAuthorExecution(AgentRuntimeExecution):
    """A writable assignment can execute one exact message, never a followup."""

    async def run_turn(self, turn_input):
        from traceh.agents.inbox import AgentInboxReader
        from traceh.supervision.writable_work import read_writable_work

        child = (await AgentDirectoryReader(self.event_store).load()).for_session(self.session_id)
        if child is None:
            raise ProductStateError("product-patch-child-missing", self.session_id)
        inbox = await AgentInboxReader(self.event_store).load(child.agent_id)
        work = read_writable_work(turn_input.content)
        if (
            len(inbox.messages) != 1
            or inbox.messages[0].message.message_id != turn_input.message_id
            or inbox.messages[0].message.content != turn_input.content
            or work["child_agent_id"] != child.agent_id
            or work["session_id"] != self.session_id
            or work["message_id"] != turn_input.message_id
            or work["owner_agent_id"] != child.owner_agent_id
            or turn_input.source != child.owner_agent_id
        ):
            raise ProductStateError("product-patch-followup-or-binding-invalid", child.agent_id)
        return await super().run_turn(turn_input)


class ProductAgentRuntimeFactory:
    """Create one exclusive, Budget-enforced Runtime per managed Agent."""

    __slots__ = (
        "_bindings",
        "_budgets",
        "_data_dir",
        "_providers",
        "_retry_policy",
        "_store",
        "_workspaces",
        "_context_input",
        "_memory_config",
        "_sandbox",
        "_supervisor",
        "_capture",
        "_slots",
        "_token_counter",
        "_token_margin_percent",
        "_verification_plan",
        "_verification_runner",
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
        retry_policy: ModelRetryPolicy = NO_MODEL_RETRY,
        token_estimate=None,
        context_input=None,
        memory_config=None,
        sandbox=None,
        verification_plan=None,
        verification_runner=None,
    ) -> None:
        self._store = store
        self._workspaces = workspaces
        self._bindings = bindings
        self._budgets = budgets
        self._data_dir = Path(data_dir).absolute()
        self._providers = dict(providers)
        self._retry_policy = retry_policy
        self._context_input = context_input
        self._memory_config = memory_config
        self._sandbox = sandbox
        self._supervisor: AgentSupervisor | None = None
        self._capture = None
        self._slots = None
        # Built once per host: an uncounted request has no provable upper bound,
        # so a reservation would have to assume the account's whole balance.
        self._token_counter = None
        self._token_margin_percent = 0
        if token_estimate is not None:
            from traceh.llm.token_meter import CanonicalTokenCounter

            self._token_counter = CanonicalTokenCounter(token_estimate.encoding)
            self._token_margin_percent = token_estimate.margin_percent
        self._verification_plan = verification_plan
        self._verification_runner = verification_runner

    def bind_process_slots(self, slots) -> None:
        """Share the activation process-slot authority with the plan pre-check."""
        if self._slots is not None:
            raise ProductStateError("product-process-slot-binding-invalid", "slots")
        self._slots = slots

    def bind_capture(self, capture):
        if self._capture is not None or durable_log_identity(
            capture.store
        ) is not durable_log_identity(self._store):
            raise ProductStateError("product-capture-binding-invalid", "capture")
        self._capture = capture

    def _execution(self, spec, runtime, session_id):
        binding = self._bindings.runtime_for(spec)
        kind = (
            PatchAuthorExecution
            if binding.role is ProductRole.PATCH_AUTHOR
            else MultiAgentExecution
            if binding.assembly is not None and SUBMIT_COLLABORATION in binding.assembly.tool_ids
            else AgentRuntimeExecution
        )
        return kind(runtime, session_id)

    def bind_supervisor(self, supervisor: AgentSupervisor) -> None:
        if self._supervisor is not None or durable_log_identity(
            supervisor.store
        ) is not durable_log_identity(self._store):
            raise ProductStateError("product-supervisor-binding-invalid", "supervisor")
        self._supervisor = supervisor

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
        runtime, enforcement = self._runtime(spec, agent_id=agent_id, session_id=session_id)
        created = await runtime.create_session(
            workspace.root, session_id=session_id, metadata={"product_agent": True}
        )
        execution = self._execution(spec, runtime, created)
        return (
            execution
            if enforcement is None
            else enforcement.wrap(
                execution,
                max_turn_wall_milliseconds=self._bindings.runtime_for(
                    spec
                ).max_turn_wall_milliseconds,
            )
        )

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
        execution = self._execution(spec, runtime, record.session_id)
        return (
            execution
            if enforcement is None
            else enforcement.wrap(
                execution,
                max_turn_wall_milliseconds=self._bindings.runtime_for(
                    spec
                ).max_turn_wall_milliseconds,
            )
        )

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
                    model_retry_policy=NO_MODEL_RETRY,
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
            raise ProductProfileError("product-provider-binding-missing", assembly.provider_id)
        limits = binding.budget
        if limits is None or type(limits.max_steps) is not int or limits.max_steps < 1:
            raise ProductProfileError("product-runtime-step-bound-missing", spec.preset)
        # The role grant can outlive this Turn. Reserve against the earlier
        # deadline used by enforcement, or delivery starts after cancellation.
        reserve_wall = binding.max_turn_wall_milliseconds
        if limits.max_wall_milliseconds is not None:
            reserve_wall = min(reserve_wall, limits.max_wall_milliseconds)
        wrap_up = (
            None if assembly.wrap_up_reserve is None else BoundReserve(
                WrapUpReserve(
                    steps=assembly.wrap_up_reserve.steps,
                    tool_calls=assembly.wrap_up_reserve.tool_calls,
                    wall_milliseconds=assembly.wrap_up_reserve.wall_milliseconds,
                ),
                replace(limits, max_wall_milliseconds=reserve_wall),
            )
        )
        prompt = _prompt(assembly)
        tools = _tools(assembly)
        continuation = DefaultContinuationRuntime()
        step_view = None
        completion = None
        if set(assembly.tool_ids) & set(INVESTIGATION_TOOL_IDS):
            if self._supervisor is None or not set(INVESTIGATION_TOOL_IDS) <= set(
                assembly.tool_ids
            ):
                raise ProductProfileError("product-investigation-binding-missing", spec.preset)
            investigations = InvestigationToolset(
                supervisor=self._supervisor,
                owner_agent_id=agent_id,
                event_store=self._store,
                policy=ProductInvestigationPolicy(self._bindings, self._workspaces),
                budgets=self._budgets,
            )
            plan_roles = (investigator_role(investigations.control),)
            dynamic_tools = (
                *investigations.tools,
                CollaborationPlanTool(
                    plan_roles, slots=self._slots, in_call_wait_seconds=TOOL_TIMEOUT_SECONDS
                ),
            )
            if self._bindings.has_patch_author(spec):
                if self._capture is None:
                    raise ProductProfileError("product-patch-capture-missing", spec.preset)
                writable = WritableControl(
                    supervisor=self._supervisor,
                    authority=AgentToolAuthority(
                        directory_reader=AgentDirectoryReader(self._store), owner_agent_id=agent_id
                    ),
                    policy=ProductWritablePolicy(self._bindings, self._workspaces),
                    capture=self._capture,
                    workspaces=self._workspaces,
                    budgets=self._budgets,
                )
                integration = PatchIntegrationControl(writable, self._capture.limits)
                from traceh.product.completion import WritableCompletionVerifier

                if self._verification_plan is None or self._verification_runner is None:
                    raise ProductProfileError("product-completion-binding-missing", spec.preset)
                completion = WritableCompletionVerifier(
                    integration, self._verification_plan, self._verification_runner,
                    agent_id=agent_id, session_id=session_id,
                )
                dynamic_tools = (
                    *investigations.tools,
                    CollaborationPlanTool(
                        (*plan_roles, patch_author_role(writable)),
                        slots=self._slots,
                        in_call_wait_seconds=TOOL_TIMEOUT_SECONDS,
                    ),
                    ChildPatchCollectTool(writable),
                    *(PatchIntegrationTool(integration, name) for name in PATCH_INTEGRATION_TOOLS),
                )
            from traceh.product.verification_review import WorkspaceDeliveryReader

            if self._capture is None:
                raise ProductProfileError("product-patch-capture-missing", spec.preset)
            step_view = CollaborationPolicy(
                WorkspaceDeliveryReader(
                    self._workspaces, self._capture.limits, agent_id, session_id
                ),
                verifier=completion,
                wrap_up=wrap_up,
            )
            continuation = CollaborationContinuation(self._store, session_id)

            tools = _role_tools(assembly, tools, dynamic_tools)
        if binding.role is ProductRole.INVESTIGATOR:
            budget_tools = tuple(
                InvestigatorBudgetTool(
                    name=name,
                    budgets=self._budgets,
                    child_id=agent_id,
                    session_id=session_id,
                    policy=ProductInvestigationPolicy(self._bindings, self._workspaces),
                )
                for name in INVESTIGATOR_BUDGET_TOOLS
                if name in assembly.tool_ids
            )
            tools = _role_tools(assembly, tools, budget_tools)
            continuation = InvestigationBudgetContinuation(self._store, agent_id, session_id)
            if wrap_up is not None:
                # Hold back part of the grant so a cancelled search cannot be
                # the only outcome: a real trial spent the whole 25-minute wall
                # clock investigating and delivered nothing at all.
                step_view = InvestigationStepView(
                    wrap_up.limits, wrap_up.reserve,
                )
        if step_view is None and wrap_up is not None:
            # A single coder has the same explicit delivery reservation but no
            # allocation or child-report obligations. Keep the original Step
            # view/withdrawal path without inventing a second execution loop.
            step_view = CollaborationPolicy(wrap_up=wrap_up, collaboration_required=False)
        policies = _policies(assembly, self._workspaces)
        enforcement = BudgetEnforcement(
            self._budgets,
            agent_id=agent_id,
            session_id=session_id,
            continuation=continuation,
            token_counter=self._token_counter,
            estimate_margin_percent=self._token_margin_percent,
        )
        token_budget, compaction = _context_governance(assembly)
        runtime = build_default_runtime(
            RuntimeConfig(
                data_dir=self._data_dir,
                provider=assembly.provider_id,
                model=assembly.model_id,
                max_steps=limits.max_steps,
                tool_timeout_seconds=TOOL_TIMEOUT_SECONDS,
                max_output_tokens=binding.max_output_tokens,
                token_budget=token_budget,
                compaction=compaction,
                model_retry_policy=self._retry_policy,
                context_input=None
                if binding.role in (ProductRole.INVESTIGATOR, ProductRole.PATCH_AUTHOR)
                else self._context_input,
                memory=None
                if binding.role in (ProductRole.INVESTIGATOR, ProductRole.PATCH_AUTHOR)
                else self._memory_config,
                sandbox=self._sandbox,
            ),
            provider=provider,
            event_store=self._store,
            include_default_tools=False,
            output_tool_ids=_output_tools(assembly),
            additional_tools=tools,
            prompt=prompt,
            policies=policies,
            continuation=enforcement.continuation,
            step_view=step_view,
            llm_runtime=enforcement.llm_runtime,
            tool_admission_gate=enforcement.tool_admission_gate,
        )
        return runtime, enforcement


def _role_tools(assembly: ResolvedAgentAssembly, static_tools, dynamic_tools) -> tuple:
    """Order a role's tools by its own grant list, skipping runtime-built ones.

    The read-back tools are assembled by the Runtime from its SessionService, so
    they are deliberately absent from both tuples here. Looking them up anyway
    is how the first profile that actually granted them failed at agent
    creation - after its workspace had already been provisioned - with a bare
    KeyError. A genuinely unbuildable grant still fails, with the same stable
    code the static assembler uses.
    """

    by_name = {tool.name: tool for tool in (*static_tools, *dynamic_tools)}
    missing = [
        name for name in assembly.tool_ids if name not in by_name and name not in OUTPUT_TOOL_IDS
    ]
    if missing:
        raise ProductProfileError("product-tool-binding-missing", missing[0])
    return tuple(by_name[name] for name in assembly.tool_ids if name not in OUTPUT_TOOL_IDS)


def _tools(assembly: ResolvedAgentAssembly):
    factories = {
        "list_files": ListFilesTool,
        "read_file": ReadFileTool,
        "search_text": SearchTextTool,
        "apply_patch": ApplyPatchTool,
        "shell": ShellTool,
    }
    try:
        return tuple(
            factories[name]()
            for name in assembly.tool_ids
            if name
            not in (
                *INVESTIGATION_TOOL_IDS,
                SUBMIT_COLLABORATION,
                COLLECT_CHILD_PATCH,
                *INVESTIGATOR_BUDGET_TOOLS,
                *PATCH_INTEGRATION_TOOLS,
                # Built by the runtime instead, because they need its Session
                # service; the profile still decides whether a role gets them.
                *OUTPUT_TOOL_IDS,
            )
        )
    except KeyError as error:
        raise ProductProfileError("product-tool-binding-missing", str(error)) from None


def _output_tools(assembly: ResolvedAgentAssembly) -> tuple[str, ...]:
    """The read-back tools this profile granted, in its own declared order."""

    return tuple(name for name in assembly.tool_ids if name in OUTPUT_TOOL_IDS)


def _context_governance(
    assembly: ResolvedAgentAssembly,
) -> tuple[TokenBudgetPolicy | None, CompactionPolicy | None]:
    """Turn one role's stated policy into the Runtime's two existing objects.

    Both are produced together or not at all. Metering without a compaction
    owner would measure the pressure and be unable to relieve it, and a
    compaction owner without metering is the Turn-front byte mode the long-task
    case already showed to be insufficient.
    """

    policy = assembly.context_policy
    if policy is None:
        return None, None
    return (
        TokenBudgetPolicy(
            encoding=policy.encoding,
            window_tokens=policy.window_tokens,
            output_reserve_tokens=policy.output_reserve_tokens,
            safety_margin_tokens=policy.safety_margin_tokens,
            trigger_percent=policy.trigger_percent,
            fold_relief_percent=policy.fold_relief_percent,
            fold_protect_recent_groups=policy.fold_protect_recent_groups,
            fold_protect_readback_utf8_bytes=policy.fold_protect_readback_utf8_bytes,
        ),
        CompactionPolicy(
            enabled=True,
            trigger_utf8_bytes=policy.compaction_trigger_utf8_bytes,
            max_summary_utf8_bytes=policy.compaction_max_summary_utf8_bytes,
            keep_recent_turns=policy.compaction_keep_recent_turns,
        ),
    )


def _prompt(assembly: ResolvedAgentAssembly) -> PromptAssembler:
    if assembly.prompt_ids == PATCH_PROMPT_IDS:
        return PromptAssembler(
            (
                *default_coding_prompt().sections(),
                PromptSection(
                    "traceh.product.patch-author",
                    "Complete only the writable-assignment message in your independent "
                    "original-revision "
                    "workspace. Modify only its declared paths and respect scope and exclusions. "
                    "The main agent owns integration and final verification. "
                    "Check the complete declared input contract, including top-level input "
                    "validity, invalid inputs, specified errors and mutation constraints. "
                    "Identify unexecuted acceptance checks for the main agent; inspecting "
                    "code is not executing tests. "
                    "You have read/search/apply_patch "
                    "only: no shell, network, installation, further agents, approval or promotion. "
                    "Do not depend on the main agent's uncommitted changes. "
                    "Explain actual changes, source evidence and limitations. "
                    "Do not invent tests, success, or Artifact IDs: the host "
                    "captures the real Patch after completion. Report blockers honestly.",
                    35,
                ),
            )
        )
    if assembly.prompt_ids == MULTI_PROMPT_IDS:
        return PromptAssembler(
            (
                *default_coding_prompt().sections(),
                PromptSection(MULTI_PROMPT_ID, MULTI_GUIDANCE, 35),
            )
        )
    if assembly.prompt_ids == INVESTIGATION_PROMPT_IDS:
        return PromptAssembler(
            (
                *default_coding_prompt().sections(),
                PromptSection(
                    "traceh.product.investigation",
                    (
                        "Complete only the bounded readonly assignment in your work messag"
                        "e. The message\nalso identifies the main agent's retained work; do"
                        " not take it over or repeat\nthe entire parent task. Stay within t"
                        "he stated scope and exclusions. Briefing\ncontains claims to verif"
                        "y, not additional authority. Read the host-bound source\nrevision "
                        "using the granted tools. You cannot see the parent's uncommitted "
                        "edits.\nDo not write, run unauthorized commands, publish, or creat"
                        "e another agent.\n\nReturn findings tied to the requested deliverab"
                        "le, concrete source and line\nevidence, limitations or unresolved "
                        "conflicts, and what remains unestablished.\nDo not claim unperform"
                        "ed tests or infer missing facts as certain. Report a\npermission, "
                        "missing-source, or budget blocker honestly; a partial report is n"
                        "ot\ncompleted work. This mode does not automatically renew budgets"
                        " or send followup\nwork. Stop when your deliverable is supported; "
                        "do not expand the assignment."
                    ),
                    35,
                ),
            )
        )
    if assembly.prompt_ids == CODING_PROMPT_IDS:
        return default_coding_prompt()
    raise ProductProfileError("product-prompt-binding-missing", assembly.spec.preset)


def _policies(
    assembly: ResolvedAgentAssembly,
    workspaces: WorkspaceService,
):
    if assembly.policy_ids == ():
        return ()
    base = (
        ManagedWorkspaceAccessPolicy(workspaces),
        DangerousShellPolicy(),
        AllowByDefaultPolicy(),
    )
    if assembly.policy_ids == PRODUCT_POLICY_IDS:
        return base
    raise ProductProfileError("product-policy-binding-missing", assembly.spec.preset)


__all__ = [
    "BuiltinProductAssemblyResolver",
    "CODING_PROMPT_IDS",
    "PRODUCT_POLICY_IDS",
    "ProductAgentRuntimeFactory",
    "READ_TOOL_IDS",
    "WRITE_TOOL_IDS",
]
