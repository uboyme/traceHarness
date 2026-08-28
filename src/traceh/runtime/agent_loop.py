"""The intentionally thin Session/Turn/Step control loop."""

from __future__ import annotations

import asyncio
import traceback
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from traceh.api.llm import Usage
from traceh.api.tools import ToolExecutionContext
from traceh.api.turns import TurnInput
from traceh.concurrency import await_worker_convergence, combine_failures
from traceh.kernel.hooks import STEP_FINISHED, TURN_FINISHED, TURN_STARTED, HookDispatcher
from traceh.llm.runtime import LlmRuntime
from traceh.runtime.composition_runtime import CompositionRuntime
from traceh.runtime.continuation import (
    ContinuationRuntime,
    Continue,
    DefaultContinuationRuntime,
    VerificationFeedback,
)
from traceh.runtime.request_builder import RequestBuilder
from traceh.runtime.verification import CompletionVerifier
from traceh.session.event_store import Durability
from traceh.session.service import SessionService


@dataclass(frozen=True, slots=True)
class TurnResult:
    session_id: str
    turn_id: str
    reason: str
    final_text: str
    steps: int
    usage: Usage
    verification_passed: bool | None = None


class AgentLoop:
    def __init__(
        self,
        *,
        sessions: SessionService,
        compositions: CompositionRuntime,
        request_builder: RequestBuilder,
        llm_runtime: LlmRuntime,
        data_dir: Path,
        max_steps: int = 20,
        continuation: ContinuationRuntime | None = None,
        verifier: CompletionVerifier | None = None,
        max_verification_retries: int = 1,
        hooks: HookDispatcher | None = None,
    ) -> None:
        self.sessions = sessions
        self.compositions = compositions
        self.request_builder = request_builder
        self.llm_runtime = llm_runtime
        self.data_dir = data_dir
        self.max_steps = max_steps
        self.continuation = continuation or DefaultContinuationRuntime()
        self.verifier = verifier
        self.max_verification_retries = max_verification_retries
        self.hooks = hooks or HookDispatcher()

    async def run_turn(self, session_id: str, task: str | TurnInput) -> TurnResult:
        # A plain ``str`` keeps the historical behaviour exactly - a fresh id
        # and ``source="user"``. A `TurnInput` lets the caller keep an identity
        # it already recorded elsewhere, so the Session Turn is addressable
        # afterwards. The loop knows nothing about who that caller is.
        turn_input = TurnInput.from_task(task)
        await self.sessions.ensure_session(session_id)
        workspace = await self.sessions.workspace_for(session_id)
        turn_id = str(uuid4())
        message_id = turn_input.message_id
        correlation_id = uuid4()
        current_step_id: str | None = None
        current_attempt_id: str | None = None
        current_attempt_revision: str | None = None
        steps = 0
        total_input_tokens = 0
        total_output_tokens = 0
        final_text = ""
        pending_messages = [turn_input.content]
        verification_failures = 0
        verification_passed: bool | None = None
        turn_open = False
        step_open = False

        await self.sessions.append_session(
            session_id,
            "inbox/accepted",
            {
                "message_id": message_id,
                "source": turn_input.source,
                "content": turn_input.content,
                "target": "new_turn",
            },
            correlation_id=correlation_id,
        )
        await self.sessions.append_session(
            session_id,
            "inbox/claimed",
            {"message_id": message_id, "turn_id": turn_id},
            correlation_id=correlation_id,
        )
        await self.sessions.append_session(
            session_id,
            "turn/start",
            {"turn_id": turn_id, "message_id": message_id},
            correlation_id=correlation_id,
        )
        turn_open = True
        await self.hooks.notify(
            TURN_STARTED,
            {"session_id": session_id, "turn_id": turn_id, "message_id": message_id},
        )

        try:
            while True:
                steps += 1
                current_step_id = str(uuid4())
                await self.sessions.append_session(
                    session_id,
                    "step/start",
                    {"turn_id": turn_id, "step_id": current_step_id, "number": steps},
                    correlation_id=correlation_id,
                )
                step_open = True

                for content in pending_messages:
                    await self.sessions.append_session(
                        session_id,
                        "user/message",
                        {
                            "turn_id": turn_id,
                            "step_id": current_step_id,
                            "content": content,
                        },
                        correlation_id=correlation_id,
                    )
                pending_messages = []

                async with self.compositions.lease(
                    workspace=workspace,
                    session_id=session_id,
                    turn_id=turn_id,
                    step_id=current_step_id,
                ) as active_composition:
                    composition = active_composition.snapshot
                    composition_event = await self.sessions.append_session(
                        session_id,
                        "composition/snapshot",
                        composition.to_dict(),
                        correlation_id=correlation_id,
                        composition_revision=composition.revision,
                    )
                    built = await self.request_builder.build(
                        session_id=session_id,
                        turn_id=turn_id,
                        step_id=current_step_id,
                        composition=composition,
                        through_seq=composition_event.seq,
                    )
                    await self.sessions.append_session(
                        session_id,
                        "request/snapshot",
                        {
                            "turn_id": turn_id,
                            "step_id": current_step_id,
                            "source_seq": built.source_seq,
                            "composition_revision": composition.revision,
                            "fingerprint": built.fingerprint,
                            "provider": built.request.provider,
                            "model": built.request.model,
                            "temperature": built.request.temperature,
                            "max_output_tokens": built.request.max_output_tokens,
                            "metadata": dict(built.request.metadata),
                            "request": built.request.to_dict(),
                        },
                        correlation_id=correlation_id,
                        composition_revision=composition.revision,
                    )
                    attempt_id = str(uuid4())
                    current_attempt_id = attempt_id
                    current_attempt_revision = composition.revision
                    await self.sessions.append_session(
                        session_id,
                        "model/attempt-start",
                        {
                            "turn_id": turn_id,
                            "step_id": current_step_id,
                            "attempt_id": attempt_id,
                            "provider": composition.provider,
                            "model": composition.model,
                        },
                        correlation_id=correlation_id,
                        composition_revision=composition.revision,
                    )
                    async def record_text_delta(
                        delta: str,
                        *,
                        step_id: str = current_step_id,
                        model_attempt_id: str = attempt_id,
                        revision: str = composition.revision,
                    ) -> None:
                        await self.sessions.append_session(
                            session_id,
                            "assistant/chunk",
                            {
                                "turn_id": turn_id,
                                "step_id": step_id,
                                "attempt_id": model_attempt_id,
                                "content": delta,
                            },
                            durability=Durability.BATCHED,
                            correlation_id=correlation_id,
                            composition_revision=revision,
                        )

                    try:
                        response = await self.llm_runtime.invoke(
                            active_composition.provider,
                            built.request,
                            on_text_delta=record_text_delta,
                        )
                    except asyncio.CancelledError:
                        # One outer owner closes Attempt -> Step -> Turn in
                        # order. Closing the Attempt in a separate shield here
                        # let repeated cancellation detach that append from the
                        # Step/Turn finalizer.
                        raise
                    except Exception as error:
                        await self.sessions.append_session(
                            session_id,
                            "model/attempt-end",
                            {
                                "turn_id": turn_id,
                                "step_id": current_step_id,
                                "attempt_id": attempt_id,
                                "status": "failed",
                                "error_type": type(error).__name__,
                                "message": str(error),
                            },
                            correlation_id=correlation_id,
                            composition_revision=composition.revision,
                        )
                        raise

                    total_input_tokens += response.usage.input_tokens
                    total_output_tokens += response.usage.output_tokens
                    await self.sessions.append_session(
                        session_id,
                        "assistant/message",
                        {
                            "turn_id": turn_id,
                            "step_id": current_step_id,
                            "attempt_id": attempt_id,
                            "content": response.content,
                            "tool_calls": [call.to_dict() for call in response.tool_calls],
                        },
                        correlation_id=correlation_id,
                        composition_revision=composition.revision,
                    )
                    await self.sessions.append_session(
                        session_id,
                        "model/attempt-end",
                        {
                            "turn_id": turn_id,
                            "step_id": current_step_id,
                            "attempt_id": attempt_id,
                            "status": "succeeded",
                            "finish_reason": response.finish_reason,
                            "usage": response.usage.to_dict(),
                        },
                        correlation_id=correlation_id,
                        composition_revision=composition.revision,
                    )

                    if response.tool_calls:
                        tool_context = ToolExecutionContext(
                            session_id=session_id,
                            turn_id=turn_id,
                            step_id=current_step_id,
                            tool_call_id="<batch>",
                            workspace=workspace,
                            data_dir=self.data_dir,
                        )
                        await active_composition.tools.execute_batch(
                            response.tool_calls,
                            context=tool_context,
                            composition_revision=composition.revision,
                        )

                    verification_feedback: VerificationFeedback | None = None
                    effective_verifier = (
                        active_composition.verifier
                        if active_composition.verifier is not None
                        else self.verifier
                    )
                    if not response.tool_calls and effective_verifier is not None:
                        verification = await effective_verifier.verify(workspace)
                        verification_passed = verification.passed
                        await self.sessions.append_session(
                            session_id,
                            "verification/result",
                            {
                                "turn_id": turn_id,
                                "step_id": current_step_id,
                                "passed": verification.passed,
                                "summary": verification.summary,
                                "exit_code": verification.exit_code,
                                "stdout": verification.stdout[-8000:],
                                "stderr": verification.stderr[-8000:],
                            },
                            correlation_id=correlation_id,
                            composition_revision=composition.revision,
                        )
                        verification_feedback = VerificationFeedback(
                            verification.passed,
                            verification.summary,
                        )
                        if not verification.passed:
                            verification_failures += 1

                await self.sessions.append_session(
                    session_id,
                    "step/end",
                    {
                        "turn_id": turn_id,
                        "step_id": current_step_id,
                        "reason": "model_response",
                    },
                    correlation_id=correlation_id,
                    composition_revision=composition.revision,
                )
                step_open = False
                await self.hooks.notify(
                    STEP_FINISHED,
                    {
                        "session_id": session_id,
                        "turn_id": turn_id,
                        "step_id": current_step_id,
                        "step_number": steps,
                    },
                )
                final_text = response.content

                directive = await self.continuation.decide(
                    response=response,
                    step_number=steps,
                    max_steps=self.max_steps,
                    verification=verification_feedback,
                    verification_failures=verification_failures,
                    max_verification_retries=self.max_verification_retries,
                )
                if isinstance(directive, Continue):
                    pending_messages.extend(directive.messages)
                    continue

                await self.sessions.append_session(
                    session_id,
                    "turn/end",
                    {
                        "turn_id": turn_id,
                        "reason": directive.reason,
                        "steps": steps,
                        "usage": {
                            "input_tokens": total_input_tokens,
                            "output_tokens": total_output_tokens,
                        },
                    },
                    correlation_id=correlation_id,
                )
                turn_open = False
                await self.hooks.notify(
                    TURN_FINISHED,
                    {
                        "session_id": session_id,
                        "turn_id": turn_id,
                        "reason": directive.reason,
                        "steps": steps,
                    },
                )
                return TurnResult(
                    session_id=session_id,
                    turn_id=turn_id,
                    reason=directive.reason,
                    final_text=final_text,
                    steps=steps,
                    usage=Usage(total_input_tokens, total_output_tokens),
                    verification_passed=verification_passed,
                )
        except asyncio.CancelledError as cancellation:
            finalizer = asyncio.create_task(
                self._close_cancelled(
                    session_id,
                    turn_id,
                    current_step_id,
                    current_attempt_id=current_attempt_id,
                    current_attempt_revision=current_attempt_revision,
                    correlation_id=correlation_id,
                ),
                name=f"traceh-turn-cancel-finalize-{turn_id}",
            )
            await await_worker_convergence(finalizer)
            if finalizer.cancelled():
                raise cancellation
            failure = finalizer.exception()
            if failure is not None:
                combined = combine_failures(
                    cancellation,
                    failure,
                    "Turn cancellation and durable finalization both failed",
                )
                assert combined is not None
                raise combined from None
            raise cancellation
        except Exception as error:
            await self.sessions.append_session(
                session_id,
                "runtime/error",
                {
                    "turn_id": turn_id,
                    "step_id": current_step_id,
                    "error_type": type(error).__name__,
                    "message": str(error),
                    "traceback": "".join(traceback.format_exception(error))[-12_000:],
                },
                correlation_id=correlation_id,
            )
            await self._close_interrupted(
                session_id,
                turn_id,
                current_step_id,
                step_open=step_open,
                turn_open=turn_open,
                reason="failed",
            )
            raise

    async def _close_interrupted(
        self,
        session_id: str,
        turn_id: str,
        step_id: str | None,
        *,
        step_open: bool,
        turn_open: bool,
        reason: str,
    ) -> None:
        if step_open and step_id is not None:
            await self.sessions.append_session(
                session_id,
                "step/end",
                {"turn_id": turn_id, "step_id": step_id, "reason": reason},
            )
        if turn_open:
            await self.sessions.append_session(
                session_id,
                "turn/end",
                {"turn_id": turn_id, "reason": reason},
            )

    async def _close_cancelled(
        self,
        session_id: str,
        turn_id: str,
        step_id: str | None,
        *,
        current_attempt_id: str | None,
        current_attempt_revision: str | None,
        correlation_id: UUID,
    ) -> None:
        """Durably close one cancelled Turn from a single owned Task.

        The fresh read resolves cancellation/commit ambiguity at the Attempt
        append boundary: if attempt-start committed but its caller was
        cancelled before observing the return, exactly one matching end is
        still appended.  All three terminals are then written sequentially, so
        no later close fact can outlive the public ``run_turn()`` call.
        """

        events = await self.sessions.read_session(session_id)
        if current_attempt_id is not None and step_id is not None:
            started = any(
                event.type == "model/attempt-start"
                and event.data.get("attempt_id") == current_attempt_id
                for event in events
            )
            ended = any(
                event.type == "model/attempt-end"
                and event.data.get("attempt_id") == current_attempt_id
                for event in events
            )
            if started and not ended:
                await self.sessions.append_session(
                    session_id,
                    "model/attempt-end",
                    {
                        "turn_id": turn_id,
                        "step_id": step_id,
                        "attempt_id": current_attempt_id,
                        "status": "cancelled",
                        "error_type": "CancelledError",
                        "message": "Model attempt was cancelled",
                    },
                    correlation_id=correlation_id,
                    composition_revision=current_attempt_revision,
                )
        step_open = step_id is not None and any(
            event.type == "step/start"
            and event.data.get("turn_id") == turn_id
            and event.data.get("step_id") == step_id
            for event in events
        ) and not any(
            event.type == "step/end"
            and event.data.get("turn_id") == turn_id
            and event.data.get("step_id") == step_id
            for event in events
        )
        turn_open = any(
            event.type == "turn/start" and event.data.get("turn_id") == turn_id
            for event in events
        ) and not any(
            event.type == "turn/end" and event.data.get("turn_id") == turn_id
            for event in events
        )
        await self._close_interrupted(
            session_id,
            turn_id,
            step_id,
            step_open=step_open,
            turn_open=turn_open,
            reason="cancelled",
        )
