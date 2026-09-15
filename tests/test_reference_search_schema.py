"""Model-visible search limits agree with the actual configured admission policy."""

import json
from contextlib import asynccontextmanager

import pytest
from retrieval_fixtures import build_case, context_policy, select
from test_history_runtime import SelectingProvider, items, policy
from test_memory_context import memory_case
from test_reference_search_history import setup

from traceh.api.llm import ModelResponse, ToolCall
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.reference_search import ReferenceSearchError, parse_request


@asynccontextmanager
async def case(tmp_path, family, provider):
    if family == "memory":
        async with memory_case(tmp_path, provider=provider, overrides={"max_blocks": 3}) as (
            runtime,
            _,
            _,
            session,
            _,
            _,
        ):
            yield runtime, session
    elif family == "skill":
        runtime, store, _, session, values = await build_case(
            tmp_path, provider=provider, context=context_policy(max_blocks=3)
        )
        try:
            await select(runtime, session, values[0])
            yield runtime, session
        finally:
            await runtime.dispose()
            await store.aclose()
    else:
        runtime, prepared, session = await setup(
            tmp_path, provider.responses, context_policy=policy(max_blocks=3)
        )
        # The helper's preparation also uses its Provider; observe its actual dispatched requests.
        provider.requests = prepared.requests
        try:
            yield runtime, session
        finally:
            await runtime.dispose()


@pytest.mark.parametrize(
    "family,query", [("history", "handover"), ("memory", "Keep"), ("skill", "Fixture navigation")]
)
async def test_dispatched_schema_guides_model_to_a_valid_configured_limit(tmp_path, family, query):
    def request_search(request):
        tool = next(t for t in request.tools if t.name == "search_" + family)
        # Reproduce the real model's limit=10 habit when no upper bound is advertised.
        limit = tool.input_schema["properties"]["limit"].get("maximum", 10)
        return ModelResponse(
            tool_calls=(ToolCall("search", tool.name, {"query": query, "limit": limit}),)
        )

    def answer(request):
        pages = [json.loads(item["body"]) for item in items(request) if item["tier"] == "search"]
        return ModelResponse(content="found" if pages and pages[0]["hits"] else "no evidence")

    provider = SelectingProvider([request_search, answer])
    async with case(tmp_path, family, provider) as (runtime, session):
        result = await runtime.run_existing(session, "What did we agree?")
        assert result.final_text == "found"
        request = provider.requests[-1]
        visible = {tool.name for tool in request.tools}
        assert "## traceh.runtime.source_navigation\n" in request.system_prompt
        navigation = request.system_prompt.split("## traceh.runtime.source_navigation\n", 1)[1]
        navigation = navigation.split("\n\n##", 1)[0]
        assert "Exposure is not authorization" in navigation
        assert "search_" + family in navigation
        for other in {"history", "memory", "skill"} - {family}:
            if "search_" + other not in visible:
                assert "search_" + other not in navigation
        for item in items(request):
            if item["tier"] == "search":
                assert "case-insensitive literal substring" in item["search_notice"]
                assert "source-unavailable is a different" in item["search_notice"]
        assert (
            "An empty reference array does not mean that earlier output is unavailable"
            in request.messages[-1].content
        )
        schema = next(
            t.input_schema for t in provider.requests[-2].tools if t.name == "search_" + family
        )
        assert schema["properties"]["limit"]["maximum"] == 3
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, session)


def test_invalid_limit_keeps_stable_code_and_explains_configured_bound():
    with pytest.raises(ReferenceSearchError) as caught:
        parse_request({"query": "example", "limit": 10}, policy(max_blocks=3))
    assert caught.value.code == "reference-search-request-invalid"
    assert "1 to 3" in str(caught.value) and "does not mean no evidence" in str(caught.value)
