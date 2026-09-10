"""The scorer must require successful, owner-bound actual dispatch, not a receipt alone."""

from dataclasses import replace

import pytest
from test_retrieval_episode_evaluator import runner, selected

from traceh.api.json_types import fingerprint
from traceh.evaluation.evaluators.episode_assessment import dispatched_evidence
from traceh.session.service import SessionService
from traceh.session.sqlite import SqliteEventStore


@pytest.mark.parametrize("corruption", ["failed-attempt", "foreign-snapshot", "not-dispatched"])
async def test_disclosure_requires_successful_owned_dispatch(tmp_path, corruption):
    report = await runner(tmp_path, "m-direct").run()
    packet = report.task_report.to_dict()["episodes"][0]
    store = SqliteEventStore(tmp_path / "run/attempts/001/events")
    try:
        sessions = SessionService(store)
        events = await sessions.read_session(packet["session_id"])
        effects = await sessions.read_effects(packet["session_id"])
    finally:
        await store.aclose()
    kwargs = dict(
        target_start=packet["target_start_seq"],
        target_turn=packet["target_turn_id"],
        max_chars=1600,
    )
    assert dispatched_evidence(events, effects, selected("m-direct").data, **kwargs)
    altered = []
    fingerprints = {}
    for event in events:
        if event.seq <= packet["target_start_seq"]:
            altered.append(event)
            continue
        if event.type == "request/snapshot":
            if corruption == "foreign-snapshot":
                event = replace(event, stream_id="session:foreign")
            elif corruption == "not-dispatched":
                data = event.to_dict()["data"]
                data["dispatch_request"]["messages"] = data["dispatch_request"]["messages"][:-1]
                data["dispatch_fingerprint"] = fingerprint(data["dispatch_request"])
                fingerprints[event.seq] = data["dispatch_fingerprint"]
                event = replace(event, data=data)
        if event.type == "model/attempt-end":
            if corruption == "failed-attempt":
                event = replace(event, data={**event.data, "status": "failed"})
            elif corruption == "not-dispatched":
                event = replace(
                    event,
                    data={
                        **event.data,
                        "dispatch_fingerprint": fingerprints[event.data["request_snapshot_seq"]],
                    },
                )
        altered.append(event)
    if corruption == "foreign-snapshot":
        with pytest.raises(ValueError, match="evaluation-evidence-mismatch"):
            dispatched_evidence(tuple(altered), effects, selected("m-direct").data, **kwargs)
    else:
        assert not dispatched_evidence(tuple(altered), effects, selected("m-direct").data, **kwargs)
