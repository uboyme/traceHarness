"""Structured plugin failures that never require exposing plugin exception text.

A plugin is third-party code. Its exception messages can quote configuration,
credentials it just tried, or arbitrary text with terminal control sequences.
The manager therefore never propagates a plugin's own exception text: it maps
each failure onto a fixed ``code`` plus a message this repository wrote, which
is what the CLI and ``doctor`` render.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PluginFailure:
    code: str
    stage: str
    message: str
    plugin_id: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "code": self.code,
            "stage": self.stage,
            "plugin_id": self.plugin_id,
            "message": self.message,
        }


class PluginError(RuntimeError):
    """Base error carrying deterministic, terminal-safe diagnostics."""

    def __init__(self, failures: tuple[PluginFailure, ...]) -> None:
        if not failures:
            raise ValueError("PluginError requires at least one failure")
        self.failures = failures
        first = failures[0]
        prefix = f"plugin {first.plugin_id}: " if first.plugin_id else ""
        super().__init__(f"{prefix}{first.message}")


class PluginDiscoveryError(PluginError):
    pass


class PluginValidationError(PluginError):
    pass


class PluginActivationError(PluginError):
    pass


class PluginDisposeError(PluginError):
    pass


__all__ = [
    "PluginActivationError",
    "PluginDiscoveryError",
    "PluginDisposeError",
    "PluginError",
    "PluginFailure",
    "PluginValidationError",
]
