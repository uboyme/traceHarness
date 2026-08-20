"""Host-owned installed-metadata check used inside an L2 validation venv."""

from __future__ import annotations

import argparse
from importlib import metadata

from packaging.utils import canonicalize_name
from packaging.version import Version

from traceh.plugins import PluginDiscovery


def check_installed_contract(
    *,
    distribution: str,
    version: str,
    plugin_id: str,
    entry_value: str,
) -> bool:
    """Check one installed Wheel without importing its plugin module."""

    try:
        installed = metadata.distribution(distribution)
        installed_name = str(installed.metadata.get("Name") or "")
        installed_version = str(installed.version)
    except Exception:
        return False
    if canonicalize_name(installed_name) != canonicalize_name(distribution):
        return False
    try:
        if Version(installed_version) != Version(version):
            return False
    except Exception:
        return False

    records = [
        record
        for record in PluginDiscovery().discover()
        if record.entry_name == plugin_id
        and canonicalize_name(record.distribution_name or "")
        == canonicalize_name(distribution)
    ]
    return (
        len(records) == 1
        and not records[0].issues
        and records[0].entry_value == entry_value
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--distribution", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--plugin-id", required=True)
    parser.add_argument("--entry-value", required=True)
    args = parser.parse_args(argv)
    return 0 if check_installed_contract(
        distribution=args.distribution,
        version=args.version,
        plugin_id=args.plugin_id,
        entry_value=args.entry_value,
    ) else 1


if __name__ == "__main__":  # pragma: no cover - exercised in a child interpreter
    raise SystemExit(main())
