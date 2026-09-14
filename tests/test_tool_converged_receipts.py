"""Failed/cancelled owned writes retain their exact outcome through original Effects."""

import asyncio

import pytest

from traceh.api.llm import ToolCall
from traceh.api.tools import (
    EffectKind,
    ToolExecutionCancelled,
    ToolExecutionContext,
    ToolExecutionFailure,
    ToolOutput,
)
from traceh.session.event_store import InMemoryEventStore
from traceh.session.service import SessionService
from traceh.session.tool_output import resolve_tool_output
from traceh.tools.policy import AllowByDefaultPolicy
from traceh.tools.registry import ToolRegistry
from traceh.tools.runtime import ToolRuntime


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_converged_receipt_retained_and_result_matches(tmp_path, cancel):
    entered, release = asyncio.Event(), asyncio.Event()
    receipt = {
        "outcome": "rolled_back",
        "files": [{"path": "real.txt", "status": "rolled_back"}],
        "detail": "explicit long receipt " * 300,
    }

    class OwnedWrite:
        name = "owned_write"
        description = "Test an actual compensated write."
        effect_kind = EffectKind.WORKSPACE_WRITE
        input_schema = {"type": "object", "properties": {}, "additionalProperties": False}

        async def execute(self, arguments, context):
            path = context.workspace / "real.txt"
            path.write_bytes(b"changed")
            entered.set()
            if cancel:
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    path.write_bytes(b"original")
                    raise ToolExecutionCancelled(
                        ToolOutput("cancelled with receipt", receipt)
                    ) from None
            path.write_bytes(b"original")
            raise ToolExecutionFailure(ToolOutput("failed with receipt", receipt))

    sessions = SessionService(InMemoryEventStore())
    session_id = await sessions.create_session(tmp_path)
    await sessions.append_session(session_id, "turn/start", {"turn_id": "t"})
    await sessions.append_session(session_id, "step/start", {"turn_id": "t", "step_id": "s"})
    registry = ToolRegistry()
    registry.register(OwnedWrite())
    runtime = ToolRuntime(
        registry, sessions, policies=(AllowByDefaultPolicy(),), max_output_chars=1000
    )
    context = ToolExecutionContext(session_id, "t", "s", "batch", tmp_path, tmp_path / "data")
    task = asyncio.create_task(
        runtime.execute_batch(
            (ToolCall("write", "owned_write", {}),), context=context, composition_revision="r"
        )
    )
    await asyncio.wait_for(entered.wait(), 5)
    if cancel:
        assert (tmp_path / "real.txt").read_bytes() == b"changed"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await task
        assert result[0].status == "failed"
    assert (tmp_path / "real.txt").read_bytes() == b"original"
    effects = await sessions.read_effects(session_id)
    outcomes = [e for e in effects if e.type == "effect/outcome"]
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.data["status"] == ("cancelled" if cancel else "failed")
    ref = outcome.data["output_ref"]
    payload = resolve_tool_output(
        await sessions.read_session(session_id),
        effects,
        session_id=session_id,
        effect_id=ref["effect_id"],
        digest=ref["digest"],
    )
    assert payload["data"] == receipt
