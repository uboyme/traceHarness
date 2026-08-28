"""F3's real local boundary: Chat -> Workflow -> Review -> Promotion."""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
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
from traceh.api.promotion import VerifierCommand, VerifierOutcome
from traceh.api.workspaces import WorkspaceStatus
from traceh.artifacts.cas import LocalArtifactCas
from traceh.artifacts.catalog import PatchArtifactCatalogReader
from traceh.budgets.projection import BudgetLedgerReader
from traceh.cli.activity import Clock
from traceh.cli.chat import run_chat
from traceh.cli.console import Console
from traceh.product.chat import (
    ConfirmProductTaskTool,
    ProductTurnActions,
    ProposeProductTaskTool,
)
from traceh.product.events import MAX_REASON_DISPLAY_CHARS
from traceh.product.host import ProductHostProfile, build_product_chat_host
from traceh.product.projection import ProductTaskStreamReader
from traceh.product.runtime import READ_TOOL_IDS, WRITE_TOOL_IDS
from traceh.promotion.models import (
    expected_approval_digest,
    promotion_identity,
    verification_evidence_digest,
)
from traceh.promotion.projection import PromotionLedgerReader
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.session.event_store import InMemoryEventStore
from traceh.workspaces.catalog import WorkspaceCatalogReader
from traceh.workspaces.local_git import LocalGitWorkspaceProvider


class _Console:
    def __init__(self, inputs: tuple[str, ...]) -> None:
        self.inputs = list(inputs)
        self.lines: list[str] = []
        self.prompts: list[str] = []
        self.waiting = asyncio.Event()

    def read(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.inputs:
            raise EOFError
        return self.inputs.pop(0)

    def write(self, text: str) -> None:
        self.lines.append(text)
        if text.startswith("[waiting ") and "task product-task-" in text:
            self.waiting.set()

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
        confirmation_text: str = "yes, do it",
        requests: list[ModelRequest] | None = None,
    ) -> None:
        self.proposal_text = proposal_text
        self.proposal_mode = proposal_mode
        self.confirmation_text = confirmation_text
        self.requests = requests

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if self.requests is not None:
            self.requests.append(request)
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
        if last_user == self.confirmation_text:
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


class _ContractAwareRouterProvider(_ProductProvider):
    """Stay within the reason bound only when the host actually discloses it."""

    def __init__(self) -> None:
        super().__init__()
        self.saw_reason_bound = False

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.system_prompt and "routing classifier" in request.system_prompt:
            last_user = next(
                message.content
                for message in reversed(request.messages)
                if message.role == "user"
            )
            self.saw_reason_bound = (
                "reason must be null or" in last_user
                and f"at most {MAX_REASON_DISPLAY_CHARS} characters" in last_user
                and "no leading or trailing whitespace" in last_user
                and "Unicode categories Cc, Cf, Cs, Co, Zl, or Zp" in last_user
            )
            reason = (
                "one bounded role is sufficient"
                if self.saw_reason_bound
                else "x" * (MAX_REASON_DISPLAY_CHARS + 1)
            )
            return _response(f'{{"mode":"single","reason":"{reason}"}}')
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
            "product-parent",
            READ_TOOL_IDS,
            4_096,
            _limits(max_children=0, max_depth=0),
        ),
        reviewer=ProductRoleProfile(
            "product-reviewer",
            READ_TOOL_IDS,
            4_096,
            _limits(max_children=0, max_depth=0),
        ),
        coder=ProductRoleProfile(
            "product-coder",
            WRITE_TOOL_IDS,
            4_096,
            _limits(max_children=0, max_depth=0),
        ),
        router=ProductRouterProfile(
            "product-router",
            256,
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
    first = _Console(("please add the accepted file", "yes, do it", "START"))
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
    assert any(
        f"Start exact ProductTask {matched.group(1)}?" in prompt
        for prompt in first.prompts
    )
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


async def test_auto_router_receives_the_complete_reason_contract(
    tmp_path: Path,
) -> None:
    source, _ = build_source_repository(tmp_path / "source")
    target = make_bare_target(source, tmp_path / "target.git")
    store = InMemoryEventStore()
    provider = _ContractAwareRouterProvider()

    task_id, _, _ = await _run_to_barrier(
        tmp_path,
        store,
        source,
        target,
        LocalArtifactCas(tmp_path / "cas"),
        RequestedTaskMode.AUTO,
        provider,
    )

    assert provider.saw_reason_bound
    summary = await ProductTaskStreamReader(store).load(task_id)
    assert summary is not None
    assert summary.resolved_mode is ResolvedTaskMode.SINGLE


async def test_model_confirmation_cannot_start_without_explicit_host_authorization(
    tmp_path: Path,
) -> None:
    source, base = build_source_repository(tmp_path / "source")
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
    refusal = "do not start the proposed task"
    chat_requests: list[ModelRequest] = []
    runtime = _chat_runtime(
        tmp_path,
        store,
        actions,
        _ChatProvider(confirmation_text=refusal, requests=chat_requests),
    )
    workspace = tmp_path / "chat-workspace"
    workspace.mkdir()
    console = _Console(
        ("please add the accepted file", refusal, "NOT AUTHORIZED", "/exit")
    )

    assert await run_chat(
        runtime,
        console.console,
        workspace=workspace,
        timeline=False,
        product=product,
    ) == 0

    task_id = _proposed_task_id(console.output)
    assert await store.list_streams(prefix="product-task:") == ()
    assert not (await BudgetLedgerReader(store).load()).accounts
    assert not (await WorkspaceCatalogReader(store).load()).workspaces
    assert any(
        f"Start exact ProductTask {task_id}?" in prompt for prompt in console.prompts
    )
    assert f"task {task_id}: start not authorized" in console.output
    assert git("rev-parse", "refs/heads/main", cwd=target) == base
    assert all(
        message.content != "NOT AUTHORIZED"
        for request in chat_requests
        for message in request.messages
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
    console = _Console(
        ("please add the accepted file", "yes, do it", "START", "/exit")
    )

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
    console = _Console((requirement, "yes, do it", "START", "/exit"))

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


async def test_approval_screen_uses_durable_evidence_and_separate_request_caps(
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
        RequestedTaskMode.AUTO,
        _ProductProvider(requests),
    )

    routing = [
        request
        for request in requests
        if request.system_prompt and "routing classifier" in request.system_prompt
    ]
    execution = [request for request in requests if request not in routing]
    assert routing and execution
    assert {request.max_output_tokens for request in routing} == {256}
    assert {request.max_output_tokens for request in execution} == {4_096}
    assert "workflow nodes:" in output
    assert "changed paths (1)" in output
    assert "added.txt" in output
    assert "added-file: passed exit=0" in output
    assert "patch preview (" in output
    assert "diff --git" in output
    assert "replay: traceh replay" in output

    model_surface = repr(requests)
    assert "diff --git" not in model_surface
    assert "import pathlib,sys;sys.exit" not in model_surface


async def test_inspection_marks_tampered_patch_evidence_unavailable(
    tmp_path: Path,
) -> None:
    source, _ = build_source_repository(tmp_path / "source")
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
    manifest = (await PatchArtifactCatalogReader(store).load()).manifests[0]
    blob_path = cas.local_root / "sha256" / manifest.blob.sha256[:2] / manifest.blob.sha256
    blob_path.write_bytes(b"x" * manifest.blob.size_bytes)

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
    console = _Console((f"/task inspect {task_id}", "/exit"))

    assert await run_chat(
        runtime,
        console.console,
        session_id=session_id,
        timeline=False,
        product=product,
    ) == 0
    assert "evidence: unavailable (artifact-cas-collision)" in console.output
    assert "do not approve until the durable evidence can be read" in console.output
    assert "decision: do not approve; retry inspection or reject the task" in console.output
    assert "decision: /task approve" not in console.output
    assert "patch preview (" not in console.output


async def test_forged_verifier_result_blocks_inspection_and_direct_approval(
    tmp_path: Path,
) -> None:
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

    # Corrupt the durable history, then observe only public Product commands.
    # Recomputing the Review's internal evidence digest proves the missing
    # invariant is its binding to the host-frozen command, not basic shape.
    event = store._streams["patch-promotions:ledger"][0]
    assert event.type == "patch/review-recorded"
    result = dict(event.data["results"][0])
    forged = "9" * 64
    assert result["argv_digest"] != forged
    result["argv_digest"] = forged
    event.data["results"] = [result]
    event.data["verification_evidence_digest"] = verification_evidence_digest(
        event.data["verifier_definition_digest"],
        (VerifierOutcome(**result),),
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
    inspect_console = _Console((f"/task inspect {task_id}", "/exit"))
    assert await run_chat(
        _chat_runtime(tmp_path, store, actions),
        inspect_console.console,
        session_id=session_id,
        timeline=False,
        product=product,
    ) == 0

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
    approve_console = _Console(
        (f"/task approve {task_id}", f"/task reject {task_id}", "/exit")
    )
    assert await run_chat(
        _chat_runtime(tmp_path, store, actions),
        approve_console.console,
        session_id=session_id,
        timeline=False,
        product=product,
    ) == 0
    assert git("rev-parse", "refs/heads/main", cwd=target) == base
    assert (
        "evidence: unavailable (product-inspection-verifier-mismatch)"
        in inspect_console.output
    )
    assert "decision: do not approve" in inspect_console.output
    assert forged not in inspect_console.output
    assert (
        "task operation failed: promotion-review-verification-mismatch"
        in approve_console.output
    )
    assert f"task {task_id}: rejected" in approve_console.output


async def test_existing_promotion_recovery_revalidates_the_frozen_plan(
    tmp_path: Path,
) -> None:
    """A durable Promotion is not authority to bypass its owning service.

    Promotion can commit before the Product terminal is recorded. Recovery from
    that real prefix must re-enter the idempotent Promotion path, which binds the
    durable Review to the host-frozen plan before Product records success.
    """

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
    completed_console = _Console((f"/task approve {task_id}", "/exit"))
    assert await run_chat(
        _chat_runtime(tmp_path, store, actions),
        completed_console.console,
        session_id=session_id,
        timeline=False,
        product=product,
    ) == 0
    completed = await ProductTaskStreamReader(store).load(task_id)
    assert completed is not None
    assert completed.status is ProductTaskStatus.COMPLETED
    assert completed.review_id is not None
    assert completed.promotion_id is not None

    ledger = await PromotionLedgerReader(store).load()
    review = ledger.review(completed.review_id)
    promotion = ledger.promotion(completed.promotion_id)
    assert review is not None and promotion is not None

    # Recreate the durable crash prefix: Promotion committed, Product terminal
    # not yet appended. The target drift below also proves recovery does not
    # infer success merely from the existence of a receipt.
    product_stream = store._streams[f"product-task:{task_id}"]
    terminal = product_stream.pop()
    assert terminal.type == "product/task-completed"
    awaiting = await ProductTaskStreamReader(store).load(task_id)
    assert awaiting is not None
    assert awaiting.status is ProductTaskStatus.AWAITING_APPROVAL

    review_event = store._streams["patch-promotions:ledger"][0]
    assert review_event.type == "patch/review-recorded"
    result = dict(review_event.data["results"][0])
    replacement = "9" * 64
    assert result["argv_digest"] != replacement
    result["argv_digest"] = replacement
    results = (VerifierOutcome(**result),)
    evidence_digest = verification_evidence_digest(
        review_event.data["verifier_definition_digest"], results
    )
    review_event.data["results"] = [result]
    review_event.data["verification_evidence_digest"] = evidence_digest
    approval_digest = expected_approval_digest(
        replace(
            review,
            results=results,
            verification_evidence_digest=evidence_digest,
        )
    )
    replacement_promotion_id = promotion_identity(approval_digest)
    for event in store._streams["patch-promotions:ledger"]:
        if "approval_digest" in event.data:
            event.data["approval_digest"] = approval_digest
        if event.type == "patch/promotion-committed":
            event.data["promotion_id"] = replacement_promotion_id
    for event in store._streams[f"workflow:{awaiting.workflow_run_id}"]:
        if "approval_digest" in event.data:
            event.data["approval_digest"] = approval_digest

    git("update-ref", "refs/heads/main", base, cwd=target)
    assert git("rev-parse", "refs/heads/main", cwd=target) == base

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
    recovery_console = _Console((f"/task approve {task_id}", "/exit"))
    assert await run_chat(
        _chat_runtime(tmp_path, store, actions),
        recovery_console.console,
        session_id=session_id,
        timeline=False,
        product=product,
    ) == 0

    recovered = await ProductTaskStreamReader(store).load(task_id)
    assert recovered is not None
    assert recovered.status is ProductTaskStatus.AWAITING_APPROVAL
    assert (
        "task operation failed: promotion-review-verification-mismatch"
        in recovery_console.output
    )
    assert f"task {task_id}: completed" not in recovery_console.output
    assert git("rev-parse", "refs/heads/main", cwd=target) == base


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


class _GatedClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.waiters: asyncio.Queue[tuple[float, asyncio.Future[None]]] = (
            asyncio.Queue()
        )

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        gate = asyncio.get_running_loop().create_future()
        await self.waiters.put((delay, gate))
        await gate
        self.now += delay

    async def advance_live_waiter(self) -> None:
        while True:
            delay, gate = await asyncio.wait_for(self.waiters.get(), timeout=10)
            del delay
            if gate.done():
                continue
            gate.set_result(None)
            return


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
    console = _Console(("please add the accepted file", "yes, do it", "START"))
    gated_clock = _GatedClock()
    running = asyncio.create_task(
        run_chat(
            runtime,
            console.console,
            workspace=workspace,
            timeline=True,
            heartbeat_seconds=1,
            clock=Clock(gated_clock.monotonic, gated_clock.sleep),
            product=product,
        )
    )
    await asyncio.wait_for(provider.entered.wait(), timeout=10)
    task_id = _proposed_task_id(console.output)
    assert f"task {task_id}: confirmation accepted; starting execution" in console.output
    await gated_clock.advance_live_waiter()
    await asyncio.wait_for(console.waiting.wait(), timeout=10)
    assert f"task {task_id}: started; workflow=running; mode=single" in console.output

    running.cancel()
    assert await asyncio.wait_for(running, timeout=10) == 130
    assert not any(
        task.get_name().startswith("traceh-product-heartbeat-")
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
    )
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
