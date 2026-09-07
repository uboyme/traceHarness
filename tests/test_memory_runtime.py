"""Actual request/Tool/Lease entry points, without Memory retrieval ahead of F4."""

import asyncio

import pytest
from memory_fixtures import Resolver, bind, closed_source, config
from test_memory_convergence import ControlledStore

from traceh.api.json_types import canonical_json
from traceh.api.llm import ModelResponse, ToolCall
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.projects.events import reference
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.session.event_store import InMemoryEventStore


async def build(tmp_path, provider, *, store=None, defaults=True):
    root = tmp_path / "repository"
    root.mkdir(exist_ok=True)
    store = store if store is not None else InMemoryEventStore()
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "data", max_steps=3, memory=config(Resolver({"source-orion": root}))
        ),
        provider=provider,
        event_store=store,
        include_default_tools=defaults,
    )
    session_id = await runtime.create_session(root, session_id="requester")
    await bind(runtime.project_scope, session_id)
    return runtime, store, session_id


async def test_model_tool_proposes_but_next_step_has_no_approved_memory(tmp_path):
    arguments = {
        "proposal_id": "model-proposal",
        "body": "Preserve reproducible decisions.",
        "source_event_ids": [],
    }
    provider = ScriptedLlmProvider(
        (
            ModelResponse(
                tool_calls=(ToolCall("proposal-call", "propose_workspace_memory", arguments),)
            ),
            ModelResponse(content="The host can review the proposal."),
        )
    )
    runtime, store, session = await build(tmp_path, provider)
    try:
        leaf = await closed_source(runtime.sessions)
        arguments["source_event_ids"] = [str(leaf.event_id)]
        await runtime.run_existing(session, "Record the established goal for review.")
        view = await runtime.memory.read(session)
        assert len(view.proposals) == 1 and not view.active
        events = await runtime.sessions.read_session(session)
        result = next(e for e in events if e.type == "tool/result")
        assert result.data["status"] == "succeeded"
        assert result.data["data"]["status"] == "proposed"
        for event in events:
            if event.type == "context/input":
                assert all(b["kind"] != "memory" for b in event.data["blocks"])
        assert await store.head("memory:project-orion") == 1
        assert "approve_workspace_memory" not in canonical_json(provider.requests[0].to_dict())
    finally:
        await runtime.dispose()


@pytest.mark.parametrize(
    "extra",
    [
        {"project_id": "foreign"},
        {"fact_slot": "claim"},
        {"action": "approve"},
        {"sources": [{"kind": "host-declaration"}]},
    ],
)
async def test_model_cannot_smuggle_host_authority_through_tool(tmp_path, extra):
    args = {"proposal_id": "forged", "body": "Claim.", "source_event_ids": ["fake"], **extra}
    provider = ScriptedLlmProvider(
        (
            ModelResponse(tool_calls=(ToolCall("forge", "propose_workspace_memory", args),)),
            ModelResponse(content="done"),
        )
    )
    runtime, store, session = await build(tmp_path, provider)
    try:
        await runtime.run_existing(session, "review only")
        assert await store.head("memory:project-orion") == 0
        result = next(
            e for e in await runtime.sessions.read_session(session) if e.type == "tool/result"
        )
        assert result.data["status"] == "invalid"
    finally:
        await runtime.dispose()


async def test_config_does_not_grant_tool_when_default_tools_are_disabled(tmp_path):
    provider = ScriptedLlmProvider((ModelResponse(content="ordinary reply"),))
    runtime, _, session = await build(tmp_path, provider, defaults=False)
    try:
        await runtime.run_existing(session, "ordinary request")
        assert "propose_workspace_memory" not in canonical_json(provider.requests[0].to_dict())
    finally:
        await runtime.dispose()


async def test_host_approval_is_drain_owned_and_historical_request_stays_frozen(tmp_path):
    store = ControlledStore()
    provider = ScriptedLlmProvider((ModelResponse(content="ordinary reply"),))
    runtime, _, session = await build(tmp_path, provider, store=store)
    await runtime.run_existing(session, "ordinary request")
    events = await runtime.sessions.read_session(session)
    before = canonical_json(provider.requests[0].to_dict())
    proposal = await runtime.memory.declare(
        session,
        proposal_id="host-proposal",
        body="A durable host fact.",
        statement="A durable host fact.",
        declaration_id="declaration",
        operation_id="declare",
        actor_id="operator",
        expected_head=0,
    )
    store.kind = "memory/approved"
    approval = asyncio.create_task(
        runtime.memory.approve(
            session,
            proposal_ref=reference(proposal),
            proposal_digest=proposal.data["proposal_digest"],
            memory_id="host-memory",
            fact_slot="goal",
            operation_id="approve",
            actor_id="operator",
            expected_head=1,
        )
    )
    await store.entered.wait()
    disposal = asyncio.create_task(runtime.dispose())
    # The host operation retains its lease until its append has converged.
    approval.cancel()
    approval.cancel()
    assert not approval.done() and not disposal.done()
    store.release.set()
    with pytest.raises(asyncio.CancelledError):
        await approval
    await disposal
    assert store.finished.is_set()
    with pytest.raises(RuntimeError, match="disposed"):
        await runtime.memory.read(session)
    assert before == canonical_json(provider.requests[0].to_dict())
    assert [e.to_dict() for e in events] == [
        e.to_dict() for e in await runtime.sessions.read_session(session)
    ]
    assert await store.head("memory:project-orion") == 2
