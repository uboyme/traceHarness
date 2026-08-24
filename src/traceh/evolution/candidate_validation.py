"""Independent L2 build/test/doctor/contract validation for plugin candidates.

The validator is a development control plane.  It never imports candidate code
into the host process and never installs it into the host environment.  A
trusted core ``HEAD`` snapshot supplies the regression suite and evaluator;
candidate tests run separately and cannot replace that suite or its pytest
configuration.

The temporary directories and virtual environments are isolation from the
working tree and host Python installation, not an operating-system sandbox.
Candidate code still runs with the current user's permissions.  That boundary
is recorded in every report rather than hidden behind the word "isolated".
"""

from __future__ import annotations

import ast
import asyncio
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
import tomllib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from traceh.concurrency import await_worker_convergence
from traceh.evolution.artifacts import (
    ArtifactContractError,
    apply_wheelhouse_environment,
    audit_candidate_wheel,
    copy_candidate_source,
    sha256_file,
    transient_wheel_members,
)
from traceh.plugins import is_plugin_id
from traceh.process_control import converge_process
from traceh.tools.builtins.shell import sanitized_environment
from traceh.version import DISTRIBUTION_NAME

REPORT_SCHEMA_VERSION = 1
VALIDATION_EXIT_CODE = 8
_HEX_COMMIT = re.compile(r"[0-9a-fA-F]{40,64}\Z")
_ENTRY_VALUE = re.compile(
    r"(?P<module>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*):"
    r"(?P<attribute>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\Z"
)
_DISTRIBUTION_NAME = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?\Z"
)
VALIDATION_CHECK_ORDER = (
    "candidate-source-contract",
    "trusted-core-snapshot",
    "core-wheel-build",
    "candidate-wheel-build",
    "candidate-wheel-audit",
    "candidate-environment-install",
    "installed-metadata-contract",
    "plugin-doctor",
    "candidate-test-collection",
    "candidate-tests",
    "regression-environment-install",
    "core-regression",
    "validated-artifact-publication",
)
_CHECK_ORDER = VALIDATION_CHECK_ORDER


class CandidateValidationConfigurationError(ValueError):
    """A safe, host-authored validation configuration error."""


class CandidateSourceError(ValueError):
    """A fixed-code candidate source contract failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class CandidateValidationConfig:
    candidate: Path
    core_project: Path
    output: Path
    plugin_id: str | None = None
    distribution: str | None = None
    wheelhouse: Path | None = None
    allow_index: bool = False
    test_requirements: tuple[str, ...] = ()
    command_timeout_seconds: float = 600.0
    core_timeout_seconds: float = 1800.0


@dataclass(frozen=True, slots=True)
class CandidateIdentity:
    distribution: str
    version: str
    plugin_id: str
    entry_value: str
    entry_module: str


@dataclass(frozen=True, slots=True)
class ValidationCheck:
    name: str
    status: str
    code: str
    summary_zh: str
    duration_seconds: float
    exit_code: int | None = None


@dataclass(frozen=True, slots=True)
class ValidatedArtifact:
    filename: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class _TrustedCoreSnapshot:
    commit: str
    version: Version


@dataclass(frozen=True, slots=True)
class _AuditedWheel:
    source_path: Path
    path: Path
    sha256: str
    entry_module: str
    content: bytes


@dataclass(frozen=True, slots=True)
class _PreparedArtifact:
    path: Path
    metadata: ValidatedArtifact
    entry_module: str
    content: bytes


@dataclass(frozen=True, slots=True)
class CandidateValidationReport:
    schema_version: int
    ok: bool
    created_at: str
    core_commit: str | None
    candidate: CandidateIdentity | None
    artifact: ValidatedArtifact | None
    checks: tuple[ValidationCheck, ...]
    boundaries_zh: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "ok": self.ok,
            "created_at": self.created_at,
            "core_commit": self.core_commit,
            "candidate": asdict(self.candidate) if self.candidate is not None else None,
            "artifact": asdict(self.artifact) if self.artifact is not None else None,
            "checks": [asdict(check) for check in self.checks],
            "boundaries_zh": list(self.boundaries_zh),
        }


@dataclass(frozen=True, slots=True)
class CommandRequest:
    purpose: str
    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    timeout_seconds: float
    capture_stdout: bool = False
    stdout_limit: int = 256


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    exit_code: int | None
    duration_seconds: float
    timed_out: bool = False
    start_failed: bool = False
    stdout: str = ""


class CommandRunner(Protocol):
    async def run(self, request: CommandRequest) -> CommandOutcome:
        ...


class SubprocessCommandRunner:
    """Run one direct child with cancellation convergence and no raw logs.

    Candidate-controlled stdout/stderr are sent to ``DEVNULL``.  Reports carry
    exit codes and fixed host summaries, avoiding both unbounded output and
    accidental credential disclosure.  Only the trusted ``git rev-parse`` call
    requests a tiny bounded stdout capture.
    """

    async def run(self, request: CommandRequest) -> CommandOutcome:
        started = time.perf_counter()
        stdout_target: object = asyncio.subprocess.DEVNULL
        capture = None
        if request.capture_stdout:
            capture = tempfile.TemporaryFile()
            stdout_target = capture
        try:
            try:
                spawn = asyncio.create_task(
                    asyncio.create_subprocess_exec(
                        *request.argv,
                        cwd=request.cwd,
                        env=request.env,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=stdout_target,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                )
                try:
                    process = await asyncio.shield(spawn)
                except asyncio.CancelledError as cancellation:
                    await await_worker_convergence(spawn)
                    if not spawn.cancelled() and spawn.exception() is None:
                        process = spawn.result()
                        await converge_process(process)
                    raise cancellation
            except (OSError, ValueError):
                return CommandOutcome(
                    None,
                    time.perf_counter() - started,
                    start_failed=True,
                )
            try:
                async with asyncio.timeout(request.timeout_seconds):
                    await process.wait()
            except asyncio.CancelledError:
                await converge_process(process)
                raise
            except TimeoutError:
                interrupted = await converge_process(process)
                if interrupted:
                    raise asyncio.CancelledError from None
                return CommandOutcome(
                    process.returncode,
                    time.perf_counter() - started,
                    timed_out=True,
                )

            stdout = ""
            if capture is not None:
                capture.seek(0, os.SEEK_END)
                size = capture.tell()
                capture.seek(max(0, size - request.stdout_limit))
                raw = capture.read(request.stdout_limit)
                stdout = raw.decode("utf-8", errors="replace").strip()
            return CommandOutcome(
                process.returncode,
                time.perf_counter() - started,
                stdout=stdout,
            )
        finally:
            if capture is not None:
                capture.close()


class CandidateValidator:
    """Execute the L2 transaction and emit an immutable-by-process report."""

    def __init__(
        self,
        config: CandidateValidationConfig,
        *,
        runner: CommandRunner | None = None,
    ) -> None:
        self.config = config
        self.runner = runner or SubprocessCommandRunner()
        self._checks: dict[str, ValidationCheck] = {}

    async def run(self) -> CandidateValidationReport:
        config = self._validated_config()
        identity: CandidateIdentity | None = None
        artifact: ValidatedArtifact | None = None
        core_commit: str | None = None
        prepared_artifact: _PreparedArtifact | None = None

        with tempfile.TemporaryDirectory(
            # Keep this deliberately short.  Candidate and trusted tests may
            # create nested build/pytest paths, and Windows tooling still
            # contains components that fail near the legacy path limit.
            prefix="thv-",
            ignore_cleanup_errors=True,
        ) as raw_root:
            root = _resolved_path(raw_root)
            environment = _isolated_environment(root)
            _apply_dependency_environment(environment, config)
            candidate_copy = root / "candidate-source"
            trusted_core = root / "trusted-core"
            core_dist = root / "core-dist"
            candidate_dist = root / "candidate-dist"
            diagnostics = root / "diagnostics"
            control = root / "control"
            control.mkdir()
            shutil.copy2(_contract_probe_source(), control / "contract_probe.py")
            (control / "pytest.ini").write_text(
                "[pytest]\naddopts =\nasyncio_mode = auto\n",
                encoding="utf-8",
            )

            started = time.perf_counter()
            try:
                copy_candidate_source(config.candidate, candidate_copy)
            except (ArtifactContractError, CandidateSourceError, OSError) as error:
                code = getattr(error, "code", "candidate-source-unreadable")
                self._record(
                    "candidate-source-contract",
                    "failed",
                    code,
                    "候选源码未通过宿主静态合同检查。",
                    time.perf_counter() - started,
                )
            else:
                core_snapshot = await self._snapshot_core(
                    config,
                    trusted_core,
                    environment,
                )
                if core_snapshot is not None:
                    core_commit = core_snapshot.commit
                    try:
                        identity = _read_candidate_identity(
                            candidate_copy,
                            requested_plugin_id=config.plugin_id,
                            requested_distribution=config.distribution,
                            trusted_core_version=core_snapshot.version,
                        )
                    except (CandidateSourceError, OSError) as error:
                        code = getattr(error, "code", "candidate-source-unreadable")
                        self._record(
                            "candidate-source-contract",
                            "failed",
                            code,
                            "候选源码未通过宿主静态合同检查。",
                            time.perf_counter() - started,
                        )
                    else:
                        self._record(
                            "candidate-source-contract",
                            "passed",
                            "candidate-source-contract-passed",
                            "候选源码、身份、依赖和专用工作区合同通过。",
                            time.perf_counter() - started,
                        )

            core_wheel: Path | None = None
            candidate_wheel: Path | None = None
            if core_commit is not None and identity is not None:
                core_wheel = await self._build_wheel(
                    name="core-wheel-build",
                    project=trusted_core,
                    destination=core_dist,
                    environment=environment,
                    config=config,
                )
            if identity is not None:
                candidate_wheel = await self._build_wheel(
                    name="candidate-wheel-build",
                    project=candidate_copy,
                    destination=candidate_dist,
                    environment=environment,
                    config=config,
                )

            audited_wheel: _AuditedWheel | None = None
            if candidate_wheel is not None and identity is not None:
                audit_started = time.perf_counter()
                try:
                    audited_wheel = _capture_audited_wheel(
                        candidate_wheel,
                        root / "audited-wheel",
                        entry_module=identity.entry_module,
                    )
                except (ArtifactContractError, OSError) as error:
                    self._record(
                        "candidate-wheel-audit",
                        "failed",
                        getattr(error, "code", "wheel-unreadable"),
                        "候选 Wheel 未通过宿主管控的成员与入口审计。",
                        time.perf_counter() - audit_started,
                    )
                else:
                    self._record(
                        "candidate-wheel-audit",
                        "passed",
                        "candidate-wheel-audit-passed",
                        "候选 Wheel 不含缓存、启动钩子或入口外顶层模块。",
                        time.perf_counter() - audit_started,
                    )

            if (
                core_wheel is not None
                and candidate_wheel is not None
                and identity is not None
                and self._passed("candidate-wheel-audit")
            ):
                assert audited_wheel is not None
                candidate_python = await self._create_and_install_environment(
                    check_name="candidate-environment-install",
                    root=root / "candidate-environment",
                    core_wheel=core_wheel,
                    candidate_wheel=audited_wheel.path,
                    environment=environment,
                    config=config,
                )
                regression_python = await self._create_and_install_environment(
                    check_name="regression-environment-install",
                    root=root / "regression-environment",
                    core_wheel=core_wheel,
                    candidate_wheel=audited_wheel.path,
                    environment=environment,
                    config=config,
                )
                # Install both environments before executing candidate code.
                # This closes the ordinary audit -> candidate-tests -> second
                # installation mutation window.  It is still not an OS
                # sandbox; the report states that stronger boundary honestly.
                if candidate_python is not None:
                    await self._run_installed_checks(
                        python=candidate_python,
                        identity=identity,
                        candidate_copy=candidate_copy,
                        control=control,
                        environment=environment,
                        config=config,
                    )
                if regression_python is not None:
                    await self._run_core_regression(
                        python=regression_python,
                        trusted_core=trusted_core,
                        environment=environment,
                        config=config,
                        diagnostics=diagnostics,
                    )

            prerequisite_checks = _CHECK_ORDER[:-1]
            if all(self._passed(name) for name in prerequisite_checks):
                assert audited_wheel is not None
                artifact_started = time.perf_counter()
                try:
                    prepared_artifact = _prepare_validated_artifact(
                        audited_wheel,
                        root / "validated-artifact",
                    )
                except (ArtifactContractError, OSError) as error:
                    self._record(
                        "validated-artifact-publication",
                        "failed",
                        getattr(
                            error,
                            "code",
                            "validated-artifact-publication-failed",
                        ),
                        "全部验证已通过，但无法发布经过验证的候选 Wheel。",
                        time.perf_counter() - artifact_started,
                    )
                else:
                    artifact = prepared_artifact.metadata
                    self._record(
                        "validated-artifact-publication",
                        "passed",
                        "validated-artifact-publication-passed",
                        "全部门禁通过后，候选 Wheel 与 SHA-256 已发布到新输出目录。",
                        time.perf_counter() - artifact_started,
                    )

            checks = self._final_checks()
            report = CandidateValidationReport(
                schema_version=REPORT_SCHEMA_VERSION,
                ok=all(check.status == "passed" for check in checks),
                created_at=datetime.now(UTC).isoformat(),
                core_commit=core_commit,
                candidate=identity,
                artifact=artifact,
                checks=checks,
                boundaries_zh=(
                    "验证使用仓库外临时源码副本和虚拟环境，不修改候选、核心工作区或宿主 Python。",
                    "候选测试属于候选自测；静态合同、Wheel 审计、安装元数据、doctor "
                    "和核心回归由宿主管控。",
                    "子进程环境不继承 Key、Token、Secret 等变量，报告不保存候选 stdout/stderr。",
                    "虚拟环境和临时目录不是操作系统沙箱；候选代码仍拥有当前用户权限，且只保证直接子进程收敛。",
                    "验证产物在候选执行结束后重审并核对初始摘要；整个输出目录作为一个事务提交。",
                    "L2 只证明构建与既定门禁，不证明能力优于基线，也不构成批准、安装或发布。",
                ),
            )
            _commit_report_bundle(
                config.output,
                report,
                artifact=prepared_artifact,
                diagnostics=diagnostics if diagnostics.exists() else None,
            )
            return report

    def _validated_config(self) -> CandidateValidationConfig:
        candidate = Path(os.path.abspath(self.config.candidate))
        core = self.config.core_project.resolve()
        output = self.config.output.resolve()
        wheelhouse = (
            self.config.wheelhouse.resolve() if self.config.wheelhouse is not None else None
        )
        if not candidate.is_dir() or not core.is_dir():
            raise CandidateValidationConfigurationError(
                "Candidate and trusted core paths must be existing directories"
            )
        if candidate.resolve() == core or _paths_overlap(candidate.resolve(), core):
            raise CandidateValidationConfigurationError(
                "Candidate and trusted core directories must be disjoint"
            )
        if output.exists():
            raise CandidateValidationConfigurationError(
                "Validation output must be a new directory"
            )
        if _paths_overlap(output, candidate) or _paths_overlap(output, core):
            raise CandidateValidationConfigurationError(
                "Validation output must be outside candidate and core directories"
            )
        if self.config.allow_index == (wheelhouse is not None):
            raise CandidateValidationConfigurationError(
                "Choose exactly one dependency source: --allow-index or --wheelhouse"
            )
        if wheelhouse is not None and not wheelhouse.is_dir():
            raise CandidateValidationConfigurationError("Wheelhouse must be an existing directory")
        if wheelhouse is not None and _paths_overlap(wheelhouse, candidate):
            raise CandidateValidationConfigurationError(
                "Wheelhouse must be outside the candidate directory"
            )
        for value in (
            self.config.command_timeout_seconds,
            self.config.core_timeout_seconds,
        ):
            if not math.isfinite(value) or value <= 0:
                raise CandidateValidationConfigurationError(
                    "Validation timeouts must be finite positive numbers"
                )
        if len(self.config.test_requirements) > 20:
            raise CandidateValidationConfigurationError("Too many explicit test requirements")
        for raw in self.config.test_requirements:
            if len(raw) > 256:
                raise CandidateValidationConfigurationError("A test requirement is too long")
            try:
                requirement = Requirement(raw)
            except InvalidRequirement as error:
                raise CandidateValidationConfigurationError(
                    "An explicit test requirement is invalid"
                ) from error
            if requirement.url is not None:
                raise CandidateValidationConfigurationError(
                    "Direct-reference test requirements are not supported"
                )
        return CandidateValidationConfig(
            candidate=candidate,
            core_project=core,
            output=output,
            plugin_id=self.config.plugin_id,
            distribution=self.config.distribution,
            wheelhouse=wheelhouse,
            allow_index=self.config.allow_index,
            test_requirements=self.config.test_requirements,
            command_timeout_seconds=self.config.command_timeout_seconds,
            core_timeout_seconds=self.config.core_timeout_seconds,
        )

    async def _snapshot_core(
        self,
        config: CandidateValidationConfig,
        destination: Path,
        environment: dict[str, str],
    ) -> _TrustedCoreSnapshot | None:
        head_outcome = await self.runner.run(
            CommandRequest(
                "trusted-core-head",
                ("git", "-C", str(config.core_project), "rev-parse", "--verify", "HEAD"),
                config.core_project,
                environment,
                min(config.command_timeout_seconds, 30.0),
                capture_stdout=True,
            )
        )
        commit = head_outcome.stdout.lower()
        if head_outcome.exit_code != 0 or not _HEX_COMMIT.fullmatch(commit):
            self._record_from_outcome(
                "trusted-core-snapshot",
                head_outcome,
                passed_summary="",
                failed_code="trusted-core-head-unavailable",
                failed_summary="无法读取可信核心仓库的 Git HEAD。",
            )
            return None

        clone = await self.runner.run(
            CommandRequest(
                "trusted-core-clone",
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
        if clone.exit_code != 0 or clone.timed_out or clone.start_failed:
            self._record_from_outcome(
                "trusted-core-snapshot",
                clone,
                passed_summary="",
                failed_code="trusted-core-clone-failed",
                failed_summary="无法克隆可信核心提交。",
            )
            return None
        checkout = await self.runner.run(
            CommandRequest(
                "trusted-core-checkout",
                ("git", "-C", str(destination), "checkout", "--detach", commit),
                destination,
                environment,
                config.command_timeout_seconds,
            )
        )
        duration = (
            head_outcome.duration_seconds
            + clone.duration_seconds
            + checkout.duration_seconds
        )
        if checkout.exit_code != 0 or checkout.timed_out or checkout.start_failed:
            self._record_from_outcome(
                "trusted-core-snapshot",
                checkout,
                passed_summary="",
                failed_code="trusted-core-checkout-failed",
                failed_summary="无法检出可信核心提交。",
                extra_duration=head_outcome.duration_seconds + clone.duration_seconds,
            )
            return None
        try:
            version = _read_trusted_core_version(destination)
        except CandidateSourceError as error:
            self._record(
                "trusted-core-snapshot",
                "failed",
                error.code,
                "可信核心提交没有可静态读取的唯一版本。",
                duration,
            )
            return None
        self._record(
            "trusted-core-snapshot",
            "passed",
            "trusted-core-snapshot-passed",
            "核心回归输入及兼容性版本已固定为显式仓库的 Git HEAD。",
            duration,
            0,
        )
        return _TrustedCoreSnapshot(commit, version)

    async def _build_wheel(
        self,
        *,
        name: str,
        project: Path,
        destination: Path,
        environment: dict[str, str],
        config: CandidateValidationConfig,
    ) -> Path | None:
        _mkdir(destination)
        argv = [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--disable-pip-version-check",
            "--no-deps",
            "--wheel-dir",
            str(destination),
            *_dependency_source_args(config),
            str(project),
        ]
        outcome = await self.runner.run(
            CommandRequest(
                name,
                tuple(argv),
                project,
                environment,
                config.command_timeout_seconds,
            )
        )
        wheels = _wheel_files(destination) if outcome.exit_code == 0 else ()
        archive_invalid = len(wheels) != 1
        if len(wheels) == 1 and transient_wheel_members(wheels[0]):
            archive_invalid = True
        if archive_invalid:
            outcome = CommandOutcome(
                1,
                outcome.duration_seconds,
                timed_out=outcome.timed_out,
                start_failed=outcome.start_failed,
            )
        self._record_from_outcome(
            name,
            outcome,
            passed_summary=(
                "可信核心 Wheel 构建成功。"
                if name == "core-wheel-build"
                else "候选 Wheel 从干净源码副本构建成功。"
            ),
            failed_code=f"{name}-failed",
            failed_summary=(
                "可信核心 Wheel 构建失败。"
                if name == "core-wheel-build"
                else "候选 Wheel 构建失败。"
            ),
        )
        return wheels[0] if self._passed(name) else None

    async def _create_and_install_environment(
        self,
        *,
        check_name: str,
        root: Path,
        core_wheel: Path,
        candidate_wheel: Path,
        environment: dict[str, str],
        config: CandidateValidationConfig,
    ) -> Path | None:
        create = await self.runner.run(
            CommandRequest(
                f"{check_name}-create",
                (sys.executable, "-m", "venv", str(root)),
                root.parent,
                environment,
                config.command_timeout_seconds,
            )
        )
        python = _venv_python(root)
        if create.exit_code != 0:
            self._record_from_outcome(
                check_name,
                create,
                passed_summary="",
                failed_code="validation-venv-create-failed",
                failed_summary="无法创建独立验证虚拟环境。",
            )
            return None
        install = await self.runner.run(
            CommandRequest(
                f"{check_name}-install",
                (
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    *_dependency_source_args(config),
                    str(core_wheel),
                    str(candidate_wheel),
                    "pytest>=8.0",
                    "pytest-asyncio>=0.23",
                    *config.test_requirements,
                ),
                root.parent,
                environment,
                config.command_timeout_seconds,
            )
        )
        self._record_from_outcome(
            check_name,
            install,
            passed_summary="候选与可信核心已安装到独立临时虚拟环境。",
            failed_code="validation-environment-install-failed",
            failed_summary="独立验证虚拟环境安装失败。",
            extra_duration=create.duration_seconds,
        )
        return python if self._passed(check_name) else None

    async def _run_installed_checks(
        self,
        *,
        python: Path,
        identity: CandidateIdentity,
        candidate_copy: Path,
        control: Path,
        environment: dict[str, str],
        config: CandidateValidationConfig,
    ) -> None:
        contract = await self.runner.run(
            CommandRequest(
                "installed-metadata-contract",
                (
                    str(python),
                    str(control / "contract_probe.py"),
                    "--distribution",
                    identity.distribution,
                    "--version",
                    identity.version,
                    "--plugin-id",
                    identity.plugin_id,
                    "--entry-value",
                    identity.entry_value,
                ),
                control,
                environment,
                config.command_timeout_seconds,
            )
        )
        self._record_from_outcome(
            "installed-metadata-contract",
            contract,
            passed_summary="安装元数据、Entry Point 与宿主 discovery 合同一致。",
            failed_code="installed-metadata-contract-failed",
            failed_summary="安装后的 Distribution 或 Entry Point 合同不一致。",
        )

        doctor = await self.runner.run(
            CommandRequest(
                "plugin-doctor",
                (
                    str(python),
                    "-m",
                    "traceh.cli.main",
                    "plugins",
                    "doctor",
                    identity.plugin_id,
                    "--json",
                ),
                control,
                environment,
                config.command_timeout_seconds,
            )
        )
        self._record_from_outcome(
            "plugin-doctor",
            doctor,
            passed_summary="真实插件 discovery、setup、health check 与 dispose 通过。",
            failed_code="plugin-doctor-failed",
            failed_summary="插件 doctor 未通过。",
        )

        pytest_base = (
            str(python),
            "-m",
            "pytest",
            "-p",
            "pytest_asyncio.plugin",
            "-c",
            str(control / "pytest.ini"),
            "-o",
            "addopts=",
            "--tb=no",
            str(candidate_copy / "tests"),
        )
        collection = await self.runner.run(
            CommandRequest(
                "candidate-test-collection",
                (*pytest_base, "--collect-only", "-q"),
                control,
                environment,
                config.command_timeout_seconds,
            )
        )
        self._record_from_outcome(
            "candidate-test-collection",
            collection,
            passed_summary="候选测试由宿主 pytest 配置成功收集，且不是空测试集。",
            failed_code="candidate-test-collection-failed",
            failed_summary="候选测试无法收集或没有测试。",
        )
        tests = await self.runner.run(
            CommandRequest(
                "candidate-tests",
                (*pytest_base, "-q"),
                control,
                environment,
                config.command_timeout_seconds,
            )
        )
        self._record_from_outcome(
            "candidate-tests",
            tests,
            passed_summary="候选自带测试在已安装 Wheel 上通过。",
            failed_code="candidate-tests-failed",
            failed_summary="候选自带测试未通过。",
        )

    async def _run_core_regression(
        self,
        *,
        python: Path,
        trusted_core: Path,
        environment: dict[str, str],
        config: CandidateValidationConfig,
        diagnostics: Path,
    ) -> None:
        outcome = await self.runner.run(
            CommandRequest(
                "core-regression",
                (
                    str(python),
                    "-m",
                    "pytest",
                    "-p",
                    "pytest_asyncio.plugin",
                    "-c",
                    str(trusted_core / "pyproject.toml"),
                    "-o",
                    "addopts=",
                    "--tb=short",
                    "-q",
                    str(trusted_core / "tests"),
                ),
                trusted_core,
                environment,
                config.core_timeout_seconds,
                capture_stdout=True,
                stdout_limit=32_768,
            )
        )
        if outcome.exit_code not in (None, 0) and outcome.stdout:
            _mkdir(diagnostics)
            _atomic_text(diagnostics / "core-regression.txt", outcome.stdout + "\n")
        self._record_from_outcome(
            "core-regression",
            outcome,
            passed_summary="候选已安装但未启用时，可信核心完整回归通过。",
            failed_code="core-regression-failed",
            failed_summary="可信核心回归未通过。",
        )

    def _record_from_outcome(
        self,
        name: str,
        outcome: CommandOutcome,
        *,
        passed_summary: str,
        failed_code: str,
        failed_summary: str,
        extra_duration: float = 0.0,
    ) -> None:
        duration = outcome.duration_seconds + extra_duration
        if outcome.exit_code == 0 and not outcome.timed_out and not outcome.start_failed:
            self._record(name, "passed", f"{name}-passed", passed_summary, duration, 0)
            return
        code = failed_code
        if outcome.timed_out:
            code = f"{name}-timed-out"
        elif outcome.start_failed:
            code = f"{name}-start-failed"
        self._record(
            name,
            "failed",
            code,
            failed_summary,
            duration,
            outcome.exit_code,
        )

    def _record(
        self,
        name: str,
        status: str,
        code: str,
        summary: str,
        duration: float,
        exit_code: int | None = None,
    ) -> None:
        self._checks[name] = ValidationCheck(
            name=name,
            status=status,
            code=code,
            summary_zh=summary,
            duration_seconds=round(max(duration, 0.0), 3),
            exit_code=exit_code,
        )

    def _passed(self, name: str) -> bool:
        return self._checks.get(name, ValidationCheck("", "", "", "", 0)).status == "passed"

    def _final_checks(self) -> tuple[ValidationCheck, ...]:
        for name in _CHECK_ORDER:
            if name not in self._checks:
                self._record(
                    name,
                    "blocked",
                    f"{name}-blocked",
                    "前置门禁未通过，本项未执行。",
                    0.0,
                )
        return tuple(self._checks[name] for name in _CHECK_ORDER)


def _read_candidate_identity(
    candidate: Path,
    *,
    requested_plugin_id: str | None,
    requested_distribution: str | None,
    trusted_core_version: Version,
) -> CandidateIdentity:
    if (candidate / "src" / "traceh" / "runtime" / "agent_runtime.py").exists():
        raise CandidateSourceError(
            "candidate-is-core-repository",
            "The candidate workspace is the TraceHarness core repository",
        )
    pyproject = candidate / "pyproject.toml"
    if not pyproject.is_file() or pyproject.stat().st_size > 1024 * 1024:
        raise CandidateSourceError(
            "candidate-pyproject-invalid",
            "Candidate pyproject.toml is missing or too large",
        )
    try:
        raw = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise CandidateSourceError(
            "candidate-pyproject-invalid",
            "Candidate pyproject.toml cannot be parsed",
        ) from error
    project = raw.get("project")
    if not isinstance(project, dict):
        raise CandidateSourceError(
            "candidate-project-metadata-missing",
            "Candidate project metadata is missing",
        )
    build_system = raw.get("build-system")
    build_requirements = (
        build_system.get("requires") if isinstance(build_system, dict) else None
    )
    if not isinstance(build_requirements, list) or not build_requirements:
        raise CandidateSourceError(
            "candidate-build-system-invalid",
            "Candidate build-system requirements are missing",
        )
    _parse_candidate_requirements(build_requirements)
    distribution = project.get("name")
    version = project.get("version")
    if (
        not isinstance(distribution, str)
        or _DISTRIBUTION_NAME.fullmatch(distribution) is None
    ):
        raise CandidateSourceError(
            "candidate-distribution-invalid",
            "Candidate distribution name is invalid",
        )
    if canonicalize_name(distribution) == canonicalize_name(DISTRIBUTION_NAME):
        raise CandidateSourceError(
            "candidate-is-core-distribution",
            "Candidate cannot claim the TraceHarness core distribution",
        )
    if requested_distribution is not None and canonicalize_name(distribution) != canonicalize_name(
        requested_distribution
    ):
        raise CandidateSourceError(
            "candidate-distribution-mismatch",
            "Candidate distribution does not match the explicit selection",
        )
    if not isinstance(version, str) or len(version) > 128:
        raise CandidateSourceError(
            "candidate-version-invalid",
            "Candidate version is invalid",
        )
    try:
        Version(version)
    except InvalidVersion as error:
        raise CandidateSourceError(
            "candidate-version-invalid",
            "Candidate version is not valid PEP 440",
        ) from error

    entry_points = project.get("entry-points")
    plugin_points = entry_points.get("traceh.plugins") if isinstance(entry_points, dict) else None
    if not isinstance(plugin_points, dict) or not plugin_points:
        raise CandidateSourceError(
            "candidate-entry-point-missing",
            "Candidate does not declare a traceh.plugins Entry Point",
        )
    if requested_plugin_id is None:
        if len(plugin_points) != 1:
            raise CandidateSourceError(
                "candidate-plugin-id-ambiguous",
                "Candidate plugin id is ambiguous and must be explicit",
            )
        plugin_id = next(iter(plugin_points))
    else:
        plugin_id = requested_plugin_id
    if not isinstance(plugin_id, str) or not is_plugin_id(plugin_id):
        raise CandidateSourceError(
            "candidate-plugin-id-invalid",
            "Candidate plugin id is invalid",
        )
    entry_value = plugin_points.get(plugin_id)
    if not isinstance(entry_value, str) or len(entry_value) > 256:
        raise CandidateSourceError(
            "candidate-entry-point-invalid",
            "Candidate Entry Point value is invalid",
        )
    match = _ENTRY_VALUE.fullmatch(entry_value)
    if match is None:
        raise CandidateSourceError(
            "candidate-entry-point-invalid",
            "Candidate Entry Point value is invalid",
        )

    dependencies = project.get("dependencies")
    if not isinstance(dependencies, list):
        raise CandidateSourceError(
            "candidate-traceh-dependency-missing",
            "Candidate does not declare its TraceHarness dependency",
        )
    parsed = _parse_candidate_requirements(dependencies)
    traceh_requirements = [
        requirement
        for requirement in parsed
        if canonicalize_name(requirement.name) == canonicalize_name(DISTRIBUTION_NAME)
        and (requirement.marker is None or requirement.marker.evaluate())
    ]
    if len(traceh_requirements) != 1:
        raise CandidateSourceError(
            "candidate-traceh-dependency-invalid",
            "Candidate must declare exactly one active TraceHarness dependency",
        )
    if trusted_core_version not in traceh_requirements[0].specifier:
        raise CandidateSourceError(
            "candidate-traceh-dependency-incompatible",
            "Candidate dependency does not accept the selected trusted core version",
        )

    review_document = candidate / "CANDIDATE.md"
    if not review_document.is_file() or review_document.stat().st_size > 1024 * 1024:
        raise CandidateSourceError(
            "candidate-review-card-missing",
            "Candidate review card is missing",
        )
    try:
        review_text = review_document.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise CandidateSourceError(
            "candidate-review-card-invalid",
            "Candidate review card cannot be read as UTF-8",
        ) from error
    if "UNVALIDATED (L1 SOURCE ONLY)" not in review_text:
        raise CandidateSourceError(
            "candidate-review-card-status-invalid",
            "Candidate review card does not carry the L1 source-only marker",
        )
    tests = candidate / "tests"
    if not tests.is_dir() or not any(tests.rglob("test*.py")):
        raise CandidateSourceError(
            "candidate-tests-missing",
            "Candidate does not contain a test module",
        )
    return CandidateIdentity(
        distribution=distribution,
        version=version,
        plugin_id=plugin_id,
        entry_value=entry_value,
        entry_module=match.group("module"),
    )


def _read_trusted_core_version(core: Path) -> Version:
    """Read the selected core's version without importing trusted checkout code."""

    source = core / "src" / "traceh" / "version.py"
    try:
        if not source.is_file() or source.stat().st_size > 1024 * 1024:
            raise OSError
        module = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    except (OSError, UnicodeError, SyntaxError) as error:
        raise CandidateSourceError(
            "trusted-core-version-unavailable",
            "Trusted core version source cannot be parsed",
        ) from error
    values: list[str] = []
    for statement in module.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target = statement.targets[0]
            value = statement.value
        elif isinstance(statement, ast.AnnAssign):
            target = statement.target
            value = statement.value
        if (
            isinstance(target, ast.Name)
            and target.id == "__version__"
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            values.append(value.value)
    if len(values) != 1:
        raise CandidateSourceError(
            "trusted-core-version-unavailable",
            "Trusted core must declare one literal __version__",
        )
    try:
        return Version(values[0])
    except InvalidVersion as error:
        raise CandidateSourceError(
            "trusted-core-version-invalid",
            "Trusted core version is not valid PEP 440",
        ) from error


def _parse_candidate_requirements(raw_requirements: list[object]) -> list[Requirement]:
    parsed: list[Requirement] = []
    for dependency in raw_requirements:
        if not isinstance(dependency, str):
            raise CandidateSourceError(
                "candidate-dependency-invalid",
                "Candidate contains an invalid dependency",
            )
        try:
            requirement = Requirement(dependency)
        except InvalidRequirement as error:
            raise CandidateSourceError(
                "candidate-dependency-invalid",
                "Candidate contains an invalid dependency",
            ) from error
        if requirement.url is not None:
            raise CandidateSourceError(
                "candidate-direct-reference-rejected",
                "Candidate dependencies cannot use direct references",
            )
        parsed.append(requirement)
    return parsed


def _dependency_source_args(config: CandidateValidationConfig) -> tuple[str, ...]:
    if config.wheelhouse is None:
        return ()
    return ("--no-index", "--find-links", str(config.wheelhouse))


def _apply_dependency_environment(
    environment: dict[str, str],
    config: CandidateValidationConfig,
) -> None:
    """Keep nested candidate/core pip calls on the selected dependency source."""

    if config.wheelhouse is None:
        return
    apply_wheelhouse_environment(environment, config.wheelhouse)


def _resolved_path(raw: str) -> Path:
    return Path(raw).resolve()


def _contract_probe_source() -> Path:
    return Path(__file__).with_name("contract_probe.py")


def _mkdir(path: Path) -> None:
    path.mkdir()


def _capture_audited_wheel(
    wheel: Path,
    destination: Path,
    *,
    entry_module: str,
) -> _AuditedWheel:
    """Anchor the audited bytes in host memory before candidate execution."""

    content = wheel.read_bytes()
    destination.mkdir()
    snapshot = destination / wheel.name
    snapshot.write_bytes(content)
    issues = audit_candidate_wheel(snapshot, entry_module=entry_module)
    if issues:
        raise ArtifactContractError(
            issues[0],
            "Candidate Wheel failed the host-controlled audit",
        )
    return _AuditedWheel(
        source_path=wheel,
        path=snapshot,
        sha256=sha256_file(snapshot),
        entry_module=entry_module,
        content=content,
    )


def _prepare_validated_artifact(
    audited: _AuditedWheel,
    destination: Path,
) -> _PreparedArtifact:
    """Re-prove and copy the exact Wheel that passed the initial audit."""

    for path in (audited.source_path, audited.path):
        issues = audit_candidate_wheel(path, entry_module=audited.entry_module)
        if issues:
            raise ArtifactContractError(
                issues[0],
                "Candidate Wheel changed after its initial audit",
            )
        if sha256_file(path) != audited.sha256:
            raise ArtifactContractError(
                "validated-artifact-identity-changed",
                "Candidate Wheel bytes changed after their initial audit",
            )
    destination.mkdir()
    artifact_path = destination / audited.path.name
    partial = artifact_path.with_suffix(artifact_path.suffix + ".partial")
    try:
        partial.write_bytes(audited.content)
        digest = sha256_file(partial)
        issues = audit_candidate_wheel(partial, entry_module=audited.entry_module)
        if issues:
            raise ArtifactContractError(
                issues[0],
                "Copied candidate Wheel failed its final audit",
            )
        if digest != audited.sha256:
            raise ArtifactContractError(
                "validated-artifact-identity-changed",
                "Copied candidate Wheel does not match the audited bytes",
            )
        size_bytes = partial.stat().st_size
        os.replace(partial, artifact_path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return _PreparedArtifact(
        artifact_path,
        ValidatedArtifact(
            filename=f"artifacts/{artifact_path.name}",
            sha256=digest,
            size_bytes=size_bytes,
        ),
        audited.entry_module,
        audited.content,
    )


def _wheel_files(path: Path) -> tuple[Path, ...]:
    return tuple(path.glob("*.whl"))


def _isolated_environment(root: Path) -> dict[str, str]:
    environment = sanitized_environment()
    # The general Shell child environment preserves PYTHONPATH for ordinary
    # user commands.  L2 must be stricter: a validation venv may only import
    # its installed Wheels, never an ambient host checkout.
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
            "TRACEH_CANDIDATE_VALIDATION": "1",
        }
    )
    return environment


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _commit_report_bundle(
    output: Path,
    report: CandidateValidationReport,
    *,
    artifact: _PreparedArtifact | None,
    diagnostics: Path | None,
) -> None:
    """Atomically expose one complete report/artifact directory.

    All visible files are first written to a sibling directory on the same
    filesystem.  A report failure, artifact failure or interrupted rename
    therefore leaves the requested output path absent rather than half-valid.
    """

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{output.name}-",
            dir=output.parent,
        )
    )
    try:
        if artifact is not None:
            artifact_dir = staging / "artifacts"
            artifact_dir.mkdir()
            staged_artifact = artifact_dir / artifact.path.name
            staged_artifact.write_bytes(artifact.content)
            if sha256_file(staged_artifact) != artifact.metadata.sha256:
                raise ArtifactContractError(
                    "validated-artifact-identity-changed",
                    "Final output artifact does not match its audited digest",
                )
            issues = audit_candidate_wheel(
                staged_artifact,
                entry_module=artifact.entry_module,
            )
            if issues:
                raise ArtifactContractError(
                    issues[0],
                    "Final output artifact failed its commit-time audit",
                )
        if diagnostics is not None:
            shutil.copytree(diagnostics, staging / "diagnostics")
        _write_report(staging, report)
        os.replace(staging, output)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _write_report(output: Path, report: CandidateValidationReport) -> None:
    payload = json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _atomic_text(output / "report.json", payload)
    lines = [
        "# TraceHarness 插件候选 L2 验证报告",
        "",
        f"结论：**{'通过' if report.ok else '未通过'}**",
        f"可信核心提交：`{report.core_commit or '未取得'}`",
    ]
    if report.candidate is not None:
        lines.extend(
            [
                f"候选：`{report.candidate.plugin_id}` / "
                f"`{report.candidate.distribution}=={report.candidate.version}`",
            ]
        )
    if report.artifact is not None:
        lines.extend(
            [
                f"候选制品：`{report.artifact.filename}`",
                f"SHA-256：`{report.artifact.sha256}`",
            ]
        )
    lines.extend(
        [
            "",
            "| 门禁 | 状态 | 固定代码 | 摘要 | 耗时 |",
            "|---|---|---|---|---:|",
        ]
    )
    for check in report.checks:
        lines.append(
            f"| `{check.name}` | {check.status} | `{check.code}` | "
            f"{check.summary_zh} | {check.duration_seconds:.3f}s |"
        )
    lines.extend(["", "## 边界", ""])
    lines.extend(f"- {item}" for item in report.boundaries_zh)
    _atomic_text(output / "report.md", "\n".join(lines) + "\n")


def _atomic_text(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


__all__ = [
    "CandidateIdentity",
    "CandidateSourceError",
    "CandidateValidationConfig",
    "CandidateValidationConfigurationError",
    "CandidateValidationReport",
    "CandidateValidator",
    "CommandOutcome",
    "CommandRequest",
    "CommandRunner",
    "SubprocessCommandRunner",
    "VALIDATION_CHECK_ORDER",
    "ValidatedArtifact",
    "ValidationCheck",
    "VALIDATION_EXIT_CODE",
]
