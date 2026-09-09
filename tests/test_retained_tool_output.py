"""Real SQLite/Tool execution and recovery; cloud journeys are run separately."""

import asyncio
import json
import shlex
import sys
from dataclasses import replace

import pytest

from traceh.api.json_types import canonical_json, fingerprint
from traceh.api.llm import ModelResponse, ToolCall
from traceh.api.tools import EffectKind, ToolExecutionContext, ToolOutput
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.invariants import CoreInvariantChecker
from traceh.session.recovery import RecoveryService
from traceh.session.service import SessionService
from traceh.session.sqlite import SqliteEventStore
from traceh.session.tool_output import (
    render_output_page,
    render_output_search,
    resolve_tool_output,
)
from traceh.tools.builtins.shell import ShellTool
from traceh.tools.output import ListToolOutputs, ReadToolOutput, SearchToolOutput
from traceh.tools.policy import AllowByDefaultPolicy
from traceh.tools.registry import ToolRegistry
from traceh.tools.runtime import ToolRuntime


def write_emitter(workspace, *, failing=False):
    payload = '测量🙂 "quoted"\n' * 400 + "END: station-Q / code-9472"
    script = workspace / "emit.py"
    script.write_text(
        "from pathlib import Path\nimport sys\n"
        'p=Path("executions.txt")\np.write_text(p.read_text()+"x" if p.exists() else "x")\n'
        f'sys.stdout.buffer.write({payload!r}.encode("utf-8"))\nsys.exit({7 if failing else 0})\n',
        encoding="utf-8",
    )
    return shlex.join([sys.executable, str(script)]), payload


def make_runtime(root, responses):
    store = SqliteEventStore(root / "events")
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=root, max_tool_output_chars=1024),
        provider=ScriptedLlmProvider(tuple(responses)),
        event_store=store,
    )
    return runtime, store


def search_page(text, query, **options):
    arguments = dict(
        effect_id="fixture",
        digest="d" * 64,
        query=query,
        part="content",
        offset=0,
        count=10,
        context_lines=2,
        case_sensitive=True,
        max_chars=1000,
    )
    arguments.update(options)
    encoded = render_output_search(
        {"content": text, "data": {"detail": text}, "evidence": []}, **arguments
    )
    assert len(encoded) <= arguments["max_chars"]
    return json.loads(encoded)


@pytest.mark.parametrize("part", ["content", "data"])
def test_search_literal_offsets_unicode_and_long_line(part):
    text = "a" * 6000 + "测🙂[x].*" + '\\"\n' * 2000
    source = text if part == "content" else canonical_json({"detail": text})
    page = search_page(text, "测🙂[x].*", part=part, max_chars=950)
    assert len(page["matches"]) == 1
    hit = page["matches"][0]
    assert source[hit["match_start"] : hit["match_end"]] == "测🙂[x].*"
    snippet = hit["before_context"] + hit["matched_lines"] + hit["after_context"]
    assert snippet == source[hit["text_offset"] : hit["text_offset"] + len(snippet)]
    assert hit["context_truncated"]
    assert page["next_offset"] is None
    assert search_page(text, "[missing].*")["matches"] == []


def test_search_pagination_preserves_repeated_matches_and_context():
    text = "before\r\n测🙂 Needle\r\nnear\r\nNeedle again\r\nafter\r\nNeedle"
    offsets, cursor = [], 0
    while cursor is not None:
        page = search_page(text, "needle", case_sensitive=False, offset=cursor, count=1)
        hit = page["matches"][0]
        offsets.append(hit["match_start"])
        assert hit["line_number"] == text.count("\n", 0, hit["match_start"]) + 1
        cursor = page["next_offset"]
    assert offsets == [text.index("Needle"), text.index("Needle again"), text.rindex("Needle")]
    assert search_page(text, "needle")["matches"] == []
    assert search_page(text, "Needle", offset=len(text))["matches"] == []
    assert search_page("İ🙂 X", "i", case_sensitive=False)["matches"][0]["match_end"] == 1


def test_search_budget_pages_and_expansion_start_at_match_not_previous_record():
    text = ("previous value\n\nneedle\n" + "body🙂\n" * 12) * 30
    offsets, cursor = [], 0
    while cursor is not None:
        page = search_page(text, "needle", offset=cursor, count=100, max_chars=1000)
        assert page["matches"]
        for hit in page["matches"]:
            offsets.append(hit["match_start"])
            assert "needle" in hit["matched_lines"]
            assert "previous value" not in hit["matched_lines"]
            action = hit["read_action"]
            expanded = json.loads(
                render_output_page(
                    {"content": text, "data": {}, "evidence": []},
                    **action["arguments"],
                    max_chars=1000,
                )
            )
            assert expanded["text"].startswith("needle\nbody🙂")
        cursor = page["next_offset"]
    assert len(offsets) == 30
    assert offsets == sorted(set(offsets))


@pytest.mark.parametrize(
    "options,code",
    [
        ({"query": ""}, "query-invalid"),
        ({"offset": -1}, "offset-invalid"),
        ({"max_chars": 2}, "page-budget-too-small"),
        ({"context_lines": 21}, "context-invalid"),
    ],
)
def test_search_invalid_request_or_budget_is_explicit(options, code):
    with pytest.raises(ValueError, match=code):
        search_page("original source", **({"query": "source"} | options))


async def test_search_restart_then_expand_on_real_runtime_and_sqlite(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    command, expected = write_emitter(workspace)
    root = tmp_path / "data"
    runtime, store = make_runtime(
        root,
        [
            ModelResponse(tool_calls=(ToolCall("emit", "shell", {"command": command}),)),
            ModelResponse(content="captured"),
        ],
    )
    try:
        sid = await runtime.create_session(workspace)
        await runtime.run_existing(sid, "Execute once")
        events = await runtime.sessions.read_session(sid)
        ref = next(e.data["output_ref"] for e in events if e.type == "tool/result")
        preview = json.loads(next(e.data["content"] for e in events if e.type == "tool/result"))
        assert preview["search_tool"] == "search_tool_output"
        assert "read_action" not in preview
        assert "preview" not in preview
        assert "Content is not loaded here" in preview["notice"]
        assert expected[:200] not in canonical_json(preview)
        await runtime.compaction.replace_through(
            sid, through_seq=events[-1].seq, summary="A diagnostic was executed."
        )
    finally:
        await runtime.dispose()
        await store.aclose()
    identity = {key: ref[key] for key in ("effect_id", "digest")}
    runtime, store = make_runtime(
        root,
        [
            ModelResponse(tool_calls=(ToolCall("list", "list_tool_outputs", {}),)),
            ModelResponse(
                tool_calls=(ToolCall("grep", "search_tool_output", identity | {"query": "END:"}),)
            ),
            ModelResponse(content="found"),
        ],
    )
    try:
        await runtime.run_existing(sid, "Find the terminal record")
        events = await runtime.sessions.read_session(sid)
        result = next(
            e
            for e in events
            if e.type == "tool/result" and e.data["tool_name"] == "search_tool_output"
        )
        assert result.data["status"] == "succeeded"
        assert "output_ref" not in result.data
        page = json.loads(result.data["content"])
        hit = page["matches"][0]
        assert hit["before_context"] == hit["after_context"] == ""
        assert "station-Q / code-9472" in hit["matched_lines"]
        read = ReadToolOutput(runtime.sessions, max_chars=1024)
        context = ToolExecutionContext(sid, "read", "read", "read", workspace, root)
        expanded = await read.execute(hit["read_action"]["arguments"], context)
        expanded_body = json.loads(expanded.content)
        assert "station-Q / code-9472" in expanded_body["text"]
        # Reading the tail reaches EOF but does not read the earlier source.
        assert expanded_body.get("body_status") == "source-excerpt"
        assert expanded_body["next_offset"] is None
        assert expanded_body["offset"] > 0
        assert expanded_body["end_offset"] == expanded_body["total_chars"]
        assert page["match_mode"] == "literal-substring"
        assert expected.endswith("END: station-Q / code-9472")
        assert (workspace / "executions.txt").read_text() == "x"
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, sid)
        assert not runtime.invariants.check(events, await runtime.sessions.read_effects(sid))
    finally:
        await runtime.dispose()
        await store.aclose()


@pytest.mark.parametrize("failure", ["session", "digest", "workspace", "empty", "cancel"])
async def test_search_public_path_rejects_wrong_source_and_converges(
    tmp_path, monkeypatch, failure
):
    runtime, sessions, store, context, call = await raw_batch(tmp_path)
    try:
        (original,) = await runtime.execute_batch(
            (call,), context=context, composition_revision="r"
        )
        arguments = {key: original.output_ref[key] for key in ("effect_id", "digest")}
        arguments["query"] = "station-Q"
        target = context
        if failure == "session":
            target = replace(context, session_id=await sessions.create_session(context.workspace))
        elif failure == "digest":
            arguments["digest"] = "0" * 64
        elif failure == "workspace":
            target = replace(context, workspace=tmp_path)
        elif failure == "empty":
            arguments["query"] = ""
        else:
            entered, release = asyncio.Event(), asyncio.Event()
            original_read = sessions.read_effects

            async def gated(sid):
                result = await original_read(sid)
                entered.set()
                await release.wait()
                return result

            monkeypatch.setattr(sessions, "read_effects", gated)
        task = asyncio.create_task(
            runtime.execute_batch(
                (ToolCall("grep", "search_tool_output", arguments),),
                context=target,
                composition_revision="r",
            )
        )
        if failure == "cancel":
            try:
                await asyncio.wait_for(entered.wait(), 10)
                task.cancel()
                task.cancel()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            events = await sessions.read_session(context.session_id)
            outcomes = [
                e for e in events if e.type == "tool/result" and e.data["tool_call_id"] == "grep"
            ]
            assert len(outcomes) == 1
            assert outcomes[0].data["status"] == "cancelled"
        else:
            (result,) = await task
            assert result.status in {"failed", "invalid"}
            assert "code-9472" not in result.content
        assert (context.workspace / "executions.txt").read_text() == "x"
    finally:
        await store.aclose()


@pytest.mark.parametrize("failing", [False, True])
async def test_real_shell_survives_restart_and_pages_without_reexecution(tmp_path, failing):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    command, expected = write_emitter(workspace, failing=failing)
    root = tmp_path / "data"
    runtime, store = make_runtime(
        root,
        [
            ModelResponse(tool_calls=(ToolCall("emit", "shell", {"command": command}),)),
            ModelResponse(content="captured"),
        ],
    )
    try:
        sid = await runtime.create_session(workspace)
        await runtime.run_existing(sid, "Run the diagnostic once.")
        events = await runtime.sessions.read_session(sid)
        result = next(e for e in events if e.type == "tool/result")
        reference = result.data["output_ref"]
        assert result.data["data"] == {}
        assert "END: station-Q" not in result.data["content"]
        assert len(result.data["content"]) < 3000
        effects = await runtime.sessions.read_effects(sid)
        original = resolve_tool_output(
            events,
            effects,
            session_id=sid,
            effect_id=reference["effect_id"],
            digest=reference["digest"],
        )
        assert expected in original["content"]
        assert original["data"]["exit_code"] == (7 if failing else 0)
        assert original["data"]["stdout"] == expected
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, sid)
        assert not runtime.invariants.check(events, effects)
        # Compaction removes the original preview from Surface, not its source.
        await runtime.compaction.replace_through(
            sid,
            through_seq=events[-1].seq,
            summary="A diagnostic was executed. Original Tool output remains available.",
        )
    finally:
        await runtime.dispose()
        await store.aclose()

    text = original["content"]
    calls = tuple(
        ToolCall(
            f"page-{offset}",
            "read_tool_output",
            {
                "effect_id": reference["effect_id"],
                "digest": reference["digest"],
                "offset": offset,
                "count": 400,
            },
        )
        for offset in range(0, len(text), 400)
    )
    calls += (
        ToolCall(
            "data-page",
            "read_tool_output",
            {
                "effect_id": reference["effect_id"],
                "digest": reference["digest"],
                "part": "data",
                "offset": 0,
                "count": 120,
            },
        ),
    )
    runtime, store = make_runtime(
        root,
        [
            ModelResponse(tool_calls=(ToolCall("directory", "list_tool_outputs", {}),)),
            ModelResponse(tool_calls=calls),
            ModelResponse(content="read"),
        ],
    )
    try:
        await runtime.run_existing(sid, "Read the original diagnostic evidence.")
        events = await runtime.sessions.read_session(sid)
        catalog = next(
            e
            for e in events
            if e.type == "tool/result" and e.data["tool_name"] == "list_tool_outputs"
        )
        assert json.loads(catalog.data["content"])["outputs"][0]["output_ref"] == reference
        results = [
            e
            for e in events
            if e.type == "tool/result" and e.data["tool_name"] == "read_tool_output"
        ]
        assert all(e.data["status"] == "succeeded" for e in results)
        assert all("output_ref" not in e.data for e in results)  # No recursive persistence.
        pages = [json.loads(e.data["content"]) for e in results[:-1]]
        assert "".join(page["text"] for page in pages) == text
        assert pages[-1]["next_offset"] is None
        assert (
            json.loads(results[-1].data["content"])["text"]
            == canonical_json(original["data"])[:120]
        )
        assert (workspace / "executions.txt").read_text() == "x"
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, sid)
        assert not runtime.invariants.check(events, await runtime.sessions.read_effects(sid))
    finally:
        await runtime.dispose()
        await store.aclose()


async def raw_batch(tmp_path, *, sessions=None, reader=True, searcher=True):
    workspace = tmp_path / "work"
    workspace.mkdir(exist_ok=True)
    command, _ = write_emitter(workspace)
    store = SqliteEventStore(tmp_path / "events") if sessions is None else None
    sessions = sessions or SessionService(store)
    sid = await sessions.create_session(workspace)
    await sessions.append_session(sid, "turn/start", {"turn_id": "turn"})
    await sessions.append_session(sid, "step/start", {"turn_id": "turn", "step_id": "step"})
    registry = ToolRegistry()
    registry.register(ShellTool())
    if reader:
        registry.register(ReadToolOutput(sessions, max_chars=1024))
    registry.register(ListToolOutputs(sessions, max_chars=1024))
    if searcher:
        registry.register(SearchToolOutput(sessions, max_chars=1024))
    runtime = ToolRuntime(
        registry, sessions, policies=(AllowByDefaultPolicy(),), max_output_chars=1024
    )
    context = ToolExecutionContext(sid, "turn", "step", "batch", workspace, tmp_path)
    return runtime, sessions, store, context, ToolCall("emit", "shell", {"command": command})


@pytest.mark.parametrize(
    "reader,searcher", [(False, False), (True, False), (False, True), (True, True)]
)
async def test_large_output_presentation_uses_actual_lookup_composition(tmp_path, reader, searcher):
    runtime, sessions, store, context, call = await raw_batch(
        tmp_path, reader=reader, searcher=searcher
    )
    try:
        (result,) = await runtime.execute_batch((call,), context=context, composition_revision="r")
        assert result.status == "succeeded"
        display = json.loads(result.content)
        events = await sessions.read_session(context.session_id)
        effects = await sessions.read_effects(context.session_id)
        ref = result.output_ref
        original = resolve_tool_output(
            events,
            effects,
            session_id=context.session_id,
            effect_id=ref["effect_id"],
            digest=ref["digest"],
        )
        assert len(original["content"]) > 1024
        if reader or searcher:
            assert "preview" not in display
            assert "Content is not loaded here" in display["notice"]
        else:
            assert display["preview"] == original["content"][:1024]
            assert "Preview is incomplete" in display["notice"]
        assert ("search_tool" in display) == searcher
        assert ("read_action" in display) == (reader and not searcher)
        assert (context.workspace / "executions.txt").read_text() == "x"
        assert not CoreInvariantChecker().check(events, effects)
    finally:
        await store.aclose()


async def test_cross_session_and_forged_digest_are_denied_by_real_tool_path(tmp_path):
    runtime, sessions, store, context, call = await raw_batch(tmp_path)
    try:
        (result,) = await runtime.execute_batch((call,), context=context, composition_revision="r")
        ref = result.output_ref
        other = await sessions.create_session(context.workspace)
        for sid, digest in [(other, ref["digest"]), (context.session_id, "0" * 64)]:
            wrong = replace(context, session_id=sid)
            (rejected,) = await runtime.execute_batch(
                (
                    ToolCall(
                        f"read-{sid}",
                        "read_tool_output",
                        {
                            "effect_id": ref["effect_id"],
                            "digest": digest,
                        },
                    ),
                ),
                context=wrong,
                composition_revision="r",
            )
            assert rejected.status == "failed"
            assert "station-Q" not in rejected.content
        (accepted,) = await runtime.execute_batch(
            (
                ToolCall(
                    "read-good",
                    "read_tool_output",
                    {
                        "effect_id": ref["effect_id"],
                        "digest": ref["digest"],
                    },
                ),
            ),
            context=context,
            composition_revision="r",
        )
        assert accepted.status == "succeeded"
        assert (context.workspace / "executions.txt").read_text() == "x"
    finally:
        await store.aclose()


@pytest.mark.parametrize("forgery", ["content", "call", "step", "reference"])
async def test_recomputed_payload_cannot_borrow_another_execution(tmp_path, forgery):
    runtime, sessions, store, context, call = await raw_batch(tmp_path)
    try:
        (result,) = await runtime.execute_batch((call,), context=context, composition_revision="r")
        events = await sessions.read_session(context.session_id)
        effects = await sessions.read_effects(context.session_id)
        outcome = next(e for e in effects if e.type == "effect/outcome")
        if forgery == "content":
            outcome.data["retained_output"]["content"] += "forged"
            outcome.data["output_ref"]["digest"] = fingerprint(outcome.data["retained_output"])
        elif forgery == "call":
            outcome.data["tool_call_id"] = "unrelated"
        elif forgery == "step":
            next(e for e in effects if e.type == "effect/intent").data["step_id"] = "unrelated"
        else:
            next(e for e in events if e.type == "tool/result").data["output_ref"]["format"] = True
        with pytest.raises(ValueError, match="tool-output-"):
            resolve_tool_output(
                events,
                effects,
                session_id=context.session_id,
                effect_id=result.effect_id,
                digest=result.output_ref["digest"],
            )
    finally:
        await store.aclose()


@pytest.mark.parametrize("cancel", [False, True])
async def test_committed_output_converges_then_recovers_without_rerun(
    tmp_path, monkeypatch, cancel
):
    runtime, sessions, store, context, call = await raw_batch(tmp_path)
    original_append = sessions.append_effect
    committed = asyncio.Event()
    release = asyncio.Event()

    async def commit_then_fail(*args, **kwargs):
        event = await original_append(*args, **kwargs)
        if event.type == "effect/outcome":
            committed.set()
            await release.wait()
            if not cancel:
                raise OSError("injected post-commit failure")
        return event

    monkeypatch.setattr(sessions, "append_effect", commit_then_fail)
    task = asyncio.create_task(
        runtime.execute_batch((call,), context=context, composition_revision="r")
    )
    try:
        await asyncio.wait_for(committed.wait(), 10)
        if cancel:
            task.cancel()
            task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError if cancel else OSError):
            await task
        monkeypatch.setattr(sessions, "append_effect", original_append)
        recovery = RecoveryService(sessions)
        await recovery.recover(context.session_id)
        assert not (await recovery.recover(context.session_id)).changed
        events = await sessions.read_session(context.session_id)
        effects = await sessions.read_effects(context.session_id)
        results = [e for e in events if e.type == "tool/result"]
        assert len(results) == 1
        assert len([e for e in effects if e.type == "effect/outcome"]) == 1
        ref = results[0].data["output_ref"]
        payload = resolve_tool_output(
            events,
            effects,
            session_id=context.session_id,
            effect_id=ref["effect_id"],
            digest=ref["digest"],
        )
        assert "END: station-Q" in payload["content"]
        assert (context.workspace / "executions.txt").read_text() == "x"
        assert not CoreInvariantChecker().check(events, effects)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await store.aclose()


def test_page_budget_counts_json_escaping_and_never_splits_unicode():
    text = '\n"测🙂\\' * 400
    payload = {"content": text, "data": {}, "evidence": []}
    offset, restored = 0, ""
    while offset is not None:
        page = render_output_page(
            payload,
            effect_id="fixture",
            digest="d" * 64,
            part="content",
            offset=offset,
            count=300,
            max_chars=400,
        )
        assert len(page) <= 400
        raw = json.loads(page)
        assert raw["end_offset"] == raw["offset"] + len(raw["text"])
        assert raw["body_status"] == (
            "complete-source"
            if raw["offset"] == 0 and raw["end_offset"] == len(text)
            else "source-excerpt"
        )
        restored += raw["text"]
        offset = raw["next_offset"]
    assert restored == text
    with pytest.raises(ValueError, match="page-budget-too-small"):
        render_output_page(
            payload,
            effect_id="fixture",
            digest="d" * 64,
            part="content",
            offset=0,
            count=1,
            max_chars=2,
        )


@pytest.mark.parametrize("mode", ["data", "timeout", "exception"])
async def test_large_data_and_reported_failures_use_the_same_retention(tmp_path, mode):
    runtime, sessions, store, context, _ = await raw_batch(tmp_path)
    body = "故障定位🙂" * 700

    class PayloadTool:
        name = "fixture_output"
        description = "Fixture output"
        effect_kind = EffectKind.PURE_READ
        input_schema = {"type": "object", "properties": {}, "additionalProperties": False}

        async def execute(self, arguments, context):
            if mode == "timeout":
                raise TimeoutError(body)
            if mode == "exception":
                raise ValueError(body)
            return ToolOutput("short", {"detail": body})

    runtime.registry.register(PayloadTool())
    try:
        (result,) = await runtime.execute_batch(
            (ToolCall("payload", "fixture_output", {}),),
            context=context,
            composition_revision="r",
        )
        assert result.status == ("succeeded" if mode == "data" else "failed")
        assert "preview" not in json.loads(result.content)
        assert result.data == {}
        ref = result.output_ref
        payload = resolve_tool_output(
            await sessions.read_session(context.session_id),
            await sessions.read_effects(context.session_id),
            session_id=context.session_id,
            effect_id=ref["effect_id"],
            digest=ref["digest"],
        )
        assert (payload["data"]["detail"] if mode == "data" else payload["content"]).endswith(body)
        (page,) = await runtime.execute_batch(
            (
                ToolCall(
                    "read",
                    "read_tool_output",
                    {
                        "effect_id": ref["effect_id"],
                        "digest": ref["digest"],
                        "part": "data" if mode == "data" else "content",
                        "count": 100,
                    },
                ),
            ),
            context=context,
            composition_revision="r",
        )
        assert page.status == "succeeded"
        assert "故障定位" in json.loads(page.content)["text"]
    finally:
        await store.aclose()


async def test_failed_outcome_commit_publishes_no_reference_and_recovery_does_not_repeat(
    tmp_path,
    monkeypatch,
):
    runtime, sessions, store, context, call = await raw_batch(tmp_path)
    append = sessions.append_effect

    async def fail_before_commit(*args, **kwargs):
        if args[1] == "effect/outcome":
            raise OSError("injected unavailable Store")
        return await append(*args, **kwargs)

    monkeypatch.setattr(sessions, "append_effect", fail_before_commit)
    try:
        with pytest.raises(OSError):
            await runtime.execute_batch((call,), context=context, composition_revision="r")
        assert (context.workspace / "executions.txt").read_text() == "x"
        assert not any(
            e.type == "effect/outcome" for e in await sessions.read_effects(context.session_id)
        )
        monkeypatch.setattr(sessions, "append_effect", append)
        await RecoveryService(sessions).recover(context.session_id)
        (result,) = (
            e for e in await sessions.read_session(context.session_id) if e.type == "tool/result"
        )
        assert result.data["status"] == "unknown_after_crash"
        assert "output_ref" not in result.data
        assert (context.workspace / "executions.txt").read_text() == "x"
    finally:
        await store.aclose()


async def test_output_directory_keeps_its_observed_page_boundary_and_session_scope(tmp_path):
    runtime, sessions, store, context, call = await raw_batch(tmp_path)

    async def execute(call):
        (result,) = await runtime.execute_batch((call,), context=context, composition_revision="r")
        assert result.status == "succeeded"
        return result

    try:
        first = await execute(call)
        second = await execute(replace(call, id="second"))
        page1 = json.loads(
            (await execute(ToolCall("list1", "list_tool_outputs", {"count": 1}))).content
        )
        assert page1["outputs"][0]["output_ref"] == second.output_ref
        await execute(replace(call, id="third"))
        page2 = json.loads(
            (
                await execute(
                    ToolCall(
                        "list2",
                        "list_tool_outputs",
                        {
                            "offset": page1["next_offset"],
                            "through_seq": page1["through_seq"],
                            "count": 1,
                        },
                    )
                )
            ).content
        )
        assert page2["outputs"][0]["output_ref"] == first.output_ref
        assert page2["next_offset"] is None
        other = await sessions.create_session(context.workspace)
        (empty,) = await runtime.execute_batch(
            (ToolCall("list-other", "list_tool_outputs", {}),),
            context=replace(context, session_id=other),
            composition_revision="r",
        )
        assert json.loads(empty.content)["outputs"] == []
        (invalid,) = await runtime.execute_batch(
            (
                ToolCall(
                    "list-invalid",
                    "list_tool_outputs",
                    {
                        "offset": 1,
                    },
                ),
            ),
            context=context,
            composition_revision="r",
        )
        assert invalid.status == "failed"
        assert "tool-output-directory-source-required" in invalid.content
        assert (context.workspace / "executions.txt").read_text() == "xxx"
    finally:
        await store.aclose()
