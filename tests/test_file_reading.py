"""Actual workspace reads, bounded continuation, cancellation and frozen evidence."""

import asyncio
import hashlib
import json
import threading
from pathlib import Path

import pytest

from traceh.api.llm import ModelResponse, ToolCall
from traceh.api.tools import ToolExecutionContext
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.sqlite import SqliteEventStore
from traceh.tools.builtins.file_page import MAX_FILE_PAGE_CHARS, MAX_FILE_PAGE_LINES
from traceh.tools.builtins.paths import WorkspaceBoundaryError
from traceh.tools.builtins.read_file import ReadFileTool
from traceh.tools.builtins.search_text import SearchTextTool


def context(root):
    return ToolExecutionContext("session", "turn", "step", "call", root, root / "data")


async def read(root, **arguments):
    output = await ReadFileTool().execute(arguments, context(root))
    assert len(output.content) <= MAX_FILE_PAGE_CHARS
    page = json.loads(output.content)
    assert output.data == {key: value for key, value in page.items() if key != "text"}
    if page.get("mode") == "outline":
        # An outline carries locations, never bodies, so the line bound is on
        # what it points at rather than on text it does not return.
        assert "text" not in page
    else:
        assert len(page["text"].splitlines()) <= MAX_FILE_PAGE_LINES
    return page


async def test_outline_reports_where_definitions_are_without_their_bodies(tmp_path):
    """The point of the mode: locate everything, carry almost none of it."""

    body = chr(10).join(
        [
            "import os",
            "",
            "SECRET_CONSTANT = 1",
            "",
            "class Alpha:",
            "    def method(self):",
            "        return SECRET_CONSTANT",
            "",
            "    async def slow(self):",
            "        return None",
            "",
            "def beta(value):",
            "    return value",
            "",
        ]
    )
    (tmp_path / "sample.py").write_text(body, encoding="utf-8")

    page = await read(tmp_path, path="sample.py", mode="outline")

    assert page["mode"] == "outline" and page["eof"] is True
    assert page["total_lines"] == 13
    assert [(item["line"], item["text"]) for item in page["definitions"]] == [
        (5, "class Alpha:"),
        (6, "def method(self):"),
        (9, "async def slow(self):"),
        (12, "def beta(value):"),
    ]
    # Bodies never travel, so a non-definition line cannot appear anywhere.
    assert "SECRET_CONSTANT = 1" not in json.dumps(page)
    assert "next_read" not in page


async def test_outline_of_a_file_without_definitions_is_empty_and_final(tmp_path):
    (tmp_path / "data.txt").write_text(chr(10).join(["one", "two", "three", ""]), encoding="utf-8")

    page = await read(tmp_path, path="data.txt", mode="outline")

    assert page["definitions"] == [] and page["eof"] is True
    assert "next_read" not in page


async def test_outline_continues_at_the_next_line_when_it_does_not_fit(tmp_path):
    lines = []
    for index in range(600):
        lines.extend([f"def fn_{index}(argument_name_{index}):", "    pass"])
    (tmp_path / "many.py").write_text(chr(10).join(lines + [""]), encoding="utf-8")

    first = await read(tmp_path, path="many.py", mode="outline")

    assert first["eof"] is False
    following = first["next_read"]
    assert following["mode"] == "outline"
    assert following["start_line"] == first["definitions"][-1]["line"] + 1
    assert following["source_sha256"] == first["source_sha256"]

    second = await read(tmp_path, **following)
    assert second["definitions"][0]["line"] >= following["start_line"]
    assert 0 < len(first["definitions"]) < 600


@pytest.mark.parametrize("extra", [{"end_line": 5}, {"start_column": 2}])
async def test_outline_refuses_body_oriented_arguments(tmp_path, extra):
    (tmp_path / "sample.py").write_text(chr(10).join(["def a():", "    pass", ""]), "utf-8")

    with pytest.raises(ValueError, match="file-read-outline-range-invalid"):
        await ReadFileTool().execute(
            {"path": "sample.py", "mode": "outline", **extra}, context(tmp_path)
        )


async def test_outline_rejects_a_stale_source_digest(tmp_path):
    path = tmp_path / "sample.py"
    path.write_text(chr(10).join(["def a():", "    pass", ""]), "utf-8")
    stale = (await read(tmp_path, path="sample.py", mode="outline"))["source_sha256"]
    path.write_text(chr(10).join(["def b():", "    pass", ""]), "utf-8")

    with pytest.raises(ValueError, match="file-read-source-changed"):
        await ReadFileTool().execute(
            {"path": "sample.py", "mode": "outline", "source_sha256": stale}, context(tmp_path)
        )


async def test_search_hit_reads_nearby_actual_lines_and_continues(tmp_path):
    raw = "before\r\n\r\nneedle 测量🙂\r\nafter\u2028last".encode()
    (tmp_path / "notes.txt").write_bytes(raw)
    result = await SearchTextTool().execute({"query": "needle"}, context(tmp_path))
    hit = result.data["matches"][0]
    page = await read(
        tmp_path, path=hit["path"], start_line=hit["line"] - 1, end_line=hit["line"] + 1
    )
    assert page["text"] == "2: \n3: needle 测量🙂\n4: after"
    assert page["total_lines"] == 5
    assert page["source_sha256"] == hashlib.sha256(raw).hexdigest()
    assert not page["truncated"] and not page["eof"]
    assert "end_line" not in page["next_read"]
    last = await read(tmp_path, **page["next_read"])
    assert last["text"] == "5: last"
    assert last["eof"] and last["next_read"] is None


@pytest.mark.parametrize("body", ["", "\n", "a\n", "a\n\n"])
async def test_empty_or_eof_has_no_phantom_trailing_line(tmp_path, body):
    (tmp_path / "note").write_bytes(body.encode())
    page = await read(tmp_path, path="note")
    assert page["text"] == "\n".join(f"{i}: {v}" for i, v in enumerate(body.splitlines(), 1))
    assert page["eof"] and not page["truncated"] and page["next_read"] is None


async def test_default_read_is_bounded_and_numbered_without_skipped_lines(tmp_path):
    lines = [f"source {i}" for i in range(350)]
    (tmp_path / "module.py").write_text("\n".join(lines), encoding="utf-8")
    arguments, seen, calls = {"path": "module.py"}, [], 0
    while arguments:
        page = await read(tmp_path, **arguments)
        seen.extend(page["text"].splitlines())
        arguments = page["next_read"]
        calls += 1
        assert calls < 10
    assert calls == 3
    assert seen == [f"{i}: {v}" for i, v in enumerate(lines, 1)]


async def test_long_escaped_unicode_line_resumes_exactly_then_finishes_requested_range(tmp_path):
    body = '汉🙂\\"\t' * 6500
    (tmp_path / "payload.txt").write_text("prefix\n" + body + "\nend", encoding="utf-8")
    arguments = {"path": "payload.txt", "start_line": 2, "end_line": 2}
    rebuilt, cursors = "", set()
    while arguments.get("start_line") == 2:
        cursor = (arguments["start_line"], arguments.get("start_column", 1))
        assert cursor not in cursors
        cursors.add(cursor)
        page = await read(tmp_path, **arguments)
        assert page["text"].startswith("2: ")
        rebuilt += page["text"][3:]
        assert page["end_column"] == len(rebuilt)
        arguments = page["next_read"]
        if page["truncated"]:
            assert arguments["end_line"] == 2
            assert arguments["start_column"] == len(rebuilt) + 1
    assert len(cursors) > 2
    assert rebuilt == body
    assert arguments["start_line"] == 3 and "end_line" not in arguments
    assert (await read(tmp_path, **arguments))["text"] == "3: end"


async def test_changed_source_rejects_old_continuation_even_at_same_length(tmp_path):
    path = tmp_path / "note"
    path.write_bytes(b"one\ntwo")
    page = await read(tmp_path, path="note", end_line=1)
    path.write_bytes(b"ONE\nTWO")
    with pytest.raises(ValueError, match="file-read-source-changed"):
        await read(tmp_path, **page["next_read"])
    assert (await read(tmp_path, path="note", start_line=2))["text"] == "2: TWO"


@pytest.mark.parametrize(
    "options,code",
    [
        ({"start_line": True}, "range-invalid"),
        ({"end_line": None}, "range-invalid"),
        ({"start_column": 0}, "range-invalid"),
        ({"start_line": "1"}, "range-invalid"),
        ({"start_line": 2, "end_line": 1}, "range-invalid"),
        ({"start_line": 3}, "range-outside-source"),
        ({"start_column": 4}, "range-outside-source"),
        ({"source_sha256": "not-a-digest"}, "source-digest-invalid"),
        ({"source_sha256": None}, "source-digest-invalid"),
    ],
)
async def test_invalid_range_and_digest_fail_explicitly(tmp_path, options, code):
    (tmp_path / "note").write_bytes(b"abc\ndef")
    with pytest.raises(ValueError, match=code):
        await read(tmp_path, path="note", **options)


async def test_digest_cannot_authorize_outside_workspace_and_failed_reads_are_explicit(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    raw = b"private"
    (tmp_path / "outside").write_bytes(raw)
    with pytest.raises(WorkspaceBoundaryError):
        await read(workspace, path="../outside", source_sha256=hashlib.sha256(raw).hexdigest())
    with pytest.raises(IsADirectoryError):
        await read(workspace, path=".")
    (workspace / "binary").write_bytes(b"\xff\x80")
    with pytest.raises(UnicodeDecodeError):
        await read(workspace, path="binary")


@pytest.mark.parametrize("worker_fails", [False, True])
async def test_repeated_cancel_waits_for_actual_read_to_converge(
    tmp_path, monkeypatch, worker_fails
):
    path = tmp_path / "source"
    path.write_bytes(b"actual source")
    loop = asyncio.get_running_loop()
    entered, converging = asyncio.Event(), asyncio.Event()
    release, closed = threading.Event(), threading.Event()
    original = Path.read_bytes

    def gated_read(self):
        actual = original(self)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10), "test did not release actual reader"
        closed.set()
        if worker_fails:
            raise OSError("read-finished-with-failure")
        return actual

    from traceh.tools.builtins import read_file

    original_converge = read_file.await_worker_convergence

    async def observed_converge(worker):
        converging.set()
        await original_converge(worker)

    monkeypatch.setattr(Path, "read_bytes", gated_read)
    monkeypatch.setattr(read_file, "await_worker_convergence", observed_converge)
    running = asyncio.create_task(ReadFileTool().execute({"path": "source"}, context(tmp_path)))
    draining = asyncio.create_task(converging.wait())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        running.cancel()
        finished, _ = await asyncio.wait(
            (running, draining), timeout=5, return_when=asyncio.FIRST_COMPLETED
        )
        assert finished, "cancel did not reach either completion or owned cleanup"
        assert not running.done() and not closed.is_set()
        running.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await running
        assert closed.is_set()
        assert isinstance(raised.value.__cause__, OSError) == worker_fails
    finally:
        release.set()
        draining.cancel()
        await asyncio.gather(running, draining, return_exceptions=True)


class ContinuingReader:
    name = "scripted"

    def __init__(self):
        self.calls = 0
        self.continuation = None

    async def complete(self, request):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(tool_calls=(ToolCall("first", "read_file", {"path": "source"}),))
        tools = [m for m in request.messages if m.role == "tool"]
        if self.calls == 2:
            page = json.loads(tools[-1].content)
            self.continuation = page["next_read"]
            assert "source evidence" not in page["text"]
            return ModelResponse(
                tool_calls=(ToolCall("bad", "read_file", {"path": "source", "start_line": 999}),)
            )
        if self.calls == 3:
            assert "range-outside-source" in tools[-1].content
            return ModelResponse(tool_calls=(ToolCall("next", "read_file", self.continuation),))
        page = json.loads(tools[-1].content)
        assert self.calls < 20
        if page["next_read"] is not None:
            return ModelResponse(
                tool_calls=(ToolCall(f"next-{self.calls}", "read_file", page["next_read"]),)
            )
        assert page["text"].endswith("121: source evidence")
        assert page["eof"]
        return ModelResponse(content="Evidence arrived through the next read.")


@pytest.mark.parametrize("line", ["line", '\\"\t测🙂' * 25])
async def test_runtime_dispatches_real_continuation_and_failure_then_reopens_evidence(
    tmp_path, line
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "source").write_text((line + "\n") * 120 + "source evidence", encoding="utf-8")
    root = tmp_path / "data"
    config = RuntimeConfig(data_dir=root)
    store = SqliteEventStore(root / "events")
    provider = ContinuingReader()
    runtime = build_default_runtime(config, provider=provider, event_store=store)
    try:
        sid = await runtime.create_session(workspace)
        result = await runtime.run_existing(sid, "Read the source.")
        assert result.reason == "completed" and provider.calls >= 4
        events = await runtime.sessions.read_session(sid)
        results = [e for e in events if e.type == "tool/result"]
        assert [e.data["status"] for e in results[:2]] == ["succeeded", "failed"]
        assert all(e.data["status"] == "succeeded" for e in results[2:])
        # These results are small, so they are still disclosed in full rather
        # than replaced by a retention notice. They are nonetheless addressable:
        # that is what lets a long Turn fold them later instead of carrying
        # every one of them forever.
        assert all(e.data["output_ref"]["disclosure"] == "inline" for e in results)
        assert all("notice" not in e.data["content"] for e in results)
        assert not await runtime.check_invariants(sid)
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, sid)
    finally:
        await runtime.dispose()
        await store.aclose()
    store = SqliteEventStore(root / "events")
    runtime = build_default_runtime(config, provider=ContinuingReader(), event_store=store)
    try:
        assert await runtime.sessions.read_session(sid) == events
        assert not await runtime.check_invariants(sid)
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, sid)
    finally:
        await runtime.dispose()
        await store.aclose()
