"""A real TraceHarness plugin for evidence-driven Python project work.

The distribution is intentionally independent from ``traceharness-py``. It is
discovered through the public ``traceh.plugins`` entry-point group and uses only
the public Plugin SDK re-exported by :mod:`traceh.plugins`.

It never reads user directories, environment variables or the network. Project
inspection is limited to fixed files at the workspace root, and every path is
resolved back inside that workspace before it is read.
"""

from __future__ import annotations

import json
import math
import shlex
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from traceh.plugins import (
    CommandVerifier,
    DecisionKind,
    EffectKind,
    PluginContext,
    PluginManifest,
    PromptSection,
    Tool,
    ToolCall,
    ToolDecision,
    ToolExecutionContext,
    ToolOutput,
    VerificationResult,
)

PLUGIN_ID = "traceh.python.quality"
PLUGIN_VERSION = "0.2.2"
VERIFIER_NAME = "python-tests"
CONFIG_TABLE = "traceh-python-quality"
MAX_CONFIG_BYTES = 1_000_000
DEFAULT_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class VerificationPlan:
    """A resolved test command whose source is explicit project evidence."""

    argv: tuple[str, ...] | None
    source: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    error: str | None = None

    @property
    def configured(self) -> bool:
        return self.argv is not None and self.error is None


@dataclass(frozen=True, slots=True)
class PythonProjectInspection:
    project_name: str | None
    requires_python: str | None
    pyproject_present: bool
    src_layout: bool
    tests_directory: bool
    pytest_configured: bool
    verification: VerificationPlan
    evidence: tuple[str, ...]

    def to_data(self) -> dict[str, object]:
        status = "configured" if self.verification.configured else "unavailable"
        if self.verification.error is not None:
            status = "invalid"
        return {
            "project_name": self.project_name,
            "requires_python": self.requires_python,
            "pyproject_present": self.pyproject_present,
            "src_layout": self.src_layout,
            "tests_directory": self.tests_directory,
            "pytest_configured": self.pytest_configured,
            "verification": {
                "status": status,
                "source": self.verification.source,
                "error": self.verification.error,
            },
            "evidence": list(self.evidence),
        }


def _workspace_entry(workspace: Path, name: str) -> Path | None:
    root = workspace.resolve()
    candidate = (root / name).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _read_root_file(workspace: Path, name: str) -> bytes | None:
    path = _workspace_entry(workspace, name)
    if path is None or not path.is_file():
        return None
    if path.stat().st_size > MAX_CONFIG_BYTES:
        raise ValueError(f"{name} exceeds the {MAX_CONFIG_BYTES}-byte inspection limit")
    return path.read_bytes()


def _root_directory_exists(workspace: Path, name: str) -> bool:
    path = _workspace_entry(workspace, name)
    return path is not None and path.is_dir()


def _positive_timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("timeout-seconds must be a positive finite number")
    timeout = float(value)
    if timeout <= 0 or not math.isfinite(timeout):
        raise ValueError("timeout-seconds must be a positive finite number")
    return timeout


def _command_argv(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("test-command must be a non-empty array of strings")
    if any(not isinstance(item, str) or not item for item in value):
        raise ValueError("test-command must contain only non-empty strings")
    return tuple(value)


def inspect_python_project(workspace: Path) -> PythonProjectInspection:
    """Inspect fixed project-root evidence without executing project code."""

    evidence: list[str] = []
    project_name: str | None = None
    requires_python: str | None = None
    pyproject_data: dict[str, Any] = {}
    pyproject_present = False

    try:
        raw_pyproject = _read_root_file(workspace, "pyproject.toml")
    except (OSError, ValueError) as error:
        plan = VerificationPlan(None, "pyproject.toml", error=str(error))
        return PythonProjectInspection(
            None,
            None,
            True,
            _root_directory_exists(workspace, "src"),
            _root_directory_exists(workspace, "tests"),
            False,
            plan,
            ("pyproject.toml",),
        )

    if raw_pyproject is not None:
        pyproject_present = True
        evidence.append("pyproject.toml")
        try:
            pyproject_data = tomllib.loads(raw_pyproject.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
            plan = VerificationPlan(
                None,
                "pyproject.toml",
                error=f"pyproject.toml is not valid UTF-8 TOML: {error}",
            )
            return PythonProjectInspection(
                None,
                None,
                True,
                _root_directory_exists(workspace, "src"),
                _root_directory_exists(workspace, "tests"),
                False,
                plan,
                tuple(evidence),
            )
        project = pyproject_data.get("project")
        if isinstance(project, dict):
            name = project.get("name")
            requires = project.get("requires-python")
            project_name = name if isinstance(name, str) else None
            requires_python = requires if isinstance(requires, str) else None

    tool = pyproject_data.get("tool")
    tool_table = tool if isinstance(tool, dict) else {}
    pytest_configured = isinstance(tool_table.get("pytest"), dict)
    pytest_ini = _workspace_entry(workspace, "pytest.ini")
    if pytest_ini is not None and pytest_ini.is_file():
        pytest_configured = True
        evidence.append("pytest.ini")

    plugin_config = tool_table.get(CONFIG_TABLE)
    if plugin_config is not None:
        if not isinstance(plugin_config, dict):
            plan = VerificationPlan(
                None,
                "pyproject.toml [tool.traceh-python-quality]",
                error="tool.traceh-python-quality must be a TOML table",
            )
        else:
            try:
                argv = _command_argv(plugin_config.get("test-command"))
                timeout = _positive_timeout(
                    plugin_config.get("timeout-seconds", DEFAULT_TIMEOUT_SECONDS)
                )
            except ValueError as error:
                plan = VerificationPlan(
                    None,
                    "pyproject.toml [tool.traceh-python-quality]",
                    error=str(error),
                )
            else:
                plan = VerificationPlan(
                    argv,
                    "pyproject.toml [tool.traceh-python-quality]",
                    timeout_seconds=timeout,
                )
    elif pytest_configured:
        plan = VerificationPlan(
            (sys.executable, "-m", "pytest", "-q"),
            "declared pytest configuration",
        )
    else:
        plan = VerificationPlan(
            None,
            "none",
            error=(
                "no test runner is declared; add [tool.traceh-python-quality] with a "
                "test-command array, or add pytest configuration"
            ),
        )

    return PythonProjectInspection(
        project_name,
        requires_python,
        pyproject_present,
        _root_directory_exists(workspace, "src"),
        _root_directory_exists(workspace, "tests"),
        pytest_configured,
        plan,
        tuple(evidence),
    )


class PythonProjectInfoTool:
    """Report bounded Python project facts and verifier readiness."""

    name = "python_project_info"
    description = (
        "Inspect fixed Python project metadata at the workspace root and report whether the "
        "python-tests verifier has an explicit test plan. Takes no arguments and executes no code."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    effect_kind = EffectKind.WORKSPACE_READ

    async def execute(
        self,
        arguments: dict[str, object],
        context: ToolExecutionContext,
    ) -> ToolOutput:
        del arguments
        inspection = inspect_python_project(context.workspace)
        data = inspection.to_data()
        return ToolOutput(
            content=json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
            data=data,
            evidence=inspection.evidence,
        )


def _command_words(command: str) -> list[str] | None:
    try:
        return shlex.split(command)
    except ValueError:
        return None


def _program_name(value: str) -> str:
    name = value.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name.removesuffix(".exe")


def _pip_arguments(argv: list[str]) -> list[str] | None:
    if not argv:
        return None
    executable = _program_name(argv[0])
    version_suffix = executable[3:].replace(".", "")
    if executable == "pip" or (executable.startswith("pip") and version_suffix.isdigit()):
        return argv[1:]
    if executable in {"python", "py"} or executable.startswith("python"):
        module_index = 1
        if executable == "py" and len(argv) > 1:
            launcher_version = argv[1].removeprefix("-").replace(".", "")
            if argv[1].startswith("-") and launcher_version.isdigit():
                module_index += 1
        if argv[module_index : module_index + 2] == ["-m", "pip"]:
            return argv[module_index + 2 :]
    return None


class PythonEnvironmentSafetyPolicy:
    """Deny the narrow pip operations that escape or remove the active environment."""

    name = "python-environment-safety"
    _external_location_flags = ("--user", "--prefix", "--root", "--target", "-t")

    async def check(
        self,
        call: ToolCall,
        tool: Tool,
        context: ToolExecutionContext,
    ) -> ToolDecision:
        del context
        if tool.name != "shell":
            return ToolDecision(DecisionKind.DEFER, policy=self.name)
        command = call.arguments.get("command")
        if not isinstance(command, str):
            return ToolDecision(DecisionKind.DEFER, policy=self.name)
        argv = _command_words(command)
        if argv is None:
            return ToolDecision(DecisionKind.DEFER, policy=self.name)
        pip_arguments = _pip_arguments(argv)
        if not pip_arguments:
            return ToolDecision(DecisionKind.DEFER, policy=self.name)

        action = pip_arguments[0].lower()
        if action == "uninstall":
            return ToolDecision(
                DecisionKind.DENY,
                "pip uninstall mutates the active Python environment",
                self.name,
            )
        if action == "install":
            for argument in pip_arguments[1:]:
                if any(
                    argument == flag or argument.startswith(f"{flag}=")
                    for flag in self._external_location_flags
                ):
                    return ToolDecision(
                        DecisionKind.DENY,
                        "pip install may not target a user, prefix, root or custom directory",
                        self.name,
                    )
        return ToolDecision(DecisionKind.DEFER, policy=self.name)


class PythonTestsVerifier:
    """Run only the project test command that explicit project evidence resolves."""

    async def verify(self, workspace: Path) -> VerificationResult:
        inspection = inspect_python_project(workspace)
        plan = inspection.verification
        if not plan.configured or plan.argv is None:
            return VerificationResult(
                False,
                f"Python Quality verifier is unavailable: {plan.error or 'no test plan'}.",
            )
        command = shlex.join(plan.argv)
        return await CommandVerifier(command, plan.timeout_seconds).verify(workspace)


class PythonQualityPlugin:
    """Entry point for the independent Python Quality plugin distribution."""

    manifest = PluginManifest(
        plugin_id=PLUGIN_ID,
        version=PLUGIN_VERSION,
        requires_traceh=">=0.5,<0.9",
        allowed_scopes=("application",),
        trust_mode="trusted",
        provides=(
            "python.project-info",
            "python.quality-guidance",
            "python.environment-safety",
            "python.tests-verifier",
        ),
    )

    async def setup(self, context: PluginContext, config: dict[str, object]) -> None:
        del config
        context.register_prompt(
            PromptSection(
                "traceh.python.quality",
                (
                    "For Python work, call python_project_info before changing the project. "
                    "Treat its verification status as evidence, keep changes inside the workspace, "
                    "and do not claim tests passed unless the configured verifier reports success."
                ),
                priority=35,
            )
        )
        context.register_tool(PythonProjectInfoTool())
        context.register_policy(PythonEnvironmentSafetyPolicy())
        context.register_verifier(VERIFIER_NAME, PythonTestsVerifier())

    async def health_check(self, context: PluginContext) -> bool:
        del context
        return True


__all__ = [
    "CONFIG_TABLE",
    "PLUGIN_ID",
    "PLUGIN_VERSION",
    "VERIFIER_NAME",
    "PythonEnvironmentSafetyPolicy",
    "PythonProjectInfoTool",
    "PythonQualityPlugin",
    "PythonTestsVerifier",
    "VerificationPlan",
    "inspect_python_project",
]
