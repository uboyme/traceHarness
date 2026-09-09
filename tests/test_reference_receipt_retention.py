"""Large-output presentation must not erase the Session's next-Step control receipt."""

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from live_active_retrieval.contract import load_manifest
from live_active_retrieval.fixtures import materialize
from live_active_retrieval.grid import prepare_runtime, setup_source
from test_history_runtime import SelectingProvider, items

from traceh.api.llm import ModelResponse, ToolCall
from traceh.api.tools import EffectKind, ToolOutput
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.tool_output import output_reference, resolve_tool_output


@pytest.mark.parametrize(
    "case_id,query", [("h-direct", "口令"), ("m-direct", "人数"), ("s-direct", "签到")]
)
async def test_retained_search_and_read_receipts_still_disclose_next_step(tmp_path, case_id, query):
    manifest = deepcopy(
        load_manifest(Path(__file__).parent / "live_active_retrieval/manifest.json")
    )
    # Deliberately smaller than every control receipt; independent from the frozen real grid.
    manifest["limits"]["max_tool_output_chars"] = 64
    fixture = next(f for f in materialize(manifest) if f["id"] == case_id)

    def read(request):
        pages = [json.loads(b["body"]) for b in items(request) if b["tier"] == "search"]
        if not pages or not pages[0]["hits"]:
            return ModelResponse(content="search evidence missing")
        action = pages[0]["hits"][0]["read_action"]
        return ModelResponse(
            tool_calls=(ToolCall("read", action["tool_name"], action["arguments"]),)
        )

    def answer(request):
        visible = any(
            b["tier"] in {"section", "chunk"} and fixture["value"] in b["body"]
            for b in items(request)
        )
        return ModelResponse(content="body present" if visible else "body missing")

    provider = SelectingProvider(
        [ModelResponse(content="noted") for _ in fixture.get("history_turns", [])]
        + [
            ModelResponse(
                tool_calls=(ToolCall("search", "search_" + fixture["family"], {"query": query}),)
            ),
            read,
            answer,
        ]
    )
    runtime, store, session, scope, value = await prepare_runtime(
        tmp_path, fixture, {"manifest": manifest}, provider, "fixture-model"
    )
    try:
        await setup_source(runtime, session, scope, value, fixture, manifest)
        result = await runtime.run_existing(session, fixture["question"])
        assert result.final_text == "body present"
        events = await runtime.sessions.read_session(session)
        results = [e for e in events if e.type == "tool/result"]
        assert len(results) == 2 and all("output_ref" in e.data for e in results)
        assert "search_receipt" in results[0].data["data"]
        assert fixture["family"] + "_receipt" in results[1].data["data"]
        assert not await runtime.check_invariants(session)
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, session)
        # Retained control projection must still match the original Effect payload.
        effect = next(
            e for e in await runtime.sessions.read_effects(session) if e.type == "effect/outcome"
        )
        forged = deepcopy(effect.data)
        forged["data"]["search_receipt"]["request"]["query"] = "forged"
        with pytest.raises(ValueError, match="tool-output-control-data-mismatch"):
            output_reference(replace(effect, data=forged))
    finally:
        await runtime.dispose()
        await store.aclose()


async def test_ordinary_tool_receipt_shaped_data_is_not_preserved_as_control(tmp_path):
    from test_retained_tool_output import raw_batch

    runtime, sessions, store, context, _ = await raw_batch(tmp_path)
    data = {"search_receipt": {"text": "untrusted result" * 1000}}

    class OrdinaryTool:
        name = "ordinary_fixture"
        description = "Explicit fixture, not a host reference tool"
        effect_kind = EffectKind.PURE_READ
        input_schema = {"type": "object", "properties": {}, "additionalProperties": False}

        async def execute(self, arguments, context):
            return ToolOutput("short", data)

    runtime.registry.register(OrdinaryTool())
    try:
        (result,) = await runtime.execute_batch(
            (ToolCall("ordinary", "ordinary_fixture", {}),),
            context=context,
            composition_revision="r",
        )
        assert result.status == "succeeded" and result.data == {}
        original = resolve_tool_output(
            await sessions.read_session(context.session_id),
            await sessions.read_effects(context.session_id),
            session_id=context.session_id,
            effect_id=result.effect_id,
            digest=result.output_ref["digest"],
        )
        assert original["data"] == data
    finally:
        await store.aclose()
