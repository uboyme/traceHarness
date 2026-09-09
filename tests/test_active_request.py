"""Current task presentation stays bound to original durable Turn input."""

import json

import pytest
from test_context_input import _fixture, _redigest
from test_history_runtime import SelectingProvider

from traceh.api.llm import ModelResponse, ToolCall
from traceh.kernel.composition import RuntimeComposition
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.context_input import (
    ContextInputError,
    ContextInputService,
    parse_context_input,
    render_context_message,
    validate_context_input_sources,
)
from traceh.session.event_store import InMemoryEventStore
from traceh.session.service import SessionService
from traceh.session.sqlite import SqliteEventStore


def active_text(request):
    tail = request.messages[-1].content
    encoded = tail.split("\n\nActive user request for this Turn (verbatim):\n")[1].split("\n")[0]
    return json.loads(encoded)


@pytest.mark.parametrize("forgery", ["content", "source"])
async def test_recomputed_anchor_digest_cannot_substitute_another_fact(tmp_path, forgery):
    sessions, kwargs = await _fixture(tmp_path, history=True)
    frozen = await ContextInputService(sessions.read_session).freeze(**kwargs)
    data = frozen.to_dict()
    events = await sessions.read_session(kwargs["session_id"])
    if forgery == "content":
        data["active_request"]["content"] = "CURRENT INPUT"  # Same byte count as original.
    else:
        from traceh.projects.events import reference

        other = next(e for e in events if e.type == "user/message")
        data["active_request"]["source_ref"] = reference(other)
    altered = parse_context_input(_redigest(data))
    with pytest.raises(ContextInputError, match="context-active-request-binding-mismatch"):
        await sessions.append_context_input(
            kwargs["session_id"], altered.to_dict(), expected_seq=events[-1].seq
        )
    assert await sessions.read_session(kwargs["session_id"]) == events
    validate_context_input_sources(frozen, events)
    written = await sessions.append_context_input(
        kwargs["session_id"], frozen.to_dict(), expected_seq=events[-1].seq
    )
    assert written.data["active_request"] == frozen.to_dict()["active_request"]


async def test_empty_context_still_requires_a_real_turn_input(tmp_path):
    sessions = SessionService(InMemoryEventStore())
    sid = await sessions.create_session(tmp_path)
    await sessions.append_session(sid, "turn/start", {"turn_id": "task"})
    await sessions.append_session(sid, "step/start", {"turn_id": "task", "step_id": "step"})
    composition = RuntimeComposition(
        provider="scripted", model="fixture", system_prompt="", tools=()
    ).snapshot()
    with pytest.raises(ContextInputError, match="context-active-request-source-unavailable"):
        await ContextInputService(sessions.read_session).freeze(
            session_id=sid, turn_id="task", step_id="step", composition=composition
        )


async def test_anchor_accounting_cannot_be_forged(tmp_path):
    sessions, kwargs = await _fixture(tmp_path)
    frozen = await ContextInputService(sessions.read_session).freeze(**kwargs)
    data = frozen.to_dict()
    budget = data["budget"]
    assert budget["rendered_bytes"] == len(render_context_message(frozen).content.encode("utf-8"))
    assert budget["rendered_bytes"] == budget["reference_bytes"] + budget["active_request_bytes"]
    assert budget["total_limit"] == budget["reference_limit"] + budget["active_request_bytes"]
    budget["active_request_bytes"] = 0
    with pytest.raises(ContextInputError, match="context-budget-mismatch"):
        parse_context_input(_redigest(data))


@pytest.mark.parametrize("end", ["success", "failure"])
async def test_public_runtime_keeps_task_during_tools_then_switches_and_replays(tmp_path, end):
    from traceh.llm.failures import ProviderFailure

    original = '校验 Ａa 与大小写\n"quoted" — no inferred task'
    first = ModelResponse(tool_calls=(ToolCall("inspect", "list_files", {}),))
    final = ModelResponse(content="finished") if end == "success" else RuntimeError("fixture")
    provider = SelectingProvider([first, final, ModelResponse(content="new topic")])
    store = SqliteEventStore(tmp_path / "events")
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data"), provider=provider, event_store=store
    )
    session = await runtime.create_session(tmp_path)
    try:
        if end == "success":
            await runtime.run_existing(session, original)
        else:
            with pytest.raises(ProviderFailure):
                await runtime.run_existing(session, original)
        await runtime.run_existing(session, "Explain tidal locking.")
        assert [active_text(r) for r in provider.requests] == [
            original,
            original,
            "Explain tidal locking.",
        ]
        assert provider.requests[1].messages[-2].role == "tool"
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, session)
        for event in await runtime.sessions.read_session(session):
            if event.type == "context/input":
                assert event.data["active_request"]["source_ref"]["type"] == "user/message"
    finally:
        await runtime.dispose()
        await store.aclose()
