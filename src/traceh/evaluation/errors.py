"""Stable, non-echoing failures for the ProductTask benchmark.

Every code is a fixed identifier.  None of them carries a requirement, a model's
prose, a repository path or an exception message: a benchmark failure is read by
a person and written into a report, so it must not become a channel for whatever
produced it.
"""

from __future__ import annotations


class EvaluationError(Exception):
    code = "benchmark-error"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class BenchmarkManifestError(EvaluationError, ValueError):
    """A manifest this build refuses to read as a benchmark definition."""

    def __init__(self, code: str, field: str) -> None:
        self.code = code
        self.field = field
        super().__init__(f"benchmark {field} is not usable")


class BenchmarkEvidenceError(EvaluationError):
    """A metric that could not be derived from the durable facts it names.

    Raised instead of substituting zero.  A benchmark whose numbers may be
    invented is worse than one that says it could not measure something.
    """

    def __init__(self, code: str, attempt_id: str | None = None) -> None:
        self.code = code
        self.attempt_id = attempt_id
        super().__init__("the durable benchmark evidence is missing or inconsistent")


class BenchmarkExecutionError(EvaluationError):
    """The harness itself could not carry one attempt through."""

    def __init__(self, code: str, attempt_id: str | None = None) -> None:
        self.code = code
        self.attempt_id = attempt_id
        super().__init__("the benchmark attempt could not be executed")


__all__ = [
    "BenchmarkEvidenceError",
    "BenchmarkExecutionError",
    "BenchmarkManifestError",
    "EvaluationError",
]
