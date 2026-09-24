"""Product execution and success stay on the existing Product attempt owner."""

from __future__ import annotations

from traceh.api.json_types import fingerprint, to_json_value
from traceh.api.product import RequestedTaskMode
from traceh.evaluation.attempt import AttemptRequest, run_attempt
from traceh.evaluation.contracts import (
    AssessmentStatus,
    CheckStatus,
    ConvergenceStatus,
    ExecutionStatus,
    TrialContext,
    TrialResult,
    TrialSpec,
)
from traceh.evaluation.errors import BenchmarkManifestError, EvaluationError
from traceh.evaluation.evaluators.product_manifest import load_product_suite
from traceh.evaluation.evaluators.product_report import (
    AttemptReport,
    BenchmarkReport,
    build_task_conditions,
)
from traceh.evaluation.manifest import BENCHMARK_PROTOCOL_VERSION
from traceh.evaluation.repositories import capture_initial_tree, initial_tree_digest
from traceh.evaluation.retrieval import unavailable_retrieval


class ProductTaskEvaluator:
    def __init__(self, manifest, *, provider, model_id, retry_policy, sandbox, monotonic):
        self.manifest = manifest
        self.suite = load_product_suite(manifest, provider_id=provider.name, model_id=model_id)
        self.providers = {provider.name: provider}
        self.retry_policy = retry_policy
        self.sandbox = sandbox
        self.monotonic = monotonic
        self.verify_inputs()

    def verify_inputs(self):
        self.manifest.verify()
        for task in self.suite.tasks:
            captured = capture_initial_tree(task.initial_dir, limits=task.initial_tree_limits)
            if initial_tree_digest(captured) != task.material_digest:
                raise BenchmarkManifestError("evaluation-frozen-input-drift", "initial_tree")
        if self.suite.retrieval is not None:
            self.suite.retrieval.verify()
        if self.suite.rubric is not None:
            self.suite.rubric.verify()

    def frozen_settings(self):
        return {
            "task_settings": self.manifest.task_settings,
            "assessment": self.manifest.assessment,
            "verification_plans": {task.task_id: task.verifier_definition_digest
                                   for task in self.suite.tasks},
            "retrieval": None if self.suite.retrieval is None else self.suite.retrieval.file_digest,
        }

    def frozen_materials(self) -> tuple[tuple[str, bytes], ...]:
        """Only explicitly declared host inputs; never copy an arbitrary directory."""
        root = self.manifest.directory.resolve()
        files = {
            self.manifest.document.relative: self.manifest.document.content,
            self.manifest.dataset.relative: self.manifest.dataset.content,
        }
        if self.suite.rubric is not None:
            rubric = self.suite.rubric
            rubric.verify()
            files[rubric.relative] = rubric.content
        for task in self.suite.tasks:
            captured = capture_initial_tree(task.initial_dir, limits=task.initial_tree_limits)
            if initial_tree_digest(captured) != task.material_digest:
                raise BenchmarkManifestError("evaluation-frozen-input-drift", "initial_tree")
            for name, data in captured:
                files[(task.initial_dir / name).resolve().relative_to(root).as_posix()] = data
        if self.suite.retrieval is not None:
            spec = self.suite.retrieval
            data = spec.path.read_bytes()
            from traceh.evaluation.inputs import digest_bytes

            if digest_bytes(data) != spec.file_digest:
                raise BenchmarkManifestError("evaluation-frozen-input-drift", "retrieval")
            files[spec.path.relative_to(root).as_posix()] = data
        return tuple(sorted(files.items()))

    def trials(self, variant_id, repetitions):
        return tuple(
            TrialSpec(
                fingerprint([variant_id, task.task_id, mode.value, repeat]),
                task.task_id,
                task.group_id,
                task.material_digest,
                None,
                repeat,
                mode.value,
                variant_id,
            )
            for task in self.suite.tasks
            for mode in self.suite.modes
            for repeat in range(1, repetitions + 1)
        )

    async def execute(self, context: TrialContext) -> TrialResult:
        spec = context.spec
        task = next(task for task in self.suite.tasks if task.task_id == spec.case_id)
        request = AttemptRequest(
            attempt_id=spec.trial_id,
            task=task,
            requested_mode=RequestedTaskMode(spec.requested_mode),
            repetition=spec.replicate,
            directory=context.directory,
            relative_directory=context.relative_directory,
        )
        uncertain_cleanup = False
        try:
            result = await run_attempt(
                request,
                manifest=self.suite,
                providers=self.providers,
                retry_policy=self.retry_policy,
                sandbox=self.sandbox,
                monotonic=self.monotonic,
            )
        except (EvaluationError, OSError) as error:
            uncertain_cleanup = (
                isinstance(error, OSError)
                or getattr(error, "code", None) == "evaluation-cleanup-unproven"
            )
            result = AttemptReport(
                spec.trial_id,
                spec.case_id,
                request.requested_mode,
                spec.replicate,
                context.relative_directory,
                getattr(error, "code", "benchmark-attempt-unreadable"),
                None,
                None,
                None
                if self.suite.retrieval is None
                else unavailable_retrieval(self.suite.retrieval, spec.case_id),
            )
        evidence = result.evidence
        converged = not uncertain_cleanup and (
            evidence is None or (evidence.workspaces.live == 0 and evidence.budget.converged)
        )
        return TrialResult(
            spec,
            ExecutionStatus.COMPLETED if result.success else ExecutionStatus.FAILED,
            result.error_code
            or (None if evidence is None else evidence.failure_code or evidence.reason_code),
            (
                AssessmentStatus.UNASSESSABLE
                if evidence is None
                else AssessmentStatus.PENDING_REVIEW
                if result.success and self.suite.rubric is not None
                else AssessmentStatus.PASSED
                if result.success
                else AssessmentStatus.FAILED
            ),
            CheckStatus.PASSED if evidence is not None else CheckStatus.UNPROVEN,
            ConvergenceStatus.CONVERGED if converged else ConvergenceStatus.UNKNOWN,
            result.measured,
            result,
            usage=tuple(
                (name, to_json_value(group.tokens) if group is not None else None)
                for name, group in (
                    ()
                    if evidence is None
                    else (
                        ("execution", evidence.execution),
                        ("unattributed", evidence.unattributed),
                    )
                )
            ),
        )

    def summarize(self, results):
        attempts = tuple(result.task_result for result in results if result.task_result is not None)
        return BenchmarkReport(
            self.suite.benchmark_id,
            BENCHMARK_PROTOCOL_VERSION,
            self.suite.profile_id,
            self.suite.provider_id,
            self.suite.model_id,
            attempts,
            tuple(
                build_task_conditions(
                    task.task_id,
                    [a for a in attempts if a.benchmark_task_id == task.task_id],
                    verifier_definition_digest=task.verifier_definition_digest,
                )
                for task in self.suite.tasks
            ),
            self.retry_policy,
        )
