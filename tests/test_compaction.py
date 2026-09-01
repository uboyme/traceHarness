from __future__ import annotations

from uuid import uuid4

import pytest

from traceh.api.llm import ModelResponse
from traceh.api.product import ProductTaskStatus
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.event_store import InMemoryEventStore
from traceh.session.product_context import (
    PRODUCT_CONTEXT_SNAPSHOT,
    product_context_snapshot_data,
)


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
        event_store=InMemoryEventStore(),
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


@pytest.mark.asyncio
async def test_manual_compaction_preserves_product_status_evidence(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = ScriptedLlmProvider(
        (ModelResponse(content="first answer"), ModelResponse(content="second answer"))
    )
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data", provider="scripted", model="demo"),
        provider=provider,
        event_store=InMemoryEventStore(),
    )
    first = await runtime.run(workspace, "first question")
    events = await runtime.sessions.read_session(first.session_id)
    source_event_id = uuid4()
    context_event = await runtime.sessions.append_session(
        first.session_id,
        PRODUCT_CONTEXT_SNAPSHOT,
        product_context_snapshot_data(
            session_id=first.session_id,
            task_id="task-compaction",
            source_stream_id="product-task:task-compaction",
            source_seq=1,
            task_order_seq=events[-1].seq,
            status=ProductTaskStatus.COMPLETED,
            source_event_id=source_event_id,
        ),
        causation_id=source_event_id,
    )

    report = await runtime.compaction.replace_through(
        first.session_id,
        through_seq=context_event.seq,
        summary="The earlier conversation was compacted.",
    )
    assert context_event.seq not in report.source_seqs
    surface = runtime.surface.project(await runtime.sessions.read_session(first.session_id))
    assert surface[0].role == "system"
    assert "- Status: completed" in surface[0].content
    assert any("<compacted-summary>" in message.content for message in surface)
    assert sum("- Status: completed" in message.content for message in surface) == 1

    second_compaction = await runtime.compaction.replace_through(
        first.session_id,
        through_seq=report.replacement_seq,
        summary="The conversation was compacted again.",
    )
    assert context_event.seq not in second_compaction.source_seqs
    surface = runtime.surface.project(await runtime.sessions.read_session(first.session_id))
    assert surface[0].role == "system"
    assert "- Status: completed" in surface[0].content
    assert sum("- Status: completed" in message.content for message in surface) == 1

    await runtime.run_existing(first.session_id, "second question")
    assert not await verify_request_snapshots(runtime.sessions, runtime.surface, first.session_id)
    assert not await runtime.check_invariants(first.session_id)
    await runtime.dispose()
