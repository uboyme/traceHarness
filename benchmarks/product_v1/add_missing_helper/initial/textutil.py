"""Text helpers used by the benchmark task.

``slugify`` is referenced by the test suite but has not been written yet.
"""

from __future__ import annotations


def normalize(text: str) -> str:
    """Collapse runs of whitespace and strip the ends."""

    return " ".join(text.split())
