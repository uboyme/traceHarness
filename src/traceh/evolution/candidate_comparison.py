"""L3 baseline/candidate comparison over a trusted, repository-owned suite.

L3 consumes an exact L2 evidence bundle.  It never rebuilds the candidate and
does not install anything into the host interpreter.  Both arms install the
same trusted core Wheel and candidate Wheel; the baseline leaves the plugin
disabled while the candidate arm enables the L2 plugin identity explicitly.

This is a development control plane, not Agent state and not an approval
system.  A completed report classifies observed differences but cannot promote
or install the candidate.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import InvalidWheelFilename, canonicalize_name, parse_wheel_filename

from traceh.evolution.artifacts import (
    apply_wheelhouse_environment,
    audit_candidate_wheel,
    copy_candidate_source,
    sha256_file,
    transient_wheel_members,
)
from traceh.evolution.candidate_validation import (
    VALIDATION_CHECK_ORDER,
    CommandRequest,
    CommandRunner,
    SubprocessCommandRunner,
)
from traceh.plugins import is_plugin_id
from traceh.tools.builtins.shell import sanitized_environment

COMPARISON_SCHEMA_VERSION = 1
COMPARISON_EXIT_CODE = 9
MAX_REPORT_BYTES = 1_000_000
MAX_RESULT_BYTES = 1_000_000
MAX_CASES = 20
MAX_SUITE_FILES = 1_000
MAX_SUITE_BYTES = 20 * 1024 * 1024
MAX_DEPENDENCY_WHEELS = 1_000
MAX_DEPENDENCY_BYTES = 2 * 1024 * 1024 * 1024
MAX_INSTALLED_DISTRIBUTIONS = 1_000
MAX_METADATA_BYTES = 1_000_000
_HEX_COMMIT = re.compile(r"[0-9a-fA-F]{40,64}\Z")
_HEX_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_CASE_ID = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?\Z")
_SUITE_ID = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")


class CandidateComparisonConfigurationError(ValueError):
    """Safe host-authored configuration failure."""


class CandidateComparisonEvidenceError(ValueError):
    """Stable failure while reading L2 or task-suite evidence."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class CandidateComparisonConfig:
    validation_evidence: Path
    core_project: Path
    suite: Path
    output: Path
    wheelhouse: Path | None = None
    allow_index: bool = False
    test_requirements: tuple[str, ...] = ()
    command_timeout_seconds: float = 600.0


@dataclass(frozen=True, slots=True)
class ComparisonCandidate:
    distribution: str
    version: str
    plugin_id: str
    entry_value: str
    entry_module: str


@dataclass(frozen=True, slots=True)
class ComparisonArtifact:
    filename: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class DependencyArtifact:
    filename: str
    distribution: str
    version: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class InstalledDistribution:
    name: str
    version: str


@dataclass(frozen=True, slots=True)
class ComparisonCheck:
    name: str
    code: str
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class ArmCaseResult:
    case_id: str
    success: bool
    failure_codes: tuple[str, ...]
    steps: int
    model_attempts: int
    tool_calls: int
    tool_non_successes: int
    verification_passed: bool | None
    invariant_violations: int
    reconstruction_violations: int
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class ArmSummary:
    successful_cases: int
    total_cases: int
    total_steps: int
    total_model_attempts: int
    total_tool_calls: int
    total_tool_non_successes: int
    invariant_violations: int
    reconstruction_violations: int
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class CaseComparison:
    case_id: str
    outcome: str
    baseline: ArmCaseResult
    candidate: ArmCaseResult


@dataclass(frozen=True, slots=True)
class CandidateComparisonReport:
    schema_version: int
    ok: bool
    created_at: str
    validation_report_sha256: str
    core_commit: str
    suite_id: str
    suite_digest: str
    candidate: ComparisonCandidate
    artifact: ComparisonArtifact
    dependency_artifacts: tuple[DependencyArtifact, ...]
    environment_receipt: tuple[InstalledDistribution, ...]
    classification: str
    improvements: tuple[str, ...]
    regressions: tuple[str, ...]
    cases: tuple[CaseComparison, ...]
    baseline: ArmSummary
    candidate_summary: ArmSummary
    checks: tuple[ComparisonCheck, ...]
    boundaries_zh: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "ok": self.ok,
            "created_at": self.created_at,
            "validation_report_sha256": self.validation_report_sha256,
            "core_commit": self.core_commit,
            "suite_id": self.suite_id,
            "suite_digest": self.suite_digest,
            "candidate": asdict(self.candidate),
            "artifact": asdict(self.artifact),
            "dependency_artifacts": [
                asdict(artifact) for artifact in self.dependency_artifacts
            ],
            "environment_receipt": [
                asdict(distribution) for distribution in self.environment_receipt
            ],
            "classification": self.classification,
            "improvements": list(self.improvements),
            "regressions": list(self.regressions),
            "cases": [
                {
                    "case_id": item.case_id,
                    "outcome": item.outcome,
                    "baseline": asdict(item.baseline),
                    "candidate": asdict(item.candidate),
                }
                for item in self.cases
            ],
            "baseline": asdict(self.baseline),
            "candidate_summary": asdict(self.candidate_summary),
            "checks": [asdict(check) for check in self.checks],
            "boundaries_zh": list(self.boundaries_zh),
        }


@dataclass(frozen=True, slots=True)
class _ValidationEvidence:
    report_content: bytes
    report_sha256: str
    core_commit: str
    candidate: ComparisonCandidate
    artifact: ComparisonArtifact
    artifact_source: Path
    artifact_content: bytes


@dataclass(frozen=True, slots=True)
class _SuiteBundle:
    suite_id: str
    digest: str
    manifest: dict[str, object]
    files: tuple[tuple[str, bytes], ...]

    def write_to(self, destination: Path) -> None:
        destination.mkdir()
        for relative, content in self.files:
            path = destination / PurePosixPath(relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)


@dataclass(frozen=True, slots=True)
class _ComparisonEnvironment:
    root: Path
    python: Path
    receipt: tuple[InstalledDistribution, ...]


class CandidateComparator:
    """Run the L3 comparison transaction and atomically expose its report."""

    def __init__(
        self,
        config: CandidateComparisonConfig,
        *,
        runner: CommandRunner | None = None,
    ) -> None:
        self.config = config
        self.runner = runner or SubprocessCommandRunner()
        self._checks: list[ComparisonCheck] = []

    async def run(self) -> CandidateComparisonReport:
        config = self._validated_config()
        evidence_started = time.perf_counter()
        evidence = _load_validation_evidence(config.validation_evidence)
        self._record("l2-evidence", "l2-evidence-passed", evidence_started)

        with tempfile.TemporaryDirectory(prefix="thc-", ignore_cleanup_errors=True) as raw:
            root = _resolved_path(raw)
            environment = _isolated_environment(root)
            trusted_core = root / "trusted-core"
            snapshot_started = time.perf_counter()
            await self._snapshot_core(
                config,
                evidence.core_commit,
                trusted_core,
                environment,
            )
            self._record("trusted-core-snapshot", "trusted-core-snapshot-passed", snapshot_started)

            suite_started = time.perf_counter()
            suite_source = _suite_path(trusted_core, config.suite)
            suite_copy = root / "suite-source"
            copy_candidate_source(suite_source, suite_copy)
            suite = _load_suite_bundle(suite_copy, evidence.candidate.plugin_id)
            self._record("host-suite-contract", "host-suite-contract-passed", suite_started)

            artifact_copy = root / PurePosixPath(evidence.artifact.filename).name
            artifact_copy.write_bytes(evidence.artifact_content)
            _verify_artifact(artifact_copy, evidence)

            core_dist = root / "core-dist"
            core_started = time.perf_counter()
            core_wheel = await self._build_core_wheel(
                trusted_core,
                core_dist,
                environment,
                config,
            )
            self._record("core-wheel-build", "core-wheel-build-passed", core_started)

            dependency_artifacts = await self._freeze_dependencies(
                root / "dependency-wheelhouse",
                core_wheel,
                artifact_copy,
                environment,
                config,
            )
            frozen_core = root / "dependency-wheelhouse" / core_wheel.name
            frozen_candidate = root / "dependency-wheelhouse" / artifact_copy.name

            baseline_environment = await self._create_environment(
                "baseline-environment",
                root / "baseline-environment",
                frozen_core,
                frozen_candidate,
                root / "dependency-wheelhouse",
                environment,
                config,
            )
            candidate_environment = await self._create_environment(
                "candidate-environment",
                root / "candidate-environment",
                frozen_core,
                frozen_candidate,
                root / "dependency-wheelhouse",
                environment,
                config,
            )
            if baseline_environment.receipt != candidate_environment.receipt:
                raise CandidateComparisonEvidenceError(
                    "comparison-environment-receipt-mismatch",
                    "Baseline and Candidate environments do not contain the same distributions",
                )
            self._checks.append(
                ComparisonCheck(
                    "environment-receipt",
                    "comparison-environment-receipt-passed",
                    0.0,
                )
            )

            control = root / "control"
            control.mkdir()
            shutil.copy2(Path(__file__).with_name("comparison_probe.py"), control / "probe.py")
            baseline_suite = root / "baseline-suite"
            candidate_suite = root / "candidate-suite"
            suite.write_to(baseline_suite)
            suite.write_to(candidate_suite)

            baseline_facts = await self._run_arm(
                "baseline",
                baseline_environment.python,
                baseline_suite,
                root / "baseline-run",
                root / "baseline-result.json",
                control,
                evidence.candidate.plugin_id,
                evidence.candidate.version,
                root / "dependency-wheelhouse",
                environment,
                config,
            )
            candidate_facts = await self._run_arm(
                "candidate",
                candidate_environment.python,
                candidate_suite,
                root / "candidate-run",
                root / "candidate-result.json",
                control,
                evidence.candidate.plugin_id,
                evidence.candidate.version,
                root / "dependency-wheelhouse",
                environment,
                config,
            )

            # Candidate code has run with current-user authority. Re-prove every
            # immutable input before interpreting or publishing the comparison.
            _verify_evidence_unchanged(config.validation_evidence, evidence)
            _verify_artifact(artifact_copy, evidence)
            _verify_dependency_artifacts(
                root / "dependency-wheelhouse",
                dependency_artifacts,
            )
            if _environment_receipt(baseline_environment.root) != (
                baseline_environment.receipt
            ):
                raise CandidateComparisonEvidenceError(
                    "comparison-environment-receipt-changed",
                    "Baseline environment changed during comparison",
                )
            if _environment_receipt(candidate_environment.root) != (
                candidate_environment.receipt
            ):
                raise CandidateComparisonEvidenceError(
                    "comparison-environment-receipt-changed",
                    "Candidate environment changed during comparison",
                )
            if _directory_digest(baseline_suite) != suite.digest:
                raise CandidateComparisonEvidenceError(
                    "comparison-suite-identity-changed",
                    "Baseline suite bytes changed during comparison",
                )
            if _directory_digest(candidate_suite) != suite.digest:
                raise CandidateComparisonEvidenceError(
                    "comparison-suite-identity-changed",
                    "Candidate suite bytes changed during comparison",
                )
            self._checks.append(
                ComparisonCheck("input-recheck", "comparison-input-recheck-passed", 0.0)
            )

            cases = _compare_cases(suite.manifest, baseline_facts, candidate_facts)
            improvements = tuple(
                item.case_id for item in cases if item.outcome == "improved"
            )
            regressions = tuple(
                item.case_id for item in cases if item.outcome == "regressed"
            )
            report = CandidateComparisonReport(
                schema_version=COMPARISON_SCHEMA_VERSION,
                ok=True,
                created_at=datetime.now(UTC).isoformat(),
                validation_report_sha256=evidence.report_sha256,
                core_commit=evidence.core_commit,
                suite_id=suite.suite_id,
                suite_digest=suite.digest,
                candidate=evidence.candidate,
                artifact=evidence.artifact,
                dependency_artifacts=dependency_artifacts,
                environment_receipt=baseline_environment.receipt,
                classification=_classification(improvements, regressions),
                improvements=improvements,
                regressions=regressions,
                cases=cases,
                baseline=_summarize(tuple(item.baseline for item in cases)),
                candidate_summary=_summarize(tuple(item.candidate for item in cases)),
                checks=tuple(self._checks),
                boundaries_zh=(
                    "L3 消费 L2 发布的精确 Wheel 与 SHA-256，不重新构建候选。",
                    "Baseline 与 Candidate 安装相同核心和候选 Wheel；只有 Candidate 显式启用插件。",
                    "全部依赖只解析一次并冻结为同一组 Wheel；两臂离线安装且安装 receipt 必须相同。",
                    "任务、脚本、期望与评价规则来自 L2 固定核心提交中的宿主任务集，不由候选提供。",
                    "报告只分类观察到的 improvement、regression、mixed 或 no-change，"
                    "不代表批准、安装或晋升。",
                    "临时目录和虚拟环境不是操作系统沙箱；候选仍有当前用户权限，且只保证直接子进程收敛。",
                    "本轮是确定性能力合同比较，不等同于真实模型质量、Token 成本或通用 Benchmark。",
                ),
            )
            _commit_report_bundle(config.output, report)
            return report

    def _validated_config(self) -> CandidateComparisonConfig:
        evidence = self.config.validation_evidence.resolve()
        core = self.config.core_project.resolve()
        output = self.config.output.resolve()
        wheelhouse = (
            self.config.wheelhouse.resolve() if self.config.wheelhouse is not None else None
        )
        if not evidence.is_dir() or not core.is_dir():
            raise CandidateComparisonConfigurationError(
                "L2 evidence and trusted core must be existing directories"
            )
        if _paths_overlap(evidence, core):
            raise CandidateComparisonConfigurationError(
                "L2 evidence and trusted core directories must be disjoint"
            )
        if self.config.suite.is_absolute() or ".." in self.config.suite.parts:
            raise CandidateComparisonConfigurationError(
                "Comparison suite must be a relative path inside the trusted core"
            )
        if not self.config.suite.parts:
            raise CandidateComparisonConfigurationError("Comparison suite path is empty")
        if output.exists():
            raise CandidateComparisonConfigurationError(
                "Comparison output must be a new directory"
            )
        if _paths_overlap(output, evidence) or _paths_overlap(output, core):
            raise CandidateComparisonConfigurationError(
                "Comparison output must be outside evidence and core directories"
            )
        if self.config.allow_index == (wheelhouse is not None):
            raise CandidateComparisonConfigurationError(
                "Choose exactly one dependency source: --allow-index or --wheelhouse"
            )
        if wheelhouse is not None and not wheelhouse.is_dir():
            raise CandidateComparisonConfigurationError("Wheelhouse must be an existing directory")
        if wheelhouse is not None and _paths_overlap(output, wheelhouse):
            raise CandidateComparisonConfigurationError(
                "Comparison output must be outside the wheelhouse"
            )
        if not math.isfinite(self.config.command_timeout_seconds) or (
            self.config.command_timeout_seconds <= 0
        ):
            raise CandidateComparisonConfigurationError(
                "Comparison timeout must be a finite positive number"
            )
        if len(self.config.test_requirements) > 20:
            raise CandidateComparisonConfigurationError("Too many explicit test requirements")
        for raw in self.config.test_requirements:
            try:
                requirement = Requirement(raw)
            except InvalidRequirement as error:
                raise CandidateComparisonConfigurationError(
                    "An explicit test requirement is invalid"
                ) from error
            if requirement.url is not None:
                raise CandidateComparisonConfigurationError(
                    "Direct-reference test requirements are not supported"
                )
        return CandidateComparisonConfig(
            validation_evidence=evidence,
            core_project=core,
            suite=self.config.suite,
            output=output,
            wheelhouse=wheelhouse,
            allow_index=self.config.allow_index,
            test_requirements=self.config.test_requirements,
            command_timeout_seconds=self.config.command_timeout_seconds,
        )

    async def _snapshot_core(
        self,
        config: CandidateComparisonConfig,
        commit: str,
        destination: Path,
        environment: dict[str, str],
    ) -> None:
        clone = await self.runner.run(
            CommandRequest(
                "comparison-core-clone",
                (
                    "git",
                    "clone",
                    "--no-hardlinks",
                    "--no-checkout",
                    str(config.core_project),
                    str(destination),
                ),
                destination.parent,
                environment,
                config.command_timeout_seconds,
            )
        )
        _require_command(clone, "comparison-core-clone-failed")
        checkout = await self.runner.run(
            CommandRequest(
                "comparison-core-checkout",
                ("git", "-C", str(destination), "checkout", "--detach", commit),
                destination,
                environment,
                config.command_timeout_seconds,
            )
        )
        _require_command(checkout, "comparison-core-checkout-failed")
        actual = await self.runner.run(
            CommandRequest(
                "comparison-core-head",
                ("git", "-C", str(destination), "rev-parse", "HEAD"),
                destination,
                environment,
                min(config.command_timeout_seconds, 30.0),
                capture_stdout=True,
            )
        )
        _require_command(actual, "comparison-core-head-unavailable")
        if actual.stdout.lower() != commit.lower():
            raise CandidateComparisonEvidenceError(
                "comparison-core-identity-mismatch",
                "Trusted core checkout does not match the L2 core commit",
            )

    async def _build_core_wheel(
        self,
        trusted_core: Path,
        destination: Path,
        environment: dict[str, str],
        config: CandidateComparisonConfig,
    ) -> Path:
        _mkdir(destination)
        outcome = await self.runner.run(
            CommandRequest(
                "comparison-core-wheel-build",
                (
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    "--disable-pip-version-check",
                    "--no-deps",
                    "--wheel-dir",
                    str(destination),
                    *_dependency_source_args(config),
                    str(trusted_core),
                ),
                trusted_core,
                environment,
                config.command_timeout_seconds,
            )
        )
        _require_command(outcome, "comparison-core-wheel-build-failed")
        wheels = _wheel_files(destination)
        if len(wheels) != 1 or transient_wheel_members(wheels[0]):
            raise CandidateComparisonEvidenceError(
                "comparison-core-wheel-invalid",
                "Trusted core build did not produce one clean Wheel",
            )
        return wheels[0]

    async def _create_environment(
        self,
        name: str,
        destination: Path,
        core_wheel: Path,
        candidate_wheel: Path,
        wheelhouse: Path,
        environment: dict[str, str],
        config: CandidateComparisonConfig,
    ) -> _ComparisonEnvironment:
        started = time.perf_counter()
        create = await self.runner.run(
            CommandRequest(
                f"{name}-create",
                (sys.executable, "-m", "venv", str(destination)),
                destination.parent,
                environment,
                config.command_timeout_seconds,
            )
        )
        _require_command(create, "comparison-environment-create-failed")
        python = _venv_python(destination)
        install_environment = dict(environment)
        apply_wheelhouse_environment(install_environment, wheelhouse)
        install = await self.runner.run(
            CommandRequest(
                f"{name}-install",
                (
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-index",
                    "--find-links",
                    str(wheelhouse),
                    "--only-binary=:all:",
                    str(core_wheel),
                    str(candidate_wheel),
                    *config.test_requirements,
                ),
                destination.parent,
                install_environment,
                config.command_timeout_seconds,
            )
        )
        _require_command(install, "comparison-environment-install-failed")
        receipt = _environment_receipt(destination)
        self._record(name, f"{name}-passed", started)
        return _ComparisonEnvironment(destination, python, receipt)

    async def _freeze_dependencies(
        self,
        destination: Path,
        core_wheel: Path,
        candidate_wheel: Path,
        environment: dict[str, str],
        config: CandidateComparisonConfig,
    ) -> tuple[DependencyArtifact, ...]:
        started = time.perf_counter()
        _mkdir(destination)
        outcome = await self.runner.run(
            CommandRequest(
                "comparison-dependency-freeze",
                (
                    sys.executable,
                    "-m",
                    "pip",
                    "download",
                    "--disable-pip-version-check",
                    "--only-binary=:all:",
                    "--dest",
                    str(destination),
                    *_dependency_source_args(config),
                    str(core_wheel),
                    str(candidate_wheel),
                    *config.test_requirements,
                ),
                destination.parent,
                environment,
                config.command_timeout_seconds,
            )
        )
        _require_command(outcome, "comparison-dependency-freeze-failed")
        artifacts = _dependency_artifacts(destination)
        expected = {
            core_wheel.name: sha256_file(core_wheel),
            candidate_wheel.name: sha256_file(candidate_wheel),
        }
        observed = {artifact.filename: artifact.sha256 for artifact in artifacts}
        if any(observed.get(name) != digest for name, digest in expected.items()):
            raise CandidateComparisonEvidenceError(
                "comparison-dependency-root-mismatch",
                "Frozen dependencies do not contain the exact core and candidate Wheels",
            )
        self._record(
            "dependency-freeze",
            "comparison-dependency-freeze-passed",
            started,
        )
        return artifacts

    async def _run_arm(
        self,
        arm: str,
        python: Path,
        suite: Path,
        run_root: Path,
        result: Path,
        control: Path,
        plugin_id: str,
        plugin_version: str,
        wheelhouse: Path,
        environment: dict[str, str],
        config: CandidateComparisonConfig,
    ) -> dict[str, object]:
        started = time.perf_counter()
        arm_environment = dict(environment)
        arm_environment["PATH"] = os.pathsep.join(
            (str(python.parent), environment.get("PATH", ""))
        )
        apply_wheelhouse_environment(arm_environment, wheelhouse)
        outcome = await self.runner.run(
            CommandRequest(
                f"comparison-{arm}",
                (
                    str(python),
                    str(control / "probe.py"),
                    "--suite",
                    str(suite),
                    "--run-root",
                    str(run_root),
                    "--output",
                    str(result),
                    "--arm",
                    arm,
                    "--plugin-id",
                    plugin_id,
                    "--plugin-version",
                    plugin_version,
                ),
                control,
                arm_environment,
                config.command_timeout_seconds,
            )
        )
        _require_command(outcome, f"comparison-{arm}-failed")
        facts = _read_probe_result(result, arm)
        self._record(f"{arm}-comparison", f"{arm}-comparison-passed", started)
        return facts

    def _record(self, name: str, code: str, started: float) -> None:
        self._checks.append(
            ComparisonCheck(name, code, round(max(time.perf_counter() - started, 0.0), 3))
        )


def _load_validation_evidence(root: Path) -> _ValidationEvidence:
    report_path = root / "report.json"
    try:
        report_content = report_path.read_bytes()
    except OSError as error:
        raise CandidateComparisonEvidenceError(
            "l2-report-unreadable", "L2 report cannot be read"
        ) from error
    if len(report_content) > MAX_REPORT_BYTES:
        raise CandidateComparisonEvidenceError("l2-report-too-large", "L2 report is too large")
    try:
        raw = json.loads(report_content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CandidateComparisonEvidenceError(
            "l2-report-invalid", "L2 report is not valid UTF-8 JSON"
        ) from error
    if not isinstance(raw, dict) or raw.get("schema_version") != 1 or raw.get("ok") is not True:
        raise CandidateComparisonEvidenceError(
            "l2-report-not-passed", "L2 evidence is not one successful schema-v1 report"
        )
    checks = raw.get("checks")
    if (
        not isinstance(checks, list)
        or tuple(
            item.get("name") if isinstance(item, dict) else None for item in checks
        )
        != VALIDATION_CHECK_ORDER
        or any(
            not isinstance(item, dict) or item.get("status") != "passed"
            for item in checks
        )
    ):
        raise CandidateComparisonEvidenceError(
            "l2-gates-not-passed", "L2 evidence does not contain the complete passed gate set"
        )
    commit = raw.get("core_commit")
    candidate_raw = raw.get("candidate")
    artifact_raw = raw.get("artifact")
    if not isinstance(commit, str) or not _HEX_COMMIT.fullmatch(commit):
        raise CandidateComparisonEvidenceError(
            "l2-core-commit-invalid", "L2 core commit is missing or invalid"
        )
    if not isinstance(candidate_raw, dict) or not isinstance(artifact_raw, dict):
        raise CandidateComparisonEvidenceError(
            "l2-identity-missing", "L2 candidate or artifact identity is missing"
        )
    candidate_values = tuple(
        candidate_raw.get(name)
        for name in ("distribution", "version", "plugin_id", "entry_value", "entry_module")
    )
    if any(not isinstance(value, str) or not value for value in candidate_values):
        raise CandidateComparisonEvidenceError(
            "l2-candidate-identity-invalid", "L2 candidate identity is invalid"
        )
    distribution, version, plugin_id, entry_value, entry_module = candidate_values
    assert all(isinstance(value, str) for value in candidate_values)
    if not is_plugin_id(plugin_id):
        raise CandidateComparisonEvidenceError(
            "l2-plugin-id-invalid", "L2 plugin id is invalid"
        )
    filename = artifact_raw.get("filename")
    digest = artifact_raw.get("sha256")
    size = artifact_raw.get("size_bytes")
    if (
        not isinstance(filename, str)
        or not isinstance(digest, str)
        or not _HEX_SHA256.fullmatch(digest)
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size < 1
    ):
        raise CandidateComparisonEvidenceError(
            "l2-artifact-identity-invalid", "L2 artifact identity is invalid"
        )
    relative = PurePosixPath(filename)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or len(relative.parts) != 2
        or relative.parts[:1] != ("artifacts",)
        or not relative.name.casefold().endswith(".whl")
    ):
        raise CandidateComparisonEvidenceError(
            "l2-artifact-path-invalid", "L2 artifact path leaves its evidence directory"
        )
    source = root.joinpath(*relative.parts)
    try:
        resolved = source.resolve(strict=True)
        resolved.relative_to(root.resolve())
        content = resolved.read_bytes()
    except (OSError, ValueError) as error:
        raise CandidateComparisonEvidenceError(
            "l2-artifact-unreadable", "L2 artifact cannot be read safely"
        ) from error
    candidate = ComparisonCandidate(
        distribution=distribution,
        version=version,
        plugin_id=plugin_id,
        entry_value=entry_value,
        entry_module=entry_module,
    )
    artifact = ComparisonArtifact(filename=filename, sha256=digest.lower(), size_bytes=size)
    evidence = _ValidationEvidence(
        report_content=report_content,
        report_sha256=hashlib.sha256(report_content).hexdigest(),
        core_commit=commit.lower(),
        candidate=candidate,
        artifact=artifact,
        artifact_source=resolved,
        artifact_content=content,
    )
    _verify_artifact(resolved, evidence)
    return evidence


def _verify_artifact(path: Path, evidence: _ValidationEvidence) -> None:
    if path.stat().st_size != evidence.artifact.size_bytes:
        raise CandidateComparisonEvidenceError(
            "l2-artifact-size-mismatch", "L2 artifact size does not match its report"
        )
    if sha256_file(path) != evidence.artifact.sha256:
        raise CandidateComparisonEvidenceError(
            "l2-artifact-digest-mismatch", "L2 artifact digest does not match its report"
        )
    issues = audit_candidate_wheel(path, entry_module=evidence.candidate.entry_module)
    if issues:
        raise CandidateComparisonEvidenceError(
            issues[0], "L2 artifact no longer passes the candidate Wheel audit"
        )


def _verify_evidence_unchanged(root: Path, evidence: _ValidationEvidence) -> None:
    if (root / "report.json").read_bytes() != evidence.report_content:
        raise CandidateComparisonEvidenceError(
            "l2-report-identity-changed", "L2 report changed during comparison"
        )
    if evidence.artifact_source.read_bytes() != evidence.artifact_content:
        raise CandidateComparisonEvidenceError(
            "l2-artifact-identity-changed", "L2 artifact changed during comparison"
        )


def _suite_path(core: Path, relative: Path) -> Path:
    source = (core / relative).resolve()
    try:
        source.relative_to(core.resolve())
    except ValueError as error:
        raise CandidateComparisonEvidenceError(
            "comparison-suite-path-invalid", "Comparison suite leaves the trusted core"
        ) from error
    if not source.is_dir():
        raise CandidateComparisonEvidenceError(
            "comparison-suite-missing", "Comparison suite is missing from the trusted core"
        )
    return source


def _load_suite_bundle(root: Path, plugin_id: str) -> _SuiteBundle:
    manifest_path = root / "suite.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CandidateComparisonEvidenceError(
            "comparison-suite-invalid", "Comparison suite manifest is invalid"
        ) from error
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise CandidateComparisonEvidenceError(
            "comparison-suite-schema-invalid", "Comparison suite schema is unsupported"
        )
    suite_id = manifest.get("suite_id")
    target = manifest.get("target_plugin_id")
    raw_cases = manifest.get("cases")
    if not isinstance(suite_id, str) or not _SUITE_ID.fullmatch(suite_id):
        raise CandidateComparisonEvidenceError(
            "comparison-suite-id-invalid", "Comparison suite id is invalid"
        )
    if target != plugin_id:
        raise CandidateComparisonEvidenceError(
            "comparison-suite-plugin-mismatch",
            "Comparison suite does not target the L2 candidate plugin",
        )
    if not isinstance(raw_cases, list) or not raw_cases or len(raw_cases) > MAX_CASES:
        raise CandidateComparisonEvidenceError(
            "comparison-suite-cases-invalid", "Comparison suite must contain 1 to 20 cases"
        )
    seen: set[str] = set()
    for raw in raw_cases:
        if not isinstance(raw, dict):
            raise CandidateComparisonEvidenceError(
                "comparison-case-invalid", "Comparison case must be an object"
            )
        case_id = raw.get("id")
        task = raw.get("task")
        if (
            not isinstance(case_id, str)
            or not _CASE_ID.fullmatch(case_id)
            or case_id in seen
            or not isinstance(task, str)
            or not task
            or len(task) > 4_000
        ):
            raise CandidateComparisonEvidenceError(
                "comparison-case-invalid", "Comparison case identity or task is invalid"
            )
        seen.add(case_id)
        initial = _trusted_relative_path(root, raw.get("initial_dir"), "initial_dir")
        script = _trusted_relative_path(root, raw.get("script"), "script")
        if not initial.is_dir() or not script.is_file():
            raise CandidateComparisonEvidenceError(
                "comparison-case-input-invalid",
                "Comparison initial_dir must be a directory and script must be a file",
            )
        for arm in ("baseline", "candidate"):
            _validate_verifier_spec(raw.get(f"{arm}_verifier"))
        baseline_verifier = raw.get("baseline_verifier")
        assert isinstance(baseline_verifier, dict)
        if baseline_verifier.get("kind") != "command":
            raise CandidateComparisonEvidenceError(
                "comparison-baseline-verifier-invalid",
                "The baseline cannot select a plugin verifier while the plugin is disabled",
            )
        _validate_expected(raw.get("expected"))
        max_steps = raw.get("max_steps", 20)
        retries = raw.get("max_verification_retries", 0)
        if (
            isinstance(max_steps, bool)
            or not isinstance(max_steps, int)
            or not 1 <= max_steps <= 50
            or isinstance(retries, bool)
            or not isinstance(retries, int)
            or not 0 <= retries <= 3
        ):
            raise CandidateComparisonEvidenceError(
                "comparison-case-budget-invalid", "Comparison case runtime budget is invalid"
            )
    files: list[tuple[str, bytes]] = []
    total = 0
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        content = path.read_bytes()
        total += len(content)
        if len(files) >= MAX_SUITE_FILES or total > MAX_SUITE_BYTES:
            raise CandidateComparisonEvidenceError(
                "comparison-suite-budget-exceeded", "Comparison suite exceeds its fixed budget"
            )
        files.append((path.relative_to(root).as_posix(), content))
    return _SuiteBundle(
        suite_id=suite_id,
        digest=_content_digest(tuple(files)),
        manifest=manifest,
        files=tuple(files),
    )


def _trusted_relative_path(root: Path, raw: object, field: str) -> Path:
    if not isinstance(raw, str) or not raw:
        raise CandidateComparisonEvidenceError(
            "comparison-case-path-invalid", f"Comparison case {field} is invalid"
        )
    relative = PurePosixPath(raw)
    if relative.is_absolute() or ".." in relative.parts:
        raise CandidateComparisonEvidenceError(
            "comparison-case-path-invalid", f"Comparison case {field} leaves the suite"
        )
    path = root.joinpath(*relative.parts)
    if not path.exists():
        raise CandidateComparisonEvidenceError(
            "comparison-case-input-missing", f"Comparison case {field} is missing"
        )
    return path


def _validate_verifier_spec(raw: object) -> None:
    if not isinstance(raw, dict) or raw.get("kind") not in {"command", "plugin"}:
        raise CandidateComparisonEvidenceError(
            "comparison-verifier-invalid", "Comparison verifier selection is invalid"
        )
    timeout = raw.get("timeout_seconds", 60.0)
    if raw["kind"] == "command":
        argv = raw.get("argv")
        selection_valid = (
            isinstance(argv, list)
            and 0 < len(argv) <= 100
            and all(isinstance(item, str) and 0 < len(item) <= 1_000 for item in argv)
        )
    else:
        name = raw.get("name")
        selection_valid = isinstance(name, str) and 0 < len(name) <= 1_000
    if (
        not selection_valid
        or isinstance(timeout, bool)
        or not isinstance(timeout, int | float)
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise CandidateComparisonEvidenceError(
            "comparison-verifier-invalid", "Comparison verifier selection is invalid"
        )


def _validate_expected(raw: object) -> None:
    if not isinstance(raw, dict):
        raise CandidateComparisonEvidenceError(
            "comparison-expectation-invalid", "Comparison expectation is invalid"
        )
    if not isinstance(raw.get("reason"), str):
        raise CandidateComparisonEvidenceError(
            "comparison-expectation-invalid", "Expected turn reason is invalid"
        )
    verification = raw.get("verification_passed")
    if verification is not None and not isinstance(verification, bool):
        raise CandidateComparisonEvidenceError(
            "comparison-expectation-invalid", "Expected verification status is invalid"
        )
    tool_results = raw.get("tool_results")
    if not isinstance(tool_results, list) or len(tool_results) > 100:
        raise CandidateComparisonEvidenceError(
            "comparison-expectation-invalid", "Expected tool results are invalid"
        )
    for item in tool_results:
        if not isinstance(item, dict):
            raise CandidateComparisonEvidenceError(
                "comparison-expectation-invalid", "Expected tool result is invalid"
            )
        if not isinstance(item.get("tool_name"), str) or not isinstance(
            item.get("status"), str
        ):
            raise CandidateComparisonEvidenceError(
                "comparison-expectation-invalid", "Expected tool result is invalid"
            )
        policy = item.get("policy")
        if policy is not None and not isinstance(policy, str):
            raise CandidateComparisonEvidenceError(
                "comparison-expectation-invalid", "Expected tool policy is invalid"
            )


def _read_probe_result(path: Path, arm: str) -> dict[str, object]:
    try:
        if path.stat().st_size > MAX_RESULT_BYTES:
            raise CandidateComparisonEvidenceError(
                "comparison-result-too-large", "Comparison probe result is too large"
            )
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CandidateComparisonEvidenceError(
            "comparison-result-invalid", "Comparison probe result is invalid"
        ) from error
    if (
        not isinstance(raw, dict)
        or raw.get("schema_version") != 1
        or raw.get("arm") != arm
        or not isinstance(raw.get("cases"), list)
    ):
        raise CandidateComparisonEvidenceError(
            "comparison-result-invalid", "Comparison probe result contract is invalid"
        )
    return raw


def _compare_cases(
    manifest: dict[str, object],
    baseline: dict[str, object],
    candidate: dict[str, object],
) -> tuple[CaseComparison, ...]:
    raw_cases = manifest["cases"]
    assert isinstance(raw_cases, list)
    baseline_cases = _indexed_facts(baseline, raw_cases)
    candidate_cases = _indexed_facts(candidate, raw_cases)
    compared = []
    for raw in raw_cases:
        assert isinstance(raw, dict)
        case_id = raw["id"]
        expected = raw["expected"]
        assert isinstance(case_id, str) and isinstance(expected, dict)
        baseline_result = _evaluate_case(case_id, baseline_cases[case_id], expected)
        candidate_result = _evaluate_case(case_id, candidate_cases[case_id], expected)
        if not baseline_result.success and candidate_result.success:
            outcome = "improved"
        elif baseline_result.success and not candidate_result.success:
            outcome = "regressed"
        elif baseline_result.success:
            outcome = "unchanged-pass"
        else:
            outcome = "unchanged-fail"
        compared.append(CaseComparison(case_id, outcome, baseline_result, candidate_result))
    return tuple(compared)


def _indexed_facts(
    raw: dict[str, object], cases: list[object]
) -> dict[str, dict[str, object]]:
    raw_facts = raw["cases"]
    assert isinstance(raw_facts, list)
    expected_ids = [item["id"] for item in cases if isinstance(item, dict)]
    indexed: dict[str, dict[str, object]] = {}
    for fact in raw_facts:
        if not isinstance(fact, dict) or not isinstance(fact.get("case_id"), str):
            raise CandidateComparisonEvidenceError(
                "comparison-result-invalid", "Comparison case facts are invalid"
            )
        case_id = fact["case_id"]
        if case_id in indexed:
            raise CandidateComparisonEvidenceError(
                "comparison-result-invalid", "Comparison case facts are duplicated"
            )
        indexed[case_id] = _validated_fact(fact)
    if list(indexed) != expected_ids:
        raise CandidateComparisonEvidenceError(
            "comparison-result-case-mismatch", "Comparison result cases do not match the suite"
        )
    return indexed


def _validated_fact(fact: dict[str, object]) -> dict[str, object]:
    if not isinstance(fact.get("completed"), bool):
        raise CandidateComparisonEvidenceError(
            "comparison-result-invalid", "Comparison completion fact is invalid"
        )
    if not isinstance(fact.get("evidence_complete"), bool):
        raise CandidateComparisonEvidenceError(
            "comparison-result-invalid", "Comparison evidence-completeness fact is invalid"
        )
    if not isinstance(fact.get("plugin_identity_matches"), bool):
        raise CandidateComparisonEvidenceError(
            "comparison-result-invalid", "Comparison plugin-identity fact is invalid"
        )
    reason = fact.get("reason")
    if not isinstance(reason, str) or len(reason) > 128:
        raise CandidateComparisonEvidenceError(
            "comparison-result-invalid", "Comparison reason fact is invalid"
        )
    for name in (
        "steps",
        "model_attempts",
        "tool_calls",
        "invariant_violations",
        "reconstruction_violations",
    ):
        _bounded_int(fact.get(name))
    _bounded_float(fact.get("duration_seconds"))
    verification = fact.get("verification_passed")
    if verification is not None and not isinstance(verification, bool):
        raise CandidateComparisonEvidenceError(
            "comparison-result-invalid", "Comparison verification fact is invalid"
        )
    exit_code = fact.get("verification_exit_code")
    if exit_code is not None and (
        isinstance(exit_code, bool) or not isinstance(exit_code, int)
    ):
        raise CandidateComparisonEvidenceError(
            "comparison-result-invalid", "Comparison verifier exit code is invalid"
        )
    tools = fact.get("tool_results")
    if not isinstance(tools, list) or len(tools) > 100:
        raise CandidateComparisonEvidenceError(
            "comparison-result-invalid", "Comparison tool facts are invalid"
        )
    for item in tools:
        if not isinstance(item, dict):
            raise CandidateComparisonEvidenceError(
                "comparison-result-invalid", "Comparison tool fact is invalid"
            )
        tool_name = item.get("tool_name")
        status = item.get("status")
        policy = item.get("policy")
        if (
            not isinstance(tool_name, str)
            or len(tool_name) > 256
            or not isinstance(status, str)
            or len(status) > 64
            or (policy is not None and (not isinstance(policy, str) or len(policy) > 256))
        ):
            raise CandidateComparisonEvidenceError(
                "comparison-result-invalid", "Comparison tool fact is invalid"
            )
    return fact


def _evaluate_case(
    case_id: str,
    facts: dict[str, object],
    expected: dict[str, object],
) -> ArmCaseResult:
    completed = facts.get("completed") is True
    evidence_complete = facts.get("evidence_complete") is True
    reason = facts.get("reason")
    verification = facts.get("verification_passed")
    raw_tools = facts.get("tool_results")
    invariants = _bounded_int(facts.get("invariant_violations"))
    reconstruction = _bounded_int(facts.get("reconstruction_violations"))
    failures = []
    if not completed:
        failures.append("runtime-not-completed")
    if not evidence_complete:
        failures.append("event-evidence-incomplete")
    if facts.get("plugin_identity_matches") is not True:
        failures.append("arm-plugin-identity-mismatch")
    if reason != expected.get("reason"):
        failures.append("turn-reason-mismatch")
    if verification != expected.get("verification_passed"):
        failures.append("verification-result-mismatch")
    if raw_tools != expected.get("tool_results"):
        failures.append("tool-results-mismatch")
    if invariants != 0:
        failures.append("invariant-violation")
    if reconstruction != 0:
        failures.append("request-reconstruction-violation")
    tool_non_successes = 0
    if isinstance(raw_tools, list):
        tool_non_successes = sum(
            isinstance(item, dict) and item.get("status") != "succeeded" for item in raw_tools
        )
    return ArmCaseResult(
        case_id=case_id,
        success=not failures,
        failure_codes=tuple(failures),
        steps=_bounded_int(facts.get("steps")),
        model_attempts=_bounded_int(facts.get("model_attempts")),
        tool_calls=_bounded_int(facts.get("tool_calls")),
        tool_non_successes=tool_non_successes,
        verification_passed=verification if isinstance(verification, bool) else None,
        invariant_violations=invariants,
        reconstruction_violations=reconstruction,
        duration_seconds=_bounded_float(facts.get("duration_seconds")),
    )


def _bounded_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1_000_000:
        raise CandidateComparisonEvidenceError(
            "comparison-result-invalid", "Comparison numeric fact is invalid"
        )
    return value


def _bounded_float(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or not 0 <= value <= 1_000_000
    ):
        raise CandidateComparisonEvidenceError(
            "comparison-result-invalid", "Comparison duration fact is invalid"
        )
    return round(float(value), 3)


def _summarize(cases: tuple[ArmCaseResult, ...]) -> ArmSummary:
    return ArmSummary(
        successful_cases=sum(item.success for item in cases),
        total_cases=len(cases),
        total_steps=sum(item.steps for item in cases),
        total_model_attempts=sum(item.model_attempts for item in cases),
        total_tool_calls=sum(item.tool_calls for item in cases),
        total_tool_non_successes=sum(item.tool_non_successes for item in cases),
        invariant_violations=sum(item.invariant_violations for item in cases),
        reconstruction_violations=sum(item.reconstruction_violations for item in cases),
        duration_seconds=round(sum(item.duration_seconds for item in cases), 3),
    )


def _classification(improvements: tuple[str, ...], regressions: tuple[str, ...]) -> str:
    if improvements and regressions:
        return "mixed"
    if regressions:
        return "regressed"
    if improvements:
        return "improved"
    return "no-change"


def _content_digest(files: tuple[tuple[str, bytes], ...]) -> str:
    digest = hashlib.sha256()
    for relative, content in files:
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _directory_digest(root: Path) -> str:
    files = tuple(
        (path.relative_to(root).as_posix(), path.read_bytes())
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    )
    return _content_digest(files)


def _require_command(outcome: object, code: str) -> None:
    exit_code = getattr(outcome, "exit_code", None)
    timed_out = getattr(outcome, "timed_out", False)
    start_failed = getattr(outcome, "start_failed", False)
    if exit_code != 0 or timed_out or start_failed:
        raise CandidateComparisonEvidenceError(code, "Comparison subprocess failed")


def _dependency_source_args(config: CandidateComparisonConfig) -> tuple[str, ...]:
    if config.wheelhouse is not None:
        return ("--no-index", "--find-links", str(config.wheelhouse))
    return ()


def _resolved_path(raw: str) -> Path:
    return Path(raw).resolve()


def _mkdir(path: Path) -> None:
    path.mkdir()


def _wheel_files(path: Path) -> tuple[Path, ...]:
    return tuple(path.glob("*.whl"))


def _dependency_artifacts(root: Path) -> tuple[DependencyArtifact, ...]:
    try:
        entries = tuple(sorted(root.iterdir(), key=lambda path: path.name.casefold()))
    except OSError as error:
        raise CandidateComparisonEvidenceError(
            "comparison-dependency-set-unreadable",
            "Frozen dependency set cannot be read",
        ) from error
    if (
        not entries
        or len(entries) > MAX_DEPENDENCY_WHEELS
        or any(not path.is_file() or path.suffix.casefold() != ".whl" for path in entries)
    ):
        raise CandidateComparisonEvidenceError(
            "comparison-dependency-set-invalid",
            "Frozen dependency set must contain only bounded Wheel files",
        )
    total = sum(path.stat().st_size for path in entries)
    if total > MAX_DEPENDENCY_BYTES:
        raise CandidateComparisonEvidenceError(
            "comparison-dependency-set-too-large",
            "Frozen dependency set exceeds its byte budget",
        )
    artifacts: list[DependencyArtifact] = []
    distributions: set[str] = set()
    for path in entries:
        try:
            raw_name, version, _build, _tags = parse_wheel_filename(path.name)
        except InvalidWheelFilename as error:
            raise CandidateComparisonEvidenceError(
                "comparison-dependency-set-invalid",
                "Frozen dependency set contains an invalid Wheel filename",
            ) from error
        distribution = canonicalize_name(raw_name)
        if distribution in distributions:
            raise CandidateComparisonEvidenceError(
                "comparison-dependency-set-ambiguous",
                "Frozen dependency set contains more than one Wheel for a distribution",
            )
        distributions.add(distribution)
        artifacts.append(
            DependencyArtifact(
                path.name,
                distribution,
                str(version),
                sha256_file(path),
                path.stat().st_size,
            )
        )
    return tuple(artifacts)


def _verify_dependency_artifacts(
    root: Path,
    expected: tuple[DependencyArtifact, ...],
) -> None:
    if _dependency_artifacts(root) != expected:
        raise CandidateComparisonEvidenceError(
            "comparison-dependency-set-changed",
            "Frozen dependency Wheels changed during comparison",
        )


def _environment_receipt(root: Path) -> tuple[InstalledDistribution, ...]:
    site_packages = _site_packages(root)
    dist_infos = tuple(
        sorted(site_packages.glob("*.dist-info"), key=lambda path: path.name.casefold())
    )
    if not dist_infos or len(dist_infos) > MAX_INSTALLED_DISTRIBUTIONS:
        raise CandidateComparisonEvidenceError(
            "comparison-environment-receipt-invalid",
            "Installed distribution receipt is missing or exceeds its budget",
        )
    receipt: list[InstalledDistribution] = []
    seen: set[str] = set()
    for dist_info in dist_infos:
        metadata_path = dist_info / "METADATA"
        try:
            content = metadata_path.read_bytes()
        except OSError as error:
            raise CandidateComparisonEvidenceError(
                "comparison-environment-receipt-invalid",
                "Installed distribution metadata cannot be read",
            ) from error
        if len(content) > MAX_METADATA_BYTES:
            raise CandidateComparisonEvidenceError(
                "comparison-environment-receipt-invalid",
                "Installed distribution metadata exceeds its budget",
            )
        metadata = BytesParser().parsebytes(content, headersonly=True)
        raw_name = metadata.get("Name")
        version = metadata.get("Version")
        if (
            not isinstance(raw_name, str)
            or not raw_name
            or not isinstance(version, str)
            or not version
            or len(raw_name) > 256
            or len(version) > 256
        ):
            raise CandidateComparisonEvidenceError(
                "comparison-environment-receipt-invalid",
                "Installed distribution identity is invalid",
            )
        name = canonicalize_name(raw_name)
        if name in seen:
            raise CandidateComparisonEvidenceError(
                "comparison-environment-receipt-invalid",
                "Installed distribution identity is duplicated",
            )
        seen.add(name)
        receipt.append(InstalledDistribution(name, version))
    return tuple(sorted(receipt, key=lambda item: item.name))


def _site_packages(root: Path) -> Path:
    windows = root / "Lib" / "site-packages"
    if windows.is_dir():
        return windows
    candidates = tuple(root.glob("lib/python*/site-packages"))
    if len(candidates) != 1 or not candidates[0].is_dir():
        raise CandidateComparisonEvidenceError(
            "comparison-environment-receipt-invalid",
            "Comparison environment has no unique site-packages directory",
        )
    return candidates[0]


def _isolated_environment(root: Path) -> dict[str, str]:
    environment = sanitized_environment()
    environment.pop("PYTHONPATH", None)
    home = root / "home"
    temp = root / "t"
    cache = root / "pip-cache"
    for path in (home, temp, cache):
        path.mkdir(parents=True, exist_ok=True)
    environment.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "APPDATA": str(home / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(home / "AppData" / "Local"),
            "TEMP": str(temp),
            "TMP": str(temp),
            "TMPDIR": str(temp),
            "PIP_CACHE_DIR": str(cache),
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "TRACEH_CANDIDATE_COMPARISON": "1",
        }
    )
    return environment


def _venv_python(root: Path) -> Path:
    return root / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _commit_report_bundle(output: Path, report: CandidateComparisonReport) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    try:
        _write_report(staging, report)
        os.replace(staging, output)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _write_report(output: Path, report: CandidateComparisonReport) -> None:
    payload = json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _atomic_text(output / "report.json", payload)
    lines = [
        "# TraceHarness 插件候选 L3 对照报告",
        "",
        f"对照完成：**{'是' if report.ok else '否'}**",
        f"观察分类：**{report.classification}**",
        f"可信核心提交：`{report.core_commit}`",
        f"任务集：`{report.suite_id}` / `{report.suite_digest}`",
        f"候选：`{report.candidate.plugin_id}` / "
        f"`{report.candidate.distribution}=={report.candidate.version}`",
        f"L2 Wheel SHA-256：`{report.artifact.sha256}`",
        f"冻结依赖 Wheel：`{len(report.dependency_artifacts)}` 个",
        f"两臂安装 receipt：同一组 `{len(report.environment_receipt)}` 个 Distribution",
        "",
        "| Case | Outcome | Baseline | Candidate | Steps B/C | Tools B/C |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for case in report.cases:
        lines.append(
            f"| `{case.case_id}` | {case.outcome} | "
            f"{'pass' if case.baseline.success else 'fail'} | "
            f"{'pass' if case.candidate.success else 'fail'} | "
            f"{case.baseline.steps}/{case.candidate.steps} | "
            f"{case.baseline.tool_calls}/{case.candidate.tool_calls} |"
        )
    lines.extend(
        [
            "",
            "## 解释",
            "",
            "- improvement 只表示 Candidate 在固定宿主期望上由失败变为通过。",
            "- regression 只表示固定宿主期望由通过变为失败。",
            "- 本报告不包含批准结论，也不会安装、启用或晋升插件。",
            "",
            "## 边界",
            "",
            *(f"- {item}" for item in report.boundaries_zh),
        ]
    )
    _atomic_text(output / "report.md", "\n".join(lines) + "\n")


def _atomic_text(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


__all__ = [
    "ArmCaseResult",
    "ArmSummary",
    "COMPARISON_EXIT_CODE",
    "CandidateComparator",
    "CandidateComparisonConfig",
    "CandidateComparisonConfigurationError",
    "CandidateComparisonEvidenceError",
    "CandidateComparisonReport",
    "CaseComparison",
    "ComparisonArtifact",
    "ComparisonCandidate",
    "DependencyArtifact",
    "InstalledDistribution",
]
