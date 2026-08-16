from __future__ import annotations

from traceh.api.events import EventEnvelope, PendingEvent
from traceh.session.invariants import CoreInvariantChecker
from traceh.session.surface import SurfaceProjector


def materialize(events: list[PendingEvent]) -> tuple[EventEnvelope, ...]:
    return tuple(
        EventEnvelope.materialize("session:s", index, event)
        for index, event in enumerate(events, start=1)
    )


def test_surface_projects_messages_and_replacement() -> None:
    events = materialize(
        [
            PendingEvent("session/created", {"session_id": "s"}),
            PendingEvent("turn/start", {"turn_id": "t"}),
            PendingEvent("step/start", {"turn_id": "t", "step_id": "a"}),
            PendingEvent("user/message", {"content": "old", "step_id": "a"}),
            PendingEvent(
                "assistant/message",
                {"content": "answer", "tool_calls": [], "step_id": "a"},
            ),
            PendingEvent("step/end", {"turn_id": "t", "step_id": "a"}),
            PendingEvent(
                "surface/replace",
                {
                    "source_seqs": [4, 5],
                    "replacement": {"role": "user", "content": "summary"},
                },
            ),
            PendingEvent("turn/end", {"turn_id": "t"}),
        ]
    )
    messages = SurfaceProjector().project(events)
    assert [(message.role, message.content) for message in messages] == [("user", "summary")]


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
