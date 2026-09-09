"""E2 checks the real Context owner with independent byte/token constraints."""

import json
from dataclasses import replace

import pytest

pytest.importorskip("tiktoken")

from test_context_input import _fixture, _persist, _policy, _redigest

from traceh.kernel.composition import RuntimeComposition
from traceh.llm.token_meter import RequestTokenMeter, TokenBudgetPolicy
from traceh.session.context_input import (
    ContextInputService,
    parse_context_input,
    read_context_input,
    render_context_message,
)


@pytest.mark.parametrize("body_expected", [False, True])
@pytest.mark.parametrize(
    "subject,narrow_window", [("中文设备约束", 2000), ("orbital telemetry", 1400)]
)
async def test_reference_admission_uses_tokens_without_changing_source_or_active_request(
    tmp_path, body_expected, subject, narrow_window
):
    sessions, kwargs = await _fixture(tmp_path, history=True, summary=(subject + " detail ") * 80)
    kwargs["composition"] = RuntimeComposition(
        provider="scripted",
        model="fixture-provider",
        system_prompt="Host policy",
        tools=(),
        max_output_tokens=256,
    ).snapshot()
    before = await sessions.read_session(kwargs["session_id"])
    meter = RequestTokenMeter(
        TokenBudgetPolicy(
            "cl100k_base",
            12000 if body_expected else narrow_window,
            256,
            256,
        )
    )
    snapshot = await ContextInputService(sessions.read_session, _policy()).freeze(
        **kwargs,
        token_meter=meter,
    )
    assert await sessions.read_session(kwargs["session_id"]) == before
    data = snapshot.to_dict()
    assert any(b["tier"] == "summary" for b in data["blocks"]) is body_expected
    assert "current input" in render_context_message(snapshot).content
    assert data["budget"]["remaining_bytes"] > 0
    measured = data["budget"]["token_measurement"]
    assert measured["status"] == "estimated"
    assert measured["input_tokens"] <= measured["input_limit"]
    if not body_expected:
        assert any(e["reason"] == "token-budget-excluded" for e in data["exclusions"])
    events, through_seq = await _persist(sessions, kwargs, snapshot)
    _, rebuilt = read_context_input(events, through_seq=through_seq, **kwargs)
    assert rebuilt == snapshot


@pytest.mark.parametrize("change", ["fixed-count", "fixed-fingerprint"])
async def test_forged_reference_allowance_cannot_rebind_the_original_surface(tmp_path, change):
    sessions, kwargs = await _fixture(tmp_path, history=True)
    kwargs["composition"] = RuntimeComposition(
        provider="scripted",
        model="fixture-provider",
        system_prompt="Host policy",
        tools=(),
        max_output_tokens=256,
    ).snapshot()
    snapshot = await ContextInputService(sessions.read_session, _policy()).freeze(
        **kwargs,
        token_meter=RequestTokenMeter(TokenBudgetPolicy("cl100k_base", 12000, 256, 256)),
    )
    data = snapshot.to_dict()
    measured = data["budget"]["token_measurement"]
    if change == "fixed-count":
        measured["fixed_input_tokens"] += 5
        measured["input_tokens"] += 5
        measured["reference_limit_tokens"] -= 5
    else:
        measured["fixed_input_fingerprint"] = "0" * 64
    forged = parse_context_input(_redigest(data))
    events, through_seq = await _persist(sessions, kwargs, forged)
    with pytest.raises(ValueError, match="context-token-source-mismatch"):
        read_context_input(events, through_seq=through_seq, **kwargs)


@pytest.mark.parametrize("window,body_expected", [(7000, False), (16000, True)])
async def test_explicit_skill_body_preserves_qualified_navigation_when_token_excluded(
    tmp_path, window, body_expected
):
    from retrieval_fixtures import build_case, context_policy, retrieval_policy, select
    from skill_fixtures import digest, policy
    from test_skill_navigation import navigation_skill

    from traceh.api.llm import ModelResponse, ToolCall
    from traceh.api.skills import SkillSectionContent
    from traceh.runtime.request_builder import verify_request_snapshots

    value = navigation_skill()
    body = "calibration observation detail. " * 900
    section = replace(
        value.descriptor.sections[0], content_digest=digest(body), content_bytes=len(body.encode())
    )
    value = replace(
        value,
        descriptor=replace(value.descriptor, sections=(section,), resources=()),
        sections=(SkillSectionContent(section.section_id, body),),
    )

    class Reader:
        name = "scripted"

        def __init__(self):
            self.requests = []

        async def complete(self, request):
            self.requests.append(request)
            blocks = json.loads(request.messages[-1].content.split("\n")[1])
            if len(self.requests) == 1:
                block = blocks[0]
                directory = json.loads(block["body"])
                return ModelResponse(
                    tool_calls=(
                        ToolCall(
                            "read-body",
                            "request_skill_reference",
                            {
                                "skill_id": block["id"],
                                "version": block["version"],
                                "catalog_digest": block["catalog_digest"],
                                "requested_tier": "section",
                                "section_id": directory["sections"][0]["section_id"],
                                "resource_id": None,
                                "chunk_id": None,
                            },
                        ),
                    )
                )
            return ModelResponse(content="Inspection finished.")

    reader = Reader()
    runtime, store, _, session, _ = await build_case(
        tmp_path,
        provider=reader,
        values=(value,),
        activation_policy=policy(max_content_bytes=50000),
        context=context_policy(
            total_bytes=60000,
            item_bytes=50000,
            skills=retrieval_policy(default_tier="directory", context_bytes=50000),
        ),
        config_changes={"token_budget": TokenBudgetPolicy("cl100k_base", window, 256, 256)},
    )
    try:
        await select(runtime, session, value)
        await runtime.skill_context.rebuild_index(session)
        result = await runtime.run_existing(session, "maintenance calibration")
        assert result.steps == 2
        blocks = json.loads(reader.requests[-1].messages[-1].content.split("\n")[1])
        assert len(blocks) == 1
        assert blocks[0]["tier"] == ("section" if body_expected else "directory")
        assert (blocks[0]["body"] == body) is body_expected
        events = await runtime.sessions.read_session(session)
        latest = [e.data for e in events if e.type == "context/input"][-1]
        assert (
            any(e["reason"] == "token-budget-excluded" for e in latest["exclusions"])
            is not body_expected
        )
        assert latest["budget"]["remaining_bytes"] > 0
        assert not await runtime.check_invariants(session)
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, session)
    finally:
        await runtime.dispose()
        await store.aclose()


async def test_fixed_product_and_active_tool_group_are_preserved_on_hard_refusal(tmp_path):
    from test_product_model_context import _snapshot_data, _task

    from traceh.api.llm import ModelResponse, ToolCall
    from traceh.api.product import ProductTaskStatus
    from traceh.api.tools import EffectKind, ToolOutput
    from traceh.llm.scripted import ScriptedLlmProvider
    from traceh.llm.token_meter import RequestTokenBudgetExceeded
    from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
    from traceh.session.product_context import PRODUCT_CONTEXT_SNAPSHOT, latest_product_context
    from traceh.session.sqlite import SqliteEventStore

    class Emit:
        name = "emit_active_records"
        description = "Emit active records and record execution."
        input_schema = {"type": "object", "properties": {}, "additionalProperties": False}
        effect_kind = EffectKind.WORKSPACE_WRITE

        async def execute(self, arguments, context):
            with (context.workspace / "active-executions.txt").open("a") as target:
                target.write("executed\n")
            return ToolOutput("active results. " * 1600)

    provider = ScriptedLlmProvider(
        (ModelResponse(tool_calls=(ToolCall("active-call", Emit.name, {}),)),)
    )
    store = SqliteEventStore(tmp_path / "events")
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path,
            max_tool_output_chars=50000,
            token_budget=TokenBudgetPolicy("cl100k_base", 5500, 256, 256),
        ),
        provider=provider,
        event_store=store,
        additional_tools=(Emit(),),
        include_default_tools=False,
    )
    try:
        session = await runtime.create_session(tmp_path)
        # A valid frozen host input, as emitted by ProductModelContext. This test
        # checks rendering/budget preservation, not approval or promotion execution.
        task = _task("pending-host-task", order=1, status=ProductTaskStatus.AWAITING_APPROVAL)
        await runtime.sessions.append_session(
            session,
            PRODUCT_CONTEXT_SNAPSHOT,
            _snapshot_data(session, task, (task,), 1),
            causation_id=task.source_event_id,
        )
        original = await runtime.sessions.read_session(session)
        product_messages = latest_product_context(original)[1].messages
        with pytest.raises(RequestTokenBudgetExceeded):
            await runtime.run_existing(session, "Emit the active records once.")
        assert len(provider.requests) == 1
        assert (tmp_path / "active-executions.txt").read_text() == "executed\n"
        events = await runtime.sessions.read_session(session)
        assert events[: len(original)] == original
        messages = runtime.surface.project(events)
        assert messages[: len(product_messages)] == product_messages
        assert any(m.content == "Emit the active records once." for m in messages)
        calls = [c for m in messages for c in m.tool_calls]
        replies = [m for m in messages if m.role == "tool"]
        assert len(calls) == len(replies) == 1 and calls[0].id == replies[0].tool_call_id
        assert replies[0].content == "active results. " * 1600
        contexts = [e.data for e in events if e.type == "context/input"]
        assert contexts[-1]["blocks"] == []
        assert contexts[-1]["budget"]["token_measurement"]["over_limit"]
        measurements = [
            e.data["measurement"] for e in events if e.type == "request/token-measurement"
        ]
        assert measurements[-1]["over_limit"] and measurements[-1]["parts"]["product"] > 0
        assert len([e for e in events if e.type == "request/snapshot"]) == 1
        assert not await runtime.check_invariants(session)
    finally:
        await runtime.dispose()
        await store.aclose()


@pytest.mark.parametrize("window,body_expected", [(5000, False), (16000, True)])
async def test_memory_token_admission_preserves_complete_fact_and_project_authority(
    tmp_path, window, body_expected
):
    from memory_fixtures import memory_policy
    from retrieval_fixtures import retrieval_policy
    from test_memory_context import DisclosureProvider, memory_case

    from traceh.api.json_types import canonical_json
    from traceh.projects.events import reference
    from traceh.runtime.request_builder import verify_request_snapshots

    body = "Approved inspection constraint. " * 900
    provider = DisclosureProvider("section")
    async with memory_case(
        tmp_path,
        provider=provider,
        tier="directory",
        body=body,
        authority_policy=memory_policy(max_body_bytes=50000),
        overrides={
            "total_bytes": 60000,
            "item_bytes": 50000,
            "memory": retrieval_policy(
                default_tier="directory", context_bytes=50000, max_corpus_bytes=200000
            ),
        },
        config_changes={"token_budget": TokenBudgetPolicy("cl100k_base", window, 256, 256)},
    ) as (runtime, _, _, session, proposal, activation):
        await runtime.memory.rebuild_index(session)
        turn = await runtime.run_existing(session, "context-fact")
        assert turn.steps == 2
        blocks = json.loads(provider.requests[-1].messages[-1].content.split("\n")[1])
        assert len(blocks) == 1
        assert blocks[0]["tier"] == ("section" if body_expected else "directory")
        assert (blocks[0]["body"] == body) is body_expected
        events = await runtime.sessions.read_session(session)
        latest = [e.data for e in events if e.type == "context/input"][-1]
        assert (
            any(e["reason"] == "token-budget-excluded" for e in latest["exclusions"])
            is not body_expected
        )
        assert latest["blocks"][0]["source_refs"][1] == reference(activation)
        unbound = await runtime.create_session(await runtime.sessions.workspace_for(session))
        await runtime.run_existing(unbound, "context-fact")
        assert body not in canonical_json(provider.requests[-1].to_dict())
        assert json.loads(provider.requests[-1].messages[-1].content.split("\n")[1]) == []
        await runtime.memory.revoke(
            session,
            memory_id="context-fact",
            fact_slot="project-boundaries",
            predecessor_ref=reference(activation),
            predecessor_digest=proposal.data["proposal_digest"],
            operation_id="revoke-token-fact",
            actor_id="host",
            expected_head=2,
        )
        await runtime.run_existing(session, "context-fact")
        assert json.loads(provider.requests[-1].messages[-1].content.split("\n")[1]) == []
        assert body not in canonical_json(provider.requests[-1].to_dict())
        assert not await runtime.check_invariants(session)
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, session)


# The narrow fixture includes the added active-search schema/instructions while
# still admitting only navigation; the original body must remain excluded.
@pytest.mark.parametrize("window,body_expected", [(5000, False), (16000, True)])
async def test_history_token_exclusion_keeps_directory_but_does_not_grant_retention(
    tmp_path, window, body_expected
):
    from test_history_runtime import SelectingProvider, items, policy, select_page
    from test_semantic_summary import history

    from traceh.api.llm import ModelResponse, ToolCall
    from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
    from traceh.runtime.request_builder import verify_request_snapshots
    from traceh.session.sqlite import SqliteEventStore

    session, before = await history(tmp_path)
    provider = SelectingProvider(
        [
            select_page,
            ModelResponse(tool_calls=(ToolCall("continue", "list_files", {}),)),
            ModelResponse(content="History inspection finished."),
        ]
    )
    store = SqliteEventStore(tmp_path / "events")
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path,
            context_input=policy(),
            token_budget=TokenBudgetPolicy("cl100k_base", window, 256, 256),
        ),
        provider=provider,
        event_store=store,
    )
    try:
        await runtime.compaction.replace_through(
            session,
            through_seq=before[-1].seq,
            summary="Earlier offline parser requirements.",
        )
        result = await runtime.run_existing(session, "Find original requirements.")
        assert result.steps == 3
        for request in provider.requests[1:]:
            blocks = items(request)
            assert len(blocks) == 1
            assert blocks[0]["tier"] == ("chunk" if body_expected else "directory")
            assert ("Earlier detail." in blocks[0]["body"]) is body_expected
        events = await runtime.sessions.read_session(session)
        contexts = [
            e.data
            for e in events
            if e.type == "context/input" and e.data["turn_id"] == result.turn_id
        ]
        assert (
            any(e["reason"] == "token-budget-excluded" for e in contexts[1]["exclusions"])
            is not body_expected
        )
        assert not await runtime.check_invariants(session)
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, session)
    finally:
        await runtime.dispose()
        await store.aclose()
