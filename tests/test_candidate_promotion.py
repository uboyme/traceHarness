from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import venv
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

import traceh.evolution.candidate_promotion as promotion_module
from traceh.cli.main import build_parser
from traceh.evolution.candidate_comparison import (
    COMPARISON_CHECKS,
    ComparisonCandidate,
    InstalledDistribution,
)
from traceh.evolution.candidate_promotion import (
    CandidatePromoter,
    CandidatePromotionConfig,
    CandidatePromotionEvidenceError,
    CandidatePromotionExecutionError,
    CandidateRollbackConfig,
    CandidateRollbacker,
    PromotionReport,
)
from traceh.evolution.candidate_validation import (
    VALIDATION_CHECK_ORDER,
    CommandOutcome,
    CommandRequest,
    SubprocessCommandRunner,
)
from traceh.version import __version__

_COMMIT = "c" * 40
_PLUGIN_ID = "review.plugin"
_DISTRIBUTION = "traceh-review-plugin"
_ENTRY_VALUE = "review_plugin:ReviewPlugin"
_PYTHON = Path(sys.executable).resolve()


@pytest.fixture(autouse=True)
def _isolated_promotion_coordination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        promotion_module,
        "_PROMOTION_COORDINATION_ROOT",
        tmp_path / "promotion-coordination",
    )


def _write_wheel(
    path: Path,
    *,
    distribution: str = _DISTRIBUTION,
    plugin_id: str = _PLUGIN_ID,
    entry_value: str = _ENTRY_VALUE,
    entry_module: str = "review_plugin",
    version: str = "0.1.0",
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    dist_info = f"{distribution.replace('-', '_')}-{version}.dist-info"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{entry_module}/__init__.py", "class ReviewPlugin: pass\n")
        archive.writestr(
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.1\nName: {distribution}\nVersion: {version}\n",
        )
        archive.writestr(f"{dist_info}/WHEEL", "Wheel-Version: 1.0\n")
        archive.writestr(
            f"{dist_info}/entry_points.txt",
            f"[traceh.plugins]\n{plugin_id} = {entry_value}\n",
        )
        archive.writestr(f"{dist_info}/RECORD", "")
    return path


def _write_evidence(
    root: Path,
    *,
    classification: str = "improved",
    distribution: str = _DISTRIBUTION,
    plugin_id: str = _PLUGIN_ID,
    entry_value: str = _ENTRY_VALUE,
    entry_module: str = "review_plugin",
) -> tuple[Path, Path]:
    validation = root / "l2"
    comparison = root / "l3"
    validation.mkdir(parents=True)
    comparison.mkdir()
    wheel = _write_wheel(
        validation
        / "artifacts"
        / f"{distribution.replace('-', '_')}-0.1.0-py3-none-any.whl",
        distribution=distribution,
        plugin_id=plugin_id,
        entry_value=entry_value,
        entry_module=entry_module,
    )
    artifact = {
        "filename": f"artifacts/{wheel.name}",
        "sha256": promotion_module.sha256_file(wheel),
        "size_bytes": wheel.stat().st_size,
    }
    candidate = {
        "distribution": distribution,
        "version": "0.1.0",
        "plugin_id": plugin_id,
        "entry_value": entry_value,
        "entry_module": entry_module,
    }
    validation_report = {
        "schema_version": 1,
        "ok": True,
        "core_commit": _COMMIT,
        "candidate": candidate,
        "artifact": artifact,
        "checks": [
            {"name": name, "status": "passed", "code": f"{name}-passed"}
            for name in VALIDATION_CHECK_ORDER
        ],
        "boundaries_zh": [],
    }
    validation_content = (
        json.dumps(validation_report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()
    (validation / "report.json").write_bytes(validation_content)
    if classification == "improved":
        outcome = "improved"
        improvements = ["capability"]
        regressions: list[str] = []
        baseline_success = False
        candidate_success = True
    elif classification == "regressed":
        outcome = "regressed"
        improvements = []
        regressions = ["capability"]
        baseline_success = True
        candidate_success = False
    else:
        outcome = "unchanged-pass"
        improvements = []
        regressions = []
        baseline_success = True
        candidate_success = True
    def arm(success: bool) -> dict[str, object]:
        return {
            "case_id": "capability",
            "success": success,
            "failure_codes": [] if success else ["verification-result-mismatch"],
            "steps": 1,
            "model_attempts": 1,
            "tool_calls": 1,
            "tool_non_successes": 0,
            "verification_passed": success,
            "invariant_violations": 0,
            "reconstruction_violations": 0,
            "duration_seconds": 0.1,
        }

    def summary(success: bool) -> dict[str, object]:
        return {
            "successful_cases": int(success),
            "total_cases": 1,
            "total_steps": 1,
            "total_model_attempts": 1,
            "total_tool_calls": 1,
            "total_tool_non_successes": 0,
            "invariant_violations": 0,
            "reconstruction_violations": 0,
            "duration_seconds": 0.1,
        }

    candidate_dependency = {
        "filename": wheel.name,
        "distribution": distribution,
        "version": "0.1.0",
        "sha256": artifact["sha256"],
        "size_bytes": artifact["size_bytes"],
    }
    core_dependency = {
        "filename": "traceharness_py-0.5.0-py3-none-any.whl",
        "distribution": "traceharness-py",
        "version": "0.5.0",
        "sha256": "a" * 64,
        "size_bytes": 1,
    }
    comparison_report = {
        "schema_version": 1,
        "ok": True,
        "created_at": "2026-08-21T00:00:00+00:00",
        "validation_report_sha256": hashlib.sha256(validation_content).hexdigest(),
        "core_commit": _COMMIT,
        "suite_id": f"{plugin_id}.v1",
        "suite_digest": "d" * 64,
        "candidate": candidate,
        "artifact": artifact,
        "dependency_artifacts": [candidate_dependency, core_dependency],
        "environment_receipt": [
            {"name": distribution, "version": "0.1.0"},
            {"name": "traceharness-py", "version": "0.5.0"},
        ],
        "classification": classification,
        "improvements": improvements,
        "regressions": regressions,
        "cases": [
            {
                "case_id": "capability",
                "outcome": outcome,
                "baseline": arm(baseline_success),
                "candidate": arm(candidate_success),
            }
        ],
        "baseline": summary(baseline_success),
        "candidate_summary": summary(candidate_success),
        "checks": [
            {"name": name, "code": code, "duration_seconds": 0.0}
            for name, code in COMPARISON_CHECKS
        ],
        "boundaries_zh": ["fixture boundary"],
    }
    (comparison / "report.json").write_text(
        json.dumps(comparison_report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return validation, comparison


class PromotionRunner:
    def __init__(self) -> None:
        self.requests: list[CommandRequest] = []
        self.installed_version: str | None = None
        self.install_started = asyncio.Event()
        self.install_gate: asyncio.Event | None = None
        self.extra_distribution: tuple[str, str] | None = None
        self.doctor_mutates_candidate = False
        self.doctor_mutates_content = False
        self.content_drift = False
        self.python_prefix = "fixture-target"

    async def run(self, request: CommandRequest) -> CommandOutcome:
        self.requests.append(request)
        if request.purpose == "promotion-target-inspect":
            candidate = None
            requested_distribution = request.argv[5]
            if (
                self.installed_version is not None
                and requested_distribution == _DISTRIBUTION
            ):
                candidate = _receipt(self.installed_version)
            distributions = [{"name": "traceharness-py", "version": "0.5.0"}]
            if self.extra_distribution is not None:
                name, version = self.extra_distribution
                distributions.append({"name": name, "version": version})
            if self.installed_version is not None:
                distributions.insert(
                    0,
                    {"name": _DISTRIBUTION, "version": self.installed_version},
                )
            distributions.sort(key=lambda item: item["name"])
            return CommandOutcome(
                0,
                0.01,
                stdout=json.dumps(
                    {
                        "python_implementation": "cpython",
                        "python_version": "3.12 fixture",
                        "python_prefix": self.python_prefix,
                        "core_version": "0.5.0",
                        "distributions": distributions,
                        "environment_content": _environment_content(
                            distributions,
                            drift=self.content_drift,
                        ),
                        "candidate": candidate,
                    }
                ),
            )
        if request.purpose in {"promotion-install", "promotion-rollback-install"}:
            self.install_started.set()
            if self.install_gate is not None:
                await self.install_gate.wait()
            self.installed_version = "0.1.0"
            self.content_drift = False
            return CommandOutcome(0, 0.01)
        if request.purpose == "promotion-rollback-uninstall":
            self.installed_version = None
            self.content_drift = False
            return CommandOutcome(0, 0.01)
        if request.purpose == "promotion-plugin-doctor":
            if self.doctor_mutates_candidate and self.installed_version is not None:
                self.installed_version = "0.2.0"
            if self.doctor_mutates_content:
                self.content_drift = True
            return CommandOutcome(0, 0.01)
        raise AssertionError(f"unexpected request purpose: {request.purpose}")


def _receipt(version: str) -> dict[str, object]:
    return {
        "distribution": _DISTRIBUTION,
        "version": version,
        "plugin_id": _PLUGIN_ID,
        "entry_value": _ENTRY_VALUE,
        "content_sha256": hashlib.sha256(version.encode()).hexdigest(),
        "file_count": 4,
        "size_bytes": 128,
    }


def _environment_content(
    distributions: list[dict[str, str]],
    *,
    drift: bool,
) -> dict[str, object]:
    encoded = json.dumps(
        {"distributions": distributions, "drift": drift},
        sort_keys=True,
    ).encode()
    return {
        "content_sha256": hashlib.sha256(encoded).hexdigest(),
        "file_count": max(len(distributions), 1),
        "size_bytes": max(len(encoded), 1),
    }


def _config(
    tmp_path: Path,
    validation: Path,
    comparison: Path,
    *,
    output: str,
    approval: str | None = None,
) -> CandidatePromotionConfig:
    target = tmp_path / "target-python.exe"
    target.touch(exist_ok=True)
    return CandidatePromotionConfig(
        validation,
        comparison,
        target,
        tmp_path / "registry",
        tmp_path / output,
        approval,
        30.0,
    )


def test_default_coordination_lane_is_derived_from_the_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(promotion_module, "_PROMOTION_COORDINATION_ROOT", None)
    target = tmp_path / "target-environment"
    identity = os.path.normcase(str(target.resolve()))

    paths = promotion_module._OwnershipPaths(identity)

    assert paths.root.parent == target.parent / ".traceh-promotion-coordination-v1"


@pytest.mark.asyncio
async def test_review_is_non_mutating_and_binds_exact_target_state(tmp_path: Path) -> None:
    validation, comparison = _write_evidence(tmp_path / "evidence")
    runner = PromotionRunner()

    report = await CandidatePromoter(
        _config(tmp_path, validation, comparison, output="review"),
        runner=runner,
    ).run()

    assert report.action == "review"
    assert report.code == "human-approval-required"
    assert report.approval_digest is not None
    assert report.promotion_id is None
    assert (tmp_path / "review" / "report.json").is_file()
    assert not (tmp_path / "registry").exists()
    assert [request.purpose for request in runner.requests] == ["promotion-target-inspect"]


@pytest.mark.asyncio
async def test_incomplete_l3_gate_set_cannot_authorize_a_review(tmp_path: Path) -> None:
    validation, comparison = _write_evidence(tmp_path / "evidence")
    report_path = comparison / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["checks"] = []
    report_path.write_text(json.dumps(report), encoding="utf-8")
    runner = PromotionRunner()

    with pytest.raises(CandidatePromotionEvidenceError) as caught:
        await CandidatePromoter(
            _config(tmp_path, validation, comparison, output="review"),
            runner=runner,
        ).run()

    assert caught.value.code == "l3-gates-not-passed"
    assert runner.requests == []
    assert not (tmp_path / "review").exists()


@pytest.mark.asyncio
async def test_skeleton_l3_json_cannot_authorize_a_review(tmp_path: Path) -> None:
    validation, comparison = _write_evidence(tmp_path / "evidence")
    report_path = comparison / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["dependency_artifacts"] = []
    report["baseline"] = {}
    report["candidate_summary"] = {}
    report["cases"] = [{"case_id": "capability", "outcome": "improved"}]
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(CandidatePromotionEvidenceError):
        await CandidatePromoter(
            _config(tmp_path, validation, comparison, output="review"),
            runner=PromotionRunner(),
        ).run()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "invalid_value", "expected_code"),
    (
        ("improvements", [{}], "l3-case-classification-mismatch"),
        ("regressions", [["capability"]], "l3-case-classification-mismatch"),
        ("baseline.failure_codes", [{}], "l3-case-arm-invalid"),
    ),
)
async def test_l3_arrays_reject_non_string_members_without_leaking_type_errors(
    tmp_path: Path,
    field: str,
    invalid_value: list[object],
    expected_code: str,
) -> None:
    validation, comparison = _write_evidence(tmp_path / "evidence")
    report_path = comparison / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if field == "baseline.failure_codes":
        report["cases"][0]["baseline"]["failure_codes"] = invalid_value
    else:
        report[field] = invalid_value
    report_path.write_text(json.dumps(report), encoding="utf-8")
    runner = PromotionRunner()

    with pytest.raises(CandidatePromotionEvidenceError) as caught:
        await CandidatePromoter(
            _config(tmp_path, validation, comparison, output="review"),
            runner=runner,
        ).run()

    assert caught.value.code == expected_code
    assert runner.requests == []
    assert not (tmp_path / "review").exists()


@pytest.mark.asyncio
async def test_exact_approval_promotes_and_first_rollback_uninstalls(tmp_path: Path) -> None:
    validation, comparison = _write_evidence(tmp_path / "evidence")
    runner = PromotionRunner()
    review = await CandidatePromoter(
        _config(tmp_path, validation, comparison, output="review"),
        runner=runner,
    ).run()
    assert review.approval_digest is not None

    promoted = await CandidatePromoter(
        _config(
            tmp_path,
            validation,
            comparison,
            output="promotion",
            approval=review.approval_digest,
        ),
        runner=runner,
    ).run()

    assert promoted.code == "promoted"
    assert promoted.promotion_id is not None
    assert runner.installed_version == "0.1.0"
    install = next(item for item in runner.requests if item.purpose == "promotion-install")
    assert "--no-index" in install.argv
    assert "--no-deps" in install.argv
    artifact = Path(install.argv[-1])
    assert promotion_module.sha256_file(artifact) == promoted.artifact_sha256
    doctor = next(item for item in runner.requests if item.purpose == "promotion-plugin-doctor")
    assert doctor.argv[1:4] == ("-m", "traceh.cli.main", "plugins")

    with pytest.raises(CandidatePromotionEvidenceError) as duplicate:
        await CandidatePromoter(
            _config(tmp_path, validation, comparison, output="duplicate-review"),
            runner=runner,
        ).run()
    assert duplicate.value.code == "promotion-artifact-already-current"

    rolled_back = await CandidateRollbacker(
        CandidateRollbackConfig(
            target_python=tmp_path / "target-python.exe",
            registry=tmp_path / "registry",
            output=tmp_path / "rollback",
            plugin_id=_PLUGIN_ID,
            distribution=_DISTRIBUTION,
            current_promotion_id=promoted.promotion_id,
            command_timeout_seconds=30.0,
        ),
        runner=runner,
    ).run()

    assert rolled_back.code == "rolled-back"
    assert rolled_back.promotion_id is None
    assert rolled_back.rollback_kind == "uninstall"
    assert runner.installed_version is None
    state = json.loads(next((tmp_path / "registry").rglob("state.json")).read_text())
    assert state["status"] == "stable"
    assert state["current_promotion_id"] is None
    assert not tuple((tmp_path / "promotion-coordination").rglob("owner.json"))


@pytest.mark.asyncio
async def test_approval_becomes_stale_when_target_changes(tmp_path: Path) -> None:
    validation, comparison = _write_evidence(tmp_path / "evidence")
    runner = PromotionRunner()
    review = await CandidatePromoter(
        _config(tmp_path, validation, comparison, output="review"),
        runner=runner,
    ).run()
    runner.installed_version = "0.1.0"

    with pytest.raises(CandidatePromotionEvidenceError) as caught:
        await CandidatePromoter(
            _config(
                tmp_path,
                validation,
                comparison,
                output="promotion",
                approval=review.approval_digest,
            ),
            runner=runner,
        ).run()

    assert caught.value.code == "promotion-unmanaged-target-conflict"
    assert not any(request.purpose == "promotion-install" for request in runner.requests)


@pytest.mark.asyncio
async def test_incorrect_approval_digest_cannot_mutate_the_target(tmp_path: Path) -> None:
    validation, comparison = _write_evidence(tmp_path / "evidence")
    runner = PromotionRunner()
    await CandidatePromoter(
        _config(tmp_path, validation, comparison, output="review"),
        runner=runner,
    ).run()

    with pytest.raises(CandidatePromotionEvidenceError) as caught:
        await CandidatePromoter(
            _config(
                tmp_path,
                validation,
                comparison,
                output="promotion",
                approval="0" * 64,
            ),
            runner=runner,
        ).run()

    assert caught.value.code == "promotion-approval-digest-mismatch"
    assert not any(request.purpose == "promotion-install" for request in runner.requests)
    assert not (tmp_path / "registry").exists()


@pytest.mark.asyncio
async def test_known_regression_cannot_be_human_overridden(tmp_path: Path) -> None:
    validation, comparison = _write_evidence(
        tmp_path / "evidence",
        classification="regressed",
    )

    with pytest.raises(CandidatePromotionEvidenceError) as caught:
        await CandidatePromoter(
            _config(tmp_path, validation, comparison, output="review"),
            runner=PromotionRunner(),
        ).run()

    assert caught.value.code == "comparison-not-promotable"
    assert not (tmp_path / "review").exists()


@pytest.mark.asyncio
async def test_review_rejects_a_target_with_uncompared_dependencies(tmp_path: Path) -> None:
    validation, comparison = _write_evidence(tmp_path / "evidence")
    runner = PromotionRunner()
    runner.extra_distribution = ("ambient-extra", "9.9")

    with pytest.raises(CandidatePromotionEvidenceError) as caught:
        await CandidatePromoter(
            _config(tmp_path, validation, comparison, output="review"),
            runner=runner,
        ).run()

    assert caught.value.code == "promotion-target-environment-mismatch"


@pytest.mark.asyncio
async def test_approval_digest_binds_the_selected_registry(tmp_path: Path) -> None:
    validation, comparison = _write_evidence(tmp_path / "evidence")
    runner = PromotionRunner()
    first_config = _config(tmp_path, validation, comparison, output="review-one")
    second_config = replace(
        first_config,
        registry=tmp_path / "other-registry",
        output=tmp_path / "review-two",
    )

    first = await CandidatePromoter(first_config, runner=runner).run()
    second = await CandidatePromoter(second_config, runner=runner).run()

    assert first.approval_digest != second.approval_digest


@pytest.mark.asyncio
async def test_l3_case_lists_cannot_forge_the_approval_card(tmp_path: Path) -> None:
    validation, comparison = _write_evidence(tmp_path / "evidence")
    report_path = comparison / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["cases"][0]["outcome"] = "unchanged-pass"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(CandidatePromotionEvidenceError) as caught:
        await CandidatePromoter(
            _config(tmp_path, validation, comparison, output="review"),
            runner=PromotionRunner(),
        ).run()

    assert caught.value.code == "l3-case-classification-mismatch"


@pytest.mark.asyncio
async def test_cancelled_install_rolls_back_before_caller_returns(tmp_path: Path) -> None:
    validation, comparison = _write_evidence(tmp_path / "evidence")
    runner = PromotionRunner()
    review = await CandidatePromoter(
        _config(tmp_path, validation, comparison, output="review"),
        runner=runner,
    ).run()
    runner.install_gate = asyncio.Event()
    promotion = asyncio.create_task(
        CandidatePromoter(
            _config(
                tmp_path,
                validation,
                comparison,
                output="promotion",
                approval=review.approval_digest,
            ),
            runner=runner,
        ).run()
    )
    await runner.install_started.wait()
    promotion.cancel()

    with pytest.raises(asyncio.CancelledError):
        await promotion

    assert runner.installed_version is None
    assert any(request.purpose == "promotion-rollback-uninstall" for request in runner.requests)
    state = json.loads(next((tmp_path / "registry").rglob("state.json")).read_text())
    assert state["status"] == "stable"
    assert state["current_promotion_id"] is None


@pytest.mark.asyncio
async def test_report_commit_failure_rolls_back_the_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validation, comparison = _write_evidence(tmp_path / "evidence")
    runner = PromotionRunner()
    review = await CandidatePromoter(
        _config(tmp_path, validation, comparison, output="review"),
        runner=runner,
    ).run()
    original = promotion_module._commit_report_bundle

    def fail_promotion_report(path: Path, report: PromotionReport) -> None:
        if report.action == "promote":
            raise OSError("fixture report failure")
        original(path, report)

    monkeypatch.setattr(promotion_module, "_commit_report_bundle", fail_promotion_report)

    with pytest.raises(OSError, match="fixture report failure"):
        await CandidatePromoter(
            _config(
                tmp_path,
                validation,
                comparison,
                output="promotion",
                approval=review.approval_digest,
            ),
            runner=runner,
        ).run()

    assert runner.installed_version is None
    assert not (tmp_path / "promotion").exists()
    state = json.loads(next((tmp_path / "registry").rglob("state.json")).read_text())
    assert state["status"] == "stable"
    assert state["current_promotion_id"] is None


@pytest.mark.asyncio
async def test_doctor_target_drift_fails_and_restores_the_previous_state(tmp_path: Path) -> None:
    validation, comparison = _write_evidence(tmp_path / "evidence")
    runner = PromotionRunner()
    review = await CandidatePromoter(
        _config(tmp_path, validation, comparison, output="review"),
        runner=runner,
    ).run()
    runner.doctor_mutates_candidate = True

    with pytest.raises(CandidatePromotionExecutionError) as caught:
        await CandidatePromoter(
            _config(
                tmp_path,
                validation,
                comparison,
                output="promotion",
                approval=review.approval_digest,
            ),
            runner=runner,
        ).run()

    assert caught.value.code == "promotion-doctor-target-drift"
    assert runner.installed_version is None
    state = json.loads(next((tmp_path / "registry").rglob("state.json")).read_text())
    assert state["status"] == "stable"
    assert state["current_promotion_id"] is None


@pytest.mark.asyncio
async def test_doctor_file_content_drift_fails_without_version_drift(tmp_path: Path) -> None:
    validation, comparison = _write_evidence(tmp_path / "evidence")
    runner = PromotionRunner()
    review = await CandidatePromoter(
        _config(tmp_path, validation, comparison, output="review"),
        runner=runner,
    ).run()
    runner.doctor_mutates_content = True

    with pytest.raises(CandidatePromotionExecutionError) as caught:
        await CandidatePromoter(
            _config(
                tmp_path,
                validation,
                comparison,
                output="promotion",
                approval=review.approval_digest,
            ),
            runner=runner,
        ).run()

    assert caught.value.code == "promotion-doctor-target-drift"
    assert runner.installed_version is None
    assert runner.content_drift is False


@pytest.mark.asyncio
async def test_review_rejects_output_or_registry_inside_target_environment(
    tmp_path: Path,
) -> None:
    validation, comparison = _write_evidence(tmp_path / "evidence")
    target = tmp_path / "target"
    python = target / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.touch()

    for field in ("output", "registry"):
        runner = PromotionRunner()
        runner.python_prefix = str(target)
        base = _config(tmp_path, validation, comparison, output=f"review-{field}")
        config = replace(base, target_python=python)
        if field == "output":
            config = replace(config, output=target / "review")
        else:
            config = replace(config, registry=target / "registry")
        with pytest.raises(promotion_module.CandidatePromotionConfigurationError):
            await CandidatePromoter(config, runner=runner).run()
        assert not config.output.exists()
        assert not config.registry.exists()


@pytest.mark.asyncio
async def test_two_registries_and_python_aliases_share_one_distribution_lock(
    tmp_path: Path,
) -> None:
    validation, comparison = _write_evidence(tmp_path / "evidence")
    runner = PromotionRunner()
    runner.install_gate = asyncio.Event()
    first = _config(tmp_path, validation, comparison, output="review-one")
    alias = tmp_path / "python-alias.exe"
    alias.touch()
    second = replace(
        _config(tmp_path, validation, comparison, output="review-two"),
        target_python=alias,
        registry=tmp_path / "other-registry",
    )
    first_review = await CandidatePromoter(first, runner=runner).run()
    second_review = await CandidatePromoter(second, runner=runner).run()
    first_apply = asyncio.create_task(
        CandidatePromoter(
            replace(
                first,
                output=tmp_path / "promotion-one",
                approval_digest=first_review.approval_digest,
            ),
            runner=runner,
        ).run()
    )
    await runner.install_started.wait()
    second_apply = asyncio.create_task(
        CandidatePromoter(
            replace(
                second,
                output=tmp_path / "promotion-two",
                approval_digest=second_review.approval_digest,
            ),
            runner=runner,
        ).run()
    )
    await asyncio.sleep(0)
    runner.install_gate.set()
    assert (await first_apply).code == "promoted"
    with pytest.raises(CandidatePromotionEvidenceError) as caught:
        await second_apply

    assert caught.value.code == "promotion-distribution-owned-by-another-registry"
    assert sum(request.purpose == "promotion-install" for request in runner.requests) == 1


@pytest.mark.asyncio
async def test_another_registry_cannot_review_an_already_owned_distribution(
    tmp_path: Path,
) -> None:
    validation, comparison = _write_evidence(tmp_path / "evidence")
    runner = PromotionRunner()
    first = _config(tmp_path, validation, comparison, output="review-one")
    review = await CandidatePromoter(first, runner=runner).run()
    await CandidatePromoter(
        replace(
            first,
            output=tmp_path / "promotion-one",
            approval_digest=review.approval_digest,
        ),
        runner=runner,
    ).run()

    with pytest.raises(CandidatePromotionEvidenceError) as caught:
        await CandidatePromoter(
            replace(
                first,
                registry=tmp_path / "other-registry",
                output=tmp_path / "review-two",
                approval_digest=None,
            ),
            runner=runner,
        ).run()

    assert caught.value.code == "promotion-distribution-owned-by-another-registry"
    assert sum(request.purpose == "promotion-install" for request in runner.requests) == 1


@pytest.mark.asyncio
async def test_target_environment_allows_only_one_managed_distribution_chain(
    tmp_path: Path,
) -> None:
    first_validation, first_comparison = _write_evidence(tmp_path / "first-evidence")
    second_distribution = "traceh-review-other"
    second_plugin_id = "review.other"
    second_validation, second_comparison = _write_evidence(
        tmp_path / "second-evidence",
        distribution=second_distribution,
        plugin_id=second_plugin_id,
        entry_value="review_other:ReviewPlugin",
        entry_module="review_other",
    )
    runner = PromotionRunner()
    first = _config(
        tmp_path,
        first_validation,
        first_comparison,
        output="first-review",
    )
    first_review = await CandidatePromoter(first, runner=runner).run()
    first_promotion = await CandidatePromoter(
        replace(
            first,
            output=tmp_path / "first-promotion",
            approval_digest=first_review.approval_digest,
        ),
        runner=runner,
    ).run()
    assert first_promotion.promotion_id is not None
    second = _config(
        tmp_path,
        second_validation,
        second_comparison,
        output="second-review",
    )

    with pytest.raises(CandidatePromotionEvidenceError) as caught:
        await CandidatePromoter(second, runner=runner).run()

    assert caught.value.code == "promotion-target-owned-by-another-distribution"
    assert sum(request.purpose == "promotion-install" for request in runner.requests) == 1

    await CandidateRollbacker(
        CandidateRollbackConfig(
            target_python=tmp_path / "target-python.exe",
            registry=tmp_path / "registry",
            output=tmp_path / "first-rollback",
            plugin_id=_PLUGIN_ID,
            distribution=_DISTRIBUTION,
            current_promotion_id=first_promotion.promotion_id,
            command_timeout_seconds=30.0,
        ),
        runner=runner,
    ).run()
    handed_over = await CandidatePromoter(second, runner=runner).run()

    assert handed_over.code == "human-approval-required"
    assert handed_over.distribution == second_distribution


def test_distribution_registries_share_one_target_environment_lock(tmp_path: Path) -> None:
    identity = os.path.normcase(str((tmp_path / "target").resolve()))
    owner = promotion_module._OwnershipPaths(identity)
    first = promotion_module._RegistryPaths(tmp_path / "registry", identity, _DISTRIBUTION)
    second = promotion_module._RegistryPaths(
        tmp_path / "registry",
        identity,
        "traceh-review-other",
    )

    assert first.lock != second.lock
    assert owner.lock == promotion_module._OwnershipPaths(identity).lock


@pytest.mark.asyncio
async def test_rollback_recovers_a_crash_marked_installing_state(tmp_path: Path) -> None:
    validation, comparison = _write_evidence(tmp_path / "evidence")
    runner = PromotionRunner()
    review = await CandidatePromoter(
        _config(tmp_path, validation, comparison, output="review"),
        runner=runner,
    ).run()
    promoted = await CandidatePromoter(
        _config(
            tmp_path,
            validation,
            comparison,
            output="promotion",
            approval=review.approval_digest,
        ),
        runner=runner,
    ).run()
    assert promoted.promotion_id is not None
    state_path = next((tmp_path / "registry").rglob("state.json"))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update(
        {
            "status": "installing",
            "current_promotion_id": None,
            "pending_promotion_id": promoted.promotion_id,
            "current_receipt": None,
        }
    )
    state_path.write_text(json.dumps(state), encoding="utf-8")

    recovered = await CandidateRollbacker(
        CandidateRollbackConfig(
            target_python=tmp_path / "target-python.exe",
            registry=tmp_path / "registry",
            output=tmp_path / "recovery",
            plugin_id=_PLUGIN_ID,
            distribution=_DISTRIBUTION,
            current_promotion_id=promoted.promotion_id,
            command_timeout_seconds=30.0,
        ),
        runner=runner,
    ).run()

    assert recovered.code == "rolled-back"
    assert runner.installed_version is None
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["status"] == "stable"
    assert state["current_promotion_id"] is None


@pytest.mark.asyncio
async def test_rollback_recovers_owner_record_before_first_state(tmp_path: Path) -> None:
    validation, comparison = _write_evidence(tmp_path / "evidence")
    runner = PromotionRunner()
    config = _config(tmp_path, validation, comparison, output="review")
    review = await CandidatePromoter(config, runner=runner).run()
    promoted = await CandidatePromoter(
        replace(
            config,
            output=tmp_path / "promotion",
            approval_digest=review.approval_digest,
        ),
        runner=runner,
    ).run()
    assert promoted.promotion_id is not None

    # Recreate the exact hard-crash window after immutable record + owner and
    # before the first installing state.  Pip has not run in that window, so
    # the target must still be absent.
    state_path = next((tmp_path / "registry").rglob("state.json"))
    state_path.unlink()
    runner.installed_version = None
    runner.content_drift = False

    recovered = await CandidateRollbacker(
        CandidateRollbackConfig(
            target_python=tmp_path / "target-python.exe",
            registry=tmp_path / "registry",
            output=tmp_path / "recovery",
            plugin_id=_PLUGIN_ID,
            distribution=_DISTRIBUTION,
            current_promotion_id=promoted.promotion_id,
            command_timeout_seconds=30.0,
        ),
        runner=runner,
    ).run()

    assert recovered.code == "rolled-back"
    assert runner.installed_version is None
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["status"] == "stable"
    assert state["current_promotion_id"] is None


@pytest.mark.asyncio
async def test_concurrent_same_approval_can_only_promote_once(tmp_path: Path) -> None:
    validation, comparison = _write_evidence(tmp_path / "evidence")
    runner = PromotionRunner()
    review = await CandidatePromoter(
        _config(tmp_path, validation, comparison, output="review"),
        runner=runner,
    ).run()
    runner.install_gate = asyncio.Event()
    first = asyncio.create_task(
        CandidatePromoter(
            _config(
                tmp_path,
                validation,
                comparison,
                output="promotion-one",
                approval=review.approval_digest,
            ),
            runner=runner,
        ).run()
    )
    await runner.install_started.wait()
    second = asyncio.create_task(
        CandidatePromoter(
            _config(
                tmp_path,
                validation,
                comparison,
                output="promotion-two",
                approval=review.approval_digest,
            ),
            runner=runner,
        ).run()
    )
    await asyncio.sleep(0)
    runner.install_gate.set()
    assert (await first).code == "promoted"

    with pytest.raises(CandidatePromotionEvidenceError) as caught:
        await second

    assert caught.value.code in {"promotion-target-not-stable", "promotion-approval-stale"}
    assert sum(request.purpose == "promotion-install" for request in runner.requests) == 1


def test_cli_exposes_two_stage_promotion_and_explicit_rollback() -> None:
    parser = build_parser()
    promote = parser.parse_args(
        [
            "plugins",
            "promote",
            "l2",
            "l3",
            "--target-python",
            "python.exe",
            "--registry",
            "registry",
            "--output",
            "review",
        ]
    )
    rollback = parser.parse_args(
        [
            "plugins",
            "rollback",
            "--target-python",
            "python.exe",
            "--registry",
            "registry",
            "--output",
            "rollback",
            "--plugin-id",
            _PLUGIN_ID,
            "--distribution",
            _DISTRIBUTION,
            "--current-promotion-id",
            "a" * 64,
        ]
    )

    assert promote.plugin_command == "promote"
    assert promote.approve is None
    assert rollback.plugin_command == "rollback"
    assert rollback.distribution == _DISTRIBUTION
    assert rollback.current_promotion_id == "a" * 64


@pytest.mark.asyncio
async def test_real_target_probe_uses_metadata_without_importing_a_candidate(
    tmp_path: Path,
) -> None:
    root = tmp_path / "probe-environment"
    venv.EnvBuilder(with_pip=False).create(root)
    site_packages = root / ("Lib/site-packages" if sys.platform == "win32" else (
        f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    ))
    core_metadata = site_packages / f"traceharness_py-{__version__}.dist-info"
    core_metadata.mkdir(parents=True)
    (core_metadata / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: traceharness-py\nVersion: {__version__}\n",
        encoding="utf-8",
    )
    package = site_packages / "cache_fixture"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    target_python = root / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    facts = await promotion_module._inspect_target(
        SubprocessCommandRunner(),
        target_python,
        ComparisonCandidate(
            "traceh-definitely-absent-promotion-fixture",
            "0",
            "fixture.absent.promotion",
            "fixture_absent:Plugin",
            "fixture_absent",
        ),
        30.0,
    )

    assert facts.core_version == __version__
    assert facts.candidate is None
    assert facts.environment_content.file_count > 0
    await asyncio.to_thread(
        subprocess.run,
        [
            str(target_python),
            "-c",
            f"import sys; sys.path.insert(0, {str(site_packages)!r}); import cache_fixture.module",
        ],
        check=True,
    )
    after_cache = await promotion_module._inspect_target(
        SubprocessCommandRunner(),
        target_python,
        ComparisonCandidate(
            "traceh-definitely-absent-promotion-fixture",
            "0",
            "fixture.absent.promotion",
            "fixture_absent:Plugin",
            "fixture_absent",
        ),
        30.0,
    )
    assert after_cache.environment_content == facts.environment_content


@pytest.mark.asyncio
async def test_target_probe_uses_the_selected_venv_without_running_site(tmp_path: Path) -> None:
    target = tmp_path / "target"
    venv.EnvBuilder(with_pip=False).create(target)
    if os.name == "nt":
        python = target / "Scripts" / "python.exe"
        site_packages = target / "Lib" / "site-packages"
    else:
        python = target / "bin" / "python"
        site_packages = (
            target
            / "lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages"
        )
    core_metadata = site_packages / f"traceharness_py-{__version__}.dist-info"
    core_metadata.mkdir(parents=True)
    (core_metadata / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: traceharness-py\nVersion: {__version__}\n",
        encoding="utf-8",
    )
    validation = tmp_path / "validation"
    comparison = tmp_path / "comparison"
    validation.mkdir()
    comparison.mkdir()
    validated = promotion_module._validated_promotion_config(
        CandidatePromotionConfig(
            validation,
            comparison,
            python,
            tmp_path / "registry",
            tmp_path / "output",
        )
    )

    facts = await promotion_module._inspect_target(
        SubprocessCommandRunner(),
        python,
        ComparisonCandidate(
            "traceh-definitely-absent-promotion-fixture",
            "0",
            "fixture.absent.promotion",
            "fixture_absent:Plugin",
            "fixture_absent",
        ),
        30.0,
    )

    assert validated.target_python == python.absolute()
    assert Path(facts.python_prefix) == target.resolve()
    assert facts.distributions == (InstalledDistribution("traceharness-py", __version__),)
    assert facts.candidate is None


@pytest.mark.asyncio
async def test_public_review_ignores_a_distro_default_scheme_for_target_venv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "target"
    venv.EnvBuilder(with_pip=False).create(target)
    if os.name == "nt":
        python = target / "Scripts" / "python.exe"
        site_packages = target / "Lib" / "site-packages"
    else:
        python = target / "bin" / "python"
        site_packages = (
            target
            / "lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages"
        )
    core_metadata = site_packages / "traceharness_py-0.5.0.dist-info"
    core_metadata.mkdir(parents=True)
    (core_metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: traceharness-py\nVersion: 0.5.0\n",
        encoding="utf-8",
    )
    validation, comparison = _write_evidence(tmp_path / "evidence")
    script = promotion_module._TARGET_INSPECTION_SCRIPT
    marker = "target_paths = sysconfig.get_paths(\n"
    assert marker in script
    biased = (
        "_real_get_paths = sysconfig.get_paths\n"
        "    def _distro_get_paths(*args, **kwargs):\n"
        "        if 'scheme' not in kwargs:\n"
        "            wrong = target_prefix / 'distro-default' / 'site-packages'\n"
        "            return {'purelib': str(wrong), 'platlib': str(wrong)}\n"
        "        return _real_get_paths(*args, **kwargs)\n"
        "    target_paths = _distro_get_paths(\n"
    )
    monkeypatch.setattr(
        promotion_module,
        "_TARGET_INSPECTION_SCRIPT",
        script.replace(marker, biased, 1),
    )
    config = CandidatePromotionConfig(
        validation,
        comparison,
        python,
        tmp_path / "registry",
        tmp_path / "review",
    )

    report = await CandidatePromoter(
        config,
        runner=SubprocessCommandRunner(),
    ).run()

    assert report.action == "review"
    assert report.code == "human-approval-required"
    assert report.approval_digest is not None
    assert (tmp_path / "review" / "report.json").is_file()
