"""Unified tool admission, scheduling, effects and result finalization."""

from __future__ import annotations

import asyncio
import traceback
from dataclasses import dataclass
from uuid import uuid4

from traceh.api.json_types import JsonValue, canonical_json, fingerprint, to_json_value
from traceh.api.llm import ToolCall
from traceh.api.tools import (
    PreparedToolCall,
    Tool,
    ToolAdmissionDecision,
    ToolAdmissionGate,
    ToolExecutionContext,
)
from traceh.concurrency import await_worker_convergence
from traceh.session.service import SessionService
from traceh.tools.middleware import ToolInvocation, ToolMiddleware, invoke_middleware_chain
from traceh.tools.policy import DecisionKind, ToolPolicy, evaluate_policies
from traceh.tools.registry import ToolRegistry
from traceh.tools.results import ToolRunResult
from traceh.tools.schema import ToolArgumentError, validate_arguments


class ToolReportedTimeout(RuntimeError):
    """A tool decided its own operation timed out.

    Distinct from the runtime's budget expiring: the tool ran to a conclusion it
    controls and produced evidence about it, so its own message and duration
    must be reported rather than replaced by the runtime's generic wording.
    Raised at a nested boundary so the two cases never have to be told apart by
    inspecting error text.
    """

    def __init__(self, error: BaseException) -> None:
        super().__init__(str(error))
        self.error = error


class ToolAdmissionProtocolError(RuntimeError):
    code = "tool-admission-protocol-invalid"


@dataclass(frozen=True, slots=True)
class _PreparedInvocation:
    call: ToolCall
    tool: Tool
    context: ToolExecutionContext
    policy: str | None


@dataclass(frozen=True, slots=True)
class ToolBatchContext:
    session_id: str
    turn_id: str
    step_id: str
    workspace: object
    data_dir: object
    composition_revision: str


class ToolRuntime:
    def __init__(
        self,
        registry: ToolRegistry,
        sessions: SessionService,
        *,
        policies: tuple[ToolPolicy, ...],
        middlewares: tuple[ToolMiddleware, ...] = (),
        timeout_seconds: float = 60.0,
        max_output_chars: int = 24_000,
        admission_gate: ToolAdmissionGate | None = None,
    ) -> None:
        self.registry = registry
        self.sessions = sessions
        self.policies = policies
        self.middlewares = middlewares
        component_bindings = [
            getattr(registry, "_composition_resource_binding", None),
            *(getattr(policy, "_composition_resource_binding", None) for policy in policies),
            *(
                getattr(middleware, "_composition_resource_binding", None)
                for middleware in middlewares
            ),
        ]
        component_bindings = [binding for binding in component_bindings if binding is not None]
        if component_bindings and any(
            binding is not component_bindings[0] for binding in component_bindings[1:]
        ):
            raise ValueError("tool runtime mixes composition resource lineages")
        self._composition_resource_binding = (
            component_bindings[0] if component_bindings else None
        )
        self.timeout_seconds = timeout_seconds
        self.max_output_chars = max_output_chars
        self.admission_gate = admission_gate

    async def execute_batch(
        self,
        calls: tuple[ToolCall, ...],
        *,
        context: ToolExecutionContext,
        composition_revision: str,
    ) -> tuple[ToolRunResult, ...]:
        if not calls:
            return ()
        call_ids = [call.id for call in calls]
        if any(not call_id for call_id in call_ids):
            raise ValueError("tool call IDs must be non-empty")
        if len(set(call_ids)) != len(call_ids):
            raise ValueError("tool call IDs must be unique within one model response")

        for call in calls:
            await self.sessions.append_session(
                context.session_id,
                "tool/call",
                {
                    "turn_id": context.turn_id,
                    "step_id": context.step_id,
                    "tool_call_id": call.id,
                    "tool_name": call.name,
                    "arguments": call.arguments,
                },
                composition_revision=composition_revision,
            )

        results: list[ToolRunResult] = []
        prepared: list[_PreparedInvocation | ToolRunResult] = []
        try:
            gate_candidates: list[PreparedToolCall] = []
            candidate_positions: list[int] = []
            for call in calls:
                item = await self._prepare_one(call, context=context)
                prepared.append(item)
                if isinstance(item, _PreparedInvocation):
                    candidate_positions.append(len(prepared) - 1)
                    detached_arguments = to_json_value(call.arguments)
                    if not isinstance(detached_arguments, dict):
                        raise ToolAdmissionProtocolError(
                            "prepared Tool arguments are not an object"
                        )
                    gate_candidates.append(
                        PreparedToolCall(
                            tool_call_id=call.id,
                            tool_name=call.name,
                            arguments=detached_arguments,
                            effect_kind=item.tool.effect_kind,
                            policy=item.policy,
                        )
                    )

            cancellation: asyncio.CancelledError | None = None
            if not gate_candidates or self.admission_gate is None:
                decisions = tuple(
                    ToolAdmissionDecision(call.tool_call_id, True)
                    for call in gate_candidates
                )
            else:
                admission_task = asyncio.create_task(
                    self.admission_gate.admit(tuple(gate_candidates), context),
                    name="traceh-tool-admission",
                )
                decisions, cancellation = await self._owned_task_result(
                    admission_task
                )
            self._validate_admission_decisions(decisions, gate_candidates)

            for position, decision in zip(
                candidate_positions, decisions, strict=True
            ):
                item = prepared[position]
                assert isinstance(item, _PreparedInvocation)
                if not decision.admitted:
                    prepared[position] = ToolRunResult(
                        item.call.id,
                        item.call.name,
                        "denied",
                        "Tool admission was denied by the host budget.",
                        data={"admission": decision.code},
                        error_type="ToolAdmissionDenied",
                    )
            admission_events = asyncio.create_task(
                self._append_admissions(
                    prepared,
                    context=context,
                    composition_revision=composition_revision,
                ),
                name="traceh-tool-admission-events",
            )
            _, event_cancellation = await self._owned_task_result(admission_events)
            cancellation = cancellation or event_cancellation
            if cancellation is not None:
                raise cancellation

            index = 0
            while index < len(calls):
                item = prepared[index]
                if isinstance(item, ToolRunResult):
                    await self._append_result(context, item, composition_revision)
                    results.append(item)
                    index += 1
                    continue
                parallel_safe = item.tool.effect_kind.is_parallel_safe
                if not parallel_safe:
                    result = await self._dispatch_one(item)
                    await self._append_result(context, result, composition_revision)
                    results.append(result)
                    index += 1
                    continue

                group: list[_PreparedInvocation] = []
                while index < len(prepared):
                    candidate = prepared[index]
                    if (
                        not isinstance(candidate, _PreparedInvocation)
                        or not candidate.tool.effect_kind.is_parallel_safe
                    ):
                        break
                    group.append(candidate)
                    index += 1

                group_results = await asyncio.gather(
                    *(self._dispatch_one(item) for item in group)
                )
                for result in group_results:
                    await self._append_result(context, result, composition_revision)
                    results.append(result)
        except asyncio.CancelledError as cancelled:
            finalizer = asyncio.create_task(
                self._finalize_cancelled_batch(
                    calls,
                    prepared=prepared,
                    completed=tuple(results),
                    context=context,
                    composition_revision=composition_revision,
                ),
                name="traceh-tool-cancel-finalize",
            )
            try:
                await asyncio.shield(finalizer)
            except asyncio.CancelledError:
                await await_worker_convergence(finalizer)
            if not finalizer.cancelled():
                failure = finalizer.exception()
                if failure is not None:
                    raise cancelled from failure
            raise cancelled

        return tuple(results)

    @staticmethod
    async def _owned_task_result(task: asyncio.Task):
        cancellation: asyncio.CancelledError | None = None
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancellation = error
            await await_worker_convergence(task)
        if task.cancelled():
            raise cancellation or asyncio.CancelledError()
        failure = task.exception()
        if failure is not None:
            if cancellation is not None:
                raise cancellation from failure
            raise failure
        return task.result(), cancellation

    @staticmethod
    def _validate_admission_decisions(
        decisions: object,
        candidates: list[PreparedToolCall],
    ) -> None:
        if type(decisions) is not tuple or len(decisions) != len(candidates):
            raise ToolAdmissionProtocolError(
                "admission decisions do not match prepared calls"
            )
        for decision, candidate in zip(decisions, candidates, strict=True):
            if (
                type(decision) is not ToolAdmissionDecision
                or decision.tool_call_id != candidate.tool_call_id
                or type(decision.admitted) is not bool
                or (decision.admitted and decision.code is not None)
                or (
                    not decision.admitted
                    and (not isinstance(decision.code, str) or not decision.code)
                )
            ):
                raise ToolAdmissionProtocolError(
                    "admission decisions do not match prepared calls"
                )

    async def _append_admissions(
        self,
        prepared: list[_PreparedInvocation | ToolRunResult],
        *,
        context: ToolExecutionContext,
        composition_revision: str,
    ) -> None:
        for item in prepared:
            if not isinstance(item, _PreparedInvocation):
                continue
            await self.sessions.append_session(
                context.session_id,
                "tool/admitted",
                {
                    "turn_id": context.turn_id,
                    "step_id": context.step_id,
                    "tool_call_id": item.call.id,
                    "tool_name": item.call.name,
                    "policy": item.policy,
                },
                composition_revision=composition_revision,
            )

    async def _finalize_cancelled_batch(
        self,
        calls: tuple[ToolCall, ...],
        *,
        prepared: list[_PreparedInvocation | ToolRunResult],
        completed: tuple[ToolRunResult, ...],
        context: ToolExecutionContext,
        composition_revision: str,
    ) -> None:
        completed_ids = {result.tool_call_id for result in completed}
        prepared_results = {
            result.tool_call_id: result
            for result in prepared
            if isinstance(result, ToolRunResult)
        }
        effect_events = await self.sessions.read_effects(context.session_id)
        outcomes = {
            str(event.data.get("tool_call_id")): event
            for event in effect_events
            if event.type in {"effect/outcome", "effect/reconciled"}
        }
        for call in calls:
            if call.id in completed_ids:
                continue
            outcome = outcomes.get(call.id)
            if outcome is None:
                recovered = prepared_results.get(call.id) or ToolRunResult(
                    tool_call_id=call.id,
                    tool_name=call.name,
                    status="aborted_before_dispatch",
                    content=(
                        "Tool execution was cancelled before a durable outcome "
                        "was available."
                    ),
                    error_type="CancelledError",
                )
            else:
                raw_data = outcome.data.get("data", {})
                data = raw_data if isinstance(raw_data, dict) else {}
                status = str(outcome.data.get("status", "cancelled"))
                recovered = ToolRunResult(
                    tool_call_id=call.id,
                    tool_name=call.name,
                    status=status,
                    content=str(
                        outcome.data.get("content")
                        or outcome.data.get("message")
                        or "Tool execution was cancelled."
                    ),
                    data=data,
                    effect_id=str(outcome.data.get("effect_id")),
                    error_type=(
                        str(outcome.data.get("error_type"))
                        if outcome.data.get("error_type")
                        else None
                    ),
                )
            await self._append_result(context, recovered, composition_revision)

    async def _append_result(
        self,
        context: ToolExecutionContext,
        result: ToolRunResult,
        composition_revision: str,
    ) -> None:
        await self.sessions.append_session(
            context.session_id,
            "tool/result",
            result.to_event_data(step_id=context.step_id),
            composition_revision=composition_revision,
        )

    async def _prepare_one(
        self,
        call: ToolCall,
        *,
        context: ToolExecutionContext,
    ) -> _PreparedInvocation | ToolRunResult:
        tool = self.registry.get(call.name)
        if tool is None:
            return ToolRunResult(
                call.id,
                call.name,
                "invalid",
                f"Unknown tool: {call.name}",
                error_type="UnknownTool",
            )

        try:
            validate_arguments(call.arguments, tool.input_schema)
        except ToolArgumentError as error:
            return ToolRunResult(
                call.id,
                call.name,
                "invalid",
                str(error),
                error_type="ToolArgumentError",
            )

        call_context = ToolExecutionContext(
            session_id=context.session_id,
            turn_id=context.turn_id,
            step_id=context.step_id,
            tool_call_id=call.id,
            workspace=context.workspace,
            data_dir=context.data_dir,
        )
        decision = await evaluate_policies(self.policies, call, tool, call_context)
        if decision.kind is DecisionKind.DENY:
            return ToolRunResult(
                call.id,
                call.name,
                "denied",
                decision.reason,
                data={"policy": decision.policy},
                error_type="ToolDenied",
            )

        return _PreparedInvocation(
            call=call,
            tool=tool,
            context=call_context,
            policy=decision.policy,
        )

    async def _dispatch_one(
        self,
        prepared: _PreparedInvocation,
    ) -> ToolRunResult:
        call = prepared.call
        tool = prepared.tool
        call_context = prepared.context

        effect_id = str(uuid4())
        intent_data: dict[str, JsonValue] = {
            "effect_id": effect_id,
            "session_id": call_context.session_id,
            "turn_id": call_context.turn_id,
            "step_id": call_context.step_id,
            "tool_call_id": call.id,
            "tool_name": call.name,
            "effect_kind": tool.effect_kind.value,
            "operation_fingerprint": fingerprint(
                {
                    "tool": call.name,
                    "arguments": call.arguments,
                    "workspace": str(call_context.workspace),
                }
            ),
            "arguments": call.arguments,
            "retry_safe": tool.effect_kind.is_retry_safe,
        }
        intent = await self.sessions.append_effect(
            call_context.session_id,
            "effect/intent",
            intent_data,
        )
        await self.sessions.append_effect(
            call_context.session_id,
            "effect/dispatched",
            {
                "effect_id": effect_id,
                "tool_call_id": call.id,
                "tool_name": call.name,
            },
            causation_id=intent.event_id,
        )

        try:
            async with asyncio.timeout(self.timeout_seconds):
                try:
                    output = await invoke_middleware_chain(
                        self.middlewares,
                        ToolInvocation(call=call, tool=tool, context=call_context),
                    )
                except TimeoutError as error:
                    # The tool timed itself out and already knows what happened,
                    # including whatever the command printed first. Re-labelled
                    # here so the runtime's own budget handler cannot swallow it.
                    raise ToolReportedTimeout(error) from error
            content = output.content
            truncated = False
            if len(content) > self.max_output_chars:
                content = content[: self.max_output_chars] + "\n...[truncated by TraceHarness]"
                truncated = True
            outcome_data: dict[str, JsonValue] = {
                "effect_id": effect_id,
                "tool_call_id": call.id,
                "tool_name": call.name,
                "status": "succeeded",
                "content": content,
                "data": output.data,
                "evidence": list(output.evidence),
                "truncated": truncated,
            }
            await self.sessions.append_effect(
                call_context.session_id,
                "effect/outcome",
                outcome_data,
                causation_id=intent.event_id,
            )
            return ToolRunResult(
                call.id,
                call.name,
                "succeeded",
                content,
                data=output.data,
                effect_id=effect_id,
            )
        except ToolReportedTimeout as reported:
            # Keep the tool's own account of its timeout: it carries the real
            # duration and whatever the command managed to print.
            message = str(reported)
            truncated = False
            if len(message) > self.max_output_chars:
                message = message[: self.max_output_chars] + "\n...[truncated by TraceHarness]"
                truncated = True
            await self.sessions.append_effect(
                call_context.session_id,
                "effect/outcome",
                {
                    "effect_id": effect_id,
                    "tool_call_id": call.id,
                    "tool_name": call.name,
                    "status": "failed",
                    "error_type": type(reported.error).__name__,
                    "message": message,
                    "truncated": truncated,
                    "reported_by": "tool",
                },
                causation_id=intent.event_id,
            )
            return ToolRunResult(
                call.id,
                call.name,
                "failed",
                message,
                effect_id=effect_id,
                error_type=type(reported.error).__name__,
            )
        except TimeoutError:
            message = f"Tool timed out after {self.timeout_seconds:.1f}s"
            await self.sessions.append_effect(
                call_context.session_id,
                "effect/outcome",
                {
                    "effect_id": effect_id,
                    "tool_call_id": call.id,
                    "tool_name": call.name,
                    "status": "failed",
                    "error_type": "TimeoutError",
                    "message": message,
                    "reported_by": "runtime",
                },
                causation_id=intent.event_id,
            )
            return ToolRunResult(
                call.id,
                call.name,
                "failed",
                message,
                effect_id=effect_id,
                error_type="TimeoutError",
            )
        except asyncio.CancelledError as cancelled:
            async def finalize_cancel() -> None:
                await self.sessions.append_effect(
                    call_context.session_id,
                    "effect/outcome",
                    {
                        "effect_id": effect_id,
                        "tool_call_id": call.id,
                        "tool_name": call.name,
                        "status": "cancelled",
                        "error_type": "CancelledError",
                        "message": "Tool execution was cancelled",
                    },
                    causation_id=intent.event_id,
                )

            finalizer = asyncio.create_task(
                finalize_cancel(),
                name="traceh-tool-effect-cancel-finalize",
            )
            try:
                await asyncio.shield(finalizer)
            except asyncio.CancelledError:
                await await_worker_convergence(finalizer)
            if not finalizer.cancelled():
                failure = finalizer.exception()
                if failure is not None:
                    raise cancelled from failure
            raise cancelled
        except Exception as error:
            message = f"{type(error).__name__}: {error}"
            await self.sessions.append_effect(
                call_context.session_id,
                "effect/outcome",
                {
                    "effect_id": effect_id,
                    "tool_call_id": call.id,
                    "tool_name": call.name,
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "message": message,
                    "traceback": "".join(traceback.format_exception(error))[-8000:],
                    "arguments_json": canonical_json(call.arguments),
                },
                causation_id=intent.event_id,
            )
            return ToolRunResult(
                call.id,
                call.name,
                "failed",
                message,
                effect_id=effect_id,
                error_type=type(error).__name__,
            )
