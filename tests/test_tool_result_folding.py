"""Stage C: actual Tool/SQLite facts, Surface pairing, recovery and exact replay."""

import asyncio
import json
from dataclasses import replace

import pytest
from sandbox_fixtures import real_sandbox_policy
from test_history_runtime import policy as history_policy
from test_retained_tool_output import write_emitter

from traceh.api.history import HistoryReadPolicy
from traceh.api.json_types import canonical_json
from traceh.api.llm import ModelResponse, ToolCall
from traceh.api.sandbox import SandboxConfiguration
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.compaction import CompactionError, CompactionPolicy
from traceh.session.history import read_history
from traceh.session.sqlite import SqliteEventStore
from traceh.session.surface_replacement import (
    SURFACE_REPLACE,
    SurfaceToolFold,
    parse_surface_replacement,
    surface_utf8_bytes,
)
from traceh.tui.presentation import compaction_notice_text


def open_runtime(
    root,
    responses,
    *,
    threshold=10_000_000,
    keep=1,
    summarizer=None,
    enabled=True,
    output_limit=2000,
):
    store = SqliteEventStore(root / "events")
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=root,
            sandbox=SandboxConfiguration(real_sandbox_policy(), root / "artifacts"),
            max_tool_output_chars=output_limit,
            compaction=CompactionPolicy(enabled, threshold, 400, keep),
            context_input=history_policy(),
        ),
        provider=ScriptedLlmProvider(tuple(responses), repeat_last=True),
        event_store=store,
        summarizer=summarizer,
    )
    return runtime, store


async def prepare_history(tmp_path, *, failing=False, pair=False, output_limit=2000):
    work = tmp_path / "work"
    work.mkdir()
    command, _ = write_emitter(work, failing=failing)
    calls = (ToolCall("original", "shell", {"command": command}),)
    if pair:
        calls += (ToolCall("short", "list_files", {}),)
    runtime, store = open_runtime(
        tmp_path / "data",
        [
            ModelResponse(tool_calls=calls),
            ModelResponse(content="captured"),
            ModelResponse(content="recent answer"),
        ],
        output_limit=output_limit,
    )
    try:
        sid = await runtime.create_session(work)
        await runtime.run_existing(sid, "Execute the diagnostic once.")
        await runtime.run_existing(sid, "Keep this recent conversation.")
        events = await runtime.sessions.read_session(sid)
        original = next(e for e in events if e.type == "tool/result")
        before = runtime.surface.project(events)
        threshold = surface_utf8_bytes(before) - 1
        return sid, original, events, before, threshold
    finally:
        await runtime.dispose()
        await store.aclose()


@pytest.mark.parametrize("boundary", ["disabled", "recent", "inline"])
async def test_fold_skips_disabled_protected_or_unhelpful_sources(tmp_path, boundary):
    sid, _, _, before, _ = await prepare_history(
        tmp_path, output_limit=1_000_000 if boundary == "inline" else 2000
    )
    runtime, store = open_runtime(
        tmp_path / "data",
        [ModelResponse(content="answer")],
        threshold=1,
        enabled=boundary != "disabled",
        keep=2 if boundary == "recent" else 1,
    )
    try:
        report = await runtime.compaction.compact_before_turn(sid)
        events = await runtime.sessions.read_session(sid)
        assert not any(
            e.type == SURFACE_REPLACE and e.data["method"] == "tool-fold" for e in events
        )
        if boundary != "inline":
            assert report is None
            assert runtime.surface.project(events) == before
        else:
            assert report.method == "automatic"  # Fall through to the existing M3 layer.
        assert not await runtime.check_invariants(sid)
    finally:
        await runtime.dispose()
        await store.aclose()


@pytest.mark.parametrize("fail_summary", [False, True])
async def test_fold_then_summary_keeps_recent_and_reports_partial_failure(tmp_path, fail_summary):
    from traceh.session.compaction import BoundedHistorySummarizer

    class Summary(BoundedHistorySummarizer):
        async def summarize(self, request):
            if fail_summary:
                raise RuntimeError("test summarizer failure")
            return await super().summarize(request)

    sid, _, _, before, _ = await prepare_history(tmp_path)
    runtime, store = open_runtime(
        tmp_path / "data", [ModelResponse(content="new answer")], threshold=1, summarizer=Summary()
    )
    try:
        # Public runtime also persists and renders the partial-maintenance failure.
        await runtime.run_existing(sid, "A new question.")
        events = await runtime.sessions.read_session(sid)
        replacements = [e for e in events if e.type == SURFACE_REPLACE]
        assert [e.data["method"] for e in replacements] == (
            ["tool-fold"] if fail_summary else ["tool-fold", "automatic"]
        )
        projected = runtime.surface.project(events)
        assert before[-2:] == projected[-4:-2]
        failures = [e for e in events if e.type == "surface/compaction-failed"]
        if fail_summary:
            assert len(failures) == 1 and failures[0].data["committed"] is True
            assert "compaction-after-tool-fold-" in failures[0].data["code"]
            assert "部分工具结果已折叠" in compaction_notice_text(failures[0])
        else:
            assert not failures
        assert not await runtime.check_invariants(sid)
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, sid)
    finally:
        await runtime.dispose()
        await store.aclose()


async def test_fold_refuses_missing_effect_source_before_commit(tmp_path, monkeypatch):
    sid, _, _, before, threshold = await prepare_history(tmp_path)
    runtime, store = open_runtime(
        tmp_path / "data", [ModelResponse(content="answer")], threshold=threshold
    )
    try:

        async def absent_effects(session_id):
            return ()

        monkeypatch.setattr(runtime.sessions, "read_effects", absent_effects)
        with pytest.raises(CompactionError) as failure:
            await runtime.compaction.compact_before_turn(sid)
        assert failure.value.code == "compaction-tool-fold-source-invalid"
        assert failure.value.committed is False
        assert runtime.surface.project(await runtime.sessions.read_session(sid)) == before
    finally:
        await runtime.dispose()
        await store.aclose()


@pytest.mark.parametrize("failing", [False, True])
async def test_automatic_fold_keeps_pair_recent_turn_and_readback_after_restart(tmp_path, failing):
    sid, original, before_events, before, threshold = await prepare_history(
        tmp_path, failing=failing, pair=True
    )
    ref = original.data["output_ref"]
    identity = {k: ref[k] for k in ("effect_id", "digest")}
    runtime, store = open_runtime(
        tmp_path / "data",
        [
            ModelResponse(
                tool_calls=(ToolCall("find", "search_tool_output", identity | {"query": "END:"}),)
            ),
            ModelResponse(content="found original"),
        ],
        threshold=threshold,
    )
    try:
        await runtime.run_existing(sid, "Find the original endpoint.")
        events = await runtime.sessions.read_session(sid)
        folds = [e for e in events if e.type == SURFACE_REPLACE]
        assert len(folds) == 1
        fold = parse_surface_replacement(folds[0])
        assert isinstance(fold, SurfaceToolFold)
        assert fold.source_seqs == (original.seq,)
        assert fold.message.tool_call_id == "original"
        assert fold.message.name == "shell"
        assert json.loads(fold.message.content)["output_ref"] == ref
        projected = runtime.surface.project(events, through_seq=folds[0].seq)
        assert len(projected) == len(before)
        for old, new in zip(before, projected, strict=True):
            if old.tool_call_id == "original":
                assert new == fold.message
            else:
                assert new == old  # Includes unchanged call arguments and recent dialogue.
        assert surface_utf8_bytes(projected) < threshold
        assert runtime.surface.project(events, through_seq=before_events[-1].seq) == before
        found = next(
            e
            for e in events
            if e.type == "tool/result" and e.data["tool_name"] == "search_tool_output"
        )
        assert found.data["status"] == "succeeded"
        assert "code-9472" in found.data["content"]
        assert (tmp_path / "work" / "executions.txt").read_text() == "x"
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, sid)
        assert not await runtime.check_invariants(sid)
        assert "折叠" in compaction_notice_text(folds[0])
        from traceh.tui.context_inspection import ContextInspectionReader

        inspected = await ContextInspectionReader(runtime.sessions).load(sid)
        assert inspected.visible_summaries == 0
        assert inspected.latest_compaction.method == "tool-fold"
        # Later M3 can consume the fold without breaking original History leaves.
        await runtime.compaction.replace_through(
            sid, through_seq=events[-1].seq, summary="Earlier diagnostic and conversation."
        )
        after = await runtime.sessions.read_session(sid)
        snapshot = read_history(
            after,
            session_id=sid,
            through_seq=after[-1].seq,
            policy=HistoryReadPolicy(
                max_blocks=8,
                max_depth=16,
                page_messages=100,
                max_source_events=5000,
                max_requests=16,
                max_source_bytes=4_000_000,
                page_bytes=20000,
            ),
        )
        root = snapshot.directory()[0]
        leaves = snapshot.leaf_refs(root.block_id)
        assert original.seq in [leaf["seq"] for leaf in leaves]
        assert folds[0].seq not in [leaf["seq"] for leaf in leaves]
        assert not await runtime.check_invariants(sid)
    finally:
        await runtime.dispose()
        await store.aclose()


@pytest.mark.parametrize("failure", ["before-write", "after-write", "cancel", "race"])
async def test_fold_commit_failure_cancel_and_cas_converge(tmp_path, monkeypatch, failure):
    sid, original, _, _, threshold = await prepare_history(tmp_path)
    runtime, store = open_runtime(
        tmp_path / "data", [ModelResponse(content="answer")], threshold=threshold
    )
    append = runtime.sessions.append_session
    entered, release = asyncio.Event(), asyncio.Event()
    injected = False

    async def inject(session_id, event_type, data, **kwargs):
        nonlocal injected
        target = event_type == SURFACE_REPLACE and data["method"] == "tool-fold"
        if target and not injected:
            injected = True
            if failure == "before-write":
                raise OSError("injected before commit")
            if failure == "race":
                await append(session_id, "fixture/race", {})
            event = await append(session_id, event_type, data, **kwargs)
            entered.set()
            if failure == "cancel":
                await release.wait()
            if failure == "after-write":
                raise OSError("injected after commit")
            return event
        return await append(session_id, event_type, data, **kwargs)

    monkeypatch.setattr(runtime.sessions, "append_session", inject)
    task = asyncio.create_task(runtime.compaction.compact_before_turn(sid))
    try:
        if failure == "cancel":
            await asyncio.wait_for(entered.wait(), 10)
            task.cancel()
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif failure == "before-write":
            with pytest.raises(CompactionError) as raised:
                await task
            assert raised.value.committed is False
        else:
            assert (await task).method == "tool-fold"
        events = await runtime.sessions.read_session(sid)
        folds = [e for e in events if e.type == SURFACE_REPLACE]
        assert len(folds) == (0 if failure == "before-write" else 1)
        assert (tmp_path / "work" / "executions.txt").read_text() == "x"
        assert not await runtime.check_invariants(sid)
        if folds:
            assert folds[0].data["source_seqs"] == [original.seq]
            assert await runtime.compaction.compact_before_turn(sid) is None
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.dispose()
        await store.aclose()


@pytest.mark.parametrize("change", ["message", "source", "cut", "bytes", "kept"])
async def test_fold_forgery_rejected_by_shared_source_checker(tmp_path, change):
    from traceh.session.invariants import check_surface_replacement_sources

    sid, _, _, _, threshold = await prepare_history(tmp_path)
    runtime, store = open_runtime(
        tmp_path / "data", [ModelResponse(content="answer")], threshold=threshold
    )
    try:
        await runtime.compaction.compact_before_turn(sid)
        events = await runtime.sessions.read_session(sid)
        fold = next(e for e in events if e.type == SURFACE_REPLACE)
        data = json.loads(canonical_json(fold.data))
        if change == "message":
            data["replacement"]["tool_call_id"] = "another-call"
        elif change == "source":
            data["source_seqs"] = [next(e.seq for e in events if e.type == "user/message")]
        elif change == "cut":
            data["cut_seq"] -= 1
        elif change == "kept":
            data["kept_recent_turns"] += 1
        else:
            data["source_utf8_bytes"] += 1
        forged = tuple(replace(e, data=data) if e.seq == fold.seq else e for e in events)
        assert check_surface_replacement_sources(forged)
        assert not check_surface_replacement_sources(events)
    finally:
        await runtime.dispose()
        await store.aclose()
