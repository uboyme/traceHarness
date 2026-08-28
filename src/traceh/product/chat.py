"""Host rendering and low-authority model actions for unified chat.

The two model Tools only leave a process-local note about the current Turn.
They cannot open a ProductTask, choose a mode, inspect a Review, approve or
promote.  After the Turn is durably closed, the chat host combines that note
with the user message identity it supplied; a confirmation suggestion must
then cross a separate exact-task terminal authorization before the host invokes
the control plane.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from traceh.api.json_types import JsonValue
from traceh.api.product import ProductTaskViewStatus, RequestedTaskMode
from traceh.api.tools import EffectKind, ToolExecutionContext, ToolOutput
from traceh.api.turns import TurnInput
from traceh.cli.activity import Clock, default_clock
from traceh.cli.command_line import (
    Literal,
    UnsafeCommandValue,
    escape_for_display,
    render_command,
)
from traceh.cli.console import (
    Console,
    contains_undecodable_input,
    normalize_input,
)
from traceh.concurrency import await_worker_convergence
from traceh.product.control import (
    PendingProductProposal,
    ProductAdvanceResult,
    ProductInspection,
    ProductTaskControlPlane,
)
from traceh.product.errors import ProductInputError
from traceh.product.inspection import (
    ProductInspectionEvidenceReader,
    ProductTaskEvidence,
)
from traceh.product.router import MAX_ROUTER_SUMMARY_CHARS


@dataclass(frozen=True, slots=True)
class ProductChatTurn:
    turn_input: TurnInput
    had_pending_proposal: bool


@dataclass(frozen=True, slots=True)
class _TurnAction:
    kind: str
    requirement: str | None = None
    requested_mode: RequestedTaskMode | None = None


class ProductTurnActions:
    """Ephemeral structured actions emitted inside one live Chat Turn."""

    __slots__ = ("_actions", "_lock")

    def __init__(self) -> None:
        self._actions: dict[tuple[str, str], _TurnAction] = {}
        self._lock = asyncio.Lock()

    async def record(
        self, session_id: str, turn_id: str, action: _TurnAction
    ) -> None:
        key = (session_id, turn_id)
        async with self._lock:
            if key in self._actions:
                raise ProductInputError("product-turn-action-duplicate", "action")
            self._actions[key] = action

    async def take(self, session_id: str, turn_id: str) -> _TurnAction | None:
        async with self._lock:
            return self._actions.pop((session_id, turn_id), None)

    async def discard(self, session_id: str, turn_id: str) -> None:
        async with self._lock:
            self._actions.pop((session_id, turn_id), None)

    async def discard_session(self, session_id: str) -> None:
        async with self._lock:
            stale = [key for key in self._actions if key[0] == session_id]
            for key in stale:
                self._actions.pop(key, None)


class ProposeProductTaskTool:
    """Let the chat model suggest one bounded host-rendered Proposal."""

    name = "propose_product_task"
    description = (
        "Suggest turning the user's coding requirement into a controlled task. "
        "If the user explicitly requested single, multi, or auto, include that "
        "mode; otherwise omit it and let the host Profile decide. This only "
        "asks the host to show a proposal; it does not start work."
    )
    effect_kind = EffectKind.PURE_READ
    input_schema: dict[str, JsonValue] = {
        "type": "object",
        "properties": {
            "requirement": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_ROUTER_SUMMARY_CHARS,
            },
            "mode": {
                "type": "string",
                "enum": [mode.value for mode in RequestedTaskMode],
            },
        },
        "required": ["requirement"],
        "additionalProperties": False,
    }

    __slots__ = ("_actions",)

    def __init__(self, actions: ProductTurnActions) -> None:
        self._actions = actions

    async def execute(
        self, arguments: dict[str, JsonValue], context: ToolExecutionContext
    ) -> ToolOutput:
        requirement = arguments.get("requirement")
        if type(requirement) is not str or not requirement.strip():
            raise ProductInputError("product-requirement-invalid", "requirement")
        requested_mode = None
        if "mode" in arguments:
            mode = arguments["mode"]
            if type(mode) is not str:
                raise ProductInputError("product-requested-mode-invalid", "mode")
            try:
                requested_mode = RequestedTaskMode(mode)
            except ValueError:
                raise ProductInputError(
                    "product-requested-mode-invalid", "mode"
                ) from None
        await self._actions.record(
            context.session_id,
            context.turn_id,
            _TurnAction("propose", requirement, requested_mode),
        )
        return ToolOutput(
            "The host will render the task proposal after this Turn closes."
        )


class ConfirmProductTaskTool:
    """Ask the host to offer its explicit start authorization boundary."""

    name = "confirm_product_task"
    description = (
        "Use only when the user appears to accept the currently displayed task "
        "proposal. This only asks the host for an explicit start authorization; "
        "the Tool Call cannot start a ProductTask."
    )
    effect_kind = EffectKind.PURE_READ
    input_schema: dict[str, JsonValue] = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }

    __slots__ = ("_actions",)

    def __init__(self, actions: ProductTurnActions) -> None:
        self._actions = actions

    async def execute(
        self, arguments: dict[str, JsonValue], context: ToolExecutionContext
    ) -> ToolOutput:
        if arguments:
            raise ProductInputError("product-confirmation-arguments-invalid", "arguments")
        await self._actions.record(
            context.session_id, context.turn_id, _TurnAction("confirm")
        )
        return ToolOutput(
            "The host will ask the user for an explicit start authorization."
        )


class ProductChatSurface:
    """One optional product surface attached to the existing chat loop."""

    __slots__ = (
        "_actions",
        "_approver_id",
        "_control",
        "_data_dir",
        "_evidence",
    )

    def __init__(
        self,
        control: ProductTaskControlPlane,
        actions: ProductTurnActions,
        evidence: ProductInspectionEvidenceReader,
        *,
        approver_id: str,
        data_dir: Path,
    ) -> None:
        if type(control) is not ProductTaskControlPlane:
            raise ProductInputError("product-control-invalid", "control")
        if type(approver_id) is not str or not approver_id.strip():
            raise ProductInputError("product-approver-invalid", "approver_id")
        if type(evidence) is not ProductInspectionEvidenceReader:
            raise ProductInputError("product-inspection-invalid", "evidence")
        self._control = control
        self._actions = actions
        self._evidence = evidence
        self._approver_id = approver_id
        self._data_dir = Path(data_dir).absolute()

    async def prepare_turn(self, session_id: str, text: str) -> ProductChatTurn:
        pending = await self._control.pending_proposal(session_id)
        return ProductChatTurn(
            TurnInput(content=text, message_id=str(uuid4()), source="user"),
            had_pending_proposal=pending is not None,
        )

    async def finish_turn(
        self,
        session_id: str,
        prepared: ProductChatTurn,
        *,
        turn_id: str,
        console: Console,
        heartbeat_seconds: float = 0.0,
        clock: Clock | None = None,
    ) -> None:
        action = await self._actions.take(session_id, turn_id)
        if action is None:
            return
        try:
            if action.kind == "propose":
                assert action.requirement is not None
                proposal = await self._control.offer(
                    session_id=session_id,
                    origin_turn_id=turn_id,
                    origin_message_id=prepared.turn_input.message_id,
                    proposed_turn_id=turn_id,
                    requirement=action.requirement,
                    requested_mode=action.requested_mode,
                )
                _render_proposal(console, proposal)
                return
            if not prepared.had_pending_proposal:
                console.write("task confirmation ignored: no proposal was pending")
                return
            pending = await self._control.pending_proposal(session_id)
            if pending is None:
                console.write("task confirmation ignored: no proposal was pending")
                return
            if not _explicit_start_authorized(console, pending):
                return
            _render_execution_started(console, pending)
            resolved_clock = clock or default_clock()
            heartbeat = (
                asyncio.create_task(
                    _emit_product_heartbeat(
                        console,
                        self._control,
                        pending.task_id,
                        interval_seconds=heartbeat_seconds,
                        clock=resolved_clock,
                    ),
                    name=f"traceh-product-heartbeat-{pending.task_id}",
                )
                if heartbeat_seconds > 0
                else None
            )
            try:
                result = await self._control.confirm(
                    session_id=session_id,
                    confirming_turn_id=turn_id,
                    confirming_message_id=prepared.turn_input.message_id,
                )
            finally:
                if heartbeat is not None:
                    heartbeat.cancel()
                    await await_worker_convergence(heartbeat)
            if (
                result.summary.status.value
                == ProductTaskViewStatus.AWAITING_APPROVAL.value
            ):
                await self._render_task_inspection(console, result.summary.task_id)
            else:
                _render_advance(console, result)
        except Exception as error:
            _render_operation_failure(console, error)

    async def discard_turn(self, session_id: str, turn_id: str | None) -> None:
        if turn_id is not None:
            await self._actions.discard(session_id, turn_id)
            return
        await self._actions.discard_session(session_id)

    async def handle_command(self, text: str, console: Console) -> bool:
        parts = text.split()
        if not parts or parts[0] != "/task":
            return False
        if len(parts) != 3 or parts[1] not in {
            "inspect",
            "approve",
            "reject",
            "cancel",
            "abandon",
        }:
            console.write(
                "usage: /task inspect|approve|reject|cancel|abandon TASK_ID"
            )
            return True
        operation, task_id = parts[1], parts[2]
        try:
            if operation == "inspect":
                await self._render_task_inspection(console, task_id)
            elif operation == "approve":
                _render_advance(
                    console,
                    await self._control.approve(
                        task_id, approver_id=self._approver_id
                    ),
                )
            elif operation == "reject":
                _render_advance(console, await self._control.reject(task_id))
            elif operation == "cancel":
                _render_advance(console, await self._control.cancel(task_id))
            else:
                _render_advance(console, await self._control.abandon(task_id))
        except Exception as error:
            _render_operation_failure(console, error)
        return True

    async def _render_task_inspection(
        self, console: Console, task_id: str
    ) -> None:
        inspection = await self._control.inspect(task_id)
        evidence = None
        evidence_error = None
        try:
            evidence = await self._evidence.load(
                inspection.view.summary, inspection.review
            )
        except Exception as error:
            code = getattr(error, "code", None)
            evidence_error = (
                code if type(code) is str and code else "product-evidence-unavailable"
            )
        _render_inspection(
            console,
            inspection,
            evidence=evidence,
            evidence_error=evidence_error,
            data_dir=self._data_dir,
        )

    async def aclose(self) -> None:
        await self._control.aclose()


def _render_proposal(console: Console, pending: PendingProductProposal) -> None:
    proposal = pending.proposal
    binding = proposal.preflight
    console.write("task proposal (not started):")
    console.write(f"  proposal: {proposal.proposal_id}")
    console.write(f"  task if confirmed: {pending.task_id}")
    console.write(f"  requirement: {escape_for_display(pending.requirement)}")
    console.write(f"  profile:  {pending.profile_id}")
    console.write(f"  mode:     {proposal.requested_mode.value}")
    console.write(f"  mode source: {proposal.mode_source.value}")
    console.write(f"  source:   {binding.base_revision}")
    console.write(
        f"  target:   {binding.promotion_target_ref} "
        f"at {binding.promotion_expected_revision}"
    )
    console.write("  safety: fixed Workflow, host verification, human approval")
    console.write(
        "Reply naturally in a later message. If acceptance is detected, the host "
        "will require exact START authorization before any task begins."
    )


def _explicit_start_authorized(
    console: Console, pending: PendingProductProposal
) -> bool:
    """Require one host-owned capability gesture for this exact pending task.

    Natural-language classification remains useful for choosing when to show
    the prompt, but it is never start authority.  A fixed control token avoids
    language-specific yes/no parsing and the prompt binds that token to the
    exact deterministic ProductTask identity currently pending in this Session.
    """

    try:
        answer = console.read_line(
            f"Start exact ProductTask {pending.task_id}? Type START to authorize: "
        )
    except EOFError:
        console.write(f"task {pending.task_id}: start not authorized (input ended)")
        return False
    if contains_undecodable_input(answer) or normalize_input(answer) != "START":
        console.write(f"task {pending.task_id}: start not authorized")
        return False
    console.write(f"task {pending.task_id}: explicit START authorized by host user")
    return True


def _render_inspection(
    console: Console,
    inspection: ProductInspection,
    *,
    evidence: ProductTaskEvidence | None,
    evidence_error: str | None,
    data_dir: Path,
) -> None:
    summary = inspection.view.summary
    console.write(f"task {summary.task_id}: {inspection.view.status.value}")
    console.write(f"  requested mode: {summary.requested_mode.value}")
    console.write(f"  mode source: {summary.mode_source.value}")
    if summary.resolved_mode is not None:
        console.write(f"  resolved mode: {summary.resolved_mode.value}")
    if inspection.view.workflow_status is not None:
        console.write(f"  workflow: {inspection.view.workflow_status.value}")
    if evidence_error is not None:
        console.write(f"  evidence: unavailable ({evidence_error})")
        console.write("  do not approve until the durable evidence can be read")
    elif evidence is not None:
        _render_evidence(console, evidence, data_dir=data_dir)
    if inspection.review is not None:
        _render_review(console, inspection)
        if evidence_error is None and evidence is not None:
            console.write(
                "  decision: /task approve TASK_ID or /task reject TASK_ID "
                "after reviewing the evidence above"
            )
        else:
            console.write(
                "  decision: do not approve; retry inspection or reject the task"
            )


def _render_review(console: Console, inspection: ProductInspection) -> None:
    review = inspection.review
    assert review is not None
    console.write(f"  review: {review.review_id}")
    console.write(f"  patch_sha256: {review.patch_sha256}")
    console.write(f"  target: {review.target_ref} at {review.expected_revision}")
    console.write(f"  integration_commit: {review.integration_commit}")
    console.write(f"  approval_digest: {inspection.approval_digest}")


def _render_evidence(
    console: Console, evidence: ProductTaskEvidence, *, data_dir: Path
) -> None:
    if evidence.nodes:
        console.write("  workflow nodes:")
    for node in evidence.nodes:
        line = f"    {node.node_id}: {node.status} ({node.kind})"
        if node.failure_code is not None:
            line += f" failure={node.failure_code}"
        console.write(line)
        if node.agent_id is not None:
            console.write(f"      agent: {node.agent_id}")
        if node.session_id is not None:
            console.write(f"      session: {node.session_id}")
            try:
                replay = render_command(
                    (
                        Literal("traceh"),
                        Literal("replay"),
                        node.session_id,
                        Literal("--data-dir"),
                        str(data_dir),
                    )
                )
            except UnsafeCommandValue:
                replay = "unavailable: a value cannot be rendered as one safe command"
            console.write(f"      replay: {replay}")
    review = evidence.review
    if review is None:
        return
    console.write(f"  changed paths ({len(review.changed_paths)})")
    for path in review.changed_paths:
        console.write(f"    {escape_for_display(path, limit=500)}")
    console.write("  verification:")
    for verifier in review.verifiers:
        exit_code = "unavailable" if verifier.exit_code is None else str(verifier.exit_code)
        console.write(
            f"    {verifier.command_id}: {verifier.status} exit={exit_code}"
        )
        executable = escape_for_display(verifier.executable, limit=500)
        console.write(
            f"      command: {executable} ({verifier.argument_count} arguments; "
            f"argv_sha256={verifier.argv_digest})"
        )
    suffix = "truncated" if review.patch_preview_truncated else "complete"
    console.write(f"  patch preview ({review.patch_size_bytes} bytes, {suffix})")
    for line in review.patch_preview.split("\n"):
        console.write(f"    {line}")
    if review.patch_utf8_replaced:
        console.write("    note: non-UTF-8 Patch bytes are shown with replacement characters")


def _render_execution_started(
    console: Console, pending: PendingProductProposal
) -> None:
    console.write(f"task {pending.task_id}: confirmation accepted; starting execution")
    console.write(f"  requested mode: {pending.proposal.requested_mode.value}")
    if pending.proposal.requested_mode is RequestedTaskMode.AUTO:
        console.write("  resolved mode: pending Router decision")


async def _emit_product_heartbeat(
    console: Console,
    control: ProductTaskControlPlane,
    task_id: str,
    *,
    interval_seconds: float,
    clock: Clock,
) -> None:
    started = clock.monotonic()
    while True:
        await clock.sleep(interval_seconds)
        try:
            inspection = await control.inspect(task_id)
        except Exception:
            continue
        summary = inspection.view.summary
        elapsed = max(0.0, clock.monotonic() - started)
        mode = (
            "pending"
            if summary.resolved_mode is None
            else summary.resolved_mode.value
        )
        workflow = (
            "not-started"
            if inspection.view.workflow_status is None
            else inspection.view.workflow_status.value
        )
        console.write(
            f"[waiting {_format_seconds(elapsed)}] task {task_id}: "
            f"{inspection.view.status.value}; workflow={workflow}; mode={mode}"
        )


def _format_seconds(value: float) -> str:
    return f"{int(value)}s" if value.is_integer() else f"{value:.1f}s"


def _render_advance(console: Console, result: ProductAdvanceResult) -> None:
    console.write(f"task {result.summary.task_id}: {result.summary.status.value}")
    if result.summary.failure_code is not None:
        console.write(f"  failure: {result.summary.failure_code}")
    if result.review is not None:
        _render_review(
            console,
            ProductInspection(
                view=_view_for_result(result),
                review=result.review,
                approval_digest=result.approval_digest,
            ),
        )
    if result.summary.status.value == ProductTaskViewStatus.COMPLETED.value:
        console.write(f"  promotion: {result.summary.promotion_id}")


def _render_operation_failure(console: Console, error: Exception) -> None:
    """Render one fixed failure code without exposing backend exception text."""

    code = getattr(error, "code", None)
    if type(code) is not str or not code:
        code = "product-execution-failed"
    task_id = getattr(error, "task_id", None)
    if type(task_id) is str and task_id:
        console.write(f"task {escape_for_display(task_id)} operation failed: {code}")
    else:
        console.write(f"task operation failed: {code}")


def _view_for_result(result: ProductAdvanceResult):
    from traceh.api.product import ProductTaskView

    status = None if result.workflow is None else result.workflow.status
    return ProductTaskView(
        summary=result.summary,
        workflow_status=status,
        owned_by_this_host=True,
    )


__all__ = [
    "ConfirmProductTaskTool",
    "ProductChatSurface",
    "ProductChatTurn",
    "ProductTurnActions",
    "ProposeProductTaskTool",
]
