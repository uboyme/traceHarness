from __future__ import annotations

from uuid import UUID, uuid4

from traceh.api.events import EventEnvelope, PendingEvent
from traceh.api.json_types import fingerprint
from traceh.api.llm import ModelMessage, ModelRequest
from traceh.session.invariants import CoreInvariantChecker
from traceh.session.surface import SurfaceProjector
from traceh.session.surface_replacement import surface_prefix, surface_replacement_data


def materialize(events: list[PendingEvent]) -> tuple[EventEnvelope, ...]:
    return tuple(
        EventEnvelope.materialize("session:s", index, event)
        for index, event in enumerate(events, start=1)
    )


def test_surface_projects_messages_and_replacement() -> None:
    history = [
        PendingEvent("session/created", {"session_id": "s"}),
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
    prefix = surface_prefix(materialize(history), cut_seq=7)
    assert prefix is not None and prefix.source_seqs == (4, 5)
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
            PendingEvent("session/created", {"session_id": "s"}),
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


def attempt_start(
    attempt_id: object = "m1",
    *,
    turn_id: str = "t",
    step_id: str = "a",
    event_id: UUID | None = None,
    ordinal: int = 1,
    request_snapshot_seq: int = 4,
) -> PendingEvent:
    return PendingEvent(
        "model/attempt-start",
        {
            "turn_id": turn_id,
            "step_id": step_id,
            "attempt_id": attempt_id,
            "ordinal": ordinal,
            "request_snapshot_seq": request_snapshot_seq,
            "dispatch_fingerprint": request_fingerprint(),
            "reservation_id": None,
            "provider": "scripted",
            "model": "model",
            "retry_wait_milliseconds": 0 if ordinal == 1 else 1,
            "retry_failure_code": None if ordinal == 1 else "provider-timeout",
            "retry_failure_category": None if ordinal == 1 else "timeout",
        },
        event_id=event_id,
    )


def request_payload() -> dict[str, object]:
    return ModelRequest(
        provider="scripted",
        model="model",
        messages=(ModelMessage(role="user", content="work"),),
        metadata={
            "session_id": "s",
            "turn_id": "t",
            "step_id": "a",
            "composition_revision": "revision",
        },
    ).to_dict()


def request_fingerprint() -> str:
    return fingerprint(request_payload())


def request_snapshot(*, turn_id: str = "t", step_id: str = "a") -> PendingEvent:
    payload = request_payload()
    request_hash = fingerprint(payload)
    return PendingEvent(
        "request/snapshot",
        {
            "turn_id": turn_id,
            "step_id": step_id,
            "source_seq": 3,
            "composition_revision": "revision",
            "composed_fingerprint": request_hash,
            "dispatch_fingerprint": request_hash,
            "composed_request": payload,
            "dispatch_request": payload,
        },
        composition_revision="revision",
    )


def recovered_attempt_end(
    attempt_id: str,
    start_event_id: UUID | None,
    *,
    turn_id: str = "t",
    step_id: str = "a",
) -> PendingEvent:
    """The shape RecoveryService appends when it repairs an old session."""

    return PendingEvent(
        "model/attempt-end",
        {
            "turn_id": turn_id,
            "step_id": step_id,
            "attempt_id": attempt_id,
            "ordinal": 1,
            "request_snapshot_seq": 4,
            "dispatch_fingerprint": request_fingerprint(),
            "reservation_id": None,
            "status": "unknown_after_crash",
            "recovered": True,
        },
        causation_id=start_event_id,
    )


def attempt_end(
    attempt_id: str,
    *,
    turn_id: str = "t",
    step_id: str = "a",
    status: str = "succeeded",
) -> PendingEvent:
    return PendingEvent(
        "model/attempt-end",
        {
            "turn_id": turn_id,
            "step_id": step_id,
            "attempt_id": attempt_id,
            "ordinal": 1,
            "request_snapshot_seq": 4,
            "dispatch_fingerprint": request_fingerprint(),
            "reservation_id": None,
            "status": status,
        },
    )


def names(events: tuple[EventEnvelope, ...]) -> set[str]:
    return {item.name for item in CoreInvariantChecker().check(events)}


def test_invariants_accept_a_paired_model_attempt() -> None:
    events = materialize(
        [
            PendingEvent("session/created", {"session_id": "s"}),
            PendingEvent("turn/start", {"turn_id": "t"}),
            PendingEvent("step/start", {"turn_id": "t", "step_id": "a"}),
            request_snapshot(),
            attempt_start("m1"),
            PendingEvent(
                "assistant/message",
                {"turn_id": "t", "step_id": "a", "attempt_id": "m1", "content": "hi"},
            ),
            attempt_end("m1"),
            PendingEvent("step/end", {"turn_id": "t", "step_id": "a"}),
            PendingEvent("turn/end", {"turn_id": "t"}),
        ]
    )
    assert CoreInvariantChecker().check(events) == ()


def test_invariants_accept_an_attempt_still_running_in_an_open_step() -> None:
    events = materialize(
        [
            PendingEvent("session/created", {"session_id": "s"}),
            PendingEvent("turn/start", {"turn_id": "t"}),
            PendingEvent("step/start", {"turn_id": "t", "step_id": "a"}),
            request_snapshot(),
            attempt_start("m1"),
        ]
    )
    assert CoreInvariantChecker().check(events) == ()


def test_invariants_accept_append_only_attempt_repair() -> None:
    # An older version closed the step and turn first; recovery could only
    # append the attempt end afterwards, so it must name the start it repairs.
    start_event_id = uuid4()
    events = materialize(
        [
            PendingEvent("session/created", {"session_id": "s"}),
            PendingEvent("turn/start", {"turn_id": "t"}),
            PendingEvent("step/start", {"turn_id": "t", "step_id": "a"}),
            request_snapshot(),
            attempt_start("m1", event_id=start_event_id),
            PendingEvent("step/end", {"turn_id": "t", "step_id": "a", "reason": "interrupted"}),
            PendingEvent("turn/end", {"turn_id": "t", "reason": "interrupted"}),
            recovered_attempt_end("m1", start_event_id),
            PendingEvent("runtime/recovered", {"closed_model_attempts": 1}),
        ]
    )
    assert CoreInvariantChecker().check(events) == ()


def test_invariants_detect_plain_attempt_end_after_the_step_closed() -> None:
    events = materialize(
        [
            PendingEvent("session/created", {"session_id": "s"}),
            PendingEvent("turn/start", {"turn_id": "t"}),
            PendingEvent("step/start", {"turn_id": "t", "step_id": "a"}),
            request_snapshot(),
            attempt_start("m1"),
            PendingEvent("step/end", {"turn_id": "t", "step_id": "a"}),
            PendingEvent("turn/end", {"turn_id": "t"}),
            attempt_end("m1"),
        ]
    )
    assert "attempt-end-inside-step" in names(events)


def test_invariants_reject_a_late_recovered_end_without_causation() -> None:
    # The append-only exemption is not a free pass: an end that does not point
    # at its own start cannot claim to be a repair.
    events = materialize(
        [
            PendingEvent("session/created", {"session_id": "s"}),
            PendingEvent("turn/start", {"turn_id": "t"}),
            PendingEvent("step/start", {"turn_id": "t", "step_id": "a"}),
            request_snapshot(),
            attempt_start("m1", event_id=uuid4()),
            PendingEvent("step/end", {"turn_id": "t", "step_id": "a"}),
            PendingEvent("turn/end", {"turn_id": "t"}),
            recovered_attempt_end("m1", uuid4()),
        ]
    )
    assert "attempt-end-inside-step" in names(events)


def test_invariants_reject_unusable_attempt_ids() -> None:
    for unusable in (None, 7, True, "", "   "):
        events = materialize(
            [
                PendingEvent("session/created", {"session_id": "s"}),
                PendingEvent("turn/start", {"turn_id": "t"}),
                PendingEvent("step/start", {"turn_id": "t", "step_id": "a"}),
                request_snapshot(),
                attempt_start(unusable),
            ]
        )
        assert "attempt-id-present" in names(events), unusable


def test_invariants_detect_attempt_started_outside_a_step() -> None:
    events = materialize(
        [
            PendingEvent("session/created", {"session_id": "s"}),
            PendingEvent("turn/start", {"turn_id": "t"}),
            attempt_start("m1"),
        ]
    )
    assert "attempt-start-inside-step" in names(events)


def test_invariants_detect_attempt_started_in_a_step_that_is_not_open() -> None:
    # The payload claims step "b" while step "a" is the one really open.
    events = materialize(
        [
            PendingEvent("session/created", {"session_id": "s"}),
            PendingEvent("turn/start", {"turn_id": "t"}),
            PendingEvent("step/start", {"turn_id": "t", "step_id": "a"}),
            request_snapshot(),
            attempt_start("m1", step_id="b"),
        ]
    )
    assert "attempt-start-inside-step" in names(events)


def test_invariants_detect_attempt_end_without_start() -> None:
    events = materialize(
        [
            PendingEvent("session/created", {"session_id": "s"}),
            PendingEvent("turn/start", {"turn_id": "t"}),
            PendingEvent("step/start", {"turn_id": "t", "step_id": "a"}),
            attempt_end("ghost"),
            PendingEvent("step/end", {"turn_id": "t", "step_id": "a"}),
            PendingEvent("turn/end", {"turn_id": "t"}),
        ]
    )
    assert "attempt-end-has-start" in names(events)


def test_invariants_detect_duplicate_attempt_start() -> None:
    events = materialize(
        [
            PendingEvent("session/created", {"session_id": "s"}),
            PendingEvent("turn/start", {"turn_id": "t"}),
            PendingEvent("step/start", {"turn_id": "t", "step_id": "a"}),
            request_snapshot(),
            attempt_start("m1"),
            attempt_start("m1"),
            attempt_end("m1"),
        ]
    )
    assert "single-attempt-start" in names(events)


def test_invariants_detect_duplicate_attempt_end() -> None:
    events = materialize(
        [
            PendingEvent("session/created", {"session_id": "s"}),
            PendingEvent("turn/start", {"turn_id": "t"}),
            PendingEvent("step/start", {"turn_id": "t", "step_id": "a"}),
            request_snapshot(),
            attempt_start("m1"),
            attempt_end("m1"),
            attempt_end("m1"),
        ]
    )
    assert "single-attempt-end" in names(events)


def test_invariants_detect_attempt_closed_in_another_step() -> None:
    events = materialize(
        [
            PendingEvent("session/created", {"session_id": "s"}),
            PendingEvent("turn/start", {"turn_id": "t"}),
            PendingEvent("step/start", {"turn_id": "t", "step_id": "a"}),
            request_snapshot(),
            attempt_start("m1"),
            PendingEvent("step/end", {"turn_id": "t", "step_id": "a"}),
            PendingEvent("step/start", {"turn_id": "t", "step_id": "b"}),
            attempt_end("m1", step_id="b"),
        ]
    )
    assert "attempt-end-same-scope" in names(events)


def test_invariants_detect_unclosed_attempt_in_closed_step() -> None:
    events = materialize(
        [
            PendingEvent("session/created", {"session_id": "s"}),
            PendingEvent("turn/start", {"turn_id": "t"}),
            PendingEvent("step/start", {"turn_id": "t", "step_id": "a"}),
            request_snapshot(),
            attempt_start("m1"),
            PendingEvent("step/end", {"turn_id": "t", "step_id": "a"}),
            PendingEvent("turn/end", {"turn_id": "t"}),
        ]
    )
    assert "attempt-has-end" in names(events)


def test_invariants_detect_missing_attempt_id() -> None:
    events = materialize(
        [
            PendingEvent("session/created", {"session_id": "s"}),
            PendingEvent("turn/start", {"turn_id": "t"}),
            PendingEvent("step/start", {"turn_id": "t", "step_id": "a"}),
            PendingEvent("model/attempt-start", {"turn_id": "t", "step_id": "a"}),
        ]
    )
    assert "attempt-id-present" in names(events)


def test_invariants_detect_two_open_attempts_in_one_step() -> None:
    events = materialize(
        [
            PendingEvent("session/created", {"session_id": "s"}),
            PendingEvent("turn/start", {"turn_id": "t"}),
            PendingEvent("step/start", {"turn_id": "t", "step_id": "a"}),
            request_snapshot(),
            attempt_start("m1"),
            attempt_start("m2"),
        ]
    )
    assert "single-open-attempt" in names(events)
