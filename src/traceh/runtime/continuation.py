"""Continuation policy kept outside the AgentLoop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from traceh.api.llm import ModelResponse
from traceh.runtime.repeated_denial import RepeatedDenialState
from traceh.runtime.response_completeness import ResponseCompleteness


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
        completeness: ResponseCompleteness,
        step_number: int,
        max_steps: int,
        verification: VerificationFeedback | None,
        verification_failures: int,
        max_verification_retries: int,
        repeated_denial: RepeatedDenialState | None = None,
    ) -> LoopDirective: ...


class DefaultContinuationRuntime:
    async def decide(
        self,
        *,
        response: ModelResponse,
        completeness: ResponseCompleteness,
        step_number: int,
        max_steps: int,
        verification: VerificationFeedback | None,
        verification_failures: int,
        max_verification_retries: int,
        repeated_denial: RepeatedDenialState | None = None,
    ) -> LoopDirective:
        # An unfinished response cannot be continued into a success and cannot
        # be retried into one either: the loop already refused to run its tool
        # calls, so there is no pending work left to carry forward. This is
        # checked first because it describes the response we actually got,
        # which is more informative than a step budget that also ran out.
        if not completeness.complete:
            return Finish(completeness.reason)
        if step_number >= max_steps:
            return Finish("max_steps_exceeded")
        if response.tool_calls:
            if repeated_denial is not None:
                if repeated_denial.count >= repeated_denial.policy.stop_after:
                    return Finish("stalled_repeated_denial")
                if repeated_denial.count == repeated_denial.policy.warn_after:
                    return Continue(
                        (
                            "The same tool calls and arguments have repeatedly received the same "
                            "denial after fresh policy checks. They did not execute. Do not repeat "
                            "unchanged calls. Choose another permitted action that addresses the "
                            "active request, or explain the actual blocker "
                            "without claiming success.",
                        )
                    )
            return Continue()
        if verification is not None and not verification.passed:
            if verification_failures <= max_verification_retries:
                return Continue(
                    (
                        "The external completion verifier failed. "
                        "Continue the task and address this "
                        f"evidence:\n{verification.summary}",
                    )
                )
            return Finish("verification_failed")
        return Finish("completed")
