"""AO-1 enters real workers and original evidence owners; all candidates are fixtures."""

import asyncio
import json
import os
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from test_evaluation_comparison import judge_pair, pair_plan
from test_evaluation_workers import model_server
from test_retrieval_episode_evaluator import BENCHMARK

from traceh.api.optimization import DevelopmentObservation, TextEdit
from traceh.cli.main import _configure_from_environment, _provider_and_model, build_parser
from traceh.evaluation.inputs import digest_bytes
from traceh.evaluation.plan import load_run_options
from traceh.evaluation.runner import EvaluationRunner
from traceh.evaluation.variants import source_digest, source_files
from traceh.evolution.optimization import (
    ManualCandidate,
    candidate_decision,
    inspect_optimization,
    run_manual_optimization,
    write_optimization_report,
)
from traceh.evolution.optimization_contract import (
    OptimizationContract,
    OptimizationContractError,
    OptimizationLimits,
    editable_text,
)


def configured(root, *, benchmark=BENCHMARK, model=None, prepare=None, sandbox=None):
    plan_file = pair_plan(root, model=model)
    raw = json.loads(plan_file.read_text())
    raw["comparison"]["min_pass_gain"] = 0
    if prepare:
        prepare(root, raw)
    plan_file.write_text(json.dumps(raw), encoding="utf-8")
    args = build_parser().parse_args(
        [
            "eval",
            str(benchmark),
            "--run-plan",
            str(plan_file),
            "--output",
            str(root / "unused"),
            "--env-file",
            str(root / "empty.env"),
        ]
    )
    _configure_from_environment(args)
    provider, model_id = _provider_and_model(args)
    runner = EvaluationRunner(
        benchmark,
        root / "unused",
        provider=provider,
        model_id=model_id,
        options=load_run_options(plan_file),
        sandbox=sandbox,
    )
    files = source_files()[1]
    nodes = editable_text(files, (("runtime/prompt.py", "_REFERENCE_GUIDANCE"),))
    cases = tuple(dict.fromkeys(t.case_id for t in runner.trials))
    contract = OptimizationContract(
        "manual-explicit-fixture",
        source_digest(files),
        runner.manifest.document.sha256,
        runner.options.document.sha256,
        runner.manifest.dataset.sha256,
        cases,
        nodes,
        OptimizationLimits(4, 3, 8, 2, 2, 2, 1, 2000, 200000),
        datetime.now(UTC) + timedelta(minutes=10),
    )
    observations = (
        DevelopmentObservation(
            cases[0],
            "navigation",
            "Explicit fixture hypothesis.",
            ("fixture:development-observation",),
        ),
    )
    node = nodes[0]
    draft = ManualCandidate(
        "Test a bounded navigation reminder.",
        ("navigation",),
        (
            TextEdit(
                node.file,
                node.selector,
                node.old_sha256,
                node.text + "\nEXPLICIT TEST CANDIDATE TEXT: check evidence.",
            ),
        ),
        "May consume more input tokens.",
    )
    return runner, contract, draft, observations


async def execute(root, *, prepare=None, model=None, candidates=None, contract_change=None):
    runner, contract, draft, observations = configured(root, prepare=prepare, model=model)
    if contract_change:
        contract = contract_change(contract)
    return await run_manual_optimization(
        runner,
        contract,
        candidates=(draft, draft) if candidates is None else candidates(draft),
        observations=observations,
        output_dir=root / "optimization",
    )


async def test_actual_candidate_dispatch_pending_review_and_offline_inspection(tmp_path):
    report = await execute(tmp_path)
    root = tmp_path / "optimization"
    native = root / "rounds/0001/evaluation"
    assert report["action"] == "await_review" and report["progress"]["pending_reviews"] == 2
    assert report["progress"]["trials_started"] == 2 and report["remaining_candidates"] == 1
    assert report["adoption_authorized"] is False
    assert report["progress"]["evidence"] == "passed"
    assert report["progress"]["convergence"] == "converged"
    receipts = [
        json.loads((native / f"arms/{i:02d}/worker-receipt.json").read_text()) for i in (1, 2)
    ]
    assert len({os.getpid(), *(r["pid"] for r in receipts)}) == 3
    for i in (1, 2):
        dbfile = native / f"arms/{i:02d}/run/attempts/001/events/events.sqlite3"
        with sqlite3.connect(dbfile) as db:
            snapshots = [
                r[0]
                for r in db.execute("SELECT envelope_json FROM events")
                if json.loads(r[0])["type"] == "request/snapshot"
            ]
        assert snapshots
        assert any("EXPLICIT TEST CANDIDATE TEXT" in s for s in snapshots) == (i == 2)
    # Cached scores are not authority. An explicit original review fixture is.
    (native / "comparison/report.json").write_text("{}")
    (root / "report/report.json").write_text("{}")
    assert inspect_optimization(root)["action"] == "await_review"
    assessment = judge_pair(native, ("passed", "passed"))
    judged = write_optimization_report(root, tmp_path / "judged", assessments={1: assessment})
    assert judged["rounds"][0]["reason"] == "no-gain"
    assert judged["rounds"][0]["assessment"]["sha256"] == digest_bytes(assessment.read_bytes())
    assert judged["rounds"][0]["comparison"]["changes"]["unchanged"] == 1
    assert not (root / "rounds/0002").exists()  # Inspection never resumes the queue.
    with pytest.raises(OptimizationContractError, match="assessment-round"):
        inspect_optimization(root, assessments={2: assessment})
    frozen_before = (root / "optimization.json").read_bytes()
    with pytest.raises(FileExistsError):
        await execute(tmp_path)
    assert (root / "optimization.json").read_bytes() == frozen_before


@pytest.mark.parametrize("fault", ["source", "scope", "thresholds", "plan"])
async def test_preflight_refuses_drift_without_creating_execution(tmp_path, fault):
    runner, contract, draft, observations = configured(tmp_path)
    if fault == "source":
        contract = replace(contract, base_source_digest="f" * 64)
    elif fault == "scope":
        contract = replace(contract, development_case_ids=("unselected-fixture",))
    elif fault == "thresholds":
        raw = runner.options.document.data
        raw["comparison"]["min_pass_gain"] = None
        path = tmp_path / "plan.json"
        path.write_text(json.dumps(raw))
        runner = runner.for_plan(tmp_path / "unused2", path)
        contract = replace(contract, run_plan_digest=runner.options.document.sha256)
    else:
        (tmp_path / "plan.json").write_text("{}")
    with pytest.raises(ValueError):
        await run_manual_optimization(
            runner,
            contract,
            candidates=(draft,),
            observations=observations,
            output_dir=tmp_path / "out",
        )
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    "limit,reason", [("trial", "trial-batch-exceeds-limit"), ("deadline", "deadline")]
)
async def test_whole_batch_budget_and_expired_deadline_do_not_start_trials(tmp_path, limit, reason):
    def change(c):
        if limit == "trial":
            return replace(c, limits=replace(c.limits, max_trials=1))
        return replace(c, deadline_utc=datetime.now(UTC) - timedelta(seconds=1))

    report = await execute(tmp_path, contract_change=change)
    assert report["reason"] == reason and not report["rounds"]
    assert report["progress"]["trials_started"] == 0


async def test_invalid_proposal_limits_and_scope_violation_never_launch_workers(tmp_path):
    report = await execute(
        tmp_path / "invalid", candidates=lambda d: (replace(d, rationale=""),) * 3
    )
    assert report["reason"] == "max-invalid-proposals"
    assert report["progress"]["invalid_proposals"] == 2
    assert report["progress"]["trials_started"] == 0
    report = await execute(
        tmp_path / "scope",
        candidates=lambda d: (
            replace(d, edits=(replace(d.edits[0], selector="unapproved_fixture_node"),)),
            d,
        ),
    )
    assert report["action"] == "stop" and report["reason"] == "optimization-edit-scope-invalid"
    assert report["remaining_candidates"] == 1 and report["progress"]["trials_started"] == 0


async def test_unknown_usage_stops_without_inventing_zero_cost(tmp_path):
    def prepare(root, raw):
        (root / "script.json").write_text(json.dumps([{"content": "fixture answer"}]))

    report = await execute(tmp_path, prepare=prepare)
    # The first arm's cost is unknown, so the executor never starts the second arm
    # (ADR-0082 / plan S0-C); the round is incomplete and still not zero-cost.
    assert report["action"] == "stop" and report["reason"] == "execution-incomplete"
    assert report["rounds"][0]["comparison"]["arms"][0]["cost"]["total_tokens"] is None
    assert report["progress"]["trials_started"] == 1
    assert report["progress"]["consecutive_no_gain"] == 0


async def test_transport_failure_preserves_actual_attempts_and_stops(tmp_path, monkeypatch):
    monkeypatch.setenv("UNUSED_EVALUATION_KEY", "explicit-local-http-fixture")
    with model_server(asyncio.get_running_loop(), disconnect=True) as (model, _, _, requests):
        report = await execute(tmp_path, model=model)
    assert len(requests) == 1
    assert report["action"] == "stop" and report["progress"]["trials_started"] == 1
    assert report["rounds"][0]["comparison"]["changes"]["loss"] == 0
    assert report["remaining_candidates"] == 1


@pytest.mark.parametrize("publication_failure", [False, True])
async def test_repeated_cancellation_converges_actual_child_before_return(
    tmp_path,
    monkeypatch,
    publication_failure,
):
    import traceh.evolution.optimization as optimization

    original_write = optimization.write_json

    def write(path, value):
        if publication_failure and path.name == "outcome.json":
            raise OSError("explicit fixture publication failure")
        return original_write(path, value)

    monkeypatch.setattr(optimization, "write_json", write)
    monkeypatch.setenv("UNUSED_EVALUATION_KEY", "explicit-local-http-fixture")
    with model_server(asyncio.get_running_loop(), blocked=True) as (
        model,
        entered,
        release,
        requests,
    ):
        task = asyncio.create_task(execute(tmp_path, model=model))
        async with asyncio.timeout(40):
            await entered.wait()
        task.cancel()
        asyncio.get_running_loop().call_soon(task.cancel)
        release.set()
        with pytest.raises(
            BaseExceptionGroup if publication_failure else asyncio.CancelledError
        ) as raised:
            await task
    root = tmp_path / "optimization"
    assert requests
    if publication_failure:
        assert any(isinstance(e, asyncio.CancelledError) for e in raised.value.exceptions)
        assert any(isinstance(e, OSError) for e in raised.value.exceptions)
        assert not (root / "report").exists()
    else:
        report = inspect_optimization(root)
        assert report["reason"] == "cancelled" and report["progress"]["trials_started"] == 1
        assert report["remaining_candidates"] == 1
    process = json.loads((root / "rounds/0001/evaluation/arms/01/process.json").read_text())
    assert process["cancelled"] and not process["forced_stop"] and process["exit_code"] is not None
    assert not (root / "rounds/0002").exists()


@pytest.mark.parametrize(
    "change,expected",
    [
        ("gain", ("review_candidate", "development-candidate-qualified")),
        ("cheaper", ("review_candidate", "development-candidate-qualified")),
        ("loss", ("continue", "not-qualified")),
        ("expensive", ("continue", "not-qualified")),
        ("same", ("continue", "no-gain")),
        ("incomplete", ("stop", "execution-incomplete")),
        ("hard", ("stop", "hard-constraints-not-passed")),
    ],
)
def test_policy_interprets_original_dimensions_without_cost_hiding_quality_loss(change, expected):
    # Pure decision fixture, explicitly not native evidence or an effect measurement.
    c = {
        "status": "no_change",
        "complete": True,
        "hard_constraints": "passed",
        "arms": [{"cost": {"total_tokens": 30}}] * 2,
        "pairs": [{"assessment": ["passed", "passed"]}],
        "changes": {"gain": 0, "loss": 0, "unknown": 0},
        "thresholds": {"min_pass_gain": True, "max_token_ratio": True, "max_tool_call_delta": True},
        "cost_delta": {"total_tokens": 0, "tool_calls": 0},
    }
    if change in ("gain", "loss"):
        c["changes"][change] = 1
        c["cost_delta"]["total_tokens"] = -1  # A quality loss stays a loss even when cheaper.
    elif change == "cheaper":
        c["cost_delta"]["total_tokens"] = -1
    elif change == "expensive":
        c["thresholds"]["max_token_ratio"] = False
    elif change == "incomplete":
        c["complete"] = False
    elif change == "hard":
        c["hard_constraints"] = "violated"
    assert candidate_decision(c) == expected


async def test_offline_inspector_rejects_another_run_and_corrupted_original_evidence(tmp_path):
    await execute(tmp_path)
    root = tmp_path / "optimization"
    directory = root / "rounds/0001"
    outcome = directory / "outcome.json"
    original = outcome.read_bytes()
    data = json.loads(original)
    data["experiment_digest"] = "e" * 64
    outcome.write_text(json.dumps(data))
    with pytest.raises(OptimizationContractError, match="run-drift"):
        inspect_optimization(root)
    outcome.write_bytes(original)
    native_report = directory / "evaluation/arms/02/run/report.json"
    data = json.loads(native_report.read_text(encoding="utf-8"))
    data["trials"][0]["assessment"]["status"] = "passed"
    native_report.write_text(json.dumps(data), encoding="utf-8")
    report = inspect_optimization(root)
    assert report["action"] == "stop" and report["reason"] == "evidence-not-comparable"
    assert report["progress"]["evidence"] == "unproven"
    assert report["progress"]["convergence"] == "unknown"


async def test_deadline_cancels_inflight_real_worker(tmp_path, monkeypatch):
    import traceh.evolution.optimization as optimization

    monkeypatch.setenv("UNUSED_EVALUATION_KEY", "explicit-local-http-fixture")
    with model_server(asyncio.get_running_loop(), blocked=True) as (
        model,
        entered,
        release,
        requests,
    ):
        # Shorten the actual asyncio deadline only after a request reaches the server.
        # Event gating proves execution happened, independent of machine startup speed.
        original_timeout = asyncio.timeout

        def gated_timeout(duration):
            timer = original_timeout(duration)

            async def expire():
                await entered.wait()
                timer.reschedule(asyncio.get_running_loop().time())
                release.set()

            if duration > 100:
                asyncio.create_task(expire())
            return timer

        monkeypatch.setattr(optimization.asyncio, "timeout", gated_timeout)
        report = await execute(tmp_path, model=model)
    assert requests and report["reason"] == "deadline"
    process = json.loads(
        (tmp_path / "optimization/rounds/0001/evaluation/arms/01/process.json").read_text()
    )
    assert not process["forced_stop"] and process["exit_code"] is not None


@pytest.fixture
def product_root():
    # Same short-root requirement as the existing Windows real Product/Git suite.
    with TemporaryDirectory(prefix="ao1p-") as directory:
        yield Path(directory)


async def test_manual_product_queue_real_git_sandbox_no_gain_dedup_and_limit(product_root):
    from sandbox_fixtures import real_sandbox_policy
    from test_product_benchmark_e2e import build_benchmark

    from traceh.api.json_types import to_json_value
    from traceh.promotion.events import PROMOTION_LEDGER_STREAM

    tmp_path = product_root
    policy = real_sandbox_policy()
    benchmark = build_benchmark(tmp_path / "benchmark", arms=(("single", 1),))

    def prepare(root, raw):
        raw["benchmark_digest"] = digest_bytes((benchmark / "benchmark.json").read_bytes())
        raw["trials"] = {"repetitions": 1}
        raw["execution"]["sandbox_config"] = "sandbox.json"
        raw["execution"]["timeout_seconds"] = 180
        (root / "sandbox.json").write_text(
            json.dumps(
                {
                    "format": 2,
                    "policy": to_json_value(policy),
                    "plugin_grants": [],
                }
            )
        )
        (root / "script.json").write_text(
            json.dumps(
                [
                    {
                        "tool_calls": [
                            {
                                "id": "write-fixture",
                                "name": "apply_patch",
                                "arguments": {
                                    "path": "added.txt",
                                    "old_text": "",
                                    "new_text": "added\n",
                                    "create": True,
                                },
                            }
                        ],
                        "usage": {"input_tokens": 20, "output_tokens": 10},
                    },
                    {"content": "implemented", "usage": {"input_tokens": 20, "output_tokens": 10}},
                ]
            )
        )

    runner, contract, draft, observations = configured(
        tmp_path / "inputs", benchmark=benchmark, prepare=prepare, sandbox=policy
    )
    next_draft = replace(
        draft,
        edits=(
            replace(
                draft.edits[0],
                new_text=draft.edits[0].new_text + "\nSECOND EXPLICIT FIXTURE: verify source.",
            ),
        ),
    )
    output = tmp_path / "optimization"
    result = await run_manual_optimization(
        runner,
        contract,
        candidates=(draft, draft, next_draft, draft),
        observations=observations,
        output_dir=output,
    )
    assert result["reason"] == "max-consecutive-no-gain"
    assert result["progress"]["trials_started"] == 4
    assert result["progress"]["duplicate_proposals"] == 1
    assert result["progress"]["consecutive_no_gain"] == 2
    assert len(result["rounds"]) == 3 and not (output / "rounds/0002/evaluation").exists()
    for n in (1, 3):
        native = output / f"rounds/{n:04d}/evaluation"
        c = result["rounds"][n - 1]["comparison"]
        assert c["complete"] and c["changes"]["unchanged"] == 1
        for arm in (1, 2):
            trial = json.loads((native / f"arms/{arm:02d}/run/report.json").read_text())["trials"][
                0
            ]
            assert trial["assessment"]["status"] == "passed"
            assert trial["convergence"] == "converged"
            assert any(e["stream_id"] == PROMOTION_LEDGER_STREAM for e in trial["evidence"])
    assert result["adoption_authorized"] is False
