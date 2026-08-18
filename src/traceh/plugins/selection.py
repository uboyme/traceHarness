"""Explicit plugin enablement parsing shared by run, chat and resume.

Installation never enables a plugin. Enabling is always an explicit act by the
operator, either ``--plugin`` on the command line or ``TRACEH_PLUGINS`` in the
environment, and every entry point in the ``traceh.plugins`` group that was not
named stays unimported.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from traceh.plugins.errors import PluginFailure, PluginValidationError

PLUGIN_ID_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")


def is_plugin_id(value: str) -> bool:
    """Whether ``value`` is a syntactically valid plugin id.

    The pattern is deliberately narrow - lowercase, no whitespace, no control
    characters, bounded length - because a plugin id is echoed by the CLI, used
    as a task name and written into session metadata.
    """

    return bool(PLUGIN_ID_PATTERN.fullmatch(value))


def _validate(values: Sequence[str], *, source: str) -> tuple[str, ...]:
    failures: list[PluginFailure] = []
    seen: set[str] = set()
    normalized: list[str] = []
    for index, raw in enumerate(values):
        value = raw.strip()
        if not value:
            failures.append(
                PluginFailure(
                    "empty-plugin-id",
                    "selection",
                    f"{source} contains an empty plugin id at position {index + 1}",
                )
            )
            continue
        if not is_plugin_id(value):
            # The rejected value is never echoed: an operator who mis-pastes a
            # token into this setting must not see it reprinted in the error.
            failures.append(
                PluginFailure(
                    "invalid-plugin-id",
                    "selection",
                    f"{source} contains an invalid plugin id at position {index + 1}",
                )
            )
            continue
        if value in seen:
            failures.append(
                PluginFailure(
                    "duplicate-plugin-id",
                    "selection",
                    f"{source} enables the same plugin more than once",
                    value,
                )
            )
            continue
        seen.add(value)
        normalized.append(value)
    if failures:
        raise PluginValidationError(tuple(failures))
    return tuple(normalized)


def resolve_enabled_plugins(
    cli_values: Sequence[str] | None,
    environment_value: str | None,
) -> tuple[str, ...]:
    """Resolve explicit enablement for one command invocation.

    A non-empty list of ``--plugin`` occurrences *replaces* ``TRACEH_PLUGINS``
    rather than adding to it, so a command line always fully determines what runs.
    Otherwise the environment value is parsed as a comma-separated list.

    Validation happens here, before discovery and before any plugin module is
    imported, so a malformed selection can never reach third-party code.
    """

    if cli_values:
        return _validate(tuple(cli_values), source="--plugin")
    if environment_value is None or environment_value == "":
        return ()
    return _validate(tuple(environment_value.split(",")), source="TRACEH_PLUGINS")


__all__ = ["PLUGIN_ID_PATTERN", "is_plugin_id", "resolve_enabled_plugins"]
