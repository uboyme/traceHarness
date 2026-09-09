"""Offline validation of the live evaluator; scripted responses are NOT live evidence."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from live_reference_journeys.assessment import answer_object, assess, summarize
from live_reference_journeys.bootstrap import question
from live_reference_journeys.run import attempt, attempt_metrics, prepare

from traceh.api.events import EventEnvelope
from traceh.api.llm import ModelResponse, ToolCall

ROOT = Path(__file__).parent
CORPUS = json.loads((ROOT / "live_reference_journeys" / "corpus.json").read_text(encoding="utf-8"))
SKILLS = json.loads((ROOT / "live_skill_navigation" / "corpus.json").read_text(encoding="utf-8"))


def test_question_discloses_field_contract_without_expected_values():
    case = {
        "query": "Read the stored measurement and report its value.",
        "answer_fields": {"measurement": "Decimal digits only; no unit or explanation"},
        "answers": {"measurement": "1937"},
    }
    rendered = question(case)
    assert case["answer_fields"]["measurement"] in rendered
    assert "1937" not in rendered
    case["answers"]["measurement"] = "6029"
    assert question(case) == rendered
    case["answer_fields"]["measurement"] = "Include the unit explicitly"
    assert question(case) != rendered


@pytest.mark.parametrize("fields", [{"other": "Different key"}, {"measurement": ""}])
def test_missing_or_ambiguous_answer_contract_fails_before_a_model_call(fields):
    with pytest.raises(ValueError, match="answer-field"):
        question(
            {
                "query": "Read a measurement.",
                "answer_fields": fields,
                "answers": {"measurement": "1937"},
            }
        )


class ScriptedJourney:
    name = "openai-compatible"

    def __init__(self, case, *, broken_file=False):
        self.case, self.broken_file = case, broken_file
        self.seen = set()
        self.requests = []
        self.section_titles = set()
        for source in case["sources"]:
            if source.startswith("skill:"):
                skill_key, section_key = source.removeprefix("skill:").split("/")
                skill = next(s for s in SKILLS["skills"] if s["key"] == skill_key)
                self.section_titles.add(
                    next(s["title"] for s in skill["sections"] if s["key"] == section_key)
                )

    async def complete(self, request):
        self.requests.append(request)
        blocks = json.loads(request.messages[-1].content.split("\n")[1])
        target = any(m.content == question(self.case) for m in request.messages[:-1])
        turn = request.metadata["turn_id"]
        if (self.case["setup"] == "history-stale" or self.case["setup"] == "current-file") and (
            turn,
            "file",
        ) not in self.seen:
            self.seen.add((turn, "file"))
            file = CORPUS["files"][
                "current" if self.case["setup"] == "current-file" else "observed"
            ]
            return ModelResponse(
                tool_calls=(
                    ToolCall(
                        f"read-file-{turn}",
                        "read_file",
                        {"path": "absent-fixture.txt" if self.broken_file else file["path"]},
                    ),
                )
            )
        if not target:
            return ModelResponse(content="已记录")
        for block in blocks:
            if block["kind"] == "history":
                action = block["read_action"]
                cursor = action["arguments"]["cursor"] if action is not None else None
                key = (turn, json.dumps(cursor, sort_keys=True))
                if cursor is not None and key not in self.seen:
                    self.seen.add(key)
                    return ModelResponse(
                        tool_calls=(
                            ToolCall(
                                f"page-{len(self.seen)}",
                                "request_history_page",
                                {
                                    "block_id": block["id"],
                                    "cursor": cursor,
                                    "requested_tier": "chunk",
                                },
                            ),
                        )
                    )
            elif block["kind"] == "memory" and block["tier"] == "directory":
                key = (turn, block["id"])
                if key not in self.seen:
                    self.seen.add(key)
                    return ModelResponse(
                        tool_calls=(
                            ToolCall(
                                f"memory-{len(self.seen)}",
                                "request_workspace_memory",
                                {
                                    "memory_id": block["id"],
                                    "version": block["version"],
                                    "requested_tier": "section",
                                },
                            ),
                        )
                    )
            elif block["kind"] == "skill" and block["tier"] == "directory":
                # This fixture targets frozen test chapters to exercise the evaluator.
                # The real runner never uses it or supplies these calls to a model.
                for section in json.loads(block["body"])["sections"]:
                    if section["title"] not in self.section_titles:
                        continue
                    key = (turn, section["section_id"])
                    if key not in self.seen:
                        self.seen.add(key)
                        return ModelResponse(
                            tool_calls=(
                                ToolCall(
                                    f"skill-{len(self.seen)}",
                                    "request_skill_reference",
                                    {
                                        "skill_id": block["id"],
                                        "version": block["version"],
                                        "catalog_digest": block["catalog_digest"],
                                        "requested_tier": "section",
                                        "section_id": section["section_id"],
                                        "resource_id": None,
                                        "chunk_id": None,
                                    },
                                ),
                            )
                        )
        return ModelResponse(content=json.dumps(self.case["answers"]))


async def run_case(tmp_path, case_id, **provider_arguments):
    case = next(c for c in CORPUS["cases"] if c["id"] == case_id)
    args = SimpleNamespace(output=tmp_path, model=CORPUS["acceptance"]["models"][0])
    provider = ScriptedJourney(case, **provider_arguments)
    result = await attempt(args, CORPUS, SKILLS, case, 0, provider=provider)
    return case, provider, result, tmp_path / f"0-{case_id}"


@pytest.mark.parametrize("case_id", [c["id"] for c in CORPUS["cases"] if c["group"] == "core"])
async def test_journey_preconditions_and_real_owner_evidence(tmp_path, case_id):
    _, provider, result, root = await run_case(tmp_path, case_id)
    assert provider.requests
    assert result["bootstrap_completed"], result
    assert result["task_passed"], result
    expected = json.loads((root / "expected.json").read_text(encoding="utf-8"))["expected"]
    if case_id == "memory_cross_session":
        assert len(result["sessions"]) == 2
        assert expected["session_id"] == result["sessions"][1]
    if case_id == "history_stale_and_current_file":
        revisions = expected["revision_change"]
        assert revisions["previous"]["source_revision"] != revisions["current"]["source_revision"]


async def test_failed_real_file_bootstrap_is_retained_and_cannot_pass(tmp_path):
    _, provider, result, root = await run_case(
        tmp_path, "history_stale_and_current_file", broken_file=True
    )
    assert len(provider.requests) == 2
    assert not result["task_passed"] and not result["bootstrap_completed"]
    assert result["fixture_failure"] == "history-bootstrap-file-not-read"
    streams = json.loads((root / "streams.json").read_text(encoding="utf-8"))
    events = streams["session:" + result["session_id"]]
    assert any(e["type"] == "tool/result" and e["data"]["status"] == "failed" for e in events)
    assert events[-1]["type"] == "turn/end"
    assert result["replay_errors"] == result["invariants"] == []


async def test_answer_only_or_foreign_body_cannot_pass_assessor(tmp_path):
    case, _, result, root = await run_case(tmp_path, "memory_summary")
    assert result["task_passed"]
    expected = json.loads((root / "expected.json").read_text(encoding="utf-8"))["expected"]
    empty = assess(
        case,
        expected,
        [],
        result["final_text"],
        "completed",
        [],
        [],
        CORPUS["bounds"]["allowed_tools"],
    )
    assert empty["answer_ok"] and not empty["evidence_ok"] and not empty["task_passed"]
    streams = json.loads((root / "streams.json").read_text(encoding="utf-8"))
    raw = streams["session:" + result["session_id"]]
    expected["project_id"] = "different-fixture-project"
    events = [EventEnvelope.from_dict(e) for e in raw]
    foreign = assess(
        case,
        expected,
        events,
        result["final_text"],
        "completed",
        [],
        [],
        CORPUS["bounds"]["allowed_tools"],
    )
    assert foreign["scope_violations"] and not foreign["task_passed"]


@pytest.mark.parametrize("text", ['{"a":"1","a":"1"}', '{"a":1}', '{"a":NaN}', 'prefix {"a":"1"}'])
def test_answer_decoder_does_not_repair_or_coerce(text):
    assert answer_object(text) is None


def test_answer_decoder_accepts_only_explicit_presentation_fence():
    assert answer_object('```json\n{"a":"1"}\n```') == {"a": "1"}


def test_partial_grid_and_semantic_diagnostics_do_not_claim_acceptance():
    results = [{"group": "core", "task_passed": True}] * 28
    assert not summarize(CORPUS, results, complete=False)["acceptance_passed"]
    assert summarize(CORPUS, results, complete=True)["acceptance_passed"]
    diagnostic = summarize(
        CORPUS, [*results, {"group": "semantic-diagnostic", "task_passed": False}], complete=True
    )
    assert diagnostic["acceptance_passed"]
    assert diagnostic["semantic_diagnostic"] == {"total": 1, "task_passed": 0}


def test_attempt_usage_keeps_bootstrap_and_missing_failure_usage_separate():
    def event(turn, status, usage=None):
        return {
            "type": "model/attempt-end",
            "data": {
                "turn_id": turn,
                "status": status,
                **({"usage": usage} if usage else {}),
            },
        }

    streams = {
        "session:fixture": [
            event(
                "seed", "succeeded", {"quality": "exact", "input_tokens": 11, "output_tokens": 3}
            ),
            event("answer", "failed"),
            event(
                "answer", "succeeded", {"quality": "exact", "input_tokens": 17, "output_tokens": 5}
            ),
        ]
    }
    metrics = attempt_metrics(streams, "answer")
    assert metrics["bootstrap"]["exact_tokens"] == 14
    assert metrics["target"] == {
        "attempts": 2,
        "succeeded": 1,
        "with_exact_usage": 1,
        "exact_tokens": 22,
        "missing_usage": 1,
    }


def test_manifest_freezes_actual_policies_and_cannot_promote_a_subset(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACEH_BASE_URL", "https://provider.example.invalid/v1")
    monkeypatch.setenv("TRACEH_API_KEY_ENV", "REFERENCE_EVALUATION_SYNTHETIC")
    monkeypatch.setenv("REFERENCE_EVALUATION_SYNTHETIC", "synthetic-test-input")
    config = tmp_path / "synthetic-provider-config.txt"
    config.write_text("# Explicit synthetic test inputs are set by this test.\n", encoding="utf-8")
    args = SimpleNamespace(
        corpus=ROOT / "live_reference_journeys" / "corpus.json",
        skill_corpus=ROOT / "live_skill_navigation" / "corpus.json",
        model=CORPUS["acceptance"]["models"][0],
        env_file=config,
        output=tmp_path / "manifest-only",
        cases=["history_two_pages"],
        repeats=1,
    )
    _, _, cases, repeats, manifest = prepare(args)
    assert [c["id"] for c in cases] == ["history_two_pages"] and repeats == 1
    assert manifest["diagnostic_subset"]
    assert manifest["context_policies"]["history_two_pages"]["history"]["page_messages"] == 2
    serialized = (args.output / "manifest.json").read_text(encoding="utf-8")
    assert "synthetic-test-input" not in serialized
    assert manifest["source_sha256"] and manifest["journey_helpers_sha256"]
