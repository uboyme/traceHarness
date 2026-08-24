"""Fixed host verification of one integration environment.

The verifier definition is frozen by the host before a Patch exists. Nothing in
the candidate Patch, the Workspace or the model can add a command, change an
argument, widen the environment or extend a timeout. Commands run through
``exec`` with an explicit argv - never a shell - and their output is hashed and
measured rather than stored.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from traceh.api.promotion import VerificationPlan, VerifierCommand, VerifierOutcome
from traceh.concurrency import await_worker_convergence
from traceh.process_control import converge_process
from traceh.promotion.cleanup import release_scratch
from traceh.promotion.errors import PromotionVerificationError
from traceh.promotion.models import (
    freeze_verification_plan,
    verification_evidence_digest,
    verifier_command_digest,
    verifier_definition_digest,
)

_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_READ_CHUNK_BYTES = 65536
"""How much of a verifier stream is consumed per read.

It also bounds the overshoot: the read that first crosses the configured limit
kills the writer, so a command can exceed the bound by at most one chunk per
stream before it is stopped.
"""


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    """The complete bounded result of one fixed verification run."""

    definition_digest: str
    evidence_digest: str
    results: tuple[VerifierOutcome, ...]
    passed: bool


class VerificationRunner(Protocol):
    """Seam for executing a frozen plan inside one integration worktree."""

    async def run(
        self, plan: VerificationPlan, *, cwd: Path
    ) -> VerificationEvidence:
        ...


class HostVerificationRunner:
    """Execute every frozen command in order and record bounded evidence."""

    __slots__ = ()

    async def run(
        self, plan: VerificationPlan, *, cwd: Path
    ) -> VerificationEvidence:
        plan = freeze_verification_plan(plan)
        try:
            root = _absolute(cwd)
        except Exception:
            raise PromotionVerificationError(
                "promotion-verifier-cwd-invalid"
            ) from None
        if not root.is_dir():
            raise PromotionVerificationError("promotion-verifier-cwd-invalid")
        # The worktree must still equal the tree under review when this returns,
        # so a verifier needs somewhere else to write. Scratch space is granted
        # outside the checkout rather than carved out of it.
        scratch = _absolute(
            await asyncio.to_thread(
                tempfile.mkdtemp, prefix="traceh-verifier-scratch-"
            )
        )
        primary: BaseException | None = None
        try:
            environment = _verifier_environment(plan, scratch)
            results: list[VerifierOutcome] = []
            for command in plan.commands:
                results.append(
                    await self._execute(
                        command,
                        cwd=root,
                        environment=environment,
                        max_output_bytes=plan.max_output_bytes,
                    )
                )
        except BaseException as error:
            primary = error
            raise
        finally:
            await _remove_scratch(scratch, primary)
        outcomes = tuple(results)
        definition_digest = verifier_definition_digest(plan)
        return VerificationEvidence(
            definition_digest=definition_digest,
            evidence_digest=verification_evidence_digest(definition_digest, outcomes),
            results=outcomes,
            passed=all(outcome.passed for outcome in outcomes),
        )

    async def _execute(
        self,
        command: VerifierCommand,
        *,
        cwd: Path,
        environment: dict[str, str],
        max_output_bytes: int,
    ) -> VerifierOutcome:
        argv_digest = verifier_command_digest(command)
        try:
            spawn = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    *command.argv,
                    cwd=cwd,
                    env=environment,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                name="traceh-promotion-verifier-spawn",
            )
            try:
                process = await asyncio.shield(spawn)
            except asyncio.CancelledError as cancellation:
                await await_worker_convergence(spawn)
                if not spawn.cancelled() and spawn.exception() is None:
                    await converge_process(spawn.result())
                raise cancellation
        except (OSError, ValueError):
            return _outcome(
                command, argv_digest, status="start-failed", exit_code=None
            )

        # Reading each pipe as it fills is what makes the bound exact: the read
        # that first crosses the limit kills the writer, so overshoot is one
        # chunk per stream rather than "whatever it managed to produce".
        captures = (_StreamCapture(), _StreamCapture())
        breach = _OutputBreach(process)
        readers = tuple(
            asyncio.create_task(
                _drain(stream, capture, max_output_bytes, breach),
                name="traceh-promotion-verifier-drain",
            )
            for stream, capture in zip(
                (process.stdout, process.stderr), captures, strict=True
            )
        )
        timed_out = False
        try:
            # The deadline covers the readers too. A pipe reaches EOF only once
            # every descendant holding it has exited, so a timeout that covered
            # the direct child alone would be extended - or held open forever -
            # by an orphaned grandchild this host never owned.
            async with asyncio.timeout(command.timeout_ms / 1000):
                await process.wait()
                await asyncio.gather(*readers)
        except asyncio.CancelledError:
            await converge_process(process)
            raise
        except TimeoutError:
            interrupted = await converge_process(process)
            if interrupted:
                raise asyncio.CancelledError from None
            timed_out = True
        finally:
            # Partial counts survive cancellation because each reader accumulates
            # into a capture the caller owns.
            for reader in readers:
                reader.cancel()
            for reader in readers:
                await await_worker_convergence(reader)
            _release_pipes(process)

        exit_code = process.returncode
        if type(exit_code) is not int:
            exit_code = None
        if breach.triggered:
            status = "output-exceeded"
        elif timed_out:
            status = "timed-out"
        elif exit_code == 0:
            status = "passed"
        else:
            status = "failed"
        return _outcome(
            command,
            argv_digest,
            status=status,
            exit_code=exit_code,
            stdout_bytes=captures[0].size,
            stdout_digest=captures[0].digest,
            stderr_bytes=captures[1].size,
            stderr_digest=captures[1].digest,
        )


def _release_pipes(process: asyncio.subprocess.Process) -> None:
    """Drop this process' end of the pipes as soon as the command is settled.

    The read ends belong to the host, not to whatever the command spawned. An
    orphaned grandchild may still hold the write end for as long as it likes;
    that must not keep a host handle alive until interpreter shutdown.
    """

    transport = getattr(process, "_transport", None)
    close = getattr(transport, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception:
        # Releasing a handle is best effort; the command result is already
        # decided and must not be replaced by a cleanup failure.
        pass


class _OutputBreach:
    """Stop the child the first time either stream crosses the bound."""

    __slots__ = ("_process", "triggered")

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        self.triggered = False

    def trip(self) -> None:
        if self.triggered:
            return
        self.triggered = True
        try:
            self._process.kill()
        except (ProcessLookupError, OSError):
            pass


class _StreamCapture:
    """Bounded, cancellation-surviving accounting for one verifier stream.

    The reader task may be cancelled at the deadline, so what it has counted so
    far has to live in an object the caller keeps rather than in the task's
    return value.
    """

    __slots__ = ("_digest", "size")

    def __init__(self) -> None:
        self._digest = hashlib.sha256()
        self.size = 0

    def update(self, chunk: bytes) -> None:
        self.size += len(chunk)
        self._digest.update(chunk)

    @property
    def digest(self) -> str:
        return self._digest.hexdigest()


async def _drain(
    stream, capture: _StreamCapture, max_output_bytes: int, breach: _OutputBreach
) -> None:
    """Consume one stream, stopping the writer on the read that crosses the bound."""

    if stream is None:  # pragma: no cover - defensive; both pipes are requested
        return
    while True:
        try:
            chunk = await stream.read(_READ_CHUNK_BYTES)
        except (ValueError, OSError):
            return
        if not chunk:
            return
        capture.update(chunk)
        if capture.size > max_output_bytes:
            breach.trip()


def _outcome(
    command: VerifierCommand,
    argv_digest: str,
    *,
    status: str,
    exit_code: int | None,
    stdout_bytes: int = 0,
    stdout_digest: str = _EMPTY_SHA256,
    stderr_bytes: int = 0,
    stderr_digest: str = _EMPTY_SHA256,
) -> VerifierOutcome:
    return VerifierOutcome(
        command_id=command.command_id,
        argv_digest=argv_digest,
        status=status,
        exit_code=exit_code if status != "timed-out" else None,
        stdout_sha256=stdout_digest,
        stdout_bytes=stdout_bytes,
        stderr_sha256=stderr_digest,
        stderr_bytes=stderr_bytes,
    )


def _absolute(value: object) -> Path:
    return Path(value).absolute()  # type: ignore[arg-type]


async def _remove_scratch(root: Path, primary: BaseException | None) -> None:
    """Release verifier scratch space, reporting - never hiding - a failure."""

    await release_scratch(
        root,
        primary,
        remove=shutil.rmtree,
        task_name="traceh-promotion-verifier-scratch-cleanup",
        alone_error=lambda: PromotionVerificationError(
            "promotion-verifier-scratch-cleanup-failed"
        ),
        group_message="verifier scratch cleanup failed",
    )


def _verifier_environment(plan: VerificationPlan, scratch: Path) -> dict[str, str]:
    # Start from nothing the host did not name. Inherited ``GIT_*`` variables
    # are refused at plan-freeze time, so no verifier can be handed Git
    # configuration injected into this process.
    environment: dict[str, str] = {}
    for name in plan.environment.passthrough:
        value = os.environ.get(name)
        if value is not None and "\0" not in value:
            environment[name] = value
    for name, value in plan.environment.overrides:
        environment[name] = value
    # Point the usual temporary-directory variables at owned scratch space
    # outside the checkout. Passthrough only inherits whatever this process
    # happens to have, and the worktree proof forbids writing into the checkout,
    # so owned scratch outranks it. An explicit override is a real host decision
    # and still wins.
    chosen = {name for name, _ in plan.environment.overrides}
    for name in ("TMPDIR", "TEMP", "TMP"):
        if name not in chosen:
            environment[name] = str(scratch)
    return environment


__all__ = [
    "HostVerificationRunner",
    "VerificationEvidence",
    "VerificationRunner",
]
