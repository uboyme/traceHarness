"""Sequential manual optimization using the original isolated evaluation owner.

Definitions and admission receipts belong here. Trial state, usage and grades
remain in evaluation evidence; inspection always recomputes from that evidence.
No installation, strategy loading, model analysis or persistent resume lives here.
"""

from __future__ import annotations

import asyncio
import io
import json
import time
import zipfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from traceh.api.json_types import fingerprint, to_json_value
from traceh.api.optimization import (
    CandidateHistory,
    CandidateProposal,
    OptimizationRequest,
    TextEdit,
    observation_from_dict,
)
from traceh.concurrency import await_worker_convergence, combine_failures
from traceh.evaluation.comparison import inspect_experiment
from traceh.evaluation.contracts import CheckStatus, ConvergenceStatus
from traceh.evaluation.inputs import digest_bytes, read_input
from traceh.evaluation.plan import load_run_options
from traceh.evaluation.variant_execution import write_json
from traceh.evaluation.variants import environment_identity, source_digest, source_files
from traceh.evolution.optimization_contract import (
    OptimizationContract,
    OptimizationProgress,
    _require,
    admit_proposal,
    decide_next,
    validate_request,
)


@dataclass(frozen=True, slots=True)
class ManualCandidate:
    rationale: str
    targeted_failure_classes: tuple[str, ...]
    edits: tuple[TextEdit, ...]
    expected_tradeoffs: str

    def bind(self, request):
        return CandidateProposal(
            request.digest,
            request.base_source_digest,
            self.rationale,
            self.targeted_failure_classes,
            self.edits,
            self.expected_tradeoffs,
        )


def _request(contract, observations, history, number):
    return OptimizationRequest(
        contract.digest,
        contract.base_source_digest,
        contract.development_dataset_digest,
        number,
        contract.limits.max_analysis_tokens,
        observations,
        contract.editable_text,
        history,
    )


def _initial_progress(contract):
    return OptimizationProgress(
        contract.digest,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        False,
        False,
        ConvergenceStatus.CONVERGED,
        CheckStatus.PASSED,
    )


def candidate_decision(comparison):
    """Interpret a verified comparison, never score answers or create judgments."""
    if comparison["status"] == "not_comparable":
        return "stop", "evidence-not-comparable"
    if not comparison["complete"]:
        return "stop", "execution-incomplete"
    if comparison["hard_constraints"] != "passed":
        return "stop", "hard-constraints-not-passed"
    if any(a["cost"]["total_tokens"] is None for a in comparison["arms"]):
        return "stop", "evaluation-usage-unknown"
    states = [s for p in comparison["pairs"] for s in p["assessment"]]
    if "unassessable" in states:
        return "stop", "assessment-unassessable"
    if "pending_review" in states:
        return "await_review", "pending-review"
    thresholds = comparison["thresholds"]
    if comparison["changes"]["unknown"] or any(v is None for v in thresholds.values()):
        return "stop", "comparison-unassessable"
    if comparison["changes"]["loss"] or not all(thresholds.values()):
        return "continue", "not-qualified"
    if comparison["changes"]["gain"] or any(
        v is not None and v < 0 for v in comparison["cost_delta"].values()
    ):
        return "review_candidate", "development-candidate-qualified"
    return "continue", "no-gain"


def _inputs(template):
    doc = template.options.document
    _require(doc is not None, "optimization-paired-template-required")
    doc.verify()
    _require(
        template.options == load_run_options(doc.root / doc.relative), "optimization-plan-drift"
    )
    variants = template.options.variants
    _require(
        len(variants) == 2
        and [v.role for v in variants] == ["baseline", "candidate"]
        and all(v.patch is None for v in variants),
        "optimization-paired-template-required",
    )
    _require(
        all(
            doc.data["comparison"][name] is not None
            for name in (
                "min_pass_gain",
                "max_token_ratio",
                "max_tool_call_delta",
            )
        ),
        "optimization-thresholds-required",
    )
    template.evaluator.verify_inputs()
    result = {}
    for group, field in (("model", "script"), ("execution", "sandbox_config")):
        name = doc.data[group][field]
        if name is not None:
            path = (doc.root / name).resolve()
            result[field] = read_input(path.parent, path.name)
    return result


def _controls(template):
    return {
        "benchmark_digest": template.manifest.document.sha256,
        "dataset_digest": template.manifest.dataset.sha256,
        "settings": template.evaluator.frozen_settings(),
        "materials_digest": source_digest(template.evaluator.frozen_materials()),
        "trials": to_json_value(template.trials),
        "provider_implementation": template.provider_implementation,
        "sandbox": to_json_value(template.sandbox),
        "environment": to_json_value(environment_identity()),
    }


def _effective_plan(definition, patch_ref):
    raw = json.loads(json.dumps(definition["plan"]))
    raw["variants"][1]["source"] = patch_ref
    for group, field in (("model", "script"), ("execution", "sandbox_config")):
        if field in definition["inputs"]:
            raw[group][field] = field + ".json"
    return raw


def _rejection(code):
    if code == "optimization-duplicate-candidate":
        return "duplicate_proposals"
    if code in (
        "optimization-proposal-invalid",
        "optimization-no-change",
        "evaluation-candidate-invalid",
    ):
        return "invalid_proposals"
    return None


def _codes(error):
    if isinstance(error, BaseExceptionGroup):
        return [c for e in error.exceptions for c in _codes(e)]
    return [getattr(error, "code", type(error).__name__)]


def _load_definition(root):
    definition = read_input(root, "optimization.json").data
    _require(definition["format"] == 1, "optimization-version-unsupported")
    contract = OptimizationContract.from_dict(definition["contract"])
    _require(contract.digest == definition["contract_digest"], "optimization-contract-drift")
    archive = (root / "sources.zip").read_bytes()
    _require(digest_bytes(archive) == definition["sources_sha256"], "optimization-base-drift")
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        files = tuple((name, z.read(name)) for name in z.namelist())
    _require(source_digest(files) == contract.base_source_digest, "optimization-base-drift")
    plan = read_input(root, "run-plan.json")
    _require(
        plan.sha256 == contract.run_plan_digest and plan.data == definition["plan"],
        "optimization-plan-drift",
    )
    for name, digest in definition["inputs"].items():
        _require(read_input(root, name + ".json").sha256 == digest, "optimization-input-drift")
    observations = tuple(
        observation_from_dict(o)
        for o in definition["observations"]
    )
    return definition, contract, files, observations


def _native_comparison(root, definition, candidate, plan, outcome, assessments):
    native = root / "evaluation"
    frozen = read_input(native, "experiment.json").data
    controls = definition["controls"]
    _require(fingerprint(frozen) == outcome["experiment_digest"], "optimization-run-drift")
    _require(
        read_input(native, "inputs/run-plan.json").content == plan.content,
        "optimization-plan-drift",
    )
    _require(
        read_input(native, "inputs/patch-02.json").data == json.loads(candidate.patch_json),
        "optimization-candidate-drift",
    )
    for field in (
        "benchmark_digest",
        "settings",
        "provider_implementation",
        "sandbox",
        "environment",
    ):
        _require(frozen[field] == controls[field], "optimization-control-drift")
    for field in ("model", "execution", "comparison"):
        _require(frozen[field] == plan.data[field], "optimization-control-drift")
    _require(
        frozen["base_source_digest"] == definition["contract"]["base_source_digest"]
        and frozen["arms"][0]["source_digest"] == frozen["base_source_digest"]
        and frozen["arms"][1]["source_digest"] == candidate.source_digest,
        "optimization-source-drift",
    )
    _require(
        [t for a in frozen["arms"] for t in a["trials"]] == controls["trials"],
        "optimization-trial-drift",
    )
    for field, digest in definition["inputs"].items():
        _require(
            read_input(native, "inputs/" + field + ".json").sha256 == digest,
            "optimization-input-drift",
        )
    with zipfile.ZipFile(native / "artifacts/materials.zip") as z:
        materials = tuple((name, z.read(name)) for name in z.namelist())
    _require(
        source_digest(materials) == controls["materials_digest"], "optimization-materials-drift"
    )
    return inspect_experiment(native, assessments=assessments)


def inspect_optimization(root, *, assessments=None):
    """Read original evidence and optional explicit UE assessment manifests.

    `assessments` maps one-based round numbers to existing assessment manifests.
    It never starts work, overwrites evidence, or resumes queued candidates.
    """
    root = Path(root).resolve()
    definition, contract, files, observations = _load_definition(root)
    assessments = {} if assessments is None else assessments
    progress, history, rounds = _initial_progress(contract), (), []
    action, reason = "continue", "within-contract"
    directories = sorted((root / "rounds").iterdir())
    _require(
        set(assessments) <= set(range(1, len(directories) + 1)),
        "optimization-assessment-round-invalid",
    )
    for number, directory in enumerate(directories, 1):
        _require(
            directory.name == f"{number:04d}" and number <= len(definition["candidates"]),
            "optimization-round-invalid",
        )
        request = _request(contract, observations, history, number)
        validate_request(contract, request, files)
        _require(
            read_input(directory, "request.json").data == to_json_value(request),
            "optimization-request-drift",
        )
        raw = definition["candidates"][number - 1]
        draft = ManualCandidate(
            raw["rationale"],
            tuple(raw["targeted_failure_classes"]),
            tuple(TextEdit(**e) for e in raw["edits"]),
            raw["expected_tradeoffs"],
        )
        proposal = draft.bind(request)
        _require(
            read_input(directory, "proposal.json").data == to_json_value(proposal),
            "optimization-proposal-drift",
        )
        progress = replace(progress, rounds_started=number)
        outcome = read_input(directory, "outcome.json").data
        row = {"round": number, "outcome": outcome, "comparison": None}
        try:
            candidate = admit_proposal(
                contract,
                request,
                proposal,
                files,
                seen_candidate_digests=tuple(h.candidate_digest for h in history),
            )
        except (ValueError, OSError) as error:
            code = _codes(error)[0]
            _require(outcome == {"status": "rejected", "code": code}, "optimization-outcome-drift")
            counter = _rejection(code)
            if counter:
                progress = replace(progress, **{counter: getattr(progress, counter) + 1})
                action, reason = "continue", code
            else:
                action, reason = "stop", code
            row.update(action=action, reason=reason)
            rounds.append(row)
            if action != "continue":
                break
            continue
        patch = read_input(directory, "candidate.json")
        _require(patch.data == json.loads(candidate.patch_json), "optimization-candidate-drift")
        plan = read_input(directory, "plan.json")
        _require(
            plan.data == _effective_plan(definition, patch.reference()), "optimization-plan-drift"
        )
        intent = read_input(directory, "intent.json").data
        _require(
            intent
            == {
                "contract_digest": contract.digest,
                "request_digest": request.digest,
                "candidate_digest": candidate.candidate_digest,
                "plan_sha256": plan.sha256,
                "reserved_trials": len(definition["controls"]["trials"]),
            },
            "optimization-reservation-drift",
        )
        progress = replace(progress, candidates_admitted=progress.candidates_admitted + 1)
        if outcome["experiment_digest"] is None:
            action, reason = "stop", "execution-evidence-missing"
            progress = replace(
                progress, evidence=CheckStatus.UNPROVEN, convergence=ConvergenceStatus.UNKNOWN
            )
        else:
            assessment = assessments.get(number)
            assessment_file = (
                None
                if assessment is None
                else read_input(
                    Path(assessment).resolve().parent,
                    Path(assessment).name,
                )
            )
            comparison = _native_comparison(
                directory,
                definition,
                candidate,
                plan,
                outcome,
                None if assessment_file is None else Path(assessment),
            )
            if assessment_file is not None:
                assessment_file.verify()
                row["assessment"] = {
                    "file": str(Path(assessment).resolve()),
                    "sha256": assessment_file.sha256,
                }
            row["comparison"] = comparison
            action, reason = candidate_decision(comparison)
            progress = replace(
                progress,
                evidence=CheckStatus(comparison.get("hard_constraints", "unproven")),
                convergence=(
                    ConvergenceStatus.CONVERGED
                    if comparison["complete"]
                    else ConvergenceStatus.UNKNOWN
                ),
                trials_started=progress.trials_started
                + sum(
                    e["status"] != "not_started"
                    for p in comparison.get("pairs", [])
                    for e in p["execution"]
                ),
                pending_reviews=sum(
                    s == "pending_review"
                    for p in comparison.get("pairs", [])
                    for s in p["assessment"]
                ),
            )
        if outcome["errors"]:
            action, reason = "stop", outcome["status"]
            progress = replace(progress, cancelled=outcome["status"] == "cancelled")
        progress = replace(
            progress,
            consecutive_no_gain=(progress.consecutive_no_gain + 1 if action == "continue" else 0),
        )
        history += (CandidateHistory(candidate.candidate_digest, reason),)
        row.update(candidate_digest=candidate.candidate_digest, action=action, reason=reason)
        rounds.append(row)
        if action != "continue":
            break
    _require(len(rounds) == len(directories), "optimization-work-after-stop")
    if (root / "stop.json").exists():
        stopped = read_input(root, "stop.json").data
        _require(
            stopped["contract_digest"] == contract.digest
            and stopped["after_rounds"] == len(rounds),
            "optimization-stop-drift",
        )
        action, reason = "stop", stopped["code"]
    if action == "continue":
        decision = decide_next(
            contract,
            progress,
            now_utc=datetime.now(UTC),
            next_trials=len(definition["controls"]["trials"]),
            next_analysis_tokens=0,
        )
        action, reason = decision.action, decision.reason
        if action == "continue" and len(rounds) == len(definition["candidates"]):
            action, reason = "stop", "manual-candidates-exhausted"
    return {
        "format": 1,
        "experiment_id": contract.experiment_id,
        "contract_digest": contract.digest,
        "action": action,
        "reason": reason,
        "adoption_authorized": False,
        "execution_resumable": False,
        "progress": to_json_value(progress),
        "rounds": rounds,
        "history": to_json_value(history),
        "remaining_candidates": len(definition["candidates"]) - len(rounds),
    }


def write_optimization_report(root, output, *, assessments=None):
    """Export a fresh report; cached earlier reports never decide the next step."""
    root, output = Path(root).resolve(), Path(output).resolve()
    _require(not output.exists() and not root.is_relative_to(output), "optimization-output-overlap")
    report = inspect_optimization(root, assessments=assessments)
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "report.json", report)
    lines = [
        "# Manual optimization",
        "",
        f"Experiment: {report['experiment_id']}",
        f"Decision: {report['action']} / {report['reason']}",
        "",
        "Development comparison only. No adoption, installation or holdout claim.",
        "",
        "| Round | Candidate | Decision | Reason |",
        "|---|---|---|---|",
    ]
    for r in report["rounds"]:
        lines.append(
            f"| {r['round']} | {r.get('candidate_digest', 'rejected')} | "
            f"{r['action']} | {r['reason']} |"
        )
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


async def run_manual_optimization(template, contract, *, candidates, observations, output_dir):
    """Run one explicit manual queue. Return only after original workers converge."""
    inputs = _inputs(template)
    _, files = source_files()
    controls = _controls(template)
    _require(contract.base_source_digest == source_digest(files), "optimization-base-drift")
    _require(
        contract.benchmark_digest == controls["benchmark_digest"]
        and contract.development_dataset_digest == controls["dataset_digest"]
        and contract.run_plan_digest == template.options.document.sha256,
        "optimization-control-drift",
    )
    _require(
        set(contract.development_case_ids) == {t.case_id for t in template.trials},
        "optimization-development-scope-invalid",
    )
    _require(
        type(candidates) is tuple and all(type(c) is ManualCandidate for c in candidates),
        "optimization-manual-queue-invalid",
    )
    validate_request(contract, _request(contract, observations, (), 1), files)
    root = Path(output_dir).resolve()  # noqa: ASYNC240 - synchronous admission before first await
    _require(
        not root.is_relative_to(template.manifest.directory.resolve()),
        "optimization-output-overlap",
    )
    # Exclusive create precedes the first await: one writer, no takeover/resume.
    root.mkdir(parents=True, exist_ok=False)
    (root / "rounds").mkdir()
    with zipfile.ZipFile(root / "sources.zip", "x", compression=zipfile.ZIP_DEFLATED) as z:
        for name, content in files:
            z.writestr(name, content)
    (root / "run-plan.json").write_bytes(template.options.document.content)
    for name, doc in inputs.items():
        (root / (name + ".json")).write_bytes(doc.content)
    definition = {
        "format": 1,
        "contract": contract.to_dict(),
        "contract_digest": contract.digest,
        "sources_sha256": digest_bytes((root / "sources.zip").read_bytes()),
        "controls": controls,
        "plan": template.options.document.data,
        "inputs": {name: doc.sha256 for name, doc in inputs.items()},
        "candidates": to_json_value(candidates),
        "observations": to_json_value(observations),
    }
    write_json(root / "optimization.json", definition)
    monotonic_deadline = (
        time.monotonic() + (contract.deadline_utc - datetime.now(UTC)).total_seconds()
    )
    while True:
        report = inspect_optimization(root)
        if report["action"] != "continue":
            return write_optimization_report(root, root / "report")
        # Revalidate the same source, model inputs and domain owners before each batch.
        try:
            current_inputs = _inputs(template)
            _require(
                _controls(template) == controls
                and source_digest(source_files()[1]) == contract.base_source_digest
                and {n: d.sha256 for n, d in current_inputs.items()} == definition["inputs"],
                "optimization-input-drift",
            )
        except (ValueError, OSError) as error:
            write_json(
                root / "stop.json",
                {
                    "contract_digest": contract.digest,
                    "after_rounds": len(report["rounds"]),
                    "code": _codes(error)[0],
                },
            )
            return write_optimization_report(root, root / "report")
        number = report["progress"]["rounds_started"] + 1
        history = tuple(CandidateHistory(**h) for h in report["history"])
        request = _request(contract, observations, history, number)
        proposal = candidates[number - 1].bind(request)
        directory = root / "rounds" / f"{number:04d}"
        directory.mkdir()
        write_json(directory / "request.json", to_json_value(request))
        write_json(directory / "proposal.json", to_json_value(proposal))
        try:
            candidate = admit_proposal(
                contract,
                request,
                proposal,
                files,
                seen_candidate_digests=tuple(h.candidate_digest for h in history),
            )
        except (ValueError, OSError) as error:
            write_json(directory / "outcome.json", {"status": "rejected", "code": _codes(error)[0]})
            continue
        write_json(directory / "candidate.json", json.loads(candidate.patch_json))
        patch = read_input(directory, "candidate.json")
        write_json(directory / "plan.json", _effective_plan(definition, patch.reference()))
        for name, doc in inputs.items():
            (directory / (name + ".json")).write_bytes(doc.content)
        plan = read_input(directory, "plan.json")
        write_json(
            directory / "intent.json",
            {
                "contract_digest": contract.digest,
                "request_digest": request.digest,
                "candidate_digest": candidate.candidate_digest,
                "plan_sha256": plan.sha256,
                "reserved_trials": len(template.trials),
            },
        )
        primary, secondary, worker = None, None, None
        status = "evaluated"
        try:
            duration = min(
                monotonic_deadline - time.monotonic(),
                (contract.deadline_utc - datetime.now(UTC)).total_seconds(),
            )
            if duration <= 0:
                raise TimeoutError
            native = template.for_plan(directory / "evaluation", directory / "plan.json")
            worker = asyncio.create_task(native.run())
            async with asyncio.timeout(duration):
                await asyncio.shield(worker)
        except BaseException as error:
            primary = error
            status = (
                "cancelled"
                if isinstance(error, asyncio.CancelledError)
                else ("deadline" if isinstance(error, TimeoutError) else "execution-failed")
            )
            if worker is not None and not worker.done():
                worker.cancel()
                await await_worker_convergence(worker)
                if not worker.cancelled():
                    secondary = worker.exception()
        error = combine_failures(
            primary, secondary, "optimization execution and convergence failed"
        )
        experiment = directory / "evaluation/experiment.json"
        try:
            write_json(
                directory / "outcome.json",
                {
                    "status": status,
                    "errors": [] if error is None else _codes(error),
                    "experiment_digest": (
                        fingerprint(read_input(experiment.parent, experiment.name).data)
                        if experiment.exists()
                        else None
                    ),
                },
            )
            if error is not None:
                write_optimization_report(root, root / "report")
        except BaseException as publication_error:
            raise combine_failures(
                error, publication_error, "optimization execution and evidence publication failed"
            ) from None
        if error is not None:
            if isinstance(primary, asyncio.CancelledError) or secondary is not None:
                raise error
            return inspect_optimization(root)
