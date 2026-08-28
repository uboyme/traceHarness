"""The single source of truth for the TraceHarness version and core identity.

Every version-bearing surface derives from this module: the distribution
metadata in ``pyproject.toml`` (via ``[tool.setuptools.dynamic]``), the public
``traceh.__version__``, the ``traceh.core`` plugin identity recorded in every
Composition Snapshot, the plugin API version a manifest is checked against, and
the CLI banner.

The concentration is not tidiness. A previous candidate carried the version in
four independent places, and the two that mattered disagreed: a runtime built
without plugins stamped ``traceh.core`` with a literal, while a runtime built
through ``PluginManager`` stamped it with whatever ``importlib.metadata``
reported. Two runtimes of the same build then produced different Composition
Snapshots for the same step, which is exactly the kind of divergence the
snapshot exists to rule out.

This module imports nothing from the rest of the package so that every layer,
including ``traceh.api``, can depend on it without a cycle.
"""

from __future__ import annotations

__version__ = "0.7.0"
"""Version of both the ``traceharness-py`` distribution and the ``traceh`` package.

``pyproject.toml`` reads this attribute, so the installed wheel's metadata and
the imported module can never drift apart. ``tests/test_version_contract.py``
asserts that equality against the real installed distribution.
"""

DISTRIBUTION_NAME = "traceharness-py"
"""PyPI distribution name a plugin must depend on to be considered installable."""

CORE_PLUGIN_ID = "traceh.core"
"""Reserved plugin id for the harness itself; no external plugin may claim it."""

DEFAULT_REQUIRES_TRACEH = ">=0.4,<1.0"
"""Default ``PluginManifest.requires_traceh`` range.

It is the compatibility window a plugin gets when it does not state one, and it
must contain :data:`__version__`; ``tests/test_version_contract.py`` proves it
does, so bumping the version can never silently orphan every default manifest.
"""

__all__ = [
    "CORE_PLUGIN_ID",
    "DEFAULT_REQUIRES_TRACEH",
    "DISTRIBUTION_NAME",
    "__version__",
]
