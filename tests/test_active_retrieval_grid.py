"""Offline fixture/runner contracts; real scored Provider calls require explicit CLI."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from live_active_retrieval.audit import dispatched_evidence
from live_active_retrieval.contract import load_manifest
from live_active_retrieval.fixtures import materialize
from live_active_retrieval.grid import (
    answer_matches,
    prepare_runtime,
    run_case,
    setup_source,
    usage,
)
from test_history_runtime import SelectingProvider

from traceh.api.llm import ModelResponse, ToolCall
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.request_builder import verify_request_snapshots

MANIFEST = Path(__file__).parent / "live_active_retrieval/manifest.json"


def test_fixture_bytes_repeat_and_values_stay_out_of_questions_and_navigation():
    manifest = load_manifest(MANIFEST)
    fixtures = materialize(manifest)
    assert fixtures == materialize(manifest) and len(fixtures) == 72
    assert len({f["identity"] for f in fixtures}) == 72
    for fixture in fixtures:
        assert fixture["question"] == next(
            c["question"] for c in manifest["cases"] if c["id"] == fixture["id"]
        )
        if fixture["value"]:
            assert fixture["value"] not in fixture["question"]
        if fixture["family"] == "skill":
            assert len(fixture["sections"]) == 31
            assert [s["id"] for s in fixture["sections"]] == sorted(
                s["id"] for s in fixture["sections"]
            )
            for item in fixture["sections"] + fixture["resources"]:
                if fixture["value"]:
                    assert fixture["value"] not in item["title"] + item["summary"]
        if fixture["family"] == "output":
            assert len(fixture["output_text"].splitlines()) == 600


@pytest.mark.parametrize(
    "case_id",
    [
        "h-direct",
        "s-large-directory",
        "s-resource",
        "m-direct",
        "m-superseded",
        "m-revoked",
        "o-nonzero",
    ],
)
async def test_frozen_sources_prepare_on_original_owners_without_network(tmp_path, case_id):
    manifest = load_manifest(MANIFEST)
    fixture = next(f for f in materialize(manifest) if f["id"] == case_id)
    responses = (
        [
            ModelResponse(
                tool_calls=(ToolCall("execute", "shell", {"command": "python check.py"}),)
            ),
            ModelResponse(content="done"),
        ]
        if fixture["family"] == "output"
        else [ModelResponse(content="noted")]
    )
    provider = ScriptedLlmProvider(tuple(responses), repeat_last=True)
    runtime, store, session, scope, value = await prepare_runtime(
        tmp_path, fixture, {"manifest": manifest}, provider, "fixture-model"
    )
    try:
        await setup_source(runtime, session, scope, value, fixture, manifest)
        assert not scope.setup
        if fixture["family"] == "output":
            assert (tmp_path / "workspace/execution-count.txt").read_text() == "1"
            events = await runtime.sessions.read_session(session)
            assert (
                sum(e.type == "tool/call" and e.data["tool_name"] == "shell" for e in events) == 1
            )
        elif fixture["family"] == "memory":
            view = await runtime.memory.read(session)
            facts = [item for item in view.active if item.memory_id == fixture["memory_id"]]
            if fixture["mode"] == "revoked":
                assert not facts
            else:
                assert len(facts) == 1 and facts[0].body == fixture["source"]
        assert not await runtime.check_invariants(session)
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, session)
    finally:
        await runtime.dispose()
        await store.aclose()


def test_number_scoring_does_not_accept_substrings_and_negatives_require_review():
    assert answer_matches(
        "批准为 25 人", {"value": "25", "value_type": "number", "expected": "value"}
    )
    assert not answer_matches(
        "125 人", {"value": "25", "value_type": "number", "expected": "value"}
    )
    assert not answer_matches("没有资料", {"value": "", "expected": "no-evidence"})
    # Expected values are not emitted as runner-generated model instructions.
    assert "{value}" not in json.dumps(
        [f["question"] for f in materialize(load_manifest(MANIFEST))]
    )


async def test_output_runner_scores_actual_retained_evidence_and_one_execution(tmp_path):
    manifest = load_manifest(MANIFEST)
    fixture = next(f for f in materialize(manifest) if f["id"] == "o-direct")

    def search_output(request):
        listing = next(
            json.loads(m.content)
            for m in reversed(request.messages)
            if m.role == "tool" and '"outputs"' in m.content
        )
        ref = listing["outputs"][0]["output_ref"]
        return ModelResponse(
            tool_calls=(
                ToolCall(
                    "search",
                    "search_tool_output",
                    {"effect_id": ref["effect_id"], "digest": ref["digest"], "query": "交接校验码"},
                ),
            )
        )

    provider = SelectingProvider(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall("execute", "shell", {"command": "python check.py"}),
                    ToolCall("duplicate", "shell", {"command": "python check.py"}),
                )
            ),
            ModelResponse(content="done"),
            ModelResponse(tool_calls=(ToolCall("list", "list_tool_outputs", {}),)),
            search_output,
            ModelResponse(content=fixture["value"]),
        ]
    )
    report = await run_case(
        tmp_path / "case", fixture, {"manifest": manifest}, provider, "fixture-model"
    )
    assert report["closed"] and report["provisional_joint_pass"], report
    assert report["output_executions"] == 1 and report["evidence"]
    events = json.loads((tmp_path / "case/source-events.json").read_text(encoding="utf-8"))
    assert any(
        e["type"] == "tool/result"
        and e["data"]["tool_call_id"] == "duplicate"
        and e["data"]["status"] == "denied"
        for e in events
    )
    assert dispatched_evidence(events, report, fixture)
    # An executed search is insufficient if its evidence never reached a successful Attempt.
    failed = deepcopy(events)
    for event in failed:
        if event["type"] == "model/attempt-end" and event["seq"] > report["target_start_seq"]:
            event["data"]["status"] = "failed"
    assert not dispatched_evidence(failed, report, fixture)
    hidden = deepcopy(events)
    for event in hidden:
        if event["type"] == "request/snapshot":
            for message in event["data"]["dispatch_request"]["messages"]:
                if message.get("tool_call_id") == "search":
                    message["content"] = "Receipt only; body was not admitted."
    assert not dispatched_evidence(hidden, report, fixture)


async def test_correct_answer_without_reading_fact_is_not_evidence(tmp_path):
    manifest = load_manifest(MANIFEST)
    fixture = next(f for f in materialize(manifest) if f["id"] == "m-direct")
    provider = ScriptedLlmProvider((ModelResponse(content=fixture["value"]),))
    report = await run_case(
        tmp_path / "case", fixture, {"manifest": manifest}, provider, "fixture-model"
    )
    assert report["closed"] and not report.get("error") and not report.get("audit_error"), report
    assert not report["evidence"] and not report["provisional_joint_pass"]
    events = json.loads((tmp_path / "case/source-events.json").read_text(encoding="utf-8"))
    assert not dispatched_evidence(events, report, fixture)


def test_unknown_usage_is_not_counted_as_reported_zero():
    result = usage(
        [
            {
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "quality": "unknown",
                }
            },
            {"usage": None},
        ]
    )
    assert result["attempts"] == 2 and result["unknown_usage"] == 2
