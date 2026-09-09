"""Errors shared by the CLI entry point and its subcommands."""

from __future__ import annotations


class CliConfigurationError(ValueError):
    """A usage or configuration problem that must be reported without a traceback."""


class SemanticSummaryConfigurationError(CliConfigurationError):
    """Fixed host-authored feedback, safe for both CLI and configuration forms."""

    def __init__(self):
        super().__init__("模型摘要需开启自动压缩、完整 Token 预算，并允许至少 2 步。")
