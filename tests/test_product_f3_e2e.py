"""F3's real local boundary: Chat -> Workflow -> Review -> Promotion."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from promotion_fixtures import (
    build_source_repository,
    capture_limits,
    git,
    make_bare_target,
    promotion_targets,
    verification_plan,
)

from traceh.api.budgets import (
    BudgetAccountStatus,
    BudgetLimits,
    BudgetReservationStatus,
    BudgetUsageReservationStatus,
)
from traceh.api.llm import ModelRequest, ModelResponse, ToolCall, Usage, UsageQuality
from traceh.api.product import (
    ProductRoleProfile,
    ProductRouterProfile,
    ProductTaskProfile,
    ProductTaskStatus,
    RequestedTaskMode,
    ResolvedTaskMode,
    TaskModeSource,
)
from traceh.api.promotion import VerifierCommand
from traceh.api.workspaces import WorkspaceStatus
from traceh.artifacts.cas import LocalArtifactCas
from traceh.budgets.projection import BudgetLedgerReader
from traceh.cli.chat import run_chat
from traceh.cli.console import Console
from traceh.product.chat import (
    ConfirmProductTaskTool,
    ProductTurnActions,
    ProposeProductTaskTool,
)
from traceh.product.host import ProductHostProfile, build_product_chat_host
from traceh.product.projection import ProductTaskStreamReader
from traceh.product.runtime import READ_TOOL_IDS, WRITE_TOOL_IDS
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.session.event_store import InMemoryEventStore
from traceh.workspaces.catalog import WorkspaceCatalogReader
from traceh.workspaces.local_git import LocalGitWorkspaceProvider


class _Console:
    def __init__(self, inputs: tuple[str, ...]) -> None:
        self.inputs = list(inputs)
        self.lines: list[str] = []

    def read(self, prompt: str) -> str:
        del prompt
        if not self.inputs:
            raise EOFError
        return self.inputs.pop(0)

    def write(self, text: str) -> None:
        self.lines.append(text)

    @property
    def console(self) -> Console:
        return Console(read_line=self.read, write=self.write)

    @property
    def output(self) -> str:
        return "\n".join(self.lines)


class _ChatProvider:
    name = "scripted"

    def __init__(
        self,
        *,
        proposal_text: str = "please add the accepted file",
        proposal_mode: RequestedTaskMode | None = None,
    ) -> None:
        self.proposal_text = proposal_text
        self.proposal_mode = proposal_mode

    async def complete(self, request: ModelRequest) -> ModelResponse:
        last_user = next(
            message.content
            for message in reversed(request.messages)
            if message.role == "user"
        )
        after_user = tuple(request.messages)[
            max(
                index
                for index, message in enumerate(request.messages)
                if message.role == "user"
            )
            + 1 :
        ]
        if any(message.role == "tool" for message in after_user):
            return _response("host action proposed")
        if last_user == self.proposal_text:
            arguments: dict[str, str] = {"requirement": last_user}
            if self.proposal_mode is not None:
                arguments["mode"] = self.proposal_mode.value
            return _response(
                "",
                ToolCall(
                    id="propose-call",
                    name="propose_product_task",
                    arguments=arguments,
                ),
            )
        if last_user == "yes, do it":
            return _response(
                "",
                ToolCall(
                    id="confirm-call",
                    name="confirm_product_task",
                    arguments={},
                ),
            )
        return _response("ordinary chat answer")


class _ProductProvider:
    name = "product-provider"

    def __init__(self, requests: list[ModelRequest] | None = None) -> None:
        self.requests = requests

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if self.requests is not None:
            self.requests.append(request)
        if request.system_prompt and "routing classifier" in request.system_prompt:
            return _response('{"mode":"multi","reason":"two reviews reduce risk"}')
        tool_names = {tool.name for tool in request.tools}
        if "apply_patch" not in tool_names:
            return _response("bounded analysis")
        if not any(message.role == "tool" for message in request.messages):
            return _response(
                "",
                ToolCall(
                    id="create-file",
                    name="apply_patch",
                    arguments={
                        "path": "added.txt",
                        "old_text": "",
                        "new_text": "added\n",
                        "create": True,
                    },
                ),
            )
        return _response("implemented and checked")


class _InvalidRouterProvider(_ProductProvider):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.system_prompt and "routing classifier" in request.system_prompt:
            return _response("not-json")
        return await super().complete(request)


def _response(content: str, *calls: ToolCall) -> ModelResponse:
    return ModelResponse(
        content=content,
        tool_calls=tuple(calls),
        usage=Usage(10, 5, UsageQuality.EXACT),
    )


def _limits(**changes: int | None) -> BudgetLimits:
    values: dict[str, int | None] = {
        "max_tokens": 20_000,
        "max_steps": 12,
        "max_tool_calls": 20,
        "max_wall_milliseconds": 120_000,
        "max_children": 4,
        "max_depth": 1,
        "max_processes": 4,
    }
    values.update(changes)
    return BudgetLimits(**values)  # type: ignore[arg-type]


def _profile(mode: RequestedTaskMode) -> ProductTaskProfile:
    return ProductTaskProfile(
        profile_version=1,
        default_mode=mode,
        provider_id="product-provider",
        model_id="product-model",
        parent=ProductRoleProfile(
            "product-parent", READ_TOOL_IDS, _limits(max_children=0, max_depth=0)
        ),
        reviewer=ProductRoleProfile(
            "product-reviewer", READ_TOOL_IDS, _limits(max_children=0, max_depth=0)
        ),
        coder=ProductRoleProfile(
            "product-coder", WRITE_TOOL_IDS, _limits(max_children=0, max_depth=0)
        ),
        router=ProductRouterProfile(
            "product-router",
            _limits(max_steps=2, max_tool_calls=0, max_children=0, max_depth=0),
            30_000,
            2_048,
        ),
        task_budget=_limits(
            max_tokens=100_000,
            max_steps=100,
            max_tool_calls=100,
            max_wall_milliseconds=600_000,
        ),
        source_id="product-source",
        source_revision="main",
        verification_plan_id="product-plan",
        promotion_target_id="product-target",
    )


async def _build_host(
    tmp_path: Path,
    store: InMemoryEventStore,
    source: Path,
    target: Path,
    cas: LocalArtifactCas,
    actions: ProductTurnActions,
    mode: RequestedTaskMode,
    product_provider: object | None = None,
):
    import sys

    plan = verification_plan(
        VerifierCommand(
            command_id="added-file",
            argv=(
                sys.executable,
                "-c",
                "import pathlib,sys;sys.exit(0 if "
                "pathlib.Path('added.txt').read_text() == 'added\\n' else 1)",
            ),
            timeout_ms=60_000,
        ),
        plan_id="product-plan",
    )
    return await build_product_chat_host(
        store=store,
        data_dir=tmp_path / "product-data",
        host_profile=ProductHostProfile(
            "product-profile", _profile(mode), plan
        ),
        providers={"product-provider": product_provider or _ProductProvider()},
        workspace_provider=LocalGitWorkspaceProvider(
            managed_root=tmp_path / "managed",
            sources={"product-source": source},
        ),
        artifact_cas=cas,
        promotion_targets=promotion_targets("product-target", target),
        capture_limits=capture_limits(),
        approver_id="local-human",
        max_report_chars=4_096,
        actions=actions,
    )


def _chat_runtime(
    tmp_path: Path,
    store: InMemoryEventStore,
    actions: ProductTurnActions,
    provider: _ChatProvider | None = None,
):
    return build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "chat-data",
            provider="scripted",
            model="chat-model",
            max_steps=8,
        ),
        provider=provider or _ChatProvider(),
        event_store=store,
        additional_tools=(
            ProposeProductTaskTool(actions),
            ConfirmProductTaskTool(actions),
        ),
    )


async def _run_to_barrier(
    tmp_path: Path,
    store: InMemoryEventStore,
    source: Path,
    target: Path,
    cas: LocalArtifactCas,
    mode: RequestedTaskMode,
    product_provider: object | None = None,
) -> tuple[str, str, str]:
    actions = ProductTurnActions()
    product = await _build_host(
        tmp_path, store, source, target, cas, actions, mode, product_provider
    )
    runtime = _chat_runtime(tmp_path, store, actions)
    first = _Console(("please add the accepted file", "yes, do it"))
    chat_workspace = tmp_path / "chat-workspace"
    chat_workspace.mkdir(exist_ok=True)
    assert await run_chat(
        runtime,
        first.console,
        workspace=chat_workspace,
        timeline=False,
        product=product,
    ) == 0
    matched = re.search(r"task ([^:]+): awaiting_approval", first.output)
    assert matched is not None, first.output
    assert "requirement: please add the accepted file" in first.output
    session_match = re.search(r"session_id=([^ ]+)", first.output)
    assert session_match is not None
    return matched.group(1), session_match.group(1), first.output


def _proposed_task_id(output: str) -> str:
    matched = re.search(r"task if confirmed: ([^\s]+)", output)
    assert matched is not None, output
    return matched.group(1)


@pytest.mark.parametrize(
    ("mode", "resolved"),
    (
        (RequestedTaskMode.SINGLE, ResolvedTaskMode.SINGLE),
        (RequestedTaskMode.MULTI, ResolvedTaskMode.MULTI),
        (RequestedTaskMode.AUTO, ResolvedTaskMode.MULTI),
    ),
)
async def test_chat_task_modes_pause_restart_and_promote(
    tmp_path: Path,
    mode: RequestedTaskMode,
    resolved: ResolvedTaskMode,
) -> None:
    source, base = build_source_repository(tmp_path / "source")
    target = make_bare_target(source, tmp_path / "target.git")
    store = InMemoryEventStore()
    cas = LocalArtifactCas(tmp_path / "cas")
    task_id, session_id, _ = await _run_to_barrier(
        tmp_path, store, source, target, cas, mode
    )
    summary = await ProductTaskStreamReader(store).load(task_id)
    assert summary is not None and summary.resolved_mode is resolved

    # A new host owns no process-local receipt.  It continues only from the
    # ProductTask, Workflow, Artifact and Review facts identified by task_id.
    actions = ProductTurnActions()
    product = await _build_host(
        tmp_path, store, source, target, cas, actions, mode
    )
    runtime = _chat_runtime(tmp_path, store, actions)
    second = _Console((f"/task approve {task_id}", "/exit"))
    assert await run_chat(
        runtime,
        second.console,
        session_id=session_id,
        timeline=False,
        product=product,
    ) == 0
    assert f"task {task_id}: completed" in second.output
    assert git("rev-parse", "refs/heads/main", cwd=target) != base
    workspaces = await WorkspaceCatalogReader(store).load()
    assert workspaces.workspaces
    assert all(
        record.status is WorkspaceStatus.RELEASED
        for record in workspaces.workspaces
    )
    ledger = await BudgetLedgerReader(store).load()
    assert ledger.accounts
    assert all(
        account.status is BudgetAccountStatus.CLOSED for account in ledger.accounts
    )
    assert all(
        item.status
        in {BudgetReservationStatus.COMMITTED, BudgetReservationStatus.RELEASED}
        for item in ledger.reservations
    )
    assert all(
        item.status
        in {
            BudgetUsageReservationStatus.SETTLED,
            BudgetUsageReservationStatus.RELEASED,
        }
        for item in ledger.usage_reservations
    )


async def test_router_failure_releases_resources_and_returns_the_durable_task(
    tmp_path: Path,
) -> None:
    source, _ = build_source_repository(tmp_path / "source")
    target = make_bare_target(source, tmp_path / "target.git")
    store = InMemoryEventStore()
    actions = ProductTurnActions()
    product = await _build_host(
        tmp_path,
        store,
        source,
        target,
        LocalArtifactCas(tmp_path / "cas"),
        actions,
        RequestedTaskMode.AUTO,
        _InvalidRouterProvider(),
    )
    runtime = _chat_runtime(tmp_path, store, actions)
    workspace = tmp_path / "chat-workspace"
    workspace.mkdir()
    console = _Console(("please add the accepted file", "yes, do it", "/exit"))

    assert await run_chat(
        runtime,
        console.console,
        workspace=workspace,
        timeline=False,
        product=product,
    ) == 0

    task_id = _proposed_task_id(console.output)
    summary = await ProductTaskStreamReader(store).load(task_id)
    assert summary is not None
    assert summary.status is ProductTaskStatus.FAILED
    assert summary.failure_code == "product-router-response-unparsable"
    assert f"task {task_id}: failed" in console.output
    assert "failure: product-router-response-unparsable" in console.output
    ledger = await BudgetLedgerReader(store).load()
    assert ledger.accounts
    assert all(
        account.status is BudgetAccountStatus.CLOSED for account in ledger.accounts
    )
    catalog = await WorkspaceCatalogReader(store).load()
    assert catalog.workspaces
    assert all(
        workspace_record.status is WorkspaceStatus.RELEASED
        for workspace_record in catalog.workspaces
    )


async def test_confirmed_single_mode_bypasses_an_auto_profiles_router(
    tmp_path: Path,
) -> None:
    source, _ = build_source_repository(tmp_path / "source")
    target = make_bare_target(source, tmp_path / "target.git")
    store = InMemoryEventStore()
    actions = ProductTurnActions()
    product_requests: list[ModelRequest] = []
    product = await _build_host(
        tmp_path,
        store,
        source,
        target,
        LocalArtifactCas(tmp_path / "cas"),
        actions,
        RequestedTaskMode.AUTO,
        _ProductProvider(product_requests),
    )
    requirement = "please add the accepted file using single"
    runtime = _chat_runtime(
        tmp_path,
        store,
        actions,
        _ChatProvider(
            proposal_text=requirement,
            proposal_mode=RequestedTaskMode.SINGLE,
        ),
    )
    workspace = tmp_path / "chat-workspace"
    workspace.mkdir()
    console = _Console((requirement, "yes, do it", "/exit"))

    assert await run_chat(
        runtime,
        console.console,
        workspace=workspace,
        timeline=False,
        product=product,
    ) == 0

    task_id = _proposed_task_id(console.output)
    summary = await ProductTaskStreamReader(store).load(task_id)
    assert summary is not None
    assert summary.requested_mode is RequestedTaskMode.SINGLE
    assert summary.mode_source is TaskModeSource.CONFIRMED_PROPOSAL
    assert summary.resolved_mode is ResolvedTaskMode.SINGLE
    assert "mode:     single" in console.output
    assert "mode source: confirmed_proposal" in console.output
    assert not any(
        request.system_prompt and "routing classifier" in request.system_prompt
        for request in product_requests
    )


async def test_rejecting_the_review_does_not_move_the_target(tmp_path: Path) -> None:
    source, base = build_source_repository(tmp_path / "source")
    target = make_bare_target(source, tmp_path / "target.git")
    store = InMemoryEventStore()
    cas = LocalArtifactCas(tmp_path / "cas")
    task_id, session_id, _ = await _run_to_barrier(
        tmp_path,
        store,
        source,
        target,
        cas,
        RequestedTaskMode.SINGLE,
    )
    actions = ProductTurnActions()
    product = await _build_host(
        tmp_path,
        store,
        source,
        target,
        cas,
        actions,
        RequestedTaskMode.SINGLE,
    )
    runtime = _chat_runtime(tmp_path, store, actions)
    console = _Console((f"/task reject {task_id}", "/exit"))
    assert await run_chat(
        runtime,
        console.console,
        session_id=session_id,
        timeline=False,
        product=product,
    ) == 0
    assert f"task {task_id}: rejected" in console.output
    assert git("rev-parse", "refs/heads/main", cwd=target) == base


async def test_model_requests_never_receive_review_or_approval_values(
    tmp_path: Path,
) -> None:
    source, _ = build_source_repository(tmp_path / "source")
    target = make_bare_target(source, tmp_path / "target.git")
    requests: list[ModelRequest] = []
    _, _, output = await _run_to_barrier(
        tmp_path,
        InMemoryEventStore(),
        source,
        target,
        LocalArtifactCas(tmp_path / "cas"),
        RequestedTaskMode.SINGLE,
        _ProductProvider(requests),
    )
    sensitive = []
    for label in ("review", "patch_sha256", "approval_digest"):
        matched = re.search(rf"{label}: ([^\s]+)", output)
        assert matched is not None, (label, output)
        sensitive.append(matched.group(1))

    model_surface = repr(requests)
    assert requests
    assert all(value not in model_surface for value in sensitive)


async def test_ordinary_chat_creates_no_product_task(tmp_path: Path) -> None:
    source, _ = build_source_repository(tmp_path / "source")
    target = make_bare_target(source, tmp_path / "target.git")
    store = InMemoryEventStore()
    actions = ProductTurnActions()
    product = await _build_host(
        tmp_path,
        store,
        source,
        target,
        LocalArtifactCas(tmp_path / "cas"),
        actions,
        RequestedTaskMode.SINGLE,
    )
    runtime = _chat_runtime(tmp_path, store, actions)
    workspace = tmp_path / "chat-workspace"
    workspace.mkdir()
    console = _Console(("hello, just answer normally", "/exit"))
    assert await run_chat(
        runtime,
        console.console,
        workspace=workspace,
        timeline=False,
        product=product,
    ) == 0
    assert await store.list_streams(prefix="product-task:") == ()


class _GatedProductProvider(_ProductProvider):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if "apply_patch" in {tool.name for tool in request.tools}:
            self.entered.set()
            await asyncio.Event().wait()
        return await super().complete(request)


async def test_interrupting_confirmation_converges_owned_work_without_promotion(
    tmp_path: Path,
) -> None:
    source, base = build_source_repository(tmp_path / "source")
    target = make_bare_target(source, tmp_path / "target.git")
    store = InMemoryEventStore()
    actions = ProductTurnActions()
    provider = _GatedProductProvider()
    product = await _build_host(
        tmp_path,
        store,
        source,
        target,
        LocalArtifactCas(tmp_path / "cas"),
        actions,
        RequestedTaskMode.SINGLE,
        provider,
    )
    runtime = _chat_runtime(tmp_path, store, actions)
    workspace = tmp_path / "chat-workspace"
    workspace.mkdir()
    console = _Console(("please add the accepted file", "yes, do it"))
    running = asyncio.create_task(
        run_chat(
            runtime,
            console.console,
            workspace=workspace,
            timeline=False,
            product=product,
        )
    )
    await asyncio.wait_for(provider.entered.wait(), timeout=10)

    running.cancel()
    assert await asyncio.wait_for(running, timeout=10) == 130
    task_id = _proposed_task_id(console.output)
    assert await store.list_streams(prefix="product-task:") == (
        f"product-task:{task_id}",
    )
    summary = await ProductTaskStreamReader(store).load(task_id)
    assert summary is not None and summary.status.value == "started"
    assert git("rev-parse", "refs/heads/main", cwd=target) == base
    ledger = await BudgetLedgerReader(store).load()
    assert all(
        item.status in {
            BudgetUsageReservationStatus.SETTLED,
            BudgetUsageReservationStatus.RELEASED,
        }
        for item in ledger.usage_reservations
    )
