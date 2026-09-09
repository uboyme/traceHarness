"""Precision contracts through public Runtime, authority and dispatched requests."""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from memory_fixtures import Resolver, approve, bind, declare, memory_policy
from retrieval_fixtures import build_case, context_policy, retrieval_policy, select
from skill_fixtures import contribution, policy, with_resource

from traceh.api.json_types import canonical_json, fingerprint
from traceh.api.llm import ModelResponse, ToolCall
from traceh.api.memory import ProjectMemoryConfig, ProjectScopeLimits
from traceh.api.skills import SkillResourceRoot
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.request_builder import reconstruct_request, verify_request_snapshots
from traceh.session.skill_requests import SKILL_TOOL_NAME


def skill(skill_id, summary):
    value = contribution("reference.author", skill_id, "Explicitly requested procedure text.")
    return replace(
        value,
        descriptor=replace(value.descriptor, title="Reference", summary=summary, tags=()),
    )


async def add_fact(runtime, session, memory_id, body):
    proposal = await declare(
        runtime.memory,
        session_id=session,
        proposal_id=f"proposal-{memory_id}",
        body=body,
    )
    return await approve(
        runtime.memory,
        proposal,
        session_id=session,
        memory_id=memory_id,
        fact_slot=f"slot-{memory_id}",
    )


@asynccontextmanager
async def precision_case(
    tmp_path, *, values, facts=(), context=None, provider=None, memory_limits=None
):
    config = ProjectMemoryConfig(
        ProjectScopeLimits(120, 100),
        memory_limits if memory_limits is not None else memory_policy(),
        Resolver({"precision-source": tmp_path}),
    )
    runtime, store, provider, session, _ = await build_case(
        tmp_path,
        values=values,
        context=context or context_policy(memory=retrieval_policy()),
        provider=provider,
        config_changes={"memory": config},
    )
    try:
        await bind(
            runtime.project_scope,
            session,
            project_id="precision-project",
            source_id="precision-source",
        )
        await select(runtime, session, *values)
        for memory_id, body in facts:
            await add_fact(runtime, session, memory_id, body)
        await runtime.skill_context.rebuild_index(session)
        await runtime.memory.rebuild_index(session)
        yield runtime, store, provider, session
    finally:
        await runtime.dispose()
        await store.aclose()


async def dispatched_context(runtime, provider, session):
    events = await runtime.sessions.read_session(session)
    contexts = [event for event in events if event.type == "context/input"]
    snapshots = [event for event in events if event.type == "request/snapshot"]
    assert len(snapshots) == len(provider.requests) > 0
    # Compare the actual Provider-bound wrapper, not only candidate scores or private state.
    assert isinstance(json.loads(provider.requests[-1].messages[-1].content.split("\n")[1]), list)
    rebuilt = await reconstruct_request(runtime.sessions, runtime.surface, session, snapshots[-1])
    assert rebuilt.request == provider.requests[-1]
    assert await verify_request_snapshots(runtime.sessions, runtime.surface, session) == ()
    return contexts[-1].data


def identities(data):
    return {(block["kind"], block["id"]) for block in data["blocks"]}


@pytest.mark.parametrize(
    "query",
    [
        "请根据传感器维护资料，对比停机标定与在线校准：分别列出流程代号。",
        "传感器维护资料:对比校准方式",
        "Notes: maintenance calibration",
    ],
)
async def test_prose_colon_does_not_turn_the_sentence_into_a_mandatory_code_anchor(tmp_path, query):
    value = skill(
        "maintenance.reference", "传感器维护资料 停机标定 在线校准 maintenance calibration"
    )
    async with precision_case(tmp_path, values=(value,)) as (runtime, _, provider, session):
        await runtime.skill_context.rebuild_index(session)
        await runtime.run_existing(session, query)
        data = await dispatched_context(runtime, provider, session)
        assert identities(data) == {("skill", "maintenance.reference")}


@pytest.mark.parametrize("literal", ["schema:revision", '"模型:关系"', '"notice:"'])
async def test_explicit_colon_literal_keeps_exact_anchor_semantics(tmp_path, literal):
    value = skill("notation.reference", literal)
    weak = skill("weak.reference", "schema revision 模型 关系 notice")
    async with precision_case(tmp_path, values=(value, weak)) as (runtime, _, provider, session):
        await runtime.skill_context.rebuild_index(session)
        await runtime.run_existing(session, literal)
        data = await dispatched_context(runtime, provider, session)
        assert identities(data) == {("skill", "notation.reference")}


@pytest.mark.parametrize(
    "query",
    [
        "renderer.removed",
        "Please inspect renderer.removed documentation",
        "renderer.active-",
        "renderer.active/",
        "renderer.active:",
        "renderer.active.",
    ],
)
async def test_unknown_complete_identifier_never_falls_back_to_components(tmp_path, query):
    value = skill("renderer.active", "renderer adapter documentation")
    async with precision_case(tmp_path, values=(value,)) as (runtime, _, provider, session):
        await runtime.run_existing(session, "renderer.active")
        known = await dispatched_context(runtime, provider, session)
        assert identities(known) == {("skill", "renderer.active")}
        await runtime.run_existing(session, query)
        events = await runtime.sessions.read_session(session)
        data = [event.data for event in events if event.type == "context/input"][-1]
        assert data["blocks"] == []
        assert json.loads(provider.requests[-1].messages[-1].content.split("\n")[1]) == []
        assert value.descriptor.summary not in provider.requests[-1].messages[-1].content
        assert await verify_request_snapshots(runtime.sessions, runtime.surface, session) == ()


@pytest.mark.parametrize(
    "query, expected", [("caching.", True), ('"caching."', False), ("`caching.`", False)]
)
async def test_sentence_period_and_quoted_terminal_period_have_distinct_meanings(
    tmp_path, query, expected
):
    value = skill("retention.handbook", "caching guidance")
    async with precision_case(tmp_path, values=(value,)) as (runtime, _, provider, session):
        await runtime.run_existing(session, query)
        data = await dispatched_context(runtime, provider, session)
        assert identities(data) == ({("skill", "retention.handbook")} if expected else set())


async def test_quoted_terminal_period_matches_its_complete_documented_literal(tmp_path):
    literal = skill("notation.handbook", 'The documented symbol is "caching."')
    prose = skill("retention.handbook", "caching guidance")
    async with precision_case(tmp_path, values=(literal, prose)) as (
        runtime,
        _,
        provider,
        session,
    ):
        await runtime.run_existing(session, '"caching."')
        data = await dispatched_context(runtime, provider, session)
        assert identities(data) == {("skill", "notation.handbook")}
        assert prose.descriptor.summary not in provider.requests[-1].messages[-1].content


async def test_multiple_complete_identifiers_preserve_each_requested_subject(tmp_path):
    first = skill("renderer.active", "Rendering method")
    second = skill("serializer.active", "Serialization method")
    noise = skill("glossary.catalog", "Inspect active adapters together")
    async with precision_case(tmp_path, values=(first, second, noise)) as (
        runtime,
        _,
        provider,
        session,
    ):
        await runtime.run_existing(
            session, "Inspect renderer.active and serializer.active together"
        )
        data = await dispatched_context(runtime, provider, session)
        assert identities(data) == {("skill", "renderer.active"), ("skill", "serializer.active")}
        assert noise.descriptor.summary not in provider.requests[-1].messages[-1].content


async def test_chinese_coverage_ignores_unmatched_politeness_without_injecting_weak_match(tmp_path):
    weak = skill("cache.handbook", "缓存部署指南")
    body = "缓存失效策略应该显式记录。"
    async with precision_case(tmp_path, values=(weak,), facts=(("memo.cedar", body),)) as (
        runtime,
        _,
        provider,
        session,
    ):
        await runtime.run_existing(session, "请帮忙解释缓存失效策略应该显式记录，谢谢")
        data = await dispatched_context(runtime, provider, session)
        assert identities(data) == {("memory", "memo.cedar")}
        assert body in provider.requests[-1].messages[-1].content
        assert weak.descriptor.summary not in provider.requests[-1].messages[-1].content
        assert any(
            e["kind"] == "skill" and e["reason"] == "query-dominated" for e in data["exclusions"]
        )
        assert data["retrieval"]["skill"]["fusion"]
        assert data["retrieval"]["memory"]["fusion"]


@pytest.mark.parametrize("equal", [True, False])
async def test_equal_and_complementary_coverage_preserve_multiple_sources(tmp_path, equal):
    first = skill("packing.handbook", "compression encryption" if equal else "compression")
    body = "compression encryption" if equal else "encryption"
    async with precision_case(tmp_path, values=(first,), facts=(("memo.birch", body),)) as (
        runtime,
        _,
        provider,
        session,
    ):
        await runtime.run_existing(session, "compression encryption")
        data = await dispatched_context(runtime, provider, session)
        assert identities(data) == {("skill", "packing.handbook"), ("memory", "memo.birch")}
        assert not any(e["reason"] == "query-dominated" for e in data["exclusions"])


async def test_over_budget_superset_does_not_suppress_a_smaller_useful_reference(tmp_path):
    small = skill("packing.handbook", "compression")
    context = context_policy(memory=retrieval_policy(), item_bytes=1400)
    # Make the raw fixture exceed the explicit limit independently of view overhead.
    body = "compression encryption " + "p" * context.item_bytes
    async with precision_case(
        tmp_path,
        values=(small,),
        facts=(("memo.willow", body),),
        context=context,
        memory_limits=memory_policy(max_body_bytes=len(body.encode("utf-8"))),
    ) as (runtime, _, provider, session):
        await runtime.run_existing(session, "compression encryption")
        data = await dispatched_context(runtime, provider, session)
        assert identities(data) == {("skill", "packing.handbook")}
        assert any(
            e["kind"] == "memory" and e["reason"] == "budget-excluded" for e in data["exclusions"]
        )
        assert not any(e["reason"] == "query-dominated" for e in data["exclusions"])
        assert json.loads(data["retrieval"]["fusion"][0]["identity"])[1] == "memo.willow"


async def test_unindexed_resource_literal_retains_exact_coverage_when_index_is_unavailable(
    tmp_path,
):
    relative = "manuals/Archive Guide (rev3)+notes.txt"
    raw = "Undisclosed resource text must remain absent."
    resource = tmp_path / relative
    resource.parent.mkdir()
    resource.write_text(raw, encoding="utf-8")
    value = with_resource(skill("archive.handbook", "Retention procedure"), relative, raw)
    runtime, store, provider, session, _ = await build_case(
        tmp_path,
        values=(value,),
        context=context_policy(skills=retrieval_policy(match_fields=("path",))),
        activation_policy=policy(SkillResourceRoot(value.descriptor.plugin, tmp_path)),
    )
    try:
        await select(runtime, session, value)
        # Deliberately no rebuild: this public path has always supported exact resource metadata.
        await runtime.run_existing(session, f'Please inspect "{relative}"')
        data = await dispatched_context(runtime, provider, session)
        assert identities(data) == {("skill", "archive.handbook")}
        receipt = data["retrieval"]["skill"]
        assert receipt["lanes"][0]["ranking"]
        assert receipt["lanes"][1]["status"] == "index-unavailable"
        assert receipt["coverage"][0]["terms"]
        assert raw not in canonical_json(provider.requests[-1].to_dict())
    finally:
        await runtime.dispose()
        await store.aclose()


async def test_rehashed_fabricated_query_coverage_is_rejected_by_session_read(
    tmp_path, monkeypatch
):
    value = skill("pigment.handbook", "cobalt")
    async with precision_case(tmp_path, values=(value,)) as (runtime, store, provider, session):
        await runtime.run_existing(session, "cobalt phantom")
        data = await dispatched_context(runtime, provider, session)
        assert identities(data) == {("skill", "pigment.handbook")}
        original = store.read
        reached = []

        async def read(stream_id, **kwargs):
            events = await original(stream_id, **kwargs)
            for event in events:
                if event.type != "context/input":
                    continue
                payload = event.data
                coverage = payload["retrieval"]["skill"]["coverage"]
                assert len(coverage) == 1 and coverage[0]["terms"] == ["cobalt"]
                reached.append(coverage[0]["identity"])
                # The forged term occurs in the query and keeps receipt/hash shape valid,
                # but it has no support in the complete Session's frozen Composition.
                coverage[0]["terms"] = ["cobalt", "phantom"]
                payload["context_digest"] = fingerprint(
                    {key: value for key, value in payload.items() if key != "context_digest"}
                )
            return events

        # Store.read returns detached envelopes: this tests the production reader's trust
        # boundary without corrupting the original successful stream or inventing a writer.
        with monkeypatch.context() as patch:
            patch.setattr(store, "read", read)
            with pytest.raises(ValueError, match="^retrieval-coverage-source-mismatch$"):
                await runtime.sessions.read_session(session)
        assert len(reached) == 1 and len(provider.requests) == 1
        restored = await dispatched_context(runtime, provider, session)
        assert restored == data


async def test_cancelled_precision_query_converges_before_returning(tmp_path, monkeypatch):
    value = skill("pigment.handbook", "cobalt")
    async with precision_case(tmp_path, values=(value,)) as (runtime, store, provider, session):
        entered, finished = asyncio.Event(), asyncio.Event()
        original = store.query_context_index

        async def query(corpus, terms):
            hits = await original(corpus, terms)
            assert hits
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

        monkeypatch.setattr(store, "query_context_index", query)
        task = asyncio.create_task(runtime.run_existing(session, "cobalt"))
        try:
            await asyncio.wait_for(entered.wait(), 10)
        finally:
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set() and provider.requests == []
        events = await runtime.sessions.read_session(session)
        assert not any(e.type in {"context/input", "request/snapshot"} for e in events)
        assert events[-1].type == "turn/end"


class GatedDisclosureProvider(ScriptedLlmProvider):
    def __init__(self):
        super().__init__((), repeat_last=True)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request):
        self.requests.append(request)
        if len(self.requests) != 1:
            return ModelResponse(content="done")
        block = json.loads(request.messages[-1].content.split("\n")[1])[0]
        self.entered.set()
        await self.release.wait()
        return ModelResponse(
            tool_calls=(
                ToolCall(
                    "read-procedure",
                    SKILL_TOOL_NAME,
                    {
                        "skill_id": block["id"],
                        "version": block["version"],
                        "catalog_digest": block["catalog_digest"],
                        "requested_tier": "section",
                        "section_id": "guide",
                        "resource_id": None,
                        "chunk_id": None,
                    },
                ),
            )
        )


async def test_explicit_disclosure_survives_a_new_automatic_superset(tmp_path):
    value = skill("packing.handbook", "compression")
    provider = GatedDisclosureProvider()
    async with precision_case(tmp_path, values=(value,), provider=provider) as (
        runtime,
        _,
        _,
        session,
    ):
        task = asyncio.create_task(runtime.run_existing(session, "compression encryption"))
        try:
            await asyncio.wait_for(provider.entered.wait(), 10)
            await add_fact(runtime, session, "memo.poplar", "compression encryption")
            await runtime.memory.rebuild_index(session)
        finally:
            provider.release.set()
        await task
        data = await dispatched_context(runtime, provider, session)
        assert len(provider.requests) == 2
        assert identities(data) == {("skill", "packing.handbook"), ("memory", "memo.poplar")}
        assert next(b for b in data["blocks"] if b["kind"] == "skill")["tier"] == "section"
        assert value.sections[0].body in provider.requests[-1].messages[-1].content
        events = await runtime.sessions.read_session(session)
        result = next(e for e in events if e.type == "tool/result")
        assert result.data["status"] == "succeeded"
        assert value.sections[0].body not in canonical_json(result.data)
