"""Metadata-only discovery for the ``traceh.plugins`` entry-point group.

Discovery reads installed distribution metadata and never imports a plugin
module. That separation is the point: ``traceh plugins list`` and
``traceh plugins inspect`` can report what is installed on a machine without
executing third-party code, so listing plugins is not itself a code-execution
step. Importing happens only in :mod:`traceh.plugins.manager`, and only for
plugins the operator explicitly enabled.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from importlib import metadata
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version

from traceh.plugins.selection import is_plugin_id
from traceh.version import DISTRIBUTION_NAME, __version__

ENTRY_POINT_GROUP = "traceh.plugins"
TRACEH_DISTRIBUTION = DISTRIBUTION_NAME


@dataclass(frozen=True, slots=True)
class DiscoveryIssue:
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class DiscoveredPlugin:
    entry_name: str
    entry_value: str
    group: str
    distribution_name: str | None
    distribution_version: str | None
    traceh_requirement: str | None
    issues: tuple[DiscoveryIssue, ...] = ()
    entry_point: Any = field(default=None, repr=False, compare=False)

    @property
    def state(self) -> str:
        return "failed" if self.issues else "discovered"

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "entry_point": {
                "group": self.group,
                "name": self.entry_name,
                "value": self.entry_value,
            },
            "distribution": {
                "name": self.distribution_name,
                "version": self.distribution_version,
            },
            "traceh_requirement": self.traceh_requirement,
            "manifest": {
                # Stated explicitly so a reader of `plugins list` output is never
                # misled into thinking the manifest was validated here. It cannot
                # be: reading it requires importing the plugin.
                "available": False,
                "requires_import": True,
            },
            "issues": [issue.to_dict() for issue in self.issues],
        }


def _select_entry_points(provider: Callable[..., object]) -> tuple[Any, ...]:
    """Support both the modern and the legacy importlib.metadata return shapes."""

    try:
        selected = provider(group=ENTRY_POINT_GROUP)
    except TypeError:
        all_points = provider()
        if hasattr(all_points, "select"):
            selected = all_points.select(group=ENTRY_POINT_GROUP)
        elif isinstance(all_points, dict):
            selected = all_points.get(ENTRY_POINT_GROUP, ())
        else:
            selected = (
                point
                for point in all_points
                if getattr(point, "group", None) == ENTRY_POINT_GROUP
            )
    return tuple(selected)  # type: ignore[arg-type]


def _matching_distribution(point: Any) -> Any | None:
    direct = getattr(point, "dist", None)
    if direct is not None:
        return direct
    try:
        distributions: Iterable[Any] = metadata.distributions()
    except Exception:
        return None
    for distribution in distributions:
        try:
            points = distribution.entry_points
        except Exception:
            continue
        for candidate in points:
            if (
                getattr(candidate, "group", None) == getattr(point, "group", None)
                and getattr(candidate, "name", None) == getattr(point, "name", None)
                and getattr(candidate, "value", None) == getattr(point, "value", None)
            ):
                return distribution
    return None


def _distribution_name(distribution: Any) -> str | None:
    if distribution is None:
        return None
    try:
        name = distribution.metadata.get("Name")
    except Exception:
        return None
    return str(name) if name else None


def _distribution_version(distribution: Any) -> str | None:
    if distribution is None:
        return None
    try:
        value = distribution.version
    except Exception:
        return None
    return str(value) if value else None


def _requirements(distribution: Any) -> tuple[str, ...] | None:
    if distribution is None:
        return None
    try:
        raw = distribution.requires
    except Exception:
        return None
    return tuple(raw or ())


def installed_traceh_version() -> Version:
    """The running harness version a plugin's ``traceharness-py`` range is checked against.

    This is :data:`traceh.version.__version__`, not ``importlib.metadata``. The
    code that will actually host the plugin is the code that is imported, and
    ``pyproject.toml`` derives the distribution version from the same attribute,
    so the two agree by construction rather than by coincidence.
    """

    return Version(__version__)


class PluginDiscovery:
    """Find plugin distributions without importing them or running setup."""

    def __init__(
        self,
        *,
        entry_points_provider: Callable[..., object] | None = None,
    ) -> None:
        self._provider = entry_points_provider or metadata.entry_points

    def discover(self) -> tuple[DiscoveredPlugin, ...]:
        try:
            points = _select_entry_points(self._provider)
        except Exception:
            # A broken global metadata provider becomes one synthetic record
            # rather than leaking its exception text or traceback to the CLI.
            return (
                DiscoveredPlugin(
                    entry_name="<metadata>",
                    entry_value="<unavailable>",
                    group=ENTRY_POINT_GROUP,
                    distribution_name=None,
                    distribution_version=None,
                    traceh_requirement=None,
                    issues=(
                        DiscoveryIssue(
                            "entry-point-metadata-error",
                            "Python entry-point metadata could not be read",
                        ),
                    ),
                ),
            )

        records: list[DiscoveredPlugin] = []
        installed = installed_traceh_version()
        for point in points:
            issues: list[DiscoveryIssue] = []
            try:
                name = str(point.name)
                value = str(point.value)
                group = str(point.group)
            except Exception:
                records.append(
                    DiscoveredPlugin(
                        entry_name="<invalid>",
                        entry_value="<invalid>",
                        group=ENTRY_POINT_GROUP,
                        distribution_name=None,
                        distribution_version=None,
                        traceh_requirement=None,
                        issues=(
                            DiscoveryIssue(
                                "entry-point-metadata-error",
                                "An entry point has unreadable metadata",
                            ),
                        ),
                        entry_point=point,
                    )
                )
                continue
            if group != ENTRY_POINT_GROUP:
                continue
            if not is_plugin_id(name):
                issues.append(
                    DiscoveryIssue(
                        "invalid-entry-point-name",
                        "Entry-point name is not a valid plugin id",
                    )
                )
            distribution = _matching_distribution(point)
            dist_name = _distribution_name(distribution)
            dist_version = _distribution_version(distribution)
            if dist_name is None or dist_version is None:
                issues.append(
                    DiscoveryIssue(
                        "distribution-metadata-missing",
                        "Distribution name or version metadata is missing",
                    )
                )
            else:
                try:
                    Version(dist_version)
                except InvalidVersion:
                    issues.append(
                        DiscoveryIssue(
                            "distribution-version-invalid",
                            "Distribution version is not valid PEP 440",
                        )
                    )

            requirement_text: str | None = None
            raw_requirements = _requirements(distribution)
            if raw_requirements is None:
                issues.append(
                    DiscoveryIssue(
                        "distribution-requirements-missing",
                        "Distribution dependency metadata could not be read",
                    )
                )
            else:
                parsed_requirements: list[Requirement] = []
                for raw in raw_requirements:
                    try:
                        parsed_requirements.append(Requirement(raw))
                    except InvalidRequirement:
                        issues.append(
                            DiscoveryIssue(
                                "distribution-requirement-invalid",
                                "Distribution contains an invalid dependency requirement",
                            )
                        )
                traceh_requirements = [
                    requirement
                    for requirement in parsed_requirements
                    if requirement.name.lower().replace("_", "-") == TRACEH_DISTRIBUTION
                    and (requirement.marker is None or requirement.marker.evaluate())
                ]
                if not traceh_requirements:
                    issues.append(
                        DiscoveryIssue(
                            "traceh-dependency-missing",
                            "Plugin distribution does not declare a traceharness-py dependency",
                        )
                    )
                elif len(traceh_requirements) > 1:
                    issues.append(
                        DiscoveryIssue(
                            "traceh-dependency-duplicate",
                            "Plugin distribution declares traceharness-py more than once",
                        )
                    )
                else:
                    requirement = traceh_requirements[0]
                    requirement_text = str(requirement.specifier)
                    if requirement.specifier and installed not in requirement.specifier:
                        issues.append(
                            DiscoveryIssue(
                                "traceh-distribution-incompatible",
                                "Installed TraceHarness distribution does not satisfy "
                                "the plugin dependency",
                            )
                        )

            records.append(
                DiscoveredPlugin(
                    entry_name=name,
                    entry_value=value,
                    group=group,
                    distribution_name=dist_name,
                    distribution_version=dist_version,
                    traceh_requirement=requirement_text,
                    issues=tuple(issues),
                    entry_point=point,
                )
            )

        # Two distributions claiming one entry-point name is ambiguous, not a
        # race to be resolved silently: every claimant is marked failed so an
        # enabled plugin can never resolve to "whichever was installed last".
        by_name: dict[str, list[int]] = defaultdict(list)
        for index, record in enumerate(records):
            by_name[record.entry_name].append(index)
        for indices in by_name.values():
            if len(indices) < 2:
                continue
            issue = DiscoveryIssue(
                "duplicate-entry-point",
                "More than one distribution declares this plugin entry-point name",
            )
            for index in indices:
                records[index] = replace(records[index], issues=(*records[index].issues, issue))

        return tuple(
            sorted(
                records,
                key=lambda item: (
                    item.entry_name,
                    item.distribution_name or "",
                    item.entry_value,
                ),
            )
        )


__all__ = [
    "ENTRY_POINT_GROUP",
    "TRACEH_DISTRIBUTION",
    "DiscoveredPlugin",
    "DiscoveryIssue",
    "PluginDiscovery",
    "installed_traceh_version",
]
