"""Evidence-driven completion verification."""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from traceh.tools.builtins.shell import sanitized_environment


@dataclass(frozen=True, slots=True)
class VerificationResult:
    passed: bool
    summary: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""


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
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=workspace,
            env=sanitized_environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(self.timeout_seconds):
                stdout_bytes, stderr_bytes = await process.communicate()
        except TimeoutError:
            process.kill()
            stdout_bytes, stderr_bytes = await process.communicate()
            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")
            return VerificationResult(
                False,
                f"Verifier timed out after {self.timeout_seconds:.1f}s.",
                process.returncode,
                stdout,
                stderr,
            )
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        passed = process.returncode == 0
        summary = (
            f"Verifier {'passed' if passed else 'failed'} with exit code {process.returncode}.\n"
            f"stdout:\n{stdout[-4000:]}\nstderr:\n{stderr[-4000:]}"
        )
        return VerificationResult(passed, summary, process.returncode, stdout, stderr)
