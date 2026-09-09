"""Exercise the candidate presentation through real Runtime request snapshots."""

import json
from pathlib import Path

import pytest
from live_active_retrieval.grid import prepare_runtime, setup_source

from traceh.api.llm import ModelResponse
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.surface import SurfaceProjector


@pytest.mark.parametrize("family", ["skill", "memory", "history"])
async def test_existing_scope_fields_precede_body_without_changing_evidence(tmp_path, family):
    frozen = json.loads((Path(__file__).parent / "frozen.json").read_text(encoding="utf-8"))
    fixture = next(f for f in frozen["fixtures"] if f["identity"] == f"re4-{family}-yes")
    provider = ScriptedLlmProvider(tuple(ModelResponse(content="recorded") for _ in range(40)))
    runtime, store, session, scope, value = await prepare_runtime(
        tmp_path, fixture, frozen, provider, "offline-fixture"
    )
    try:
        await setup_source(runtime, session, scope, value, fixture, frozen["manifest"])
        await runtime.run_existing(session, fixture["question"])
        events = await runtime.sessions.read_session(session)
        snapshots = [e for e in events if e.type == "request/snapshot"]
        wrapper = snapshots[-1].data["dispatch_request"]["messages"][-1]["content"]
        items = json.loads(wrapper.split("\n")[1])
        contexts = [e for e in events if e.type == "context/input"]
        blocks = contexts[-1].data["blocks"]
        assert items and len(items) == len(blocks)
        for item, block in zip(items, blocks, strict=True):
            assert list(item)[:3] == ["kind", "tier", "body_status"]
            assert list(item)[-1] == "body"
            assert item["body"] == block["body"]
            assert item["kind"] == block["kind"]
            assert item["tier"] == block["tier"]
        assert not await verify_request_snapshots(runtime.sessions, SurfaceProjector(), session)
        assert not await runtime.check_invariants(session)
    finally:
        await runtime.dispose()
        await store.aclose()
