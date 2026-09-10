"""Shared runner lifecycle using the original Product execution owner."""

import asyncio
import json
import sqlite3
import zipfile
from dataclasses import replace

import pytest
from test_product_benchmark_e2e import _ProductProvider, _runner

from traceh.api.json_types import fingerprint
from traceh.evaluation.inputs import digest_bytes
from traceh.evaluation.plan import RunOptions
from traceh.session.sqlite import SqliteEventStore


class ClosingProvider(_ProductProvider):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.closing = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request):
        if "apply_patch" in {tool.name for tool in request.tools}:
            self.entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.closing.set()
                await self.release.wait()
        return await super().complete(request)


async def test_repeated_cancel_waits_for_real_product_and_preserves_planned_denominator(tmp_path):
    provider = ClosingProvider()
    runner = _runner(tmp_path, arms=(("single", 2),), provider=provider)
    running = asyncio.create_task(runner.run())
    try:
        await asyncio.wait_for(provider.entered.wait(), 60)
        running.cancel()
        await asyncio.wait_for(provider.closing.wait(), 30)
        running.cancel()
        progressed = asyncio.Event()
        asyncio.get_running_loop().call_soon(progressed.set)
        await progressed.wait()
        assert not running.done()
    finally:
        provider.release.set()
        if not running.done():
            running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, 60)
    report = json.loads((runner.output / "report.json").read_text())
    assert report["complete"] is False
    assert [t["execution"]["status"] for t in report["trials"]] == ["cancelled", "not_started"]
    assert report["trials"][0]["convergence"] == "converged"
    assert not (runner.output / "attempts/002").exists()
    refs = report["trials"][0]["evidence"]
    assert any(ref["stream_id"].startswith("product-task:") for ref in refs)
    for ref in refs:
        with sqlite3.connect(runner.output / ref["file"]) as db:
            rows = db.execute(
                "SELECT seq, envelope_json FROM events WHERE stream_id=? ORDER BY seq",
                (ref["stream_id"],),
            ).fetchall()
        assert ref["sha256"] == fingerprint([row[1] for row in rows])
        assert (ref["first_seq"], ref["last_seq"]) == (rows[0][0], rows[-1][0])
        if ref["stream_id"].startswith("product-task:"):
            assert any(json.loads(row[1])["type"] == "product/task-cancelled" for row in rows)
    frozen = json.loads((runner.output / "frozen.json").read_text())
    for item in frozen["artifacts"]:
        assert digest_bytes((runner.output / item["file"]).read_bytes()) == item["sha256"]
    with zipfile.ZipFile(runner.output / "artifacts/materials.zip") as archive:
        assert archive.read("benchmark.json") == runner.manifest.document.content
        assert archive.read("dataset.json") == runner.manifest.dataset.content
        material = "write_expected_file/initial/kept.txt"
        assert archive.read(material) == (runner.manifest.directory / material).read_bytes()


async def test_real_attempt_store_close_failure_stops_later_trials_and_is_not_hidden(
    tmp_path, monkeypatch
):
    original = SqliteEventStore.aclose
    closed = []
    failure = RuntimeError("injected-store-close-failure")

    async def fail_after_close(store):
        await original(store)
        closed.append(store)
        raise failure

    monkeypatch.setattr(SqliteEventStore, "aclose", fail_after_close)
    runner = _runner(tmp_path, arms=(("single", 2),))
    with pytest.raises((RuntimeError, BaseExceptionGroup)) as caught:
        await runner.run()
    assert closed
    assert caught.value is failure or failure in caught.value.exceptions
    report = json.loads((runner.output / "report.json").read_text())
    assert report["complete"] is False
    assert report["trials"][0]["convergence"] == "unknown"
    assert report["trials"][1]["execution"]["status"] == "not_started"
    assert report["trials"][0]["evidence"]


async def test_deadline_before_dispatch_is_explicit_and_preserves_every_trial(tmp_path):
    runner = _runner(tmp_path, arms=(("single", 2),))
    runner.options = replace(RunOptions(repetitions=2), timeout_seconds=1e-9)
    report = await runner.run()
    assert not report.complete
    assert report.errors == ("TimeoutError",)
    assert [t.execution.value for t in report.trials] == ["not_started", "not_started"]
    assert report.trials[0].convergence.value == "converged"
    assert not (runner.output / "attempts").exists()


async def test_report_write_failure_keeps_the_original_deadline_failure(tmp_path, monkeypatch):
    import traceh.evaluation.runner as module

    runner = _runner(tmp_path, arms=(("single", 1),))
    runner.options = RunOptions(timeout_seconds=1e-9)
    original = module._write_json
    failure = OSError("injected-report-write-failure")

    def fail_report(path, value):
        if path.name == "report.json":
            raise failure
        original(path, value)

    monkeypatch.setattr(module, "_write_json", fail_report)
    with pytest.raises(BaseExceptionGroup) as caught:
        await runner.run()
    assert isinstance(caught.value.exceptions[0], TimeoutError)
    assert caught.value.exceptions[1] is failure
    assert (runner.output / "frozen.json").is_file()
    assert (runner.output / "evidence-manifest.json").is_file()
    assert not (runner.output / "attempts").exists()
