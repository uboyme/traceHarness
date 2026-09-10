"""Evidence-driven completion verification."""

from __future__ import annotations

import shlex
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from traceh.api.json_types import JsonValue
from traceh.api.sandbox import SandboxCommandPort, SandboxOwner

_current_execution: ContextVar[SandboxCommandPort | None] = ContextVar(
    "traceh_verification_execution", default=None
)


@dataclass(frozen=True, slots=True)
class VerificationResult:
    passed: bool
    summary: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    sandbox_receipt: dict[str, JsonValue] | None = None


#: How much of each stream a summary carries. The summary is what the
#: continuation policy feeds back to the model, so it is bounded on purpose;
#: `VerificationResult.stdout`/`stderr` keep the full text either way.
SUMMARY_TAIL_CHARS = 4000


def _summary(headline: str, stdout: str, stderr: str) -> str:
    """Attach the same bounded output tails to every outcome.

    A timed-out verifier is exactly the case where the model needs to see what
    the command printed before it hung, so the timeout summary carries the same
    evidence, under the same bound, as a normal failure.
    """

    return (
        f"{headline}\n"
        f"stdout:\n{stdout[-SUMMARY_TAIL_CHARS:]}\n"
        f"stderr:\n{stderr[-SUMMARY_TAIL_CHARS:]}"
    )


class CompletionVerifier(Protocol):
    async def verify(self, workspace: Path) -> VerificationResult: ...


@dataclass(slots=True)
class CommandVerifier:
    command: str
    timeout_seconds: float = 60.0

    async def verify(self, workspace: Path) -> VerificationResult:
        argv = shlex.split(self.command)
        if not argv:
            return VerificationResult(False, "Verifier command is empty.")
        execution = _current_execution.get()
        if execution is None:
            return VerificationResult(
                False, "sandbox-not-configured: verification requires isolated execution"
            )
        result = await execution.run(tuple(argv), timeout_seconds=self.timeout_seconds)
        stdout = result.stdout.decode("utf-8", "replace")
        stderr = result.stderr.decode("utf-8", "replace")
        passed = result.status == "finished" and result.exit_code == 0
        return VerificationResult(
            passed,
            _summary(
                f"Verifier {result.status.replace('-', ' ')} with exit code {result.exit_code}.",
                stdout,
                stderr,
            ),
            result.exit_code,
            stdout,
            stderr,
            result.receipt,
        )


async def invoke_verifier(
    verifier: CompletionVerifier,
    workspace: Path,
    *,
    sandbox_service,
    sessions,
    session_id: str,
    turn_id: str,
    step_id: str,
    data_dir: Path,
) -> VerificationResult:
    """Bind a live execution capability around the existing trusted verifier API.

    ContextVar carries a capability, never facts or messages. Inherited task
    contexts retain only a port that is closed when this owner exits. This keeps
    plugin verifier callbacks unchanged without a second execution fallback.
    """
    if sandbox_service is None:
        return await verifier.verify(workspace)
    if sandbox_service.store is not sessions.store:
        raise ValueError("sandbox-verifier-store-owner-mismatch")
    owner = SandboxOwner("verification", step_id, session_id, turn_id, step_id)
    async with sandbox_service.scope(
        owner,
        stream_id=sessions.session_stream(session_id),
        workspace=workspace,
        data_dir=data_dir,
        publish_changes=False,
    ) as scope:
        token = _current_execution.set(scope)
        try:
            return await verifier.verify(workspace)
        finally:
            _current_execution.reset(token)
