"""Discovery reads metadata and must never import a plugin.

That separation is the security property behind ``traceh plugins list``: listing
what is installed is not itself a code-execution step.
"""

from __future__ import annotations

from plugin_fixtures import (
    COMPATIBLE_REQUIREMENT,
    FakeDistribution,
    FakeEntryPoint,
    LoadCounter,
    provider_for,
)

from traceh.plugins.discovery import ENTRY_POINT_GROUP, PluginDiscovery
from traceh.version import __version__


def _discover(*points: FakeEntryPoint):
    return PluginDiscovery(entry_points_provider=provider_for(*points)).discover()


def test_discovery_does_not_import_any_plugin() -> None:
    counter = LoadCounter()
    point = FakeEntryPoint(name="a.plugin", counter=counter)

    records = _discover(point)

    assert [record.entry_name for record in records] == ["a.plugin"]
    assert counter.loaded == [], "discovery must not call EntryPoint.load()"


def test_discovery_reports_metadata_without_manifest() -> None:
    records = _discover(FakeEntryPoint(name="a.plugin", value="mod:Plugin"))
    payload = records[0].to_dict()

    assert payload["state"] == "discovered"
    assert payload["entry_point"] == {
        "group": ENTRY_POINT_GROUP,
        "name": "a.plugin",
        "value": "mod:Plugin",
    }
    assert payload["distribution"] == {"name": "example-dist", "version": "1.0.0"}
    # Stated, not implied: the manifest genuinely was not read.
    assert payload["manifest"] == {"available": False, "requires_import": True}
    assert payload["issues"] == []


def test_entry_point_name_that_is_not_a_plugin_id_is_an_issue() -> None:
    records = _discover(FakeEntryPoint(name="Not A Plugin Id"))
    codes = [issue.code for issue in records[0].issues]
    assert "invalid-entry-point-name" in codes


def test_duplicate_entry_point_name_marks_every_claimant() -> None:
    """Ambiguity is never resolved silently in favour of one distribution."""

    records = _discover(
        FakeEntryPoint(name="dup", value="one:Plugin", dist=FakeDistribution("dist-one")),
        FakeEntryPoint(name="dup", value="two:Plugin", dist=FakeDistribution("dist-two")),
    )
    assert len(records) == 2
    for record in records:
        assert "duplicate-entry-point" in [issue.code for issue in record.issues]


def test_missing_traceh_dependency_is_an_issue() -> None:
    records = _discover(
        FakeEntryPoint(name="a.plugin", dist=FakeDistribution(requires=("requests>=2",)))
    )
    assert "traceh-dependency-missing" in [issue.code for issue in records[0].issues]


def test_duplicate_traceh_dependency_is_an_issue() -> None:
    records = _discover(
        FakeEntryPoint(
            name="a.plugin",
            dist=FakeDistribution(requires=(COMPATIBLE_REQUIREMENT, "traceharness-py>=0.1")),
        )
    )
    assert "traceh-dependency-duplicate" in [issue.code for issue in records[0].issues]


def test_incompatible_traceh_requirement_is_an_issue() -> None:
    records = _discover(
        FakeEntryPoint(name="a.plugin", dist=FakeDistribution(requires=("traceharness-py<0.2",)))
    )
    assert "traceh-distribution-incompatible" in [issue.code for issue in records[0].issues]


def test_compatible_requirement_records_the_specifier() -> None:
    records = _discover(FakeEntryPoint(name="a.plugin"))
    assert records[0].issues == ()
    assert f">={__version__}" in records[0].traceh_requirement


def test_missing_distribution_metadata_is_an_issue() -> None:
    records = _discover(FakeEntryPoint(name="a.plugin", dist=FakeDistribution(name=None)))
    assert "distribution-metadata-missing" in [issue.code for issue in records[0].issues]


def test_invalid_distribution_version_is_an_issue() -> None:
    records = _discover(
        FakeEntryPoint(name="a.plugin", dist=FakeDistribution(version="not-a-version"))
    )
    assert "distribution-version-invalid" in [issue.code for issue in records[0].issues]


def test_unreadable_requirements_are_an_issue() -> None:
    class UnreadableRequires(FakeDistribution):
        @property
        def requires(self):  # type: ignore[override]
            raise RuntimeError("metadata is corrupt")

        @requires.setter
        def requires(self, value):  # type: ignore[misc]
            pass

    records = _discover(FakeEntryPoint(name="a.plugin", dist=UnreadableRequires()))
    assert "distribution-requirements-missing" in [issue.code for issue in records[0].issues]


def test_distribution_with_no_declared_requirements_reports_missing_dependency() -> None:
    records = _discover(FakeEntryPoint(name="a.plugin", dist=FakeDistribution(requires=())))
    assert "traceh-dependency-missing" in [issue.code for issue in records[0].issues]


def test_invalid_requirement_string_is_an_issue() -> None:
    records = _discover(
        FakeEntryPoint(
            name="a.plugin",
            dist=FakeDistribution(requires=("not a requirement!!", COMPATIBLE_REQUIREMENT)),
        )
    )
    assert "distribution-requirement-invalid" in [issue.code for issue in records[0].issues]


def test_entry_points_from_other_groups_are_ignored() -> None:
    records = _discover(
        FakeEntryPoint(name="a.plugin"),
        FakeEntryPoint(name="console.thing", group="console_scripts"),
    )
    assert [record.entry_name for record in records] == ["a.plugin"]


def test_broken_metadata_provider_becomes_one_synthetic_record() -> None:
    """A broken provider must not leak its exception or traceback to the CLI."""

    def exploding_provider(**_kwargs):
        raise RuntimeError("metadata backend exploded with secret=abc")

    records = PluginDiscovery(entry_points_provider=exploding_provider).discover()

    assert len(records) == 1
    assert records[0].entry_name == "<metadata>"
    assert [issue.code for issue in records[0].issues] == ["entry-point-metadata-error"]
    assert "secret" not in records[0].issues[0].message


def test_records_are_sorted_deterministically() -> None:
    records = _discover(
        FakeEntryPoint(name="z.plugin"),
        FakeEntryPoint(name="a.plugin"),
        FakeEntryPoint(name="m.plugin"),
    )
    assert [record.entry_name for record in records] == ["a.plugin", "m.plugin", "z.plugin"]
