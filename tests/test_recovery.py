from __future__ import annotations

import pytest

from traceh.session.event_store import InMemoryEventStore
from traceh.session.invariants import CoreInvariantChecker
from traceh.session.recovery import RecoveryService
from traceh.session.service import SessionService


@pytest.mark.asyncio
async def test_recovery_synthesizes_result_from_durable_effect_outcome(tmp_path) -> None:
    sessions = SessionService(InMemoryEventStore())
    session_id = await sessions.create_session(tmp_path)
    await sessions.append_session(session_id, "turn/start", {"turn_id": "t"})
    await sessions.append_session(
        session_id,
        "step/start",
        {"turn_id": "t", "step_id": "s"},
    )
    await sessions.append_session(
        session_id,
        "tool/call",
        {
            "turn_id": "t",
            "step_id": "s",
            "tool_call_id": "c",
            "tool_name": "apply_patch",
            "arguments": {},
        },
    )
    await sessions.append_effect(
        session_id,
        "effect/intent",
        {"effect_id": "e", "tool_call_id": "c", "tool_name": "apply_patch"},
    )
    await sessions.append_effect(
        session_id,
        "effect/outcome",
        {
            "effect_id": "e",
            "tool_call_id": "c",
            "tool_name": "apply_patch",
            "status": "succeeded",
            "content": "file changed",
            "data": {"after_sha256": "abc"},
        },
    )

    report = await RecoveryService(sessions).recover(session_id)
    assert report.changed
    assert report.synthesized_tool_results == 1
    assert report.closed_step and report.closed_turn
    events = await sessions.read_session(session_id)
    result = next(event for event in events if event.type == "tool/result")
    assert result.data["status"] == "succeeded"
    assert result.data["content"] == "file changed"
    assert not CoreInvariantChecker().check(
        await sessions.read_session(session_id),
        await sessions.read_effects(session_id),
    )


@pytest.mark.asyncio
async def test_recovery_never_replays_unknown_write_effect(tmp_path) -> None:
    sessions = SessionService(InMemoryEventStore())
    session_id = await sessions.create_session(tmp_path)
    await sessions.append_session(session_id, "turn/start", {"turn_id": "t"})
    await sessions.append_session(
        session_id,
        "step/start",
        {"turn_id": "t", "step_id": "s"},
    )
    await sessions.append_session(
        session_id,
        "tool/call",
        {
            "turn_id": "t",
            "step_id": "s",
            "tool_call_id": "c",
            "tool_name": "apply_patch",
            "arguments": {},
        },
    )
    await sessions.append_effect(
        session_id,
        "effect/intent",
        {"effect_id": "e", "tool_call_id": "c", "tool_name": "apply_patch"},
    )

    await RecoveryService(sessions).recover(session_id)
    events = await sessions.read_session(session_id)
    result = next(event for event in events if event.type == "tool/result")
    assert result.data["status"] == "unknown_after_crash"
    effects = await sessions.read_effects(session_id)
    assert any(event.type == "effect/reconciled" for event in effects)
    assert not CoreInvariantChecker().check(
        await sessions.read_session(session_id),
        effects,
    )
