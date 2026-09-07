"""F2 model input follows selection, retrieval and request-only disclosure."""

import json
from dataclasses import replace

import pytest
from retrieval_fixtures import build_case, context_policy, retrieval_policy, sample_skills, select
from skill_fixtures import policy, with_resource

from traceh.api.json_types import canonical_json, fingerprint
from traceh.api.llm import ModelResponse, ToolCall
from traceh.api.skills import SkillResourceRoot
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.request_builder import reconstruct_request, verify_request_snapshots
from traceh.session.skill_requests import SKILL_TOOL_NAME
from traceh.session.skill_retrieval import tokenize


@pytest.mark.parametrize("query", ["boundary.notes", "模块所有权边界", "ＡＲＣＨＩＴＥＣＴＵＲＥ"])
async def test_selected_exact_and_fts_inputs_reconstruct_without_unselected_content(
    tmp_path, query
):
    runtime, store, provider, session, values = await build_case(tmp_path)
    try:
        await select(runtime, session, values[0])
        await runtime.skill_context.rebuild_index(session)
        await runtime.run_existing(session, query)
        events = await runtime.sessions.read_session(session)
        context = next(e for e in events if e.type == "context/input")
        assert [b["id"] for b in context.data["blocks"]] == ["boundary.notes"]
        assert all(e["reason"] != "index-unavailable" for e in context.data["exclusions"])
        rendered = canonical_json(provider.requests[0].to_dict())
        assert values[0].descriptor.summary in rendered
        assert values[1].descriptor.summary not in rendered
        assert values[0].sections[0].body not in rendered
        assert values[1].sections[0].body not in rendered
        assert await verify_request_snapshots(runtime.sessions, runtime.surface, session) == ()
        assert runtime.invariants.check(events) == ()
        original = next(e for e in events if e.type == "request/snapshot")
        await select(runtime, session, operation="clear", head=1)
        rebuilt = await reconstruct_request(runtime.sessions, runtime.surface, session, original)
        assert rebuilt.request == provider.requests[0]
    finally:
        await runtime.dispose()
        await store.aclose()


async def test_missing_index_is_explicit_and_exact_lane_still_operates(tmp_path):
    runtime, store, _, session, values = await build_case(tmp_path)
    try:
        await select(runtime, session, values[0])
        await runtime.run_existing(session, "boundary.notes")
        context = next(
            e for e in await runtime.sessions.read_session(session) if e.type == "context/input"
        )
        assert context.data["blocks"]
        assert any(e["reason"] == "index-unavailable" for e in context.data["exclusions"])
    finally:
        await runtime.dispose()
        await store.aclose()


@pytest.mark.parametrize("tier", ["directory", "summary", "section", "chunk"])
async def test_model_disclosure_uses_exact_next_step_and_never_persists_body_in_surface(
    tmp_path, tier
):
    from retrieval_fixtures import sample_skills

    values = sample_skills()
    resource = "关联资源 original bytes"
    (tmp_path / "reference.txt").write_text(resource, encoding="utf-8")
    first = with_resource(values[0], "reference.txt", resource)
    values = (first, values[1])
    digest = fingerprint([v.descriptor.to_dict() for v in values])
    args = {
        "skill_id": first.descriptor.skill_id,
        "version": first.descriptor.version,
        "catalog_digest": digest,
        "requested_tier": tier,
        "section_id": "guide" if tier == "section" else None,
        "resource_id": "reference" if tier == "chunk" else None,
        "chunk_id": "whole" if tier == "chunk" else None,
    }
    provider = ScriptedLlmProvider(
        (
            ModelResponse(tool_calls=(ToolCall("disclose", SKILL_TOOL_NAME, args),)),
            ModelResponse(content="done"),
            ModelResponse(content="new turn"),
        )
    )
    runtime, store, _, session, _ = await build_case(
        tmp_path,
        provider=provider,
        values=values,
        activation_policy=policy(SkillResourceRoot(first.descriptor.plugin, tmp_path)),
    )
    try:
        await select(runtime, session, first)
        await runtime.skill_context.rebuild_index(session)
        await runtime.run_existing(session, "boundary.notes")
        events = await runtime.sessions.read_session(session)
        contexts = [e for e in events if e.type == "context/input"]
        assert len(contexts) == 2
        assert contexts[1].data["blocks"][0]["tier"] == tier
        tool = next(e for e in events if e.type == "tool/result")
        assert tool.data["data"]["skill_receipt"]["target_rule"] == "immediate-next-step"
        assert first.sections[0].body not in canonical_json(tool.data)
        assert resource not in canonical_json(tool.data)
        assert await verify_request_snapshots(runtime.sessions, runtime.surface, session) == ()
        assert runtime.invariants.check(events) == ()
        await runtime.run_existing(session, "unrelated zzzzzz")
        last = canonical_json(provider.requests[-1].to_dict())
        assert first.sections[0].body not in last and resource not in last
        assert not json.loads(provider.requests[-1].messages[0].content.split("\n")[1])
    finally:
        await runtime.dispose()
        await store.aclose()


@pytest.mark.parametrize("limit", [0, 80])
async def test_atomic_budget_exclusion_records_reason(tmp_path, limit):
    context = context_policy(skills=retrieval_policy(skill_bytes=limit))
    runtime, store, provider, session, values = await build_case(tmp_path, context=context)
    try:
        await select(runtime, session, values[0])
        await runtime.skill_context.rebuild_index(session)
        await runtime.run_existing(session, "boundary.notes")
        event = next(
            e for e in await runtime.sessions.read_session(session) if e.type == "context/input"
        )
        assert event.data["blocks"] == []
        assert any(e["reason"] == "budget-excluded" for e in event.data["exclusions"])
        assert values[0].descriptor.summary not in canonical_json(provider.requests[0].to_dict())
    finally:
        await runtime.dispose()
        await store.aclose()


def test_unicode_tokenizer_han_bigrams_and_code_fragments():
    assert tokenize("边界 ＡＰＩ abc.py ERR_42") == (
        "边",
        "界",
        "边界",
        "api",
        "abc",
        "py",
        "err",
        "42",
    )
    with pytest.raises(ValueError, match="unicode-version"):
        replace(retrieval_policy(), unicode_version="not-this-runtime")


@pytest.mark.parametrize("extra", ["semantic", "reranker", "embedding_model"])
def test_unimplemented_retrieval_configuration_is_rejected(extra):
    from traceh.api.retrieval import SkillRetrievalPolicy

    with pytest.raises(ValueError, match="policy-unsupported"):
        SkillRetrievalPolicy.from_dict({**retrieval_policy().to_dict(), extra: "unauthorized"})


async def test_foreign_corpus_cannot_change_eligible_bm25_statistics(tmp_path):
    runtime, store, _, session, values = await build_case(tmp_path)
    try:
        await select(runtime, session, values[0])
        await runtime.skill_context.rebuild_index(session)
        await runtime.run_existing(session, "模块边界")
        before = next(
            e.data["retrieval"]
            for e in await runtime.sessions.read_session(session)
            if e.type == "context/input"
        )
        other = await runtime.create_session(tmp_path)
        await select(runtime, other, *values)
        await runtime.skill_context.rebuild_index(other)
        await runtime.run_existing(session, "模块边界")
        after = [
            e.data["retrieval"]
            for e in await runtime.sessions.read_session(session)
            if e.type == "context/input"
        ][-1]
        assert before["eligible_count"] == after["eligible_count"] == 1
        assert before == after
        assert after["lanes"][1]["ranking"]
    finally:
        await runtime.dispose()
        await store.aclose()


@pytest.mark.parametrize("query", ["zzzznonexistent", '" OR NOT * : () NEAR(unavailable)'])
async def test_no_hit_and_fts_syntax_are_literal_bounded_queries(tmp_path, query):
    runtime, store, _, session, values = await build_case(tmp_path)
    try:
        await select(runtime, session, values[0])
        await runtime.skill_context.rebuild_index(session)
        await runtime.run_existing(session, query)
        event = next(
            e for e in await runtime.sessions.read_session(session) if e.type == "context/input"
        )
        assert event.data["blocks"] == []
        assert any(e["reason"] == "no-hit" for e in event.data["exclusions"])
        assert all(lane["status"] == "available" for lane in event.data["retrieval"]["lanes"])
    finally:
        await runtime.dispose()
        await store.aclose()


async def test_candidate_limit_excludes_whole_lane(tmp_path):
    runtime, store, _, session, values = await build_case(
        tmp_path, context=context_policy(skills=retrieval_policy(max_candidates=1))
    )
    try:
        await select(runtime, session, *values)
        await runtime.skill_context.rebuild_index(session)
        await runtime.run_existing(session, "boundary.notes orbital.notes")
        event = next(
            e for e in await runtime.sessions.read_session(session) if e.type == "context/input"
        )
        assert event.data["blocks"] == []
        assert all(lane["status"] == "resource-limit" for lane in event.data["retrieval"]["lanes"])
        assert any(e["reason"] == "resource-limit" for e in event.data["exclusions"])
    finally:
        await runtime.dispose()
        await store.aclose()


@pytest.mark.parametrize(
    "tiers",
    [("directory", "summary"), ("summary", "directory"), ("summary", "summary")],
)
async def test_mixed_skill_tiers_share_next_step_and_identical_requests_coalesce(tmp_path, tiers):
    values = sample_skills()
    descriptor = values[0].descriptor
    args = {
        "skill_id": descriptor.skill_id,
        "version": descriptor.version,
        "catalog_digest": fingerprint([v.descriptor.to_dict() for v in values]),
        "section_id": None,
        "resource_id": None,
        "chunk_id": None,
    }
    provider = ScriptedLlmProvider(
        (
            ModelResponse(
                tool_calls=tuple(
                    ToolCall(f"disclose-{i}", SKILL_TOOL_NAME, {**args, "requested_tier": tier})
                    for i, tier in enumerate(tiers)
                )
            ),
            ModelResponse(content="done"),
            ModelResponse(content="new turn"),
        )
    )
    runtime, store, _, session, _ = await build_case(tmp_path, provider=provider, values=values)
    try:
        await select(runtime, session, values[0])
        await runtime.skill_context.rebuild_index(session)
        await runtime.run_existing(session, descriptor.skill_id)
        events = await runtime.sessions.read_session(session)
        results = [e for e in events if e.type == "tool/result"]
        assert len(results) == len(tiers)
        assert all(e.data["status"] == "succeeded" for e in results)
        contexts = [e for e in events if e.type == "context/input"]
        assert len(contexts) == len(provider.requests) == 2
        assert [b["tier"] for b in contexts[1].data["blocks"]] == list(dict.fromkeys(tiers))
        rendered = json.loads(provider.requests[1].messages[0].content.split("\n")[1])
        assert [item["tier"] for item in rendered] == list(dict.fromkeys(tiers))
        assert await verify_request_snapshots(runtime.sessions, runtime.surface, session) == ()
        assert runtime.invariants.check(events) == ()
        await runtime.run_existing(session, "unrelated zzzzzz")
        assert json.loads(provider.requests[-1].messages[0].content.split("\n")[1]) == []
    finally:
        await runtime.dispose()
        await store.aclose()


@pytest.mark.parametrize(
    "relative", ["manual pages.txt", "handbook/Quick Start (v2)+manual.txt", "资料/索引 清单.txt"]
)
@pytest.mark.parametrize(
    "query_pattern,expected",
    [
        ("{}", True),
        ('Please read "{}"', True),
        ("prefix{}", False),
        ("{}.bak", False),
        ("archive/{}", False),
    ],
)
async def test_exact_resource_path_preserves_complete_literal_and_boundaries(
    tmp_path, relative, query_pattern, expected
):
    values = sample_skills()
    body = "RESOURCE BYTES ARE NOT SEARCH METADATA"
    resource = tmp_path / relative
    resource.parent.mkdir(parents=True, exist_ok=True)
    resource.write_text(body, encoding="utf-8")
    first = with_resource(values[0], relative, body)
    runtime, store, provider, session, _ = await build_case(
        tmp_path,
        values=(first, values[1]),
        context=context_policy(skills=retrieval_policy(match_fields=("path",))),
        activation_policy=policy(SkillResourceRoot(first.descriptor.plugin, tmp_path)),
    )
    try:
        await select(runtime, session, first)
        await runtime.skill_context.rebuild_index(session)
        await runtime.run_existing(session, query_pattern.format(relative.upper()))
        events = await runtime.sessions.read_session(session)
        context = next(e for e in events if e.type == "context/input")
        exact, fts = context.data["retrieval"]["lanes"]
        assert exact["status"] == fts["status"] == "available"
        assert fts["ranking"] == []
        assert bool(exact["ranking"]) is expected
        assert [b["id"] for b in context.data["blocks"]] == (
            [first.descriptor.skill_id] if expected else []
        )
        if not expected:
            assert any(e["reason"] == "no-hit" for e in context.data["exclusions"])
        assert body not in canonical_json(provider.requests[0].to_dict())
        assert await verify_request_snapshots(runtime.sessions, runtime.surface, session) == ()
        assert runtime.invariants.check(events) == ()
    finally:
        await runtime.dispose()
        await store.aclose()
