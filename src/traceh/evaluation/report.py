"""Common result envelope. Task-specific success and statistics remain typed."""

from dataclasses import dataclass

from traceh.api.json_types import to_json_value
from traceh.evaluation.contracts import AssessmentStatus, ConvergenceStatus, TaskReport, TrialResult
from traceh.evaluation.manifest import BENCHMARK_PROTOCOL_VERSION


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    run_id: str
    benchmark_id: str
    task_type: str
    frozen_digest: str
    trials: tuple[TrialResult, ...]
    task_report: TaskReport
    errors: tuple[str, ...] = ()

    @property
    def complete(self):
        return (
            bool(self.trials)
            and not self.errors
            and self.task_report.complete
            and all(
                t.measured and t.convergence is ConvergenceStatus.CONVERGED for t in self.trials
            )
        )

    @property
    def assessment_complete(self):
        return all(
            t.assessment not in {AssessmentStatus.PENDING_REVIEW, AssessmentStatus.UNASSESSABLE}
            for t in self.trials
        )

    def to_dict(self):
        return {
            "protocol_version": BENCHMARK_PROTOCOL_VERSION,
            "run_id": self.run_id,
            "benchmark_id": self.benchmark_id,
            "task_type": self.task_type,
            "frozen_digest": self.frozen_digest,
            "complete": self.complete,
            "assessment_complete": self.assessment_complete,
            "errors": list(self.errors),
            "trials": [
                {
                    "identity": to_json_value(t.spec),
                    "execution": {"status": t.execution.value, "reason": t.reason},
                    "assessment": {"status": t.assessment.value},
                    "invariants": t.invariants.value,
                    "convergence": t.convergence.value,
                    "measured": t.measured,
                    "evidence": to_json_value(t.evidence),
                    "usage": dict(t.usage),
                    "errors": list(t.errors),
                }
                for t in self.trials
            ],
            "task_report": self.task_report.to_dict(),
        }


def render_markdown(report):
    data = report.to_dict()
    lines = [
        f"# Evaluation: {data['benchmark_id']}",
        "",
        f"Run: {data['run_id']}",
        f"Task type: {data['task_type']}",
        f"Measurement complete: {data['complete']}",
        f"Assessment complete: {data['assessment_complete']}",
        "",
        "| Trial | Execution | Assessment | Convergence |",
        "|---|---|---|---|",
    ]
    lines.extend(
        f"| {t['identity']['trial_id']} | {t['execution']['status']} | "
        f"{t['assessment']['status']} | {t['convergence']} |"
        for t in data["trials"]
    )
    return "\n".join(lines) + "\n\n" + report.task_report.markdown()
