"""The one benchmark runner ``traceh eval`` drives.

There is exactly one benchmark path in this repository.  A second one would mean
two definitions of "did this work", and the definition nobody looks at is the one
that rots.  So this runner does not re-implement anything the product mainline
already owns: it assembles the same Product host ``traceh chat --product-config``
assembles, drives the same control plane, and reads every number back out of the
fact sources that already own it.

What the runner itself owns is small and deliberate:

* the grid - tasks x arms x repetitions - and the order it is walked in;
* one throwaway source repository and one one-shot bare target per attempt;
* one monotonic clock, because no durable fact records when a host decided;
* the coherence checks that make two arms comparable rather than merely adjacent.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from traceh.api.llm import LlmProvider
from traceh.evaluation.attempt import AttemptRequest, run_attempt
from traceh.evaluation.errors import BenchmarkExecutionError, EvaluationError
from traceh.evaluation.manifest import (
    BENCHMARK_PROTOCOL_VERSION,
    BenchmarkManifest,
    BenchmarkTask,
    load_benchmark_manifest,
)
from traceh.evaluation.report import (
    AttemptReport,
    BenchmarkReport,
    build_task_conditions,
    render_markdown,
)
from traceh.llm.retry import NO_MODEL_RETRY, ModelRetryPolicy

REPORT_JSON = "report.json"
REPORT_MARKDOWN = "report.md"
ATTEMPTS_DIRECTORY = "attempts"


class ProductBenchmarkRunner:
    """Run one manifest and write one report.

    ``providers`` is supplied by the composition root, exactly as the Product
    Chat host requires: the Profile names a provider id and a model id, and the
    host refuses any object whose ``name`` does not match.  The runner never
    constructs a model client of its own.
    """

    __slots__ = ("_manifest", "_monotonic", "_output_dir", "_providers", "_retry_policy")

    def __init__(
        self,
        benchmark_dir: Path,
        output_dir: Path,
        *,
        provider: LlmProvider,
        model_id: str,
        retry_policy: ModelRetryPolicy = NO_MODEL_RETRY,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        provider_id = getattr(provider, "name", None)
        if type(provider_id) is not str or not provider_id:
            # The Product host identifies a provider by name; an object without
            # one could never be matched to the Profile it is supposed to serve.
            raise BenchmarkExecutionError("benchmark-provider-binding-missing")
        self._manifest = load_benchmark_manifest(
            Path(benchmark_dir), provider_id=provider_id, model_id=model_id
        )
        self._output_dir = Path(output_dir).absolute()
        self._providers: Mapping[str, LlmProvider] = {provider_id: provider}
        self._monotonic = monotonic
        self._retry_policy = retry_policy

    @property
    def manifest(self) -> BenchmarkManifest:
        return self._manifest

    async def run(self) -> BenchmarkReport:
        """Walk the grid, write both outputs and return the report."""

        self._output_dir.mkdir(parents=True, exist_ok=False)
        attempts: list[AttemptReport] = []
        index = 0
        for task in self._manifest.tasks:
            for arm in self._manifest.arms:
                for repetition in range(1, arm.repetitions + 1):
                    index += 1
                    attempts.append(
                        await self._attempt(
                            task, arm.requested_mode, repetition, index
                        )
                    )
        report = BenchmarkReport(
            benchmark_id=self._manifest.benchmark_id,
            protocol_version=BENCHMARK_PROTOCOL_VERSION,
            profile_id=self._manifest.settings.host_profile.profile_id,
            provider_id=self._manifest.settings.host_profile.profile.provider_id,
            model_id=self._manifest.settings.host_profile.profile.model_id,
            attempts=tuple(attempts),
            tasks=tuple(
                build_task_conditions(
                    task.task_id,
                    [
                        attempt
                        for attempt in attempts
                        if attempt.benchmark_task_id == task.task_id
                    ],
                    verifier_definition_digest=(
                        self._manifest.verifier_definition_digest
                    ),
                )
                for task in self._manifest.tasks
            ),
            retry_policy=self._retry_policy,
        )
        self._write(report)
        return report

    async def _attempt(
        self, task: BenchmarkTask, requested_mode, repetition: int, index: int
    ) -> AttemptReport:
        relative = f"{ATTEMPTS_DIRECTORY}/{index:03d}"
        request = AttemptRequest(
            attempt_id=f"{task.task_id}/{requested_mode.value}/{repetition}",
            task=task,
            requested_mode=requested_mode,
            repetition=repetition,
            directory=self._output_dir / ATTEMPTS_DIRECTORY / f"{index:03d}",
            relative_directory=relative,
        )
        try:
            return await run_attempt(
                request,
                manifest=self._manifest,
                providers=self._providers,
                retry_policy=self._retry_policy,
                monotonic=self._monotonic,
            )
        except (EvaluationError, OSError) as error:
            # An attempt that cannot be measured is reported as unmeasured, not
            # dropped and not folded in as a zero. The run continues so the rest
            # of the grid still produces evidence, and the report stays
            # incomplete so the exit code still says the measurement failed.
            #
            # ``OSError`` belongs here for a specific, observed reason: on
            # Windows a long output directory plus a derived stream file name can
            # exceed the path limit, and the honest answer is one unmeasured
            # attempt rather than a traceback that destroys the whole run's
            # evidence.
            code = getattr(error, "code", None)
            return AttemptReport(
                attempt_id=request.attempt_id,
                benchmark_task_id=task.task_id,
                requested_mode=requested_mode,
                repetition=repetition,
                directory=relative,
                error_code=code if type(code) is str else "benchmark-attempt-unreadable",
                evidence=None,
                timing=None,
            )

    def _write(self, report: BenchmarkReport) -> None:
        (self._output_dir / REPORT_JSON).write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        (self._output_dir / REPORT_MARKDOWN).write_text(
            render_markdown(report), encoding="utf-8"
        )


__all__ = [
    "ATTEMPTS_DIRECTORY",
    "REPORT_JSON",
    "REPORT_MARKDOWN",
    "ProductBenchmarkRunner",
]
