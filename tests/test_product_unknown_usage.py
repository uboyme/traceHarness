"""Missing Provider usage is retained through a real Product execution and report."""

from dataclasses import replace

from test_product_benchmark_e2e import _ProductProvider, _runner

from traceh.api.llm import Usage, UsageQuality


class UnknownFinalUsage(_ProductProvider):
    async def complete(self, request):
        response = await super().complete(request)
        if any(message.role == "tool" for message in request.messages):
            return replace(response, usage=Usage(0, 0, UsageQuality.UNKNOWN))
        return response


async def test_final_usage_unknown_is_reported_not_zeroed(tmp_path):
    requests = []
    runner = _runner(tmp_path, arms=(("single", 1),),
                     provider=UnknownFinalUsage(requests))
    report = (await runner.run()).task_report
    attempt, = report.attempts
    assert any(message.role == "tool" for request in requests for message in request.messages)
    assert attempt.success
    evidence = attempt.evidence
    assert evidence.execution.tokens is None
    assert "execution.coder.tokens" in evidence.unavailable
    assert evidence.converged
    data = report.to_dict()
    assert data["attempts_with_unavailable_metrics"] == 1
    assert data["quality_arms"][0]["execution_tokens"]["unavailable"] == 1
    assert data["quality_arms"][0]["execution_tokens"]["values"] == []
