"""RE-3 bounded literal union and public ToolRuntime checks, all offline."""

import asyncio
import json

import pytest
from test_retained_tool_output import raw_batch

from traceh.api.llm import ToolCall
from traceh.session.tool_output import render_output_search


def page(text, queries, **overrides):
    options = dict(
        effect_id="fixture",
        digest="d" * 64,
        queries=queries,
        part="content",
        cursor=None,
        count=10,
        context_lines=0,
        case_sensitive=True,
        max_chars=1600,
    )
    options.update(overrides)
    result = render_output_search({"content": text, "data": {}, "evidence": []}, **options)
    assert len(result) <= options["max_chars"]
    return json.loads(result)


def test_multiple_literals_deduplicate_position_and_keep_case_behavior():
    result = page("故障代号=X8\nother word\n", ["故障", "故障代号", "故障"])
    assert len(result["matches"]) == 1
    assert result["matches"][0]["matched_queries"] == ["故障", "故障代号"]
    assert "X8" in result["matches"][0]["matched_lines"]
    assert not page("WORD", ["word"])["matches"]
    assert page("WORD", ["word"], case_sensitive=False)["matches"]


@pytest.mark.parametrize("queries", [[], [""], "word", ["a"] * 5, ["汉" * 171], [None]])
def test_invalid_or_aggregate_oversize_queries_rejected(queries):
    with pytest.raises(ValueError, match="tool-output-query-invalid"):
        page("body", queries)


def test_cursor_is_bound_to_full_query_options_and_scan_boundary_does_not_skip():
    text = "-" * 199997 + "needle" + " tail"
    first = page(text, ["needle", "other"])
    assert not first["matches"] and first["next_cursor"]
    assert first["scan_window"]["end"] - first["scan_window"]["start"] == 200000
    second = page(text, ["needle", "other"], cursor=first["next_cursor"])
    assert second["matches"][0]["match_start"] == 199997
    for options in [{"queries": ["other", "needle"]}, {"part": "data"}, {"case_sensitive": False}]:
        args = {"queries": ["needle", "other"], **options}
        with pytest.raises(ValueError, match="tool-output-cursor-invalid"):
            page(text, cursor=first["next_cursor"], **args)


def test_match_pagination_is_one_shared_limit_not_per_query():
    text = "alpha beta gamma\n" * 20
    cursor = None
    positions = []
    while True:
        result = page(text, ["alpha", "beta", "gamma"], count=2, cursor=cursor)
        assert len(result["matches"]) <= 2
        positions.extend(m["match_start"] for m in result["matches"])
        cursor = result["next_cursor"]
        if cursor is None:
            break
    assert len(positions) == len(set(positions)) == 60


@pytest.mark.parametrize("failure", [None, "digest", "cancel"])
async def test_real_tool_path_reads_once_rejects_identity_and_converges(tmp_path, failure):
    runtime, sessions, store, context, call = await raw_batch(tmp_path)
    release = asyncio.Event()
    try:
        (original,) = await runtime.execute_batch(
            (call,), context=context, composition_revision="r"
        )
        args = {k: original.output_ref[k] for k in ["effect_id", "digest"]}
        args["queries"] = ["station-Q", "code-9472"]
        if failure == "digest":
            args["digest"] = "0" * 64
        if failure == "cancel":
            entered = asyncio.Event()
            read = sessions.read_effects

            async def gated(sid):
                result = await read(sid)
                entered.set()
                await release.wait()
                return result

            sessions.read_effects = gated
        task = asyncio.create_task(
            runtime.execute_batch(
                (ToolCall("lookup", "search_tool_output", args),),
                context=context,
                composition_revision="r",
            )
        )
        if failure == "cancel":
            await asyncio.wait_for(entered.wait(), 10)
            task.cancel()
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            events = await sessions.read_session(context.session_id)
            assert any(e.type == "tool/result" and e.data["status"] == "cancelled" for e in events)
        else:
            (result,) = await task
            if failure:
                assert result.status == "failed" and "code-9472" not in result.content
            else:
                assert result.status == "succeeded"
                assert "code-9472" in result.content
        assert (context.workspace / "executions.txt").read_text() == "x"
    finally:
        release.set()
        await store.aclose()


async def test_public_cursor_cannot_resume_a_different_query(tmp_path):
    runtime, sessions, store, context, call = await raw_batch(tmp_path)
    try:
        (original,) = await runtime.execute_batch(
            (call,), context=context, composition_revision="r"
        )
        args = {k: original.output_ref[k] for k in ["effect_id", "digest"]}
        args.update(queries=["quoted"], count=1)
        (first,) = await runtime.execute_batch(
            (ToolCall("first", "search_tool_output", args),),
            context=context,
            composition_revision="r",
        )
        assert first.status == "succeeded"
        cursor = json.loads(first.content)["next_cursor"]
        assert cursor is not None
        args.update(queries=["测量"], cursor=cursor)
        (changed,) = await runtime.execute_batch(
            (ToolCall("changed", "search_tool_output", args),),
            context=context,
            composition_revision="r",
        )
        assert changed.status == "failed", "A cursor from another query must be rejected"
        assert (context.workspace / "executions.txt").read_text() == "x"
    finally:
        await store.aclose()
