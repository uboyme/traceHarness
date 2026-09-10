"""Offline paired measurements. No candidate generation, execution or adoption authority."""

import json
import sqlite3
import zipfile
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

from traceh.api.json_types import fingerprint
from traceh.evaluation.errors import BenchmarkManifestError
from traceh.evaluation.evidence import load_run
from traceh.evaluation.inputs import object_fields, read_input
from traceh.evaluation.plan import comparison_policy
from traceh.evaluation.variants import apply_candidate, source_digest


def _require(condition, field):
    if not condition:
        raise BenchmarkManifestError("evaluation-comparison-incompatible", field)


def _archive(root, relative):
    with zipfile.ZipFile(root / relative) as archive:
        return tuple(sorted((n, archive.read(n)) for n in archive.namelist()))


def _unknown(spec, reason):
    return {
        "identity": spec,
        "execution": {"status": "not_started", "reason": reason},
        "assessment": {"status": "unassessable"},
        "invariants": "unproven",
        "convergence": "unknown",
        "measured": False,
        "evidence": [],
        "usage": {},
        "errors": [reason],
    }


def _statistics(root, report):
    """Both evaluator types share native Session events, never inferred token counts."""
    result = {}
    for trial in report["trials"]:
        ends, calls, rejected = [], [], []
        for ref in trial["evidence"]:
            if not ref["stream_id"].startswith("session:"):
                continue
            with closing(
                sqlite3.connect((root / ref["file"]).as_uri() + "?mode=ro", uri=True)
            ) as db:
                rows = db.execute(
                    "SELECT envelope_json FROM events WHERE stream_id=? ORDER BY seq",
                    (ref["stream_id"],),
                ).fetchall()
            for (raw,) in rows:
                event = json.loads(raw)
                if event["type"] == "model/attempt-end":
                    ends.append(event["data"])
                elif event["type"] == "tool/call":
                    calls.append(event["data"])
                elif event["type"] == "tool/result" and event["data"].get("status") != "succeeded":
                    rejected.append(event["data"])
        known = [
            e["usage"]
            for e in ends
            if e.get("usage") is not None and e["usage"].get("quality") in {"exact", "estimated"}
        ]
        exact = len(known) == len(ends) and all(u["quality"] == "exact" for u in known)
        fingerprints = Counter(
            fingerprint({"name": c["tool_name"], "arguments": c["arguments"]}) for c in calls
        )
        result[trial["identity"]["trial_id"]] = {
            "attempts": len(ends),
            "unknown_attempts": len(ends) - len(known),
            "estimated_attempts": sum(u["quality"] == "estimated" for u in known),
            "total_tokens": sum(u["total_tokens"] for u in known)
            if exact and trial["measured"]
            else None,
            "known_subtotal_tokens": sum(u["total_tokens"] for u in known),
            "tool_calls": len(calls) if trial["measured"] else None,
            "search_read_calls": sum(
                c["tool_name"].startswith(("search_", "request_", "read_tool_")) for c in calls
            ),
            "repeated_call_arguments": sum(n - 1 for n in fingerprints.values()),
            "non_success_tool_results": len(rejected),
            "failed_attempts": sum(e["status"] != "succeeded" for e in ends),
            "phase_usage": trial["usage"],
        }
    return result


def _load(root, assessments):
    frozen = read_input(root, "experiment.json").data
    policy = comparison_policy(frozen["comparison"])
    _require(frozen["format"] == 1 and len(frozen["arms"]) == 2, "experiment")
    for ref in (*frozen["artifacts"], *frozen["inputs"]):
        _require(read_input(root, ref["file"]).sha256 == ref["sha256"], "artifact")
    sources = _archive(root, "artifacts/base-source.zip")
    materials = _archive(root, "artifacts/materials.zip")
    _require(source_digest(sources) == frozen["base_source_digest"], "base-source")
    execution = read_input(root, "execution.json").data
    _require(execution["experiment_digest"] == fingerprint(frozen), "execution")
    judged = {}
    if assessments is not None:
        doc = read_input(Path(assessments).resolve().parent, Path(assessments).name)
        raw = object_fields(doc.data, {"format", "experiment_digest", "assessments"}, "assessments")
        _require(
            raw["format"] == 1 and raw["experiment_digest"] == fingerprint(frozen), "assessments"
        )
        _require(
            type(raw["assessments"]) is dict
            and set(raw["assessments"]) <= {a["variant_id"] for a in frozen["arms"]},
            "assessments",
        )
        from traceh.evaluation.review import load_assessment

        for name, ref in raw["assessments"].items():
            object_fields(ref, {"file", "sha256"}, "assessment")
            report, artifact = load_assessment(doc.root / ref["file"])
            _require(artifact.sha256 == ref["sha256"], "assessment-digest")
            judged[name] = (report, artifact.reference())
    reports, bindings, statistics, packets = [], [], [], []
    for index, arm in enumerate(frozen["arms"]):
        _require(arm["role"] == ("baseline", "candidate")[index], "variant-role")
        _require(arm["directory"] == f"arms/{index + 1:02d}", "variant-directory")
        expected = sources
        if arm["patch"] is not None:
            _require(index == 1, "baseline-patch")
            patch = read_input(root, arm["patch"]["file"])
            _require(patch.sha256 == arm["patch"]["sha256"], "patch")
            expected = apply_candidate(sources, patch.data)
        _require(source_digest(expected) == arm["source_digest"], "variant-source")
        directory = root / arm["directory"]
        _require(read_input(directory, "plan.json").sha256 == arm["plan_sha256"], "arm-plan")
        run = directory / "run"
        if not (run / "report.json").is_file():
            reports.append(
                {
                    "trials": [_unknown(t, "evaluation-worker-no-report") for t in arm["trials"]],
                    "complete": False,
                    "task_report": {},
                }
            )
            bindings.append(None)
            statistics.append({})
            packets.append({})
            continue
        condition, report, binding = load_run(run)
        _require(
            condition["variant"]["source_digest"] == arm["source_digest"]
            and condition["variant"]["variant_id"] == arm["variant_id"]
            and condition["environment"] == frozen["environment"]
            and condition["settings"] == frozen["settings"]
            and condition["sandbox"] == frozen["sandbox"]
            and condition["benchmark"]["sha256"] == frozen["benchmark_digest"]
            and condition["task_type"] == frozen["task_type"]
            and condition["trials"] == arm["trials"],
            "controls",
        )
        model = condition["model"]
        plan_model = frozen["model"]
        _require(
            model["provider_id"] == plan_model["provider"]
            and model["model_id"] == plan_model["model"]
            and model["implementation"] == frozen["provider_implementation"]
            and model["configuration"]["network_mode"] == frozen["execution"]["network_mode"],
            "model",
        )
        _require(_archive(run, "artifacts/materials.zip") == materials, "materials")
        receipt = read_input(directory, "worker-receipt.json").data
        request = read_input(directory, "worker.json").data
        process = read_input(directory, "process.json").data
        _require(
            receipt["format"] == 2
            and receipt["request_digest"] == fingerprint(request)
            and request["experiment_digest"] == fingerprint(frozen)
            and request["arm"] == arm
            and (
                (
                    receipt["pid"] == process["pid"]
                    and receipt["parent_pid"] == process["owner_pid"]
                )
                or receipt["parent_pid"] == process["pid"]
            )
            and receipt["source_digest"] == arm["source_digest"]
            and receipt["environment"] == frozen["environment"],
            "worker",
        )
        _require(receipt["report_digest"] == binding["report_digest"], "worker-report")
        if index < len(execution["outcomes"]):
            outcome = execution["outcomes"][index]
            _require(
                outcome.get("receipt_sha256")
                == read_input(directory, "worker-receipt.json").sha256,
                "worker-receipt",
            )
        binding["controls_digest"] = fingerprint(
            {k: condition[k] for k in ("model", "environment", "sandbox", "settings", "execution")}
        )
        binding["run_directory"] = (run.relative_to(root)).as_posix()
        binding["resource_convergence"] = receipt["convergence"]
        binding["forced_stop"] = process["forced_stop"]
        statistics.append(_statistics(run, report))
        packets.append({p["trial_id"]: p for p in report["task_report"].get("episodes", [])})
        if arm["variant_id"] in judged:
            assessed, ref = judged[arm["variant_id"]]
            _require(
                assessed["run_id"] == report["run_id"]
                and assessed["frozen_digest"] == report["frozen_digest"],
                "assessment-run",
            )
            report = assessed
            binding["assessment"] = ref
        reports.append(report)
        bindings.append(binding)
    if all(bindings):
        _require(
            bindings[0]["controls_digest"] == bindings[1]["controls_digest"], "paired-controls"
        )
    return frozen, reports, bindings, statistics, packets, policy, execution


def paired_measurements(reports, statistics, packets, policy):
    """Pure calculation over validated observations. Keep every planned slot."""

    def key(trial):
        identity = trial["identity"]
        return tuple(
            identity[k]
            for k in (
                "case_id",
                "group_id",
                "material_digest",
                "material_seed",
                "replicate",
                "requested_mode",
            )
        )

    indexed = [{key(t): t for t in r["trials"]} for r in reports]
    _require(
        all(len(i) == len(r["trials"]) for i, r in zip(indexed, reports, strict=True))
        and set(indexed[0]) == set(indexed[1]),
        "pairing",
    )
    pairs, groups = [], defaultdict(lambda: [Counter(), Counter()])
    costs = [{"total_tokens": 0, "tool_calls": 0}, {"total_tokens": 0, "tool_calls": 0}]
    for k, baseline in indexed[0].items():
        trials = baseline, indexed[1][k]
        states = [t["assessment"]["status"] for t in trials]
        stats = [statistics[i].get(t["identity"]["trial_id"], {}) for i, t in enumerate(trials)]
        prep = [
            packets[i].get(t["identity"]["trial_id"], {}).get("preparation_text_digest")
            for i, t in enumerate(trials)
        ]
        reason = None
        if prep[0] != prep[1]:
            reason = "preparation-text-differs"
        elif any(s not in ("passed", "failed") for s in states) or any(
            not t["measured"] for t in trials
        ):
            reason = "assessment-unavailable"
        change = (
            "unknown"
            if reason
            else (
                "unchanged"
                if states[0] == states[1]
                else "gain"
                if states[1] == "passed"
                else "loss"
            )
        )
        for i, _trial in enumerate(trials):
            for metric in costs[i]:
                value = stats[i].get(metric)
                costs[i][metric] = (
                    None if value is None or costs[i][metric] is None else costs[i][metric] + value
                )
            groups[k[1]][i][states[i]] += 1
        pairs.append(
            {
                "key": dict(
                    zip(
                        (
                            "case_id",
                            "group_id",
                            "material_digest",
                            "material_seed",
                            "replicate",
                            "requested_mode",
                        ),
                        k,
                        strict=True,
                    )
                ),
                "change": change,
                "reason": reason,
                "assessment": states,
                "execution": [t["execution"] for t in trials],
                "invariants": [t["invariants"] for t in trials],
                "convergence": [t["convergence"] for t in trials],
                "metrics": stats,
                "preparation_text_equal": prep[0] == prep[1],
            }
        )
    counts = Counter(p["change"] for p in pairs)
    quality_status = (
        "inconclusive"
        if counts["unknown"]
        else "mixed"
        if counts["gain"] and counts["loss"]
        else "improved"
        if counts["gain"]
        else "regressed"
        if counts["loss"]
        else "no_change"
    )
    deltas = {
        k: None if any(c[k] is None for c in costs) else costs[1][k] - costs[0][k] for k in costs[0]
    }
    threshold = {}
    if policy["min_pass_gain"] is not None:
        threshold["min_pass_gain"] = (
            None
            if counts["unknown"]
            else counts["gain"] - counts["loss"] >= policy["min_pass_gain"]
        )
    if policy["max_token_ratio"] is not None:
        threshold["max_token_ratio"] = (
            None
            if deltas["total_tokens"] is None
            else (costs[1]["total_tokens"] <= costs[0]["total_tokens"] * policy["max_token_ratio"])
        )
    if policy["max_tool_call_delta"] is not None:
        threshold["max_tool_call_delta"] = (
            None
            if deltas["tool_calls"] is None
            else deltas["tool_calls"] <= policy["max_tool_call_delta"]
        )
    violations = any(t["invariants"] == "violated" for r in reports for t in r["trials"])
    unproven = any(
        t["invariants"] == "unproven" or t["convergence"] != "converged"
        for r in reports
        for t in r["trials"]
    )
    improvement = counts["gain"] > 0 or any(v is not None and v < 0 for v in deltas.values())
    regression = (
        counts["loss"] > 0 or violations or any(v is not None and v > 0 for v in deltas.values())
    )
    if counts["unknown"] or unproven or any(v is None for v in threshold.values()):
        status = "inconclusive"
    elif improvement and regression:
        status = "mixed"
    elif regression:
        status = "regressed"
    elif improvement:
        status = "improved"
    else:
        status = "no_change"
    summaries = []
    for i, report in enumerate(reports):
        c = Counter(t["assessment"]["status"] for t in report["trials"])
        summaries.append(
            {
                "planned": len(pairs),
                "assessment_counts": dict(c),
                "passed_over_planned": c["passed"] / len(pairs) if pairs else None,
                "assessable": c["passed"] + c["failed"],
                "cost": costs[i],
            }
        )
    return {
        "status": status,
        "quality_status": quality_status,
        "planned_pairs": len(pairs),
        "pairs": pairs,
        "changes": {s: counts[s] for s in ("gain", "loss", "unchanged", "unknown")},
        "arms": summaries,
        "groups": {g: [dict(c) for c in cs] for g, cs in groups.items()},
        "cost_delta": deltas,
        "thresholds": threshold,
        "hard_constraints": "violated" if violations else "unproven" if unproven else "passed",
    }


def comparison_markdown(report):
    lines = [
        "# Evaluation comparison",
        "",
        f"Run: {report['run_id']}",
        f"Status: {report['status']}",
        f"Measurement complete: {report['complete']}",
        "",
        "Descriptive results only; this report grants no adoption or installation authority.",
        "",
        "| Case | Replicate | Baseline | Candidate | Change |",
        "|---|---|---|---|---|",
    ]
    for p in report.get("pairs", []):
        lines.append(
            f"| {p['key']['case_id']} | {p['key']['replicate']} | {p['assessment'][0]} | "
            f"{p['assessment'][1]} | {p['change']} |"
        )
    lines += [
        "",
        "## Frozen result",
        "",
        "```json",
        json.dumps(report, ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    return "\n".join(lines)


def inspect_experiment(root, *, assessments=None):
    """Recompute from original evidence without creating files or executing work."""
    root = Path(root).resolve()
    frozen = read_input(root, "experiment.json").data
    result = {
        "format": 1,
        "run_id": frozen["run_id"],
        "benchmark_id": frozen["benchmark_id"],
        "task_type": frozen["task_type"],
        "experiment_digest": fingerprint(frozen),
        "execution_run": str(root),
        "adoption_authorized": False,
        "remote_model_revision": "unavailable",
        "complete": False,
        "planned_trials": [t for arm in frozen["arms"] for t in arm["trials"]],
        "planned_pairs": len(frozen["arms"][0]["trials"]),
    }
    try:
        frozen, reports, bindings, statistics, packets, policy, execution = _load(root, assessments)
        result.update(paired_measurements(reports, statistics, packets, policy))
        result["bindings"] = bindings
        result["execution_errors"] = execution["errors"]
        result["complete"] = (
            not execution["errors"]
            and all(r["complete"] for r in reports)
            and all(
                b is not None and b["resource_convergence"] == "converged" and not b["forced_stop"]
                for b in bindings
            )
        )
        if not result["complete"]:
            result["status"] = "inconclusive"
    except (BenchmarkManifestError, KeyError, ValueError, OSError) as error:
        result.update(
            status="not_comparable", reason=getattr(error, "code", "evaluation-evidence-mismatch")
        )
    return result


def compare_experiment(root, output, *, assessments=None):
    root, output = Path(root).resolve(), Path(output).resolve()
    _require(not output.exists() and not root.is_relative_to(output), "output")
    result = inspect_experiment(root, assessments=assessments)
    from traceh.evaluation.variant_execution import write_json

    output.mkdir(parents=True)
    write_json(output / "report.json", result)
    (output / "report.md").write_text(comparison_markdown(result), encoding="utf-8")
    return result
