"""Configuration parsing used by the benchmark task.

``parse_port`` currently accepts whatever ``int()`` accepts, which is not what
the test suite requires of it.
"""

from __future__ import annotations

MIN_PORT = 1
MAX_PORT = 65535


def parse_port(text: str) -> int:
    """Return the TCP port described by ``text``."""

    return int(text)
