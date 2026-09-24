"""A response the model never finished must not become a completed Turn.

The pilot that motivated this ended with ``finish_reason=length``, empty
content and no tool calls, and was still recorded as ``completed`` - so the
Turn reported success while the Agent had delivered nothing.  These tests fix
the three things that made that possible: the ending must be classified, an
unfinished response must not run its tool calls, and it must not end the Turn
well.
"""

from __future__ import annotations

import pytest

from traceh.api.llm import CompletionCategory, ModelResponse, ToolCall
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.runtime.continuation import Finish
from traceh.runtime.response_completeness import (
    COMPLETION_UNKNOWN,
    REFUSED,
    TOOL_HANDOFF_WITHOUT_CALLS,
    TRUNCATED,
    judge_response,
)
from traceh.session.event_store import InMemoryEventStore


async def run_one_turn(tmp_path, *responses: ModelResponse):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "target.txt").write_text("original\n", encoding="utf-8")
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data"),
        provider=ScriptedLlmProvider(responses, repeat_last=True),
        event_store=InMemoryEventStore(),
    )
    session_id = await runtime.create_session(workspace)
    try:
        result = await runtime.run_existing(session_id, "do the task")
        events = await runtime.sessions.read_session(session_id)
    finally:
        await runtime.dispose()
    return result, events, workspace


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (ModelResponse(content="", completion=CompletionCategory.LENGTH), TRUNCATED),
        (ModelResponse(content="partial", completion=CompletionCategory.LENGTH), TRUNCATED),
        (ModelResponse(content="no", completion=CompletionCategory.REFUSAL), REFUSED),
        (ModelResponse(content="?", completion=CompletionCategory.UNKNOWN), COMPLETION_UNKNOWN),
        (
            ModelResponse(content="", completion=CompletionCategory.TOOL_HANDOFF),
            TOOL_HANDOFF_WITHOUT_CALLS,
        ),
    ],
    ids=("truncated-empty", "truncated-partial", "refused", "unknown", "handoff-without-calls"),
)
def test_unfinished_endings_are_not_complete(response: ModelResponse, expected: str) -> None:
    verdict = judge_response(response)
    assert verdict.complete is False
    assert verdict.may_execute_tools is False
    assert verdict.reason == expected


@pytest.mark.parametrize(
    "response",
    [
        ModelResponse(content="here is the answer", completion=CompletionCategory.NORMAL),
        ModelResponse(
            content="",
            tool_calls=(ToolCall("c1", "read_file", {"path": "target.txt"}),),
            completion=CompletionCategory.TOOL_HANDOFF,
        ),
    ],
    ids=("normal-answer", "tool-handoff-with-calls-and-no-text"),
)
def test_finished_endings_stay_complete(response: ModelResponse) -> None:
    """A tool step legitimately carries no prose; that is not an empty delivery."""

    verdict = judge_response(response)
    assert verdict.complete is True
    assert verdict.may_execute_tools is True
    assert verdict.reason == ""


@pytest.mark.asyncio
async def test_truncated_empty_response_ends_the_turn_unsuccessfully(tmp_path) -> None:
    """The exact pilot signature: length, empty body, no calls, previously completed."""

    result, events, _ = await run_one_turn(
        tmp_path,
        ModelResponse(content="", completion=CompletionCategory.LENGTH),
    )

    assert result.reason == TRUNCATED
    turn_end = next(e for e in events if e.type == "turn/end")
    assert turn_end.data["reason"] == TRUNCATED
    # The response and its usage are still recorded faithfully; what changed is
    # the verdict about it, not the evidence.
    attempt_end = next(e for e in events if e.type == "model/attempt-end")
    assert attempt_end.data["status"] == "succeeded"
    assert attempt_end.data["completion"] == CompletionCategory.LENGTH.value


@pytest.mark.asyncio
async def test_truncated_response_does_not_execute_its_tool_calls(tmp_path) -> None:
    """A cut-off response's tool calls are a fragment, not an authorisation."""

    result, events, workspace = await run_one_turn(
        tmp_path,
        ModelResponse(
            content="I will just",
            tool_calls=(
                ToolCall(
                    "truncated-write",
                    "write_file",
                    {"path": "target.txt", "content": "overwritten by a partial intent\n"},
                ),
            ),
            completion=CompletionCategory.LENGTH,
        ),
    )

    assert result.reason == TRUNCATED
    # The external side effect is the thing that must not have happened.
    assert (workspace / "target.txt").read_text(encoding="utf-8") == "original\n"
    assert not [e for e in events if e.type == "tool/call"]
    assert not [e for e in events if e.type == "tool/result"]


@pytest.mark.asyncio
async def test_a_finished_answer_still_completes(tmp_path) -> None:
    """The gate must not turn ordinary success into a failure."""

    result, events, _ = await run_one_turn(
        tmp_path,
        ModelResponse(content="the answer", completion=CompletionCategory.NORMAL),
    )

    assert result.reason == "completed"
    assert next(e for e in events if e.type == "turn/end").data["reason"] == "completed"


@pytest.mark.asyncio
async def test_a_finished_tool_step_still_runs_its_tools(tmp_path) -> None:
    """Tools behind the gate must still run on a complete hand-off."""

    result, events, _ = await run_one_turn(
        tmp_path,
        ModelResponse(
            content="",
            tool_calls=(ToolCall("c1", "read_file", {"path": "target.txt"}),),
            completion=CompletionCategory.TOOL_HANDOFF,
        ),
        ModelResponse(content="I read it", completion=CompletionCategory.NORMAL),
    )

    results = [e for e in events if e.type == "tool/result"]
    assert results and results[0].data["status"] == "succeeded"
    assert result.reason == "completed"


def test_finish_reason_reports_the_ending_that_actually_happened() -> None:
    """Each unfinished ending keeps its own code rather than one generic failure."""

    reasons = {
        judge_response(ModelResponse(completion=category)).reason
        for category in (
            CompletionCategory.LENGTH,
            CompletionCategory.REFUSAL,
            CompletionCategory.UNKNOWN,
        )
    }
    assert reasons == {TRUNCATED, REFUSED, COMPLETION_UNKNOWN}
    assert Finish(TRUNCATED).reason == TRUNCATED
