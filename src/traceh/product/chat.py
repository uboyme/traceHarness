"""UI-neutral Product Chat coordination and low-authority model Tools.

The proposal and confirmation Tools only leave a process-local note about the
current Turn.  The evidence Tool performs a fresh, Session-scoped read.  None
can open, start, approve, reject, cancel or promote a ProductTask.  Terminal
rendering lives in :mod:`traceh.cli.product`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from uuid import uuid4

from traceh.api.json_types import JsonValue, canonical_json
from traceh.api.product import RequestedTaskMode
from traceh.api.tools import EffectKind, ToolExecutionContext, ToolOutput
from traceh.api.turns import TurnInput
from traceh.product.context import ProductModelContext
from traceh.product.control import (
    PendingProductProposal,
    ProductAdvanceResult,
    ProductInspection,
    ProductTaskControlPlane,
)
from traceh.product.errors import ProductInputError
from traceh.product.events import require_product_identifier
from traceh.product.inspection import (
    ProductInspectionEvidenceReader,
    ProductTaskEvidence,
)
from traceh.product.memory import ProductTaskMemoryReader, product_task_evidence_data
from traceh.product.router import MAX_ROUTER_SUMMARY_CHARS


@dataclass(frozen=True, slots=True)
class ProductChatTurn:
    turn_input: TurnInput
    had_pending_proposal: bool


class ProductCommandOperation(StrEnum):
    INSPECT = "inspect"
    APPROVE = "approve"
    REJECT = "reject"
    CANCEL = "cancel"
    ABANDON = "abandon"


@dataclass(frozen=True, slots=True)
class ProductCommand:
    operation: ProductCommandOperation
    task_id: str


@dataclass(frozen=True, slots=True)
class ProductStartRequest:
    pending: PendingProductProposal
    session_id: str
    confirming_turn_id: str
    confirming_message_id: str


@dataclass(frozen=True, slots=True)
class ProductTurnResolution:
    proposal: PendingProductProposal | None = None
    start_request: ProductStartRequest | None = None
    notice_code: str | None = None


@dataclass(frozen=True, slots=True)
class ProductInspectionResult:
    inspection: ProductInspection
    evidence: ProductTaskEvidence | None
    evidence_error: str | None


@dataclass(frozen=True, slots=True)
class ProductCommandResult:
    command: ProductCommand
    advance: ProductAdvanceResult | None = None
    inspection: ProductInspectionResult | None = None


def parse_product_command(text: str) -> ProductCommand | None:
    """Parse ``/task`` without reading, writing or invoking a Product owner."""

    parts = text.split()
    if not parts or parts[0] != "/task":
        return None
    if len(parts) != 3:
        raise ProductInputError("product-command-invalid", "command")
    try:
        operation = ProductCommandOperation(parts[1])
    except ValueError:
        raise ProductInputError("product-command-invalid", "command") from None
    from traceh.product.events import require_product_identifier

    task_id = require_product_identifier(parts[2], field="task_id")
    return ProductCommand(operation, task_id)


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


class ReadProductTaskEvidenceTool:
    """Read bounded durable evidence for one task related to this Session."""

    name = "read_product_task_evidence"
    description = (
        "Read verified execution evidence for an exact ProductTask id already "
        "shown in the host ProductTask context. Use this when the user asks "
        "which files changed, which managed tools ran, how verification ended, "
        "or what was promoted. It is read-only and grants no control authority."
    )
    effect_kind = EffectKind.PURE_READ
    input_schema: dict[str, JsonValue] = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 256,
            }
        },
        "required": ["task_id"],
        "additionalProperties": False,
    }

    __slots__ = ("_memory",)

    def __init__(self, memory: ProductTaskMemoryReader) -> None:
        if type(memory) is not ProductTaskMemoryReader:
            raise ProductInputError("product-memory-reader-invalid", "memory")
        self._memory = memory

    async def execute(
        self, arguments: dict[str, JsonValue], context: ToolExecutionContext
    ) -> ToolOutput:
        if set(arguments) != {"task_id"}:
            raise ProductInputError("product-evidence-arguments-invalid", "arguments")
        task_id = require_product_identifier(arguments.get("task_id"), field="task_id")
        try:
            memory = await self._memory.load(context.session_id, task_id)
            data = product_task_evidence_data(memory)
        except Exception:
            # Missing, foreign and unreadable tasks deliberately have one public
            # result.  A requester must not use this read Tool as an id oracle.
            unavailable: dict[str, JsonValue] = {
                "available": False,
                "code": "product-task-evidence-unavailable",
            }
            return ToolOutput(
                "ProductTask evidence is unavailable for this requester Session.",
                data=unavailable,
            )
        return ToolOutput(
            "Verified ProductTask evidence from the durable log:\n"
            + canonical_json(data),
            data={"available": True, "task_id": task_id},
        )


class ProductChatSurface:
    """Typed Product coordination shared by Line and future TUI adapters."""

    __slots__ = (
        "_actions",
        "_approver_id",
        "_control",
        "_evidence",
        "_model_context",
    )

    def __init__(
        self,
        control: ProductTaskControlPlane,
        actions: ProductTurnActions,
        evidence: ProductInspectionEvidenceReader,
        model_context: ProductModelContext,
        *,
        approver_id: str,
    ) -> None:
        if type(control) is not ProductTaskControlPlane:
            raise ProductInputError("product-control-invalid", "control")
        if type(approver_id) is not str or not approver_id.strip():
            raise ProductInputError("product-approver-invalid", "approver_id")
        if type(evidence) is not ProductInspectionEvidenceReader:
            raise ProductInputError("product-inspection-invalid", "evidence")
        if type(model_context) is not ProductModelContext:
            raise ProductInputError("product-context-invalid", "model_context")
        self._control = control
        self._actions = actions
        self._evidence = evidence
        self._model_context = model_context
        self._approver_id = approver_id

    async def prepare_turn(self, session_id: str, text: str) -> ProductChatTurn:
        # Freeze the latest canonical ProductTask head before AgentLoop records
        # the new Turn. RequestBuilder and recovery can then reconstruct the
        # exact model Surface without a request-time cross-stream join.
        await self._model_context.synchronize(session_id)
        pending = await self._control.pending_proposal(session_id)
        return ProductChatTurn(
            TurnInput(content=text, message_id=str(uuid4()), source="user"),
            had_pending_proposal=pending is not None,
        )

    async def resolve_turn(
        self,
        session_id: str,
        prepared: ProductChatTurn,
        *,
        turn_id: str,
    ) -> ProductTurnResolution:
        """Convert the model's ephemeral action into a typed host decision."""

        action = await self._actions.take(session_id, turn_id)
        if action is None:
            return ProductTurnResolution()
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
            return ProductTurnResolution(proposal=proposal)
        if not prepared.had_pending_proposal:
            return ProductTurnResolution(notice_code="product-proposal-missing")
        pending = await self._control.pending_proposal(session_id)
        if pending is None:
            return ProductTurnResolution(notice_code="product-proposal-missing")
        return ProductTurnResolution(
            start_request=ProductStartRequest(
                pending=pending,
                session_id=session_id,
                confirming_turn_id=turn_id,
                confirming_message_id=prepared.turn_input.message_id,
            )
        )

    async def start(self, request: ProductStartRequest) -> ProductAdvanceResult:
        """Invoke the existing control owner for one exact typed authorization."""

        if type(request) is not ProductStartRequest:
            raise ProductInputError("product-start-request-invalid", "request")
        pending = await self._control.pending_proposal(request.session_id)
        if pending is None or pending.task_id != request.pending.task_id:
            raise ProductInputError("product-start-request-stale", "request")
        return await self._control.confirm(
            session_id=request.session_id,
            confirming_turn_id=request.confirming_turn_id,
            confirming_message_id=request.confirming_message_id,
        )

    async def discard_turn(self, session_id: str, turn_id: str | None) -> None:
        if turn_id is not None:
            await self._actions.discard(session_id, turn_id)
            return
        await self._actions.discard_session(session_id)

    async def execute_command(self, command: ProductCommand) -> ProductCommandResult:
        """Route one parsed command to the pre-existing control-plane owner."""

        if type(command) is not ProductCommand:
            raise ProductInputError("product-command-invalid", "command")
        operation, task_id = command.operation, command.task_id
        if operation is ProductCommandOperation.INSPECT:
            return ProductCommandResult(
                command,
                inspection=await self.inspect(task_id),
            )
        if operation is ProductCommandOperation.APPROVE:
            advance = await self._control.approve(
                task_id, approver_id=self._approver_id
            )
        elif operation is ProductCommandOperation.REJECT:
            advance = await self._control.reject(task_id)
        elif operation is ProductCommandOperation.CANCEL:
            advance = await self._control.cancel(task_id)
        else:
            advance = await self._control.abandon(task_id)
        return ProductCommandResult(command, advance=advance)

    async def inspect(self, task_id: str) -> ProductInspectionResult:
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
        return ProductInspectionResult(inspection, evidence, evidence_error)

    async def aclose(self) -> None:
        await self._control.aclose()


__all__ = [
    "ConfirmProductTaskTool",
    "ProductChatSurface",
    "ProductChatTurn",
    "ProductCommand",
    "ProductCommandResult",
    "ProductCommandOperation",
    "ProductInspectionResult",
    "ProductStartRequest",
    "ProductTurnResolution",
    "ProductTurnActions",
    "ProposeProductTaskTool",
    "ReadProductTaskEvidenceTool",
    "parse_product_command",
]
