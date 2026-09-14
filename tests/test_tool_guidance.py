"""Actual requests use the offered tools without advertising absent capabilities."""

import pytest

from traceh.api.llm import ModelResponse, ToolCall
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.event_store import InMemoryEventStore
from traceh.tools.builtins.read_file import ReadFileTool


@pytest.mark.asyncio
@pytest.mark.parametrize("offered", ["none", "read", "default"])
async def test_current_navigation_does_not_advertise_unlisted_tools(tmp_path, offered):
    (tmp_path / "note.txt").write_text("independent source", encoding="utf-8")
    replies = (
        []
        if offered == "none"
        else [
            ModelResponse(tool_calls=(ToolCall("read", "read_file", {"path": "note.txt"}),)),
        ]
    )
    provider = ScriptedLlmProvider((*replies, ModelResponse(content="Finished.")))
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data"),
        provider=provider,
        event_store=InMemoryEventStore(),
        include_default_tools=offered == "default",
        additional_tools=(ReadFileTool(),) if offered == "read" else (),
    )
    try:
        result = await runtime.run(tmp_path, "Inspect available evidence.")
        for request in provider.requests:
            navigation = request.messages[-1].content
            assert "Use only the tools listed in the current request" in navigation
            for name in (
                "search_skill",
                "search_memory",
                "search_history",
                "list_tool_outputs",
                "search_tool_output",
                "read_tool_output",
            ):
                assert name not in navigation
            if offered != "default":
                assert {t.name for t in request.tools} == (
                    {"read_file"} if offered == "read" else set()
                )
        events = await runtime.sessions.read_session(result.session_id)
        reads = [e for e in events if e.type == "tool/result"]
        assert len(reads) == (0 if offered == "none" else 1)
        assert all(e.data["status"] == "succeeded" for e in reads)
        assert not await verify_request_snapshots(
            runtime.sessions, runtime.surface, result.session_id
        )
        assert not await runtime.check_invariants(result.session_id)
    finally:
        await runtime.dispose()
