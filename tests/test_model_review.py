"""Independent model judgments must rejoin original evidence and hard gates."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from test_control_model_calls import config, responder
from test_retrieval_episode_evaluator import NavigatingProvider, runner, selected

from traceh.evaluation.errors import BenchmarkManifestError
from traceh.evaluation.model_review import review_with_model
from traceh.evaluation.review import export_review, load_assessment, reviewed_report
from traceh.llm.failures import ProviderFailure


async def review(root, *, provider=None, **changes):
    return await review_with_model(
        root / "run",
        root / "model-review",
        provider=provider or responder(),
        config=config(),
        max_calls=1,
        max_tokens=16000,
        deadline_utc=datetime.now(UTC) + timedelta(minutes=2),
        **changes,
    )


@pytest.mark.frozen_unicode
async def test_model_judgment_is_explicit_and_original_evidence_is_unchanged(tmp_path):
    await runner(tmp_path, "m-direct").run()
    original = (tmp_path / "run/report.json").read_bytes()
    result = await review(tmp_path)
    report, _ = load_assessment(result["assessment"])
    assert report["trials"][0]["assessment"]["status"] == "passed"
    assert report["trials"][0]["assessment"]["origin"] == "model"
    assert result["cost"]["calls"] == 1 and result["cost"]["tokens"] == 30
    assert (tmp_path / "run/report.json").read_bytes() == original


@pytest.mark.frozen_unicode
async def test_model_grade_cannot_be_edited_after_actual_call(tmp_path):
    await runner(tmp_path, "m-direct").run()
    await review(tmp_path)
    path = tmp_path / "model-review/judgment.json"
    data = json.loads(path.read_text())
    data["judgments"][0]["status"] = "failed"
    path.write_text(json.dumps(data))
    with pytest.raises(BenchmarkManifestError) as error:
        reviewed_report(tmp_path / "run", path)
    assert error.value.code == "evaluation-judgment-stale"


@pytest.mark.frozen_unicode
async def test_invalid_judge_json_remains_pending(tmp_path):
    await runner(tmp_path, "m-direct").run()
    result = await review(tmp_path, provider=responder("PASS!"))
    assert result["assessment_counts"]["pending_review"] == 1


@pytest.mark.frozen_unicode
async def test_guessed_answer_is_failed_by_program_without_calling_judge(tmp_path):
    case = selected("s-direct")
    await runner(tmp_path, "s-direct", NavigatingProvider(case, guess=True)).run()
    judge = responder()
    result = await review(tmp_path, provider=judge)
    assert result["assessment_counts"]["failed"] == 1
    assert result["cost"]["calls"] == 0 and not judge.requests


@pytest.mark.frozen_unicode
async def test_review_oversized_batch_is_refused_before_model(tmp_path):
    await runner(tmp_path, "m-direct").run()
    judge = responder()
    with pytest.raises(ValueError, match="budget-insufficient"):
        await review_with_model(
            tmp_path / "run",
            tmp_path / "review",
            provider=judge,
            config=config(),
            max_calls=0,
            max_tokens=16000,
            deadline_utc=datetime.now(UTC) + timedelta(minutes=2),
        )
    assert not judge.requests and not (tmp_path / "review").exists()


@pytest.mark.frozen_unicode
async def test_review_rejects_old_ambiguous_origin_format(tmp_path):
    await runner(tmp_path, "m-direct").run()
    export_review(tmp_path / "run", tmp_path / "human")
    path = tmp_path / "human/judgment-template.json"
    data = json.loads(path.read_text())
    data["format"] = 1
    del data["origin"]
    data["reviewer"] = "old-fixture"
    path.write_text(json.dumps(data))
    with pytest.raises(BenchmarkManifestError):
        reviewed_report(tmp_path / "run", path)


@pytest.mark.frozen_unicode
async def test_judge_failure_and_assessment_publication_failure_are_preserved(
    tmp_path, monkeypatch
):
    import traceh.evaluation.model_review as owner

    class FailingJudge:
        name = "scripted"

        def __init__(self):
            self.called = False

        async def complete(self, request):
            self.called = True
            raise RuntimeError("explicit judge transport failure")

    def fail_publication(*args):
        raise OSError("explicit assessment publication failure")

    await runner(tmp_path, "m-direct").run()
    judge = FailingJudge()
    monkeypatch.setattr(owner, "assess_run", fail_publication)
    with pytest.raises(ExceptionGroup) as errors:
        await review(tmp_path, provider=judge)
    assert judge.called
    assert [type(e) for e in errors.value.exceptions] == [ProviderFailure, OSError]
    assert (tmp_path / "model-review/calls/0001/result.json").exists()
