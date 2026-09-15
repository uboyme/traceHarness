"""One sequential evaluation scheduler; execution and grading belong to evaluators."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import zipfile
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from traceh.api.json_types import fingerprint, to_json_value
from traceh.concurrency import await_worker_convergence, combine_failures
from traceh.evaluation.contracts import (
    AssessmentStatus,
    CheckStatus,
    ConvergenceStatus,
    Evaluator,
    EvidenceRef,
    ExecutionStatus,
    TaskType,
    TrialContext,
    TrialResult,
)
from traceh.evaluation.errors import BenchmarkExecutionError, BenchmarkManifestError
from traceh.evaluation.inputs import digest_bytes
from traceh.evaluation.manifest import BENCHMARK_PROTOCOL_VERSION, load_benchmark_manifest
from traceh.evaluation.plan import RunOptions
from traceh.evaluation.report import EvaluationReport, render_markdown
from traceh.evaluation.variants import environment_identity, source_digest, source_files
from traceh.llm.retry import NO_MODEL_RETRY, ModelRetryPolicy

REPORT_JSON = "report.json"
REPORT_MARKDOWN = "report.md"
ATTEMPTS_DIRECTORY = "attempts"


def _write_json(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def _source_files():
    return source_files()


def _source_digest(files):
    return source_digest(files)


def _errors(error):
    if isinstance(error, BaseExceptionGroup):
        return tuple(code for part in error.exceptions for code in _errors(part))
    code = getattr(error, "code", None)
    return (code if isinstance(code, str) else type(error).__name__,)


def _evidence_files(output, directory):
    """Read only closed stores; domain validity was checked by the evaluator."""
    refs = []
    for path in sorted(directory.rglob("events.sqlite3")):
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            streams = connection.execute(
                "SELECT DISTINCT stream_id FROM events ORDER BY stream_id"
            ).fetchall()
            for (stream,) in streams:
                rows = connection.execute(
                    "SELECT seq, envelope_json FROM events WHERE stream_id=? ORDER BY seq",
                    (stream,),
                ).fetchall()
                refs.append(
                    EvidenceRef(
                        path.relative_to(output).as_posix(),
                        stream,
                        rows[0][0],
                        rows[-1][0],
                        fingerprint([row[1] for row in rows]),
                    )
                )
        finally:
            connection.close()
    return tuple(refs)


def _write_reports(output: Path, report: EvaluationReport) -> None:
    _write_json(
        output / "evidence-manifest.json",
        {
            "format": 1,
            "run_id": report.run_id,
            "frozen_digest": report.frozen_digest,
            "trials": [
                {
                    "trial_id": item.spec.trial_id,
                    "convergence": item.convergence.value,
                    "references": to_json_value(item.evidence),
                }
                for item in report.trials
            ],
        },
    )
    _write_json(output / REPORT_JSON, report.to_dict())
    (output / REPORT_MARKDOWN).write_text(render_markdown(report), encoding="utf-8")


class EvaluationRunner:
    def __init__(
        self,
        benchmark_dir,
        output_dir,
        *,
        provider,
        model_id,
        retry_policy=NO_MODEL_RETRY,
        sandbox=None,
        monotonic=time.monotonic,
        options: RunOptions | None = None,
        provider_binding=None,
        worker_api_key=None,
    ):
        provider_id = getattr(provider, "name", None)
        if type(provider_id) is not str or not provider_id:
            raise BenchmarkExecutionError("benchmark-provider-binding-missing")
        self.manifest = load_benchmark_manifest(Path(benchmark_dir))
        self.output = Path(output_dir).resolve()
        if self.output.is_relative_to(self.manifest.directory.resolve()):
            raise BenchmarkManifestError("evaluation-output-overlap", "output")
        from traceh.evaluation.evaluators.product import ProductTaskEvaluator
        from traceh.evaluation.evaluators.retrieval_episode import RetrievalEpisodeEvaluator

        evaluator_type = {
            TaskType.PRODUCT_TASK: ProductTaskEvaluator,
            TaskType.RETRIEVAL_EPISODE: RetrievalEpisodeEvaluator,
        }[self.manifest.task_type]
        self.evaluator: Evaluator = evaluator_type(
            self.manifest,
            provider=provider,
            model_id=model_id,
            retry_policy=retry_policy,
            sandbox=sandbox,
            monotonic=monotonic,
        )
        self.options = options or RunOptions()
        identifiers = (
            tuple(v.variant_id for v in self.options.variants)
            if self.options.variants
            else (self.options.variant_id,)
        )
        self.trials = tuple(
            t
            for identifier in identifiers
            for t in self.evaluator.trials(identifier, self.options.repetitions)
        )
        if self.options.variants:
            policy = self.options.document.data["comparison"]
            if (
                policy["kind"] == "execution_strategy"
                and self.manifest.task_type is not TaskType.PRODUCT_TASK
            ):
                raise BenchmarkManifestError("evaluation-comparison-incompatible", "task_type")
            if policy["requested_modes"] is not None:
                modes = dict(zip(identifiers, policy["requested_modes"], strict=True))
                if any(
                    not any(t.variant_id == v and t.requested_mode == m for t in self.trials)
                    for v, m in modes.items()
                ):
                    raise BenchmarkManifestError(
                        "evaluation-comparison-incompatible", "requested_modes"
                    )
                self.trials = tuple(
                    t for t in self.trials if t.requested_mode == modes[t.variant_id]
                )
        for name, field in (
            ("case_ids", "case_id"),
            ("material_seeds", "material_seed"),
            ("requested_modes", "requested_mode"),
        ):
            selected = getattr(self.options, name)
            if selected is not None:
                if not set(selected) <= {getattr(trial, field) for trial in self.trials}:
                    raise BenchmarkManifestError("evaluation-manifest-invalid", name)
                self.trials = tuple(t for t in self.trials if getattr(t, field) in selected)
        if not self.trials:
            raise BenchmarkManifestError("evaluation-manifest-invalid", "empty-selection")
        if self.options.max_trials is not None and len(self.trials) > self.options.max_trials:
            raise BenchmarkManifestError("evaluation-trial-limit", "max_trials")
        if self.options.document is not None:
            if self.options.document.data["benchmark_digest"] != self.manifest.document.sha256:
                raise BenchmarkManifestError("evaluation-frozen-input-drift", "benchmark_digest")
            supplied = self.options.document.data["model"]
            if (
                supplied["provider"] != provider_id
                or supplied["model"] != model_id
                or ModelRetryPolicy(**supplied["retry_policy"]).to_dict() != retry_policy.to_dict()
            ):
                raise BenchmarkManifestError("evaluation-run-plan-conflict", "model")
        self.provider_id = provider_id
        self.model_id = model_id
        self.provider_implementation = f"{type(provider).__module__}.{type(provider).__qualname__}"
        self.provider_binding = provider_binding
        # Process-local credential borrow; never part of frozen settings or reports.
        self.worker_api_key = worker_api_key
        self.retry_policy = retry_policy
        self.sandbox = sandbox
        self._provider = provider
        self._monotonic = monotonic

    def for_plan(self, output_dir: Path, plan_file: Path) -> EvaluationRunner:
        """Build the same evaluator with an explicit plan; borrow no live trial state."""
        from traceh.evaluation.plan import load_run_options

        return EvaluationRunner(
            self.manifest.directory,
            output_dir,
            provider=self._provider,
            model_id=self.model_id,
            retry_policy=self.retry_policy,
            sandbox=self.sandbox,
            monotonic=self._monotonic,
            options=load_run_options(plan_file),
            provider_binding=self.provider_binding,
            worker_api_key=self.worker_api_key,
        )

    async def run(self):
        if self.options.variants:
            from traceh.evaluation.variant_execution import execute_variants

            return await execute_variants(self)
        self.evaluator.verify_inputs()
        if self.options.document is not None:
            self.options.document.verify()
        materials = self.evaluator.frozen_materials()
        self.output.mkdir(parents=True, exist_ok=False)
        run_id = str(uuid4())
        _, sources = _source_files()
        source_digest = _source_digest(sources)
        artifacts = self.output / "artifacts"
        artifacts.mkdir()
        with zipfile.ZipFile(
            artifacts / "source.zip", "x", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for name, data in sources:
                archive.writestr(name, data)
        with zipfile.ZipFile(
            artifacts / "materials.zip", "x", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for name, data in materials:
                archive.writestr(name, data)
        frozen = {
            "format": 1,
            "protocol_version": BENCHMARK_PROTOCOL_VERSION,
            "run_id": run_id,
            "benchmark": self.manifest.document.reference(),
            "dataset": self.manifest.dataset.reference(),
            "task_type": self.manifest.task_type.value,
            "variant": {
                "variant_id": self.options.variant_id,
                "role": "current",
                "source_digest": source_digest,
            },
            "model": {
                "provider_id": self.provider_id,
                "model_id": self.model_id,
                "implementation": self.provider_implementation,
                "configuration": self.provider_binding,
                "revision": None,
                "retry_policy": self.retry_policy.to_dict(),
            },
            "environment": to_json_value(environment_identity()),
            "sandbox": to_json_value(self.sandbox),
            "settings": self.evaluator.frozen_settings(),
            "artifacts": [
                {
                    "file": f"artifacts/{name}",
                    "sha256": digest_bytes((artifacts / name).read_bytes()),
                }
                for name in ("source.zip", "materials.zip")
            ],
            "run_plan": None
            if self.options.document is None
            else self.options.document.reference(),
            "trials": to_json_value(self.trials),
            "execution": {
                "repetitions": self.options.repetitions,
                "max_trials": len(self.trials)
                if self.options.max_trials is None
                else self.options.max_trials,
                "timeout_seconds": self.options.timeout_seconds,
            },
        }
        _write_json(self.output / "frozen.json", frozen)
        results = []
        primary = None
        errors = ()
        started = time.monotonic()
        for index, spec in enumerate(self.trials, 1):
            relative = f"attempts/{index:03d}"
            context = TrialContext(run_id, spec, self.output / relative, relative)
            worker = None
            try:
                self.evaluator.verify_inputs()
                if self.options.document is not None:
                    self.options.document.verify()
                if _source_digest(_source_files()[1]) != source_digest:
                    raise BenchmarkExecutionError("evaluation-frozen-input-drift")
                remaining = (
                    None
                    if self.options.timeout_seconds is None
                    else self.options.timeout_seconds - (time.monotonic() - started)
                )
                if remaining is not None and remaining <= 0:
                    raise TimeoutError
                worker = asyncio.create_task(self.evaluator.execute(context))
                async with asyncio.timeout(remaining):
                    result = await asyncio.shield(worker)
                if result.spec != spec:
                    raise BenchmarkExecutionError("evaluation-evidence-mismatch")
                refs = _evidence_files(self.output, context.directory)
                results.append(replace(result, evidence=refs))
                self.evaluator.verify_inputs()
                if _source_digest(_source_files()[1]) != source_digest:
                    raise BenchmarkExecutionError("evaluation-frozen-input-drift")
                if result.convergence is not ConvergenceStatus.CONVERGED:
                    raise BenchmarkExecutionError("evaluation-cleanup-unproven")
            except BaseException as error:
                primary = error
                if worker is not None and not worker.done():
                    worker.cancel()
                    await await_worker_convergence(worker)
                if worker is not None and not worker.cancelled():
                    if worker.exception() is not None and worker.exception() is not primary:
                        primary = combine_failures(
                            primary, worker.exception(), "evaluation shutdown failed"
                        )
                errors = _errors(primary)
                if len(results) < index:
                    cancelled = isinstance(error, (asyncio.CancelledError, TimeoutError))
                    converged = worker is None or (cancelled and worker.cancelled())
                    refs = ()
                    try:
                        if context.directory.exists():
                            refs = _evidence_files(self.output, context.directory)
                    except Exception as evidence_error:
                        primary = combine_failures(
                            primary, evidence_error, "evaluation evidence failed"
                        )
                        errors = _errors(primary)
                    results.append(
                        TrialResult(
                            spec,
                            ExecutionStatus.NOT_STARTED
                            if worker is None
                            else (
                                ExecutionStatus.CANCELLED if cancelled else ExecutionStatus.FAILED
                            ),
                            errors[0],
                            AssessmentStatus.UNASSESSABLE,
                            CheckStatus.UNPROVEN,
                            ConvergenceStatus.CONVERGED if converged else ConvergenceStatus.UNKNOWN,
                            False,
                            None,
                            evidence=refs,
                            errors=errors,
                        )
                    )
                break
        for spec in self.trials[len(results) :]:
            results.append(
                TrialResult(
                    spec,
                    ExecutionStatus.NOT_STARTED,
                    "evaluation-stopped",
                    AssessmentStatus.UNASSESSABLE,
                    CheckStatus.UNPROVEN,
                    ConvergenceStatus.CONVERGED,
                    False,
                    None,
                )
            )
        report = EvaluationReport(
            run_id,
            self.manifest.benchmark_id,
            self.manifest.task_type.value,
            fingerprint(frozen),
            tuple(results),
            self.evaluator.summarize(tuple(results)),
            errors,
        )
        try:
            _write_reports(self.output, report)
        except BaseException as report_error:
            raise combine_failures(primary, report_error, "evaluation report failed") from None
        if primary is not None and not isinstance(
            primary, (TimeoutError, BenchmarkExecutionError, BenchmarkManifestError)
        ):
            raise primary
        return report
