"""Detections come from original evidence: real evaluation runs and real Session events."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from test_context_diagnostics import run as run_session
from test_context_diagnostics import tight_policy
from test_evaluation_comparison import pair_plan
from test_retrieval_episode_evaluator import BENCHMARK

from traceh.api.json_types import fingerprint
from traceh.cli.main import _configure_from_environment, _eval, build_parser
from traceh.evaluation.inputs import digest_bytes
from traceh.evolution.background import (
    BackgroundOptimizationHost,
    BackgroundPeriod,
    EpisodeReservation,
)
from traceh.evolution.detection import evaluation_findings
from traceh.evolution.product_feedback import stream_findings
from traceh.session.event_store import InMemoryEventStore
from traceh.session.service import SessionService


async def truncated_evaluation(root, monkeypatch):
    """One real evaluation pair whose model stopped at the output limit."""
    monkeypatch.setenv("UNUSED_EVALUATION_KEY", "local-fixture-not-a-real-key")
    plan = pair_plan(root, case_id="m-direct")
    (root / "script.json").write_text(
        json.dumps(
            [
                {
                    "content": "partial answer cut",
                    "completion": "length",
                    "usage": {"input_tokens": 20, "output_tokens": 10},
                }
            ]
        ),
        encoding="utf-8",
    )
    output = root / "experiment"
    args = build_parser().parse_args(
        [
            "eval",
            str(BENCHMARK),
            "--run-plan",
            str(plan),
            "--output",
            str(output),
            "--env-file",
            str(root / "empty.env"),
        ]
    )
    _configure_from_environment(args)
    await _eval(args)
    return output / "arms/01/run"


@pytest.mark.frozen_unicode
async def test_evaluation_findings_cite_the_original_trial_streams(tmp_path, monkeypatch):
    run = await truncated_evaluation(tmp_path, monkeypatch)
    findings = evaluation_findings(run)
    report = json.loads((run / "report.json").read_text(encoding="utf-8"))
    trial = report["trials"][0]
    truncated = [f for f in findings if f.observation.failure_class == "truncated-response"]
    assert truncated, findings
    finding = truncated[0]
    assert finding.source.endswith("/" + trial["identity"]["trial_id"])
    assert finding.observation.case_id == "m-direct"
    streams = {ref["stream_id"] for ref in trial["evidence"]}
    assert all(loc.rsplit("@", 1)[0] in streams for loc in finding.observation.evidence_locations)
    # No model prose travels into the observation, only counts and locations.
    assert "partial answer cut" not in repr(findings)


@pytest.mark.frozen_unicode
async def test_a_run_whose_evidence_was_altered_is_refused(tmp_path, monkeypatch):
    run = await truncated_evaluation(tmp_path, monkeypatch)
    report = json.loads((run / "report.json").read_text(encoding="utf-8"))
    report["run_id"] = "another-run"
    (run / "report.json").write_text(json.dumps(report), encoding="utf-8")
    from traceh.evaluation.errors import BenchmarkManifestError

    with pytest.raises(BenchmarkManifestError) as refused:
        evaluation_findings(run)
    assert refused.value.code == "evaluation-evidence-mismatch"


@pytest.mark.frozen_unicode
async def test_the_host_only_accepts_runs_of_its_own_benchmark(tmp_path, monkeypatch):
    run = await truncated_evaluation(tmp_path, monkeypatch)
    sessions = SessionService(InMemoryEventStore())
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()

    async def execute(*args):
        pytest.fail("one source is not a cluster")

    def host(benchmark_digest, case_ids):
        return BackgroundOptimizationHost(
            sessions,
            BackgroundPeriod(
                "detection-test-period",
                str(workspace),
                fingerprint("source"),
                fingerprint("plan"),
                datetime.now(UTC) + timedelta(hours=1),
                1,
                1000,
                10,
                1,
            ),
            reservation=EpisodeReservation(1000),
            execute=execute,
            scope={"benchmark_digest": benchmark_digest, "case_ids": frozenset(case_ids)},
        )

    own = digest_bytes((BENCHMARK / "benchmark.json").read_bytes())
    other = host(fingerprint("another benchmark"), {"m-direct"})
    await other.open()
    await other.set_enabled(True)
    with pytest.raises(ValueError, match="outside-scope"):
        await other.observe_evaluation(run)
    matching = host(own, {"m-direct"})
    await matching.open()
    assert await matching.observe_evaluation(run)
    state = await matching.view()
    assert {item["source"] for item in state["observations"].values()} == {
        f.source for f in evaluation_findings(run)
    }
    assert not await matching.observe_evaluation(run)  # Same evidence is not counted twice.
    await matching.aclose()
    await other.aclose()


async def test_product_stream_findings_cut_at_the_last_turn_end(tmp_path):
    session_id, events = await run_session(tmp_path, reads=6, token_budget=tight_policy())
    stream = f"session:{session_id}"
    findings = stream_findings("product:task-1", stream, events)
    classes = {f.observation.failure_class for f in findings}
    assert "rerun-after-fold" in classes
    assert all(f.source == "product:task-1" for f in findings)
    end = max(e.seq for e in events if e.type == "turn/end")
    assert all(f.observation.cut_seq == end for f in findings)
    # A stream with no finished Turn has nothing to report yet.
    assert (
        stream_findings("product:task-1", stream, tuple(e for e in events if e.type != "turn/end"))
        == ()
    )


def _result(seq, content, *, status="failed", tool="shell"):
    from uuid import uuid4

    from traceh.api.events import EventEnvelope

    return EventEnvelope(
        event_id=uuid4(),
        stream_id="session:s",
        seq=seq,
        type="tool/result",
        schema_version=1,
        data={"tool_call_id": f"c{seq}", "tool_name": tool, "status": status, "content": content},
        occurred_at=datetime.now(UTC),
    )


START_FAILED = (
    "RuntimeError: exit_code=None timed_out=false sandbox_status=start-failed\n"
    "--- stdout ---\n\n--- stderr ---\n"
    "FileNotFoundError: [Errno 2] No such file or directory: '{exe}'\n"
    "sandbox_receipt={receipt} publication=None"
)


def test_a_tool_failure_names_the_cause_the_host_recorded():
    """The analysis must see why a tool failed, not only that it did.

    Given only "3 tool results failed", a real analysis proposed "diagnose and
    retry", while every one of those results said the sandbox could not find an
    executable named ``PYTHONPATH=src`` or ``cd`` - shell syntax written for a
    tool that runs argv. Receipts differ per call, so they are masked and the
    same cause is counted once with its number.
    """

    from traceh.evolution.detection import detect

    events = (
        _result(1, START_FAILED.format(exe="PYTHONPATH=src", receipt="a" * 64)),
        _result(2, START_FAILED.format(exe="PYTHONPATH=src", receipt="b" * 64)),
        _result(3, START_FAILED.format(exe="cd", receipt="c" * 64)),
        _result(4, "ok", status="succeeded"),
    )
    (failed,) = detect("s", events, stream_id="session:s", quote_causes=True)
    assert failed.failure_class == "tool-failed" and failed.count == 3
    top, other = failed.causes
    assert top[1] == 2 and "'PYTHONPATH=src'" in top[0] and top[0].startswith("shell: ")
    assert other[1] == 1 and "'cd'" in other[0]
    assert "sandbox_status=start-failed" in failed.summary
    assert "PYTHONPATH=src" in failed.summary
    # Receipts never travel, so two runs of one cause read the same.
    assert "a" * 32 not in failed.summary and "sandbox_receipt" not in failed.summary


def test_listed_causes_are_bounded_and_single_line():
    from traceh.evolution.detection import MAX_CAUSE_CHARS, MAX_CAUSES, detect

    events = tuple(
        _result(seq, f"Error {seq}\x07\n" + "x" * 5000 + " error tail") for seq in range(1, 9)
    )
    (failed,) = detect("s", events, stream_id="session:s", quote_causes=True)
    assert failed.count == 8
    assert len(failed.causes) == MAX_CAUSES
    for cause, _ in failed.causes:
        assert len(cause) <= MAX_CAUSE_CHARS
        assert cause.isprintable()


def test_other_classes_carry_no_quoted_text():
    from traceh.evolution.detection import CAUSED_CLASSES, Detection

    assert CAUSED_CLASSES == {"tool-failed", "tool-denied"}
    plain = Detection("truncated-response", 2, ("session:s@1",))
    assert "Host-recorded" not in plain.summary


def test_one_cause_quoting_different_identifiers_is_counted_once():
    """An error line that quotes a per-call digest is still one cause."""

    from traceh.evolution.detection import detect

    events = tuple(
        _result(seq, f"RuntimeError: failed\nValueError: artifact {digit * 64} is missing")
        for seq, digit in ((1, "a"), (2, "b"))
    )
    (failed,) = detect("s", events, stream_id="session:s", quote_causes=True)
    assert failed.causes == (
        ("shell: RuntimeError: failed | ValueError: artifact <id> is missing", 2),
    )


def test_chat_and_product_findings_stay_counts_only():
    """Only benchmark evidence quotes a cause; a person's own Session never does."""

    from traceh.evolution.detection import detect

    events = (_result(1, START_FAILED.format(exe="SECRET-PATH", receipt="a" * 64)),)
    (failed,) = detect("s", events, stream_id="session:s")
    assert failed.causes == ()
    assert "SECRET-PATH" not in failed.summary
