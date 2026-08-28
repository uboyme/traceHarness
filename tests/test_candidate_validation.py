from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import traceh.evolution.candidate_validation as candidate_validation_module
from traceh.cli.main import build_parser
from traceh.evolution import contract_probe
from traceh.evolution.artifacts import (
    ArtifactContractError,
    audit_candidate_wheel,
    copy_candidate_source,
)
from traceh.evolution.candidate_validation import (
    CandidateValidationConfig,
    CandidateValidationConfigurationError,
    CandidateValidator,
    CommandOutcome,
    CommandRequest,
    SubprocessCommandRunner,
)
from traceh.session.file_lock import FileLockTimeout, exclusive_file_lock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_CREATOR = (
    PROJECT_ROOT / "examples" / "plugins" / "traceh-plugin-creator-skill-plugin"
)
_COMMIT = "a" * 40
_DIRECT_CHILD = r"""
import os
import sys
import time

try:
    import fcntl
except ImportError:
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None

started, lock = sys.argv[1], sys.argv[2]
descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
if fcntl is not None:
    fcntl.flock(descriptor, fcntl.LOCK_EX)
else:
    os.lseek(descriptor, 0, os.SEEK_SET)
    msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
with open(started, "w", encoding="utf-8") as handle:
    handle.write("started")
time.sleep(60)
"""


def make_candidate(root: Path, *, extra_entry: bool = False) -> Path:
    package = root / "src" / "review_plugin"
    tests = root / "tests"
    package.mkdir(parents=True)
    tests.mkdir()
    entries = '"review.plugin" = "review_plugin:ReviewPlugin"\n'
    if extra_entry:
        entries += '"review.other" = "review_plugin:OtherPlugin"\n'
    (root / "pyproject.toml").write_text(
        """
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "traceh-review-plugin"
version = "0.1.0"
dependencies = ["traceharness-py>=0.5,<0.6"]

[project.entry-points."traceh.plugins"]
""" + entries + """

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
addopts = "--ignore=HOST-CORE-TESTS"
""",
        encoding="utf-8",
    )
    (root / "README.md").write_text("review candidate\n", encoding="utf-8")
    (root / "CANDIDATE.md").write_text(
        "# Candidate\n\nUNVALIDATED (L1 SOURCE ONLY)\n",
        encoding="utf-8",
    )
    (package / "__init__.py").write_text("class ReviewPlugin: pass\n", encoding="utf-8")
    (tests / "test_review_plugin.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    return root


class FakeRunner:
    def __init__(self, *, fail: str | None = None, core_version: str = "0.5.0") -> None:
        self.fail = fail
        self.core_version = core_version
        self.requests: list[CommandRequest] = []

    async def run(self, request: CommandRequest) -> CommandOutcome:
        self.requests.append(request)
        if request.purpose == self.fail:
            return CommandOutcome(1, 0.01)
        if request.purpose == "trusted-core-head":
            return CommandOutcome(0, 0.01, stdout=_COMMIT)
        if request.purpose == "trusted-core-clone":
            destination = Path(request.argv[-1])
            (destination / "tests").mkdir(parents=True)
            version_source = destination / "src" / "traceh"
            version_source.mkdir(parents=True)
            (version_source / "version.py").write_text(
                f'__version__ = "{self.core_version}"\n',
                encoding="utf-8",
            )
            (destination / "pyproject.toml").write_text(
                "[tool.pytest.ini_options]\naddopts = '-q'\n",
                encoding="utf-8",
            )
            (destination / "tests" / "test_core.py").write_text(
                "def test_core(): assert True\n",
                encoding="utf-8",
            )
        if request.purpose == "core-wheel-build":
            destination = Path(request.argv[request.argv.index("--wheel-dir") + 1])
            _write_wheel(
                destination / f"traceharness_py-{self.core_version}-py3-none-any.whl",
                package="traceh",
                dist_info=f"traceharness_py-{self.core_version}.dist-info",
            )
        if request.purpose == "candidate-wheel-build":
            destination = Path(request.argv[request.argv.index("--wheel-dir") + 1])
            _write_wheel(
                destination / "traceh_review_plugin-0.1.0-py3-none-any.whl",
                package="review_plugin",
                dist_info="traceh_review_plugin-0.1.0.dist-info",
            )
        if request.purpose.endswith("-create"):
            python = _fake_venv_python(Path(request.argv[-1]))
            python.parent.mkdir(parents=True)
            python.write_text("", encoding="utf-8")
        return CommandOutcome(0, 0.01)


class WheelMutatingRunner(FakeRunner):
    async def run(self, request: CommandRequest) -> CommandOutcome:
        outcome = await super().run(request)
        if request.purpose == "candidate-tests":
            _mutate_built_wheel(self.requests)
        return outcome


class RecordingSubprocessRunner:
    def __init__(self) -> None:
        self._delegate = SubprocessCommandRunner()
        self.outcomes: dict[str, CommandOutcome] = {}

    async def run(self, request: CommandRequest) -> CommandOutcome:
        outcome = await self._delegate.run(request)
        self.outcomes[request.purpose] = outcome
        return outcome


def _write_wheel(path: Path, *, package: str, dist_info: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{package}/__init__.py", "VALUE = 1\n")
        archive.writestr(f"{dist_info}/METADATA", "Metadata-Version: 2.1\n")
        archive.writestr(f"{dist_info}/WHEEL", "Wheel-Version: 1.0\n")
        archive.writestr(f"{dist_info}/RECORD", "")


def _fake_venv_python(root: Path) -> Path:
    return root / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def _mutate_built_wheel(requests: list[CommandRequest]) -> None:
    build = next(item for item in requests if item.purpose == "candidate-wheel-build")
    destination = Path(build.argv[build.argv.index("--wheel-dir") + 1])
    wheel = next(destination.glob("*.whl"))
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("sitecustomize.py", "VALUE = 1\n")


@pytest.mark.asyncio
async def test_validator_uses_host_owned_gates_and_emits_exact_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "ambient-host-source"))
    candidate = make_candidate(tmp_path / "candidate")
    core = tmp_path / "core"
    core.mkdir()
    output = tmp_path / "evidence"
    runner = FakeRunner()

    report = await CandidateValidator(
        CandidateValidationConfig(
            candidate=candidate,
            core_project=core,
            output=output,
            allow_index=True,
        ),
        runner=runner,
    ).run()

    assert report.ok is True
    assert report.core_commit == _COMMIT
    assert report.artifact is not None
    artifact = output / report.artifact.filename
    assert artifact.is_file()
    assert len(report.artifact.sha256) == 64
    assert all(check.status == "passed" for check in report.checks)
    assert json.loads((output / "report.json").read_text(encoding="utf-8"))["ok"] is True
    assert "结论：**通过**" in (output / "report.md").read_text(encoding="utf-8")

    core_request = next(item for item in runner.requests if item.purpose == "core-regression")
    candidate_request = next(
        item for item in runner.requests if item.purpose == "candidate-tests"
    )
    assert "--tb=line" in core_request.argv
    assert "trusted-core" in " ".join(core_request.argv)
    assert "candidate-source/pyproject.toml" not in " ".join(candidate_request.argv)
    config_path = Path(candidate_request.argv[candidate_request.argv.index("-c") + 1])
    assert config_path.name == "pytest.ini"
    assert config_path.parent.name == "control"
    installs = [item for item in runner.requests if item.purpose.endswith("-install")]
    assert len(installs) == 2
    assert installs[0].argv[0] != installs[1].argv[0]
    assert all("PYTHONPATH" not in request.env for request in runner.requests)


@pytest.mark.asyncio
async def test_wheelhouse_applies_to_nested_candidate_and_core_processes(
    tmp_path: Path,
) -> None:
    candidate = make_candidate(tmp_path / "candidate")
    core = tmp_path / "core"
    core.mkdir()
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    runner = FakeRunner()

    report = await CandidateValidator(
        CandidateValidationConfig(
            candidate=candidate,
            core_project=core,
            output=tmp_path / "evidence",
            wheelhouse=wheelhouse,
        ),
        runner=runner,
    ).run()

    assert report.ok is True
    nested = next(item for item in runner.requests if item.purpose == "core-regression")
    assert nested.env["PIP_NO_INDEX"] == "1"
    assert nested.env["PIP_FIND_LINKS"] == wheelhouse.resolve().as_uri()


@pytest.mark.asyncio
async def test_build_failure_blocks_execution_and_still_writes_report(tmp_path: Path) -> None:
    candidate = make_candidate(tmp_path / "candidate")
    core = tmp_path / "core"
    core.mkdir()
    output = tmp_path / "evidence"
    runner = FakeRunner(fail="candidate-wheel-build")

    report = await CandidateValidator(
        CandidateValidationConfig(
            candidate=candidate,
            core_project=core,
            output=output,
            allow_index=True,
        ),
        runner=runner,
    ).run()

    assert report.ok is False
    by_name = {check.name: check for check in report.checks}
    assert by_name["candidate-wheel-build"].status == "failed"
    assert by_name["plugin-doctor"].status == "blocked"
    assert not any(item.purpose == "plugin-doctor" for item in runner.requests)
    assert report.artifact is None
    assert (output / "report.md").is_file()


@pytest.mark.asyncio
async def test_failed_candidate_tests_never_publish_a_validated_artifact(
    tmp_path: Path,
) -> None:
    candidate = make_candidate(tmp_path / "candidate")
    core = tmp_path / "core"
    core.mkdir()
    output = tmp_path / "evidence"

    report = await CandidateValidator(
        CandidateValidationConfig(
            candidate=candidate,
            core_project=core,
            output=output,
            allow_index=True,
        ),
        runner=FakeRunner(fail="candidate-tests"),
    ).run()

    checks = {check.name: check for check in report.checks}
    assert report.ok is False
    assert checks["candidate-tests"].status == "failed"
    assert checks["validated-artifact-publication"].status == "blocked"
    assert report.artifact is None
    assert not (output / "artifacts").exists()


@pytest.mark.asyncio
async def test_wheel_changed_after_audit_is_never_published(tmp_path: Path) -> None:
    candidate = make_candidate(tmp_path / "candidate")
    core = tmp_path / "core"
    core.mkdir()
    output = tmp_path / "evidence"

    report = await CandidateValidator(
        CandidateValidationConfig(
            candidate=candidate,
            core_project=core,
            output=output,
            allow_index=True,
        ),
        runner=WheelMutatingRunner(),
    ).run()

    checks = {check.name: check for check in report.checks}
    assert checks["candidate-wheel-audit"].status == "passed"
    assert checks["validated-artifact-publication"].status == "failed"
    assert checks["validated-artifact-publication"].code == (
        "wheel-python-startup-hook-rejected"
    )
    assert report.ok is False
    assert report.artifact is None
    assert not (output / "artifacts").exists()


@pytest.mark.asyncio
async def test_report_failure_leaves_no_partial_output_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = make_candidate(tmp_path / "candidate")
    core = tmp_path / "core"
    core.mkdir()
    output = tmp_path / "evidence"

    def fail_after_first_report(path: Path, report: object) -> None:
        del report
        (path / "report.json").write_text("partial", encoding="utf-8")
        raise OSError("synthetic report failure")

    monkeypatch.setattr(
        candidate_validation_module,
        "_write_report",
        fail_after_first_report,
    )

    with pytest.raises(OSError, match="synthetic report failure"):
        await CandidateValidator(
            CandidateValidationConfig(
                candidate=candidate,
                core_project=core,
                output=output,
                allow_index=True,
            ),
            runner=FakeRunner(),
        ).run()

    assert not output.exists()
    assert not any(
        entry.name.startswith(".evidence-") for entry in os.scandir(tmp_path)
    )


@pytest.mark.asyncio
async def test_dependency_contract_uses_selected_trusted_core_version(
    tmp_path: Path,
) -> None:
    candidate = make_candidate(tmp_path / "candidate")
    pyproject = candidate / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace(
            "traceharness-py>=0.5,<0.6",
            "traceharness-py>=0.6,<0.7",
        ),
        encoding="utf-8",
    )
    core = tmp_path / "core"
    core.mkdir()

    report = await CandidateValidator(
        CandidateValidationConfig(
            candidate=candidate,
            core_project=core,
            output=tmp_path / "evidence",
            allow_index=True,
        ),
        runner=FakeRunner(core_version="0.6.0"),
    ).run()

    assert report.ok is True
    assert report.checks[0].status == "passed"


@pytest.mark.asyncio
async def test_ambiguous_identity_fails_without_guessing_a_default(tmp_path: Path) -> None:
    candidate = make_candidate(tmp_path / "candidate", extra_entry=True)
    core = tmp_path / "core"
    core.mkdir()
    output = tmp_path / "evidence"

    report = await CandidateValidator(
        CandidateValidationConfig(
            candidate=candidate,
            core_project=core,
            output=output,
            allow_index=True,
        ),
        runner=FakeRunner(),
    ).run()

    source = report.checks[0]
    assert source.status == "failed"
    assert source.code == "candidate-plugin-id-ambiguous"
    assert report.candidate is None


@pytest.mark.parametrize("filename", [".env", ".ENV"])
def test_candidate_copy_rejects_env_without_reading_it(
    tmp_path: Path,
    filename: str,
) -> None:
    candidate = make_candidate(tmp_path / "candidate")
    (candidate / filename).write_text("SECRET_VALUE=do-not-read\n", encoding="utf-8")

    with pytest.raises(ArtifactContractError) as info:
        copy_candidate_source(candidate, tmp_path / "copy")

    assert info.value.code == "candidate-secret-file-rejected"
    assert "do-not-read" not in str(info.value)


def test_candidate_copy_excludes_stale_build_outputs(tmp_path: Path) -> None:
    candidate = make_candidate(tmp_path / "candidate")
    stale = candidate / "src" / "review_plugin" / "__pycache__"
    stale.mkdir()
    (stale / "module.pyc").write_bytes(b"stale")
    (candidate / "build").mkdir()
    (candidate / "build" / "old.txt").write_text("old", encoding="utf-8")

    copied = copy_candidate_source(candidate, tmp_path / "copy")

    assert not (copied / "src" / "review_plugin" / "__pycache__").exists()
    assert not (copied / "build").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Junction contract")
def test_candidate_copy_rejects_a_windows_junction(tmp_path: Path) -> None:
    candidate = make_candidate(tmp_path / "candidate")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "outside.txt").write_text("outside", encoding="utf-8")
    junction = candidate / "linked-outside"
    created = subprocess.run(
        ("cmd", "/c", "mklink", "/J", str(junction), str(outside)),
        check=False,
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        pytest.skip("this Windows host cannot create a directory Junction")

    with pytest.raises(ArtifactContractError) as info:
        copy_candidate_source(candidate, tmp_path / "copy")

    assert info.value.code == "candidate-reparse-point-rejected"
    assert not (tmp_path / "copy" / "linked-outside" / "outside.txt").exists()


def test_cli_requires_explicit_core_output_and_dependency_source(tmp_path: Path) -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "plugins",
            "validate",
            str(tmp_path / "candidate"),
            "--core-project",
            str(tmp_path / "core"),
            "--output",
            str(tmp_path / "evidence"),
            "--allow-index",
        ]
    )

    assert args.plugin_command == "validate"
    assert args.allow_index is True
    assert args.wheelhouse is None


@pytest.mark.asyncio
async def test_cli_json_configuration_failure_is_structured(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "plugins",
            "validate",
            str(tmp_path / "missing-candidate"),
            "--core-project",
            str(tmp_path / "missing-core"),
            "--output",
            str(tmp_path / "evidence"),
            "--allow-index",
            "--json",
        ]
    )

    code = await args.handler(args)
    payload = json.loads(capsys.readouterr().out)

    assert code == 8
    assert payload == {
        "code": "candidate-validation-configuration-invalid",
        "command": "validate",
        "ok": False,
    }


@pytest.mark.asyncio
async def test_direct_reference_dependencies_are_rejected(
    tmp_path: Path,
) -> None:
    candidate = make_candidate(tmp_path / "candidate")
    pyproject = candidate / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace(
            'dependencies = ["traceharness-py>=0.5,<0.6"]',
            'dependencies = ["traceharness-py @ https://packages.invalid/core.whl"]',
        ),
        encoding="utf-8",
    )
    core = tmp_path / "core"
    core.mkdir()

    report = await CandidateValidator(
        CandidateValidationConfig(
            candidate=candidate,
            core_project=core,
            output=tmp_path / "evidence",
            allow_index=True,
        ),
        runner=FakeRunner(),
    ).run()

    assert report.checks[0].code == "candidate-direct-reference-rejected"


@pytest.mark.asyncio
async def test_direct_reference_test_requirement_is_rejected(
    tmp_path: Path,
) -> None:
    candidate = make_candidate(tmp_path / "candidate")
    core = tmp_path / "core"
    core.mkdir()

    with pytest.raises(CandidateValidationConfigurationError):
        await CandidateValidator(
            CandidateValidationConfig(
                candidate=candidate,
                core_project=core,
                output=tmp_path / "evidence",
                allow_index=True,
                test_requirements=("review-helper @ https://packages.invalid/helper.whl",),
            ),
            runner=FakeRunner(),
        ).run()


def test_installed_contract_probe_requires_exact_metadata(monkeypatch) -> None:
    class Distribution:
        metadata = {"Name": "traceh-review-plugin"}
        version = "0.1.0"

    class Record:
        entry_name = "review.plugin"
        distribution_name = "traceh-review-plugin"
        entry_value = "review_plugin:ReviewPlugin"
        issues = ()

    class Discovery:
        def discover(self):
            return (Record(),)

    monkeypatch.setattr(contract_probe.metadata, "distribution", lambda _: Distribution())
    monkeypatch.setattr(contract_probe, "PluginDiscovery", Discovery)

    assert contract_probe.check_installed_contract(
        distribution="traceh-review-plugin",
        version="0.1.0",
        plugin_id="review.plugin",
        entry_value="review_plugin:ReviewPlugin",
    )
    assert not contract_probe.check_installed_contract(
        distribution="traceh-review-plugin",
        version="0.1.1",
        plugin_id="review.plugin",
        entry_value="review_plugin:ReviewPlugin",
    )


@pytest.mark.parametrize(
    ("member", "code"),
    [
        ("sitecustomize.py", "wheel-python-startup-hook-rejected"),
        ("review_plugin.pth", "wheel-path-hook-rejected"),
        ("unrelated_package/__init__.py", "wheel-unexpected-top-level-member"),
    ],
)
def test_wheel_audit_rejects_execution_outside_the_entry_package(
    tmp_path: Path,
    member: str,
    code: str,
) -> None:
    wheel = tmp_path / "candidate.whl"
    _write_wheel(wheel, package="review_plugin", dist_info="review_plugin-0.1.dist-info")
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr(member, "VALUE = 1\n")

    assert code in audit_candidate_wheel(wheel, entry_module="review_plugin")


def test_wheel_audit_rejects_a_symbolic_link_member(tmp_path: Path) -> None:
    wheel = tmp_path / "candidate.whl"
    _write_wheel(wheel, package="review_plugin", dist_info="review_plugin-0.1.dist-info")
    link = zipfile.ZipInfo("review_plugin/linked.py")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr(link, "outside.py")

    assert "wheel-member-unsafe" in audit_candidate_wheel(
        wheel,
        entry_module="review_plugin",
    )


@pytest.mark.parametrize("reserved_root", ["traceh", "pytest"])
def test_wheel_audit_rejects_host_control_namespaces(
    tmp_path: Path,
    reserved_root: str,
) -> None:
    wheel = tmp_path / "candidate.whl"
    _write_wheel(
        wheel,
        package=reserved_root,
        dist_info="candidate_plugin-0.1.dist-info",
    )

    assert "wheel-host-namespace-collision" in audit_candidate_wheel(
        wheel,
        entry_module=f"{reserved_root}.extension",
    )


@pytest.mark.asyncio
async def test_subprocess_runner_converges_child_before_cancellation_returns(
    tmp_path: Path,
) -> None:
    started = tmp_path / "started"
    lock = tmp_path / "child.lock"
    runner = SubprocessCommandRunner()
    task = asyncio.create_task(
        runner.run(
            CommandRequest(
                "cancel-test",
                (sys.executable, "-c", _DIRECT_CHILD, str(started), str(lock)),
                tmp_path,
                os.environ.copy(),
                120.0,
            )
        )
    )
    await _wait_for_file(started)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    try:
        with exclusive_file_lock(lock, timeout=5.0):
            pass
    except FileLockTimeout as error:  # pragma: no cover - only on a real leak
        raise AssertionError("validation child escaped cancellation") from error


@pytest.mark.slow
@pytest.mark.skipif(
    os.environ.get("TRACEH_CANDIDATE_VALIDATION") == "1",
    reason="avoid recursively running the validator inside its own core regression",
)
@pytest.mark.asyncio
async def test_real_candidate_validation_runs_every_l2_gate(tmp_path: Path) -> None:
    if not _head_contains_l2_validator():
        pytest.skip("real L2 recursion guard requires the validator to exist in core HEAD")
    candidate = tmp_path / "candidate"
    shutil.copytree(PLUGIN_CREATOR, candidate)
    (candidate / "CANDIDATE.md").write_text(
        "# Plugin Creator validation fixture\n\n"
        "UNVALIDATED (L1 SOURCE ONLY)\n",
        encoding="utf-8",
    )
    output = tmp_path / "evidence"
    runner = RecordingSubprocessRunner()

    report = await CandidateValidator(
        CandidateValidationConfig(
            candidate=candidate,
            core_project=PROJECT_ROOT,
            output=output,
            allow_index=True,
            command_timeout_seconds=600.0,
            core_timeout_seconds=1800.0,
        ),
        runner=runner,
    ).run()

    if not report.ok:
        diagnostic = output / "diagnostics" / "core-regression.txt"
        if diagnostic.is_file():
            pytest.fail(
                "real L2 core regression failed:\n"
                + diagnostic.read_text(encoding="utf-8"),
                pytrace=False,
            )
    assert report.ok is True, {
        "checks": {check.name: (check.status, check.code) for check in report.checks},
        "core_regression": runner.outcomes.get("core-regression"),
    }
    assert report.artifact is not None
    assert (output / report.artifact.filename).is_file()
    assert {check.name for check in report.checks} == {
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
    }


async def _wait_for_file(path: Path) -> None:
    for _ in range(6000):
        if _path_exists(path):
            return
        await asyncio.sleep(0.005)
    raise AssertionError("child never signalled that it reached the cancellation gate")


def _path_exists(path: Path) -> bool:
    return path.exists()


def _head_contains_l2_validator() -> bool:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(PROJECT_ROOT),
            "cat-file",
            "-e",
            "HEAD:src/traceh/evolution/candidate_validation.py",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode == 0
