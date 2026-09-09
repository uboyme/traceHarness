"""Compact navigation through the public Runtime and original reference authority."""

import json
from dataclasses import replace

import pytest
from test_context_input import _fixture, _policy
from test_history_runtime import SelectingProvider, items, policy
from test_memory_context import BODY, memory_case

from traceh.api.llm import ModelResponse, ToolCall
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.context_input import ContextInputService, render_context_message
from traceh.session.event_store import InMemoryEventStore


class ActionReader:
    name = "scripted"

    def __init__(self):
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            action = items(request)[0]["read_action"]
            return ModelResponse(
                tool_calls=(ToolCall("disclose", action["tool_name"], action["arguments"]),)
            )
        return ModelResponse(content="done")


@pytest.mark.parametrize("tools", [True, False])
async def test_memory_action_reads_only_when_the_original_tool_allows_it(tmp_path, tools):
    provider = ActionReader()
    async with memory_case(tmp_path, provider=provider, tier="directory", tools=tools) as (
        runtime,
        _,
        _,
        session,
        _,
        _,
    ):
        await runtime.memory.rebuild_index(session)
        await runtime.run_existing(session, "context-fact")
        assert len(provider.requests) == 2
        first, second = (items(r)[0] for r in provider.requests)
        assert first["body_status"] == "metadata-only"
        assert BODY not in first["body"]
        action = first["read_action"]
        assert action["arguments"]["memory_id"] == first["id"]
        assert action["arguments"]["version"] == first["version"]
        assert (BODY in second["body"]) is tools
        if tools:
            assert second["read_action"] is None
        events = await runtime.sessions.read_session(session)
        result = next(e for e in events if e.type == "tool/result")
        assert (result.data["status"] == "succeeded") is tools
        assert runtime.invariants.check(events) == ()
        assert await verify_request_snapshots(runtime.sessions, runtime.surface, session) == ()


async def test_history_action_copies_next_cursor_and_terminates(tmp_path):
    page_indexes = []

    def read_action(request):
        block = items(request)[0]
        notice = block
        action = notice["read_action"]
        assert action["arguments"]["block_id"] == block["id"]
        cursor = action["arguments"]["cursor"]
        assert cursor["index"] == len(page_indexes)
        page_indexes.append(cursor["index"])
        assert notice["more_pages_available"]
        return ModelResponse(
            tool_calls=(
                ToolCall(f"page-{cursor['index']}", action["tool_name"], action["arguments"]),
            )
        )

    provider = SelectingProvider(
        [
            ModelResponse(content="first"),
            ModelResponse(content="second"),
            read_action,
            read_action,
            ModelResponse(content="done"),
        ]
    )
    configured = policy()
    configured = replace(configured, history=replace(configured.history, page_messages=2))
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data", context_input=configured),
        provider=provider,
        event_store=InMemoryEventStore(),
    )
    try:
        first = await runtime.run(tmp_path, "early measurement")
        await runtime.run_existing(first.session_id, "later measurement")
        events = await runtime.sessions.read_session(first.session_id)
        await runtime.compaction.replace_through(
            first.session_id, through_seq=events[-1].seq, summary="Earlier measurements"
        )
        await runtime.run_existing(first.session_id, "Compare both earlier measurements")
        blocks = items(provider.requests[-1])
        assert any("early measurement" in b["body"] for b in blocks)
        assert any("later measurement" in b["body"] for b in blocks)
        notice = blocks[0]
        assert page_indexes == [0, 1]
        assert notice["read_action"] is None
        assert not notice["more_pages_available"]
        assert (
            await verify_request_snapshots(runtime.sessions, runtime.surface, first.session_id)
            == ()
        )
        assert runtime.invariants.check(await runtime.sessions.read_session(first.session_id)) == ()
    finally:
        await runtime.dispose()


async def test_derived_action_bytes_are_part_of_the_whole_block_budget(tmp_path):
    sessions, kwargs = await _fixture(tmp_path, history=True, summary="测量记录")
    configured = replace(_policy("directory"), history=policy().history)
    complete = await ContextInputService(sessions.read_session, configured).freeze(**kwargs)
    content = render_context_message(complete).content
    assert json.loads(content.split("\n")[1])[0]["read_action"]
    rendered_bytes = len(content.encode("utf-8"))
    assert complete.to_dict()["budget"]["rendered_bytes"] == rendered_bytes
    constrained = await ContextInputService(
        sessions.read_session,
        replace(configured, total_bytes=complete.to_dict()["budget"]["reference_bytes"] - 1),
    ).freeze(**kwargs)
    assert constrained.to_dict()["blocks"] == []
    assert json.loads(render_context_message(constrained).content.split("\n")[1]) == []
    assert len(render_context_message(constrained).content.encode("utf-8")) <= rendered_bytes - 1
