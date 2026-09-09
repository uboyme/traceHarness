"""Selected navigation discovery through original Runtime and leased body readers."""

import asyncio
import json
from dataclasses import replace

import pytest
from plugin_fixtures import ScriptedPlugin, manifest
from retrieval_fixtures import build_case, context_policy, retrieval_policy, select
from skill_fixtures import contribution, digest, discovery, policy
from test_history_runtime import SelectingProvider, items
from test_skill_navigation import navigation_skill

from traceh.api.llm import ModelResponse, ToolCall
from traceh.api.skills import SkillResourceRoot, SkillSection, SkillSectionContent
from traceh.runtime.request_builder import verify_request_snapshots


async def setup(tmp_path, provider, *, config_changes=None):
    value = navigation_skill()
    (tmp_path / "appendix.txt").write_bytes(b"TABLE BODY 682")
    return await build_case(
        tmp_path,
        provider=provider,
        values=(value,),
        activation_policy=policy(SkillResourceRoot(value.descriptor.plugin, tmp_path)),
        config_changes=config_changes,
    )


def search(query, cursor=None, call_id="skill-search"):
    return ModelResponse(
        tool_calls=(
            ToolCall(
                call_id,
                "search_skill",
                {"query": query, "limit": 1, "cursor": cursor},
            ),
        )
    )


def page(request):
    return json.loads(next(item["body"] for item in items(request) if item["tier"] == "search"))


def read_hit(request):
    action = page(request)["hits"][0]["read_action"]
    return ModelResponse(tool_calls=(ToolCall("read", action["tool_name"], action["arguments"]),))


@pytest.mark.parametrize(
    "query,tier,body",
    [
        ("离线标定", "section", "OFFLINE BODY 731"),
        ("离线参数", "chunk", "TABLE BODY 682"),
    ],
)
async def test_zero_automatic_hits_search_navigation_then_read_original(
    tmp_path, query, tier, body
):
    provider = SelectingProvider([search(query), read_hit, ModelResponse(content="done")])
    runtime, store, _, session, values = await setup(tmp_path, provider)
    try:
        await select(runtime, session, *values)
        result = await runtime.run_existing(session, "What did we arrange?")
        assert result.steps == 3
        assert items(provider.requests[0]) == []
        hit = page(provider.requests[1])["hits"][0]
        assert query in hit["text"] and body not in provider.requests[1].messages[-1].content
        assert hit["reference"]["requested_tier"] == tier
        assert any(
            item["tier"] == tier and item["body"] == body for item in items(provider.requests[2])
        )
        assert not await runtime.check_invariants(session)
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, session)
    finally:
        await runtime.dispose()
        await store.aclose()


@pytest.mark.parametrize(
    "selected,query,status",
    [
        (False, "离线标定", "source-unavailable"),
        (True, "OFFLINE BODY 731", "no-hit"),
    ],
)
async def test_unselected_and_hidden_body_are_not_searchable(tmp_path, selected, query, status):
    provider = SelectingProvider([search(query), ModelResponse(content="missing")])
    runtime, store, _, session, values = await setup(tmp_path, provider)
    try:
        if selected:
            await select(runtime, session, *values)
        await runtime.run_existing(session, "What did we arrange?")
        assert page(provider.requests[-1])["status"] == status
        assert page(provider.requests[-1])["hits"] == []
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, session)
    finally:
        await runtime.dispose()
        await store.aclose()


@pytest.mark.parametrize("field,value", [("section_id", "other"), ("catalog_digest", "f" * 64)])
async def test_search_handle_cannot_authorize_other_location(tmp_path, field, value):
    def wrong_read(request):
        action = page(request)["hits"][0]["read_action"]
        return ModelResponse(
            tool_calls=(
                ToolCall(
                    "wrong-read",
                    action["tool_name"],
                    {**action["arguments"], field: value},
                ),
            )
        )

    provider = SelectingProvider([search("离线标定"), wrong_read, ModelResponse(content="missing")])
    runtime, store, _, session, values = await setup(tmp_path, provider)
    try:
        await select(runtime, session, *values)
        await runtime.run_existing(session, "What did we arrange?")
        events = await runtime.sessions.read_session(session)
        result = next(
            e for e in events if e.type == "tool/result" and e.data["tool_call_id"] == "wrong-read"
        )
        assert result.data["status"] == "failed"
        assert not any(item["tier"] == "section" for item in items(provider.requests[-1]))
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, session)
    finally:
        await runtime.dispose()
        await store.aclose()


async def test_search_cursor_pages_navigation_without_revealing_body(tmp_path):
    def next_page(request):
        return search("离线", page(request)["next_cursor"], "next-search")

    provider = SelectingProvider(
        [
            search("离线"),
            next_page,
            read_hit,
            ModelResponse(content="done"),
        ]
    )
    runtime, store, _, session, values = await setup(tmp_path, provider)
    try:
        await select(runtime, session, *values)
        await runtime.run_existing(session, "What did we arrange?")
        first, second = page(provider.requests[1]), page(provider.requests[2])
        assert first["next_cursor"] and second["next_cursor"] is None
        assert first["hits"][0]["reference"]["requested_tier"] == "section"
        assert second["hits"][0]["reference"]["requested_tier"] == "chunk"
        assert any(item["body"] == "TABLE BODY 682" for item in items(provider.requests[-1]))
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, session)
    finally:
        await runtime.dispose()
        await store.aclose()


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("output_limit", [64, 24000])
async def test_selection_change_or_cancel_before_admission(
    tmp_path, monkeypatch, cancel, output_limit
):
    provider = SelectingProvider(
        [
            search("离线标定"),
            ModelResponse(content="unavailable"),
            ModelResponse(content="later"),
        ]
    )
    runtime, store, _, session, values = await setup(
        tmp_path, provider, config_changes={"max_tool_output_chars": output_limit}
    )
    original = store.query_context_index
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def query(corpus, terms):
        result = await original(corpus, terms)
        events = await store.read("session:" + session)
        if any(
            e.type == "tool/result" and e.data.get("data", {}).get("search_receipt") for e in events
        ):
            entered.set()
            try:
                await release.wait()
            finally:
                finished.set()
        return result

    task = None
    try:
        await select(runtime, session, *values)
        monkeypatch.setattr(store, "query_context_index", query)
        task = asyncio.create_task(runtime.run_existing(session, "What did we arrange?"))
        await asyncio.wait_for(entered.wait(), 10)
        if cancel:
            task.cancel()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert finished.is_set() and len(provider.requests) == 1
        else:
            await select(runtime, session, operation="unselect", head=1)
            release.set()
            await task
            assert not any(item["kind"] == "skill" for item in items(provider.requests[-1]))
        monkeypatch.setattr(store, "query_context_index", original)
        await runtime.run_existing(session, "New topic")
        assert not any(item["tier"] == "search" for item in items(provider.requests[-1]))
        assert not await runtime.check_invariants(session)
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, session)
    finally:
        release.set()
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await runtime.dispose()
        await store.aclose()


async def test_large_directory_omitted_but_search_reads_target(tmp_path):
    value = contribution("reference.author", "large.manual", "placeholder")
    parts = [(f"part-{index:02}", "普通条目", "常规说明", "常规原文") for index in range(31)]
    parts[23] = ("part-23", "停机校准", "校准步骤", "实际校准口令 BIRCH-821")
    value = replace(
        value,
        descriptor=replace(
            value.descriptor,
            sections=tuple(
                SkillSection(
                    sid, "section", digest(body), len(body.encode()), title=title, summary=summary
                )
                for sid, title, summary, body in parts
            ),
        ),
        sections=tuple(SkillSectionContent(sid, body) for sid, _, _, body in parts),
    )
    provider = SelectingProvider([search("停机校准"), read_hit, ModelResponse(content="done")])
    runtime, store, _, session, _ = await build_case(
        tmp_path,
        values=(value,),
        provider=provider,
        activation_policy=policy(max_catalog_bytes=80000),
        context=context_policy(
            item_bytes=2500,
            skills=retrieval_policy(
                max_catalog_bytes=80000, max_corpus_items=80, max_corpus_bytes=200000
            ),
        ),
    )
    try:
        assert len(json.dumps(value.descriptor.directory(), ensure_ascii=False).encode()) > 2500
        await select(runtime, session, value)
        await runtime.run_existing(session, "What did we arrange?")
        assert items(provider.requests[0]) == []
        assert page(provider.requests[1])["hits"][0]["reference"]["section_id"] == "part-23"
        assert any(item["body"] == parts[23][3] for item in items(provider.requests[-1]))
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, session)
    finally:
        await runtime.dispose()
        await store.aclose()


async def test_plugin_catalog_change_invalidates_selection_and_old_search(tmp_path):
    value = contribution("reference.author", "manual", "source body")
    provider = SelectingProvider(
        [
            search("navigation"),
            ModelResponse(content="found"),
            search("navigation", call_id="stale-search"),
            ModelResponse(content="unavailable"),
            search("navigation", call_id="new-search"),
            read_hit,
            ModelResponse(content="done"),
        ]
    )
    runtime, store, _, session, _ = await build_case(tmp_path, provider=provider, values=(value,))
    try:
        await select(runtime, session, value)
        await runtime.run_existing(session, "What did we arrange?")
        old_digest = page(provider.requests[-1])["hits"][0]["reference"]["catalog_digest"]
        changed = replace(value, descriptor=replace(value.descriptor, summary="updated navigation"))
        await runtime.replace_plugin_composition(
            ("reference.author",),
            plugin_discovery=discovery(
                ScriptedPlugin(manifest("reference.author"), skills=(changed,))
            ),
        )
        await runtime.run_existing(session, "What did we arrange?")
        assert page(provider.requests[-1])["status"] == "source-unavailable"
        await select(runtime, session, changed, operation="reconfirm", head=1)
        await runtime.run_existing(session, "What did we arrange?")
        assert page(provider.requests[-2])["hits"][0]["reference"]["catalog_digest"] != old_digest
        assert not await runtime.check_invariants(session)
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, session)
    finally:
        await runtime.dispose()
        await store.aclose()
