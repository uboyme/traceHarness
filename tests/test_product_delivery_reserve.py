"""An explicitly configured single-coder reserve reaches the original Product path."""

import json
from pathlib import Path

import pytest
from sandbox_fixtures import real_sandbox_policy
from test_product_benchmark_e2e import (
    PRODUCT_MODEL_ID,
    _ProductProvider,
    _response,
    build_benchmark,
)

from traceh.api.llm import ToolCall, Usage, UsageQuality
from traceh.evaluation.runner import EvaluationRunner
from traceh.llm.failures import ProviderFailure, ProviderFailureCategory
from traceh.llm.retry import NO_MODEL_RETRY


@pytest.mark.parametrize("trigger", ["steps", "turn-wall"])
@pytest.mark.parametrize("ending", ["deliver", "provider-failure", "withdrawn-tool"])
async def test_single_reserve_delivers_or_fails_without_bypassing_verification(
    tmp_path: Path, monkeypatch, trigger: str, ending: str
):
    benchmark = build_benchmark(tmp_path / "benchmark", arms=(("single", 1),))
    path = benchmark / "benchmark.json"
    manifest = json.loads(path.read_bytes())
    coder = manifest["task_settings"]["roles"]["coder"]
    coder["budget"].update(max_steps=4, max_wall_milliseconds=120_000)
    coder["max_turn_wall_milliseconds"] = 60_000
    coder["wrap_up_reserve"] = {
        "steps": 2 if trigger == "steps" else 0,
        "tool_calls": 0,
        "wall_milliseconds": 10_000,
    }
    path.write_text(json.dumps(manifest), encoding="utf-8")
    if trigger == "turn-wall":
        # Deterministic elapsed-time input at the existing clock projection
        # seam: the first real write has finished; no arbitrary sleep.
        monkeypatch.setattr(
            "traceh.supervision.investigation_wrap_up._elapsed_milliseconds",
            lambda events, turn_id: 55_000 if any(
                e.type == "tool/result" and e.data.get("tool_name") == "apply_patch"
                for e in events
            ) else 0,
        )

    class Provider(_ProductProvider):
        async def complete(self, request):
            if request.tools:
                if any(m.role == "tool" for m in request.messages):
                    self.requests.append(request)
                    return _response(
                        "", ToolCall("keep-reading", "read_file", {"path": "kept.txt"})
                    )
                return await super().complete(request)
            self.requests.append(request)
            assert "Deliver now" in request.system_prompt
            if ending == "provider-failure":
                raise ProviderFailure(
                    "provider-dns-temporary", ProviderFailureCategory.DNS,
                    usage=Usage(0, 0, UsageQuality.EXACT),
                )
            if ending == "withdrawn-tool" and len(self.requests) == 2:
                return _response("", ToolCall("forbidden", "apply_patch", {
                    "path": "added.txt", "old_text": "added\n", "new_text": "corrupted\n",
                }))
            return _response("Wrote added.txt; the host's fixed check has not yet run.")

    requests = []
    runner = EvaluationRunner(
        benchmark, tmp_path / "run", provider=Provider(requests), model_id=PRODUCT_MODEL_ID,
        sandbox=real_sandbox_policy(), retry_policy=NO_MODEL_RETRY,
    )
    report = (await runner.run()).task_report
    attempt, = report.attempts
    assert len(requests) >= 2 and requests[0].tools and not requests[1].tools
    assert attempt.evidence.converged
    if ending in {"deliver", "withdrawn-tool"}:
        assert attempt.success and attempt.evidence.review_passed
        assert attempt.evidence.promotion_id is not None
        if ending == "withdrawn-tool":
            assert len(requests) == 3
            assert any(
                "publishes no tools at all" in message.content
                for message in requests[-1].messages if message.role == "tool"
            )
    else:
        assert not attempt.success and attempt.evidence.promotion_id is None
