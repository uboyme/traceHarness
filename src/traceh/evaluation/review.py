"""Offline, append-only retrieval assessment. Never construct a Provider or Runtime."""

import json
import sqlite3
import zipfile
from collections import Counter
from pathlib import Path

from traceh.api.events import EventEnvelope
from traceh.api.json_types import fingerprint
from traceh.evaluation.errors import BenchmarkManifestError
from traceh.evaluation.evaluators.episode_diagnostics import (
    diagnostic_row,
    diagnostics_markdown,
    observe_episode,
)
from traceh.evaluation.inputs import (
    decode_document,
    digest_bytes,
    object_fields,
    read_input,
    text_field,
)
from traceh.evaluation.variants import source_digest, source_files


def stale():
    raise BenchmarkManifestError("evaluation-judgment-stale", "review")


def _write(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def _run(root):
    from traceh.evaluation.evidence import load_run

    try:
        frozen, report, base_binding = load_run(root)
    except BenchmarkManifestError:
        stale()
    if report["task_type"] not in {"retrieval_episode", "product_task"}:
        stale()
    assessment = frozen["settings"]["assessment"]
    if assessment["rubric"] is None or not assessment["requires_review"]:
        stale()
    with zipfile.ZipFile(root / "artifacts/materials.zip") as archive:
        rubric_bytes = archive.read(assessment["rubric"]["file"])
        if digest_bytes(rubric_bytes) != assessment["rubric"]["sha256"]:
            stale()
        rubric = decode_document(rubric_bytes)
    binding = {
        **base_binding,
        "scorer_id": assessment["scorer_id"],
        "scorer_version": assessment["version"],
        "rubric_digest": assessment["rubric"]["sha256"],
    }
    return report, binding, rubric


def _destination(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    if output.is_relative_to(root) or root.is_relative_to(output):
        raise BenchmarkManifestError("evaluation-output-overlap", "review-output")
    if output.exists():
        raise BenchmarkManifestError("evaluation-output-exists", "review-output")
    return root, output


def _review_packets(root, report, rubric):
    if report["task_type"] == "product_task":
        from traceh.evaluation.evaluators.product_review import packets

        return packets(root, report, rubric)
    return {p["trial_id"]: p for p in report["task_report"]["episodes"]}


def _episode_events(root, trial, packet):
    if packet.get("task_type") == "product_task":
        from traceh.evaluation.evaluators.product_review import trial_events

        return trial_events(root, trial)
    stream = "session:" + packet["session_id"]
    ref = next((r for r in trial["evidence"] if r["stream_id"] == stream), None)
    if ref is None:
        stale()
    connection = sqlite3.connect((root / ref["file"]).as_uri() + "?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT envelope_json FROM events WHERE stream_id=? ORDER BY seq", (stream,)
        ).fetchall()
    finally:
        connection.close()
    if fingerprint([row[0] for row in rows]) != ref["sha256"]:
        stale()
    return ref, [json.loads(row[0]) for row in rows]


def _diagnostics(report, binding, observations):
    if report["task_type"] == "product_task":
        from traceh.evaluation.evaluators.product_review import diagnostics

        return {
            **diagnostics(report, observations),
            "binding": binding,
            "analyzer": {"version": 1, "source_digest": source_digest(source_files()[1])},
        }
    packets = {p["trial_id"]: p for p in report["task_report"]["episodes"]}
    return {
        "format": 1,
        "binding": binding,
        "analyzer": {"version": 1, "source_digest": source_digest(source_files()[1])},
        "rows": [
            diagnostic_row(
                packets.get(t["identity"]["trial_id"]),
                observations.get(t["identity"]["trial_id"]),
                t,
            )
            for t in report["trials"]
        ],
    }


def _write_diagnostics(output, report):
    _write(output / "diagnostics.json", report)
    (output / "diagnostics.md").write_text(
        (
            "# Product task evidence\n\nHard verification and semantic review remain separate.\n"
            if report.get("task_type") == "product_task"
            else diagnostics_markdown(report["rows"])
        )
        + "\n```json\n"
        + json.dumps(report, ensure_ascii=False, indent=2)
        + "\n```\n",
        encoding="utf-8",
    )


def export_review(root, output):
    root, output = _destination(root, output)
    report, binding, rubric = _run(root)
    packets = _review_packets(root, report, rubric)
    exports = []
    observations = {}
    for index, trial in enumerate(report["trials"], 1):
        packet = packets.get(trial["identity"]["trial_id"])
        if packet is None:
            continue
        ref, events = _episode_events(root, trial, packet)
        product = report["task_type"] == "product_task"
        observations[trial["identity"]["trial_id"]] = (
            packet
            if product
            else observe_episode(packet, tuple(EventEnvelope.from_dict(e) for e in events))
        )
        relative = f"evidence/{index:03d}.json"
        target = (
            events
            if product
            else [event for event in events if event["seq"] > packet["target_start_seq"]]
        )
        exports.append((relative, {"reference": ref, "events": target}))
        packet["target_events"] = relative
    diagnostics = _diagnostics(report, binding, observations)
    output.mkdir(parents=True)
    (output / "evidence").mkdir()
    _write_diagnostics(output, diagnostics)
    for relative, value in exports:
        _write(output / relative, value)
    _write(
        output / "review.json",
        {
            "format": 1,
            "binding": binding,
            "execution_run": str(root),
            "rubric": rubric,
            "trials": report["trials"],
            "task_type": report["task_type"],
            **{
                ("tasks" if report["task_type"] == "product_task" else "episodes"): list(
                    packets.values()
                )
            },
        },
    )
    _write(
        output / "judgment-template.json",
        {
            "format": 2,
            "binding": binding,
            "reviewer": "",
            "origin": {"kind": "human"},
            "supersedes": None,
            "judgments": [],
        },
    )
    (output / "README.md").write_text(
        "# Evaluation review\n\nRead review.json and the original run evidence. "
        "Fill reviewer and judgments: trial_id, status (passed/failed/pending_review), reason. "
        "Missing judgments remain pending. Submit with traceh eval --assess. "
        "See diagnostics.md for source/evidence/answer observations and per-query coverage. "
        "This export did not call a model or any tool.\n",
        encoding="utf-8",
    )
    return {"run_id": binding["run_id"], "review": str(output), "entries": len(report["trials"])}


def reviewed_report(root, judgment_file):
    root = Path(root).resolve()
    report, binding, rubric = _run(root)
    path = Path(judgment_file).resolve()
    document = read_input(path.parent, path.name)
    judgment = object_fields(
        document.data,
        {"format", "binding", "reviewer", "origin", "supersedes", "judgments"},
        "judgment",
    )
    if (
        type(judgment["format"]) is not int
        or judgment["format"] != 2
        or judgment["binding"] != binding
    ):
        stale()
    text_field(judgment["reviewer"], "reviewer")
    previous = judgment["supersedes"]
    if previous is not None:
        object_fields(previous, {"file", "sha256"}, "supersedes")
        previous_path = (path.parent / previous["file"]).resolve()
        prior = read_input(previous_path.parent, previous_path.name)
        if prior.sha256 != previous["sha256"] or prior.data["binding"] != binding:
            stale()
    if type(judgment["judgments"]) is not list:
        stale()
    if judgment["origin"] == {"kind": "human"}:
        pass
    elif isinstance(judgment["origin"], dict) and judgment["origin"].get("kind") == "model":
        from traceh.evaluation.model_review_protocol import validate_model_origin

        try:
            validate_model_origin(root, report, binding, rubric, judgment)
        except (ValueError, TypeError, KeyError, OSError):
            stale()
    else:
        stale()
    trials = {t["identity"]["trial_id"]: t for t in report["trials"]}
    packets = _review_packets(root, report, rubric)
    seen = set()
    for item in judgment["judgments"]:
        object_fields(item, {"trial_id", "status", "reason"}, "judgment-item")
        trial_id, status = item["trial_id"], item["status"]
        if (
            trial_id not in trials
            or trial_id in seen
            or status not in {"passed", "failed", "pending_review"}
        ):
            stale()
        seen.add(trial_id)
        text_field(item["reason"], "reason")
        trial = trials[trial_id]
        if trial["execution"]["status"] != "completed" or not trial["measured"]:
            stale()
        if status == "passed":
            packet = packets[trial_id]
            from traceh.evaluation.model_review_protocol import hard_rejection

            if hard_rejection(trial, packet):
                stale()
        trial["assessment"] = {
            "status": status,
            "reviewer": judgment["reviewer"],
            "origin": judgment["origin"]["kind"],
            "reason": item["reason"],
        }
    report["assessment_complete"] = all(
        t["assessment"]["status"] in {"passed", "failed"} for t in report["trials"]
    )
    counts = Counter(t["assessment"]["status"] for t in report["trials"])
    report["task_report"]["assessment_counts"] = {
        s: counts[s] for s in ("passed", "failed", "pending_review", "unassessable")
    }
    assessment = {
        "format": 1,
        "binding": binding,
        "execution_run": str(root),
        "judgment": {"file": "judgments/" + document.sha256 + ".json", "sha256": document.sha256},
        "supersedes": previous,
    }
    report["assessment_artifact"] = fingerprint(assessment)
    report["execution_run"] = str(root)
    return report, assessment, document


def assess_run(root, judgment_file, output):
    root, output = _destination(root, output)
    report, assessment, document = reviewed_report(root, judgment_file)
    binding = assessment["binding"]
    _, _, rubric = _run(root)
    packets = _review_packets(root, report, rubric)
    observations = {}
    for trial in report["trials"]:
        packet = packets.get(trial["identity"]["trial_id"])
        if packet is not None:
            _, events = _episode_events(root, trial, packet)
            observations[trial["identity"]["trial_id"]] = (
                packet
                if report["task_type"] == "product_task"
                else observe_episode(packet, tuple(EventEnvelope.from_dict(e) for e in events))
            )
    diagnostics = _diagnostics(report, binding, observations)
    output.mkdir(parents=True)
    _write_diagnostics(output, diagnostics)
    (output / "judgments").mkdir()
    (output / assessment["judgment"]["file"]).write_bytes(document.content)
    _write(output / "assessment.json", assessment)
    _write(output / "report.json", report)
    lines = [
        "# Evaluation assessment",
        "",
        "Run: " + binding["run_id"],
        "",
        "| Trial | Assessment |",
        "|---|---|",
    ]
    lines += [
        f"| {t['identity']['trial_id']} | {t['assessment']['status']} |" for t in report["trials"]
    ]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "run_id": binding["run_id"],
        "assessment_complete": report["assessment_complete"],
        "assessment_counts": report["task_report"]["assessment_counts"],
    }


def load_assessment(path):
    """Recompute a derived assessment from its immutable judgment and original evidence."""
    path = Path(path).resolve()
    artifact = read_input(path.parent, path.name)
    raw = artifact.data
    report, expected, _ = reviewed_report(
        raw["execution_run"], path.parent / raw["judgment"]["file"]
    )
    if expected != raw or read_input(path.parent, "report.json").data != report:
        stale()
    return report, artifact
