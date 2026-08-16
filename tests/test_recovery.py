from __future__ import annotations

from uuid import uuid4

import pytest

from traceh.session.event_store import InMemoryEventStore
from traceh.session.invariants import CoreInvariantChecker
from traceh.session.recovery import RecoveryService
from traceh.session.service import SessionService


async def open_step_with_attempt(
    sessions: SessionService,
    session_id: str,
    *,
    attempt_id: str = "a",
    turn_id: str = "t",
    step_id: str = "s",
    correlation_id=None,
    composition_revision: str | None = None,
) -> None:
    """Drive a session up to a started-but-unfinished model attempt."""

    await sessions.append_session(session_id, "turn/start", {"turn_id": turn_id})
    await sessions.append_session(
        session_id, "step/start", {"turn_id": turn_id, "step_id": step_id}
    )
    await sessions.append_session(
        session_id,
        "model/attempt-start",
        {
            "turn_id": turn_id,
            "step_id": step_id,
            "attempt_id": attempt_id,
            "provider": "scripted",
            "model": "scripted-model",
        },
        correlation_id=correlation_id,
        composition_revision=composition_revision,
    )


def types_of(events) -> list[str]:
    return [event.type for event in events]


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


@pytest.mark.asyncio
async def test_recovery_closes_attempt_that_crashed_right_after_start(tmp_path) -> None:
    sessions = SessionService(InMemoryEventStore())
    session_id = await sessions.create_session(tmp_path)
    correlation_id = uuid4()
    await open_step_with_attempt(
        sessions,
        session_id,
        correlation_id=correlation_id,
        composition_revision="rev-1",
    )
    start = (await sessions.read_session(session_id))[-1]

    report = await RecoveryService(sessions).recover(session_id)
    assert report.changed
    assert report.closed_model_attempts == 1

    events = await sessions.read_session(session_id)
    attempt_end = next(event for event in events if event.type == "model/attempt-end")
    assert attempt_end.data["status"] == "unknown_after_crash"
    assert attempt_end.data["error_type"] == "RecoveredAfterCrash"
    assert attempt_end.data["recovered"] is True
    assert attempt_end.data["recovered_from"] == "none"
    assert attempt_end.data["partial_chunks"] == 0
    assert attempt_end.data["attempt_id"] == "a"
    # Nothing may be invented about a response that was never observed.
    assert "usage" not in attempt_end.data
    assert "finish_reason" not in attempt_end.data
    assert "assistant/message" not in types_of(events)

    # Audit trail back to the attempt that was recovered.
    assert attempt_end.causation_id == start.event_id
    assert attempt_end.correlation_id == correlation_id
    assert attempt_end.composition_revision == "rev-1"

    ordered = types_of(events)
    assert ordered.index("model/attempt-end") < ordered.index("step/end")
    assert ordered.index("step/end") < ordered.index("turn/end")
    assert ordered.index("turn/end") < ordered.index("runtime/recovered")
    assert not CoreInvariantChecker().check(events, await sessions.read_effects(session_id))


@pytest.mark.asyncio
async def test_recovery_keeps_partial_chunks_without_building_a_message(tmp_path) -> None:
    sessions = SessionService(InMemoryEventStore())
    session_id = await sessions.create_session(tmp_path)
    await open_step_with_attempt(sessions, session_id)
    for piece in ("par", "tial"):
        await sessions.append_session(
            session_id,
            "assistant/chunk",
            {"turn_id": "t", "step_id": "s", "attempt_id": "a", "content": piece},
        )

    report = await RecoveryService(sessions).recover(session_id)
    assert report.closed_model_attempts == 1

    events = await sessions.read_session(session_id)
    attempt_end = next(event for event in events if event.type == "model/attempt-end")
    assert attempt_end.data["status"] == "unknown_after_crash"
    assert attempt_end.data["partial_chunks"] == 2
    # Chunks stay as audit evidence and are never merged into a message.
    assert types_of(events).count("assistant/chunk") == 2
    assert "assistant/message" not in types_of(events)
    assert not CoreInvariantChecker().check(events, await sessions.read_effects(session_id))


@pytest.mark.asyncio
async def test_recovery_closes_attempt_with_durable_assistant_message(tmp_path) -> None:
    sessions = SessionService(InMemoryEventStore())
    session_id = await sessions.create_session(tmp_path)
    await open_step_with_attempt(sessions, session_id)
    await sessions.append_session(
        session_id,
        "assistant/message",
        {
            "turn_id": "t",
            "step_id": "s",
            "attempt_id": "a",
            "content": "done",
            "tool_calls": [],
        },
    )

    report = await RecoveryService(sessions).recover(session_id)
    assert report.closed_model_attempts == 1

    events = await sessions.read_session(session_id)
    attempt_end = next(event for event in events if event.type == "model/attempt-end")
    assert attempt_end.data["status"] == "succeeded"
    assert attempt_end.data["recovered"] is True
    assert attempt_end.data["recovered_from"] == "assistant/message"
    assert "usage" not in attempt_end.data
    assert "finish_reason" not in attempt_end.data
    # The durable message is evidence, not something to duplicate.
    assert types_of(events).count("assistant/message") == 1
    assert not CoreInvariantChecker().check(events, await sessions.read_effects(session_id))


@pytest.mark.asyncio
async def test_recovery_ignores_assistant_message_from_another_step(tmp_path) -> None:
    sessions = SessionService(InMemoryEventStore())
    session_id = await sessions.create_session(tmp_path)
    await open_step_with_attempt(sessions, session_id)
    await sessions.append_session(
        session_id,
        "assistant/message",
        {
            "turn_id": "t",
            "step_id": "other-step",
            "attempt_id": "a",
            "content": "not this attempt",
            "tool_calls": [],
        },
    )

    await RecoveryService(sessions).recover(session_id)
    events = await sessions.read_session(session_id)
    attempt_end = next(event for event in events if event.type == "model/attempt-end")
    assert attempt_end.data["status"] == "unknown_after_crash"
    assert attempt_end.data["recovered_from"] == "none"


@pytest.mark.asyncio
async def test_recovering_twice_appends_no_second_attempt_end(tmp_path) -> None:
    sessions = SessionService(InMemoryEventStore())
    session_id = await sessions.create_session(tmp_path)
    await open_step_with_attempt(sessions, session_id)

    recovery = RecoveryService(sessions)
    first = await recovery.recover(session_id)
    assert first.changed
    after_first = await sessions.read_session(session_id)

    second = await recovery.recover(session_id)
    assert second.changed is False
    assert second.closed_model_attempts == 0
    after_second = await sessions.read_session(session_id)
    assert len(after_second) == len(after_first)
    assert types_of(after_second).count("model/attempt-end") == 1
    assert types_of(after_second).count("runtime/recovered") == 1


@pytest.mark.asyncio
async def test_recovery_closes_attempt_left_open_by_an_older_version(tmp_path) -> None:
    sessions = SessionService(InMemoryEventStore())
    session_id = await sessions.create_session(tmp_path)
    await open_step_with_attempt(sessions, session_id)
    # An older recovery closed the lifecycle but left the attempt unpaired.
    await sessions.append_session(
        session_id, "step/end", {"turn_id": "t", "step_id": "s", "reason": "interrupted"}
    )
    await sessions.append_session(
        session_id, "turn/end", {"turn_id": "t", "reason": "interrupted"}
    )
    before = await sessions.read_session(session_id)
    assert any(item.name == "attempt-has-end" for item in CoreInvariantChecker().check(before))

    report = await RecoveryService(sessions).recover(session_id)
    assert report.changed
    assert report.closed_model_attempts == 1
    assert report.closed_step is False
    assert report.closed_turn is False

    events = await sessions.read_session(session_id)
    # Append-only: only the attempt end and the recovery marker are added.
    assert types_of(events)[len(before) :] == ["model/attempt-end", "runtime/recovered"]
    assert types_of(events).count("step/end") == 1
    assert types_of(events).count("turn/end") == 1
    assert not CoreInvariantChecker().check(events, await sessions.read_effects(session_id))


@pytest.mark.asyncio
async def test_recovery_skips_attempt_starts_without_a_usable_id(tmp_path) -> None:
    sessions = SessionService(InMemoryEventStore())
    session_id = await sessions.create_session(tmp_path)
    await sessions.append_session(session_id, "turn/start", {"turn_id": "t"})
    await sessions.append_session(session_id, "step/start", {"turn_id": "t", "step_id": "s"})
    for unusable in (None, 7, "   "):
        await sessions.append_session(
            session_id,
            "model/attempt-start",
            {"turn_id": "t", "step_id": "s", "attempt_id": unusable},
        )

    report = await RecoveryService(sessions).recover(session_id)
    assert report.closed_model_attempts == 0
    # No identity means no attempt end: an id like "None" must never be invented.
    events = await sessions.read_session(session_id)
    assert "model/attempt-end" not in types_of(events)
    assert any("without a usable attempt_id" in note for note in report.notes)


@pytest.mark.asyncio
async def test_recovery_uses_a_later_matching_message_after_a_mismatch(tmp_path) -> None:
    sessions = SessionService(InMemoryEventStore())
    session_id = await sessions.create_session(tmp_path)
    await open_step_with_attempt(sessions, session_id)
    # A wrongly scoped message first, the real one afterwards.
    await sessions.append_session(
        session_id,
        "assistant/message",
        {"turn_id": "t", "step_id": "other", "attempt_id": "a", "content": "wrong scope"},
    )
    await sessions.append_session(
        session_id,
        "assistant/message",
        {"turn_id": "t", "step_id": "s", "attempt_id": "a", "content": "real answer"},
    )

    await RecoveryService(sessions).recover(session_id)
    events = await sessions.read_session(session_id)
    attempt_end = next(event for event in events if event.type == "model/attempt-end")
    assert attempt_end.data["status"] == "succeeded"
    assert attempt_end.data["recovered_from"] == "assistant/message"


@pytest.mark.asyncio
async def test_recovery_ignores_a_message_written_before_the_attempt_started(tmp_path) -> None:
    sessions = SessionService(InMemoryEventStore())
    session_id = await sessions.create_session(tmp_path)
    await sessions.append_session(session_id, "turn/start", {"turn_id": "t"})
    await sessions.append_session(session_id, "step/start", {"turn_id": "t", "step_id": "s"})
    # Same identity and scope, but it cannot describe an attempt that had not
    # started yet.
    await sessions.append_session(
        session_id,
        "assistant/message",
        {"turn_id": "t", "step_id": "s", "attempt_id": "a", "content": "earlier"},
    )
    await sessions.append_session(
        session_id,
        "model/attempt-start",
        {"turn_id": "t", "step_id": "s", "attempt_id": "a"},
    )

    await RecoveryService(sessions).recover(session_id)
    events = await sessions.read_session(session_id)
    attempt_end = next(event for event in events if event.type == "model/attempt-end")
    assert attempt_end.data["status"] == "unknown_after_crash"
    assert attempt_end.data["recovered_from"] == "none"


@pytest.mark.asyncio
async def test_recovery_counts_only_chunks_inside_the_attempt_scope(tmp_path) -> None:
    sessions = SessionService(InMemoryEventStore())
    session_id = await sessions.create_session(tmp_path)
    await sessions.append_session(session_id, "turn/start", {"turn_id": "t"})
    await sessions.append_session(session_id, "step/start", {"turn_id": "t", "step_id": "s"})
    await sessions.append_session(
        session_id,
        "assistant/chunk",
        {"turn_id": "t", "step_id": "s", "attempt_id": "a", "content": "before start"},
    )
    await sessions.append_session(
        session_id,
        "model/attempt-start",
        {"turn_id": "t", "step_id": "s", "attempt_id": "a"},
    )
    for scope in (
        {"turn_id": "t", "step_id": "s"},
        {"turn_id": "t", "step_id": "s"},
        {"turn_id": "t", "step_id": "other"},
        {"turn_id": "other", "step_id": "s"},
    ):
        await sessions.append_session(
            session_id,
            "assistant/chunk",
            {**scope, "attempt_id": "a", "content": "piece"},
        )

    await RecoveryService(sessions).recover(session_id)
    events = await sessions.read_session(session_id)
    attempt_end = next(event for event in events if event.type == "model/attempt-end")
    assert attempt_end.data["status"] == "unknown_after_crash"
    # Only the two in-scope chunks written after the start are counted.
    assert attempt_end.data["partial_chunks"] == 2


@pytest.mark.asyncio
async def test_recovery_ignores_evidence_from_another_turn(tmp_path) -> None:
    sessions = SessionService(InMemoryEventStore())
    session_id = await sessions.create_session(tmp_path)
    await open_step_with_attempt(sessions, session_id)
    await sessions.append_session(
        session_id, "step/end", {"turn_id": "t", "step_id": "s", "reason": "interrupted"}
    )
    await sessions.append_session(session_id, "turn/end", {"turn_id": "t", "reason": "interrupted"})
    # A later turn reuses the attempt id; it says nothing about the old attempt.
    await sessions.append_session(session_id, "turn/start", {"turn_id": "t2"})
    await sessions.append_session(session_id, "step/start", {"turn_id": "t2", "step_id": "s2"})
    await sessions.append_session(
        session_id,
        "assistant/message",
        {"turn_id": "t2", "step_id": "s2", "attempt_id": "a", "content": "other turn"},
    )
    await sessions.append_session(
        session_id,
        "assistant/chunk",
        {"turn_id": "t2", "step_id": "s2", "attempt_id": "a", "content": "other turn"},
    )

    await RecoveryService(sessions).recover(session_id)
    events = await sessions.read_session(session_id)
    attempt_end = next(event for event in events if event.type == "model/attempt-end")
    assert attempt_end.data["status"] == "unknown_after_crash"
    assert attempt_end.data["partial_chunks"] == 0
    assert not CoreInvariantChecker().check(events, await sessions.read_effects(session_id))


@pytest.mark.asyncio
async def test_recovery_closes_multiple_attempts_in_start_order(tmp_path) -> None:
    sessions = SessionService(InMemoryEventStore())
    session_id = await sessions.create_session(tmp_path)
    await sessions.append_session(session_id, "turn/start", {"turn_id": "t"})
    for step_id, attempt_id in (("s1", "a1"), ("s2", "a2")):
        await sessions.append_session(
            session_id, "step/start", {"turn_id": "t", "step_id": step_id}
        )
        await sessions.append_session(
            session_id,
            "model/attempt-start",
            {"turn_id": "t", "step_id": step_id, "attempt_id": attempt_id},
        )
        if step_id == "s1":
            await sessions.append_session(
                session_id,
                "step/end",
                {"turn_id": "t", "step_id": step_id, "reason": "interrupted"},
            )

    report = await RecoveryService(sessions).recover(session_id)
    assert report.closed_model_attempts == 2

    events = await sessions.read_session(session_id)
    ends = [event for event in events if event.type == "model/attempt-end"]
    assert [event.data["attempt_id"] for event in ends] == ["a1", "a2"]
    assert not CoreInvariantChecker().check(events, await sessions.read_effects(session_id))
