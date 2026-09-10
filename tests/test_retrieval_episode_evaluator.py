"""UE-2 tests drive the public runner and the original domain owners."""

import asyncio
import json
import shutil
from pathlib import Path

import pytest
from sandbox_fixtures import real_sandbox_policy

from traceh.api.llm import ModelResponse, ToolCall
from traceh.evaluation.contracts import AssessmentStatus
from traceh.evaluation.errors import BenchmarkManifestError
from traceh.evaluation.evaluators.episode_manifest import load_episode_suite
from traceh.evaluation.inputs import digest_bytes
from traceh.evaluation.manifest import load_benchmark_manifest
from traceh.evaluation.plan import RunOptions
from traceh.evaluation.review import assess_run, export_review
from traceh.evaluation.runner import EvaluationRunner

BENCHMARK = Path(__file__).parents[1] / "benchmarks/retrieval_episodes_v1"


def references(request):
    # Test Provider interprets the public rendered navigation as a model would.
    content = request.messages[-1].content
    return json.loads(content.splitlines()[1])


class NavigatingProvider:
    name = "episode-test"

    def __init__(self, case, *, guess=False):
        self.case, self.guess = case, guess
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        data = self.case.data
        question = data["question"]
        family = data["family"]

        def call(name, arguments):
            return ModelResponse(
                tool_calls=(ToolCall(f"call-{len(self.requests)}", name, arguments),)
            )

        if not any(question == m.content for m in request.messages):
            if family == "output" and not any(m.role == "tool" for m in request.messages):
                return call("shell", {"command": data["setup"]["command"]})
            return ModelResponse(content="noted")
        if self.guess:
            return ModelResponse(content=data["expectation"]["value"])
        if data["expectation"]["kind"] == "no-evidence":
            return ModelResponse(content="当前未找到有效证据，不能给出具体值。")
        blocks = references(request)
        if data["case_id"] == "h-late-page":
            for block in blocks:
                if block["tier"] == "search":
                    hit = json.loads(block["body"])["hits"][0]
                    action = hit["read_action"]
                    return call(action["tool_name"], action["arguments"])
        if family in {"history", "memory"}:
            if any(
                b["tier"] in {"search", "chunk", "section"}
                and data["expectation"]["value"] in b["body"]
                for b in blocks
            ):
                return ModelResponse(content=data["expectation"]["source_text"])
            return call("search_" + family, {"query": data["expectation"]["value"]})
        if family == "skill":
            for block in blocks:
                if block["tier"] in {"section", "chunk"}:
                    return ModelResponse(content=data["expectation"]["source_text"])
                if block["tier"] == "search":
                    for hit in json.loads(block["body"])["hits"]:
                        action = hit["read_action"]
                        if action and action["arguments"]["requested_tier"] in {"section", "chunk"}:
                            return call(action["tool_name"], action["arguments"])
            descriptor = data["setup"]["descriptor"]
            targets = descriptor["resources"] or descriptor["sections"]
            target = next(
                (
                    t
                    for t in targets
                    if t["content_digest"]
                    == digest_bytes(data["expectation"]["source_text"].encode())
                ),
                targets[-1],
            )
            return call("search_skill", {"query": target["title"]})
        tools = [m for m in request.messages if m.role == "tool"]
        for message in reversed(tools):
            try:
                body = json.loads(message.content)
            except ValueError:
                continue
            if "matches" in body:
                if data["case_id"] == "o-direct":
                    action = body["matches"][0]["read_action"]
                    return call(action["tool"], action["arguments"])
                return ModelResponse(content=data["expectation"]["source_text"])
            if "text" in body:
                return ModelResponse(content=data["expectation"]["source_text"])
            if "outputs" in body:
                ref = body["outputs"][0]["output_ref"]
                return call(
                    "search_tool_output",
                    {
                        "effect_id": ref["effect_id"],
                        "digest": ref["digest"],
                        "query": data["expectation"]["value"],
                    },
                )
        return call("list_tool_outputs", {})


def selected(case_id):
    return next(
        c
        for c in load_episode_suite(load_benchmark_manifest(BENCHMARK)).cases
        if c.data["case_id"] == case_id and c.data["material_seed"] == 113
    )


def runner(tmp_path, case_id, provider=None, sandbox=None):
    return EvaluationRunner(
        BENCHMARK,
        tmp_path / "run",
        provider=provider or NavigatingProvider(selected(case_id)),
        model_id="test-model",
        sandbox=sandbox,
        options=RunOptions(max_trials=1, case_ids=(case_id,), material_seeds=(113,)),
    )


@pytest.mark.parametrize(
    "case_id",
    ["h-direct", "h-late-page", "s-direct", "s-resource", "m-direct", "m-superseded", "m-revoked"],
)
async def test_real_production_sources_and_dispatched_evidence(tmp_path, case_id):
    report = await runner(tmp_path, case_id).run()
    packet = report.task_report.to_dict()["episodes"][0]
    assert report.complete, report.to_dict()
    assert report.trials[0].assessment is AssessmentStatus.PENDING_REVIEW
    assert not packet["replay_errors"] and not packet["invariant_errors"]
    if case_id == "m-revoked":
        assert not packet["dispatched_evidence"]
        assert packet["retrieval_diagnostics"]["evidence"]["status"] == "not_applicable"
    else:
        assert packet["provisional_joint_pass"], packet
        assert packet["retrieval_diagnostics"]["evidence"]["status"] == "observed"
    if case_id.startswith("m-"):
        assert packet["session_id"] != packet["setup_session_id"]
    if case_id == "h-late-page":
        assert any(c["name"] == "request_history_page" for c in packet["calls"])
        views = packet["retrieval_diagnostics"]["source_views"]
        assert any(v["tier"] == "chunk" and v["provenance"]["page"] for v in views)


@pytest.mark.parametrize("case_id", ["o-direct", "o-nonzero", "o-absent"])
async def test_output_uses_real_sandbox_once(tmp_path, case_id):
    report = await runner(tmp_path, case_id, sandbox=real_sandbox_policy()).run()
    packet = report.task_report.to_dict()["episodes"][0]
    assert report.complete and report.trials[0].execution.value == "completed", packet
    assert packet["output_executions"] == 1
    assert packet["output_sources"][0]["exit_code"] == selected(case_id).data["setup"]["exit_code"]
    if case_id != "o-absent":
        assert packet["provisional_joint_pass"], packet
        assert packet["retrieval_diagnostics"]["candidate"]["status"] == "observed"
        assert packet["retrieval_diagnostics"]["evidence"]["status"] == "observed"
    else:
        assert packet["retrieval_diagnostics"]["evidence"]["status"] == "not_applicable"
    if case_id == "o-direct":
        assert any(c["name"] == "read_tool_output" for c in packet["calls"])


async def test_guessing_value_without_reading_is_pending_and_cannot_be_approved(tmp_path):
    case = selected("s-direct")
    report = await runner(tmp_path, "s-direct", NavigatingProvider(case, guess=True)).run()
    packet = report.task_report.to_dict()["episodes"][0]
    assert packet["provisional_answer_match"] and not packet["dispatched_evidence"]
    export_review(tmp_path / "run", tmp_path / "review")
    path = tmp_path / "review/judgment-template.json"
    value = json.loads(path.read_text())
    value.update(
        reviewer="test-reviewer",
        judgments=[
            {"trial_id": report.trials[0].spec.trial_id, "status": "passed", "reason": "guessed"}
        ],
    )
    path.write_text(json.dumps(value))
    with pytest.raises(BenchmarkManifestError, match="not usable"):
        assess_run(tmp_path / "run", path, tmp_path / "assessment")
    assert not (tmp_path / "assessment").exists()


async def test_offline_review_binding_pending_and_immutable_original(tmp_path):
    report = await runner(tmp_path, "m-direct").run()
    original = (tmp_path / "run/report.json").read_bytes()
    export_review(tmp_path / "run", tmp_path / "review")
    path = tmp_path / "review/judgment-template.json"
    value = json.loads(path.read_text())
    value["reviewer"] = "test-reviewer"
    path.write_text(json.dumps(value))
    pending = assess_run(tmp_path / "run", path, tmp_path / "pending")
    assert pending["assessment_counts"]["pending_review"] == 1
    value["judgments"] = [
        {
            "trial_id": report.trials[0].spec.trial_id,
            "status": "passed",
            "reason": "source and answer reviewed",
        }
    ]
    path.write_text(json.dumps(value))
    result = assess_run(tmp_path / "run", path, tmp_path / "approved")
    assert result["assessment_counts"]["passed"] == 1
    assert (tmp_path / "run/report.json").read_bytes() == original
    value["binding"]["run_id"] = "wrong-owner"
    path.write_text(json.dumps(value))
    with pytest.raises(BenchmarkManifestError):
        assess_run(tmp_path / "run", path, tmp_path / "stale")


async def test_missing_sandbox_is_measured_setup_failure(tmp_path):
    report = await runner(tmp_path, "o-direct").run()
    packet = report.task_report.to_dict()["episodes"][0]
    assert report.complete
    assert report.trials[0].execution.value == "failed"
    assert report.trials[0].assessment is AssessmentStatus.UNASSESSABLE
    assert packet["output_executions"] == 0


async def test_repeated_cancel_waits_for_provider_and_store(tmp_path):
    entered, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class GateProvider:
        name = "gate"

        async def complete(self, request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                closing.set()
                await release.wait()

    task = asyncio.create_task(runner(tmp_path, "h-direct", GateProvider()).run())
    await entered.wait()
    task.cancel()
    await closing.wait()
    task.cancel()
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    report = json.loads((tmp_path / "run/report.json").read_text())
    assert report["trials"][0]["convergence"] == "converged"
    assert report["trials"][0]["evidence"]
    export_review(tmp_path / "run", tmp_path / "cancelled-review")
    diagnostics = json.loads(
        (tmp_path / "cancelled-review/diagnostics.json").read_text(encoding="utf-8")
    )
    assert len(diagnostics["rows"]) == 1
    assert diagnostics["rows"][0]["observation"] is None
    assert diagnostics["rows"][0]["assessment"]["status"] == "unassessable"


def test_materials_and_selection_are_frozen_and_distinct():
    manifest = load_benchmark_manifest(BENCHMARK)
    suite = load_episode_suite(manifest)
    assert len(suite.cases) == 72
    assert len({c.digest for c in suite.cases}) == 72
    assert {c.data["material_seed"] for c in suite.cases} == {113, 227, 419}
    assert all(
        c.data["expectation"]["value"] not in c.data["question"]
        for c in suite.cases
        if c.data["expectation"]["value"]
    )


async def test_output_metadata_is_not_answer_evidence(tmp_path):
    suite = tmp_path / "suite"
    shutil.copytree(BENCHMARK, suite)
    manifest = json.loads((suite / "benchmark.json").read_text(encoding="utf-8"))
    dataset = json.loads((suite / "dataset.json").read_text(encoding="utf-8"))
    case = next(
        c for c in dataset["cases"] if c["case_id"] == "o-nonzero" and c["material_seed"] == 113
    )
    old = case["expectation"]["value"]
    case["expectation"].update(
        value="7",
        value_type="number",
        source_text=case["expectation"]["source_text"].replace(old, "7"),
    )
    script = suite / case["setup"]["script"]["file"]
    script.write_bytes(script.read_bytes().replace(old.encode(), b"7"))
    case["setup"]["script"]["sha256"] = digest_bytes(script.read_bytes())
    raw = json.dumps(dataset, ensure_ascii=False).encode()
    (suite / "dataset.json").write_bytes(raw)
    manifest["dataset"]["sha256"] = digest_bytes(raw)
    (suite / "benchmark.json").write_text(json.dumps(manifest), encoding="utf-8")

    class MetadataProvider:
        name = "metadata-test"

        def __init__(self):
            self.count = 0

        async def complete(self, request):
            self.count += 1

            def call(name, args):
                return ModelResponse(tool_calls=(ToolCall(str(self.count), name, args),))

            if self.count == 1:
                return call("shell", {"command": "python check.py"})
            if self.count == 2:
                return ModelResponse(content="done")
            if self.count == 3:
                return call("list_tool_outputs", {})
            if self.count == 4:
                message = next(m for m in reversed(request.messages) if m.role == "tool")
                ref = json.loads(message.content)["outputs"][0]["output_ref"]
                return call(
                    "read_tool_output",
                    {
                        "effect_id": ref["effect_id"],
                        "digest": ref["digest"],
                        "offset": 0,
                        "count": 1,
                    },
                )
            return ModelResponse(content="7")

    report = await EvaluationRunner(
        suite,
        tmp_path / "run",
        provider=MetadataProvider(),
        model_id="test-model",
        sandbox=real_sandbox_policy(),
        options=RunOptions(max_trials=1, case_ids=("o-nonzero",), material_seeds=(113,)),
    ).run()
    packet = report.task_report.to_dict()["episodes"][0]
    assert report.trials[0].execution.value == "completed", packet
    assert packet["output_executions"] == 1
    assert packet["output_sources"][0]["exit_code"] == 7
    assert packet["provisional_answer_match"] and not packet["dispatched_evidence"]
