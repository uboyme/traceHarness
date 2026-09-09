"""F4 references through the real Runtime, authority, SQLite index and request builder."""

import json
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from memory_fixtures import Resolver, memory_policy
from retrieval_fixtures import context_policy, retrieval_policy

from traceh.api.json_types import canonical_json
from traceh.api.llm import ModelResponse, ToolCall
from traceh.api.memory import ProjectMemoryConfig, ProjectScopeLimits
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.projects.events import reference
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.runtime.request_builder import reconstruct_request, verify_request_snapshots
from traceh.session.memory_requests import MEMORY_TOOL_NAME
from traceh.session.sqlite import SqliteEventStore

BODY = "项目边界必须明确。Keep build.graph and `handbook/Quick Start.txt` reproducible."


@asynccontextmanager
async def memory_case(
    tmp_path,
    *,
    provider=None,
    tier="summary",
    overrides=None,
    tools=True,
    max_steps=20,
    config_changes=None,
    body=BODY,
    authority_policy=None,
):
    workspace = tmp_path / "source"
    workspace.mkdir()
    store = SqliteEventStore(tmp_path / "events")
    config = ProjectMemoryConfig(
        ProjectScopeLimits(100, 100),
        authority_policy or memory_policy(),
        Resolver({"context-source": workspace}),
    )
    provider = provider or ScriptedLlmProvider((ModelResponse(content="done"),), repeat_last=True)
    policy = context_policy(skills=None, memory=retrieval_policy(default_tier=tier))
    if overrides:
        policy = replace(policy, **overrides)
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "data",
            context_input=policy,
            memory=config,
            max_steps=max_steps,
            **(config_changes or {}),
        ),
        provider=provider,
        event_store=store,
        include_default_tools=tools,
    )
    try:
        session = await runtime.create_session(workspace)
        scope = runtime.project_scope
        await scope.create(
            project_id="context-project",
            label="Explicit project fixture",
            operation_id="create",
            actor_id="host",
            expected_head=0,
        )
        await scope.bind_source(
            project_id="context-project",
            source_id="context-source",
            operation_id="source",
            actor_id="host",
            expected_head=1,
        )
        await scope.bind_session(
            session,
            project_id="context-project",
            operation_id="bind",
            actor_id="host",
            expected_head=2,
        )
        proposal = await runtime.memory.declare(
            session,
            proposal_id="context-proposal",
            body=body,
            statement=body,
            declaration_id="statement",
            operation_id="propose",
            actor_id="host",
            expected_head=0,
        )
        activation = await runtime.memory.approve(
            session,
            proposal_ref=reference(proposal),
            proposal_digest=proposal.data["proposal_digest"],
            memory_id="context-fact",
            fact_slot="project-boundaries",
            operation_id="approve",
            actor_id="host",
            expected_head=1,
        )
        yield runtime, store, provider, session, proposal, activation
    finally:
        await runtime.dispose()
        await store.aclose()


@pytest.mark.parametrize(
    "query", ["context-fact", "项目边界", "build.graph", "handbook/Quick Start.txt"]
)
async def test_memory_exact_fts_and_historical_request_rebuild(tmp_path, query):
    async with memory_case(tmp_path) as (runtime, store, provider, session, proposal, activation):
        await runtime.memory.rebuild_index(session)
        await runtime.run_existing(session, query)
        events = await runtime.sessions.read_session(session)
        data = next(e.data for e in events if e.type == "context/input")
        assert len(data["blocks"]) == 1 and data["blocks"][0]["body"] == BODY
        assert data["scope"]["kind"] == "project"
        assert data["source_heads"][1]["head_seq"] == 2
        assert data["blocks"][0]["provenance"]["activation_ref"] == reference(activation)
        assert (
            data["blocks"][0]["content_digest"]
            == data["blocks"][0]["provenance"]["approved_content_digest"]
        )
        assert all(lane["status"] == "available" for lane in data["retrieval"]["memory"]["lanes"])
        original = next(e for e in events if e.type == "request/snapshot")
        await runtime.memory.revoke(
            session,
            memory_id="context-fact",
            fact_slot="project-boundaries",
            predecessor_ref=reference(activation),
            predecessor_digest=proposal.data["proposal_digest"],
            operation_id="revoke",
            actor_id="host",
            expected_head=2,
        )
        assert (
            await reconstruct_request(runtime.sessions, runtime.surface, session, original)
        ).request == provider.requests[0]
        await runtime.run_existing(session, query)
        current = [
            e for e in await runtime.sessions.read_session(session) if e.type == "context/input"
        ][-1]
        assert current.data["blocks"] == []
        assert BODY not in canonical_json(provider.requests[-1].to_dict())
        assert await verify_request_snapshots(runtime.sessions, runtime.surface, session) == ()
        assert await store.head("memory:context-project") == 3


@pytest.mark.parametrize(
    "query", ["unrelated-topic", '" OR NOT * NEAR(unmatched)', "preserve provenance"]
)
async def test_memory_zero_hit_and_disabled_semantics(tmp_path, query):
    async with memory_case(tmp_path) as (runtime, _, provider, session, _, _):
        await runtime.memory.rebuild_index(session)
        await runtime.run_existing(session, query)
        context = next(
            e for e in await runtime.sessions.read_session(session) if e.type == "context/input"
        )
        assert context.data["blocks"] == []
        assert context.data["policy"]["config"]["local_lanes"] == {
            "semantic": None,
            "reranker": None,
        }
        assert BODY not in canonical_json(provider.requests[0].to_dict())


class DisclosureProvider(ScriptedLlmProvider):
    def __init__(self, tier):
        super().__init__((), repeat_last=True)
        self.tier = tier

    async def complete(self, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            blocks = json.loads(request.messages[-1].content.split("\n")[1])
            block = next(b for b in blocks if b["kind"] == "memory")
            return ModelResponse(
                tool_calls=(
                    ToolCall(
                        "reference-call",
                        MEMORY_TOOL_NAME,
                        {
                            "memory_id": block["id"],
                            "version": block["version"],
                            "requested_tier": self.tier,
                        },
                    ),
                )
            )
        return ModelResponse(content="done")


@pytest.mark.parametrize("tier", ["summary", "section"])
async def test_memory_disclosure_only_enters_the_immediate_request(tmp_path, tier):
    provider = DisclosureProvider(tier)
    async with memory_case(tmp_path, provider=provider, tier="directory") as (
        runtime,
        _,
        _,
        session,
        _,
        _,
    ):
        await runtime.memory.rebuild_index(session)
        await runtime.run_existing(session, "context-fact")
        events = await runtime.sessions.read_session(session)
        contexts = [e for e in events if e.type == "context/input"]
        assert [e.data["blocks"][0]["tier"] for e in contexts] == ["directory", tier]
        result = next(e for e in events if e.type == "tool/result")
        assert result.data["status"] == "succeeded" and "memory_receipt" in result.data["data"]
        assert BODY not in canonical_json(result.data)
        assert BODY not in canonical_json(provider.requests[0].to_dict())
        assert BODY in canonical_json(provider.requests[1].to_dict())
        await runtime.run_existing(session, "unrelated-fact")
        assert BODY not in canonical_json(provider.requests[-1].to_dict())
        assert await verify_request_snapshots(runtime.sessions, runtime.surface, session) == ()


async def test_unbound_session_cannot_retrieve_at_the_same_path(tmp_path):
    async with memory_case(tmp_path) as (runtime, _, provider, session, _, _):
        await runtime.memory.rebuild_index(session)
        unbound = await runtime.create_session(await runtime.sessions.workspace_for(session))
        await runtime.run_existing(unbound, "context-fact")
        data = next(
            e.data
            for e in await runtime.sessions.read_session(unbound)
            if e.type == "context/input"
        )
        assert data["scope"]["kind"] == "session" and data["memory_source"] is None
        assert data["blocks"] == []
        assert BODY not in canonical_json(provider.requests[0].to_dict())


@pytest.mark.parametrize("limit", [0, 80])
async def test_memory_budget_excludes_whole_fact(tmp_path, limit):
    async with memory_case(
        tmp_path, overrides={"memory": retrieval_policy(context_bytes=limit)}
    ) as (
        runtime,
        _,
        _,
        session,
        _,
        _,
    ):
        await runtime.memory.rebuild_index(session)
        await runtime.run_existing(session, "context-fact")
        data = next(
            e.data
            for e in await runtime.sessions.read_session(session)
            if e.type == "context/input"
        )
        assert data["blocks"] == []
        assert any(
            e["kind"] == "memory" and e["reason"] == "budget-excluded" for e in data["exclusions"]
        )
