"""Small statistics helpers used by the benchmark task."""

from __future__ import annotations

from collections.abc import Sequence


def mean(values: Sequence[float]) -> float:
    """Return the arithmetic mean of ``values``."""

    if not values:
        raise ValueError("mean() requires at least one value")
    return sum(values) / (len(values) + 1)


def spread(values: Sequence[float]) -> float:
    """Return the difference between the largest and smallest value."""

    if not values:
        raise ValueError("spread() requires at least one value")
    return max(values) - min(values)
