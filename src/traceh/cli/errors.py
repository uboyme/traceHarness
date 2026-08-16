"""Errors shared by the CLI entry point and its subcommands."""

from __future__ import annotations


class CliConfigurationError(ValueError):
    """A usage or configuration problem that must be reported without a traceback."""
