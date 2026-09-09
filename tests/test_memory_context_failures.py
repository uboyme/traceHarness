"""Memory retrieval counterexamples exercise public Runtime/Session boundaries."""

import asyncio

import pytest
from retrieval_fixtures import retrieval_policy
from test_history_runtime import SelectingProvider
from test_memory_context import BODY, DisclosureProvider, memory_case

from traceh.api.json_types import canonical_json, fingerprint
from traceh.api.llm import ModelResponse, ToolCall
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.memory_requests import MEMORY_TOOL_NAME


async def revoke(runtime, session, proposal, activation):
    from traceh.projects.events import reference

    await runtime.memory.revoke(
        session,
        memory_id="context-fact",
        fact_slot="project-boundaries",
        predecessor_ref=reference(activation),
        predecessor_digest=proposal.data["proposal_digest"],
        operation_id="revoke-during-context",
        actor_id="host",
        expected_head=2,
    )


async def test_revoke_during_index_query_drops_blocks_and_ranking(tmp_path, monkeypatch):
    async with memory_case(tmp_path) as (runtime, store, provider, session, proposal, activation):
        await runtime.memory.rebuild_index(session)
        entered, release = asyncio.Event(), asyncio.Event()
        original = store.query_context_index

        async def query(corpus, terms):
            hits = await original(corpus, terms)
            entered.set()
            await release.wait()
            return hits

        monkeypatch.setattr(store, "query_context_index", query)
        task = asyncio.create_task(runtime.run_existing(session, "context-fact"))
        try:
            await asyncio.wait_for(entered.wait(), 10)
            await revoke(runtime, session, proposal, activation)
        finally:
            release.set()
        await task
        data = next(
            e.data
            for e in await runtime.sessions.read_session(session)
            if e.type == "context/input"
        )
        assert data["blocks"] == [] and data["retrieval"] is None
        assert data["memory_source"] is None and data["source_heads"] == []
        assert "context-fact" not in canonical_json(data["exclusions"])
        assert BODY not in canonical_json(provider.requests[0].to_dict())


async def test_cancel_query_leaves_no_context_or_model_dispatch(tmp_path, monkeypatch):
    async with memory_case(tmp_path) as (runtime, store, provider, session, _, _):
        entered, finished = asyncio.Event(), asyncio.Event()

        async def query(corpus, terms):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

        monkeypatch.setattr(store, "query_context_index", query)
        task = asyncio.create_task(runtime.run_existing(session, "context-fact"))
        await asyncio.wait_for(entered.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set() and provider.requests == []
        events = await runtime.sessions.read_session(session)
        assert not any(e.type == "context/input" for e in events)
        assert events[-1].type == "turn/end"


@pytest.mark.parametrize(
    "change",
    [
        {"memory_id": "not-disclosed"},
        {"version": "f" * 64},
        {"requested_tier": "chunk"},
        {"source_session": "foreign"},
    ],
)
async def test_model_cannot_expand_undisclosed_or_raw_memory_sources(tmp_path, change):
    provider = SelectingProvider([])
    async with memory_case(tmp_path, provider=provider, tier="directory") as (
        runtime,
        _,
        _,
        session,
        proposal,
        _,
    ):
        request = {
            "memory_id": "context-fact",
            "version": proposal.data["proposal_digest"],
            "requested_tier": "section",
            **change,
        }
        provider.responses = [
            ModelResponse(tool_calls=(ToolCall("bad-memory", MEMORY_TOOL_NAME, request),)),
            ModelResponse(content="done"),
        ]
        await runtime.run_existing(session, "context-fact")
        events = await runtime.sessions.read_session(session)
        result = next(e for e in events if e.type == "tool/result")
        expected = (
            "invalid"
            if "source_session" in change or change.get("requested_tier") == "chunk"
            else "failed"
        )
        assert result.data["status"] == expected
        assert all(
            b["tier"] == "directory"
            for e in events
            if e.type == "context/input"
            for b in e.data["blocks"]
        )
        assert all(BODY not in canonical_json(request.to_dict()) for request in provider.requests)


async def test_accepted_receipt_cannot_reactivate_revoked_fact(tmp_path, monkeypatch):
    provider = DisclosureProvider("section")
    async with memory_case(tmp_path, provider=provider, tier="directory") as (
        runtime,
        _,
        _,
        session,
        proposal,
        activation,
    ):
        original = runtime.sessions.append_session

        async def append(session_id, event_type, data, **kwargs):
            event = await original(session_id, event_type, data, **kwargs)
            if event_type == "tool/result" and data.get("tool_name") == MEMORY_TOOL_NAME:
                assert data["status"] == "succeeded"
                await revoke(runtime, session, proposal, activation)
            return event

        monkeypatch.setattr(runtime.sessions, "append_session", append)
        await runtime.run_existing(session, "context-fact")
        assert len(provider.requests) == 2
        assert BODY not in canonical_json(provider.requests[-1].to_dict())
        events = await runtime.sessions.read_session(session)
        assert [e for e in events if e.type == "context/input"][-1].data["blocks"] == []
        assert await verify_request_snapshots(runtime.sessions, runtime.surface, session) == ()


async def test_max_steps_expires_memory_receipt(tmp_path):
    provider = DisclosureProvider("section")
    async with memory_case(tmp_path, provider=provider, tier="directory", max_steps=1) as (
        runtime,
        _,
        _,
        session,
        _,
        _,
    ):
        await runtime.run_existing(session, "context-fact")
        await runtime.run_existing(session, "unrelated-topic")
        assert len(provider.requests) == 2
        assert BODY not in canonical_json(provider.requests[-1].to_dict())


@pytest.mark.parametrize("field", ["activation_ref", "approved_content_digest", "fact_slot"])
async def test_rehashed_memory_metadata_is_refused_before_dispatch(tmp_path, monkeypatch, field):
    async with memory_case(tmp_path) as (runtime, _, provider, session, _, _):
        original = runtime.sessions.append_context_input

        async def append(session_id, data, **kwargs):
            # Preserve shape, body, budgets and hash consistency; only authority evidence changes.
            provenance = data["blocks"][0]["provenance"]
            if field == "activation_ref":
                provenance[field]["digest"] = "f" * 64
                data["blocks"][0]["source_refs"][1]["digest"] = "f" * 64
            else:
                provenance[field] = "f" * 64 if field.endswith("digest") else "project-b0undaries"
            data["context_digest"] = fingerprint(
                {k: v for k, v in data.items() if k != "context_digest"}
            )
            return await original(session_id, data, **kwargs)

        monkeypatch.setattr(runtime.sessions, "append_context_input", append)
        with pytest.raises(ValueError):
            await runtime.run_existing(session, "context-fact")
        assert provider.requests == []
        assert not any(
            e.type == "context/input" for e in await runtime.sessions.read_session(session)
        )


async def test_memory_catalog_limit_fails_before_model_dispatch(tmp_path):
    async with memory_case(
        tmp_path, overrides={"memory": retrieval_policy(max_catalog_bytes=1)}
    ) as (
        runtime,
        _,
        provider,
        session,
        _,
        _,
    ):
        with pytest.raises(ValueError, match="memory-catalog-resource-limit"):
            await runtime.run_existing(session, "context-fact")
        assert provider.requests == []


@pytest.mark.parametrize(
    "query,hit",
    [
        ("handbook/Quick Start.txt", True),
        ("handbook/Quick", False),
        ("prefix/handbook/Quick Start.txt", False),
    ],
)
async def test_memory_exact_preserves_delimited_literal_and_boundaries(tmp_path, query, hit):
    async with memory_case(
        tmp_path, overrides={"memory": retrieval_policy(match_fields=("path",))}
    ) as (
        runtime,
        _,
        _,
        session,
        _,
        _,
    ):
        # No index is built: any body here must be authorized by the exact lane.
        await runtime.run_existing(session, query)
        data = next(
            e.data
            for e in await runtime.sessions.read_session(session)
            if e.type == "context/input"
        )
        assert bool(data["blocks"]) is hit
        assert bool(data["retrieval"]["memory"]["lanes"][0]["ranking"]) is hit


@pytest.mark.parametrize(
    "variant", ["context2", "old-policy", "missing-memory", "enabled-semantic"]
)
async def test_old_or_unimplemented_context_policy_is_rejected(tmp_path, variant):
    from traceh.session.context_input import parse_context_input

    async with memory_case(tmp_path) as (runtime, _, _, session, _, _):
        await runtime.run_existing(session, "context-fact")
        data = next(
            e.data
            for e in await runtime.sessions.read_session(session)
            if e.type == "context/input"
        )
        if variant == "context2":
            data["format"] = 2
        elif variant == "old-policy":
            data["policy"]["version"] = "f2-context-policy-v1"
        elif variant == "missing-memory":
            del data["policy"]["config"]["memory"]
        else:
            data["policy"]["config"]["local_lanes"]["semantic"] = {"enabled": True}
        data["policy"]["config_digest"] = fingerprint(data["policy"]["config"])
        data["context_digest"] = fingerprint(
            {k: v for k, v in data.items() if k != "context_digest"}
        )
        with pytest.raises(ValueError):
            parse_context_input(data)


async def test_disabling_default_tools_does_not_grant_memory_disclosure(tmp_path):
    async with memory_case(tmp_path, tools=False) as (runtime, _, provider, session, _, _):
        await runtime.run_existing(session, "context-fact")
        assert not any(tool.name == MEMORY_TOOL_NAME for tool in provider.requests[0].tools)
