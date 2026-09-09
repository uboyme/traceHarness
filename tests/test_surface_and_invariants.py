from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

from traceh.api.events import EventEnvelope, PendingEvent
from traceh.api.llm import ModelAttemptIdentity
from traceh.kernel.composition import RuntimeComposition
from traceh.runtime.request_builder import RequestBuilder
from traceh.session.context_input import ContextInputService
from traceh.session.event_store import InMemoryEventStore
from traceh.session.invariants import CoreInvariantChecker
from traceh.session.protocol import CONTEXT_PROTOCOL
from traceh.session.service import SessionService
from traceh.session.surface import SurfaceProjector
from traceh.session.surface_replacement import surface_prefix, surface_replacement_data


def session_created() -> PendingEvent:
    return PendingEvent(
        "session/created",
        {
            "session_id": "s",
            "workspace": "fixture-workspace",
            "metadata": {},
            "context_protocol": CONTEXT_PROTOCOL,
        },
    )


def materialize(events: list[PendingEvent]) -> tuple[EventEnvelope, ...]:
    return tuple(
        EventEnvelope.materialize("session:s", index, event)
        for index, event in enumerate(events, start=1)
    )


def test_surface_projects_messages_and_replacement() -> None:
    history = [
        session_created(),
        PendingEvent("turn/start", {"turn_id": "t"}),
        PendingEvent("step/start", {"turn_id": "t", "step_id": "a"}),
        PendingEvent("user/message", {"content": "old", "step_id": "a"}),
        PendingEvent(
            "assistant/message",
            {"content": "answer", "tool_calls": [], "step_id": "a"},
        ),
        PendingEvent("step/end", {"turn_id": "t", "step_id": "a"}),
        PendingEvent("turn/end", {"turn_id": "t"}),
    ]
    # Derived from the real history rather than invented: the invariant checker
    # recomputes the digest and both byte counts.
    original = materialize(history)
    prefix = surface_prefix(original, cut_seq=original[-1].seq)
    assert prefix is not None and prefix.source_seqs == tuple(
        event.seq for event in original if event.type in {"user/message", "assistant/message"}
    )
    events = materialize(
        history
        + [
            PendingEvent(
                "surface/replace",
                surface_replacement_data(
                    method="manual",
                    cut_seq=prefix.cut_seq,
                    source_seqs=prefix.source_seqs,
                    source_digest=prefix.source_digest,
                    source_utf8_bytes=prefix.source_utf8_bytes,
                    history_utf8_bytes=prefix.history_utf8_bytes,
                    kept_recent_turns=0,
                    policy_digest=None,
                    summarizer=None,
                    summary="summary",
                    summary_truncated=False,
                ),
            ),
        ]
    )
    messages = SurfaceProjector().project(events)
    assert [message.role for message in messages] == ["user"]
    assert messages[0].content.startswith("Compacted earlier conversation")
    assert '"summary":"summary"' in messages[0].content
    assert not CoreInvariantChecker().check(events)


def test_invariants_detect_unmatched_tool_result() -> None:
    events = materialize(
        [
            session_created(),
            PendingEvent("turn/start", {"turn_id": "t"}),
            PendingEvent("step/start", {"turn_id": "t", "step_id": "a"}),
            PendingEvent(
                "tool/result",
                {"step_id": "a", "tool_call_id": "missing", "tool_name": "x"},
            ),
            PendingEvent("step/end", {"turn_id": "t", "step_id": "a"}),
            PendingEvent("turn/end", {"turn_id": "t"}),
        ]
    )
    violations = CoreInvariantChecker().check(events)
    assert any(item.name == "tool-result-has-call" for item in violations)


async def recorded_attempt(tmp_path):
    """Get one real Context/Composition/Request/permit prefix before corrupting it."""
    sessions = SessionService(InMemoryEventStore())
    session_id = await sessions.create_session(tmp_path, session_id="s")
    await sessions.append_session(session_id, "turn/start", {"turn_id": "t"})
    await sessions.append_session(
        session_id, "user/message", {"turn_id": "t", "content": "Inspect the current source."}
    )
    await sessions.append_session(session_id, "step/start", {"turn_id": "t", "step_id": "a"})
    composition = RuntimeComposition(
        provider="scripted", model="model", system_prompt="Follow host policy.", tools=()
    ).snapshot()
    context = await ContextInputService(sessions.read_session).freeze(
        session_id=session_id, turn_id="t", step_id="a", composition=composition
    )
    await sessions.append_context_input(
        session_id, context.to_dict(), expected_seq=context.to_dict()["observed_session_seq"]
    )
    composition_event = await sessions.append_session(
        session_id,
        "composition/snapshot",
        composition.to_dict(),
        composition_revision=composition.revision,
    )
    built = await RequestBuilder(sessions, SurfaceProjector()).build(
        session_id=session_id,
        turn_id="t",
        step_id="a",
        composition=composition,
        through_seq=composition_event.seq,
    )
    await sessions.start_model_attempt(
        session_id,
        attempt=ModelAttemptIdentity(session_id, "t", "a", "m1", 1),
        source_seq=built.source_seq,
        composition_revision=composition.revision,
        composed_request=built.request,
        composed_fingerprint=built.fingerprint,
        dispatch_request=built.request,
        dispatch_fingerprint=built.fingerprint,
        reservation_id=None,
    )
    events = await sessions.read_session(session_id)
    assert CoreInvariantChecker().check(events) == ()
    return events


def append_tail(events, *pending):
    tail = tuple(
        EventEnvelope.materialize(events[0].stream_id, events[-1].seq + index, event)
        for index, event in enumerate(pending, 1)
    )
    return (*events, *tail)


def attempt_end(start, *, step_id=None, recovered=False, causation_id=None):
    data = {
        key: start.data[key]
        for key in (
            "turn_id",
            "step_id",
            "attempt_id",
            "ordinal",
            "request_snapshot_seq",
            "dispatch_fingerprint",
            "reservation_id",
        )
    }
    if step_id is not None:
        data["step_id"] = step_id
    data["status"] = "unknown_after_crash" if recovered else "succeeded"
    if recovered:
        data["recovered"] = True
    return PendingEvent(
        "model/attempt-end",
        data,
        causation_id=causation_id,
        composition_revision=start.composition_revision,
    )


def close_step():
    return PendingEvent("step/end", {"turn_id": "t", "step_id": "a", "reason": "interrupted"})


def close_turn():
    return PendingEvent("turn/end", {"turn_id": "t", "reason": "interrupted"})


def names(events):
    return {item.name for item in CoreInvariantChecker().check(events)}


async def test_invariants_accept_a_paired_model_attempt(tmp_path) -> None:
    events = await recorded_attempt(tmp_path)
    start = events[-1]
    events = append_tail(
        events,
        PendingEvent(
            "assistant/message",
            {"turn_id": "t", "step_id": "a", "attempt_id": "m1", "content": "hi"},
        ),
        attempt_end(start),
        close_step(),
        close_turn(),
    )
    assert CoreInvariantChecker().check(events) == ()


async def test_invariants_accept_an_attempt_still_running_in_an_open_step(tmp_path) -> None:
    assert CoreInvariantChecker().check(await recorded_attempt(tmp_path)) == ()


async def test_invariants_accept_append_only_attempt_repair(tmp_path) -> None:
    events = await recorded_attempt(tmp_path)
    start = events[-1]
    events = append_tail(
        events,
        close_step(),
        close_turn(),
        attempt_end(start, recovered=True, causation_id=start.event_id),
        PendingEvent("runtime/recovered", {"closed_model_attempts": 1}),
    )
    assert CoreInvariantChecker().check(events) == ()


async def test_invariants_detect_plain_attempt_end_after_the_step_closed(tmp_path) -> None:
    events = await recorded_attempt(tmp_path)
    events = append_tail(events, close_step(), close_turn(), attempt_end(events[-1]))
    assert "attempt-end-inside-step" in names(events)


async def test_invariants_reject_a_late_recovered_end_without_causation(tmp_path) -> None:
    events = await recorded_attempt(tmp_path)
    events = append_tail(
        events,
        close_step(),
        close_turn(),
        attempt_end(events[-1], recovered=True, causation_id=uuid4()),
    )
    assert "attempt-end-inside-step" in names(events)


async def test_invariants_reject_unusable_attempt_ids(tmp_path) -> None:
    original = await recorded_attempt(tmp_path)
    for unusable in (None, 7, True, "", "   "):
        start = replace(original[-1], data={**original[-1].data, "attempt_id": unusable})
        assert "attempt-id-present" in names((*original[:-1], start)), unusable


async def test_invariants_detect_attempt_started_outside_a_step(tmp_path) -> None:
    events = await recorded_attempt(tmp_path)
    before_step = tuple(
        event
        for event in events
        if event.seq < next(item.seq for item in events if item.type == "step/start")
    )
    start = events[-1]
    invalid = append_tail(before_step, PendingEvent(start.type, start.data))
    assert "attempt-start-inside-step" in names(invalid)


async def test_invariants_detect_attempt_started_in_a_step_that_is_not_open(tmp_path) -> None:
    events = await recorded_attempt(tmp_path)
    start = replace(events[-1], data={**events[-1].data, "step_id": "b"})
    assert "attempt-start-inside-step" in names((*events[:-1], start))


async def test_invariants_detect_attempt_end_without_start(tmp_path) -> None:
    events = await recorded_attempt(tmp_path)
    invalid = append_tail(events[:-1], attempt_end(events[-1]), close_step(), close_turn())
    assert "attempt-end-has-start" in names(invalid)


async def test_invariants_detect_duplicate_attempt_start(tmp_path) -> None:
    events = await recorded_attempt(tmp_path)
    start = events[-1]
    invalid = append_tail(events, PendingEvent(start.type, start.data), attempt_end(start))
    assert "single-attempt-start" in names(invalid)


async def test_invariants_detect_duplicate_attempt_end(tmp_path) -> None:
    events = await recorded_attempt(tmp_path)
    invalid = append_tail(events, attempt_end(events[-1]), attempt_end(events[-1]))
    assert "single-attempt-end" in names(invalid)


async def test_invariants_detect_attempt_closed_in_another_step(tmp_path) -> None:
    events = await recorded_attempt(tmp_path)
    invalid = append_tail(
        events,
        close_step(),
        PendingEvent("step/start", {"turn_id": "t", "step_id": "b"}),
        attempt_end(events[-1], step_id="b"),
    )
    assert "attempt-end-same-scope" in names(invalid)


async def test_invariants_detect_unclosed_attempt_in_closed_step(tmp_path) -> None:
    events = append_tail(await recorded_attempt(tmp_path), close_step(), close_turn())
    assert "attempt-has-end" in names(events)


async def test_invariants_detect_missing_attempt_id(tmp_path) -> None:
    events = await recorded_attempt(tmp_path)
    data = {key: value for key, value in events[-1].data.items() if key != "attempt_id"}
    assert "attempt-id-present" in names((*events[:-1], replace(events[-1], data=data)))


async def test_invariants_detect_two_open_attempts_in_one_step(tmp_path) -> None:
    events = await recorded_attempt(tmp_path)
    second = PendingEvent(events[-1].type, {**events[-1].data, "attempt_id": "m2"})
    assert "single-open-attempt" in names(append_tail(events, second))
