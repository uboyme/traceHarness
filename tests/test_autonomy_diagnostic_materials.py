"""Diagnostic preflight: actual material, bounded runner and honest evidence selection."""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from live_dynamic_collaboration.diagnosis import positive_control_started
from live_dynamic_collaboration.diagnosis_audit import events_at, main_requests
from live_dynamic_collaboration.diagnosis_candidate import candidate_patch
from live_dynamic_collaboration.diagnosis_evidence import visible_tool_results
from live_dynamic_collaboration.diagnosis_materials import COMPLEX, EXPLICIT, build

from traceh.evaluation.errors import BenchmarkManifestError
from traceh.evaluation.manifest import load_benchmark_manifest
from traceh.evaluation.plan import RunOptions
from traceh.evaluation.runner import EvaluationRunner
from traceh.evaluation.variants import apply_candidate, source_files


def test_actual_source_conditions_share_material_but_not_delegation_instruction(tmp_path):
    root = build(Path(__file__).parents[1], tmp_path / "material")
    manifest = load_benchmark_manifest(root)
    cases = manifest.dataset.data["cases"]
    assert cases[0]["sha256"] == cases[1]["sha256"] != cases[2]["sha256"]
    assert cases[0]["requirement"] == COMPLEX + EXPLICIT
    assert cases[1]["requirement"] == COMPLEX
    assert all(word not in COMPLEX for word in ("委派", "助手", "并行", "delegate"))
    with pytest.raises(BenchmarkManifestError):
        EvaluationRunner(
            root,
            tmp_path / "run",
            provider=SimpleNamespace(name="fixture"),
            model_id="fixture",
            options=RunOptions(repetitions=2, max_trials=6),
        )


@pytest.mark.parametrize("case_id", ["explicit", "natural", "simple"])
def test_verifier_reaches_assertions_and_accepts_only_unchanged_source_and_valid_output(
    tmp_path, case_id
):
    root = build(Path(__file__).parents[1], tmp_path / "material")
    cases = json.loads((root / "dataset.json").read_text(encoding="utf-8"))["cases"]
    case = next(c for c in cases if c["case_id"] == case_id)
    directory = root / case["initial_tree"]
    argv = [sys.executable, *case["verification"]["commands"][0]["argv"][1:]]
    (directory / "answer.json").write_text("{}")
    wrong = subprocess.run(argv, cwd=directory, capture_output=True)
    assert wrong.returncode == 1 and b"AssertionError" in wrong.stderr
    if case_id == "simple":
        namespace = {}
        exec((directory / "src/traceh/version.py").read_bytes(), namespace)
        answer = {"version": namespace["__version__"]}
    else:
        files = list(directory.rglob("*.py"))
        answer = {
            "sections": [
                {
                    "topic": topic,
                    "conclusion": "Explicit structural fixture, not a semantic success judgment.",
                    "evidence": [
                        {
                            "path": p.relative_to(directory).as_posix(),
                            "start_line": 1,
                            "end_line": 2,
                        }
                        for p in files[:2]
                    ],
                }
                for topic in ("authority", "budget", "lifecycle")
            ]
        }
    (directory / "answer.json").write_text(json.dumps(answer), encoding="utf-8")
    passed = subprocess.run(argv, cwd=directory, capture_output=True)
    assert passed.returncode == 0, passed.stderr
    source = next(directory.rglob("*.py"))
    source.write_bytes(source.read_bytes() + b"\n# changed\n")
    drift = subprocess.run(argv, cwd=directory, capture_output=True)
    assert drift.returncode == 1 and b"AssertionError" in drift.stderr


def test_requester_and_other_sessions_cannot_count_as_main_tool_exposure():
    events = [
        {
            "type": "request/snapshot",
            "stream_id": "session:" + sid,
            "data": {"dispatch_request": {"tools": tools}},
        }
        for sid, tools in [
            ("requester", []),
            ("main", [{"name": "delegate_investigation"}]),
            ("foreign", []),
        ]
    ]
    assert main_requests(events, ["main"]) == [events[1]]
    assert main_requests(events, ["absent"]) == []


def test_closed_evidence_is_read_without_mutation_and_live_wal_is_refused(tmp_path):
    file = tmp_path / "events.sqlite3"
    connection = sqlite3.connect(file)
    connection.execute("CREATE TABLE events(stream_id TEXT,seq INTEGER,envelope_json TEXT)")
    event = {"type": "request/snapshot", "stream_id": "session:fixture", "seq": 1, "data": {}}
    connection.execute(
        "INSERT INTO events VALUES(?,?,?)", ("session:fixture", 1, json.dumps(event))
    )
    connection.commit()
    connection.close()
    before = file.read_bytes()
    assert events_at(file) == [event] and file.read_bytes() == before
    file.with_name(file.name + "-wal").write_bytes(b"active fixture")
    with pytest.raises(ValueError, match="closed-checkpointed-evidence-required"):
        events_at(file)


def test_candidate_changes_only_declared_text_and_cannot_change_runtime_or_stale_base():
    _, files = source_files()
    patch = candidate_patch(files)
    with pytest.raises(BenchmarkManifestError) as rejected:
        apply_candidate(files, patch)
    assert rejected.value.code == "evaluation-candidate-scope-invalid"
    patch["edits"][0]["selector"] = "_InvestigationControl.delegate"
    with pytest.raises(BenchmarkManifestError) as rejected:
        apply_candidate(files, patch)
    assert rejected.value.code == "evaluation-candidate-scope-invalid"
    patch = candidate_patch(files)
    patch["base_source_digest"] = "0" * 64
    with pytest.raises(BenchmarkManifestError) as stale:
        apply_candidate(files, patch)
    assert stale.value.code == "evaluation-frozen-input-drift"


def test_failed_child_can_have_visible_reads_but_failed_or_foreign_requests_do_not_count():
    def event(seq, kind, data):
        return {"stream_id": "session:child", "seq": seq, "type": kind, "data": data}

    message = {"role": "tool", "name": "read_file", "tool_call_id": "read", "content": "body"}
    events = [
        event(
            1,
            "tool/result",
            {
                "status": "succeeded",
                "tool_name": "read_file",
                "tool_call_id": "read",
                "content": "body",
                "data": {"path": "source.py"},
            },
        ),
        event(
            2,
            "request/snapshot",
            {
                "dispatch_fingerprint": "frozen",
                "dispatch_request": {"messages": [message]},
            },
        ),
        event(
            3,
            "model/attempt-end",
            {
                "status": "succeeded",
                "request_snapshot_seq": 2,
                "dispatch_fingerprint": "frozen",
            },
        ),
        event(4, "runtime/error", {"error_type": "BudgetExhaustedError"}),
    ]
    assert len(visible_tool_results(events, "child")) == 1
    assert visible_tool_results(events, "parent") == []
    events[2]["data"]["status"] = "failed"
    assert visible_tool_results(events, "child") == []
    events[2]["data"]["status"] = "succeeded"
    events[1]["data"]["dispatch_fingerprint"] = "changed"
    with pytest.raises(ValueError, match="request-receipt-mismatch"):
        visible_tool_results(events, "child")


def test_positive_control_requires_an_accepted_child_not_just_a_tool_attempt():
    rejected = {"delegate_calls": 1, "handoffs": []}
    assert not positive_control_started({"rows": [rejected, rejected]})
    accepted = {"delegate_calls": 1, "handoffs": [{"agent_id": "owned-child"}]}
    assert positive_control_started({"rows": [rejected, accepted]})
