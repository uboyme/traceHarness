"""Controlled Reader diagnosis; scripted navigation is not an autonomous-model score."""

import json
from pathlib import Path

import pytest
from live_active_retrieval.grid import prepare_runtime, setup_source
from test_history_runtime import SelectingProvider, items

from traceh.api.llm import ModelResponse, ToolCall
from traceh.runtime.request_builder import verify_request_snapshots


@pytest.mark.parametrize("identity,query", [("re2-omitted", "呼号"), ("re4-memory-yes", "凭证")])
async def test_fewer_hits_restore_context_and_original_reader_reaches_target(
    tmp_path, identity, query
):
    frozen = json.loads((Path(__file__).parent / "frozen.json").read_text(encoding="utf-8"))
    fixture = next(f for f in frozen["fixtures"] if f["identity"] == identity)
    pages = []

    def advance(request):
        references = items(request)
        if any(i["tier"] == "section" and fixture["value"] in i["body"] for i in references):
            return ModelResponse(content=fixture["value"])
        page = json.loads(next(i["body"] for i in references if i["tier"] == "search"))
        pages.append(page)
        for hit in page["hits"]:
            if hit["reference"]["id"] == fixture["memory_id"]:
                action = hit["read_action"]
                return ModelResponse(
                    tool_calls=(ToolCall("read-target", action["tool_name"], action["arguments"]),)
                )
        if len(pages) == 2:
            # Oracle-assisted diagnosis only: this is never used by the live model.
            locator = fixture["source"].split(fixture["value"], 1)[0].strip()
            return ModelResponse(
                tool_calls=(ToolCall("diagnostic-locator", "search_memory", {"query": locator}),)
            )
        assert page["next_cursor"] is not None
        return ModelResponse(
            tool_calls=(
                ToolCall(
                    "next-" + str(len(pages)),
                    "search_memory",
                    {"query": query, "cursor": page["next_cursor"]},
                ),
            )
        )

    provider = SelectingProvider(
        [
            ModelResponse(tool_calls=(ToolCall("first", "search_memory", {"query": query}),)),
            *([advance] * 11),
        ]
    )
    runtime, store, session, scope, value = await prepare_runtime(
        tmp_path, fixture, frozen, provider, "controlled-reader-diagnosis"
    )
    try:
        await setup_source(runtime, session, scope, value, fixture, frozen["manifest"])
        result = await runtime.run_existing(session, fixture["question"])
        assert result.steps <= frozen["manifest"]["limits"]["max_steps"]
        assert fixture["value"] in provider.requests[-1].messages[-1].content
        assert pages[0]["total"] == len(fixture["memory_noise"]) + 1
        assert pages[0]["next_cursor"] is not None
        assert all(len(hit["text"]) > len(query) for hit in pages[0]["hits"])
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, session)
        assert not await runtime.check_invariants(session)
        print(
            json.dumps(
                {
                    "identity": identity,
                    "search_pages": len(pages),
                    "first_texts": [h["text"] for h in pages[0]["hits"]],
                    "first_scanned": pages[0]["scanned"],
                    "steps": result.steps,
                },
                ensure_ascii=False,
            )
        )
    finally:
        await runtime.dispose()
        await store.aclose()
