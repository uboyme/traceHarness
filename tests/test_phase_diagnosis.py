from __future__ import annotations

import asyncio
import json
import sqlite3
import zipfile
from dataclasses import replace

import pytest
from live_dynamic_collaboration.phase_diagnosis import (
    CONDITIONS,
    DECISION_TOOL,
    assess,
    prepare,
    probe,
    run,
    transform,
)

from traceh.api.json_types import fingerprint
from traceh.api.llm import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCall,
    ToolSchema,
    Usage,
    UsageQuality,
)
from traceh.llm.openai_compatible import OpenAICompatibleProvider

SCOUT = "Inspect the supplied materials in this temporary phase."
DECIDE = "This step must select an execution shape."


def request():
    props = {
        k: {"type": "string", "maxLength": 100}
        for k in ("main_goal", "child_goal", "child_deliverable", "briefing")
    }
    props.update(
        decision={"type": "string", "enum": ["local", "separable"]},
        independent_readonly={"type": "boolean"},
    )
    return ModelRequest(
        "openai-compatible",
        "test-model",
        (
            ModelMessage("user", "Analyze these supplied materials."),
            ModelMessage("user", SCOUT),
            ModelMessage(
                "assistant",
                "Inspecting",
                tool_calls=(ToolCall("call-a", "read_file", {"path": "notes.txt"}),),
            ),
            ModelMessage(
                "tool", "Source evidence remains verbatim.", tool_call_id="call-a", name="read_file"
            ),
            ModelMessage("user", "Current references and original goal."),
        ),
        tools=(
            ToolSchema(
                DECISION_TOOL,
                "Choose a shape",
                {
                    "type": "object",
                    "properties": props,
                    "required": list(props),
                    "additionalProperties": False,
                },
            ),
        ),
        system_prompt="General instructions\n\n" + DECIDE,
        temperature=0,
        max_output_tokens=1000,
    )


@pytest.mark.parametrize("condition", CONDITIONS)
def test_transforms_keep_evidence_atomic_and_only_change_declared_controls(condition):
    original = request()
    before = original.to_dict()
    changed = transform(original, condition, SCOUT, DECIDE)
    assert original.to_dict() == before
    old = [m for m in original.messages if m.role in {"assistant", "tool"}]
    assert [m for m in changed.messages if m.role in {"assistant", "tool"}] == old
    assert changed.tools == original.tools and changed.system_prompt == original.system_prompt
    assert changed.messages[0] == original.messages[0]
    assert original.messages[-1] in changed.messages
    assert sum(m.content == SCOUT for m in changed.messages) == (
        condition in {"original", "phase-last"}
    )
    if condition in {"phase-last", "both"}:
        assert changed.messages[-1] == ModelMessage("system", DECIDE)
    if condition == "original":
        assert changed.to_dict() == before


@pytest.mark.parametrize("fault", ["missing", "duplicate", "wrong-tool", "wrong-prompt"])
def test_rejects_ambiguous_or_wrong_input(fault):
    value = request()
    if fault == "missing":
        value = replace(value, messages=tuple(m for m in value.messages if m.content != SCOUT))
    elif fault == "duplicate":
        value = replace(value, messages=(*value.messages, ModelMessage("user", SCOUT)))
    elif fault == "wrong-tool":
        value = replace(value, tools=())
    else:
        value = replace(value, system_prompt="Different phase")
    with pytest.raises(ValueError):
        transform(value, "both", SCOUT, DECIDE)


def local_call(**changes):
    args = dict(
        decision="local",
        main_goal="Read the supplied note",
        child_goal="",
        child_deliverable="",
        briefing="",
        independent_readonly=False,
    )
    args.update(changes)
    return ToolCall("next-call", DECISION_TOOL, args)


@pytest.mark.parametrize(
    "changes,valid",
    [
        ({}, True),
        ({"child_goal": "Unexpected work"}, False),
        ({"main_goal": " "}, False),
        ({"main_goal": "x" * 101}, False),
        (
            {
                "decision": "separable",
                "child_goal": "Independent review",
                "child_deliverable": "Findings",
                "briefing": "Use original sources",
                "independent_readonly": True,
            },
            True,
        ),
    ],
)
def test_proposal_contract_is_not_task_success(changes, valid):
    result = assess(request(), ModelResponse(tool_calls=(local_call(**changes),)))
    assert result["decision_valid"] == valid
    assert result["tool_executed"] is False


def test_hidden_calls_are_never_valid_decisions():
    result = assess(request(), ModelResponse(tool_calls=(ToolCall("x", "read_file", {}),)))
    assert not result["decision_valid"] and result["decision"] is None


@pytest.mark.asyncio
async def test_real_provider_serialization_keeps_phase_and_tool_pairs(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return json.dumps(
                {
                    "choices": [{"message": {"content": "observed"}}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1},
                }
            ).encode()

    def urlopen(req, timeout):
        captured.update(json.loads(req.data))
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    provider = OpenAICompatibleProvider(
        "https://example.invalid", api_key_env="TRACEH_TEST_UNUSED_KEY"
    )
    value = transform(request(), "both", SCOUT, DECIDE)
    await provider.complete(value)
    assert captured["messages"][-1] == {"role": "system", "content": DECIDE}
    assert captured["messages"][2]["tool_calls"][0]["id"] == captured["messages"][3]["tool_call_id"]
    assert [t["function"]["name"] for t in captured["tools"]] == [DECISION_TOOL]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [RuntimeError("do not persist raw errors"), asyncio.CancelledError()]
)
async def test_failure_and_cancellation_record_once_and_propagate(tmp_path, failure):
    count = 0
    path = tmp_path / "probe.json"

    class Provider:
        async def complete(self, value):
            nonlocal count
            count += 1
            assert json.loads(path.read_text())["status"] == "started"
            raise failure

    with pytest.raises(type(failure)):
        await probe(Provider(), request(), path)
    data = json.loads(path.read_text())
    assert count == 1 and data["status"] in {"failed", "cancelled"}
    assert "do not persist raw errors" not in path.read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("quality", [UsageQuality.EXACT, UsageQuality.UNKNOWN])
async def test_probe_records_real_response_without_executing_tools(tmp_path, quality):
    class Provider:
        async def complete(self, value):
            return ModelResponse(tool_calls=(local_call(),), usage=Usage(2, 1, quality))

    path = tmp_path / "result.json"
    if quality == UsageQuality.UNKNOWN:
        with pytest.raises(ValueError, match="unknown-usage-stop"):
            await probe(Provider(), request(), path)
    else:
        await probe(Provider(), request(), path)
    assert json.loads(path.read_text())["assessment"]["tool_executed"] is False


def frozen_material(tmp_path, *, bad_digest=False):
    from live_dynamic_collaboration.phase_diagnosis import CASES

    root = tmp_path / "historical"
    archive = tmp_path / "candidate.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr(
            "src/traceh/product/decomposition.py",
            f"_SCOUT_PROMPT={SCOUT!r}\n_DECISION_PROMPT={DECIDE!r}\n",
        )
    for case in CASES:
        path = root / "runs" / case / "attempts/001/ev/events.sqlite3"
        path.parent.mkdir(parents=True)
        con = sqlite3.connect(path)
        try:
            con.execute("CREATE TABLE events (stream_id TEXT,seq INTEGER,envelope_json TEXT)")
            raw = request().to_dict()
            event = {
                "stream_id": "session:test",
                "seq": 1,
                "type": "request/snapshot",
                "data": {
                    "dispatch_request": raw,
                    "dispatch_fingerprint": "wrong" if bad_digest else fingerprint(raw),
                },
            }
            con.execute(
                "INSERT INTO events VALUES (?,?,?)", (event["stream_id"], 1, json.dumps(event))
            )
            con.commit()
        finally:
            con.close()
    return root, archive


def test_prepare_rejects_wrong_original_digest(tmp_path):
    root, archive = frozen_material(tmp_path, bad_digest=True)
    with pytest.raises(ValueError, match="original-request-fingerprint-mismatch"):
        prepare(root, archive, tmp_path / "out")
    assert not (tmp_path / "out").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_frozen_batch_call_cap_and_stop_after_failure(tmp_path, monkeypatch, fail):
    root, archive = frozen_material(tmp_path)
    out = tmp_path / "out"
    prepare(root, archive, out)
    count = 0

    class Provider:
        name = "openai-compatible"

        async def complete(self, value):
            nonlocal count
            count += 1
            if fail:
                raise RuntimeError("failure")
            return ModelResponse(tool_calls=(local_call(),), usage=Usage(2, 1, UsageQuality.EXACT))

    monkeypatch.setattr(
        "live_dynamic_collaboration.phase_diagnosis.connection",
        lambda p: (None, Provider(), "test-model"),
    )
    with pytest.raises(RuntimeError, match="historical-collaboration-contract"):
        await run(out, tmp_path / "unused")
    assert count == 0
    before = count
    with pytest.raises(FileExistsError):
        await run(out, tmp_path / "unused")
    assert count == before
