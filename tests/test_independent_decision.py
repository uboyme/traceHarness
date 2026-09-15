from __future__ import annotations

import asyncio
import copy
import json
import sqlite3

import pytest
from live_dynamic_collaboration.independent_decision import (
    CONDITIONS,
    INPUT_NOTICE,
    evidence_input,
    prepare,
    run,
    transform,
)
from live_dynamic_collaboration.phase_diagnosis import CASES
from test_phase_diagnosis import DECIDE, frozen_material, local_call, request

from traceh.api.json_types import fingerprint
from traceh.api.llm import ModelResponse, Usage, UsageQuality


def original():
    value = request()
    source = {
        "stream_id": "session:sample",
        "seq": 9,
        "dispatch_fingerprint": fingerprint(value.to_dict()),
    }
    events = [
        {
            "stream_id": source["stream_id"],
            "seq": 2,
            "type": "tool/call",
            "data": {
                "step_id": "step-sample",
                "turn_id": "turn-sample",
                "tool_call_id": "call-a",
                "tool_name": "read_file",
                "arguments": {"path": "notes.txt"},
            },
        },
        {
            "stream_id": source["stream_id"],
            "seq": 3,
            "type": "tool/result",
            "data": {
                "step_id": "step-sample",
                "tool_call_id": "call-a",
                "tool_name": "read_file",
                "effect_id": "effect-sample",
                "status": "succeeded",
                "content": value.messages[3].content,
            },
        },
        {
            "stream_id": source["stream_id"],
            "seq": 9,
            "type": "request/snapshot",
            "data": {"turn_id": "turn-sample", "dispatch_request": value.to_dict()},
        },
    ]
    return value, source, events


@pytest.mark.parametrize("condition", CONDITIONS)
def test_original_or_independent_input_preserves_exact_goal_and_evidence(condition):
    value, source, events = original()
    before = copy.deepcopy(events)
    packet = evidence_input(value, source, events)
    changed = transform(value, condition, DECIDE, packet)
    assert events == before
    assert packet["target_work"] == value.messages[0].content
    assert packet["evidence"][0]["body"] == value.messages[3].content
    assert packet["evidence"][0]["source"] == {
        "stream_id": source["stream_id"],
        "seq": 3,
        "step_id": "step-sample",
        "tool_call_id": "call-a",
        "effect_id": "effect-sample",
    }
    assert changed.tools == value.tools
    assert (
        changed.temperature == value.temperature
        and changed.max_output_tokens == value.max_output_tokens
    )
    if condition == "original":
        assert changed.to_dict() == value.to_dict()
    else:
        assert len(changed.messages) == 1 and changed.messages[0].role == "user"
        parsed = json.loads(changed.messages[0].content[len(INPUT_NOTICE) + 1 :])
        assert parsed == packet
        assert not any(m.tool_calls or m.tool_call_id for m in changed.messages)
        assert changed.system_prompt == (
            DECIDE if condition == "evidence-focused-system" else value.system_prompt
        )


@pytest.mark.parametrize(
    "fault",
    ["stream", "future", "failed", "body", "step", "turn", "arguments", "duplicate", "fingerprint"],
)
def test_wrong_identity_or_non_successful_evidence_is_rejected(fault):
    value, source, events = original()
    if fault == "stream":
        events[1]["stream_id"] = "session:other"
    elif fault == "future":
        events[1]["seq"] = 10
    elif fault == "failed":
        events[1]["data"]["status"] = "failed"
    elif fault == "body":
        events[1]["data"]["content"] = "Different source"
    elif fault == "step":
        events[1]["data"]["step_id"] = "wrong-step"
    elif fault == "turn":
        events[0]["data"]["turn_id"] = "wrong-turn"
    elif fault == "arguments":
        events[0]["data"]["arguments"] = {"path": "different.txt"}
    elif fault == "duplicate":
        events.insert(1, copy.deepcopy(events[1]))
    else:
        source["dispatch_fingerprint"] = "wrong"
    with pytest.raises(ValueError):
        evidence_input(value, source, events)


def test_no_read_result_cannot_be_promoted_to_evidence():
    from dataclasses import replace

    value, source, events = original()
    value = replace(value, messages=(value.messages[0],))
    source["dispatch_fingerprint"] = fingerprint(value.to_dict())
    events[-1]["data"]["dispatch_request"] = value.to_dict()
    with pytest.raises(ValueError, match="read-evidence-required"):
        evidence_input(value, source, events)


def prepared(tmp_path):
    root, archive = frozen_material(tmp_path)
    value, source, events = original()
    sources, rows = [], []
    for case in CASES:
        database = root / "runs" / case / "attempts/001/ev/events.sqlite3"
        con = sqlite3.connect(database)
        try:
            con.execute("DELETE FROM events")
            for e in events:
                con.execute(
                    "INSERT INTO events VALUES (?,?,?)", (e["stream_id"], e["seq"], json.dumps(e))
                )
            con.commit()
        finally:
            con.close()
        from traceh.evaluation.inputs import digest_bytes

        s = {
            **source,
            "case": case,
            "database": str(database),
            "database_sha256": digest_bytes(database.read_bytes()),
        }
        sources.append(s)
        rows.append(
            {"case": case, "condition": "original", "source": s, "request": value.to_dict()}
        )
    from traceh.evaluation.inputs import digest_bytes

    prior = tmp_path / "prior.json"
    prior.write_text(
        json.dumps(
            {"sources": sources, "rows": rows, "archive_sha256": digest_bytes(archive.read_bytes())}
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out"
    prepare(prior, archive, out)
    return out


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled"])
async def test_batch_runs_nine_or_stops_and_never_retries(tmp_path, monkeypatch, outcome):
    out = prepared(tmp_path)
    count = 0

    class Provider:
        name = "openai-compatible"

        async def complete(self, value):
            nonlocal count
            count += 1
            if outcome == "failure":
                raise RuntimeError("failed")
            if outcome == "cancelled":
                raise asyncio.CancelledError()
            return ModelResponse(tool_calls=(local_call(),), usage=Usage(2, 1, UsageQuality.EXACT))

    monkeypatch.setattr(
        "live_dynamic_collaboration.independent_decision.connection",
        lambda p: (None, Provider(), "test-model"),
    )
    with pytest.raises(RuntimeError, match="historical-collaboration-contract"):
        await run(out, tmp_path / "unused")
    assert count == 0
    before = count
    with pytest.raises(FileExistsError):
        await run(out, tmp_path / "unused")
    assert count == before
