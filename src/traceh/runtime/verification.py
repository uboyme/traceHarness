"""Evidence-driven completion verification."""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from traceh.process_control import converge_process
from traceh.tools.builtins.shell import sanitized_environment
from traceh.tools.process_control import CapturedOutput, capture_output


@dataclass(frozen=True, slots=True)
class VerificationResult:
    passed: bool
    summary: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""


#: How much of each stream a summary carries. The summary is what the
#: continuation policy feeds back to the model, so it is bounded on purpose;
#: `VerificationResult.stdout`/`stderr` keep the full text either way.
SUMMARY_TAIL_CHARS = 4000


def _decode(capture: CapturedOutput) -> tuple[str, str]:
    stdout_bytes, stderr_bytes = capture.read()
    return (
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
    )


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
    async def verify(self, workspace: Path) -> VerificationResult:
        ...


@dataclass(slots=True)
class CommandVerifier:
    command: str
    timeout_seconds: float = 60.0

    async def verify(self, workspace: Path) -> VerificationResult:
        argv = shlex.split(self.command)
        if not argv:
            return VerificationResult(False, "Verifier command is empty.")
        # The captured files are the one owner of this command's output, so
        # whatever it flushed survives a timeout or a cancellation.
        with capture_output() as capture:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=workspace,
                env=sanitized_environment(),
                stdout=capture.stdout,
                stderr=capture.stderr,
            )
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    await process.wait()
            except asyncio.CancelledError:
                # The verifier command runs real tests in the workspace. Letting
                # it survive cancellation would keep changing the workspace
                # after the caller was told the turn was over.
                await converge_process(process)
                raise
            except TimeoutError:
                # Converge first, then read: a cancellation arriving during the
                # shutdown is absorbed and only re-raised once the child is gone.
                interrupted = await converge_process(process)
                stdout, stderr = _decode(capture)
                if interrupted:
                    raise asyncio.CancelledError from None
                return VerificationResult(
                    False,
                    _summary(
                        f"Verifier timed out after {self.timeout_seconds:.1f}s.",
                        stdout,
                        stderr,
                    ),
                    process.returncode,
                    stdout,
                    stderr,
                )
            stdout, stderr = _decode(capture)

        passed = process.returncode == 0
        summary = _summary(
            f"Verifier {'passed' if passed else 'failed'} with exit code {process.returncode}.",
            stdout,
            stderr,
        )
        return VerificationResult(passed, summary, process.returncode, stdout, stderr)
