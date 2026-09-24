"""Diagnose real native journeys without changing their scoring or forcing more reads."""

import json
from dataclasses import replace

import pytest
from test_retrieval_episode_evaluator import NavigatingProvider, runner, selected

from traceh.api.json_types import fingerprint
from traceh.api.llm import ModelResponse, ToolCall
from traceh.evaluation.evaluators.episode_diagnostics import observe_episode
from traceh.evaluation.review import assess_run, export_review
from traceh.session.service import SessionService
from traceh.session.sqlite import SqliteEventStore


@pytest.mark.frozen_unicode
@pytest.mark.parametrize("case_id", ["h-direct", "m-direct"])
async def test_complete_search_excerpt_is_evidence_without_a_read(tmp_path, case_id):
    report = await runner(tmp_path, case_id).run()
    packet = report.task_report.to_dict()["episodes"][0]
    diagnostic = packet["retrieval_diagnostics"]
    assert diagnostic["candidate"]["status"] == "observed"
    assert diagnostic["evidence"]["status"] == "observed"
    assert not diagnostic["read_requests"]
    assert diagnostic["search_coverage"]
    assert diagnostic["gap"] == "evidence-dispatched"
    assert report.trials[0].assessment.value == "pending_review"


@pytest.mark.frozen_unicode
async def test_correct_guess_after_directory_is_still_missing_body_evidence(tmp_path):
    case = selected("s-direct")
    report = await runner(tmp_path, "s-direct", NavigatingProvider(case, guess=True)).run()
    packet = report.task_report.to_dict()["episodes"][0]
    diagnostic = packet["retrieval_diagnostics"]
    assert packet["provisional_answer_match"]
    assert diagnostic["candidate"]["status"] == "observed"
    assert diagnostic["evidence"]["status"] == "not_observed"
    assert diagnostic["gap"] == "candidate-without-evidence"
    assert not diagnostic["read_requests"]
    assert "candidate-without-evidence" in (tmp_path / "run/report.md").read_text(encoding="utf-8")


@pytest.mark.frozen_unicode
@pytest.mark.parametrize("invalid", [True, False])
async def test_failed_read_and_successful_wrong_section_are_not_body_delivery(tmp_path, invalid):
    case = selected("s-direct")

    class WrongRead(NavigatingProvider):
        changed = False

        async def complete(self, request):
            if self.changed:
                return ModelResponse(content="The requested fact has not been established.")
            response = await super().complete(request)
            if response.tool_calls and response.tool_calls[0].name == "request_skill_reference":
                call = response.tool_calls[0]
                sections = self.case.data["setup"]["descriptor"]["sections"]
                other = next(
                    s["section_id"]
                    for s in sections
                    if s["section_id"] != call.arguments["section_id"]
                )
                self.changed = True
                return ModelResponse(
                    tool_calls=(
                        ToolCall(
                            call.id,
                            call.name,
                            {
                                **call.arguments,
                                "section_id": "not-an-authorized-section" if invalid else other,
                            },
                        ),
                    )
                )
            return response

    provider = WrongRead(case)
    report = await runner(tmp_path, "s-direct", provider).run()
    diagnostic = report.task_report.to_dict()["episodes"][0]["retrieval_diagnostics"]
    assert provider.changed and report.complete
    assert diagnostic["gap"] == "candidate-without-evidence"
    assert len(diagnostic["read_requests"]) == 1
    read = diagnostic["read_requests"][0]
    assert read["status"] == ("failed" if invalid else "succeeded")
    assert read["result_seq"] > read["call_seq"]
    assert diagnostic["evidence"]["status"] == "not_observed"


@pytest.mark.frozen_unicode
async def test_failed_model_attempt_does_not_count_its_prepared_directory(tmp_path):
    seen = []

    class FailedProvider:
        name = "diagnostic-failure-fixture"

        async def complete(self, request):
            seen.append(request)
            raise OSError("deliberate transport failure")

    report = await runner(tmp_path, "s-direct", FailedProvider()).run()
    packet = report.task_report.to_dict()["episodes"][0]
    diagnostic = packet["retrieval_diagnostics"]
    assert seen and any(
        selected("s-direct").data["expectation"]["reference_id"] in m.content
        for m in seen[0].messages
    )
    assert diagnostic["candidate"]["status"] == diagnostic["evidence"]["status"] == "unknown"
    assert diagnostic["successful_answer_dispatches"] == 0
    assert report.trials[0].assessment.value == "unassessable"


@pytest.mark.frozen_unicode
@pytest.mark.parametrize(
    "answer", ["The entire manual has no such fact.", "当前可见资料未找到这项信息。"]
)
async def test_negative_wording_never_becomes_a_second_automatic_scorer(tmp_path, answer):
    class AnswerProvider:
        name = "negative-scope-fixture"

        async def complete(self, request):
            return ModelResponse(content=answer)

    report = await runner(tmp_path, "s-absent", AnswerProvider()).run()
    diagnostic = report.task_report.to_dict()["episodes"][0]["retrieval_diagnostics"]
    assert diagnostic["negative_scope_review_required"]
    assert diagnostic["candidate"]["status"] == diagnostic["evidence"]["status"] == "not_applicable"
    assert not diagnostic["search_coverage"]
    assert report.trials[0].assessment.value == "pending_review"


@pytest.mark.frozen_unicode
async def test_offline_diagnostics_rederive_and_follow_explicit_human_assessment(tmp_path):
    report = await runner(tmp_path, "m-direct").run()
    original = (tmp_path / "run/report.json").read_bytes()
    live = report.task_report.to_dict()["episodes"][0]["retrieval_diagnostics"]
    export_review(tmp_path / "run", tmp_path / "review")
    diagnostics = json.loads((tmp_path / "review/diagnostics.json").read_text(encoding="utf-8"))
    assert diagnostics["rows"][0]["observation"] == live
    markdown = (tmp_path / "review/diagnostics.md").read_text(encoding="utf-8")
    assert json.loads(markdown.split("```json\n", 1)[1].split("\n```", 1)[0]) == diagnostics
    path = tmp_path / "review/judgment-template.json"
    judgment = json.loads(path.read_text())
    judgment.update(
        reviewer="explicit-test-reviewer",
        judgments=[
            {
                "trial_id": report.trials[0].spec.trial_id,
                "status": "passed",
                "reason": "Fixture human reviewed answer and native evidence.",
            }
        ],
    )
    path.write_text(json.dumps(judgment))
    assess_run(tmp_path / "run", path, tmp_path / "assessment")
    assessed = json.loads((tmp_path / "assessment/diagnostics.json").read_text(encoding="utf-8"))
    assert assessed["rows"][0]["assessment"]["status"] == "passed"
    assert assessed["rows"][0]["observation"] == live
    assert (tmp_path / "run/report.json").read_bytes() == original


@pytest.mark.frozen_unicode
async def test_context_prepared_but_absent_from_dispatch_does_not_count(tmp_path):
    report = await runner(
        tmp_path, "s-direct", NavigatingProvider(selected("s-direct"), guess=True)
    ).run()
    packet = report.task_report.to_dict()["episodes"][0]
    store = SqliteEventStore(tmp_path / "run/attempts/001/events")
    try:
        events = await SessionService(store).read_session(packet["session_id"])
    finally:
        await store.aclose()
    assert observe_episode(packet, events)["candidate"]["status"] == "observed"
    changed, digests = [], {}
    for event in events:
        if event.type == "request/snapshot":
            data = event.to_dict()["data"]
            data["dispatch_request"]["messages"] = data["dispatch_request"]["messages"][:-1]
            data["dispatch_fingerprint"] = fingerprint(data["dispatch_request"])
            digests[event.seq] = data["dispatch_fingerprint"]
            event = replace(event, data=data)
        elif event.type == "model/attempt-end":
            event = replace(
                event,
                data={
                    **event.data,
                    "dispatch_fingerprint": digests[event.data["request_snapshot_seq"]],
                },
            )
        changed.append(event)
    diagnostic = observe_episode(packet, tuple(changed))
    assert diagnostic["candidate"]["status"] == "not_observed"
    assert not diagnostic["source_views"]
