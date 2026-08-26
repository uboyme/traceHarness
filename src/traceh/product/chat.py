"""Host rendering and low-authority model actions for unified chat.

The two model Tools only leave a process-local note about the current Turn.
They cannot open a ProductTask, choose a mode, inspect a Review, approve or
promote.  After the Turn is durably closed, the chat host combines that note
with the user message identity it supplied and invokes the host control plane.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import uuid4

from traceh.api.json_types import JsonValue
from traceh.api.product import ProductTaskViewStatus, RequestedTaskMode
from traceh.api.tools import EffectKind, ToolExecutionContext, ToolOutput
from traceh.api.turns import TurnInput
from traceh.cli.command_line import escape_for_display
from traceh.cli.console import Console
from traceh.product.control import (
    PendingProductProposal,
    ProductAdvanceResult,
    ProductInspection,
    ProductTaskControlPlane,
)
from traceh.product.errors import ProductInputError
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
    """Recognize the user's current message as confirmation of the shown offer."""

    name = "confirm_product_task"
    description = (
        "Use only when the user clearly accepts the currently displayed task "
        "proposal. The host independently verifies the later user Turn."
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
            "The host will verify this user Turn before starting any task."
        )


class ProductChatSurface:
    """One optional product surface attached to the existing chat loop."""

    __slots__ = ("_actions", "_approver_id", "_control")

    def __init__(
        self,
        control: ProductTaskControlPlane,
        actions: ProductTurnActions,
        *,
        approver_id: str,
    ) -> None:
        if type(control) is not ProductTaskControlPlane:
            raise ProductInputError("product-control-invalid", "control")
        if type(approver_id) is not str or not approver_id.strip():
            raise ProductInputError("product-approver-invalid", "approver_id")
        self._control = control
        self._actions = actions
        self._approver_id = approver_id

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
            result = await self._control.confirm(
                session_id=session_id,
                confirming_turn_id=turn_id,
                confirming_message_id=prepared.turn_input.message_id,
            )
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
                _render_inspection(console, await self._control.inspect(task_id))
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
    console.write("Reply naturally in a later message to accept or decline this proposal.")


def _render_inspection(console: Console, inspection: ProductInspection) -> None:
    summary = inspection.view.summary
    console.write(f"task {summary.task_id}: {inspection.view.status.value}")
    if summary.resolved_mode is not None:
        console.write(f"  mode: {summary.resolved_mode.value}")
    if inspection.review is not None:
        _render_review(console, inspection)


def _render_review(console: Console, inspection: ProductInspection) -> None:
    review = inspection.review
    assert review is not None
    console.write(f"  review: {review.review_id}")
    console.write(f"  patch_sha256: {review.patch_sha256}")
    console.write(f"  target: {review.target_ref} at {review.expected_revision}")
    console.write(f"  integration_commit: {review.integration_commit}")
    console.write(f"  approval_digest: {inspection.approval_digest}")


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
