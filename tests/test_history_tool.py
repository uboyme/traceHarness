"""History requests traverse the leased ToolRuntime and next-Step freeze."""

from __future__ import annotations

import asyncio
import json
from dataclasses import fields, replace

import pytest
from test_history_requests import ControlledProvider, disclosed, open_host_step

from traceh.api.events import PendingEvent
from traceh.api.history import HistoryCursor
from traceh.api.json_types import canonical_json, fingerprint
from traceh.api.llm import ModelResponse, ToolCall
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.session.context_input import parse_context_input, render_context_message
from traceh.session.event_store import InMemoryEventStore
from traceh.session.history_requests import (
    HISTORY_TOOL_NAME,
    HistoryRequestError,
    validate_history_request_events,
    validate_user_requests,
)
from traceh.session.recovery import RecoveryService
from traceh.session.service import SessionService
from traceh.tools.history import HistoryDisclosureTool


async def execute_request(runtime, provider, sid, request):
    provider.response = ModelResponse(
        tool_calls=(ToolCall("history-call", HISTORY_TOOL_NAME, request.to_dict()),),
    )
    return await runtime.run_existing(sid, "request that disclosed page")


async def test_tool_returns_only_receipt_and_next_step_gets_raw_once(tmp_path):
    runtime, provider, sid, _, request = await disclosed(tmp_path)
    try:
        outcome = await execute_request(runtime, provider, sid, request)
        assert outcome.steps == 2
        events = await runtime.sessions.read_session(sid)
        receipt_event = next(event for event in events if event.type == "tool/result")
        receipt = receipt_event.data["data"]["history_receipt"]
        assert receipt_event.data["status"] == "succeeded"
        assert set(receipt_event.data["data"]) == {"history_receipt"}
        assert "old telemetry" not in canonical_json(receipt_event.to_dict())
        assert len(receipt_event.data["content"]) < 256
        assert receipt["source_step_id"] != next(
            e.data["step_id"]
            for e in events
            if e.type == "context/input" and any(b["tier"] == "section" for b in e.data["blocks"])
        )
        assert "old telemetry" in provider.requests[-1].messages[0].content
        assert all(
            "old telemetry" not in message.content for message in runtime.surface.project(events)
        )
        await runtime.run_existing(sid, "later turn")
        assert "old telemetry" not in provider.requests[-1].messages[0].content
        assert runtime.invariants.check(await runtime.sessions.read_session(sid)) == ()
    finally:
        await runtime.dispose()


async def test_undisclosed_cursor_fails_tool_without_raw_or_accepted_receipt(tmp_path):
    runtime, provider, sid, policy, request = await disclosed(tmp_path)
    try:
        wrong = replace(request, cursor=HistoryCursor(request.block_id, policy.digest, 1))
        await execute_request(runtime, provider, sid, wrong)
        events = await runtime.sessions.read_session(sid)
        result = next(e for e in events if e.type == "tool/result")
        assert result.data["status"] == "failed"
        assert "history_receipt" not in result.data["data"]
        assert "old telemetry" not in provider.requests[-1].messages[0].content
        assert runtime.invariants.check(events) == ()
    finally:
        await runtime.dispose()


@pytest.mark.parametrize(
    "field,value",
    [
        ("session_id", "unrelated-session"),
        ("turn_id", "unrelated-turn"),
        ("source_step_id", "unrelated-step"),
        ("tool_call_id", "unrelated-call"),
    ],
)
async def test_receipt_rejects_wrong_identity_even_without_consuming_context(
    tmp_path, field, value
):
    runtime, provider, sid, _, request = await disclosed(tmp_path)
    try:
        await execute_request(runtime, provider, sid, request)
        events = await runtime.sessions.read_session(sid)
        result = next(e for e in events if e.type == "tool/result")
        forged_receipt = {**result.data["data"]["history_receipt"], field: value}
        forged = replace(result, data={**result.data, "data": {"history_receipt": forged_receipt}})
        # A terminal/failure prefix may never freeze a target Context. The
        # accepted receipt still has to prove all three identity layers.
        prefix = (*events[: result.seq - 1], forged)
        with pytest.raises(HistoryRequestError):
            validate_history_request_events(prefix)
    finally:
        await runtime.dispose()


@pytest.mark.parametrize("change", ["tool-name", "arguments", "failed-result"])
async def test_receipt_cannot_grant_authority_from_unrelated_tool_or_arguments(tmp_path, change):
    runtime, provider, sid, _, request = await disclosed(tmp_path)
    try:
        await execute_request(runtime, provider, sid, request)
        events = list(await runtime.sessions.read_session(sid))
        result = next(e for e in events if e.type == "tool/result")
        if change == "failed-result":
            events[result.seq - 1] = replace(result, data={**result.data, "status": "failed"})
        else:
            call = next(e for e in events if e.type == "tool/call")
            change_data = (
                {"tool_name": "another-tool"}
                if change == "tool-name"
                else {
                    "arguments": {**call.data["arguments"], "requested_tier": "chunk"},
                }
            )
            events[call.seq - 1] = replace(call, data={**call.data, **change_data})
        with pytest.raises(HistoryRequestError):
            validate_history_request_events(tuple(events[: result.seq]))
    finally:
        await runtime.dispose()


async def test_max_steps_receipt_expires_without_next_context_and_recovery_reexecution(tmp_path):
    runtime, provider, sid, _, request = await disclosed(tmp_path)
    try:
        runtime.loop.max_steps = 1
        outcome = await execute_request(runtime, provider, sid, request)
        assert outcome.reason == "max_steps_exceeded"
        events = await runtime.sessions.read_session(sid)
        assert any(e.type == "tool/result" and "history_receipt" in e.data["data"] for e in events)
        assert not any(
            e.type == "context/input" and any(b["tier"] == "section" for b in e.data["blocks"])
            for e in events
        )
        assert not (await RecoveryService(runtime.sessions).recover(sid)).changed
        await runtime.run_existing(sid, "a new turn")
        assert "old telemetry" not in provider.requests[-1].messages[0].content
    finally:
        await runtime.dispose()


async def test_crash_recovery_restores_receipt_from_effect_without_replaying_or_reusing_it(
    tmp_path,
):
    runtime, provider, sid, _, request = await disclosed(tmp_path)
    recovered_runtime = None
    try:
        await execute_request(runtime, provider, sid, request)
        events = await runtime.sessions.read_session(sid)
        effects = await runtime.sessions.read_effects(sid)
        result = next(e for e in events if e.type == "tool/result")
        # Persist the actual pre-result crash prefix and the real successful
        # effect outcome in another Store, preserving every evidence identity.
        store = InMemoryEventStore()
        for stream, source in (
            (runtime.sessions.session_stream(sid), events[: result.seq - 1]),
            (runtime.sessions.effect_stream(sid), effects),
        ):
            await store.append(
                stream,
                expected_seq=0,
                events=tuple(
                    PendingEvent(
                        **{field.name: getattr(event, field.name) for field in fields(PendingEvent)}
                    )
                    for event in source
                ),
            )
        sessions = SessionService(store)
        before_effects = await sessions.read_effects(sid)
        report = await RecoveryService(sessions).recover(sid)
        assert report.synthesized_tool_results == 1
        assert report.closed_step and report.closed_turn
        recovered = await sessions.read_session(sid)
        repaired = next(e for e in recovered if e.type == "tool/result")
        assert repaired.data["data"]["history_receipt"] == result.data["data"]["history_receipt"]
        assert repaired.data["error_type"] == "RecoveredAfterCrash"
        validate_history_request_events(recovered)
        assert await sessions.read_effects(sid) == before_effects
        later_provider = ControlledProvider()
        recovered_runtime = build_default_runtime(
            RuntimeConfig(
                data_dir=tmp_path / "recovered", context_input=runtime.config.context_input
            ),
            provider=later_provider,
            event_store=store,
        )
        await recovered_runtime.run_existing(sid, "new turn after crash")
        assert "old telemetry" not in later_provider.requests[-1].messages[0].content
        assert await sessions.read_effects(sid) == before_effects
        assert recovered_runtime.invariants.check(await sessions.read_session(sid)) == ()
    finally:
        if recovered_runtime is not None:
            await recovered_runtime.dispose()
        await runtime.dispose()


async def test_cancelled_tool_request_closes_without_accepted_receipt_or_next_step(
    tmp_path, monkeypatch
):
    runtime, provider, sid, _, request = await disclosed(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    original = HistoryDisclosureTool.execute

    async def gated(self, arguments, context):
        entered.set()
        await release.wait()
        return await original(self, arguments, context)

    monkeypatch.setattr(HistoryDisclosureTool, "execute", gated)
    try:
        task = asyncio.create_task(execute_request(runtime, provider, sid, request))
        await asyncio.wait_for(entered.wait(), 5)
        assert await runtime.cancel(sid, reason="cancel History request fixture")
        with pytest.raises(asyncio.CancelledError):
            await task
        events = await runtime.sessions.read_session(sid)
        result = next(e for e in events if e.type == "tool/result")
        assert result.data["status"] == "cancelled"
        assert "history_receipt" not in result.data["data"]
        assert events[-1].type == "turn/end"
        assert runtime.invariants.check(events) == ()
    finally:
        release.set()
        await runtime.dispose()


async def test_oversized_page_cannot_be_forged_into_accepted_failure_prefix(tmp_path):
    runtime, provider, sid, _, request = await disclosed(tmp_path, policy_changes={"page_bytes": 1})
    try:
        await execute_request(runtime, provider, sid, request)
        events = await runtime.sessions.read_session(sid)
        result = next(e for e in events if e.type == "tool/result")
        call = next(e for e in events if e.type == "tool/call")
        assert result.data["status"] == "failed"
        assert "history_receipt" not in result.data["data"]
        forged = replace(
            result,
            data={
                **result.data,
                "status": "succeeded",
                "error_type": None,
                "data": {
                    "history_receipt": {
                        "format": 1,
                        "status": "accepted",
                        "session_id": sid,
                        "turn_id": call.data["turn_id"],
                        "source_step_id": call.data["step_id"],
                        "tool_call_id": call.data["tool_call_id"],
                        **request.to_dict(),
                        "target_rule": "immediate-next-step",
                    }
                },
            },
        )
        with pytest.raises(HistoryRequestError, match="history-page-resource-limit"):
            validate_history_request_events((*events[: result.seq - 1], forged))
    finally:
        await runtime.dispose()


@pytest.mark.parametrize("change", [None, "source-ref", "request-ref"])
async def test_next_cursor_disclosure_requires_verified_raw_page_sources(tmp_path, change):
    runtime, provider, sid, policy, request = await disclosed(
        tmp_path,
        policy_changes={"page_messages": 2},
        history_turns=2,
    )
    try:
        await execute_request(runtime, provider, sid, request)
        scope = await open_host_step(runtime.sessions, sid)
        events = list(await runtime.sessions.read_session(sid))
        context = next(e for e in reversed(events) if e.type == "context/input")
        data = json.loads(canonical_json(context.data))
        raw = next(block for block in data["blocks"] if block["tier"] == "section")
        next_request = replace(
            request,
            cursor=HistoryCursor.from_dict(
                raw["provenance"]["page"]["next_cursor"],
            ),
        )
        if change is not None:
            ref = (
                raw["source_refs"][0]
                if change == "source-ref"
                else raw["provenance"]["request_ref"]
            )
            ref["digest"] = "f" * 64
            data["context_digest"] = fingerprint(
                {k: v for k, v in data.items() if k != "context_digest"}
            )
            snapshot = parse_context_input(data)
            events[context.seq - 1] = replace(context, data=data)
            frozen = next(
                e
                for e in events
                if e.type == "request/snapshot" and e.data["context_input_seq"] == context.seq
            )
            frozen_data = json.loads(canonical_json(frozen.data))
            frozen_data["context_input_digest"] = snapshot.context_digest
            for key in ("composed", "dispatch"):
                request_data = frozen_data[f"{key}_request"]
                request_data["metadata"]["context_input_digest"] = snapshot.context_digest
                request_data["messages"][0] = render_context_message(snapshot).to_dict()
                frozen_data[f"{key}_fingerprint"] = fingerprint(request_data)
            events[frozen.seq - 1] = replace(frozen, data=frozen_data)
            with pytest.raises(HistoryRequestError, match="history-disclosure-invalid"):
                validate_user_requests(
                    tuple(events),
                    session_id=sid,
                    **scope,
                    requests=(next_request,),
                    policy=policy,
                )
        else:
            payloads = validate_user_requests(
                tuple(events),
                session_id=sid,
                **scope,
                requests=(next_request,),
                policy=policy,
            )
            assert payloads[0]["cursor"] == next_request.cursor.to_dict()
    finally:
        await runtime.dispose()
