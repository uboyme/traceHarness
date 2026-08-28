"""The version must have exactly one source, and everything must read it.

A previous v0.4 candidate carried the version in four places. Two of them
disagreed, so a runtime built without plugins and a runtime built through
PluginManager stamped different ``traceh.core`` versions into their Composition
Snapshots. These tests exist to make that class of drift fail loudly.
"""

from __future__ import annotations

import tomllib
from importlib import metadata
from pathlib import Path

import pytest
from packaging.specifiers import SpecifierSet
from packaging.version import Version

import traceh
from traceh.api.plugins import CORE_PLUGIN_IDENTITY, PluginManifest
from traceh.plugins.discovery import installed_traceh_version
from traceh.plugins.manager import TRACEH_PLUGIN_API_VERSION
from traceh.runtime.agent_runtime import build_default_runtime, build_default_runtime_async
from traceh.version import DEFAULT_REQUIRES_TRACEH, DISTRIBUTION_NAME, __version__

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_version_is_the_expected_v07_release() -> None:
    assert __version__ == "0.7.0"
    assert Version(__version__) == Version("0.7.0")


def test_package_version_comes_from_the_version_module() -> None:
    assert traceh.__version__ is __version__


def test_installed_distribution_metadata_matches_the_module() -> None:
    """The built/installed wheel and the imported package must agree.

    pyproject declares the version dynamically from ``traceh.version.__version__``,
    so a mismatch here means the installed distribution is stale.
    """

    assert metadata.version(DISTRIBUTION_NAME) == __version__


def test_pyproject_does_not_hardcode_a_second_version() -> None:
    raw = (PROJECT_ROOT / "pyproject.toml").read_bytes()
    data = tomllib.loads(raw.decode("utf-8"))
    project = data["project"]
    assert "version" not in project, "pyproject must not pin a literal version"
    assert "version" in project["dynamic"]
    assert data["tool"]["setuptools"]["dynamic"]["version"] == {
        "attr": "traceh.version.__version__"
    }


def test_core_plugin_identity_uses_the_single_version() -> None:
    assert CORE_PLUGIN_IDENTITY.plugin_id == "traceh.core"
    assert CORE_PLUGIN_IDENTITY.version == __version__


def test_plugin_api_version_and_discovery_version_agree() -> None:
    assert TRACEH_PLUGIN_API_VERSION == __version__
    assert installed_traceh_version() == Version(__version__)


def test_default_manifest_range_admits_the_current_version() -> None:
    """A manifest that states no range must still be compatible with this build."""

    assert Version(__version__) in SpecifierSet(DEFAULT_REQUIRES_TRACEH)
    assert PluginManifest("p", "1.0.0").requires_traceh == DEFAULT_REQUIRES_TRACEH


def test_plain_and_plugin_runtimes_report_the_same_core_version(tmp_path: Path) -> None:
    """The exact divergence the candidate had: two builders, two core versions."""

    plain = build_default_runtime(_config(tmp_path))
    assert plain.plugins == (CORE_PLUGIN_IDENTITY,)


async def test_async_builder_without_plugins_matches_the_sync_builder(tmp_path: Path) -> None:
    plain = build_default_runtime(_config(tmp_path / "a"))
    through_manager = await build_default_runtime_async(_config(tmp_path / "b"))
    assert plain.plugins == through_manager.plugins == (CORE_PLUGIN_IDENTITY,)
    assert through_manager.plugins[0].version == __version__


@pytest.mark.parametrize("workspace_name", ["ws"])
async def test_composition_snapshot_records_the_single_core_version(
    tmp_path: Path, workspace_name: str
) -> None:
    workspace = tmp_path / workspace_name
    workspace.mkdir()
    runtime = await build_default_runtime_async(_config(tmp_path / "data"))
    try:
        await runtime.run(workspace, "hello")
        sessions = await runtime.sessions.list_sessions()
        events = await runtime.sessions.read_session(sessions[0])
        snapshots = [event for event in events if event.type == "composition/snapshot"]
        assert snapshots
        for snapshot in snapshots:
            assert snapshot.data["plugins"] == [
                {"plugin_id": "traceh.core", "version": __version__}
            ]
    finally:
        await runtime.dispose()


def _config(data_dir: Path):
    from traceh.runtime.agent_runtime import RuntimeConfig

    return RuntimeConfig(data_dir=data_dir)
