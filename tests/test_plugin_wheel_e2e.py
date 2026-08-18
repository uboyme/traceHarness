"""Real wheels, a real clean virtual environment, a real entry point.

Everything else in the plugin suite substitutes a fake ``entry_points`` provider.
That is fine for driving the manager deterministically, but it proves nothing
about packaging: it cannot show that a separately built distribution declaring
``traceharness-py>=0.4,<1.0`` actually installs alongside this build, or that
``importlib.metadata`` finds it.

So this module builds both wheels, populates an offline wheelhouse (including
``packaging``, v0.4's first runtime dependency), creates a fresh venv, installs
with ``--no-index``, and runs the whole mainline inside it.

It is slow by nature - one venv and three wheel builds - so it is marked
``slow``. Deselect with ``-m "not slow"``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_PLUGIN = PROJECT_ROOT / "examples" / "plugins" / "traceh-example-skill-plugin"
DRIVER = Path(__file__).resolve().parent / "plugin_e2e_driver.py"

pytestmark = pytest.mark.slow


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **kwargs,
    )


def venv_python(venv: Path) -> Path:
    if sys.platform == "win32":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


@pytest.fixture(scope="module")
def wheelhouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build both wheels and fetch packaging into one offline install source."""

    house = tmp_path_factory.mktemp("wheelhouse")

    for project in (PROJECT_ROOT, EXAMPLE_PLUGIN):
        built = run(
            [sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(house),
             str(project)]
        )
        assert built.returncode == 0, f"building {project.name} failed:\n{built.stderr}"

    # packaging is a real runtime dependency now, so an offline install of
    # TraceHarness needs its wheel present too. Acquiring it may need the network
    # or a warm pip cache; the install itself is then strictly offline.
    fetched = run(
        [sys.executable, "-m", "pip", "download", "packaging", "--no-deps", "--dest", str(house)]
    )
    if fetched.returncode != 0:
        pytest.skip(f"cannot populate offline wheelhouse with packaging: {fetched.stderr[-400:]}")

    return house


@pytest.fixture(scope="module")
def clean_environment(wheelhouse: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    venv = tmp_path_factory.mktemp("venv-root") / "venv"
    created = run([sys.executable, "-m", "venv", str(venv)])
    assert created.returncode == 0, f"venv creation failed:\n{created.stderr}"

    python = venv_python(venv)
    installed = run(
        [
            str(python), "-m", "pip", "install",
            "--no-index", "--find-links", str(wheelhouse),
            "traceharness-py", "traceh-example-skill-plugin",
        ]
    )
    assert installed.returncode == 0, (
        f"offline install failed:\n{installed.stdout}\n{installed.stderr}"
    )
    return venv


@pytest.fixture(scope="module")
def e2e_report(clean_environment: Path, tmp_path_factory: pytest.TempPathFactory) -> dict:
    scratch = tmp_path_factory.mktemp("e2e-scratch")
    driver = scratch / "plugin_e2e_driver.py"
    shutil.copyfile(DRIVER, driver)

    completed = run([str(venv_python(clean_environment)), str(driver), str(scratch / "work")])
    assert completed.returncode == 0, (
        f"driver failed inside the clean venv:\n{completed.stdout}\n{completed.stderr}"
    )
    marker = "<<<E2E_JSON>>>"
    assert marker in completed.stdout, completed.stdout
    return json.loads(completed.stdout.split(marker, 1)[1])


# --------------------------------------------------------------------------
# Packaging and discovery
# --------------------------------------------------------------------------


def test_wheels_install_together_in_a_clean_environment(e2e_report: dict) -> None:
    """The blocking acceptance: a plugin declaring >=0.4,<1.0 installs with this build."""

    from traceh.version import __version__

    versions = e2e_report["installed_versions"]
    assert versions["traceharness-py"] == __version__
    assert versions["traceh-example-skill-plugin"] == "0.1.0"
    assert versions["packaging"], "packaging must be installed from the offline wheelhouse"


def test_wheel_metadata_version_matches_the_package(e2e_report: dict) -> None:
    from traceh.version import __version__

    assert e2e_report["traceh_version"] == __version__
    assert e2e_report["installed_versions"]["traceharness-py"] == __version__


def test_real_entry_point_is_discovered(e2e_report: dict) -> None:
    names = [point["name"] for point in e2e_report["entry_points"]]
    assert "traceh.example.skill" in names
    value = next(
        p["value"] for p in e2e_report["entry_points"] if p["name"] == "traceh.example.skill"
    )
    assert value == "traceh_example_skill_plugin:ExampleSkillPlugin"


def test_discovery_reports_the_plugin_without_issues(e2e_report: dict) -> None:
    record = next(
        item for item in e2e_report["discovered"] if item["name"] == "traceh.example.skill"
    )
    assert record["issues"] == []
    assert record["distribution"] == "traceh-example-skill-plugin"
    assert ">=0.4" in record["requirement"]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_cli_list_inspect_and_doctor_all_succeed(e2e_report: dict) -> None:
    assert e2e_report["cli"] == {"list": 0, "inspect": 0, "doctor": 0}


# --------------------------------------------------------------------------
# The mainline turn
# --------------------------------------------------------------------------


def test_model_sees_the_plugin_tool_and_prompt(e2e_report: dict) -> None:
    visible = e2e_report["model_visible"]
    assert "example_skill_info" in visible["tool_schemas"]
    assert visible["prompt_contains_section"] is True


def test_turn_completes_and_the_plugin_tool_really_ran(e2e_report: dict) -> None:
    assert e2e_report["turn"]["reason"] == "completed"
    pairing = e2e_report["pairing"]
    assert pairing["called_tool_names"] == ["example_skill_info"]
    assert pairing["result_mentions_skill"] is True


def test_tool_calls_and_results_pair_exactly(e2e_report: dict) -> None:
    pairing = e2e_report["pairing"]
    assert pairing["tool_calls"] == pairing["tool_results"] == 1


def test_effect_intents_and_outcomes_pair_exactly(e2e_report: dict) -> None:
    pairing = e2e_report["pairing"]
    assert pairing["effect_intents"] == pairing["effect_outcomes"] == 1


def test_composition_snapshot_contains_the_real_plugin_identity(e2e_report: dict) -> None:
    from traceh.version import __version__

    assert e2e_report["composition_plugins"] == [
        {"plugin_id": "traceh.core", "version": __version__},
        {"plugin_id": "traceh.example.skill", "version": "0.1.0"},
    ]


def test_session_records_the_plugin_identity(e2e_report: dict) -> None:
    assert e2e_report["session_plugin_metadata"] == [
        {"plugin_id": "traceh.example.skill", "version": "0.1.0"}
    ]


def test_no_invariant_violations(e2e_report: dict) -> None:
    assert e2e_report["invariant_violations"] == []


def test_no_request_reconstruction_violations(e2e_report: dict) -> None:
    assert e2e_report["reconstruction_violations"] == []


# --------------------------------------------------------------------------
# The unchanged default
# --------------------------------------------------------------------------


def test_default_runtime_is_untouched_even_though_the_plugin_is_installed(
    e2e_report: dict,
) -> None:
    from traceh.version import __version__

    plain = e2e_report["plain_runtime"]
    assert plain["tools"] == [
        "apply_patch",
        "list_files",
        "read_file",
        "search_text",
        "shell",
    ]
    assert plain["plugins"] == [{"plugin_id": "traceh.core", "version": __version__}]
    assert plain["prompt_has_plugin_section"] is False


def test_dispose_removes_the_plugin_tool(e2e_report: dict) -> None:
    assert "example_skill_info" not in e2e_report["after_dispose_tools"]
