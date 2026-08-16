from __future__ import annotations

import asyncio

import pytest

from traceh.api.llm import ModelResponse
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime


@pytest.mark.asyncio
async def test_cancel_closes_turn_and_reaches_quiescence(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = ScriptedLlmProvider(
        (ModelResponse(content="late"),),
        delay_seconds=10,
    )
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data", provider="scripted", model="slow"),
        provider=provider,
    )
    session_id = await runtime.create_session(workspace)
    waiter = asyncio.create_task(runtime.run_existing(session_id, "wait"))

    for _ in range(100):
        events = await runtime.sessions.read_session(session_id)
        if any(event.type == "model/attempt-start" for event in events):
            break
        await asyncio.sleep(0.01)
    assert await runtime.cancel(session_id, reason="test cancellation")
    with pytest.raises(asyncio.CancelledError):
        await waiter

    events = await runtime.sessions.read_session(session_id)
    assert events[-1].type == "turn/end"
    assert events[-1].data["reason"] == "cancelled"
    assert not await runtime.check_invariants(session_id)
    await runtime.dispose()
