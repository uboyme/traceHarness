from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from traceh.api.llm import ToolCall
from traceh.api.tools import EffectKind, ToolExecutionContext, ToolOutput
from traceh.session.event_store import InMemoryEventStore
from traceh.session.service import SessionService
from traceh.tools.policy import AllowByDefaultPolicy, DangerousShellPolicy
from traceh.tools.registry import ToolRegistry
from traceh.tools.runtime import ToolRuntime


@dataclass(slots=True)
class SlowReadTool:
    name: str = "slow_read"
    description: str = "slow"
    input_schema: dict = field(init=False, repr=False)
    effect_kind: EffectKind = EffectKind.PURE_READ

    def __post_init__(self) -> None:
        self.input_schema = {"type": "object", "properties": {}, "additionalProperties": False}

    async def execute(self, arguments, context) -> ToolOutput:
        del arguments, context
        await asyncio.sleep(1)
        return ToolOutput("late")


@pytest.mark.asyncio
async def test_unknown_and_denied_tools_receive_results_without_effects(tmp_path) -> None:
    sessions = SessionService(InMemoryEventStore())
    session_id = await sessions.create_session(tmp_path)
    await sessions.append_session(session_id, "turn/start", {"turn_id": "t"})
    await sessions.append_session(
        session_id,
        "step/start",
        {"turn_id": "t", "step_id": "s"},
    )
    registry = ToolRegistry()

    from traceh.tools.builtins.shell import ShellTool

    registry.register(ShellTool())
    runtime = ToolRuntime(
        registry,
        sessions,
        policies=(DangerousShellPolicy(), AllowByDefaultPolicy()),
    )
    context = ToolExecutionContext(session_id, "t", "s", "batch", tmp_path, tmp_path)
    results = await runtime.execute_batch(
        (
            ToolCall("unknown", "missing", {}),
            ToolCall("denied", "shell", {"command": "rm -rf ."}),
        ),
        context=context,
        composition_revision="r",
    )
    assert [result.status for result in results] == ["invalid", "denied"]
    assert await sessions.read_effects(session_id) == ()


@pytest.mark.asyncio
async def test_tool_timeout_is_durable(tmp_path) -> None:
    sessions = SessionService(InMemoryEventStore())
    session_id = await sessions.create_session(tmp_path)
    await sessions.append_session(session_id, "turn/start", {"turn_id": "t"})
    await sessions.append_session(
        session_id,
        "step/start",
        {"turn_id": "t", "step_id": "s"},
    )
    registry = ToolRegistry()
    registry.register(SlowReadTool())
    runtime = ToolRuntime(
        registry,
        sessions,
        policies=(AllowByDefaultPolicy(),),
        timeout_seconds=0.01,
    )
    context = ToolExecutionContext(session_id, "t", "s", "batch", tmp_path, tmp_path)
    results = await runtime.execute_batch(
        (ToolCall("slow", "slow_read", {}),),
        context=context,
        composition_revision="r",
    )
    assert results[0].status == "failed"
    assert results[0].error_type == "TimeoutError"
    effects = await sessions.read_effects(session_id)
    assert [event.type for event in effects] == [
        "effect/intent",
        "effect/dispatched",
        "effect/outcome",
    ]
    assert effects[-1].data["status"] == "failed"
