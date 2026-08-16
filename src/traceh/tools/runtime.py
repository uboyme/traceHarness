"""Unified tool admission, scheduling, effects and result finalization."""

from __future__ import annotations

import asyncio
import traceback
from dataclasses import dataclass
from uuid import uuid4

from traceh.api.json_types import JsonValue, canonical_json, fingerprint
from traceh.api.llm import ToolCall
from traceh.api.tools import EffectKind, ToolExecutionContext
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
    ) -> None:
        self.registry = registry
        self.sessions = sessions
        self.policies = policies
        self.middlewares = middlewares
        self.timeout_seconds = timeout_seconds
        self.max_output_chars = max_output_chars

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
        index = 0
        try:
            while index < len(calls):
                call = calls[index]
                tool = self.registry.get(call.name)
                parallel_safe = bool(tool and tool.effect_kind.is_parallel_safe)
                if not parallel_safe:
                    result = await self._execute_one(
                        call,
                        context=context,
                        composition_revision=composition_revision,
                    )
                    await self._append_result(context, result, composition_revision)
                    results.append(result)
                    index += 1
                    continue

                group: list[ToolCall] = []
                while index < len(calls):
                    candidate = calls[index]
                    candidate_tool = self.registry.get(candidate.name)
                    if not candidate_tool or not candidate_tool.effect_kind.is_parallel_safe:
                        break
                    group.append(candidate)
                    index += 1

                group_results = await asyncio.gather(
                    *(
                        self._execute_one(
                            item,
                            context=context,
                            composition_revision=composition_revision,
                        )
                        for item in group
                    )
                )
                for result in group_results:
                    await self._append_result(context, result, composition_revision)
                    results.append(result)
        except asyncio.CancelledError:
            completed = {result.tool_call_id for result in results}
            effect_events = await asyncio.shield(
                self.sessions.read_effects(context.session_id)
            )
            outcomes = {
                str(event.data.get("tool_call_id")): event
                for event in effect_events
                if event.type in {"effect/outcome", "effect/reconciled"}
            }
            for call in calls:
                if call.id in completed:
                    continue
                outcome = outcomes.get(call.id)
                if outcome is None:
                    recovered = ToolRunResult(
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
                await asyncio.shield(
                    self._append_result(context, recovered, composition_revision)
                )
            raise

        return tuple(results)

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

    async def _execute_one(
        self,
        call: ToolCall,
        *,
        context: ToolExecutionContext,
        composition_revision: str,
    ) -> ToolRunResult:
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

        await self.sessions.append_session(
            context.session_id,
            "tool/admitted",
            {
                "turn_id": context.turn_id,
                "step_id": context.step_id,
                "tool_call_id": call.id,
                "tool_name": call.name,
                "policy": decision.policy,
            },
            composition_revision=composition_revision,
        )

        effect_id = str(uuid4())
        intent_data: dict[str, JsonValue] = {
            "effect_id": effect_id,
            "session_id": context.session_id,
            "turn_id": context.turn_id,
            "step_id": context.step_id,
            "tool_call_id": call.id,
            "tool_name": call.name,
            "effect_kind": tool.effect_kind.value,
            "operation_fingerprint": fingerprint(
                {"tool": call.name, "arguments": call.arguments, "workspace": str(context.workspace)}
            ),
            "arguments": call.arguments,
            "retry_safe": tool.effect_kind.is_retry_safe,
        }
        intent = await self.sessions.append_effect(
            context.session_id,
            "effect/intent",
            intent_data,
        )
        await self.sessions.append_effect(
            context.session_id,
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
                context.session_id,
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
                context.session_id,
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
                context.session_id,
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
        except asyncio.CancelledError:
            async def finalize_cancel() -> None:
                await self.sessions.append_effect(
                    context.session_id,
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

            await asyncio.shield(finalize_cancel())
            raise
        except Exception as error:
            message = f"{type(error).__name__}: {error}"
            await self.sessions.append_effect(
                context.session_id,
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
