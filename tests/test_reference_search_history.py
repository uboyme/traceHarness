"""Active History search exercises original Runtime, receipt, Context and replay."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace

import pytest
from test_history_runtime import SelectingProvider, items, policy

from traceh.api.json_types import canonical_json, fingerprint
from traceh.api.llm import ModelResponse, ToolCall
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.event_store import InMemoryEventStore
from traceh.session.reference_search import ReferenceSearchError, parse_request


def search(query, *, cursor=None, limit=1, call_id="search"):
    return ModelResponse(
        tool_calls=(
            ToolCall(
                call_id,
                "search_history",
                {
                    "query": query,
                    "cursor": cursor,
                    "limit": limit,
                },
            ),
        )
    )


def search_body(request):
    return json.loads(next(i["body"] for i in items(request) if i["tier"] == "search"))


def read_hit(request):
    action = search_body(request)["hits"][0]["read_action"]
    return ModelResponse(
        tool_calls=(ToolCall("read-hit", action["tool_name"], action["arguments"]),)
    )


async def setup(tmp_path, responses, *, context_policy=None, contents=None, event_store=None):
    contents = contents or ("earlier noise", "more unrelated records", "handover: PINE-392")
    provider = SelectingProvider([*[ModelResponse(content="noted") for _ in contents], *responses])
    context_policy = context_policy or policy()
    context_policy = replace(
        context_policy, history=replace(context_policy.history, page_messages=2)
    )
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data", context_input=context_policy),
        provider=provider,
        event_store=event_store if event_store is not None else InMemoryEventStore(),
    )
    result = await runtime.run(tmp_path, contents[0])
    for content in contents[1:]:
        await runtime.run_existing(result.session_id, content)
    events = await runtime.sessions.read_session(result.session_id)
    await runtime.compaction.replace_through(
        result.session_id,
        through_seq=events[-1].seq,
        summary="Earlier arrangements recorded",
    )
    return runtime, provider, result.session_id


async def test_search_late_page_then_original_read_retains_and_replays(tmp_path):
    runtime, provider, session_id = await setup(
        tmp_path,
        [
            search("handover"),
            read_hit,
            ModelResponse(tool_calls=(ToolCall("list", "list_files", {"path": "."}),)),
            ModelResponse(content="done"),
            ModelResponse(content="new turn"),
        ],
    )
    try:
        result = await runtime.run_existing(session_id, "Who handles the handover?")
        assert result.steps == 4
        body = search_body(provider.requests[4])
        assert body["status"] == "matches"
        assert body["hits"][0]["reference"]["cursor"]["index"] == 2
        assert "PINE-392" in body["hits"][0]["text"]
        assert "PINE-392" in items(provider.requests[5])[0]["body"]
        assert items(provider.requests[5])[0]["tier"] == "chunk"
        assert items(provider.requests[6])[0]["tier"] == "chunk"
        assert not any(i["tier"] == "search" for i in items(provider.requests[5]))
        events = await runtime.sessions.read_session(session_id)
        receipt = next(
            e for e in events if e.type == "tool/result" and e.data["tool_name"] == "search_history"
        )
        assert receipt.data["status"] == "succeeded"
        assert "PINE-392" not in canonical_json(receipt.data)
        await runtime.run_existing(session_id, "New unrelated question")
        assert all(i["tier"] == "directory" for i in items(provider.requests[-1]))
        assert runtime.invariants.check(await runtime.sessions.read_session(session_id)) == ()
        assert await verify_request_snapshots(runtime.sessions, runtime.surface, session_id) == ()
    finally:
        await runtime.dispose()


async def test_cursor_continues_only_admitted_query_and_keeps_source_stable(tmp_path):
    def next_page(request):
        return search("record", cursor=search_body(request)["next_cursor"], call_id="search-next")

    runtime, provider, session_id = await setup(
        tmp_path,
        [
            search("record"),
            next_page,
            ModelResponse(content="done"),
        ],
        contents=("record one", "record two", "record three"),
    )
    try:
        await runtime.run_existing(session_id, "Find my earlier records")
        first, second = search_body(provider.requests[4]), search_body(provider.requests[5])
        assert first["hits"][0]["text"] == "record one"
        assert second["hits"][0]["text"] == "record two"
        assert first["next_cursor"]["source_digest"] == second["next_cursor"]["source_digest"]
        assert await verify_request_snapshots(runtime.sessions, runtime.surface, session_id) == ()
    finally:
        await runtime.dispose()


async def test_nonmatching_query_is_bounded_no_hit_not_missing_source(tmp_path):
    runtime, provider, session_id = await setup(
        tmp_path, [search("absent-token"), ModelResponse(content="done")]
    )
    try:
        await runtime.run_existing(session_id, "Find a record")
        body = search_body(provider.requests[-1])
        assert body["status"] == "no-hit" and body["hits"] == []
        assert body["total"] == body["scanned"] == 6
        assert body["next_cursor"] is None
        assert await verify_request_snapshots(runtime.sessions, runtime.surface, session_id) == ()
    finally:
        await runtime.dispose()


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"query": " "},
        {"query": "abc", "limit": True},
        {"query": "abc", "limit": 99},
        {"query": "abc", "other": "x"},
        {"query": "abc", "cursor": {"offset": 1}},
    ],
)
def test_strict_search_request(arguments):
    with pytest.raises(ReferenceSearchError):
        parse_request(arguments, policy())


@pytest.mark.parametrize("cancel", [False, True])
async def test_failure_or_cancel_before_search_admission_expires_receipt(
    tmp_path, monkeypatch, cancel
):
    runtime, provider, session_id = await setup(
        tmp_path, [search("handover"), ModelResponse(content="later")]
    )
    owner = type(runtime.loop.context_inputs)
    original = owner.freeze
    entered, release = asyncio.Event(), asyncio.Event()

    async def fail_target(self, **kwargs):
        snapshot = await original(self, **kwargs)
        if any(b["tier"] == "search" for b in snapshot.to_dict()["blocks"]):
            entered.set()
            await release.wait()
            raise RuntimeError("search admission persistence failure")
        return snapshot

    try:
        monkeypatch.setattr(owner, "freeze", fail_target)
        task = asyncio.create_task(runtime.run_existing(session_id, "Find handover"))
        await asyncio.wait_for(entered.wait(), timeout=10)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            with pytest.raises(RuntimeError, match="search admission persistence failure"):
                await task
        events = await runtime.sessions.read_session(session_id)
        assert [e.type for e in events[-2:]] == ["step/end", "turn/end"]
        assert any(
            e.type == "tool/result" and e.data.get("data", {}).get("search_receipt") for e in events
        )
        assert len(provider.requests) == 4
        monkeypatch.setattr(owner, "freeze", original)
        await runtime.recovery.recover(session_id)
        await runtime.run_existing(session_id, "New turn")
        assert not any(i["tier"] == "search" for i in items(provider.requests[-1]))
        assert runtime.invariants.check(await runtime.sessions.read_session(session_id)) == ()
    finally:
        release.set()
        await runtime.dispose()


async def test_same_step_search_receipt_cannot_authorize_guessed_late_page(tmp_path):
    def premature(request):
        first = items(request)[0]["read_action"]["arguments"]
        guessed = {**first, "cursor": {**first["cursor"], "index": 2}}
        return ModelResponse(
            tool_calls=(
                ToolCall("search", "search_history", {"query": "handover"}),
                ToolCall("premature", "request_history_page", guessed),
            )
        )

    runtime, provider, session_id = await setup(
        tmp_path, [premature, ModelResponse(content="done")]
    )
    try:
        await runtime.run_existing(session_id, "Find handover")
        events = await runtime.sessions.read_session(session_id)
        result = next(
            e
            for e in events
            if e.type == "tool/result" and e.data.get("tool_call_id") == "premature"
        )
        assert result.data["status"] == "failed"
        assert not any(i["tier"] == "chunk" for i in items(provider.requests[-1]))
        assert search_body(provider.requests[-1])["hits"]
        assert await verify_request_snapshots(runtime.sessions, runtime.surface, session_id) == ()
    finally:
        await runtime.dispose()


async def test_search_hit_expires_when_next_step_has_no_read(tmp_path):
    saved = {}

    def save_then_wait(request):
        saved.update(search_body(request)["hits"][0]["read_action"])
        return ModelResponse(tool_calls=(ToolCall("list", "list_files", {"path": "."}),))

    def stale_read(request):
        assert not any(i["tier"] == "search" for i in items(request))
        return ModelResponse(
            tool_calls=(ToolCall("stale-read", saved["tool_name"], saved["arguments"]),)
        )

    runtime, provider, session_id = await setup(
        tmp_path,
        [
            search("handover"),
            save_then_wait,
            stale_read,
            ModelResponse(content="done"),
        ],
    )
    try:
        await runtime.run_existing(session_id, "Find handover")
        events = await runtime.sessions.read_session(session_id)
        result = next(
            e
            for e in events
            if e.type == "tool/result" and e.data.get("tool_call_id") == "stale-read"
        )
        assert result.data["status"] == "failed"
        assert not any(i["tier"] == "chunk" for i in items(provider.requests[-1]))
        assert await verify_request_snapshots(runtime.sessions, runtime.surface, session_id) == ()
    finally:
        await runtime.dispose()


async def test_unreadable_full_turn_has_snippet_but_no_false_page_grant(tmp_path):
    configured = policy()
    configured = replace(configured, history=replace(configured.history, page_bytes=200))
    runtime, provider, session_id = await setup(
        tmp_path,
        [search("handover"), ModelResponse(content="done")],
        context_policy=configured,
        contents=("handover " + "x" * 1000,),
    )
    try:
        await runtime.run_existing(session_id, "Find handover")
        hit = search_body(provider.requests[-1])["hits"][0]
        assert hit["read_action"] is None
        assert hit["reference"]["readable"] is False
        assert await verify_request_snapshots(runtime.sessions, runtime.surface, session_id) == ()
    finally:
        await runtime.dispose()


async def test_search_receipt_digest_tamper_is_rejected_without_target_context(tmp_path):
    from traceh.session.reference_search import validate_search_events

    runtime, _, session_id = await setup(
        tmp_path, [search("handover"), ModelResponse(content="done")]
    )
    try:
        await runtime.run_existing(session_id, "Find handover")
        events = await runtime.sessions.read_session(session_id)
        result = next(
            e
            for e in events
            if e.type == "tool/result" and e.data.get("tool_name") == "search_history"
        )
        altered = replace(
            result,
            data={
                **result.data,
                "data": {
                    "search_receipt": {
                        **result.data["data"]["search_receipt"],
                        "source_digest": "0" * 64,
                    }
                },
            },
        )
        prefix = (*events[: result.seq - 1], altered)
        with pytest.raises(ReferenceSearchError, match="receipt-invalid"):
            validate_search_events(prefix)
        assert any(
            v.name == "reference-search-receipt-invalid" for v in runtime.invariants.check(prefix)
        )
    finally:
        await runtime.dispose()


async def test_public_context_append_rejects_rehashed_forged_search_text(tmp_path, monkeypatch):
    from traceh.session.context_input import ContextInputError, parse_context_input

    runtime, provider, session_id = await setup(
        tmp_path, [search("handover"), ModelResponse(content="tampered source reached")]
    )
    owner = type(runtime.loop.context_inputs)
    original = owner.freeze

    async def tamper(self, **kwargs):
        snapshot = await original(self, **kwargs)
        data = snapshot.to_dict()
        for block in data["blocks"]:
            if block["tier"] == "search":
                assert "PINE-392" in block["body"]
                block["body"] = block["body"].replace("PINE-392", "FAKE-000")
                block["content_digest"] = hashlib.sha256(block["body"].encode()).hexdigest()
                data["context_digest"] = fingerprint(
                    {k: v for k, v in data.items() if k != "context_digest"}
                )
                return parse_context_input(data)
        return snapshot

    try:
        monkeypatch.setattr(owner, "freeze", tamper)
        with pytest.raises(ContextInputError, match="context-source-binding-mismatch"):
            await runtime.run_existing(session_id, "Find handover")
        events = await runtime.sessions.read_session(session_id)
        assert len(provider.requests) == 4
        assert not any("FAKE-000" in canonical_json(e.data) for e in events)
        assert any(
            e.type == "tool/result" and e.data.get("data", {}).get("search_receipt") for e in events
        )
    finally:
        await runtime.dispose()


async def test_guessed_search_offset_with_valid_digests_is_not_a_grant(tmp_path):
    def forged_next(request):
        cursor = search_body(request)["next_cursor"]
        return search(
            "record", cursor={**cursor, "offset": cursor["offset"] + 1}, call_id="forged-next"
        )

    runtime, provider, session_id = await setup(
        tmp_path,
        [search("record"), forged_next, ModelResponse(content="done")],
        contents=("record one", "record two", "record three"),
    )
    try:
        await runtime.run_existing(session_id, "Find earlier records")
        events = await runtime.sessions.read_session(session_id)
        result = next(
            e
            for e in events
            if e.type == "tool/result" and e.data.get("tool_call_id") == "forged-next"
        )
        assert result.data["status"] == "failed"
        assert not any(i["tier"] == "search" for i in items(provider.requests[-1]))
        assert await verify_request_snapshots(runtime.sessions, runtime.surface, session_id) == ()
    finally:
        await runtime.dispose()


async def test_current_session_search_does_not_discover_or_read_another_session(tmp_path):
    saved = {}

    def save_hit(request):
        saved.update(search_body(request)["hits"][0]["read_action"])
        return ModelResponse(content="done")

    def foreign_read(request):
        assert search_body(request)["status"] == "source-unavailable"
        assert "PINE-392" not in request.messages[-1].content
        return ModelResponse(
            tool_calls=(ToolCall("foreign", saved["tool_name"], saved["arguments"]),)
        )

    runtime, provider, session_id = await setup(
        tmp_path,
        [
            search("handover"),
            save_hit,
            search("handover", call_id="other-search"),
            foreign_read,
            ModelResponse(content="unavailable"),
        ],
    )
    try:
        await runtime.run_existing(session_id, "Find handover")
        other = await runtime.run(tmp_path, "Find prior handover")
        assert other.session_id != session_id
        events = await runtime.sessions.read_session(other.session_id)
        result = next(
            e for e in events if e.type == "tool/result" and e.data.get("tool_call_id") == "foreign"
        )
        assert result.data["status"] == "failed"
        assert "PINE-392" not in provider.requests[-1].messages[-1].content
        assert (
            await verify_request_snapshots(runtime.sessions, runtime.surface, other.session_id)
            == ()
        )
    finally:
        await runtime.dispose()


@pytest.mark.parametrize(
    ("source", "query"),
    [
        ("联络代号：雪松-三九", "联络代号"),
        ("Ångström contact: IZ-7", "ångström"),
        ("literal a.b should match only itself", "a.b"),
    ],
)
async def test_unicode_and_literal_matching_survive_nested_compaction(tmp_path, source, query):
    runtime, provider, session_id = await setup(
        tmp_path,
        [ModelResponse(content="noted"), search(query), read_hit, ModelResponse(content="done")],
        contents=(source, "unrelated background"),
    )
    try:
        await runtime.run_existing(session_id, "One more unrelated arrangement")
        events = await runtime.sessions.read_session(session_id)
        await runtime.compaction.replace_through(
            session_id,
            through_seq=events[-1].seq,
            summary="Earlier arrangements and additional background",
        )
        await runtime.run_existing(session_id, "Find earlier arrangement")
        body = search_body(provider.requests[-2])
        assert body["hits"][0]["text"] == source
        assert source in items(provider.requests[-1])[0]["body"]
        assert await verify_request_snapshots(runtime.sessions, runtime.surface, session_id) == ()
    finally:
        await runtime.dispose()


async def test_search_after_sqlite_restart_uses_original_closed_history(tmp_path):
    from traceh.session.sqlite import SqliteEventStore

    store = SqliteEventStore(tmp_path / "events")
    runtime, _, session_id = await setup(tmp_path, [], event_store=store)
    config = runtime.config
    await runtime.dispose()
    await store.aclose()
    reopened = SqliteEventStore(tmp_path / "events")
    provider = SelectingProvider([search("handover"), read_hit, ModelResponse(content="done")])
    runtime = build_default_runtime(config, provider=provider, event_store=reopened)
    try:
        await runtime.run_existing(session_id, "Find earlier handover")
        assert search_body(provider.requests[1])["hits"][0]["reference"]["cursor"]["index"] == 2
        assert "PINE-392" in items(provider.requests[-1])[0]["body"]
        assert await verify_request_snapshots(runtime.sessions, runtime.surface, session_id) == ()
    finally:
        await runtime.dispose()
        await reopened.aclose()


async def test_search_page_budget_reduces_hits_and_gives_exact_continuation(tmp_path):
    runtime, provider, session_id = await setup(
        tmp_path,
        [search("record", limit=6), ModelResponse(content="done")],
        context_policy=policy(item_bytes=3200),
        contents=("record one", "record two", "record three"),
    )
    try:
        await runtime.run_existing(session_id, "Find records")
        body = search_body(provider.requests[-1])
        assert 0 < len(body["hits"]) < 3
        assert body["next_cursor"] is not None
        assert body["next_cursor"]["offset"] == body["scanned"]
        events = await runtime.sessions.read_session(session_id)
        context = next(e for e in reversed(events) if e.type == "context/input")
        assert (
            context.data["budget"]["reference_bytes"] <= context.data["budget"]["reference_limit"]
        )
        assert await verify_request_snapshots(runtime.sessions, runtime.surface, session_id) == ()
    finally:
        await runtime.dispose()


async def test_unadmitted_search_page_does_not_linger_or_authorize_reads(tmp_path):
    runtime, provider, session_id = await setup(
        tmp_path,
        [
            search("handover"),
            ModelResponse(tool_calls=(ToolCall("list", "list_files", {"path": "."}),)),
            ModelResponse(content="done"),
        ],
        context_policy=policy(item_bytes=500),
    )
    try:
        result = await runtime.run_existing(session_id, "Find handover")
        events = await runtime.sessions.read_session(session_id)
        contexts = [
            e for e in events if e.type == "context/input" and e.data["turn_id"] == result.turn_id
        ]
        assert not any(b["tier"] == "search" for e in contexts for b in e.data["blocks"])
        assert any(e["reason"] == "budget-excluded" for e in contexts[1].data["exclusions"])
        assert "PINE-392" not in provider.requests[-1].messages[-1].content
        assert await verify_request_snapshots(runtime.sessions, runtime.surface, session_id) == ()
    finally:
        await runtime.dispose()
