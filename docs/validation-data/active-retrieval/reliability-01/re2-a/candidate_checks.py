"""Memory navigation is a bounded presentation, not a new source or permission."""

import json
from pathlib import Path

import pytest
from live_active_retrieval.grid import prepare_runtime, setup_source

from traceh.api.llm import ModelResponse
from traceh.llm.scripted import ScriptedLlmProvider


@pytest.mark.parametrize("identity", ["re2-omitted", "re2-unbound"])
async def test_memory_navigation_uses_selected_view_and_keeps_unknown_totals(tmp_path, identity):
    root = Path(__file__).parent
    frozen = json.loads((root / "frozen.json").read_text(encoding="utf-8"))
    fixture = next(f for f in frozen["fixtures"] if f["identity"] == identity)
    provider = ScriptedLlmProvider((ModelResponse(content="done"),))
    runtime, store, session, scope, value = await prepare_runtime(
        tmp_path, fixture, frozen, provider, "offline-fixture"
    )
    try:
        await setup_source(runtime, session, scope, value, fixture, frozen["manifest"])
        await runtime.run_existing(session, fixture["question"])
        events = await runtime.sessions.read_session(session)
        request = next(e.data["dispatch_request"] for e in events if e.type == "request/snapshot")
        wrapper = request["messages"][-1]["content"]
        items = json.loads(wrapper.split("\n")[1])
        assert items and items[0]["kind"] == "source-navigation", "Source search must precede selected entries"
        nav = items[0]
        assert nav["selection"]["total_eligible_facts"] is None
        assert nav["selection"]["displayed_entries"] == sum(i["kind"] == "memory" for i in items[1:])
        if identity == "re2-unbound":
            assert nav["selection"]["displayed_entries"] == 0
        assert "does not establish binding" in nav["availability"]
        context = next(e for e in events if e.type == "context/input")
        assert all(b["kind"] != "source-navigation" for b in context.data["blocks"])
        assert not await runtime.check_invariants(session)
    finally:
        await runtime.dispose()
        await store.aclose()
