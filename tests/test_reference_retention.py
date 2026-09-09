"""Public Runtime serial-read probe. Synthetic Provider; never real-model evidence."""

import json
from dataclasses import replace

import pytest
from retrieval_fixtures import build_case, context_policy, retrieval_policy, select
from test_skill_navigation import navigation_skill

from traceh.api.json_types import canonical_json
from traceh.api.llm import ModelResponse, ToolCall
from traceh.runtime.request_builder import verify_request_snapshots


def reference(request):
    return request.messages[-1]


class SerialReader:
    name = "scripted"

    def __init__(self):
        self.requests = []
        self.navigation = None
        self.handle = None

    async def complete(self, request):
        self.requests.append(request)
        step = len(self.requests)
        blocks = json.loads(reference(request).content.split("\n")[1])
        if step == 1:
            block = next(b for b in blocks if b["kind"] == "skill")
            self.navigation = json.loads(block["body"])["sections"]
            self.handle = {
                "skill_id": block["id"],
                "version": block["version"],
                "catalog_digest": block["catalog_digest"],
                "resource_id": None,
                "chunk_id": None,
            }
        if step in (1, 2):
            return ModelResponse(
                tool_calls=(
                    ToolCall(
                        f"read-{step}",
                        "request_skill_reference",
                        {
                            **self.handle,
                            "requested_tier": "section",
                            "section_id": self.navigation[step - 1]["section_id"],
                        },
                    ),
                )
            )
        bodies = {b["body"] for b in blocks if b["tier"] == "section"}
        return ModelResponse(content="both present" if len(bodies) == 2 else "one or none")


@pytest.mark.parametrize("limit,expected", [(4, "both present"), (1, "one or none")])
async def test_serial_bodies_are_retained_only_within_the_same_turn_and_budget(
    tmp_path, limit, expected
):
    value = navigation_skill()
    # Sections are the test subject; no unconsumed resource root is needed.
    value = replace(value, descriptor=replace(value.descriptor, resources=()))
    provider = SerialReader()
    runtime, store, _, session, _ = await build_case(
        tmp_path,
        values=(value,),
        provider=provider,
        context=replace(
            context_policy(skills=retrieval_policy(default_tier="directory")), max_blocks=limit
        ),
    )
    try:
        await select(runtime, session, value)
        await runtime.skill_context.rebuild_index(session)
        turn = await runtime.run_existing(session, "maintenance calibration")
        assert turn.final_text == expected
        events = await runtime.sessions.read_session(session)
        last = [e for e in events if e.type == "context/input"][-1]
        bodies = [b for b in last.data["blocks"] if b["tier"] == "section"]
        assert len(bodies) == (2 if limit == 4 else 1)
        assert "OFFLINE BODY 731" in reference(provider.requests[-1]).content
        for event in events:
            if event.type in {"assistant/message", "tool/result"}:
                assert "REPLACEMENT ONLY 829" not in canonical_json(event.data)
                assert "OFFLINE BODY 731" not in canonical_json(event.data)
        assert runtime.invariants.check(events) == ()
        assert await verify_request_snapshots(runtime.sessions, runtime.surface, session) == ()
        await runtime.run_existing(session, "unrelated")
        current = reference(provider.requests[-1]).content
        assert "REPLACEMENT ONLY 829" not in current
        assert "OFFLINE BODY 731" not in current
    finally:
        await runtime.dispose()
        await store.aclose()


async def test_deselection_breaks_retention_even_if_host_reselects_in_the_same_turn(tmp_path):
    value = navigation_skill()
    value = replace(value, descriptor=replace(value.descriptor, resources=()))

    class HostChangesSelection(SerialReader):
        async def complete(self, request):
            step = len(self.requests) + 1
            if step == 1:
                return await super().complete(request)
            self.requests.append(request)
            if step == 2:
                assert "REPLACEMENT ONLY 829" in reference(request).content
                await select(runtime, session, operation="clear", head=1)
            elif step == 3:
                assert json.loads(reference(request).content.split("\n")[1]) == []
                await select(runtime, session, value, operation="reselect", head=2)
                await runtime.skill_context.rebuild_index(session)
            else:
                return ModelResponse(content="old disclosure did not revive")
            return ModelResponse(tool_calls=(ToolCall(f"list-{step}", "list_files", {}),))

    provider = HostChangesSelection()
    runtime, store, _, session, _ = await build_case(
        tmp_path,
        values=(value,),
        provider=provider,
        context=context_policy(skills=retrieval_policy(default_tier="directory")),
    )
    try:
        await select(runtime, session, value)
        await runtime.skill_context.rebuild_index(session)
        result = await runtime.run_existing(session, "maintenance calibration")
        assert result.final_text == "old disclosure did not revive"
        assert len(provider.requests) == 4
        final_reference = reference(provider.requests[-1]).content
        blocks = json.loads(final_reference.split("\n")[1])
        assert "REPLACEMENT ONLY 829" not in final_reference
        assert any(b["tier"] == "directory" for b in blocks)
        assert await verify_request_snapshots(runtime.sessions, runtime.surface, session) == ()
    finally:
        await runtime.dispose()
        await store.aclose()


@pytest.mark.parametrize("limit", [1, 4])
async def test_sequential_history_pages_keep_both_or_evict_without_revival(tmp_path, limit):
    from test_history_runtime import SelectingProvider, items, policy, select_page

    from traceh.api.history import HistoryCursor, HistoryPageRequest
    from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
    from traceh.session.sqlite import SqliteEventStore

    def second_page(request):
        first = next(b for b in items(request) if b["tier"] == "chunk")
        cursor = HistoryCursor.from_dict(first["read_action"]["arguments"]["cursor"])
        assert cursor.index == 1
        return ModelResponse(
            tool_calls=(
                ToolCall(
                    "page-two",
                    "request_history_page",
                    HistoryPageRequest(cursor.block_id, cursor, "chunk").to_dict(),
                ),
            )
        )

    provider = SelectingProvider(
        [
            ModelResponse(content="first historical answer"),
            ModelResponse(content="second historical answer"),
            select_page,
            second_page,
            ModelResponse(tool_calls=(ToolCall("continue", "list_files", {}),)),
            ModelResponse(content="comparison finished"),
            ModelResponse(content="new turn"),
        ]
    )
    context = policy(max_blocks=limit, history=replace(policy().history, page_messages=2))
    store = SqliteEventStore(tmp_path / "events")
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data", context_input=context),
        provider=provider,
        event_store=store,
    )
    try:
        first = await runtime.run(tmp_path, "HISTORICAL ALPHA 147")
        session = first.session_id
        await runtime.run_existing(session, "HISTORICAL BETA 258")
        events = await runtime.sessions.read_session(session)
        await runtime.compaction.replace_through(
            session,
            through_seq=events[-1].seq,
            summary="Two earlier records",
        )
        turn = await runtime.run_existing(session, "Compare the two historical records")
        assert turn.steps == 4
        for request in provider.requests[-2:]:
            pages = [b for b in items(request) if b["tier"] == "chunk"]
            assert len(pages) == (2 if limit == 4 else 1)
            # The assertions below verify the actual visible page bodies; durable
            # page identities remain independently checked by replay/invariants.
            assert "HISTORICAL BETA 258" in reference(request).content
            assert ("HISTORICAL ALPHA 147" in reference(request).content) == (limit == 4)
        events = await runtime.sessions.read_session(session)
        assert runtime.invariants.check(events) == ()
        assert "HISTORICAL ALPHA 147" not in canonical_json(
            [message.to_dict() for message in runtime.surface.project(events)]
        )
        await runtime.run_existing(session, "Continue with a different task")
        assert all(b["tier"] == "directory" for b in items(provider.requests[-1]))
        assert await verify_request_snapshots(runtime.sessions, runtime.surface, session) == ()
    finally:
        await runtime.dispose()
        await store.aclose()


@pytest.mark.parametrize("tier", ["summary", "section"])
@pytest.mark.parametrize("revoke", [False, True])
async def test_memory_rechecks_active_authority_after_body_was_already_admitted(
    tmp_path, tier, revoke
):
    from test_memory_context import BODY, DisclosureProvider, memory_case

    from traceh.projects.events import reference as event_reference

    class HostRevokes(DisclosureProvider):
        async def complete(self, request):
            if not self.requests:
                return await super().complete(request)
            self.requests.append(request)
            if len(self.requests) == 2:
                assert BODY in reference(request).content
                if revoke:
                    await runtime.memory.revoke(
                        session,
                        memory_id="context-fact",
                        fact_slot="project-boundaries",
                        predecessor_ref=event_reference(activation),
                        predecessor_digest=proposal.data["proposal_digest"],
                        operation_id="revoke-after-admission",
                        actor_id="host",
                        expected_head=2,
                    )
                return ModelResponse(tool_calls=(ToolCall("continue", "list_files", {}),))
            return ModelResponse(content="active authority respected")

    provider = HostRevokes(tier)
    async with memory_case(tmp_path, provider=provider, tier="directory") as (
        runtime,
        _,
        _,
        session,
        proposal,
        activation,
    ):
        await runtime.memory.rebuild_index(session)
        result = await runtime.run_existing(session, "context-fact")
        assert result.steps == 3 and result.final_text == "active authority respected"
        assert (BODY in reference(provider.requests[-1]).content) is not revoke
        events = await runtime.sessions.read_session(session)
        assert runtime.invariants.check(events) == ()
        assert await verify_request_snapshots(runtime.sessions, runtime.surface, session) == ()


@pytest.mark.parametrize("unit", ["bytes", "tokens"])
async def test_new_skill_body_precedes_retained_history_under_shared_budget(tmp_path, unit):
    from skill_fixtures import digest
    from skill_fixtures import policy as skill_policy
    from test_history_runtime import SelectingProvider, items, policy, select_page

    from traceh.api.skills import SkillSectionContent
    from traceh.llm.token_meter import TokenBudgetPolicy

    value = navigation_skill()
    body = "CURRENT GUIDANCE " + ("unit info. " * 1100 if unit == "tokens" else "k" * 7000)
    section = replace(
        value.descriptor.sections[0],
        content_digest=digest(body),
        content_bytes=len(body),
    )
    value = replace(
        value,
        descriptor=replace(value.descriptor, sections=(section,), resources=()),
        sections=(SkillSectionContent(section.section_id, body),),
    )

    def read_skill(request):
        blocks = items(request)
        assert any(b["kind"] == "history" and b["tier"] == "chunk" for b in blocks)
        directory = next(b for b in blocks if b["kind"] == "skill")
        target = json.loads(directory["body"])["sections"][0]["section_id"]
        return ModelResponse(
            tool_calls=(
                ToolCall(
                    "read-skill",
                    "request_skill_reference",
                    {
                        "skill_id": directory["id"],
                        "version": directory["version"],
                        "catalog_digest": directory["catalog_digest"],
                        "requested_tier": "section",
                        "section_id": target,
                        "resource_id": None,
                        "chunk_id": None,
                    },
                ),
            )
        )

    provider = SelectingProvider(
        [
            ModelResponse(content="historical answer"),
            select_page,
            read_skill,
            ModelResponse(tool_calls=(ToolCall("continue", "list_files", {}),)),
            ModelResponse(content="current requested source retained"),
        ]
    )
    context = policy(
        skills=retrieval_policy(default_tier="directory"),
        total_bytes=10000,
        history_bytes=9000,
        item_bytes=9000,
        max_blocks=2,
    )
    if unit == "tokens":
        context = replace(
            context,
            total_bytes=80000,
            history_bytes=40000,
            item_bytes=40000,
            max_blocks=10,
            skills=retrieval_policy(default_tier="directory", context_bytes=40000),
        )
    runtime, store, _, session, _ = await build_case(
        tmp_path,
        values=(value,),
        provider=provider,
        context=context,
        activation_policy=skill_policy(max_content_bytes=40000),
        config_changes={"token_budget": TokenBudgetPolicy("cl100k_base", 7800, 256, 256)}
        if unit == "tokens"
        else None,
    )
    try:
        await runtime.run_existing(
            session,
            "OLDER OBSERVATION " + ("log entry. " * 350 if unit == "tokens" else "h" * 1500),
        )
        events = await runtime.sessions.read_session(session)
        await runtime.compaction.replace_through(
            session,
            through_seq=events[-1].seq,
            summary="Earlier observation",
        )
        await select(runtime, session, value)
        await runtime.skill_context.rebuild_index(session)
        result = await runtime.run_existing(session, "maintenance calibration")
        assert result.steps == 4
        for request in provider.requests[-2:]:
            blocks = items(request)
            assert blocks[0]["kind"] == "skill" and blocks[0]["body"] == body
            assert not any(b["kind"] == "history" and b["tier"] == "chunk" for b in blocks)
        events = await runtime.sessions.read_session(session)
        if unit == "tokens":
            assert any(
                item["reason"] == "token-budget-excluded" and item["kind"] == "history"
                for e in events
                if e.type == "context/input"
                for item in e.data["exclusions"]
            )
        assert runtime.invariants.check(events) == ()
        assert await verify_request_snapshots(runtime.sessions, runtime.surface, session) == ()
    finally:
        await runtime.dispose()
        await store.aclose()
