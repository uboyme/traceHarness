"""Continuation policy kept outside the AgentLoop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from traceh.api.llm import ModelResponse


@dataclass(frozen=True, slots=True)
class VerificationFeedback:
    passed: bool
    summary: str


@dataclass(frozen=True, slots=True)
class Continue:
    messages: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Finish:
    reason: str


LoopDirective = Continue | Finish


class ContinuationRuntime(Protocol):
    async def decide(
        self,
        *,
        response: ModelResponse,
        step_number: int,
        max_steps: int,
        verification: VerificationFeedback | None,
        verification_failures: int,
        max_verification_retries: int,
    ) -> LoopDirective:
        ...


class DefaultContinuationRuntime:
    async def decide(
        self,
        *,
        response: ModelResponse,
        step_number: int,
        max_steps: int,
        verification: VerificationFeedback | None,
        verification_failures: int,
        max_verification_retries: int,
    ) -> LoopDirective:
        if step_number >= max_steps:
            return Finish("max_steps_exceeded")
        if response.tool_calls:
            return Continue()
        if verification is not None and not verification.passed:
            if verification_failures <= max_verification_retries:
                return Continue(
                    (
                        "The external completion verifier failed. Continue the task and address this "
                        f"evidence:\n{verification.summary}",
                    )
                )
            return Finish("verification_failed")
        return Finish("completed")
