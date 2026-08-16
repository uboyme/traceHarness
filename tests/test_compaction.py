from __future__ import annotations

import pytest

from traceh.api.llm import ModelResponse
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.runtime.request_builder import verify_request_snapshots


@pytest.mark.asyncio
async def test_manual_compaction_changes_surface_without_deleting_history(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = ScriptedLlmProvider(
        (ModelResponse(content="first answer"), ModelResponse(content="second answer"))
    )
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data", provider="scripted", model="demo"),
        provider=provider,
    )
    first = await runtime.run(workspace, "first question")
    original_events = await runtime.sessions.read_session(first.session_id)
    assistant = next(event for event in original_events if event.type == "assistant/message")

    report = await runtime.compaction.replace_through(
        first.session_id,
        through_seq=assistant.seq,
        summary="The user asked the first question and the assistant answered it.",
    )
    surface = runtime.surface.project(await runtime.sessions.read_session(first.session_id))
    assert surface[0].content.startswith("<compacted-summary>")
    assert all(message.content != "first question" for message in surface)
    assert any(event.type == "user/message" for event in original_events)
    assert report.source_seqs

    second_compaction = await runtime.compaction.replace_through(
        first.session_id,
        through_seq=report.replacement_seq,
        summary="A shorter replacement summary.",
    )
    surface = runtime.surface.project(await runtime.sessions.read_session(first.session_id))
    summaries = [message for message in surface if "<compacted-summary>" in message.content]
    assert len(summaries) == 1
    assert "A shorter replacement summary" in summaries[0].content
    assert report.replacement_seq in second_compaction.source_seqs

    await runtime.run_existing(first.session_id, "second question")
    assert not await verify_request_snapshots(runtime.sessions, runtime.surface, first.session_id)
    assert not await runtime.check_invariants(first.session_id)
    await runtime.dispose()
