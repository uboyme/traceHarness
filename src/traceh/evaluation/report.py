"""The benchmark report: descriptive, decomposable and honest about n.

Three rules shape this module.

**``auto`` is not a third quality arm.**  A run whose Router chose ``multi`` is a
``multi`` result that also paid a routing cost.  Quality aggregates are therefore
keyed by *resolved* mode and contain every attempt that resolved to it, while the
routing cost of ``auto`` attempts is reported separately.  Comparing three arms
would compare ``multi`` against itself.

**Small n stays small.**  Aggregates are counts, totals, minima, maxima and a
mean.  There is no variance, no confidence interval and no significance claim,
and an arm with one observation says so in both outputs.

**Unavailable is a value.**  A metric the durable facts could not support is
counted as unavailable rather than folded in as zero, so a mean can never be
quietly dragged down by measurements that did not happen.

Markdown is rendered from :meth:`BenchmarkReport.to_dict`, not from the objects,
so the two outputs cannot disagree about a number.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from traceh.api.json_types import JsonValue
from traceh.api.product import RequestedTaskMode, ResolvedTaskMode
from traceh.evaluation.metrics import AttemptEvidence, SessionWork, TokenTotals

APPROVAL_POLICY = "programmatic-immediate"
"""The benchmark host approves its own one-shot local target, immediately.

Labelled everywhere it appears.  It exists so ``active elapsed`` measures work
rather than how long a person was away from the keyboard, and it grants no
authority outside this harness: ordinary Chat still requires a human ``/task
approve``.
"""

REPORT_NOTES = (
    "auto is not a third quality arm: each auto attempt is counted in the arm "
    "its Router resolved to, and its routing cost is reported separately.",
    f"approval policy is {APPROVAL_POLICY}: the benchmark host approves its own "
    "one-shot local bare target as soon as the Approval barrier is reached.",
    "approval wait is measured separately and excluded from active elapsed; "
    "wall elapsed includes it.",
    "aggregates are descriptive only. No statistical significance is claimed.",
    "success requires a ProductTask terminal, a Workflow terminal, a passed "
    "Review and a Promotion receipt whose new revision is what the target ref "
    "actually points at.",
    "the frozen verifier proves the declared checks passed on the reviewed "
    "bytes. It does not prove a candidate left those checks as strong as it "
    "found them.",
)


@dataclass(frozen=True, slots=True)
class PhaseTiming:
    """Host-owned monotonic intervals for one attempt.

    These are the only numbers in the report that are not durable facts, because
    no durable fact records when a host decided.  They come from one monotonic
    clock the runner owns, and ``active`` is defined by subtraction so the three
    can never disagree.
    """

    wall_ms: int
    approval_wait_ms: int

    @property
    def active_ms(self) -> int:
        return self.wall_ms - self.approval_wait_ms


@dataclass(frozen=True, slots=True)
class NumericSummary:
    """A descriptive summary that keeps missing measurements visible."""

    values: tuple[int, ...]
    unavailable: int

    @property
    def observations(self) -> int:
        return len(self.values)

    @property
    def total(self) -> int:
        return sum(self.values)

    @property
    def minimum(self) -> int | None:
        return min(self.values) if self.values else None

    @property
    def maximum(self) -> int | None:
        return max(self.values) if self.values else None

    @property
    def mean(self) -> float | None:
        if not self.values:
            return None
        return round(self.total / len(self.values), 2)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "observations": self.observations,
            "unavailable": self.unavailable,
            "values": list(self.values),
            "total": self.total,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "mean": self.mean,
        }


def summarize(values: Sequence[int | None]) -> NumericSummary:
    present = tuple(value for value in values if value is not None)
    return NumericSummary(present, len(values) - len(present))


@dataclass(frozen=True, slots=True)
class AttemptReport:
    """One attempt, its evidence and its host-clock phases."""

    attempt_id: str
    benchmark_task_id: str
    requested_mode: RequestedTaskMode
    repetition: int
    directory: str
    error_code: str | None
    evidence: AttemptEvidence | None
    timing: PhaseTiming | None

    @property
    def measured(self) -> bool:
        return self.evidence is not None

    @property
    def success(self) -> bool:
        return self.evidence is not None and self.evidence.success

    @property
    def resolved_mode(self) -> ResolvedTaskMode | None:
        return None if self.evidence is None else self.evidence.resolved_mode

    def to_dict(self) -> dict[str, JsonValue]:
        evidence = self.evidence
        timing = self.timing
        result: dict[str, JsonValue] = {
            "attempt_id": self.attempt_id,
            "benchmark_task_id": self.benchmark_task_id,
            "requested_mode": self.requested_mode.value,
            "repetition": self.repetition,
            # Relative on purpose: an evidence report should locate its own
            # attempt without writing the host's absolute paths into a file that
            # gets copied around.
            "directory": self.directory,
            "measured": self.measured,
            "error_code": self.error_code,
            "success": self.success,
        }
        result["timing"] = (
            None
            if timing is None
            else {
                "wall_ms": timing.wall_ms,
                "approval_wait_ms": timing.approval_wait_ms,
                "active_ms": timing.active_ms,
                "approval_policy": APPROVAL_POLICY,
            }
        )
        if evidence is None:
            result["evidence"] = None
            return result
        result["evidence"] = {
            "task_id": evidence.task_id,
            "product_status": evidence.product_status.value,
            "workflow_status": (
                None if evidence.workflow_status is None else evidence.workflow_status.value
            ),
            "mode_source": evidence.mode_source.value,
            "resolved_mode": (
                None if evidence.resolved_mode is None else evidence.resolved_mode.value
            ),
            "requirement_digest": evidence.requirement_digest,
            "profile_digest": evidence.profile_digest,
            "preflight_digest": evidence.preflight_digest,
            "source_base_revision": evidence.source_base_revision,
            "definition_hash": evidence.definition_hash,
            "routing": _session_dict(evidence.routing),
            "routing_parsed": evidence.routing_parsed,
            "execution": {
                "sessions": [
                    _session_dict(item) for item in evidence.execution.sessions
                ],
                "steps": evidence.execution.steps,
                "tool_calls": evidence.execution.tool_calls,
                "turns": evidence.execution.turns,
                "tokens": _tokens_dict(evidence.execution.tokens),
                "cumulative_work_duration_ms": evidence.execution.work_duration_ms,
            },
            # Owned Agents no durable Product or Workflow fact names - a Router
            # whose answer was rejected is the one that really happens. Their
            # cost is stated rather than folded into a total that would then
            # claim an attribution the streams do not support.
            "unattributed": {
                "sessions": [
                    _session_dict(item) for item in evidence.unattributed.sessions
                ],
                "tokens": _tokens_dict(evidence.unattributed.tokens),
            },
            "budget": {
                "accounts": evidence.budget.accounts,
                "accounts_closed": evidence.budget.accounts_closed,
                "child_reservations": evidence.budget.child_reservations,
                "child_reservations_terminal": (
                    evidence.budget.child_reservations_terminal
                ),
                "usage_reservations": evidence.budget.usage_reservations,
                "usage_reservations_terminal": (
                    evidence.budget.usage_reservations_terminal
                ),
                "settled_tokens": evidence.budget.settled_tokens,
                "charged_steps": evidence.budget.charged_steps,
                "charged_tool_calls": evidence.budget.charged_tool_calls,
                "converged": evidence.budget.converged,
            },
            "workspaces": {
                "workspaces": evidence.workspaces.workspaces,
                "released": evidence.workspaces.released,
                "quarantined": evidence.workspaces.quarantined,
                "live": evidence.workspaces.live,
                "converged": evidence.workspaces.converged,
            },
            "review_id": evidence.review_id,
            "review_passed": evidence.review_passed,
            "verifier_definition_digest": evidence.verifier_definition_digest,
            "promotion_id": evidence.promotion_id,
            "previous_revision": evidence.previous_revision,
            "new_revision": evidence.new_revision,
            "target_ref": evidence.target_ref,
            "target_revision": evidence.target_revision,
            "failure_code": evidence.failure_code,
            "reason_code": evidence.reason_code,
            "unavailable": list(evidence.unavailable),
        }
        return result


@dataclass(frozen=True, slots=True)
class QualityArm:
    """Every attempt that *resolved* to one mode, whatever was requested."""

    resolved_mode: ResolvedTaskMode
    attempts: tuple[AttemptReport, ...]

    @property
    def observations(self) -> int:
        return len(self.attempts)

    @property
    def single_observation(self) -> bool:
        return self.observations == 1

    @property
    def successes(self) -> int:
        return sum(1 for attempt in self.attempts if attempt.success)

    def to_dict(self) -> dict[str, JsonValue]:
        evidences = [
            attempt.evidence
            for attempt in self.attempts
            if attempt.evidence is not None
        ]
        timings = [attempt.timing for attempt in self.attempts]
        requested: dict[str, int] = {}
        for attempt in self.attempts:
            key = attempt.requested_mode.value
            requested[key] = requested.get(key, 0) + 1
        return {
            "resolved_mode": self.resolved_mode.value,
            "observations": self.observations,
            "single_observation": self.single_observation,
            "successes": self.successes,
            "requested_modes": dict(sorted(requested.items())),
            "execution_tokens": summarize(
                [
                    None if item.execution.tokens is None else item.execution.tokens.total_tokens
                    for item in evidences
                ]
            ).to_dict(),
            "ledger_settled_tokens": summarize(
                [item.budget.settled_tokens for item in evidences]
            ).to_dict(),
            "steps": summarize([item.execution.steps for item in evidences]).to_dict(),
            "tool_calls": summarize(
                [item.execution.tool_calls for item in evidences]
            ).to_dict(),
            "cumulative_work_duration_ms": summarize(
                [item.execution.work_duration_ms for item in evidences]
            ).to_dict(),
            "wall_ms": summarize(
                [None if item is None else item.wall_ms for item in timings]
            ).to_dict(),
            "approval_wait_ms": summarize(
                [None if item is None else item.approval_wait_ms for item in timings]
            ).to_dict(),
            "active_ms": summarize(
                [None if item is None else item.active_ms for item in timings]
            ).to_dict(),
        }


@dataclass(frozen=True, slots=True)
class RoutingArm:
    """What ``auto`` cost and what it decided - never a quality comparison."""

    attempts: tuple[AttemptReport, ...]

    @property
    def observations(self) -> int:
        return len(self.attempts)

    @property
    def parsed(self) -> int:
        return sum(
            1
            for attempt in self.attempts
            if attempt.evidence is not None and attempt.evidence.routing_parsed
        )

    def to_dict(self) -> dict[str, JsonValue]:
        evidences = [
            attempt.evidence
            for attempt in self.attempts
            if attempt.evidence is not None
        ]
        resolved: dict[str, int] = {}
        for item in evidences:
            key = "unresolved" if item.resolved_mode is None else item.resolved_mode.value
            resolved[key] = resolved.get(key, 0) + 1
        return {
            "requested_mode": RequestedTaskMode.AUTO.value,
            "observations": self.observations,
            "single_observation": self.observations == 1,
            "parsed": self.parsed,
            "resolved_modes": dict(sorted(resolved.items())),
            "routing_tokens": summarize(
                [
                    None
                    if item.routing is None or item.routing.tokens is None
                    else item.routing.tokens.total_tokens
                    for item in evidences
                ]
            ).to_dict(),
            "routing_elapsed_ms": summarize(
                [
                    None if item.routing is None else item.routing.work_duration_ms
                    for item in evidences
                ]
            ).to_dict(),
        }


@dataclass(frozen=True, slots=True)
class TaskConditions:
    """The experiment condition every arm of one task must have shared."""

    benchmark_task_id: str
    requirement_digest: str | None
    profile_digest: str | None
    source_base_revision: str | None
    verifier_definition_digest: str
    divergent_fields: tuple[str, ...]
    unproven_fields: tuple[str, ...] = ()

    @property
    def coherent(self) -> bool:
        """No arm contradicted another about a condition it did establish.

        ``unproven_fields`` is reported beside this rather than folded into it:
        an attempt that failed before it started legitimately never established a
        source revision, and calling the whole task incoherent for that would
        report a normal failure as a broken experiment. The reader is told which
        columns were not proved everywhere.
        """

        return not self.divergent_fields

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "benchmark_task_id": self.benchmark_task_id,
            "requirement_digest": self.requirement_digest,
            "profile_digest": self.profile_digest,
            "source_base_revision": self.source_base_revision,
            "verifier_definition_digest": self.verifier_definition_digest,
            "coherent": self.coherent,
            "divergent_fields": list(self.divergent_fields),
            "unproven_fields": list(self.unproven_fields),
        }


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    benchmark_id: str
    protocol_version: int
    profile_id: str
    provider_id: str
    model_id: str
    attempts: tuple[AttemptReport, ...]
    tasks: tuple[TaskConditions, ...]

    @property
    def quality_arms(self) -> tuple[QualityArm, ...]:
        arms = []
        for mode in ResolvedTaskMode:
            selected = tuple(
                attempt for attempt in self.attempts if attempt.resolved_mode is mode
            )
            if selected:
                arms.append(QualityArm(mode, selected))
        return tuple(arms)

    @property
    def routing_arm(self) -> RoutingArm | None:
        selected = tuple(
            attempt
            for attempt in self.attempts
            if attempt.requested_mode is RequestedTaskMode.AUTO
        )
        return RoutingArm(selected) if selected else None

    @property
    def measured(self) -> int:
        return sum(1 for attempt in self.attempts if attempt.measured)

    @property
    def attempts_with_unavailable_metrics(self) -> int:
        return sum(
            1
            for attempt in self.attempts
            if attempt.evidence is not None and attempt.evidence.unavailable
        )

    @property
    def complete(self) -> bool:
        """Whether every attempt could be measured and every task stayed coherent.

        Deliberately *not* the same as "every metric was available". An attempt
        can be fully measured - success, steps, phases, Budget outcome - while one
        provider refused to report trustworthy token counts. That is a gap in one
        column, reported as such, not a failed measurement of the run.
        """

        return (
            bool(self.attempts)
            and self.measured == len(self.attempts)
            and all(task.coherent for task in self.tasks)
        )

    def to_dict(self) -> dict[str, JsonValue]:
        routing = self.routing_arm
        return {
            "benchmark_id": self.benchmark_id,
            "protocol_version": self.protocol_version,
            "profile_id": self.profile_id,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "approval_policy": APPROVAL_POLICY,
            "attempts_run": len(self.attempts),
            "attempts_measured": self.measured,
            "attempts_with_unavailable_metrics": self.attempts_with_unavailable_metrics,
            "complete": self.complete,
            "notes": list(REPORT_NOTES),
            "tasks": [task.to_dict() for task in self.tasks],
            "quality_arms": [arm.to_dict() for arm in self.quality_arms],
            "routing_arm": None if routing is None else routing.to_dict(),
            "attempts": [attempt.to_dict() for attempt in self.attempts],
        }


def render_markdown(report: BenchmarkReport) -> str:
    """Render the same values the JSON carries, read from the same dictionary."""

    data = report.to_dict()
    lines = [
        f"# Benchmark: {data['benchmark_id']}",
        "",
        f"- protocol version: {data['protocol_version']}",
        f"- profile: {data['profile_id']}",
        f"- provider/model: {data['provider_id']} / {data['model_id']}",
        f"- approval policy: {data['approval_policy']}",
        f"- attempts run/measured: {data['attempts_run']} / {data['attempts_measured']}",
        "- attempts with unavailable metrics: "
        f"{data['attempts_with_unavailable_metrics']}",
        f"- complete: {str(data['complete']).lower()}",
        "",
        "## Notes",
        "",
    ]
    for note in data["notes"]:
        lines.append(f"- {note}")
    lines.extend(("", "## Experiment conditions", ""))
    lines.append(
        "| task | coherent | requirement digest | profile digest | "
        "source base revision | verifier digest |"
    )
    lines.append("|---|---|---|---|---|---|")
    for task in data["tasks"]:
        lines.append(
            f"| {task['benchmark_task_id']} | {str(task['coherent']).lower()} | "
            f"{_short(task['requirement_digest'])} | {_short(task['profile_digest'])} | "
            f"{_short(task['source_base_revision'])} | "
            f"{_short(task['verifier_definition_digest'])} |"
        )
        if task["divergent_fields"]:
            lines.append(
                f"| {task['benchmark_task_id']} divergent | "
                f"{', '.join(task['divergent_fields'])} | | | | |"
            )
        if task["unproven_fields"]:
            lines.append(
                f"| {task['benchmark_task_id']} unproven | "
                f"{', '.join(task['unproven_fields'])} | | | | |"
            )
    lines.extend(("", "## Quality arms (by resolved mode)", ""))
    for arm in data["quality_arms"]:
        lines.append(f"### {arm['resolved_mode']}")
        lines.append("")
        lines.append(
            f"- observations: {arm['observations']}"
            + (" (single observation)" if arm["single_observation"] else "")
        )
        lines.append(f"- successes: {arm['successes']}")
        lines.append(
            "- requested modes: "
            + ", ".join(
                f"{name}={count}" for name, count in arm["requested_modes"].items()
            )
        )
        for label in (
            "execution_tokens",
            "ledger_settled_tokens",
            "steps",
            "tool_calls",
            "cumulative_work_duration_ms",
            "active_ms",
            "approval_wait_ms",
            "wall_ms",
        ):
            lines.append(f"- {label}: {_summary(arm[label])}")
        lines.append("")
    routing = data["routing_arm"]
    lines.extend(("## Routing (auto only, not a quality arm)", ""))
    if routing is None:
        lines.append("- no auto attempt was requested")
    else:
        lines.append(
            f"- observations: {routing['observations']}"
            + (" (single observation)" if routing["single_observation"] else "")
        )
        lines.append(f"- strictly parsed: {routing['parsed']}")
        lines.append(
            "- resolved: "
            + ", ".join(
                f"{name}={count}" for name, count in routing["resolved_modes"].items()
            )
        )
        lines.append(f"- routing_tokens: {_summary(routing['routing_tokens'])}")
        lines.append(
            f"- routing_elapsed_ms: {_summary(routing['routing_elapsed_ms'])}"
        )
    lines.extend(("", "## Attempts", ""))
    lines.append(
        "| attempt | directory | requested | resolved | success | measured | "
        "active_ms | approval_wait_ms | wall_ms | promotion |"
    )
    lines.append("|---|---|---|---|---|---|---:|---:|---:|---|")
    for attempt in data["attempts"]:
        evidence = attempt["evidence"]
        timing = attempt["timing"]
        resolved = "-" if evidence is None else _text(evidence["resolved_mode"])
        promotion = "-" if evidence is None else _text(evidence["promotion_id"])
        lines.append(
            f"| {attempt['attempt_id']} | {attempt['directory']} | "
            f"{attempt['requested_mode']} | {resolved} | "
            f"{str(attempt['success']).lower()} | "
            f"{str(attempt['measured']).lower()} | "
            f"{_number(timing, 'active_ms')} | "
            f"{_number(timing, 'approval_wait_ms')} | "
            f"{_number(timing, 'wall_ms')} | {promotion} |"
        )
    unavailable = [
        (attempt["attempt_id"], attempt["evidence"]["unavailable"])
        for attempt in data["attempts"]
        if attempt["evidence"] is not None and attempt["evidence"]["unavailable"]
    ]
    failures = [
        (attempt["attempt_id"], attempt["error_code"])
        for attempt in data["attempts"]
        if attempt["error_code"] is not None
    ]
    lines.extend(("", "## Unavailable measurements", ""))
    if not unavailable and not failures:
        lines.append("- none")
    for attempt_id, labels in unavailable:
        lines.append(f"- {attempt_id}: {', '.join(labels)}")
    for attempt_id, code in failures:
        lines.append(f"- {attempt_id}: attempt error {code}")
    return "\n".join(lines) + "\n"


def _summary(value: dict[str, JsonValue]) -> str:
    return (
        f"n={value['observations']} total={value['total']} "
        f"min={_text(value['minimum'])} max={_text(value['maximum'])} "
        f"mean={_text(value['mean'])} unavailable={value['unavailable']}"
    )


def _number(timing: object, key: str) -> str:
    if type(timing) is not dict:
        return "-"
    return _text(timing.get(key))


def _text(value: object) -> str:
    return "-" if value is None else str(value)


def _short(value: object) -> str:
    if value is None:
        return "-"
    text = str(value)
    return text if len(text) <= 16 else f"{text[:16]}..."


def _session_dict(work: SessionWork | None) -> JsonValue:
    if work is None:
        return None
    return {
        "session_id": work.session_id,
        "agent_id": work.agent_id,
        "turns": work.turns,
        "steps": work.steps,
        "tool_calls": work.tool_calls,
        "tokens": _tokens_dict(work.tokens),
        "work_duration_ms": work.work_duration_ms,
    }


def _tokens_dict(tokens: TokenTotals | None) -> JsonValue:
    if tokens is None:
        return None
    return {
        "input_tokens": tokens.input_tokens,
        "output_tokens": tokens.output_tokens,
        "total_tokens": tokens.total_tokens,
        "quality": tokens.quality,
    }


def build_task_conditions(
    benchmark_task_id: str,
    attempts: Sequence[AttemptReport],
    *,
    verifier_definition_digest: str,
) -> TaskConditions:
    """Prove, rather than assume, that every arm ran the same experiment.

    ``single`` and ``multi`` are only comparable when the requirement, the
    Profile, the source revision and the frozen verifier were identical.

    The verifier is proved from the **host-frozen manifest**, not from whichever
    attempts happened to reach a Review. Inferring it from survivors is how a
    task where ``single`` succeeded and ``multi`` failed early ended up claiming
    a shared verifier that only one arm ever demonstrated.

    The durable per-attempt digests are compared only among attempts that
    actually established them, and every attempt that did not is counted in
    ``unproven``. Dropping those silently is the same mistake in a different
    column: an absent value is not agreement.
    """

    evidences = [
        attempt.evidence for attempt in attempts if attempt.evidence is not None
    ]
    total = len(attempts)
    fields: dict[str, list[str | None]] = {
        "requirement_digest": [item.requirement_digest for item in evidences],
        "profile_digest": [item.profile_digest for item in evidences],
        "source_base_revision": [item.source_base_revision for item in evidences],
        # Compared *against the frozen plan*, so an arm that never reached a
        # Review cannot make a mismatch invisible by simply being absent.
        "verifier_definition_digest": [
            item.verifier_definition_digest for item in evidences
        ],
    }
    divergent: list[str] = []
    unproven: list[str] = []
    for name, values in fields.items():
        present = [value for value in values if value is not None]
        missing = total - len(present)
        if name == "verifier_definition_digest":
            if any(value != verifier_definition_digest for value in present):
                divergent.append(name)
        elif len(set(present)) > 1:
            divergent.append(name)
        if missing:
            unproven.append(name)
    return TaskConditions(
        benchmark_task_id=benchmark_task_id,
        requirement_digest=_only(fields["requirement_digest"]),
        profile_digest=_only(fields["profile_digest"]),
        source_base_revision=_only(fields["source_base_revision"]),
        verifier_definition_digest=verifier_definition_digest,
        divergent_fields=tuple(sorted(divergent)),
        unproven_fields=tuple(sorted(unproven)),
    )


def _only(values: Sequence[str | None]) -> str | None:
    unique = {value for value in values if value is not None}
    return unique.pop() if len(unique) == 1 else None


__all__ = [
    "APPROVAL_POLICY",
    "REPORT_NOTES",
    "AttemptReport",
    "BenchmarkReport",
    "NumericSummary",
    "PhaseTiming",
    "QualityArm",
    "RoutingArm",
    "TaskConditions",
    "build_task_conditions",
    "render_markdown",
    "summarize",
]
