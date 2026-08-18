"""Manifest validation covers every field and reports all failures at once."""

from __future__ import annotations

import pytest

from traceh.api.plugins import PluginDependency, PluginManifest
from traceh.plugins.manager import validate_manifest
from traceh.version import __version__


def codes(manifest: object, *, expected_id: str = "a.plugin") -> set[str]:
    return {failure.code for failure in validate_manifest(manifest, expected_id=expected_id)}


def test_minimal_valid_manifest_passes() -> None:
    assert validate_manifest(PluginManifest("a.plugin", "1.0.0"), expected_id="a.plugin") == ()


def test_non_manifest_value_is_rejected() -> None:
    assert codes({"plugin_id": "a.plugin"}) == {"manifest-type-invalid"}
    assert codes(None) == {"manifest-type-invalid"}


def test_plugin_id_must_be_valid() -> None:
    assert "plugin-id-invalid" in codes(PluginManifest("Not Valid", "1.0.0"))


def test_plugin_id_must_match_the_entry_point_name() -> None:
    """Otherwise discovery and the running plugin disagree about what is loaded."""

    assert "plugin-id-mismatch" in codes(PluginManifest("b.other", "1.0.0"))


def test_core_plugin_id_is_reserved() -> None:
    assert "plugin-id-reserved" in codes(
        PluginManifest("traceh.core", "1.0.0"), expected_id="traceh.core"
    )


@pytest.mark.parametrize("version", ["not-a-version", "1.0.0.0.0.x", ""])
def test_plugin_version_must_be_pep440(version: str) -> None:
    assert "plugin-version-invalid" in codes(PluginManifest("a.plugin", version))


def test_plugin_version_must_be_a_string() -> None:
    assert "plugin-version-invalid" in codes(PluginManifest("a.plugin", 1))  # type: ignore[arg-type]


def test_requires_traceh_must_be_a_valid_specifier() -> None:
    assert "traceh-api-requirement-invalid" in codes(
        PluginManifest("a.plugin", "1.0.0", requires_traceh="not a specifier")
    )


def test_requires_traceh_must_admit_the_running_version() -> None:
    assert "traceh-api-incompatible" in codes(
        PluginManifest("a.plugin", "1.0.0", requires_traceh=">=99.0")
    )


def test_requires_traceh_matching_current_version_passes() -> None:
    assert (
        validate_manifest(
            PluginManifest("a.plugin", "1.0.0", requires_traceh=f"=={__version__}"),
            expected_id="a.plugin",
        )
        == ()
    )


def test_dependency_entry_must_be_a_plugin_dependency() -> None:
    assert "invalid-dependency" in codes(
        PluginManifest("a.plugin", "1.0.0", requires_plugins=("b.other",))  # type: ignore[arg-type]
    )


def test_dependency_id_must_be_valid() -> None:
    assert "invalid-dependency-id" in codes(
        PluginManifest("a.plugin", "1.0.0", requires_plugins=(PluginDependency("Bad Id", ">=1"),))
    )


def test_dependency_version_spec_must_be_pep440() -> None:
    assert "invalid-dependency-version" in codes(
        PluginManifest(
            "a.plugin", "1.0.0", requires_plugins=(PluginDependency("b.other", "not a spec"),)
        )
    )


def test_duplicate_dependency_is_rejected() -> None:
    assert "duplicate-dependency" in codes(
        PluginManifest(
            "a.plugin",
            "1.0.0",
            requires_plugins=(
                PluginDependency("b.other", ">=1"),
                PluginDependency("b.other", ">=2"),
            ),
        )
    )


def test_dependency_cannot_be_required_and_optional() -> None:
    assert "dependency-kind-conflict" in codes(
        PluginManifest(
            "a.plugin",
            "1.0.0",
            requires_plugins=(PluginDependency("b.other", ">=1"),),
            optional_plugins=(PluginDependency("b.other", ">=1"),),
        )
    )


def test_dependency_lists_must_be_tuples() -> None:
    assert "dependency-list-invalid" in codes(
        PluginManifest("a.plugin", "1.0.0", requires_plugins=[])  # type: ignore[arg-type]
    )


def test_allowed_scopes_must_be_non_empty() -> None:
    assert "allowed-scopes-invalid" in codes(PluginManifest("a.plugin", "1.0.0", allowed_scopes=()))


def test_unknown_scope_is_rejected() -> None:
    assert "allowed-scopes-invalid" in codes(
        PluginManifest("a.plugin", "1.0.0", allowed_scopes=("application", "galaxy"))
    )


def test_duplicate_scope_is_rejected() -> None:
    assert "allowed-scopes-duplicate" in codes(
        PluginManifest("a.plugin", "1.0.0", allowed_scopes=("application", "application"))
    )


def test_application_scope_is_required_in_v04() -> None:
    assert "application-scope-not-allowed" in codes(
        PluginManifest("a.plugin", "1.0.0", allowed_scopes=("workspace",))
    )


def test_isolated_trust_mode_is_explicitly_rejected() -> None:
    """Rejected, not silently downgraded to trusted in-process execution."""

    failures = validate_manifest(
        PluginManifest("a.plugin", "1.0.0", trust_mode="isolated"), expected_id="a.plugin"
    )
    assert [failure.code for failure in failures] == ["isolated-mode-unsupported"]
    assert "not implemented in v0.4" in failures[0].message


def test_unknown_trust_mode_is_rejected() -> None:
    assert "trust-mode-invalid" in codes(PluginManifest("a.plugin", "1.0.0", trust_mode="yolo"))


def test_provides_must_be_a_tuple_of_valid_ids() -> None:
    assert "provides-invalid" in codes(PluginManifest("a.plugin", "1.0.0", provides=("bad id",)))
    assert "provides-invalid" in codes(
        PluginManifest("a.plugin", "1.0.0", provides="cap")  # type: ignore[arg-type]
    )


def test_duplicate_provides_is_rejected() -> None:
    assert "provides-duplicate" in codes(
        PluginManifest("a.plugin", "1.0.0", provides=("cap", "cap"))
    )


def test_all_failures_are_reported_together() -> None:
    """An author fixing a manifest should see everything in one run."""

    reported = codes(
        PluginManifest(
            "b.other",
            "bad-version",
            requires_traceh=">=99.0",
            allowed_scopes=("workspace",),
            trust_mode="isolated",
            provides=("bad id",),
        )
    )
    assert {
        "plugin-id-mismatch",
        "plugin-version-invalid",
        "traceh-api-incompatible",
        "application-scope-not-allowed",
        "isolated-mode-unsupported",
        "provides-invalid",
    } <= reported


def test_defaults_are_application_scoped_and_trusted() -> None:
    default = PluginManifest("a.plugin", "1.0.0")
    assert default.allowed_scopes == ("application",)
    assert default.trust_mode == "trusted"
    assert default.requires_plugins == ()
    assert default.optional_plugins == ()
    assert default.provides == ()
