from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from urllib.request import url2pathname

import pytest

import traceh.evolution.candidate_comparison as comparison_module
import traceh.evolution.comparison_probe as probe_module
from traceh.api.events import EventEnvelope, PendingEvent
from traceh.cli.main import build_parser
from traceh.evolution.artifacts import apply_wheelhouse_environment
from traceh.evolution.candidate_comparison import (
    CandidateComparator,
    CandidateComparisonConfig,
    CandidateComparisonEvidenceError,
)
from traceh.evolution.candidate_validation import (
    VALIDATION_CHECK_ORDER,
    CommandOutcome,
    CommandRequest,
)
from traceh.tools.builtins.shell import sanitized_environment

_COMMIT = "b" * 40


def _write_wheel(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("review_plugin/__init__.py", "VALUE = 1\n")
        archive.writestr(
            "traceh_review_plugin-0.1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: traceh-review-plugin\nVersion: 0.1.0\n",
        )
        archive.writestr(
            "traceh_review_plugin-0.1.0.dist-info/WHEEL", "Wheel-Version: 1.0\n"
        )
        archive.writestr("traceh_review_plugin-0.1.0.dist-info/RECORD", "")
    return path


def _write_evidence(root: Path, *, digest_override: str | None = None) -> Path:
    wheel = _write_wheel(
        root / "artifacts" / "traceh_review_plugin-0.1.0-py3-none-any.whl"
    )
    digest = comparison_module.sha256_file(wheel)
    report = {
        "schema_version": 1,
        "ok": True,
        "core_commit": _COMMIT,
        "candidate": {
            "distribution": "traceh-review-plugin",
            "version": "0.1.0",
            "plugin_id": "review.plugin",
            "entry_value": "review_plugin:ReviewPlugin",
            "entry_module": "review_plugin",
        },
        "artifact": {
            "filename": f"artifacts/{wheel.name}",
            "sha256": digest_override or digest,
            "size_bytes": wheel.stat().st_size,
        },
        "checks": [
            {"name": name, "status": "passed", "code": f"{name}-passed"}
            for name in VALIDATION_CHECK_ORDER
        ],
        "boundaries_zh": [],
    }
    (root / "report.json").write_text(json.dumps(report), encoding="utf-8")
    return root


def _write_suite(root: Path) -> Path:
    initial = root / "case" / "initial"
    initial.mkdir(parents=True)
    (initial / "project.txt").write_text("fixed host input\n", encoding="utf-8")
    (root / "case" / "script.json").write_text(
        '[{"content": "done", "finish_reason": "stop"}]\n',
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 1,
        "suite_id": "review.plugin.v1",
        "target_plugin_id": "review.plugin",
        "cases": [
            {
                "id": "capability",
                "task": "Use the reviewed capability.",
                "initial_dir": "case/initial",
                "script": "case/script.json",
                "baseline_verifier": {
                    "kind": "command",
                    "argv": ["{python}", "-c", "raise SystemExit(0)"],
                },
                "candidate_verifier": {
                    "kind": "plugin",
                    "name": "review-verifier",
                },
                "expected": {
                    "reason": "completed",
                    "verification_passed": True,
                    "tool_results": [
                        {"tool_name": "review_tool", "status": "succeeded", "policy": None}
                    ],
                },
            }
        ],
    }
    (root / "suite.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def _facts(arm: str, *, passes: bool) -> dict[str, object]:
    return {
        "schema_version": 1,
        "arm": arm,
        "cases": [
            {
                "case_id": "capability",
                "completed": True,
                "evidence_complete": True,
                "plugin_identity_matches": True,
                "reason": "completed",
                "steps": 2,
                "model_attempts": 2,
                "tool_calls": 1 if passes else 0,
                "tool_results": (
                    [
                        {
                            "tool_name": "review_tool",
                            "status": "succeeded",
                            "policy": None,
                        }
                    ]
                    if passes
                    else []
                ),
                "verification_passed": True,
                "verification_exit_code": 0,
                "invariant_violations": 0,
                "reconstruction_violations": 0,
                "duration_seconds": 0.01,
            }
        ],
    }


class ComparisonRunner:
    def __init__(
        self,
        suite: Path,
        *,
        baseline_passes: bool = False,
        candidate_passes: bool = True,
    ) -> None:
        self.suite = suite
        self.baseline_passes = baseline_passes
        self.candidate_passes = candidate_passes
        self.requests: list[CommandRequest] = []

    async def run(self, request: CommandRequest) -> CommandOutcome:
        self.requests.append(request)
        if request.purpose == "comparison-core-clone":
            destination = Path(request.argv[-1])
            _copy_suite_for_fake_clone(self.suite, destination)
        elif request.purpose == "comparison-core-head":
            return CommandOutcome(0, 0.01, stdout=_COMMIT)
        elif request.purpose == "comparison-core-wheel-build":
            destination = Path(request.argv[request.argv.index("--wheel-dir") + 1])
            _write_core_wheel(destination / "traceharness_py-0.5.0-py3-none-any.whl")
        elif request.purpose == "comparison-dependency-freeze":
            destination = Path(request.argv[request.argv.index("--dest") + 1])
            for raw in request.argv:
                source = Path(raw)
                if source.suffix.casefold() == ".whl":
                    shutil.copy2(source, destination / source.name)
        elif request.purpose.endswith("-create"):
            environment_root = Path(request.argv[-1])
            python = _venv_python(environment_root)
            python.parent.mkdir(parents=True)
            python.write_text("", encoding="utf-8")
            site_packages = (
                environment_root / "Lib" / "site-packages"
                if sys.platform == "win32"
                else environment_root / "lib" / "python3.12" / "site-packages"
            )
            dist_info = site_packages / "pip-1.0.dist-info"
            dist_info.mkdir(parents=True)
            (dist_info / "METADATA").write_text(
                "Metadata-Version: 2.1\nName: pip\nVersion: 1.0\n",
                encoding="utf-8",
            )
        elif request.purpose in {"comparison-baseline", "comparison-candidate"}:
            arm = request.purpose.removeprefix("comparison-")
            result = Path(request.argv[request.argv.index("--output") + 1])
            _write_fake_facts(
                result,
                _facts(
                    arm,
                    passes=(
                        self.baseline_passes if arm == "baseline" else self.candidate_passes
                    ),
                ),
            )
        return CommandOutcome(0, 0.01)


class CancellationRunner(ComparisonRunner):
    def __init__(self, suite: Path) -> None:
        super().__init__(suite)
        self.candidate_entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancellations = 0

    async def run(self, request: CommandRequest) -> CommandOutcome:
        if request.purpose != "comparison-candidate":
            return await super().run(request)
        self.requests.append(request)
        self.candidate_entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError as cancellation:
            self.cancellations += 1
            while not self.release.is_set():
                try:
                    await asyncio.shield(self.release.wait())
                except asyncio.CancelledError:
                    self.cancellations += 1
            raise cancellation
        raise AssertionError("candidate gate must be cancelled in this test")


class FrozenDependencyMutationRunner(ComparisonRunner):
    async def run(self, request: CommandRequest) -> CommandOutcome:
        outcome = await super().run(request)
        if request.purpose == "comparison-candidate":
            wheelhouse = Path(
                url2pathname(urlsplit(request.env["PIP_FIND_LINKS"]).path)
            )
            _write_wheel(wheelhouse / "late_dependency-1.0-py3-none-any.whl")
        return outcome


class ReceiptMismatchRunner(ComparisonRunner):
    async def run(self, request: CommandRequest) -> CommandOutcome:
        outcome = await super().run(request)
        if request.purpose == "candidate-environment-install":
            environment_root = request.cwd / "candidate-environment"
            site_packages = (
                environment_root / "Lib" / "site-packages"
                if sys.platform == "win32"
                else environment_root / "lib" / "python3.12" / "site-packages"
            )
            dist_info = site_packages / "different-1.0.dist-info"
            dist_info.mkdir(parents=True)
            (dist_info / "METADATA").write_text(
                "Metadata-Version: 2.1\nName: different\nVersion: 1.0\n",
                encoding="utf-8",
            )
        return outcome


def _write_core_wheel(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("traceh/__init__.py", "")
        archive.writestr("traceharness_py-0.5.0.dist-info/METADATA", "Metadata-Version: 2.1\n")
        archive.writestr("traceharness_py-0.5.0.dist-info/WHEEL", "Wheel-Version: 1.0\n")
        archive.writestr("traceharness_py-0.5.0.dist-info/RECORD", "")


def _copy_suite_for_fake_clone(suite: Path, destination: Path) -> None:
    destination.mkdir(parents=True)
    shutil.copytree(
        suite,
        destination / "benchmarks" / "evolution" / "review_v1",
    )


def _write_fake_facts(path: Path, facts: dict[str, object]) -> None:
    path.write_text(json.dumps(facts), encoding="utf-8")


def _venv_python(root: Path) -> Path:
    return root / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def _config(tmp_path: Path, suite: Path) -> CandidateComparisonConfig:
    evidence = _write_evidence(tmp_path / "evidence")
    core = tmp_path / "core"
    core.mkdir()
    return CandidateComparisonConfig(
        validation_evidence=evidence,
        core_project=core,
        suite=Path("benchmarks/evolution/review_v1"),
        output=tmp_path / "comparison",
        allow_index=True,
    )


def _write_probe_case(root: Path, *, verifier_program: str = "{python}") -> dict[str, object]:
    initial = root / "initial"
    initial.mkdir(parents=True)
    (initial / "project.txt").write_text("probe input\n", encoding="utf-8")
    (root / "script.json").write_text(
        '[{"content": "done", "finish_reason": "stop"}]\n',
        encoding="utf-8",
    )
    return {
        "id": "probe-case",
        "task": "Finish with verifier evidence.",
        "initial_dir": "initial",
        "script": "script.json",
        "baseline_verifier": {
            "kind": "command",
            "argv": [verifier_program, "-c", "raise SystemExit(0)"],
        },
        "candidate_verifier": {"kind": "plugin", "name": "unused-verifier"},
        "max_verification_retries": 0,
    }


def _event(seq: int, event_type: str, data: dict[str, object]) -> EventEnvelope:
    return EventEnvelope.materialize("session", seq, PendingEvent(event_type, data))


class _ProbeSessions:
    def __init__(self, events: tuple[EventEnvelope, ...]) -> None:
        self.events = events

    async def read_session(self, session_id: str) -> tuple[EventEnvelope, ...]:
        del session_id
        return self.events

    async def read_effects(self, session_id: str) -> tuple[object, ...]:
        del session_id
        return ()


class _ProbeRuntime:
    def __init__(
        self,
        events: tuple[EventEnvelope, ...],
        *,
        reason: str = "completed",
        steps: int = 1,
    ) -> None:
        self.sessions = _ProbeSessions(events)
        self.invariants = SimpleNamespace(check=lambda events, effects: ())
        self.surface = object()
        self._reason = reason
        self._steps = steps
        self.disposed = False

    async def create_session(self, workspace: Path, metadata: object) -> str:
        del workspace, metadata
        return "session"

    async def run_existing(self, session_id: str, task: str) -> object:
        del session_id, task
        return SimpleNamespace(turn_id="turn", reason=self._reason, steps=self._steps)

    async def dispose(self) -> None:
        self.disposed = True


async def _no_reconstruction_issues(
    *args: object, **kwargs: object
) -> tuple[object, ...]:
    del args, kwargs
    return ()


@pytest.mark.asyncio
async def test_comparator_consumes_exact_l2_artifact_and_reports_improvement(
    tmp_path: Path,
) -> None:
    suite = _write_suite(tmp_path / "suite")
    config = _config(tmp_path, suite)
    runner = ComparisonRunner(suite)

    report = await CandidateComparator(config, runner=runner).run()

    assert report.classification == "improved"
    assert report.improvements == ("capability",)
    assert report.regressions == ()
    assert report.baseline.successful_cases == 0
    assert report.candidate_summary.successful_cases == 1
    assert report.artifact.sha256 == comparison_module.sha256_file(
        config.validation_evidence / report.artifact.filename
    )
    assert json.loads((config.output / "report.json").read_text(encoding="utf-8"))[
        "classification"
    ] == "improved"
    assert not any(
        request.purpose == "candidate-wheel-build" for request in runner.requests
    )
    installs = [request for request in runner.requests if request.purpose.endswith("-install")]
    freezes = [
        request
        for request in runner.requests
        if request.purpose == "comparison-dependency-freeze"
    ]
    artifact = str(config.validation_evidence / report.artifact.filename)
    assert len(installs) == 2
    assert len(freezes) == 1
    assert report.dependency_artifacts
    assert report.environment_receipt == (
        comparison_module.InstalledDistribution("pip", "1.0"),
    )
    assert all("--no-index" in request.argv for request in installs)
    assert all("--only-binary=:all:" in request.argv for request in installs)
    arm_requests = [
        request
        for request in runner.requests
        if request.purpose in {"comparison-baseline", "comparison-candidate"}
    ]
    assert all(request.env.get("PIP_NO_INDEX") == "1" for request in arm_requests)
    assert len({request.env.get("PIP_FIND_LINKS") for request in arm_requests}) == 1
    wheelhouse_uri = arm_requests[0].env["PIP_FIND_LINKS"]
    assert wheelhouse_uri.startswith("file:///")
    assert len(wheelhouse_uri.split()) == 1
    assert all(artifact not in request.argv for request in installs)
    artifact_name = report.artifact.filename.rsplit("/", 1)[-1]
    assert all(
        any(argument.endswith(artifact_name) for argument in request.argv)
        for request in installs
    )


def test_wheelhouse_uri_with_spaces_is_one_real_pip_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheelhouse = tmp_path / "wheel house"
    _write_wheel(wheelhouse / "traceh_review_plugin-0.1.0-py3-none-any.whl")
    policy: dict[str, str] = {}
    apply_wheelhouse_environment(policy, wheelhouse)
    for key, value in policy.items():
        monkeypatch.setenv(key, value)

    environment = sanitized_environment()
    destination = tmp_path / "download"
    destination.mkdir()
    completed = subprocess.run(
        (
            sys.executable,
            "-m",
            "pip",
            "download",
            "--disable-pip-version-check",
            "--no-deps",
            "--dest",
            str(destination),
            "traceh-review-plugin==0.1.0",
        ),
        check=False,
        capture_output=True,
        env=environment,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert (destination / "traceh_review_plugin-0.1.0-py3-none-any.whl").is_file()


@pytest.mark.asyncio
async def test_comparator_rejects_different_installed_distribution_receipts(
    tmp_path: Path,
) -> None:
    suite = _write_suite(tmp_path / "suite")
    config = _config(tmp_path, suite)

    with pytest.raises(CandidateComparisonEvidenceError) as caught:
        await CandidateComparator(
            config,
            runner=ReceiptMismatchRunner(suite),
        ).run()

    assert caught.value.code == "comparison-environment-receipt-mismatch"
    assert not config.output.exists()


@pytest.mark.asyncio
async def test_comparator_rechecks_the_frozen_dependency_set_after_candidate_code(
    tmp_path: Path,
) -> None:
    suite = _write_suite(tmp_path / "suite")
    config = _config(tmp_path, suite)

    with pytest.raises(CandidateComparisonEvidenceError) as caught:
        await CandidateComparator(
            config,
            runner=FrozenDependencyMutationRunner(suite),
        ).run()

    assert caught.value.code == "comparison-dependency-set-changed"
    assert not config.output.exists()


@pytest.mark.asyncio
async def test_probe_uses_the_real_runtime_and_collects_persisted_evidence(
    tmp_path: Path,
) -> None:
    suite = tmp_path / "probe-suite"
    case = _write_probe_case(suite)

    facts = await probe_module._run_case(
        suite=suite,
        run_root=tmp_path / "probe-run",
        case=case,
        arm="baseline",
        plugin_id="unused.plugin",
        plugin_version="1.0",
    )

    assert facts["completed"] is True
    assert facts["evidence_complete"] is True
    assert facts["verification_passed"] is True
    assert facts["invariant_violations"] == 0
    assert facts["reconstruction_violations"] == 0


@pytest.mark.asyncio
async def test_probe_runtime_failure_does_not_claim_missing_evidence_is_clean(
    tmp_path: Path,
) -> None:
    suite = tmp_path / "probe-suite"
    case = _write_probe_case(suite, verifier_program="missing-l3-verifier-fixture")

    facts = await probe_module._run_case(
        suite=suite,
        run_root=tmp_path / "probe-run",
        case=case,
        arm="baseline",
        plugin_id="unused.plugin",
        plugin_version="1.0",
    )

    assert facts["completed"] is False
    assert facts["evidence_complete"] is True
    assert facts["reason"] == "failed"


@pytest.mark.asyncio
async def test_probe_rejects_a_normal_return_without_a_durable_turn_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite = tmp_path / "probe-suite"
    case = _write_probe_case(suite)
    runtime = _ProbeRuntime(
        (
            _event(1, "session/created", {"workspace": str(tmp_path), "metadata": {}}),
            _event(2, "turn/start", {"turn_id": "turn"}),
            _event(3, "step/start", {"turn_id": "turn", "step_id": "step"}),
            _event(
                4,
                "composition/snapshot",
                {"plugins": [{"plugin_id": "traceh.core", "version": "0.5.0"}]},
            ),
        )
    )

    async def build(*args: object, **kwargs: object) -> _ProbeRuntime:
        del args, kwargs
        return runtime

    monkeypatch.setattr(probe_module, "build_default_runtime_async", build)
    monkeypatch.setattr(
        probe_module,
        "verify_request_snapshots",
        _no_reconstruction_issues,
    )

    facts = await probe_module._run_case(
        suite=suite,
        run_root=tmp_path / "probe-run",
        case=case,
        arm="baseline",
        plugin_id="unused.plugin",
        plugin_version="1.0",
    )

    assert facts["completed"] is True
    assert facts["evidence_complete"] is False
    assert facts["reason"] == "runtime_error"
    assert runtime.disposed is True


@pytest.mark.asyncio
async def test_probe_uses_the_durable_reason_and_rejects_run_result_disagreement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite = tmp_path / "probe-suite"
    case = _write_probe_case(suite)
    runtime = _ProbeRuntime(
        (
            _event(1, "session/created", {"workspace": str(tmp_path), "metadata": {}}),
            _event(2, "turn/start", {"turn_id": "turn"}),
            _event(3, "step/start", {"turn_id": "turn", "step_id": "step"}),
            _event(
                4,
                "composition/snapshot",
                {"plugins": [{"plugin_id": "traceh.core", "version": "0.5.0"}]},
            ),
            _event(5, "step/end", {"turn_id": "turn", "step_id": "step"}),
            _event(6, "turn/end", {"turn_id": "turn", "reason": "failed", "steps": 1}),
        ),
        reason="completed",
    )

    async def build(*args: object, **kwargs: object) -> _ProbeRuntime:
        del args, kwargs
        return runtime

    monkeypatch.setattr(probe_module, "build_default_runtime_async", build)
    monkeypatch.setattr(
        probe_module,
        "verify_request_snapshots",
        _no_reconstruction_issues,
    )

    facts = await probe_module._run_case(
        suite=suite,
        run_root=tmp_path / "probe-run",
        case=case,
        arm="baseline",
        plugin_id="unused.plugin",
        plugin_version="1.0",
    )

    assert facts["evidence_complete"] is False
    assert facts["reason"] == "failed"
    assert facts["steps"] == 1


@pytest.mark.asyncio
async def test_probe_rejects_a_candidate_arm_without_the_persisted_plugin_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite = tmp_path / "probe-suite"
    case = _write_probe_case(suite)
    runtime = _ProbeRuntime(
        (
            _event(1, "session/created", {"workspace": str(tmp_path), "metadata": {}}),
            _event(2, "turn/start", {"turn_id": "turn"}),
            _event(3, "step/start", {"turn_id": "turn", "step_id": "step"}),
            _event(
                4,
                "composition/snapshot",
                {"plugins": [{"plugin_id": "traceh.core", "version": "0.5.0"}]},
            ),
            _event(5, "step/end", {"turn_id": "turn", "step_id": "step"}),
            _event(
                6,
                "turn/end",
                {"turn_id": "turn", "reason": "completed", "steps": 1},
            ),
        )
    )

    async def build(*args: object, **kwargs: object) -> _ProbeRuntime:
        del args, kwargs
        return runtime

    monkeypatch.setattr(probe_module, "build_default_runtime_async", build)
    monkeypatch.setattr(
        probe_module,
        "verify_request_snapshots",
        _no_reconstruction_issues,
    )

    facts = await probe_module._run_case(
        suite=suite,
        run_root=tmp_path / "probe-run",
        case=case,
        arm="candidate",
        plugin_id="review.plugin",
        plugin_version="0.1.0",
    )

    assert facts["evidence_complete"] is True
    assert facts["plugin_identity_matches"] is False
    result = comparison_module._evaluate_case(
        "probe-case",
        facts,
        {
            "reason": "completed",
            "verification_passed": True,
            "tool_results": [],
        },
    )
    assert "arm-plugin-identity-mismatch" in result.failure_codes


@pytest.mark.asyncio
async def test_comparator_reports_regression_without_approving_or_promoting(
    tmp_path: Path,
) -> None:
    suite = _write_suite(tmp_path / "suite")
    config = _config(tmp_path, suite)

    report = await CandidateComparator(
        config,
        runner=ComparisonRunner(suite, baseline_passes=True, candidate_passes=False),
    ).run()

    assert report.ok is True
    assert report.classification == "regressed"
    assert report.regressions == ("capability",)
    payload = report.to_dict()
    assert "approved" not in payload
    assert "promoted" not in payload


@pytest.mark.asyncio
async def test_comparator_rejects_a_tampered_l2_digest_before_running_commands(
    tmp_path: Path,
) -> None:
    suite = _write_suite(tmp_path / "suite")
    evidence = _write_evidence(tmp_path / "evidence", digest_override="0" * 64)
    core = tmp_path / "core"
    core.mkdir()
    runner = ComparisonRunner(suite)

    with pytest.raises(CandidateComparisonEvidenceError) as caught:
        await CandidateComparator(
            CandidateComparisonConfig(
                evidence,
                core,
                Path("benchmarks/evolution/review_v1"),
                tmp_path / "output",
                allow_index=True,
            ),
            runner=runner,
        ).run()

    assert caught.value.code == "l2-artifact-digest-mismatch"
    assert runner.requests == []
    assert not (tmp_path / "output").exists()


@pytest.mark.asyncio
async def test_comparator_rejects_an_incomplete_l2_gate_set_before_running_commands(
    tmp_path: Path,
) -> None:
    suite = _write_suite(tmp_path / "suite")
    evidence = _write_evidence(tmp_path / "evidence")
    report_path = evidence / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["checks"][0]["status"] = "failed"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    core = tmp_path / "core"
    core.mkdir()
    runner = ComparisonRunner(suite)

    with pytest.raises(CandidateComparisonEvidenceError) as caught:
        await CandidateComparator(
            CandidateComparisonConfig(
                evidence,
                core,
                Path("benchmarks/evolution/review_v1"),
                tmp_path / "output",
                allow_index=True,
            ),
            runner=runner,
        ).run()

    assert caught.value.code == "l2-gates-not-passed"
    assert runner.requests == []
    assert not (tmp_path / "output").exists()


@pytest.mark.asyncio
async def test_report_commit_failure_leaves_no_visible_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite = _write_suite(tmp_path / "suite")
    config = _config(tmp_path, suite)

    def fail_report(path: Path, report: object) -> None:
        del report
        (path / "partial.txt").write_text("partial", encoding="utf-8")
        raise OSError("fixture report failure")

    monkeypatch.setattr(comparison_module, "_write_report", fail_report)
    with pytest.raises(OSError, match="fixture report failure"):
        await CandidateComparator(config, runner=ComparisonRunner(suite)).run()

    assert not config.output.exists()


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_candidate_process_convergence(
    tmp_path: Path,
) -> None:
    suite = _write_suite(tmp_path / "suite")
    config = _config(tmp_path, suite)
    runner = CancellationRunner(suite)
    comparing = asyncio.create_task(CandidateComparator(config, runner=runner).run())
    await asyncio.wait_for(runner.candidate_entered.wait(), 30)

    comparing.cancel()
    await asyncio.sleep(0)
    comparing.cancel()
    await asyncio.sleep(0)
    assert not comparing.done()
    assert not config.output.exists()

    runner.release.set()
    with pytest.raises(asyncio.CancelledError):
        await comparing
    assert runner.cancellations >= 2
    assert not config.output.exists()


def test_cli_parser_requires_an_explicit_suite_and_dependency_source() -> None:
    args = build_parser().parse_args(
        [
            "plugins",
            "compare",
            "evidence",
            "--core-project",
            "core",
            "--suite",
            "benchmarks/evolution/review_v1",
            "--output",
            "comparison",
            "--allow-index",
        ]
    )

    assert args.validation_evidence == Path("evidence")
    assert args.suite == Path("benchmarks/evolution/review_v1")
    assert args.allow_index is True
