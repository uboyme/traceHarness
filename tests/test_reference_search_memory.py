"""Memory search discovers omitted active facts on original authority/Context paths."""

import asyncio
import hashlib
import json

import pytest
from test_history_runtime import SelectingProvider, items
from test_memory_context import memory_case
from test_memory_context_failures import revoke

from traceh.api.json_types import canonical_json, fingerprint
from traceh.api.llm import ModelResponse, ToolCall
from traceh.projects.events import reference
from traceh.runtime.request_builder import verify_request_snapshots


def search(query, call_id="memory-search", cursor=None):
    return ModelResponse(
        tool_calls=(
            ToolCall(
                call_id,
                "search_memory",
                {
                    "query": query,
                    "limit": 1,
                    "cursor": cursor,
                },
            ),
        )
    )


def search_body(request):
    return json.loads(next(i["body"] for i in items(request) if i["tier"] == "search"))


def read_hit(request):
    action = search_body(request)["hits"][0]["read_action"]
    return ModelResponse(
        tool_calls=(ToolCall("read-memory", action["tool_name"], action["arguments"]),)
    )


async def test_zero_automatic_hits_then_search_original_read_and_revoked_replay(tmp_path):
    body = "Approved rendezvous code is FIR-819."
    provider = SelectingProvider(
        [
            search("rendezvous"),
            read_hit,
            ModelResponse(tool_calls=(ToolCall("list", "list_files", {}),)),
            ModelResponse(content="done"),
            search("rendezvous", "after-revoke"),
            ModelResponse(content="unavailable"),
        ]
    )
    async with memory_case(tmp_path, provider=provider, tier="directory", body=body) as (
        runtime,
        _,
        _,
        session,
        proposal,
        activation,
    ):
        result = await runtime.run_existing(session, "What did we agree?")
        assert result.steps == 4
        assert items(provider.requests[0]) == []
        hit = search_body(provider.requests[1])["hits"][0]
        assert body in hit["text"]
        assert hit["reference"]["source_refs"][1] == reference(activation)
        for request in provider.requests[2:4]:
            assert items(request)[0]["tier"] == "section" and items(request)[0]["body"] == body
        events = await runtime.sessions.read_session(session)
        receipt = next(
            e for e in events if e.type == "tool/result" and e.data["tool_name"] == "search_memory"
        )
        assert receipt.data["status"] == "succeeded" and body not in canonical_json(receipt.data)
        await revoke(runtime, session, proposal, activation)
        await runtime.run_existing(session, "What did we agree?")
        assert search_body(provider.requests[-1])["status"] == "no-hit"
        assert body not in provider.requests[-1].messages[-1].content
        assert not await runtime.check_invariants(session)
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, session)


async def test_unbound_session_search_is_unavailable_not_project_discovery(tmp_path):
    provider = SelectingProvider([search("project"), ModelResponse(content="unavailable")])
    async with memory_case(tmp_path, provider=provider) as (runtime, _, _, session, _, _):
        other = await runtime.create_session(await runtime.sessions.workspace_for(session))
        await runtime.run_existing(other, "What are the project limits?")
        body = search_body(provider.requests[-1])
        assert body["status"] == "source-unavailable" and body["hits"] == []
        assert body["total"] == 0
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, other)


async def test_proposal_only_text_is_never_searchable(tmp_path):
    provider = SelectingProvider([search("unapproved-label"), ModelResponse(content="unavailable")])
    async with memory_case(tmp_path, provider=provider) as (runtime, _, _, session, _, _):
        await runtime.memory.declare(
            session,
            proposal_id="unapproved-proposal",
            body="unapproved-label = RANDOM-67",
            statement="unapproved-label = RANDOM-67",
            declaration_id="unapproved-statement",
            operation_id="declare-pending",
            actor_id="host",
            expected_head=2,
        )
        await runtime.run_existing(session, "Find relevant prior agreement")
        body = search_body(provider.requests[-1])
        assert body["status"] == "no-hit" and body["total"] == 1
        assert "RANDOM-67" not in canonical_json(provider.requests[-1].to_dict())
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, session)


@pytest.mark.parametrize("change", ["id", "version"])
async def test_search_hit_does_not_authorize_other_memory_identity(tmp_path, change):
    def wrong_read(request):
        action = search_body(request)["hits"][0]["read_action"]
        arguments = {**action["arguments"]}
        arguments["memory_id" if change == "id" else "version"] = (
            "different" if change == "id" else "f" * 64
        )
        return ModelResponse(tool_calls=(ToolCall("wrong", action["tool_name"], arguments),))

    provider = SelectingProvider([search("Keep"), wrong_read, ModelResponse(content="done")])
    async with memory_case(tmp_path, provider=provider, tier="directory") as (
        runtime,
        _,
        _,
        session,
        _,
        _,
    ):
        await runtime.run_existing(session, "Find relevant prior agreement")
        events = await runtime.sessions.read_session(session)
        result = next(
            e for e in events if e.type == "tool/result" and e.data.get("tool_call_id") == "wrong"
        )
        assert result.data["status"] == "failed"
        assert not any(i["tier"] == "section" for i in items(provider.requests[-1]))
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, session)


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("output_limit", [64, 24000])
async def test_revoke_or_cancel_after_receipt_before_admission_never_leaks_body(
    tmp_path, monkeypatch, cancel, output_limit
):
    from test_memory_context import BODY

    provider = SelectingProvider(
        [search("Keep"), ModelResponse(content="unavailable"), ModelResponse(content="later")]
    )
    async with memory_case(
        tmp_path,
        provider=provider,
        tier="directory",
        config_changes={"max_tool_output_chars": output_limit},
    ) as (
        runtime,
        store,
        _,
        session,
        proposal,
        activation,
    ):
        original = store.query_context_index
        entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def query(corpus, terms):
            result = await original(corpus, terms)
            events = await store.read("session:" + session)
            if any(
                e.type == "tool/result" and e.data.get("data", {}).get("search_receipt")
                for e in events
            ):
                entered.set()
                try:
                    await release.wait()
                finally:
                    finished.set()
            return result

        monkeypatch.setattr(store, "query_context_index", query)
        task = asyncio.create_task(runtime.run_existing(session, "Find relevant agreement"))
        try:
            await asyncio.wait_for(entered.wait(), 10)
            if cancel:
                task.cancel()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert finished.is_set()
                assert len(provider.requests) == 1
            else:
                await revoke(runtime, session, proposal, activation)
                release.set()
                await task
                assert search_body(provider.requests[-1])["status"] == "source-unavailable"
                assert BODY not in provider.requests[-1].messages[-1].content
            monkeypatch.setattr(store, "query_context_index", original)
            await runtime.run_existing(session, "New unrelated question")
            assert not any(i["tier"] == "search" for i in items(provider.requests[-1]))
            assert not await runtime.check_invariants(session)
            assert not await verify_request_snapshots(runtime.sessions, runtime.surface, session)
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def test_rehashed_search_snippet_is_checked_against_approved_source(tmp_path, monkeypatch):
    from traceh.session.context_input import parse_context_input
    from traceh.session.reference_search import ReferenceSearchError

    provider = SelectingProvider([search("rendezvous"), ModelResponse(content="forgery reached")])
    async with memory_case(
        tmp_path, provider=provider, tier="directory", body="Approved rendezvous code is FIR-819."
    ) as (
        runtime,
        _,
        _,
        session,
        _,
        _,
    ):
        owner = type(runtime.loop.context_inputs)
        original = owner.freeze

        async def tamper(self, **kwargs):
            snapshot = await original(self, **kwargs)
            data = snapshot.to_dict()
            events = await runtime.sessions.read_session(session)
            for block in data["blocks"]:
                if block["kind"] == "memory" and block["tier"] == "search":
                    assert "FIR-819" in block["body"]
                    block["body"] = block["body"].replace("FIR-819", "FAKE000")
                    body = json.loads(block["body"])
                    receipt = events[block["provenance"]["request_ref"]["seq"] - 1].data["data"][
                        "search_receipt"
                    ]
                    block["id"] = fingerprint({"request": receipt, "body": body})
                    block["content_digest"] = hashlib.sha256(block["body"].encode()).hexdigest()
                    data["context_digest"] = fingerprint(
                        {k: v for k, v in data.items() if k != "context_digest"}
                    )
                    return parse_context_input(data)
            return snapshot

        monkeypatch.setattr(owner, "freeze", tamper)
        with pytest.raises(ReferenceSearchError, match="source-mismatch"):
            await runtime.run_existing(session, "What did we agree?")
        events = await runtime.sessions.read_session(session)
        assert len(provider.requests) == 1
        assert any(
            e.type == "tool/result" and e.data.get("data", {}).get("search_receipt") for e in events
        )
        assert not any("FAKE000" in canonical_json(e.data) for e in events)
