"""Context transparency: the read-only projection and its display contract.

These tests never assert on a hand-built snapshot alone. Where a fact can be
produced by the real runtime it is produced there, so the projection is checked
against events the production owners actually wrote.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from rich.cells import cell_len

from traceh.api.events import PendingEvent
from traceh.api.llm import ModelResponse, ToolCall
from traceh.api.product import ProductTaskStatus, RequestedTaskMode, ResolvedTaskMode
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.session.compaction import CompactionPolicy
from traceh.session.event_store import InMemoryEventStore
from traceh.session.product_context import (
    PRODUCT_CONTEXT_SNAPSHOT,
    ProductContextExecutionSummary,
    ProductContextTask,
    product_context_snapshot_data,
)
from traceh.session.surface_replacement import SURFACE_COMPACTION_FAILED
from traceh.tui.context_inspection import (
    ContextInspectionError,
    ContextInspectionReader,
)
from traceh.tui.presentation import (
    context_detail_lines,
    context_status_line,
    format_utf8_bytes,
)


def _policy(*, trigger: int = 1, summary_bytes: int = 400, keep: int = 0):
    return CompactionPolicy(
        enabled=True,
        trigger_utf8_bytes=trigger,
        max_summary_utf8_bytes=summary_bytes,
        keep_recent_turns=keep,
    )


def _runtime(tmp_path: Path, *, policy=None, responses=None):
    return build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "data",
            provider="scripted",
            model="demo",
            compaction=policy,
        ),
        provider=ScriptedLlmProvider(
            responses or (ModelResponse(content="answer"),), repeat_last=True
        ),
        event_store=InMemoryEventStore(),
    )


def _reader(runtime) -> ContextInspectionReader:
    return ContextInspectionReader(
        runtime.sessions, policy=runtime.compaction.policy
    )


def _context_task(
    task_id: str = "task-context",
    order: int = 6,
    *,
    focus: bool = True,
) -> ProductContextTask:
    return ProductContextTask(
        task_id=task_id,
        source_stream_id=f"product-task:{task_id}",
        source_seq=1,
        source_event_id=uuid4(),
        task_order_seq=order,
        status=ProductTaskStatus.COMPLETED,
        requested_mode=RequestedTaskMode.SINGLE,
        resolved_mode=ResolvedTaskMode.SINGLE,
        requirement_digest="a" * 64,
        origin_message_id=f"origin-{task_id}",
        source_excerpt="earlier requirement",
        source_excerpt_truncated=False,
        execution_summary=(
            ProductContextExecutionSummary(
                workflow_status=None,
                managed_tool_call_count=1,
                changed_path_count=1,
                verification_passed=True,
                verifier_count=1,
                promotion_recorded=True,
            )
            if focus
            else None
        ),
    )


async def _append_product_context(runtime, session_id, tasks, *, total=None):
    focus = tasks[0]
    return await runtime.sessions.append_session(
        session_id,
        PRODUCT_CONTEXT_SNAPSHOT,
        product_context_snapshot_data(
            session_id=session_id,
            focus=focus,
            tasks=tasks,
            total_tasks=total if total is not None else len(tasks),
        ),
        causation_id=focus.source_event_id,
    )


# -- 1. empty session -------------------------------------------------------


@pytest.mark.asyncio
async def test_an_empty_session_projects_zeroes_and_still_renders(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_id = await runtime.create_session(workspace)

    snapshot = await _reader(runtime).load(session_id)
    assert snapshot.conversation_messages == 0
    assert snapshot.conversation_utf8_bytes == 0
    assert snapshot.compaction_count == 0
    assert snapshot.failure_count == 0
    assert snapshot.visible_summaries == 0
    assert snapshot.request is None
    assert snapshot.product is None
    assert snapshot.policy is None
    assert snapshot.trigger_ratio is None

    line = context_status_line(snapshot)
    assert "0 B" in line
    assert "自动压缩关闭" in line
    assert "无任务上下文" in line
    rows = context_detail_lines(snapshot)
    rendered = "\n".join(row.plain for row in rows)
    assert "本 Session 尚无 request/snapshot" in rendered
    assert "尚无 surface/replace" in rendered
    await runtime.dispose()


# -- 2/3. policy on and off -------------------------------------------------


@pytest.mark.asyncio
async def test_compaction_disabled_states_it_without_a_fictional_threshold(
    tmp_path,
) -> None:
    runtime = _runtime(tmp_path, policy=None)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = await runtime.run(workspace, "first question")

    snapshot = await _reader(runtime).load(result.session_id)
    assert snapshot.policy is None
    assert snapshot.trigger_ratio is None
    line = context_status_line(snapshot)
    assert "自动压缩关闭" in line
    assert "阈值" not in line
    assert "%" not in line
    rendered = "\n".join(row.plain for row in context_detail_lines(snapshot))
    assert "关闭（未配置或显式关闭）" in rendered
    assert "占压缩阈值" not in rendered
    await runtime.dispose()


@pytest.mark.asyncio
async def test_enabled_compaction_shows_the_exact_policy_and_byte_denominator(
    tmp_path,
) -> None:
    policy = _policy(trigger=65_536, summary_bytes=8_192, keep=2)
    runtime = _runtime(tmp_path, policy=policy)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = await runtime.run(workspace, "first question")

    snapshot = await _reader(runtime).load(result.session_id)
    assert snapshot.policy is not None
    assert snapshot.policy.trigger_utf8_bytes == 65_536
    assert snapshot.policy.max_summary_utf8_bytes == 8_192
    assert snapshot.policy.keep_recent_turns == 2
    assert snapshot.policy.digest == policy.digest
    assert snapshot.trigger_ratio == snapshot.conversation_utf8_bytes / 65_536

    line = context_status_line(snapshot)
    assert f"{format_utf8_bytes(snapshot.conversation_utf8_bytes)} / 64.0 KiB 阈值" in line
    rendered = "\n".join(row.plain for row in context_detail_lines(snapshot))
    assert f"{snapshot.conversation_utf8_bytes} bytes" in rendered
    assert "65536 bytes" in rendered
    assert "8192 bytes" in rendered
    assert "2 turns" in rendered
    assert "占压缩阈值" in rendered
    await runtime.dispose()


def test_bytes_are_never_labelled_as_tokens_or_a_context_window() -> None:
    """The wording contract: no token claim, no model-window percentage."""

    assert format_utf8_bytes(29_104) == "28.4 KiB"
    assert format_utf8_bytes(0) == "0 B"
    assert format_utf8_bytes(2_097_152) == "2.0 MiB"
    for text in (format_utf8_bytes(1024), format_utf8_bytes(10)):
        lowered = text.lower()
        assert "token" not in lowered
        assert "tok" not in lowered
        assert "%" not in text


# -- 4. single, multiple and widening compaction ----------------------------


@pytest.mark.asyncio
async def test_durable_compaction_count_is_separate_from_visible_summaries(
    tmp_path,
) -> None:
    runtime = _runtime(tmp_path, policy=_policy(keep=0))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = await runtime.run(workspace, "first question")
    for text in ("second question", "third question", "fourth question"):
        await runtime.run_existing(result.session_id, text)

    snapshot = await _reader(runtime).load(result.session_id)
    assert snapshot.compaction_count >= 3
    # Widening compaction folds earlier summaries into a later one, so durable
    # events accumulate while the model still sees exactly one summary.
    assert snapshot.visible_summaries == 1
    assert snapshot.compaction_count != snapshot.visible_summaries
    assert all(record.method == "automatic" for record in snapshot.compactions)
    assert all(
        record.matches_current_policy for record in snapshot.compactions
    )
    assert [record.seq for record in snapshot.compactions] == sorted(
        record.seq for record in snapshot.compactions
    )
    line = context_status_line(snapshot)
    assert f"压缩 {snapshot.compaction_count} 次" in line
    await runtime.dispose()


@pytest.mark.asyncio
async def test_manual_and_automatic_records_are_distinguished(tmp_path) -> None:
    runtime = _runtime(tmp_path, policy=_policy(keep=1))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = await runtime.run(workspace, "first question")
    await runtime.run_existing(result.session_id, "second question")
    events = await runtime.sessions.read_session(result.session_id)
    turn_ends = [event.seq for event in events if event.type == "turn/end"]
    await runtime.compaction.replace_through(
        result.session_id,
        through_seq=turn_ends[0],
        summary="a human summary of the first exchange",
    )

    snapshot = await _reader(runtime).load(result.session_id)
    methods = {record.method for record in snapshot.compactions}
    assert "manual" in methods
    manual = next(r for r in snapshot.compactions if r.method == "manual")
    assert manual.policy_digest is None
    assert manual.summarizer_name is None
    # A manual record binds no policy, so it can never be shown as matching the
    # current one.
    assert manual.matches_current_policy is False
    assert manual.kept_recent_turns == 0
    latest = snapshot.latest_compaction
    assert latest is not None and latest.seq == max(
        record.seq for record in snapshot.compactions
    )
    await runtime.dispose()


@pytest.mark.asyncio
async def test_a_historical_record_under_another_policy_is_not_relabelled(
    tmp_path,
) -> None:
    """A replacement stores a digest, never the historical threshold values."""

    runtime = _runtime(tmp_path, policy=_policy(trigger=1, keep=0))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = await runtime.run(workspace, "first question")
    await runtime.run_existing(result.session_id, "second question")

    other = ContextInspectionReader(
        runtime.sessions, policy=_policy(trigger=999_999, keep=3)
    )
    snapshot = await other.load(result.session_id)
    assert snapshot.compactions
    assert all(
        record.matches_current_policy is False for record in snapshot.compactions
    )
    rendered = "\n".join(row.plain for row in context_detail_lines(snapshot))
    assert "策略与当前一致" not in rendered
    await runtime.dispose()


# -- 5. failures are honest about commit state ------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("committed", "expected"),
    [(False, False), (True, True), (None, None), (0, None), ("false", None)],
)
async def test_a_failure_record_never_invents_a_commit_answer(
    tmp_path, committed, expected
) -> None:
    runtime = _runtime(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = await runtime.run(workspace, "first question")
    await runtime.sessions.append_session(
        result.session_id,
        SURFACE_COMPACTION_FAILED,
        {
            "method": "automatic",
            "code": "compaction-write-unknown",
            "committed": committed,
        },
    )

    snapshot = await _reader(runtime).load(result.session_id)
    assert snapshot.failure_count == 1
    assert snapshot.failures[0].committed is expected
    assert snapshot.failures[0].code == "compaction-write-unknown"
    rendered = "\n".join(row.plain for row in context_detail_lines(snapshot))
    if expected is False:
        assert "历史未改变" in rendered
    elif expected is True:
        assert "已写入但读回失败" in rendered
    else:
        assert "是否已写入未知" in rendered
        assert "历史未改变" not in rendered
    assert "失败 1 次" in context_status_line(snapshot)
    await runtime.dispose()


# -- 6. Product context -----------------------------------------------------


@pytest.mark.asyncio
async def test_product_context_counts_come_from_the_projected_snapshot(
    tmp_path,
) -> None:
    runtime = _runtime(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = await runtime.run(workspace, "first question")
    tasks = (
        _context_task("task-focus", order=9),
        _context_task("task-older", order=4, focus=False),
    )
    await _append_product_context(runtime, result.session_id, tasks, total=9)

    snapshot = await _reader(runtime).load(result.session_id)
    product = snapshot.product
    assert product is not None
    assert product.focus_task_id == "task-focus"
    assert product.focus_status == "completed"
    assert (product.shown, product.total, product.omitted) == (2, 9, 7)
    assert [entry.task_id for entry in product.tasks] == ["task-focus", "task-older"]
    assert product.messages == 2
    assert product.utf8_bytes > 0
    line = context_status_line(snapshot)
    assert "任务 task-focus" in line
    assert "2/9" in line
    await runtime.dispose()


@pytest.mark.asyncio
async def test_a_frozen_request_keeps_the_product_context_of_its_own_boundary(
    tmp_path,
) -> None:
    """Today's newer ProductTask head must not rewrite an older request."""

    runtime = _runtime(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = await runtime.run(workspace, "first question")
    first = _context_task("task-first", order=3)
    await _append_product_context(runtime, result.session_id, (first,), total=1)
    await runtime.run_existing(result.session_id, "second question")

    events = await runtime.sessions.read_session(result.session_id)
    frozen = [event for event in events if event.type == "request/snapshot"][-1]

    # A newer Product head lands after that request was frozen.
    later = _context_task("task-later", order=8)
    await _append_product_context(runtime, result.session_id, (later,), total=1)

    snapshot = await _reader(runtime).load(result.session_id)
    assert snapshot.product is not None
    # The current projection follows the newest snapshot ...
    assert snapshot.product.focus_task_id == "task-later"
    request = snapshot.request
    assert request is not None
    assert request.seq == frozen.seq
    # ... while the frozen request keeps what it actually carried.
    assert request.product_context_messages == 2
    assert request.product_context_utf8_bytes > 0
    assert request.source_seq < snapshot.product.snapshot_seq
    await runtime.dispose()


# -- 7. the latest frozen request -------------------------------------------


@pytest.mark.asyncio
async def test_the_frozen_request_is_read_not_recomputed(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = await runtime.run(workspace, "first question")

    events = await runtime.sessions.read_session(result.session_id)
    frozen = [event for event in events if event.type == "request/snapshot"][-1]
    before = await _reader(runtime).load(result.session_id)
    assert before.request is not None
    assert before.request.seq == frozen.seq
    assert before.request.composed_fingerprint == frozen.data["composed_fingerprint"]
    assert before.request.dispatch_fingerprint == frozen.data["dispatch_fingerprint"]
    assert before.request.dispatch_matches_composed is True
    assert before.request.provider == "scripted"
    assert before.request.model == "demo"
    assert before.request.conversation_messages >= 1
    assert before.request.product_context_messages == 0
    assert before.request.composed_utf8_bytes > 0
    assert before.request.dispatch_utf8_bytes > 0

    # Compacting afterwards must not change how that frozen request is shown.
    turn_ends = [event.seq for event in events if event.type == "turn/end"]
    await runtime.compaction.replace_through(
        result.session_id,
        through_seq=turn_ends[-1],
        summary="the first exchange, summarized later",
    )
    after = await _reader(runtime).load(result.session_id)
    assert after.request == before.request
    # The current projection did change.
    assert after.conversation_messages != before.conversation_messages
    await runtime.dispose()


@pytest.mark.asyncio
async def test_tool_results_and_system_prompt_are_classified(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "hello.txt").write_text("hello", encoding="utf-8")
    runtime = _runtime(
        tmp_path,
        responses=(
            ModelResponse(
                content="reading",
                tool_calls=(
                    ToolCall(
                        id="call-1", name="read_file", arguments={"path": "hello.txt"}
                    ),
                ),
            ),
            ModelResponse(content="done"),
        ),
    )
    result = await runtime.run(workspace, "please read")

    snapshot = await _reader(runtime).load(result.session_id)
    request = snapshot.request
    assert request is not None
    assert request.tool_schemas > 0
    assert request.tool_utf8_bytes > 0
    assert request.system_prompt_utf8_bytes > 0
    # The last Step's request already contains the Tool result.
    assert request.conversation_messages >= 3
    assert snapshot.conversation_messages >= 3
    await runtime.dispose()


# -- 8. corrupt and unreadable ----------------------------------------------


@pytest.mark.asyncio
async def test_a_store_read_failure_fails_closed_with_a_stable_code(
    tmp_path,
) -> None:
    runtime = _runtime(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = await runtime.run(workspace, "first question")

    class _Broken:
        def __init__(self, sessions):
            self._sessions = sessions

        def __getattr__(self, name):
            return getattr(self._sessions, name)

        async def read_session(self, session_id):
            raise OSError("store is unavailable")

    reader = _reader(runtime)
    reader._sessions = _Broken(runtime.sessions)  # type: ignore[attr-defined]
    with pytest.raises(ContextInspectionError) as failure:
        await reader.load(result.session_id)
    assert failure.value.code == "context-inspection-read-failed"
    assert "store is unavailable" not in context_status_line(
        None, error_code=failure.value.code
    )
    assert failure.value.code in context_status_line(
        None, error_code=failure.value.code
    )
    await runtime.dispose()


@pytest.mark.asyncio
async def test_a_malformed_replacement_fails_closed(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = await runtime.run(workspace, "first question")
    await runtime.sessions.append_session(
        result.session_id,
        "surface/replace",
        {"source_seqs": [4], "replacement": {"role": "user", "content": "legacy"}},
    )

    with pytest.raises(ContextInspectionError) as failure:
        await _reader(runtime).load(result.session_id)
    # The invariant checker rejects the superseded format first; either way the
    # projection refuses to describe the Session rather than showing half-truths.
    assert failure.value.code in {
        "context-inspection-session-invalid",
        "context-inspection-surface-invalid",
        "context-inspection-replacement-invalid",
    }
    await runtime.dispose()


@pytest.mark.asyncio
async def test_a_malformed_latest_request_snapshot_fails_closed(tmp_path) -> None:
    """An older snapshot must never be presented as the latest one."""

    runtime = _runtime(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = await runtime.run(workspace, "first question")

    events = await runtime.sessions.read_session(result.session_id)
    good = [event for event in events if event.type == "request/snapshot"][-1]
    broken = dict(good.data)
    broken.pop("composed_request")
    reader = _reader(runtime)

    class _Injected:
        def __init__(self, sessions, extra):
            self._sessions = sessions
            self._extra = extra

        def __getattr__(self, name):
            return getattr(self._sessions, name)

        async def read_session(self, session_id):
            return (*await self._sessions.read_session(session_id), self._extra)

    from traceh.api.events import EventEnvelope

    injected = EventEnvelope.materialize(
        f"session:{result.session_id}",
        events[-1].seq + 1,
        PendingEvent("request/snapshot", broken),
    )
    reader._sessions = _Injected(runtime.sessions, injected)  # type: ignore[attr-defined]
    with pytest.raises(ContextInspectionError) as failure:
        await reader.load(result.session_id)
    assert failure.value.code in {
        "context-inspection-request-invalid",
        "context-inspection-session-invalid",
    }
    await runtime.dispose()


# -- 10. the projection never writes ----------------------------------------


@pytest.mark.asyncio
async def test_loading_the_projection_writes_nothing(tmp_path) -> None:
    runtime = _runtime(tmp_path, policy=_policy(keep=0))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = await runtime.run(workspace, "first question")
    await runtime.run_existing(result.session_id, "second question")

    before = await runtime.sessions.read_session(result.session_id)
    reader = _reader(runtime)
    for _ in range(5):
        snapshot = await reader.load(result.session_id)
        context_status_line(snapshot)
        context_detail_lines(snapshot)
    after = await runtime.sessions.read_session(result.session_id)
    assert len(after) == len(before)
    assert after[-1].seq == before[-1].seq
    assert snapshot.head_seq == before[-1].seq
    assert not await runtime.check_invariants(result.session_id)
    await runtime.dispose()


# -- narrow rendering and hostile text --------------------------------------


@pytest.mark.asyncio
async def test_the_status_row_stays_one_deterministic_line(tmp_path) -> None:
    runtime = _runtime(tmp_path, policy=_policy(trigger=65_536, keep=2))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = await runtime.run(workspace, "first question")
    await _append_product_context(
        runtime, result.session_id, (_context_task("task-a7981234567890", order=5),), total=9
    )

    snapshot = await _reader(runtime).load(result.session_id)
    wide = context_status_line(snapshot, width=110)
    narrow = context_status_line(snapshot, width=40)
    for text in (wide, narrow):
        assert "\n" not in text
        assert len(text.splitlines()) == 1
    # The narrow row is shorter but still keeps the threshold.
    assert cell_len(narrow) < cell_len(wide)
    assert "64.0 KiB" in narrow and "64.0 KiB" in wide
    assert "1/9" in narrow
    assert "task-a798123…" in wide
    await runtime.dispose()


@pytest.mark.parametrize("width", (110, 80, 60, 44, 40, 30, 20, 12))
def test_the_status_row_always_fits_the_cells_it_has(width: int) -> None:
    """Every rendering is measured; Textual is never asked to clip the row."""

    from traceh.tui.context_inspection import (
        ContextCompactionFailure,
        ContextPolicyView,
        ContextProductView,
        ContextSnapshot,
    )

    snapshot = ContextSnapshot(
        session_id="session-1",
        head_seq=40,
        conversation_messages=12,
        conversation_utf8_bytes=29_104,
        visible_summaries=1,
        compactions=(),
        failures=(ContextCompactionFailure(3, "compaction-write-unknown", None),),
        product=ContextProductView(
            9, "cid", "task-a798123456789", "completed", 6, 9, 3, (), 2, 100
        ),
        request=None,
        policy=ContextPolicyView(True, 65_536, 8_192, 2, "d" * 64),
    )
    row = context_status_line(snapshot, width=width)
    assert cell_len(row) <= width
    assert len(row.splitlines()) == 1


@pytest.mark.parametrize("width", (110, 60, 44, 40, 34, 30))
def test_a_failure_code_survives_every_width_it_can_fit(width: int) -> None:
    """The stable code is the payload of the error row, so it is dropped last."""

    code = "context-inspection-read-failed"
    row = context_status_line(None, error_code=code, width=width)
    assert cell_len(row) <= width
    # The code fits whole down to exactly its own width.
    assert code in row


def test_an_impossibly_narrow_row_is_cut_explicitly() -> None:
    row = context_status_line(
        None, error_code="context-inspection-read-failed", width=12
    )
    assert cell_len(row) <= 12
    assert row.endswith("…")


@pytest.mark.asyncio
async def test_a_hostile_summary_cannot_break_the_detail_layout(tmp_path) -> None:
    hostile = "esc\x1b[31m rtl‮private next"

    class _Summarizer:
        @property
        def identity(self):
            from traceh.session.compaction import BoundedHistorySummarizer

            return BoundedHistorySummarizer().identity

        async def summarize(self, request) -> str:
            return hostile

    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "data",
            provider="scripted",
            model="demo",
            compaction=_policy(keep=0),
        ),
        provider=ScriptedLlmProvider(
            (ModelResponse(content="answer"),), repeat_last=True
        ),
        event_store=InMemoryEventStore(),
        summarizer=_Summarizer(),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = await runtime.run(workspace, "first question")
    await runtime.compaction.compact_before_turn(result.session_id)

    snapshot = await _reader(runtime).load(result.session_id)
    rows = context_detail_lines(snapshot)
    rendered = "\n".join(row.plain for row in rows)
    assert "不可信历史摘要" in rendered
    from unicodedata import category

    assert not any(
        category(character) in {"Cc", "Cf", "Cs", "Co", "Zl", "Zp"}
        for row in rows
        for character in row.plain
    )
    await runtime.dispose()


def test_detail_styles_never_bleed_into_following_rows() -> None:
    """A bold section heading must not style the plain rows after it."""

    from traceh.tui.context_inspection import ContextSnapshot

    snapshot = ContextSnapshot(
        session_id="session-1",
        head_seq=12,
        conversation_messages=4,
        conversation_utf8_bytes=1234,
        visible_summaries=0,
        compactions=(),
        failures=(),
        product=None,
        request=None,
        policy=None,
    )
    rows = context_detail_lines(snapshot)
    headings = [row for row in rows if row.plain in {"当前投影", "自动压缩"}]
    assert headings
    for row in headings:
        # The heading carries its own style and owns no spans, so the style
        # cannot reach any following row.
        assert row.style == "bold"
        assert row.spans == []
    for row in rows:
        # Every span is bounded by its own row; nothing runs past the end.
        for span in row.spans:
            assert 0 <= span.start <= span.end <= len(row.plain)
        if row.plain.strip().startswith("Session "):
            assert row.style == ""
            assert all("bold" not in str(span.style) for span in row.spans)
            # The label is dim, the value is unstyled: the dim span must stop
            # before the value begins.
            dim = [span for span in row.spans if str(span.style) == "dim"]
            assert dim and dim[0].end < len(row.plain)
