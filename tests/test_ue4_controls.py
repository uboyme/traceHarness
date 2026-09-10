"""Validate the auxiliary fixture against the native Runtime before using a real model."""

import asyncio
import json
from pathlib import Path

import pytest
from live_unified_evaluation.controls import prepared_control, run_control
from sandbox_fixtures import real_sandbox_policy

from traceh.api.llm import ModelResponse, ToolCall
from traceh.session.service import SessionService
from traceh.session.sqlite import SqliteEventStore


def materials():
    root = Path(__file__).parents[1]
    spec = json.loads(
        (root / "tests/live_unified_evaluation/controls.json").read_text(encoding="utf-8")
    )
    settings = json.loads(
        (root / "benchmarks/retrieval_episodes_v1/benchmark.json").read_text(encoding="utf-8")
    )["task_settings"]
    return spec, settings


@pytest.mark.parametrize("case_id", ["competing-memory", "ordinary-arithmetic"])
async def test_native_sources_and_output_preparation(tmp_path, case_id):
    spec, settings = materials()
    case = next(c for c in spec["cases"] if c["case_id"] == case_id)

    class Provider:
        name = "control-test"
        count = 0
        target = False

        async def complete(self, request):
            self.count += 1
            if any(m.content == case["question"] for m in request.messages):
                self.target = True
                if case["family"] is None:
                    return ModelResponse(content=case["expected_value"])
                blocks = json.loads(request.messages[-1].content.splitlines()[1])
                if any(
                    b["kind"] == "memory"
                    and b["tier"] == "search"
                    and case["expected_value"] in b["body"]
                    for b in blocks
                ):
                    return ModelResponse(content=spec["memory"]["items"][0]["body"])
                return ModelResponse(
                    tool_calls=(
                        ToolCall(f"call-{self.count}", "search_memory", {"query": "长期交接"}),
                    )
                )
            if any(m.content == spec["output"]["prompt"] for m in request.messages) and not any(
                m.role == "tool" for m in request.messages
            ):
                return ModelResponse(
                    tool_calls=(
                        ToolCall(
                            f"call-{self.count}", "shell", {"command": spec["output"]["command"]}
                        ),
                    )
                )
            return ModelResponse(content="已记录")

    provider = Provider()
    row = await run_control(
        tmp_path / "case", spec, case, settings, provider, "fixture", real_sandbox_policy()
    )
    assert provider.target and row["failure"] is None
    assert row["output_execution_count"] == "1"
    assert not row["replay_errors"] and not row["invariant_errors"]
    assert row["assessment"] == "pending_review"
    if case["family"]:
        assert row["evidence"]
    else:
        assert not row["calls"] and not row["evidence"]


async def test_failed_preparation_preserves_native_attempt_evidence(tmp_path):
    spec, settings = materials()
    seen = []

    class Failed:
        name = "control-failure"

        async def complete(self, request):
            seen.append(request)
            raise OSError("deliberate preparation transport failure")

    row = await run_control(
        tmp_path / "case", spec, spec["cases"][0], settings, Failed(), "fixture", None
    )
    assert seen and row["failure"] == "ProviderFailure"
    assert row["session_id"] and row["usage"]["all"]["attempts"] == 1
    assert not row["evidence"] and row["target_start_seq"] == 0


async def test_repeat_cancel_waits_for_original_runtime_then_closes_store(tmp_path):
    spec, settings = materials()
    entered, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    sessions = []

    class Gate:
        name = "control-cancellation"

        async def complete(self, request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                closing.set()
                await release.wait()

    async def run():
        async with prepared_control(
            tmp_path / "case", spec, settings, Gate(), "fixture", None
        ) as p:
            sessions.append(p.session_id)
            await p.runtime.run_existing(p.session_id, "开始取消测试")

    task = asyncio.create_task(run())
    await entered.wait()
    task.cancel()
    await closing.wait()
    task.cancel()
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    store = SqliteEventStore(tmp_path / "case/events")
    try:
        events = await SessionService(store).read_session(sessions[0])
        assert any(e.type == "turn/end" and e.data["reason"] == "cancelled" for e in events)
    finally:
        await store.aclose()
