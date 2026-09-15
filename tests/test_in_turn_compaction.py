"""E1 follows the actual request/Tool/summary/disclosure owners."""

import asyncio
import json
from dataclasses import replace

import pytest

pytest.importorskip("tiktoken")

from test_history_runtime import items, policy, select_page
from test_semantic_summary import SummaryProvider, config, history

from traceh.api.llm import ModelResponse, ToolCall
from traceh.api.tools import EffectKind, ToolOutput
from traceh.llm.token_meter import TokenBudgetPolicy
from traceh.runtime.agent_runtime import build_default_runtime
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.sqlite import SqliteEventStore


async def test_summary_then_tool_history_read_is_admitted_in_same_turn(tmp_path):
    sid, _ = await history(tmp_path)

    class ReadingProvider(SummaryProvider):
        async def complete(self, request):
            if "summary_input_seq" in request.metadata:
                return await super().complete(request)
            self.requests.append(request)
            blocks = items(request)
            if len(self.requests) == 2:
                assert blocks and blocks[0]["tier"] == "directory"
                return select_page(request)
            return ModelResponse(content="Read original evidence after summary.")

    provider = ReadingProvider()
    store = SqliteEventStore(tmp_path / "events")
    runtime = build_default_runtime(
        config(tmp_path, context_input=policy(), max_steps=4), provider=provider, event_store=store
    )
    try:
        result = await runtime.run_existing(sid, "Find the original requirements.")
        assert result.final_text == "Read original evidence after summary."
        blocks = items(provider.requests[-1])
        assert blocks and blocks[0]["tier"] == "chunk", json.dumps(blocks)
        assert "offline parser" in blocks[0]["body"]
        assert result.steps == 3
        events = await runtime.sessions.read_session(sid)
        assert sum(e.type == "summary/input" for e in events) == 1
        assert sum(e.type == "tool/result" for e in events) == 1
        assert not await runtime.check_invariants(sid)
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, sid)
    finally:
        await runtime.dispose()
        await store.aclose()


@pytest.mark.parametrize(
    "semantic,invalid_summary,max_steps,cancel_summary",
    [
        (False, False, 4, False),
        (True, False, 4, False),
        (True, True, 4, False),
        (True, False, 2, False),
        (False, False, 2, False),
        (True, False, 4, True),
    ],
)
async def test_tool_pressure_compacts_only_old_turns_without_reexecution(
    tmp_path, semantic, invalid_summary, max_steps, cancel_summary
):
    sid, before = await history(tmp_path)
    entered, converged = asyncio.Event(), asyncio.Event()

    # 1800 units, not 1400: the reference guidance is no longer assembled for a
    # composition that has none of the reference tools, which took ~1,091 tokens
    # out of this prompt. The pressure this case is about must come from the tool
    # output itself rather than from prompt text that a real run would not carry.
    EMIT_UNITS = 1800

    class Emit:
        name = "emit_records"
        description = "Emit records once and record its execution in the workspace."
        input_schema = {"type": "object", "properties": {}, "additionalProperties": False}
        effect_kind = EffectKind.WORKSPACE_WRITE

        async def execute(self, arguments, context):
            with (context.workspace / "executions.txt").open("a") as output:
                output.write("executed\n")
            return ToolOutput(content="result unit. " * EMIT_UNITS)

    class Provider(SummaryProvider):
        async def complete(self, request):
            if "summary_input_seq" in request.metadata:
                if cancel_summary:
                    self.requests.append(request)
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        converged.set()
                response = await super().complete(request)
                return replace(response, content="invalid summary") if invalid_summary else response
            self.requests.append(request)
            if len(self.requests) == 1:
                return ModelResponse(tool_calls=(ToolCall("emit-once", Emit.name, {}),))
            return ModelResponse(content="Records explained.")

    provider = Provider()
    settings = replace(
        config(tmp_path, max_steps=max_steps),
        semantic_summary=semantic,
        token_budget=TokenBudgetPolicy("cl100k_base", 11_000, 2048, 1024),
    )
    store = SqliteEventStore(tmp_path / "events")
    runtime = build_default_runtime(
        settings,
        provider=provider,
        event_store=store,
        additional_tools=(Emit(),),
        include_default_tools=False,
    )
    try:
        if cancel_summary:
            task = asyncio.create_task(runtime.run_existing(sid, "Emit records and explain them."))
            await asyncio.wait_for(entered.wait(), 10)
            task.cancel()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert converged.is_set()
            messages = runtime.surface.project(await runtime.sessions.read_session(sid))
        else:
            result = await runtime.run_existing(sid, "Emit records and explain them.")
            assert result.final_text == "Records explained."
            messages = provider.requests[-1].messages
        calls = [call for m in messages for call in m.tool_calls]
        results = [m for m in messages if m.role == "tool"]
        assert [call.id for call in calls] == ["emit-once"]
        assert len(results) == 1 and results[0].tool_call_id == "emit-once"
        assert results[0].content == "result unit. " * EMIT_UNITS
        assert any(m.content == "Keep this recent question intact." for m in messages)
        assert any(m.content == "Emit records and explain them." for m in messages)
        assert (tmp_path / "executions.txt").read_text() == "executed\n"
        events = await runtime.sessions.read_session(sid)
        assert events[: len(before)] == before
        current = events[len(before) :]
        measures = [e.data["measurement"] for e in current if e.type == "request/token-measurement"]
        assert measures[0]["input_tokens"] < measures[0]["trigger_tokens"], measures
        summaries = [e for e in current if e.type == "summary/input"]
        summary_allowed = semantic and max_steps > 2
        assert len(summaries) == int(summary_allowed)
        replacements = [e for e in current if e.type == "surface/replace"]
        if cancel_summary:
            assert not replacements
            assert any(e.type == "turn/end" and e.data["reason"] == "cancelled" for e in current)
        elif semantic and not summary_allowed:
            assert not replacements
        elif invalid_summary:
            assert not replacements
            assert any(e.type == "surface/compaction-failed" for e in current)
        else:
            assert len(replacements) == 1
            tool_result = next(e for e in current if e.type == "tool/result")
            assert replacements[0].seq > tool_result.seq
            assert all(seq <= before[-1].seq for seq in replacements[0].data["source_seqs"])
        assert not await runtime.check_invariants(sid)
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, sid)
    finally:
        await runtime.dispose()
        await store.aclose()
