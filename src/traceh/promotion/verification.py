"""Fixed host verification through the shared sandbox owner capability."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from traceh.api.promotion import VerificationPlan, VerifierCommand, VerifierOutcome
from traceh.api.sandbox import SandboxOwner, SandboxReceiptReference
from traceh.promotion.models import (
    freeze_verification_plan,
    verification_evidence_digest,
    verifier_command_digest,
    verifier_definition_digest,
)
from traceh.sandbox.service import GUEST_ENVIRONMENT, SandboxExecutionService


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    definition_digest: str
    evidence_digest: str
    results: tuple[VerifierOutcome, ...]
    passed: bool


class VerificationRunner(Protocol):
    async def run(
        self, plan: VerificationPlan, *, cwd: Path, owner: SandboxOwner, stream_id: str
    ) -> VerificationEvidence: ...


class HostVerificationRunner:
    """No subprocess fallback; raw verifier output stays transient."""

    def __init__(self, sandbox_service: SandboxExecutionService | None = None) -> None:
        self.sandbox_service = sandbox_service

    async def run(
        self, plan: VerificationPlan, *, cwd: Path, owner: SandboxOwner, stream_id: str
    ) -> VerificationEvidence:
        plan = freeze_verification_plan(plan)
        if owner.kind != "promotion" or not cwd.is_absolute():
            raise ValueError("promotion-verifier-owner-invalid")
        results = []
        if self.sandbox_service is None:
            results = [_outcome(command, status="start-failed") for command in plan.commands]
        else:
            environment = dict(GUEST_ENVIRONMENT)
            for name in plan.environment.passthrough:
                value = os.environ.get(name)
                if value is not None:
                    environment[name] = value
            environment.update(plan.environment.overrides)
            async with self.sandbox_service.scope(
                owner,
                stream_id=stream_id,
                workspace=cwd,
                data_dir=None,
                publish_changes=False,
                environment=tuple(sorted(environment.items())),
                retain_output=False,
            ) as execution:
                for command in plan.commands:
                    result = await execution.run(
                        command.argv,
                        timeout_seconds=command.timeout_ms / 1000,
                        max_output_bytes=plan.max_output_bytes,
                    )
                    status = verification_status(result.status, result.exit_code)
                    reference = SandboxReceiptReference(
                        stream_id,
                        result.receipt["execution_id"],
                        result.receipt["digest"],
                        result.receipt["policy_digest"],
                    )
                    results.append(
                        _outcome(
                            command,
                            status=status,
                            exit_code=result.exit_code,
                            stdout=result.stdout,
                            stderr=result.stderr,
                            execution=reference,
                        )
                    )
        outcomes = tuple(results)
        definition = verifier_definition_digest(plan)
        return VerificationEvidence(
            definition,
            verification_evidence_digest(definition, outcomes),
            outcomes,
            all(outcome.passed for outcome in outcomes),
        )


def _outcome(
    command: VerifierCommand, *, status: str, exit_code=None, stdout=b"", stderr=b"", execution=None
) -> VerifierOutcome:
    return VerifierOutcome(
        command.command_id,
        verifier_command_digest(command),
        status,
        exit_code,
        hashlib.sha256(stdout).hexdigest(),
        len(stdout),
        hashlib.sha256(stderr).hexdigest(),
        len(stderr),
        execution,
    )


def verification_status(status: str, exit_code: int | None) -> str:
    if status == "finished":
        return "passed" if exit_code == 0 else "failed"
    return status if status in {"timed-out", "output-exceeded"} else "start-failed"


__all__ = ["HostVerificationRunner", "VerificationEvidence", "VerificationRunner"]
