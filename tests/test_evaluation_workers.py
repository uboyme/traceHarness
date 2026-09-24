"""Real child processes and local HTTP transport, with deterministic cancellation gates."""

import asyncio
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from test_evaluation_comparison import judge_pair, run_pair
from test_retrieval_episode_evaluator import NavigatingProvider, selected

from traceh.evaluation.comparison import compare_experiment


@contextmanager
def model_server(loop, *, blocked=False, regression=False, disconnect=False):
    provider = NavigatingProvider(selected("m-direct"))
    entered, released = asyncio.Event(), threading.Event()
    requests = []
    if not blocked:
        released.set()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            raw = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(raw)
            loop.call_soon_threadsafe(entered.set)
            if not released.wait(60):
                return
            if disconnect:
                self.close_connection = True
                return
            is_candidate = regression and "EXPLICIT TEST CANDIDATE TEXT" in json.dumps(raw)
            if is_candidate:
                message, tokens = (
                    {"role": "assistant", "content": "No source consulted; wrong answer."},
                    200,
                )
            else:
                request = SimpleNamespace(messages=[SimpleNamespace(**m) for m in raw["messages"]])
                response = asyncio.run(provider.complete(request))
                message = {"role": "assistant", "content": response.content}
                if response.tool_calls:
                    message["tool_calls"] = [
                        {
                            "id": t.id,
                            "type": "function",
                            "function": {
                                "name": t.name,
                                "arguments": json.dumps(t.arguments),
                            },
                        }
                        for t in response.tool_calls
                    ]
                tokens = 30
            payload = json.dumps(
                {
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": tokens,
                        "completion_tokens": 5,
                        "total_tokens": tokens + 5,
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            {
                "provider": "openai-compatible",
                "model": "explicit-fixture",
                "script": None,
                "base_url": f"http://127.0.0.1:{server.server_address[1]}/v1",
            },
            entered,
            released,
            requests,
        )
    finally:
        released.set()
        server.shutdown()
        server.server_close()
        thread.join()


async def test_deliberately_regressed_candidate_uses_real_requests_and_original_memory(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("UNUSED_EVALUATION_KEY", "local-fixture-not-a-real-key")
    with model_server(asyncio.get_running_loop(), regression=True) as (model, _, _, requests):
        root, code = await run_pair(tmp_path, patch=True, case_id="m-direct", model=model)
    assert code == 0
    assert len(requests) >= 3
    reports = [
        json.loads((root / f"arms/{i:02d}/run/report.json").read_text(encoding="utf-8"))
        for i in (1, 2)
    ]
    assert reports[0]["task_report"]["episodes"][0]["dispatched_evidence"]
    assert not reports[1]["task_report"]["episodes"][0]["dispatched_evidence"]
    path = judge_pair(root, ("passed", "failed"))
    result = compare_experiment(root, tmp_path / "judged", assessments=path)
    # Wrong answer and more tokens, but one fewer tool call: expose both axes.
    assert result["status"] == "mixed" and result["quality_status"] == "regressed", result
    assert result["changes"]["loss"] == 1 and result["cost_delta"]["total_tokens"] > 0


async def test_repeated_cancel_waits_for_child_runtime_and_preserves_unstarted_arm(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("UNUSED_EVALUATION_KEY", "local-fixture-not-a-real-key")
    with model_server(asyncio.get_running_loop(), blocked=True) as (
        model,
        entered,
        release,
        requests,
    ):
        task = asyncio.create_task(run_pair(tmp_path, case_id="m-direct", model=model))
        try:
            await asyncio.wait_for(entered.wait(), 60)
            task.cancel()
            progressed = asyncio.Event()
            asyncio.get_running_loop().call_soon(progressed.set)
            await progressed.wait()
            task.cancel()
            assert not task.done()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 60)
    root = tmp_path / "experiment"
    process = json.loads((root / "arms/01/process.json").read_text())
    receipt = json.loads((root / "arms/01/worker-receipt.json").read_text())
    assert requests and process["cancelled"] and not process["forced_stop"]
    assert receipt["convergence"] == "converged"
    assert not (root / "arms/02/run").exists()
    report = json.loads((root / "comparison/report.json").read_text())
    assert not report["complete"] and report["planned_pairs"] == 1
    assert len(report["planned_trials"]) == 2
    assert report["pairs"][0]["execution"][0]["status"] == "cancelled"


async def test_connection_failure_stops_the_pair_before_the_other_arm_spends(tmp_path, monkeypatch):
    monkeypatch.setenv("UNUSED_EVALUATION_KEY", "local-fixture-not-a-real-key")
    with model_server(asyncio.get_running_loop(), disconnect=True) as (model, _, _, requests):
        root, code = await run_pair(tmp_path, case_id="m-direct", model=model)
    execution = json.loads((root / "execution.json").read_text(encoding="utf-8"))
    report = json.loads((root / "comparison/report.json").read_text(encoding="utf-8"))
    # The first arm's cost is unknown after a failed connection; paying for the
    # second arm could no longer produce a comparable pair.
    assert len(requests) == 1 and execution["errors"] == ["evaluation-arm-usage-unknown"]
    assert len(execution["outcomes"]) == 1 and not (root / "arms/02/run").exists()
    assert not report["complete"] and report["status"] == "inconclusive", report
    first = json.loads((root / "arms/01/run/report.json").read_text(encoding="utf-8"))
    assert first["trials"][0]["assessment"]["status"] == "unassessable"
    assert first["trials"][0]["usage"]["all"]["unknown_attempts"] > 0


async def test_a_settled_first_arm_still_lets_the_second_arm_run(tmp_path, monkeypatch):
    monkeypatch.setenv("UNUSED_EVALUATION_KEY", "local-fixture-not-a-real-key")
    with model_server(asyncio.get_running_loop()) as (model, _, _, requests):
        root, code = await run_pair(tmp_path, case_id="m-direct", model=model)
    execution = json.loads((root / "execution.json").read_text(encoding="utf-8"))
    assert code == 0 and execution["errors"] == [] and len(execution["outcomes"]) == 2
    assert (root / "arms/02/run/report.json").is_file() and len(requests) >= 2


async def test_forced_child_exit_is_unproven_and_does_not_start_the_other_arm(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("UNUSED_EVALUATION_KEY", "local-fixture-not-a-real-key")
    with model_server(asyncio.get_running_loop(), blocked=True) as (
        model,
        entered,
        release,
        requests,
    ):
        task = asyncio.create_task(
            run_pair(
                tmp_path, case_id="m-direct", model=model, execution={"shutdown_seconds": 0.01}
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 60)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 30)
        finally:
            release.set()
    root = tmp_path / "experiment"
    process = json.loads((root / "arms/01/process.json").read_text())
    assert requests and process["forced_stop"] and process["exit_code"] is not None
    assert not (root / "arms/02/run").exists()
    report = json.loads((root / "comparison/report.json").read_text())
    assert not report["complete"] and len(report["planned_trials"]) == 2
    assert report["hard_constraints"] == "unproven"


def _write_run(tmp_path, *, usage, attempts=()):
    run = tmp_path / "run"
    run.mkdir()
    report = {
        "trials": [{"measured": True, "usage": usage}],
        "task_report": {"attempts": list(attempts)},
    }
    (run / "report.json").write_text(json.dumps(report), encoding="utf-8")
    return run


def test_a_provider_failure_with_known_usage_still_stops_the_pair(tmp_path):
    from traceh.evaluation.variant_execution import _arm_stop_reason

    tokens = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12, "quality": "exact"}
    failed = {"evidence": {"execution": {"provider_failure_categories": ["protocol"]}}}
    run = _write_run(tmp_path, usage={"execution": tokens}, attempts=(failed,))
    assert _arm_stop_reason(run) == "evaluation-arm-provider-failure"
    assert _arm_stop_reason(tmp_path / "missing") == "evaluation-arm-report-missing"


def test_a_clean_product_arm_has_no_stop_reason(tmp_path):
    from traceh.evaluation.variant_execution import _arm_stop_reason

    tokens = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12, "quality": "exact"}
    clean = {"evidence": {"execution": {"provider_failure_categories": []}}}
    usage = {"execution": tokens, "unattributed": tokens}
    run = _write_run(tmp_path, usage=usage, attempts=(clean,))
    assert _arm_stop_reason(run) is None
