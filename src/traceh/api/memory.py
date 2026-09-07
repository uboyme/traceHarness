"""Explicit host limits and read-only source seam for project Memory."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ProjectScopeLimits:
    max_catalog_events: int
    max_label_bytes: int

    def __post_init__(self):
        for value in (self.max_catalog_events, self.max_label_bytes):
            if type(value) is not int or value < 1:
                raise ValueError("project-limits-invalid")


@dataclass(frozen=True, slots=True)
class MemoryPolicy:
    max_body_bytes: int
    max_sources: int
    max_source_events: int
    max_source_bytes: int
    max_memory_events: int
    denied_patterns: tuple[str, ...]

    def __post_init__(self):
        for value in (
            self.max_body_bytes,
            self.max_sources,
            self.max_source_events,
            self.max_source_bytes,
            self.max_memory_events,
        ):
            if type(value) is not int or value < 1:
                raise ValueError("memory-policy-invalid")
        if type(self.denied_patterns) is not tuple or not self.denied_patterns:
            raise ValueError("memory-content-rules-required")
        for pattern in self.denied_patterns:
            if type(pattern) is not str or not pattern:
                raise ValueError("memory-content-rule-invalid")
            try:
                re.compile(pattern)
            except re.error:
                raise ValueError("memory-content-rule-invalid") from None


class ProjectSourceResolver(Protocol):
    async def project_fingerprint(self, source_id: str, workspace: Path | None) -> str:
        """Prove current host mapping, and optional workspace membership, before returning.

        Cancellation must converge all owned I/O. A path or caller-supplied fingerprint
        alone is not membership evidence. None asks only for the registered source.
        """
        ...


@dataclass(frozen=True, slots=True)
class ProjectMemoryConfig:
    project_limits: ProjectScopeLimits
    memory_policy: MemoryPolicy
    source_resolver: ProjectSourceResolver

    def __post_init__(self):
        if (
            type(self.project_limits) is not ProjectScopeLimits
            or type(self.memory_policy) is not MemoryPolicy
        ):
            raise ValueError("project-memory-config-invalid")
        if not callable(getattr(self.source_resolver, "project_fingerprint", None)):
            raise ValueError("project-source-resolver-required")


__all__ = ["MemoryPolicy", "ProjectMemoryConfig", "ProjectScopeLimits", "ProjectSourceResolver"]
