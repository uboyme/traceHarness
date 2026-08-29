"""Explicit host policy for bounded retries of one frozen model request."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from math import isfinite, ldexp

from traceh.api.json_types import JsonValue
from traceh.llm.failures import ProviderFailure, ProviderFailureCategory

_RETRYABLE_CATEGORIES = frozenset(
    {
        ProviderFailureCategory.DNS,
        ProviderFailureCategory.TIMEOUT,
        ProviderFailureCategory.TLS_EOF,
        ProviderFailureCategory.DISCONNECTED,
        ProviderFailureCategory.RATE_LIMITED,
        ProviderFailureCategory.SERVER_TRANSIENT,
    }
)


@dataclass(frozen=True, slots=True)
class RetryDecision:
    delay_seconds: float


@dataclass(frozen=True, slots=True)
class ModelRetryPolicy:
    """All retry authority and bounds selected by the host.

    No field is inferred from exception text or supplied by a model.  A policy
    with ``max_attempts=1`` is the explicit no-retry policy.
    """

    max_attempts: int
    max_elapsed_seconds: float
    base_delay_seconds: float
    max_delay_seconds: float
    retry_after_cap_seconds: float
    jitter_ratio: float
    retryable_categories: frozenset[ProviderFailureCategory] = _RETRYABLE_CATEGORIES

    def __post_init__(self) -> None:
        if type(self.max_attempts) is not int or self.max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        for name in (
            "max_elapsed_seconds",
            "base_delay_seconds",
            "max_delay_seconds",
            "retry_after_cap_seconds",
            "jitter_ratio",
        ):
            value = getattr(self, name)
            if type(value) not in {int, float} or value < 0 or not isfinite(value):
                raise ValueError(f"{name} must be finite and non-negative")
        if self.jitter_ratio > 1:
            raise ValueError("jitter_ratio cannot exceed 1")
        if self.max_attempts > 1 and (
            self.max_elapsed_seconds <= 0 or self.max_delay_seconds <= 0
        ):
            raise ValueError("enabled retry requires positive elapsed and delay bounds")
        if not isinstance(self.retryable_categories, frozenset) or any(
            type(item) is not ProviderFailureCategory
            for item in self.retryable_categories
        ):
            raise TypeError("retryable_categories must contain ProviderFailureCategory values")
        if not self.retryable_categories <= _RETRYABLE_CATEGORIES:
            raise ValueError("retryable_categories cannot include permanent failures")

    def decide(
        self,
        failure: BaseException,
        *,
        completed_ordinal: int,
        elapsed_seconds: float,
        entropy: float,
    ) -> RetryDecision | None:
        """Return one bounded delay or refuse the next paid Attempt."""

        if (
            type(failure) is not ProviderFailure
            or failure.category not in self.retryable_categories
            or type(completed_ordinal) is not int
            or completed_ordinal < 1
            or completed_ordinal >= self.max_attempts
            or type(elapsed_seconds) not in {int, float}
            or not isfinite(elapsed_seconds)
            or elapsed_seconds < 0
            or elapsed_seconds >= self.max_elapsed_seconds
        ):
            return None
        if not 0 <= entropy <= 1:
            raise ValueError("retry entropy must be between zero and one")
        jitter_factor = 1 + self.jitter_ratio * ((2 * entropy) - 1)
        if self.base_delay_seconds == 0 or jitter_factor == 0:
            delay = 0.0
        else:
            try:
                delay = ldexp(
                    self.base_delay_seconds * jitter_factor,
                    completed_ordinal - 1,
                )
            except OverflowError:
                # The exact value is already above every finite host cap.  Do
                # not materialize an unbounded integer before applying it.
                delay = self.max_delay_seconds
            delay = min(delay, self.max_delay_seconds)
        if failure.retry_after_seconds is not None:
            delay = max(
                delay,
                min(failure.retry_after_seconds, self.retry_after_cap_seconds),
            )
        delay = min(delay, self.max_delay_seconds)
        if elapsed_seconds + delay > self.max_elapsed_seconds:
            return None
        return RetryDecision(delay_seconds=delay)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "max_attempts": self.max_attempts,
            "max_elapsed_seconds": float(self.max_elapsed_seconds),
            "base_delay_seconds": float(self.base_delay_seconds),
            "max_delay_seconds": float(self.max_delay_seconds),
            "retry_after_cap_seconds": float(self.retry_after_cap_seconds),
            "jitter_ratio": float(self.jitter_ratio),
            "retryable_categories": sorted(
                category.value for category in self.retryable_categories
            ),
        }


NO_MODEL_RETRY = ModelRetryPolicy(
    max_attempts=1,
    max_elapsed_seconds=0.0,
    base_delay_seconds=0.0,
    max_delay_seconds=0.0,
    retry_after_cap_seconds=0.0,
    jitter_ratio=0.0,
)


@dataclass(frozen=True, slots=True)
class RetryScheduler:
    """Injectable monotonic clock, sleeper and entropy source."""

    monotonic: Callable[[], float]
    sleep: Callable[[float], Awaitable[None]]
    entropy: Callable[[], float]

    @classmethod
    def real(cls) -> RetryScheduler:
        return cls(time.monotonic, asyncio.sleep, random.random)


__all__ = [
    "ModelRetryPolicy",
    "NO_MODEL_RETRY",
    "RetryDecision",
    "RetryScheduler",
]
