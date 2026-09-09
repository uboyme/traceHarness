"""Experimental presentation contract on the existing public ToolRuntime path."""

import json

from test_retained_tool_output import raw_batch

from traceh.session.invariants import CoreInvariantChecker
from traceh.session.tool_output import resolve_tool_output


async def test_read_action_precedes_storage_and_preserves_original_identity(tmp_path):
    runtime, sessions, store, context, call = await raw_batch(tmp_path)
    try:
        (result,) = await runtime.execute_batch((call,), context=context, composition_revision="r")
        assert result.status == "succeeded"
        display = json.loads(result.content)
        assert "source_content" in display, "Original-content action must be independently visible"
        assert display["source_content"]["loaded"] is False
        assert display["storage_navigation"]["output_ref"] == result.output_ref
        args = display["source_content"]["read_action"]["arguments"]
        assert args["digest"] == result.output_ref["digest"]
        events = await sessions.read_session(context.session_id)
        effects = await sessions.read_effects(context.session_id)
        source = resolve_tool_output(events, effects, session_id=context.session_id,
                                     effect_id=args["effect_id"], digest=args["digest"])
        assert "code-9472" in source["content"]
        assert "code-9472" not in result.content
        assert not CoreInvariantChecker().check(events, effects)
        assert (context.workspace / "executions.txt").read_text() == "x"
    finally:
        await store.aclose()
