"""Descriptive overlap and duplicate inputs must not become success judgments."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from traceh.evaluation.evaluators.product_handoffs import collaboration_diagnostics


def turn(start, end=None):
    base = datetime(2026, 1, 1, tzinfo=UTC)
    events = [
        SimpleNamespace(
            type="turn/start", data={"turn_id": "turn"}, occurred_at=base + timedelta(seconds=start)
        )
    ]
    if end is not None:
        events.append(
            SimpleNamespace(
                type="turn/end", data={"turn_id": "turn"}, occurred_at=base + timedelta(seconds=end)
            )
        )
    return events


def test_overlap_uses_half_open_intervals_and_includes_the_main_turn():
    result = collaboration_diagnostics([turn(0, 10), turn(2, 6), turn(6, 9)], [])
    assert result["observed_peak_active_turns"] == 2
    assert result["available_reports"] == 0


def test_unsettled_or_invalid_intervals_are_unavailable_not_zero():
    for sessions in ([turn(0)], [turn(4, 2)]):
        assert collaboration_diagnostics(sessions, [])["observed_peak_active_turns"] is None


def test_identical_inputs_and_report_visibility_remain_distinct():
    rows = [
        {"work_digest": "same", "report_available": True, "parent_report_dispatches": []},
        {
            "work_digest": "same",
            "report_available": True,
            "parent_report_dispatches": ["request@1"],
        },
        {"work_digest": "different", "report_available": False, "parent_report_dispatches": []},
    ]
    result = collaboration_diagnostics([turn(0, 2)], rows)
    assert result["identical_work_groups"] == result["identical_work_extra_deliveries"] == 1
    assert result["available_reports"] == 2
    assert result["reports_dispatched_to_parent"] == 1
    assert "success" not in result
