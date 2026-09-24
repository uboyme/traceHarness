"""Context diagnostics are derived from a Session's own events and nothing else.

The fold paths drive the real Runtime with a scripted provider: one Turn that
reads the same file with the same arguments every Step, under a water mark that
actually fires. Every read after a fold is therefore the behaviour the fold
placeholder asks the model not to do.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from traceh.api.events import EventEnvelope
from traceh.api.llm import CompletionCategory, ModelResponse, ToolCall, Usage, UsageQuality
from traceh.evaluation.errors import BenchmarkEvidenceError
from traceh.evaluation.evaluators.context_diagnostics import context_work
from traceh.llm.token_meter import TOKEN_MEASUREMENT, TokenBudgetPolicy
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.session.compaction import CompactionPolicy
from traceh.session.event_store import InMemoryEventStore

BODY = ("a line of source that is long enough to matter for token counting\n") * 40


def tight_policy() -> TokenBudgetPolicy:
    return TokenBudgetPolicy(
        encoding="cl100k_base",
        window_tokens=6000,
        output_reserve_tokens=256,
        safety_margin_tokens=64,
        trigger_percent=40,
        fold_relief_percent=25,
        fold_protect_recent_groups=1,
    )


class Reader:
    """Same read every Step, then answers; reports the usage it is told to."""

    name = "scripted"

    def __init__(self, reads: int, *, unknown_at: int | None = None) -> None:
        self.reads = reads
        self.unknown_at = unknown_at
        self.calls = 0

    async def complete(self, request):
        self.calls += 1
        usage = (
            Usage()
            if self.calls == self.unknown_at
            else Usage(1000 * self.calls, 10, UsageQuality.EXACT)
        )
        if self.calls <= self.reads:
            return ModelResponse(
                content="",
                tool_calls=(ToolCall(f"c{self.calls}", "read_file", {"path": "source.txt"}),),
                completion=CompletionCategory.TOOL_HANDOFF,
                usage=usage,
            )
        return ModelResponse(content="done", completion=CompletionCategory.NORMAL, usage=usage)


async def run(tmp_path, *, reads, token_budget, unknown_at=None):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "source.txt").write_text(BODY, encoding="utf-8")
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "data",
            max_steps=reads + 2,
            max_output_tokens=256,
            token_budget=token_budget,
            compaction=CompactionPolicy(
                enabled=True,
                trigger_utf8_bytes=1_000_000,
                max_summary_utf8_bytes=2048,
                keep_recent_turns=1,
            ),
        ),
        provider=Reader(reads, unknown_at=unknown_at),
        event_store=InMemoryEventStore(),
    )
    session_id = await runtime.create_session(workspace)
    try:
        await runtime.run_existing(session_id, "read the source repeatedly")
        events = await runtime.sessions.read_session(session_id)
    finally:
        await runtime.dispose()
    return session_id, events


@pytest.mark.asyncio
async def test_a_folding_run_reports_pressure_folds_and_reruns(tmp_path) -> None:
    session_id, events = await run(tmp_path, reads=6, token_budget=tight_policy())
    work = context_work(session_id, events)

    folds = sum(1 for e in events if e.type == "surface/replace")
    assert work.tool_folds == folds > 0
    assert work.summaries == 0
    assert work.folded_source_utf8_bytes > 0
    # Every read is identical, so each read issued after the first fold re-fetches
    # something already folded. At least one must have happened.
    assert work.reruns_after_fold > 0
    assert work.requests == 7
    assert (work.input_peak, work.input_last) == (7000, 7000)
    assert work.input_mean == pytest.approx(4000)
    assert work.metered_requests == 7 and work.metered_peak is not None
    assert work.trigger_tokens == tight_policy().input_limit * 40 // 100
    assert work.input_limit == tight_policy().input_limit
    assert work.over_limit_requests == 0
    assert work.completions == {"tool_handoff": 6, "normal": 1}
    assert work.readback_calls == 0 and work.empty_responses == 0


@pytest.mark.asyncio
async def test_an_ungoverned_run_is_unmetered_and_folds_nothing(tmp_path) -> None:
    session_id, events = await run(tmp_path, reads=6, token_budget=None)
    work = context_work(session_id, events)

    assert work.tool_folds == 0 and work.reruns_after_fold == 0
    assert work.metered_requests == 0
    assert (work.metered_peak, work.trigger_tokens, work.input_limit) == (None, None, None)
    assert work.input_peak == 7000


@pytest.mark.asyncio
async def test_one_unknown_usage_makes_input_sizes_unavailable_not_zero(tmp_path) -> None:
    session_id, events = await run(tmp_path, reads=2, token_budget=None, unknown_at=2)
    work = context_work(session_id, events)

    assert work.requests == 3 and work.unknown_usage_requests == 1
    assert (work.input_peak, work.input_mean, work.input_last) == (None, None, None)


def _event(seq, type_, data):
    return EventEnvelope(
        event_id=uuid4(),
        stream_id="session:s",
        seq=seq,
        type=type_,
        schema_version=1,
        data=data,
        occurred_at=datetime.now(UTC),
    )


def test_repeated_read_backs_and_empty_replies_are_counted() -> None:
    page = {"effect_id": "e", "digest": "d", "part": 1}
    events = (
        _event(1, "tool/call", {"tool_name": "read_tool_output", "arguments": page}),
        _event(2, "tool/call", {"tool_name": "read_tool_output", "arguments": dict(page)}),
        _event(3, "tool/call", {"tool_name": "read_tool_output", "arguments": {**page, "part": 2}}),
        _event(4, "assistant/message", {"content": "  ", "tool_calls": []}),
        _event(5, "assistant/message", {"content": "", "tool_calls": [{"id": "x"}]}),
    )
    work = context_work("s", events)

    assert (work.readback_calls, work.readback_repeats) == (3, 1)
    # A read-back is the intended path after a fold; it never counts as a rerun.
    assert work.reruns_after_fold == 0
    assert work.empty_responses == 1


def test_a_malformed_measurement_is_refused_rather_than_skipped() -> None:
    events = (_event(1, TOKEN_MEASUREMENT, {"measurement": {"input_tokens": "many"}}),)

    with pytest.raises(BenchmarkEvidenceError) as caught:
        context_work("s", events)
    assert caught.value.code == "benchmark-context-evidence-invalid"
