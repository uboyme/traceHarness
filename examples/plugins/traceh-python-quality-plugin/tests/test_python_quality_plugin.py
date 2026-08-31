from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from traceh.plugins import DecisionKind, ToolCall, ToolExecutionContext

from traceh_python_quality_plugin import (
    PLUGIN_VERSION,
    PythonEnvironmentSafetyPolicy,
    PythonProjectInfoTool,
    PythonQualityPlugin,
    PythonTestsVerifier,
    inspect_python_project,
)


def write_pyproject(workspace: Path, body: str) -> None:
    (workspace / "pyproject.toml").write_text(body, encoding="utf-8")


def test_distribution_and_manifest_use_one_v08_compatible_identity() -> None:
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["version"] == PLUGIN_VERSION == "0.2.2"
    assert project["project"]["dependencies"] == ["traceharness-py>=0.5,<0.9"]
    assert PythonQualityPlugin.manifest.version == PLUGIN_VERSION
    assert PythonQualityPlugin.manifest.requires_traceh == ">=0.5,<0.9"


def test_explicit_project_command_is_resolved_without_echoing_it(tmp_path: Path) -> None:
    write_pyproject(
        tmp_path,
        """
[project]
name = "quality-fixture"
requires-python = ">=3.12"

[tool.traceh-python-quality]
test-command = ["python", "-m", "unittest", "-v"]
timeout-seconds = 12
""".strip(),
    )

    inspection = inspect_python_project(tmp_path)

    assert inspection.project_name == "quality-fixture"
    assert inspection.requires_python == ">=3.12"
    assert inspection.verification.argv == ("python", "-m", "unittest", "-v")
    assert inspection.verification.timeout_seconds == 12
    assert inspection.to_data()["verification"] == {
        "status": "configured",
        "source": "pyproject.toml [tool.traceh-python-quality]",
        "error": None,
    }
    assert "unittest" not in json.dumps(inspection.to_data())


@pytest.mark.parametrize("marker", ["pyproject", "pytest.ini"])
def test_pytest_is_inferred_only_from_declared_project_evidence(
    tmp_path: Path,
    marker: str,
) -> None:
    if marker == "pyproject":
        write_pyproject(tmp_path, "[tool.pytest.ini_options]\naddopts = '-q'\n")
    else:
        (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")

    inspection = inspect_python_project(tmp_path)

    assert inspection.pytest_configured is True
    assert inspection.verification.argv == (sys.executable, "-m", "pytest", "-q")
    assert inspection.verification.source == "declared pytest configuration"


def test_missing_runner_is_reported_instead_of_guessed(tmp_path: Path) -> None:
    write_pyproject(tmp_path, "[project]\nname = 'no-runner'\n")

    inspection = inspect_python_project(tmp_path)

    assert inspection.verification.argv is None
    assert inspection.verification.source == "none"
    assert "no test runner is declared" in str(inspection.verification.error)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("'python -m unittest'", "non-empty array"),
        ("[]", "non-empty array"),
        ("['python', '']", "non-empty strings"),
    ],
)
def test_invalid_explicit_command_fails_closed(
    tmp_path: Path,
    value: str,
    expected: str,
) -> None:
    write_pyproject(
        tmp_path,
        f"[tool.traceh-python-quality]\ntest-command = {value}\n",
    )

    inspection = inspect_python_project(tmp_path)

    assert inspection.verification.argv is None
    assert expected in str(inspection.verification.error)


async def test_project_info_tool_returns_bounded_structured_evidence(tmp_path: Path) -> None:
    write_pyproject(tmp_path, "[project]\nname = 'tool-fixture'\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    tool = PythonProjectInfoTool()
    context = ToolExecutionContext("s", "t", "p", "c", tmp_path, tmp_path / "data")

    result = await tool.execute({}, context)

    assert result.data["project_name"] == "tool-fixture"
    assert result.data["src_layout"] is True
    assert result.data["tests_directory"] is True
    assert result.evidence == ("pyproject.toml",)
    assert str(tmp_path) not in result.content


@pytest.mark.parametrize(
    "command",
    [
        "python -m pip uninstall sample-package -y",
        "py -3 -m pip uninstall sample-package -y",
        "pip install --user sample-package",
        "pip3 install --target vendor sample-package",
        "pip install -t vendor sample-package",
        "python.exe -m pip install --prefix=outside sample-package",
    ],
)
async def test_policy_denies_environment_removal_or_external_install_targets(
    tmp_path: Path,
    command: str,
) -> None:
    policy = PythonEnvironmentSafetyPolicy()
    call = ToolCall("c", "shell", {"command": command})
    context = ToolExecutionContext("s", "t", "p", "c", tmp_path, tmp_path / "data")

    decision = await policy.check(call, SimpleNamespace(name="shell"), context)

    assert decision.kind is DecisionKind.DENY
    assert decision.policy == policy.name


async def test_policy_defers_safe_or_unrelated_commands(tmp_path: Path) -> None:
    policy = PythonEnvironmentSafetyPolicy()
    context = ToolExecutionContext("s", "t", "p", "c", tmp_path, tmp_path / "data")
    for command in ("python -m pip install sample-package", "python -m unittest -v"):
        decision = await policy.check(
            ToolCall("c", "shell", {"command": command}),
            SimpleNamespace(name="shell"),
            context,
        )
        assert decision.kind is DecisionKind.DEFER


async def test_named_verifier_runs_the_explicit_project_command(tmp_path: Path) -> None:
    test_module = tmp_path / "test_quality.py"
    test_module.write_text(
        "import unittest\n\n"
        "class QualityTest(unittest.TestCase):\n"
        "    def test_truth(self):\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    command = json.dumps([sys.executable, "-m", "unittest", "-v"])
    write_pyproject(
        tmp_path,
        f"[tool.traceh-python-quality]\ntest-command = {command}\n",
    )

    result = await PythonTestsVerifier().verify(tmp_path)

    assert result.passed is True
    assert result.exit_code == 0
    assert "OK" in result.stderr


async def test_named_verifier_fails_without_project_evidence(tmp_path: Path) -> None:
    result = await PythonTestsVerifier().verify(tmp_path)

    assert result.passed is False
    assert result.exit_code is None
    assert "no test runner is declared" in result.summary
