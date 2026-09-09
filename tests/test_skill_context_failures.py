"""F2 public-path counterexamples, source observations, retry and cancellation."""

import asyncio
from dataclasses import replace

import pytest
from plugin_fixtures import ScriptedPlugin, manifest
from retrieval_fixtures import build_case, context_policy, sample_skills, select
from skill_fixtures import discovery

from traceh.api.json_types import canonical_json, fingerprint
from traceh.api.llm import ModelResponse, ToolCall
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.plugins.skills import LeasedSkillReader
from traceh.runtime.request_builder import reconstruct_request
from traceh.runtime.skill_context import SkillContextControl
from traceh.session.context_input import parse_context_input
from traceh.session.service import SessionService
from traceh.session.skill_requests import SKILL_TOOL_NAME
from traceh.session.sqlite import SqliteEventStore


def request_for(values, index=0, **changes):
    descriptor = values[index].descriptor
    return {
        "skill_id": descriptor.skill_id,
        "version": descriptor.version,
        "catalog_digest": fingerprint([v.descriptor.to_dict() for v in values]),
        "requested_tier": "section",
        "section_id": "guide",
        "resource_id": None,
        "chunk_id": None,
        **changes,
    }


@pytest.mark.parametrize(
    "variant", ["unselected", "wrong-version", "wrong-catalog", "wrong-section"]
)
async def test_model_cannot_bypass_selection_or_descriptor_identity(tmp_path, variant):
    values = sample_skills()
    changes = {
        "unselected": {},
        "wrong-version": {"version": "9.0.0"},
        "wrong-catalog": {"catalog_digest": "a" * 64},
        "wrong-section": {"section_id": "absent"},
    }
    args = request_for(values, 1 if variant == "unselected" else 0, **changes[variant])
    provider = ScriptedLlmProvider(
        (
            ModelResponse(tool_calls=(ToolCall("bad", SKILL_TOOL_NAME, args),)),
            ModelResponse(content="done"),
        )
    )
    runtime, store, _, session, _ = await build_case(tmp_path, provider=provider)
    try:
        await select(runtime, session, values[0])
        await runtime.skill_context.rebuild_index(session)
        await runtime.run_existing(session, "boundary.notes")
        events = await runtime.sessions.read_session(session)
        result = next(e for e in events if e.type == "tool/result")
        assert result.data["status"] == "failed"
        assert all(
            b["tier"] == "summary"
            for e in events
            if e.type == "context/input"
            for b in e.data["blocks"]
        )
        assert values[1].descriptor.summary not in canonical_json(provider.requests[-1].to_dict())
        assert len(await runtime.sessions.read_skill_selection(session)) == 1
    finally:
        await runtime.dispose()
        await store.aclose()


async def test_resource_drift_fails_before_next_model_dispatch(tmp_path, monkeypatch):
    values = sample_skills()
    provider = ScriptedLlmProvider(
        (
            ModelResponse(tool_calls=(ToolCall("read", SKILL_TOOL_NAME, request_for(values)),)),
            ModelResponse(content="must not dispatch"),
        )
    )
    runtime, store, _, session, _ = await build_case(tmp_path, provider=provider)
    try:
        await select(runtime, session, values[0])
        await runtime.skill_context.rebuild_index(session)
        monkeypatch.setattr(LeasedSkillReader, "read_section", lambda *_: b"changed raw bytes")
        with pytest.raises(ValueError, match="content-mismatch"):
            await runtime.run_existing(session, "boundary.notes")
        events = await runtime.sessions.read_session(session)
        assert len(provider.requests) == 1
        assert sum(e.type == "context/input" for e in events) == 1
        assert events[-1].type == "turn/end"
    finally:
        await runtime.dispose()
        await store.aclose()


async def test_reload_marks_previous_selection_stale_until_host_reconfirms(tmp_path):
    runtime, store, provider, session, values = await build_case(tmp_path)
    try:
        await select(runtime, session, values[0])
        await runtime.skill_context.rebuild_index(session)
        await runtime.run_existing(session, "boundary.notes")
        original = next(
            e for e in await runtime.sessions.read_session(session) if e.type == "request/snapshot"
        )
        changed = replace(
            values[0], descriptor=replace(values[0].descriptor, summary="changed summary")
        )
        plugin = ScriptedPlugin(manifest("reference.author"), skills=(changed, values[1]))
        await runtime.replace_plugin_composition(
            ("reference.author",), plugin_discovery=discovery(plugin)
        )
        await runtime.run_existing(session, "boundary.notes")
        event = [
            e for e in await runtime.sessions.read_session(session) if e.type == "context/input"
        ][-1]
        assert event.data["blocks"] == []
        assert any(e["reason"] == "stale-selection" for e in event.data["exclusions"])
        await select(runtime, session, changed, operation="reconfirm", head=1)
        await runtime.skill_context.rebuild_index(session)
        await runtime.run_existing(session, "boundary.notes")
        assert "changed summary" in provider.requests[-1].messages[-1].content
        rebuilt = await reconstruct_request(runtime.sessions, runtime.surface, session, original)
        assert rebuilt.request == provider.requests[0]
    finally:
        await runtime.dispose()
        await store.aclose()


async def test_selection_change_during_query_never_substitutes_latest_source(tmp_path, monkeypatch):
    runtime, store, provider, session, values = await build_case(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    original = store.query_context_index

    async def query(corpus, terms):
        entered.set()
        await release.wait()
        return await original(corpus, terms)

    try:
        await select(runtime, session, values[0])
        await runtime.skill_context.rebuild_index(session)
        monkeypatch.setattr(store, "query_context_index", query)
        task = asyncio.create_task(runtime.run_existing(session, "boundary.notes"))
        await entered.wait()
        await select(runtime, session, values[1], operation="change", head=1)
        release.set()
        await task
        event = next(
            e for e in await runtime.sessions.read_session(session) if e.type == "context/input"
        )
        assert event.data["blocks"] == []
        assert any(
            e["kind"] == "skill" and e["reason"] == "source-unavailable"
            for e in event.data["exclusions"]
        )
        assert values[1].descriptor.summary not in canonical_json(provider.requests[0].to_dict())
        await runtime.skill_context.rebuild_index(session)
        await runtime.run_existing(session, "orbital.notes")
        assert values[1].descriptor.summary in provider.requests[-1].messages[-1].content
    finally:
        release.set()
        await runtime.dispose()
        await store.aclose()


async def test_cancelled_context_query_converges_without_context_or_model_call(
    tmp_path, monkeypatch
):
    runtime, store, provider, session, values = await build_case(tmp_path)
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def query(*_):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    try:
        await select(runtime, session, values[0])
        monkeypatch.setattr(store, "query_context_index", query)
        task = asyncio.create_task(runtime.run_existing(session, "boundary.notes"))
        await entered.wait()
        await runtime.cancel(session)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()
        events = await runtime.sessions.read_session(session)
        assert not any(e.type in {"context/input", "request/snapshot"} for e in events)
        assert provider.requests == [] and events[-1].type == "turn/end"
    finally:
        await runtime.dispose()
        await store.aclose()


async def test_control_rejects_foreign_store_with_same_session_identity(tmp_path):
    runtime, store, _, session, values = await build_case(tmp_path)
    foreign = SqliteEventStore(tmp_path / "foreign")
    other = SessionService(foreign)
    try:
        await other.create_session(tmp_path, session_id=session)
        control = SkillContextControl(other, runtime.loop.compositions, context_policy().skills)
        with pytest.raises(ValueError, match="owner-mismatch"):
            await control.select(
                session,
                operation_id="foreign",
                expected_head=0,
                actor_id="host",
                skills=({"skill_id": values[0].descriptor.skill_id, "version": "1.0.0"},),
            )
        assert await other.read_skill_selection(session) == ()
    finally:
        await runtime.dispose()
        await store.aclose()
        await foreign.aclose()


async def test_frozen_lane_receipt_rejects_changed_fusion_even_with_new_context_digest(tmp_path):
    runtime, store, _, session, values = await build_case(tmp_path)
    try:
        await select(runtime, session, values[0])
        await runtime.skill_context.rebuild_index(session)
        await runtime.run_existing(session, "boundary.notes")
        data = next(
            e for e in await runtime.sessions.read_session(session) if e.type == "context/input"
        ).data
        assert data["retrieval"]["skill"]["lanes"][1]["status"] == "available"
        data["retrieval"]["fusion"][0]["numerator"] += 1
        data["context_digest"] = fingerprint(
            {k: v for k, v in data.items() if k != "context_digest"}
        )
        with pytest.raises(ValueError, match="context-retrieval-fusion-mismatch"):
            parse_context_input(data)
    finally:
        await runtime.dispose()
        await store.aclose()


async def test_retry_retains_frozen_selection_without_requery(tmp_path, monkeypatch):
    from test_model_retry import FakeScheduler, OutcomeProvider, retry_policy

    from traceh.api.llm import Usage, UsageQuality
    from traceh.llm.failures import ProviderFailure, ProviderFailureCategory

    scheduler = FakeScheduler()
    scheduler.sleep_release = asyncio.Event()
    provider = OutcomeProvider(
        scheduler,
        (
            ProviderFailure(
                "temporary-dns", ProviderFailureCategory.DNS, usage=Usage(0, 0, UsageQuality.EXACT)
            ),
            ModelResponse(content="done"),
        ),
    )
    runtime, store, _, session, values = await build_case(
        tmp_path,
        provider=provider,
        config_changes={"model_retry_policy": retry_policy()},
        runtime_options={"retry_scheduler": scheduler.scheduler()},
    )
    original, queries = store.query_context_index, []

    async def query(corpus, terms):
        queries.append(corpus.key)
        return await original(corpus, terms)

    try:
        await select(runtime, session, values[0])
        await runtime.skill_context.rebuild_index(session)
        monkeypatch.setattr(store, "query_context_index", query)
        task = asyncio.create_task(runtime.run_existing(session, "boundary.notes"))
        await scheduler.sleep_entered.wait()
        await select(runtime, session, values[1], operation="retry-change", head=1)
        scheduler.sleep_release.set()
        await task
        assert len(queries) == 1
        assert provider.requests[0] == provider.requests[1]
        assert values[0].descriptor.summary in canonical_json(provider.requests[1].to_dict())
        assert values[1].descriptor.summary not in canonical_json(provider.requests[1].to_dict())
        events = await runtime.sessions.read_session(session)
        assert sum(e.type == "context/input" for e in events) == 1
        assert sum(e.type == "model/attempt-start" for e in events) == 2
        assert runtime.invariants.check(events) == ()
    finally:
        scheduler.sleep_release.set()
        await runtime.dispose()
        await store.aclose()


async def test_last_step_receipt_expires_at_turn_boundary(tmp_path):
    values = sample_skills()
    provider = ScriptedLlmProvider(
        (
            ModelResponse(
                tool_calls=(ToolCall("last-step", SKILL_TOOL_NAME, request_for(values)),)
            ),
            ModelResponse(content="done"),
        )
    )
    runtime, store, _, session, _ = await build_case(
        tmp_path, provider=provider, config_changes={"max_steps": 1}
    )
    try:
        await select(runtime, session, values[0])
        await runtime.skill_context.rebuild_index(session)
        await runtime.run_existing(session, "boundary.notes")
        events = await runtime.sessions.read_session(session)
        tool = next(e for e in events if e.type == "tool/result")
        assert tool.data["status"] == "succeeded" and "skill_receipt" in tool.data["data"]
        assert len(provider.requests) == 1
        await runtime.run_existing(session, "boundary.notes")
        assert values[0].sections[0].body not in canonical_json(provider.requests[1].to_dict())
        assert all(
            b["tier"] == "summary"
            for e in await runtime.sessions.read_session(session)
            if e.type == "context/input"
            for b in e.data["blocks"]
        )
    finally:
        await runtime.dispose()
        await store.aclose()


async def test_historical_request_ignores_missing_index_and_changed_resource(tmp_path, monkeypatch):
    import sqlite3

    values = sample_skills()
    provider = ScriptedLlmProvider(
        (
            ModelResponse(
                tool_calls=(ToolCall("read-history", SKILL_TOOL_NAME, request_for(values)),)
            ),
            ModelResponse(content="done"),
        )
    )
    runtime, store, _, session, _ = await build_case(tmp_path, provider=provider)
    try:
        await select(runtime, session, values[0])
        await runtime.skill_context.rebuild_index(session)
        await runtime.run_existing(session, "boundary.notes")
        snapshots = [
            e for e in await runtime.sessions.read_session(session) if e.type == "request/snapshot"
        ]
        with sqlite3.connect(store.path) as connection:
            connection.execute("DELETE FROM context_index_manifest")

        async def forbidden(*_):
            raise AssertionError("historical reconstruction must not query current index")

        def changed(*_):
            raise AssertionError("historical reconstruction must not read current resources")

        monkeypatch.setattr(store, "query_context_index", forbidden)
        monkeypatch.setattr(LeasedSkillReader, "read_section", changed)
        rebuilt = await reconstruct_request(
            runtime.sessions, runtime.surface, session, snapshots[1]
        )
        assert rebuilt.request == provider.requests[1]
        assert values[0].sections[0].body in canonical_json(rebuilt.request.to_dict())
    finally:
        await runtime.dispose()
        await store.aclose()


async def test_failed_tool_receipt_cannot_authorize_next_step_body(tmp_path, monkeypatch):
    values = sample_skills()
    provider = ScriptedLlmProvider(
        (
            ModelResponse(
                tool_calls=(ToolCall("failed-result", SKILL_TOOL_NAME, request_for(values)),)
            ),
            ModelResponse(content="forbidden"),
        )
    )
    runtime, store, _, session, _ = await build_case(tmp_path, provider=provider)
    original, captured = runtime.sessions.append_session, []

    async def append(session_id, event_type, data, **kwargs):
        if event_type == "tool/result" and "skill_receipt" in data.get("data", {}):
            captured.append(data)
            data = {**data, "status": "failed", "error_type": "InjectedFailure"}
        return await original(session_id, event_type, data, **kwargs)

    try:
        await select(runtime, session, values[0])
        await runtime.skill_context.rebuild_index(session)
        monkeypatch.setattr(runtime.sessions, "append_session", append)
        with pytest.raises(ValueError, match="skill-receipt-not-successful"):
            await runtime.run_existing(session, "boundary.notes")
        assert len(captured) == 1 and captured[0]["status"] == "succeeded"
        assert len(provider.requests) == 1
        events = await runtime.sessions.read_session(session)
        assert sum(e.type == "context/input" for e in events) == 1
        assert events[-1].type == "turn/end"
    finally:
        await runtime.dispose()
        await store.aclose()


async def test_duplicate_skill_block_is_rejected_with_recomputed_budget_and_digest(tmp_path):
    runtime, store, _, session, values = await build_case(tmp_path)
    try:
        await select(runtime, session, values[0])
        await runtime.skill_context.rebuild_index(session)
        await runtime.run_existing(session, values[0].descriptor.skill_id)
        events = await runtime.sessions.read_session(session)
        data = next(e for e in events if e.type == "context/input").to_dict()["data"]
        assert len(data["blocks"]) == 1
        assert parse_context_input(data).to_dict() == data
        data["blocks"].append(dict(data["blocks"][0]))
        # A second identical rendered item adds its bytes and one JSON array comma.
        budget = data["budget"]
        extra = budget["kind_bytes"]["skill"] + 1
        budget["kind_bytes"]["skill"] *= 2
        budget["body_bytes"] *= 2
        budget["rendered_bytes"] += extra
        budget["remaining_bytes"] -= extra
        data["context_digest"] = fingerprint(
            {key: value for key, value in data.items() if key != "context_digest"}
        )
        with pytest.raises(ValueError, match="^context-source-duplicate$"):
            parse_context_input(data)
    finally:
        await runtime.dispose()
        await store.aclose()
