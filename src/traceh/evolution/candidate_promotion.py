"""L4 human-approved promotion and deterministic rollback control plane.

The module never imports a candidate and never participates in AgentRuntime.
It binds one successful L2 artifact, one successful L3 comparison, one target
interpreter and its currently managed receipt into an approval digest.  A
second invocation must present that exact digest before any installation is
attempted.
"""

from __future__ import annotations

import asyncio
import configparser
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import threading
import unicodedata
import zipfile
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Any

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from traceh.concurrency import await_worker_convergence
from traceh.evolution.artifacts import sha256_file
from traceh.evolution.candidate_comparison import (
    MAX_REPORT_BYTES,
    CandidateComparisonEvidenceError,
    ComparisonCandidate,
    InstalledDistribution,
    ValidationEvidence,
    load_comparison_evidence,
    load_validation_evidence,
    verify_validation_artifact,
    verify_validation_evidence_unchanged,
)
from traceh.evolution.candidate_comparison import (
    ComparisonEvidence as _ComparisonEvidence,
)
from traceh.evolution.candidate_validation import (
    CommandRequest,
    CommandRunner,
    SubprocessCommandRunner,
)
from traceh.session.file_lock import exclusive_file_lock
from traceh.tools.builtins.shell import sanitized_environment

PROMOTION_SCHEMA_VERSION = 1
PROMOTION_EXIT_CODE = 10
MAX_TARGET_RECEIPT_FILES = 10_000
MAX_TARGET_RECEIPT_BYTES = 100 * 1024 * 1024
MAX_TARGET_ENVIRONMENT_FILES = 100_000
MAX_TARGET_ENVIRONMENT_BYTES = 2 * 1024 * 1024 * 1024
MAX_TARGET_DISTRIBUTIONS = 1_000
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_ID = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
# Tests may override the coordination base.  Production derives it from the
# canonical target prefix instead of TEMP: two processes selecting different
# temporary directories must still contend on the same target-environment
# mutation lock.
_PROMOTION_COORDINATION_ROOT: Path | None = None


class CandidatePromotionConfigurationError(ValueError):
    """Safe host-authored configuration failure."""


class CandidatePromotionEvidenceError(ValueError):
    """Stable failure while reading L2/L3 or managed target evidence."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class CandidatePromotionExecutionError(RuntimeError):
    """Stable failure while changing or checking the target interpreter."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class CandidatePromotionRollbackError(BaseExceptionGroup):
    """Promotion failed and its convergent rollback also failed."""


@dataclass(frozen=True, slots=True)
class CandidatePromotionConfig:
    validation_evidence: Path
    comparison_evidence: Path
    target_python: Path
    registry: Path
    output: Path
    approval_digest: str | None = None
    command_timeout_seconds: float = 600.0


@dataclass(frozen=True, slots=True)
class CandidateRollbackConfig:
    target_python: Path
    registry: Path
    output: Path
    plugin_id: str
    distribution: str
    current_promotion_id: str
    command_timeout_seconds: float = 600.0


@dataclass(frozen=True, slots=True)
class TargetDistributionReceipt:
    distribution: str
    version: str
    plugin_id: str
    entry_value: str
    content_sha256: str
    file_count: int
    size_bytes: int


@dataclass(frozen=True, slots=True)
class TargetEnvironmentReceipt:
    content_sha256: str
    file_count: int
    size_bytes: int


@dataclass(frozen=True, slots=True)
class PromotionReport:
    schema_version: int
    action: str
    ok: bool
    created_at: str
    code: str
    approval_digest: str | None
    promotion_id: str | None
    plugin_id: str
    distribution: str
    version: str
    artifact_sha256: str
    validation_report_sha256: str | None
    comparison_report_sha256: str | None
    core_commit: str | None
    suite_id: str | None
    suite_digest: str | None
    target_python: str
    target_core_version: str
    classification: str
    improvements: tuple[str, ...]
    regressions: tuple[str, ...]
    previous_promotion_id: str | None
    rollback_kind: str
    boundaries_zh: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["improvements"] = list(self.improvements)
        result["regressions"] = list(self.regressions)
        result["boundaries_zh"] = list(self.boundaries_zh)
        return result


@dataclass(frozen=True, slots=True)
class _TargetFacts:
    python_implementation: str
    python_version: str
    python_prefix: str
    core_version: str
    distributions: tuple[InstalledDistribution, ...]
    environment_content: TargetEnvironmentReceipt
    candidate: TargetDistributionReceipt | None


@dataclass(frozen=True, slots=True)
class _PromotionState:
    status: str
    target_python: str
    plugin_id: str
    distribution: str
    current_promotion_id: str | None
    pending_promotion_id: str | None
    current_receipt: TargetDistributionReceipt | None
    environment_receipt: tuple[InstalledDistribution, ...]
    environment_content: TargetEnvironmentReceipt


@dataclass(frozen=True, slots=True)
class _PromotionRecord:
    promotion_id: str
    previous_promotion_id: str | None
    plugin_id: str
    distribution: str
    version: str
    entry_value: str
    artifact_sha256: str
    artifact_filename: str
    validation_report_sha256: str
    comparison_report_sha256: str
    approval_digest: str
    target_python: str
    target_core_version: str
    environment_receipt: tuple[InstalledDistribution, ...]
    environment_content: TargetEnvironmentReceipt | None


@dataclass(frozen=True, slots=True)
class _PromotionOwner:
    target_identity: str
    distribution: str
    registry: str


class CandidatePromoter:
    """Review or apply one exact, human-approved L4 promotion."""

    def __init__(
        self,
        config: CandidatePromotionConfig,
        *,
        runner: CommandRunner | None = None,
    ) -> None:
        self.config = config
        self.runner = runner or SubprocessCommandRunner()

    async def run(self) -> PromotionReport:
        config = _validated_promotion_config(self.config)
        validation = load_validation_evidence(config.validation_evidence)
        _verify_candidate_wheel_identity(validation)
        comparison = _load_comparison_evidence(config.comparison_evidence, validation)
        _require_promotable(comparison)
        facts = await _inspect_target(
            self.runner,
            config.target_python,
            validation.candidate,
            config.command_timeout_seconds,
        )
        _require_target_core(comparison, facts)
        _require_control_paths_outside_target(config.output, config.registry, facts)
        target_identity = _target_identity(facts)
        distribution = canonicalize_name(validation.candidate.distribution)
        ownership = _OwnershipPaths(target_identity)
        owner = _load_owner_if_present(ownership)
        _require_available_owner(owner, config.registry, distribution)
        _require_target_environment(comparison, facts, validation.candidate)
        paths = _RegistryPaths(
            config.registry,
            target_identity,
            distribution,
        )
        state = _load_state_if_present(paths, validation.candidate, target_identity)
        _verify_target_matches_state(state, facts)
        _reject_already_current(paths, state, validation)
        approval = _approval_digest(
            validation,
            comparison,
            config.target_python,
            config.registry,
            facts,
            state,
            owner,
        )
        report = _promotion_report(
            action="review" if config.approval_digest is None else "promote",
            code=("human-approval-required" if config.approval_digest is None else "promoted"),
            approval_digest=approval,
            promotion_id=None,
            validation=validation,
            comparison=comparison,
            target_python=config.target_python,
            target_core_version=facts.core_version,
            previous_promotion_id=state.current_promotion_id if state else None,
        )
        if config.approval_digest is None:
            _commit_report_bundle(config.output, report)
            return report
        if config.approval_digest != approval:
            raise CandidatePromotionEvidenceError(
                "promotion-approval-digest-mismatch",
                "Approval digest does not match the current evidence and target state",
            )

        async with _AsyncPromotionLocks(ownership.lock, paths.lock):
            owner = _load_owner_if_present(ownership)
            _require_available_owner(owner, config.registry, distribution)
            # Re-read every fact under the cross-process mutation lock.  An
            # approval is deliberately one-shot with respect to target drift.
            validation = load_validation_evidence(config.validation_evidence)
            _verify_candidate_wheel_identity(validation)
            comparison = _load_comparison_evidence(config.comparison_evidence, validation)
            _require_promotable(comparison)
            facts = await _inspect_target(
                self.runner,
                config.target_python,
                validation.candidate,
                config.command_timeout_seconds,
            )
            _require_target_core(comparison, facts)
            _require_target_environment(comparison, facts, validation.candidate)
            _require_control_paths_outside_target(config.output, config.registry, facts)
            if _target_identity(facts) != target_identity:
                raise CandidatePromotionEvidenceError(
                    "promotion-target-identity-changed",
                    "Target environment identity changed after human review",
                )
            state = _load_state_if_present(paths, validation.candidate, target_identity)
            _verify_target_matches_state(state, facts)
            _reject_already_current(paths, state, validation)
            if _approval_digest(
                validation,
                comparison,
                config.target_python,
                config.registry,
                facts,
                state,
                owner,
            ) != config.approval_digest:
                raise CandidatePromotionEvidenceError(
                    "promotion-approval-stale",
                    "Evidence or target state changed after human review",
                )
            verify_validation_evidence_unchanged(config.validation_evidence, validation)
            if (config.comparison_evidence / "report.json").read_bytes() != comparison.content:
                raise CandidatePromotionEvidenceError(
                    "l3-report-identity-changed",
                    "L3 report changed during promotion",
                )
            verify_validation_artifact(validation.artifact_source, validation)
            claimed_owner = owner is None
            promotion_id = _promotion_id(config.approval_digest)
            artifact = _store_artifact(paths, validation)
            record = _PromotionRecord(
                promotion_id=promotion_id,
                previous_promotion_id=state.current_promotion_id if state else None,
                plugin_id=validation.candidate.plugin_id,
                distribution=canonicalize_name(validation.candidate.distribution),
                version=validation.candidate.version,
                entry_value=validation.candidate.entry_value,
                artifact_sha256=validation.artifact.sha256,
                artifact_filename=artifact.name,
                validation_report_sha256=validation.report_sha256,
                comparison_report_sha256=comparison.sha256,
                approval_digest=config.approval_digest,
                target_python=target_identity,
                target_core_version=facts.core_version,
                environment_receipt=comparison.environment_receipt,
                environment_content=None,
            )
            _store_record(paths, record)
            if claimed_owner:
                _store_owner(ownership, config.registry, distribution)
            previous = state or _empty_state(
                target_identity,
                validation.candidate,
                facts.distributions,
                facts.environment_content,
            )
            pending = _PromotionState(
                status="installing",
                target_python=previous.target_python,
                plugin_id=previous.plugin_id,
                distribution=previous.distribution,
                current_promotion_id=previous.current_promotion_id,
                pending_promotion_id=promotion_id,
                current_receipt=previous.current_receipt,
                environment_receipt=previous.environment_receipt,
                environment_content=previous.environment_content,
            )
            try:
                _store_state(paths, pending)
            except BaseException:
                if claimed_owner:
                    _remove_owner(ownership, config.registry, distribution)
                raise
            try:
                installed_facts = await self._install_and_check(
                    config,
                    validation,
                    comparison,
                    artifact,
                )
            except BaseException as error:
                await _rollback_after_failure(
                    self.runner,
                    config.target_python,
                    paths,
                    previous,
                    validation.candidate,
                    config.command_timeout_seconds,
                    error,
                )
                if claimed_owner:
                    _remove_owner(ownership, config.registry, distribution)
                raise error from None
            stable = _PromotionState(
                status="stable",
                target_python=previous.target_python,
                plugin_id=previous.plugin_id,
                distribution=previous.distribution,
                current_promotion_id=promotion_id,
                pending_promotion_id=None,
                current_receipt=installed_facts.candidate,
                environment_receipt=comparison.environment_receipt,
                environment_content=installed_facts.environment_content,
            )
            assert installed_facts.candidate is not None
            _store_receipt(paths, promotion_id, installed_facts.candidate)
            _store_record(
                paths,
                replace(record, environment_content=installed_facts.environment_content),
            )
            _store_state(paths, stable)
            report = _promotion_report(
                action="promote",
                code="promoted",
                approval_digest=config.approval_digest,
                promotion_id=promotion_id,
                validation=validation,
                comparison=comparison,
                target_python=config.target_python,
                target_core_version=facts.core_version,
                previous_promotion_id=previous.current_promotion_id,
            )
            try:
                _commit_report_bundle(config.output, report)
            except BaseException as error:
                shutil.rmtree(config.output, ignore_errors=True)
                await _rollback_after_failure(
                    self.runner,
                    config.target_python,
                    paths,
                    previous,
                    validation.candidate,
                    config.command_timeout_seconds,
                    error,
                )
                if claimed_owner:
                    _remove_owner(ownership, config.registry, distribution)
                raise error from None
            return report

    async def _install_and_check(
        self,
        config: CandidatePromotionConfig,
        validation: ValidationEvidence,
        comparison: _ComparisonEvidence,
        artifact: Path,
    ) -> _TargetFacts:
        await _run_required(
            self.runner,
            CommandRequest(
                "promotion-install",
                (
                    str(config.target_python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-input",
                    "--no-index",
                    "--no-deps",
                    "--no-compile",
                    "--force-reinstall",
                    str(artifact),
                ),
                artifact.parent,
                _target_environment(),
                config.command_timeout_seconds,
            ),
            "promotion-install-failed",
        )
        facts = await _inspect_target(
            self.runner,
            config.target_python,
            validation.candidate,
            config.command_timeout_seconds,
        )
        receipt = facts.candidate
        if (
            receipt is None
            or receipt.version != validation.candidate.version
            or receipt.entry_value != validation.candidate.entry_value
        ):
            raise CandidatePromotionExecutionError(
                "promotion-installed-identity-mismatch",
                "Installed candidate identity does not match the approved artifact",
            )
        if facts.distributions != comparison.environment_receipt:
            raise CandidatePromotionExecutionError(
                "promotion-installed-environment-mismatch",
                "Installed target does not match the compared Distribution receipt",
            )
        await _doctor(
            self.runner,
            config.target_python,
            validation.candidate.plugin_id,
            artifact.parent,
            config.command_timeout_seconds,
        )
        after_doctor = await _inspect_target(
            self.runner,
            config.target_python,
            validation.candidate,
            config.command_timeout_seconds,
        )
        if (
            after_doctor.candidate != receipt
            or after_doctor.distributions != comparison.environment_receipt
            or after_doctor.environment_content != facts.environment_content
        ):
            raise CandidatePromotionExecutionError(
                "promotion-doctor-target-drift",
                "Plugin doctor changed the approved target environment",
            )
        return after_doctor


class CandidateRollbacker:
    """Roll a managed plugin back to its immediately previous exact state."""

    def __init__(
        self,
        config: CandidateRollbackConfig,
        *,
        runner: CommandRunner | None = None,
    ) -> None:
        self.config = config
        self.runner = runner or SubprocessCommandRunner()

    async def run(self) -> PromotionReport:
        config = _validated_rollback_config(self.config)
        requested = ComparisonCandidate(
            config.distribution,
            "unknown",
            config.plugin_id,
            "unknown:unknown",
            "unknown",
        )
        facts = await _inspect_target(
            self.runner,
            config.target_python,
            requested,
            config.command_timeout_seconds,
        )
        _require_control_paths_outside_target(config.output, config.registry, facts)
        target_identity = _target_identity(facts)
        ownership = _OwnershipPaths(target_identity)
        paths = _RegistryPaths(config.registry, target_identity, config.distribution)
        async with _AsyncPromotionLocks(ownership.lock, paths.lock):
            owner = _load_owner_if_present(ownership)
            if owner is None:
                raise CandidatePromotionEvidenceError(
                    "promotion-owner-missing",
                    "Target Distribution has no promotion Registry owner",
                )
            _require_available_owner(owner, config.registry, config.distribution)
            state = _load_state_if_present(paths, requested, target_identity)
            if state is None:
                # Apply writes the immutable record and package owner before
                # it writes the first ``installing`` state.  A process killed
                # in that narrow window has not started pip yet, but must not
                # leave an owner that no later command can recover.  Rebuild
                # the missing pending state only from that exact first record
                # and the observed absent target, then use the ordinary
                # rollback path below.
                interrupted = _load_record(paths, config.current_promotion_id)
                if (
                    interrupted.plugin_id != config.plugin_id
                    or interrupted.distribution
                    != canonicalize_name(config.distribution)
                    or interrupted.previous_promotion_id is not None
                    or facts.candidate is not None
                ):
                    raise CandidatePromotionEvidenceError(
                        "promotion-state-missing",
                        "Promotion state is missing and cannot be reconstructed safely",
                    )
                state = _PromotionState(
                    status="installing",
                    target_python=target_identity,
                    plugin_id=interrupted.plugin_id,
                    distribution=interrupted.distribution,
                    current_promotion_id=None,
                    pending_promotion_id=interrupted.promotion_id,
                    current_receipt=None,
                    environment_receipt=facts.distributions,
                    environment_content=facts.environment_content,
                )
                _store_state(paths, state)
            if state.status == "stable":
                source_id = state.current_promotion_id
                if source_id != config.current_promotion_id or source_id is None:
                    raise CandidatePromotionEvidenceError(
                        "rollback-current-state-mismatch",
                        "Rollback target is not the current stable managed promotion",
                    )
                current = _load_record(paths, source_id)
                target_id = current.previous_promotion_id
                require_stable_receipt = True
            elif state.status == "installing":
                source_id = state.pending_promotion_id
                if source_id != config.current_promotion_id or source_id is None:
                    raise CandidatePromotionEvidenceError(
                        "rollback-current-state-mismatch",
                        "Rollback id does not name the unfinished promotion",
                    )
                current = _load_record(paths, source_id)
                target_id = state.current_promotion_id
                require_stable_receipt = False
            else:
                source_id = state.current_promotion_id
                if source_id != config.current_promotion_id or source_id is None:
                    raise CandidatePromotionEvidenceError(
                        "rollback-current-state-mismatch",
                        "Rollback id does not name the unfinished rollback source",
                    )
                current = _load_record(paths, source_id)
                target_id = state.pending_promotion_id
                require_stable_receipt = False
            if current.plugin_id != config.plugin_id:
                raise CandidatePromotionEvidenceError(
                    "rollback-current-state-mismatch",
                    "Rollback record does not belong to this plugin",
                )
            candidate = ComparisonCandidate(
                current.distribution,
                current.version,
                current.plugin_id,
                current.entry_value,
                current.entry_value.partition(":")[0],
            )
            facts = await _inspect_target(
                self.runner,
                config.target_python,
                candidate,
                config.command_timeout_seconds,
            )
            if require_stable_receipt:
                _verify_target_matches_state(state, facts)
            rollbacking = _PromotionState(
                status="rollbacking",
                target_python=state.target_python,
                plugin_id=state.plugin_id,
                distribution=state.distribution,
                current_promotion_id=source_id,
                pending_promotion_id=target_id,
                current_receipt=state.current_receipt,
                environment_receipt=state.environment_receipt,
                environment_content=state.environment_content,
            )
            _store_state(paths, rollbacking)
            restore = asyncio.create_task(
                _restore_previous(
                    self.runner,
                    config.target_python,
                    paths,
                    target_id,
                    candidate,
                    config.command_timeout_seconds,
                )
            )
            cancellation: asyncio.CancelledError | None = None
            try:
                restored_facts = await asyncio.shield(restore)
            except asyncio.CancelledError as error:
                cancellation = error
                await await_worker_convergence(restore)
                if restore.cancelled():
                    raise
                cleanup_error = restore.exception()
                if cleanup_error is not None:
                    raise cleanup_error from error
                restored_facts = restore.result()
            stable = _PromotionState(
                status="stable",
                target_python=state.target_python,
                plugin_id=state.plugin_id,
                distribution=state.distribution,
                current_promotion_id=target_id,
                pending_promotion_id=None,
                current_receipt=restored_facts.candidate,
                environment_receipt=restored_facts.distributions,
                environment_content=restored_facts.environment_content,
            )
            _store_state(paths, stable)
            if target_id is None:
                _remove_owner(ownership, config.registry, config.distribution)
            report = PromotionReport(
                schema_version=PROMOTION_SCHEMA_VERSION,
                action="rollback",
                ok=True,
                created_at=datetime.now(UTC).isoformat(),
                code="rolled-back",
                approval_digest=None,
                promotion_id=target_id,
                plugin_id=current.plugin_id,
                distribution=current.distribution,
                version=(
                    _load_record(paths, target_id).version if target_id is not None else "absent"
                ),
                artifact_sha256=(
                    _load_record(paths, target_id).artifact_sha256
                    if target_id is not None
                    else ""
                ),
                validation_report_sha256=current.validation_report_sha256,
                comparison_report_sha256=current.comparison_report_sha256,
                core_commit=None,
                suite_id=None,
                suite_digest=None,
                target_python=str(config.target_python),
                target_core_version=facts.core_version,
                classification="rollback",
                improvements=(),
                regressions=(),
                previous_promotion_id=current.promotion_id,
                rollback_kind="exact-wheel" if target_id is not None else "uninstall",
                boundaries_zh=_boundaries(),
            )
            try:
                _commit_report_bundle(config.output, report)
            except BaseException:
                # The rollback itself is complete and is recorded in the
                # Registry.  The requested report path is a mirror, not the
                # state authority, so a reporting failure cannot undo a
                # successful recovery to the previous state.
                raise
            if cancellation is not None:
                raise cancellation
            return report


class _RegistryPaths:
    def __init__(self, root: Path, target_identity: str, distribution: str) -> None:
        owner = f"{target_identity}\0{canonicalize_name(distribution)}"
        owner_key = hashlib.sha256(owner.encode()).hexdigest()[:32]
        self.root = root
        # Keep the registry deliberately shallow.  The target interpreter may
        # itself live under a long Windows path, and L4 must not recreate the
        # legacy MAX_PATH problem inside a user-selected registry.
        self.target = root / "managed" / owner_key
        self.lock = self.target / "mutation.lock"
        self.state = self.target / "state.json"
        self.records = self.target / "r"
        self.receipts = self.target / "q"
        self.artifacts = root / "a"


class _OwnershipPaths:
    def __init__(self, target_identity: str) -> None:
        owner_key = hashlib.sha256(target_identity.encode()).hexdigest()
        self.target_identity = target_identity
        coordination_root = _PROMOTION_COORDINATION_ROOT
        if coordination_root is None:
            coordination_root = (
                Path(target_identity).parent / ".traceh-promotion-coordination-v1"
            )
        self.root = coordination_root / owner_key[:32]
        self.lock = self.root / "mutation.lock"
        self.owner = self.root / "owner.json"


def _record_path(directory: Path, promotion_id: str) -> Path:
    return directory / f"{promotion_id[:32]}.json"


def _target_identity(facts: _TargetFacts) -> str:
    return os.path.normcase(str(Path(facts.python_prefix).resolve()))


def _load_owner_if_present(paths: _OwnershipPaths) -> _PromotionOwner | None:
    if not paths.owner.exists():
        return None
    raw = _read_json(paths.owner, "promotion-owner-invalid")
    if raw.pop("schema_version", None) != PROMOTION_SCHEMA_VERSION:
        raise CandidatePromotionEvidenceError(
            "promotion-owner-invalid", "Promotion owner record is invalid"
        )
    try:
        owner = _PromotionOwner(**raw)
    except TypeError as error:
        raise CandidatePromotionEvidenceError(
            "promotion-owner-invalid", "Promotion owner record is invalid"
        ) from error
    if (
        owner.target_identity != paths.target_identity
        or owner.distribution != canonicalize_name(owner.distribution)
        or not owner.distribution
        or owner.registry != str(Path(owner.registry).resolve())
    ):
        raise CandidatePromotionEvidenceError(
            "promotion-owner-invalid", "Promotion owner record is invalid"
        )
    return owner


def _require_available_owner(
    owner: _PromotionOwner | None,
    registry: Path,
    distribution: str,
) -> None:
    if owner is None:
        return
    if owner.distribution != canonicalize_name(distribution):
        raise CandidatePromotionEvidenceError(
            "promotion-target-owned-by-another-distribution",
            "Target environment already has another managed Distribution",
        )
    if owner.registry != str(registry):
        raise CandidatePromotionEvidenceError(
            "promotion-distribution-owned-by-another-registry",
            "Target Distribution is managed by another promotion Registry",
        )


def _store_owner(paths: _OwnershipPaths, registry: Path, distribution: str) -> None:
    owner = _PromotionOwner(
        paths.target_identity,
        canonicalize_name(distribution),
        str(registry),
    )
    _atomic_json(
        paths.owner,
        {"schema_version": PROMOTION_SCHEMA_VERSION, **asdict(owner)},
    )


def _remove_owner(paths: _OwnershipPaths, registry: Path, distribution: str) -> None:
    owner = _load_owner_if_present(paths)
    if owner is None:
        return
    _require_available_owner(owner, registry, distribution)
    paths.owner.unlink()
    try:
        paths.root.rmdir()
    except OSError:
        pass


class _AsyncRegistryLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._cancel = threading.Event()
        self._ready = threading.Event()
        self._release = threading.Event()
        self._error: BaseException | None = None
        self._holder: asyncio.Task[None] | None = None

    async def __aenter__(self) -> _AsyncRegistryLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        def hold() -> None:
            try:
                with exclusive_file_lock(self.path, cancel_event=self._cancel):
                    self._ready.set()
                    self._release.wait()
            except BaseException as error:
                self._error = error
                self._ready.set()

        self._holder = asyncio.create_task(asyncio.to_thread(hold))
        waiter = asyncio.create_task(asyncio.to_thread(self._ready.wait))
        try:
            await asyncio.shield(waiter)
        except asyncio.CancelledError as cancellation:
            self._cancel.set()
            self._release.set()
            await await_worker_convergence(waiter)
            await await_worker_convergence(self._holder)
            raise cancellation
        if self._error is not None:
            await self._holder
            raise self._error
        return self

    async def __aexit__(self, *args: object) -> None:
        assert self._holder is not None
        self._release.set()
        try:
            await asyncio.shield(self._holder)
        except asyncio.CancelledError as cancellation:
            await await_worker_convergence(self._holder)
            raise cancellation


class _AsyncPromotionLocks:
    """Acquire the target-wide lock before the caller-selected Registry lock."""

    def __init__(self, ownership: Path, registry: Path) -> None:
        self._ownership = _AsyncRegistryLock(ownership)
        self._registry = _AsyncRegistryLock(registry)

    async def __aenter__(self) -> _AsyncPromotionLocks:
        await self._ownership.__aenter__()
        try:
            await self._registry.__aenter__()
        except BaseException:
            await self._ownership.__aexit__()
            raise
        return self

    async def __aexit__(self, *args: object) -> None:
        registry_error: BaseException | None = None
        try:
            await self._registry.__aexit__(*args)
        except BaseException as error:
            registry_error = error
        try:
            await self._ownership.__aexit__(*args)
        except BaseException as error:
            if registry_error is not None:
                raise BaseExceptionGroup(
                    "promotion lock release failed", (registry_error, error)
                ) from None
            raise
        if registry_error is not None:
            raise registry_error


def _validated_promotion_config(config: CandidatePromotionConfig) -> CandidatePromotionConfig:
    validation = config.validation_evidence.resolve()
    comparison = config.comparison_evidence.resolve()
    # A POSIX venv's ``bin/python`` is commonly a symlink to the base
    # interpreter.  Resolving that final symlink would silently inspect and
    # mutate the base environment instead of the explicitly selected venv.
    target = Path(os.path.abspath(config.target_python))
    registry = config.registry.resolve()
    output = config.output.resolve()
    if not validation.is_dir() or not comparison.is_dir():
        raise CandidatePromotionConfigurationError("L2 and L3 evidence must exist")
    if not target.is_file():
        raise CandidatePromotionConfigurationError("Target Python must be an existing file")
    if output.exists():
        raise CandidatePromotionConfigurationError("Promotion output must be a new directory")
    if registry.exists() and not registry.is_dir():
        raise CandidatePromotionConfigurationError("Promotion registry must be a directory")
    if any(_paths_overlap(output, path) for path in (validation, comparison, registry)):
        raise CandidatePromotionConfigurationError("Promotion output must be disjoint")
    if not math.isfinite(config.command_timeout_seconds) or config.command_timeout_seconds <= 0:
        raise CandidatePromotionConfigurationError("Promotion timeout must be positive")
    approval = config.approval_digest
    if approval is not None and not _SHA256.fullmatch(approval):
        raise CandidatePromotionConfigurationError("Approval digest must be lowercase SHA-256")
    return CandidatePromotionConfig(
        validation,
        comparison,
        target,
        registry,
        output,
        approval,
        config.command_timeout_seconds,
    )


def _validated_rollback_config(config: CandidateRollbackConfig) -> CandidateRollbackConfig:
    target = Path(os.path.abspath(config.target_python))
    registry = config.registry.resolve()
    output = config.output.resolve()
    if not target.is_file() or not registry.is_dir():
        raise CandidatePromotionConfigurationError("Target Python and registry must exist")
    if output.exists() or _paths_overlap(output, registry):
        raise CandidatePromotionConfigurationError("Rollback output must be new and disjoint")
    if not _SAFE_ID.fullmatch(config.plugin_id):
        raise CandidatePromotionConfigurationError("Plugin id is invalid")
    distribution = canonicalize_name(config.distribution)
    if not config.distribution or distribution != config.distribution:
        raise CandidatePromotionConfigurationError("Distribution name must be canonical")
    if not _SHA256.fullmatch(config.current_promotion_id):
        raise CandidatePromotionConfigurationError("Current promotion id is invalid")
    if not math.isfinite(config.command_timeout_seconds) or config.command_timeout_seconds <= 0:
        raise CandidatePromotionConfigurationError("Rollback timeout must be positive")
    return CandidateRollbackConfig(
        target,
        registry,
        output,
        config.plugin_id,
        distribution,
        config.current_promotion_id,
        config.command_timeout_seconds,
    )


def _verify_candidate_wheel_identity(evidence: ValidationEvidence) -> None:
    """Rebind the archive's own metadata to the L2 report identity.

    Archive safety and byte identity are checked by the shared L2 parser.  L4
    additionally needs this semantic check because a mismatched distribution
    could otherwise be installed but not found (and therefore not removed) by
    the rollback path named in a corrupted report.
    """

    try:
        with zipfile.ZipFile(evidence.artifact_source) as archive:
            roots = {
                PurePosixPath(info.filename).parts[0]
                for info in archive.infolist()
                if PurePosixPath(info.filename).parts
                and PurePosixPath(info.filename).parts[0].endswith(".dist-info")
            }
            if len(roots) != 1:
                raise CandidatePromotionEvidenceError(
                    "promotion-wheel-identity-invalid",
                    "Candidate Wheel has no unique metadata root",
                )
            root = next(iter(roots))
            metadata_content = archive.read(f"{root}/METADATA")
            entry_content = archive.read(f"{root}/entry_points.txt")
    except (OSError, KeyError, zipfile.BadZipFile) as error:
        raise CandidatePromotionEvidenceError(
            "promotion-wheel-identity-invalid",
            "Candidate Wheel metadata cannot be read",
        ) from error
    if len(metadata_content) > MAX_REPORT_BYTES or len(entry_content) > MAX_REPORT_BYTES:
        raise CandidatePromotionEvidenceError(
            "promotion-wheel-identity-invalid",
            "Candidate Wheel metadata exceeds its budget",
        )
    metadata = BytesParser().parsebytes(metadata_content, headersonly=True)
    name = metadata.get("Name")
    version = metadata.get("Version")
    try:
        parsed_version = Version(version) if isinstance(version, str) else None
        expected_version = Version(evidence.candidate.version)
    except InvalidVersion as error:
        raise CandidatePromotionEvidenceError(
            "promotion-wheel-identity-invalid",
            "Candidate Wheel version is invalid",
        ) from error
    if (
        not isinstance(name, str)
        or canonicalize_name(name) != canonicalize_name(evidence.candidate.distribution)
        or parsed_version != expected_version
    ):
        raise CandidatePromotionEvidenceError(
            "promotion-wheel-identity-mismatch",
            "Candidate Wheel metadata does not match its L2 identity",
        )
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    try:
        parser.read_string(entry_content.decode("utf-8"))
        entries = [
            value.strip()
            for key, value in parser.items("traceh.plugins")
            if key == evidence.candidate.plugin_id
        ]
    except (UnicodeDecodeError, configparser.Error) as error:
        raise CandidatePromotionEvidenceError(
            "promotion-wheel-entry-point-invalid",
            "Candidate Wheel Entry Point metadata is invalid",
        ) from error
    if entries != [evidence.candidate.entry_value]:
        raise CandidatePromotionEvidenceError(
            "promotion-wheel-entry-point-mismatch",
            "Candidate Wheel Entry Point does not match its L2 identity",
        )


def _load_comparison_evidence(
    root: Path,
    validation: ValidationEvidence,
) -> _ComparisonEvidence:
    try:
        evidence = load_comparison_evidence(root)
    except CandidateComparisonEvidenceError as error:
        raise CandidatePromotionEvidenceError(
            error.code, "L3 report is not one canonical successful comparison"
        ) from error
    if evidence.candidate != validation.candidate or evidence.artifact != validation.artifact:
        raise CandidatePromotionEvidenceError(
            "l3-l2-identity-mismatch", "L3 candidate does not match its L2 evidence"
        )
    if (
        evidence.validation_report_sha256 != validation.report_sha256
        or evidence.core_commit != validation.core_commit
    ):
        raise CandidatePromotionEvidenceError(
            "l3-l2-evidence-mismatch", "L3 report does not bind the selected L2 evidence"
        )
    return evidence


def _environment_receipt(raw: object) -> tuple[InstalledDistribution, ...]:
    if not isinstance(raw, list) or not raw or len(raw) > 1_000:
        raise CandidatePromotionEvidenceError("l3-receipt-invalid", "L3 receipt is invalid")
    result: list[InstalledDistribution] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise CandidatePromotionEvidenceError("l3-receipt-invalid", "L3 receipt is invalid")
        name, version = item.get("name"), item.get("version")
        if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
            raise CandidatePromotionEvidenceError("l3-receipt-invalid", "L3 receipt is invalid")
        normalized = canonicalize_name(name)
        if normalized in seen:
            raise CandidatePromotionEvidenceError("l3-receipt-invalid", "L3 receipt is invalid")
        seen.add(normalized)
        result.append(InstalledDistribution(normalized, version))
    return tuple(sorted(result, key=lambda item: item.name))


def _managed_environment_receipt(
    raw: object,
    code: str,
) -> tuple[InstalledDistribution, ...]:
    try:
        return _environment_receipt(raw)
    except CandidatePromotionEvidenceError as error:
        raise CandidatePromotionEvidenceError(
            code,
            "Managed Distribution receipt is invalid",
        ) from error


def _require_promotable(comparison: _ComparisonEvidence) -> None:
    if comparison.classification != "improved" or not comparison.improvements or (
        comparison.regressions
    ):
        raise CandidatePromotionEvidenceError(
            "comparison-not-promotable",
            "Only an improved L3 report with zero regressions can be promoted",
        )


def _require_target_core(comparison: _ComparisonEvidence, facts: _TargetFacts) -> None:
    versions = {item.name: item.version for item in comparison.environment_receipt}
    expected = versions.get("traceharness-py")
    if expected is None or facts.core_version != expected:
        raise CandidatePromotionEvidenceError(
            "promotion-target-core-mismatch",
            "Target core version does not match the compared environment",
        )


def _require_target_environment(
    comparison: _ComparisonEvidence,
    facts: _TargetFacts,
    candidate: ComparisonCandidate,
) -> None:
    candidate_name = canonicalize_name(candidate.distribution)
    expected = tuple(
        item for item in comparison.environment_receipt if item.name != candidate_name
    )
    observed = tuple(item for item in facts.distributions if item.name != candidate_name)
    if expected != observed:
        raise CandidatePromotionEvidenceError(
            "promotion-target-environment-mismatch",
            "Target dependencies do not match the compared environment",
        )


def _approval_digest(
    validation: ValidationEvidence,
    comparison: _ComparisonEvidence,
    target_python: Path,
    registry: Path,
    facts: _TargetFacts,
    state: _PromotionState | None,
    owner: _PromotionOwner | None,
) -> str:
    payload = {
        "schema_version": PROMOTION_SCHEMA_VERSION,
        "validation_report_sha256": validation.report_sha256,
        "comparison_report_sha256": comparison.sha256,
        "artifact_sha256": validation.artifact.sha256,
        "candidate": asdict(validation.candidate),
        "target_python": str(target_python),
        "registry": str(registry),
        "python_implementation": facts.python_implementation,
        "python_version": facts.python_version,
        "python_prefix": facts.python_prefix,
        "target_core_version": facts.core_version,
        "target_distributions": [asdict(item) for item in facts.distributions],
        "target_environment_content": asdict(facts.environment_content),
        "target_candidate": asdict(facts.candidate) if facts.candidate else None,
        "managed_current": state.current_promotion_id if state else None,
        "coordination_owner": asdict(owner) if owner else None,
        "classification": comparison.classification,
        "improvements": list(comparison.improvements),
        "regressions": list(comparison.regressions),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _promotion_id(approval_digest: str) -> str:
    return hashlib.sha256(f"traceh-promotion-v1:{approval_digest}".encode()).hexdigest()


def _empty_state(
    target_identity: str,
    candidate: ComparisonCandidate,
    environment_receipt: tuple[InstalledDistribution, ...],
    environment_content: TargetEnvironmentReceipt,
) -> _PromotionState:
    return _PromotionState(
        "stable",
        target_identity,
        candidate.plugin_id,
        canonicalize_name(candidate.distribution),
        None,
        None,
        None,
        environment_receipt,
        environment_content,
    )


def _verify_target_matches_state(
    state: _PromotionState | None,
    facts: _TargetFacts,
) -> None:
    if state is None:
        if facts.candidate is not None:
            raise CandidatePromotionEvidenceError(
                "promotion-unmanaged-target-conflict",
                "Candidate distribution is installed but is not owned by this registry",
            )
        return
    if state.status != "stable":
        raise CandidatePromotionEvidenceError(
            "promotion-target-not-stable",
            "Managed target has an unfinished install or rollback",
        )
    if facts.distributions != state.environment_receipt:
        raise CandidatePromotionEvidenceError(
            "promotion-target-drift",
            "Target Distribution environment drifted from its managed receipt",
        )
    if facts.environment_content != state.environment_content:
        raise CandidatePromotionEvidenceError(
            "promotion-target-drift",
            "Target installed-package content drifted from its managed receipt",
        )
    if state.current_promotion_id is None:
        if facts.candidate is not None or state.current_receipt is not None:
            raise CandidatePromotionEvidenceError("promotion-target-drift", "Target state drifted")
    elif facts.candidate != state.current_receipt:
        raise CandidatePromotionEvidenceError("promotion-target-drift", "Target state drifted")


def _reject_already_current(
    paths: _RegistryPaths,
    state: _PromotionState | None,
    validation: ValidationEvidence,
) -> None:
    if state is None or state.current_promotion_id is None:
        return
    current = _load_record(paths, state.current_promotion_id)
    if (
        current.artifact_sha256 == validation.artifact.sha256
        and current.version == validation.candidate.version
        and current.entry_value == validation.candidate.entry_value
    ):
        raise CandidatePromotionEvidenceError(
            "promotion-artifact-already-current",
            "The exact candidate artifact is already the current managed promotion",
        )


async def _inspect_target(
    runner: CommandRunner,
    python: Path,
    candidate: ComparisonCandidate,
    timeout_seconds: float,
) -> _TargetFacts:
    outcome = await runner.run(
        CommandRequest(
            "promotion-target-inspect",
            (
                str(python),
                "-I",
                "-S",
                "-c",
                _TARGET_INSPECTION_SCRIPT,
                canonicalize_name(candidate.distribution),
                candidate.plugin_id,
                str(MAX_TARGET_RECEIPT_FILES),
                str(MAX_TARGET_RECEIPT_BYTES),
                str(MAX_TARGET_DISTRIBUTIONS),
                str(MAX_TARGET_ENVIRONMENT_FILES),
                str(MAX_TARGET_ENVIRONMENT_BYTES),
            ),
            python.parent,
            _target_environment(),
            min(timeout_seconds, 120.0),
            capture_stdout=True,
            stdout_limit=1_000_000,
        )
    )
    if outcome.start_failed or outcome.timed_out or outcome.exit_code != 0:
        raise CandidatePromotionExecutionError(
            "promotion-target-inspection-failed", "Target interpreter cannot be inspected"
        )
    try:
        raw = json.loads(outcome.stdout)
    except json.JSONDecodeError as error:
        raise CandidatePromotionExecutionError(
            "promotion-target-inspection-invalid", "Target receipt is invalid"
        ) from error
    if not isinstance(raw, dict) or not isinstance(raw.get("core_version"), str):
        raise CandidatePromotionExecutionError(
            "promotion-target-inspection-invalid", "Target receipt is invalid"
        )
    implementation = raw.get("python_implementation")
    python_version = raw.get("python_version")
    python_prefix = raw.get("python_prefix")
    if any(
        not isinstance(value, str) or not value or len(value) > 1_000
        for value in (implementation, python_version, python_prefix)
    ):
        raise CandidatePromotionExecutionError(
            "promotion-target-inspection-invalid",
            "Target Python identity is invalid",
        )
    try:
        distributions = _environment_receipt(raw.get("distributions"))
    except CandidatePromotionEvidenceError as error:
        raise CandidatePromotionExecutionError(
            "promotion-target-inspection-invalid",
            "Target Distribution receipt is invalid",
        ) from error
    environment_raw = raw.get("environment_content")
    if not isinstance(environment_raw, dict):
        raise CandidatePromotionExecutionError(
            "promotion-target-inspection-invalid",
            "Target installed-package content receipt is invalid",
        )
    try:
        environment_content = TargetEnvironmentReceipt(**environment_raw)
    except TypeError as error:
        raise CandidatePromotionExecutionError(
            "promotion-target-inspection-invalid",
            "Target installed-package content receipt is invalid",
        ) from error
    _validate_environment_content_receipt(environment_content)
    candidate_raw = raw.get("candidate")
    receipt = None
    if candidate_raw is not None:
        if not isinstance(candidate_raw, dict):
            raise CandidatePromotionExecutionError(
                "promotion-target-inspection-invalid", "Target receipt is invalid"
            )
        values = tuple(candidate_raw.get(key) for key in (
            "distribution", "version", "plugin_id", "entry_value", "content_sha256",
            "file_count", "size_bytes"
        ))
        distribution, version, plugin_id, entry_value, digest, count, size = values
        if (
            not isinstance(distribution, str)
            or not isinstance(version, str)
            or plugin_id != candidate.plugin_id
            or not isinstance(entry_value, str)
            or not isinstance(digest, str)
            or not _SHA256.fullmatch(digest)
            or isinstance(count, bool)
            or not isinstance(count, int)
            or isinstance(size, bool)
            or not isinstance(size, int)
        ):
            raise CandidatePromotionExecutionError(
                "promotion-target-inspection-invalid", "Target receipt is invalid"
            )
        receipt = TargetDistributionReceipt(*values)  # type: ignore[arg-type]
        _validate_target_receipt(
            receipt,
            candidate.plugin_id,
            canonicalize_name(candidate.distribution),
        )
    return _TargetFacts(
        implementation,
        python_version,
        python_prefix,
        raw["core_version"],
        distributions,
        environment_content,
        receipt,
    )


async def _doctor(
    runner: CommandRunner,
    python: Path,
    plugin_id: str,
    cwd: Path,
    timeout_seconds: float,
) -> None:
    await _run_required(
        runner,
        CommandRequest(
            "promotion-plugin-doctor",
            (
                str(python),
                "-m",
                "traceh.cli.main",
                "plugins",
                "doctor",
                plugin_id,
                "--json",
            ),
            cwd,
            _target_environment(),
            timeout_seconds,
        ),
        "promotion-plugin-doctor-failed",
    )


async def _run_required(runner: CommandRunner, request: CommandRequest, code: str) -> None:
    outcome = await runner.run(request)
    if outcome.start_failed or outcome.timed_out or outcome.exit_code != 0:
        raise CandidatePromotionExecutionError(code, "Target command failed")


async def _rollback_after_failure(
    runner: CommandRunner,
    target_python: Path,
    paths: _RegistryPaths,
    previous: _PromotionState,
    candidate: ComparisonCandidate,
    timeout_seconds: float,
    original: BaseException,
) -> None:
    rollback = asyncio.create_task(
        _restore_and_record(
            runner,
            target_python,
            paths,
            previous,
            candidate,
            timeout_seconds,
        )
    )
    try:
        await asyncio.shield(rollback)
    except asyncio.CancelledError:
        await await_worker_convergence(rollback)
    if rollback.cancelled():
        raise original
    cleanup_error = rollback.exception()
    if cleanup_error is not None:
        raise CandidatePromotionRollbackError(
            "promotion failed and rollback failed",
            (original, cleanup_error),
        )


async def _restore_and_record(
    runner: CommandRunner,
    target_python: Path,
    paths: _RegistryPaths,
    previous: _PromotionState,
    candidate: ComparisonCandidate,
    timeout_seconds: float,
) -> None:
    facts = await _restore_previous(
        runner,
        target_python,
        paths,
        previous.current_promotion_id,
        candidate,
        timeout_seconds,
    )
    if (
        facts.candidate != previous.current_receipt
        or facts.distributions != previous.environment_receipt
        or facts.environment_content != previous.environment_content
    ):
        raise CandidatePromotionExecutionError(
            "promotion-rollback-target-drift",
            "Rollback did not restore the previous managed target",
        )
    restored = _PromotionState(
        "stable",
        previous.target_python,
        previous.plugin_id,
        previous.distribution,
        previous.current_promotion_id,
        None,
        facts.candidate,
        facts.distributions,
        facts.environment_content,
    )
    _store_state(paths, restored)


async def _restore_previous(
    runner: CommandRunner,
    target_python: Path,
    paths: _RegistryPaths,
    previous_id: str | None,
    candidate: ComparisonCandidate,
    timeout_seconds: float,
) -> _TargetFacts:
    if previous_id is None:
        await _run_required(
            runner,
            CommandRequest(
                "promotion-rollback-uninstall",
                (
                    str(target_python), "-m", "pip", "uninstall", "-y",
                    canonicalize_name(candidate.distribution),
                ),
                target_python.parent,
                _target_environment(),
                timeout_seconds,
            ),
            "promotion-rollback-uninstall-failed",
        )
        facts = await _inspect_target(
            runner,
            target_python,
            candidate,
            timeout_seconds,
        )
        if facts.candidate is not None:
            raise CandidatePromotionExecutionError(
                "promotion-rollback-identity-mismatch", "Candidate remains installed"
            )
        return facts
    record = _load_record(paths, previous_id)
    artifact = paths.artifacts / record.artifact_sha256 / record.artifact_filename
    if not artifact.is_file() or sha256_file(artifact) != record.artifact_sha256:
        raise CandidatePromotionEvidenceError(
            "promotion-rollback-artifact-missing", "Exact rollback Wheel is unavailable"
        )
    await _run_required(
        runner,
        CommandRequest(
            "promotion-rollback-install",
            (
                str(target_python), "-m", "pip", "install",
                "--disable-pip-version-check", "--no-input", "--no-index", "--no-deps",
                "--no-compile", "--force-reinstall", str(artifact),
            ),
            artifact.parent,
            _target_environment(),
            timeout_seconds,
        ),
        "promotion-rollback-install-failed",
    )
    previous_candidate = ComparisonCandidate(
        record.distribution,
        record.version,
        record.plugin_id,
        record.entry_value,
        record.entry_value.partition(":")[0],
    )
    facts = await _inspect_target(
        runner,
        target_python,
        previous_candidate,
        timeout_seconds,
    )
    if facts.candidate is None or facts.candidate.version != record.version:
        raise CandidatePromotionExecutionError(
            "promotion-rollback-identity-mismatch", "Rollback identity does not match"
        )
    await _doctor(
        runner,
        target_python,
        record.plugin_id,
        artifact.parent,
        timeout_seconds,
    )
    facts = await _inspect_target(
        runner,
        target_python,
        previous_candidate,
        timeout_seconds,
    )
    expected = _load_receipt(paths, previous_id)
    if (
        expected.plugin_id != record.plugin_id
        or expected.distribution != record.distribution
        or expected.version != record.version
        or expected.entry_value != record.entry_value
    ):
        raise CandidatePromotionEvidenceError(
            "promotion-rollback-receipt-mismatch",
            "Rollback receipt does not belong to its promotion record",
        )
    if facts.candidate != expected:
        raise CandidatePromotionEvidenceError(
            "promotion-rollback-receipt-mismatch", "Rollback receipt changed"
        )
    if facts.distributions != record.environment_receipt:
        raise CandidatePromotionEvidenceError(
            "promotion-rollback-environment-mismatch",
            "Rollback Distribution environment changed",
        )
    if (
        record.environment_content is None
        or facts.environment_content != record.environment_content
    ):
        raise CandidatePromotionEvidenceError(
            "promotion-rollback-environment-mismatch",
            "Rollback installed-package content changed",
        )
    return facts


def _load_state_if_present(
    paths: _RegistryPaths,
    candidate: ComparisonCandidate,
    target_identity: str,
) -> _PromotionState | None:
    if not paths.state.exists():
        return None
    return _load_state_required(paths, candidate.plugin_id, target_identity)


def _load_state_required(
    paths: _RegistryPaths,
    plugin_id: str,
    target_identity: str,
) -> _PromotionState:
    raw = _read_json(paths.state, "promotion-state-invalid")
    if raw.pop("schema_version", None) != PROMOTION_SCHEMA_VERSION:
        raise CandidatePromotionEvidenceError(
            "promotion-state-invalid",
            "Promotion state schema is invalid",
        )
    try:
        receipt_raw = raw.get("current_receipt")
        receipt = TargetDistributionReceipt(**receipt_raw) if receipt_raw is not None else None
        environment_receipt = _managed_environment_receipt(
            raw["environment_receipt"],
            "promotion-state-invalid",
        )
        environment_content = TargetEnvironmentReceipt(**raw["environment_content"])
        state = _PromotionState(
            status=raw["status"],
            target_python=raw["target_python"],
            plugin_id=raw["plugin_id"],
            distribution=raw["distribution"],
            current_promotion_id=raw.get("current_promotion_id"),
            pending_promotion_id=raw.get("pending_promotion_id"),
            current_receipt=receipt,
            environment_receipt=environment_receipt,
            environment_content=environment_content,
        )
    except (KeyError, TypeError) as error:
        raise CandidatePromotionEvidenceError(
            "promotion-state-invalid", "Promotion state is invalid"
        ) from error
    if (
        state.status not in {"stable", "installing", "rollbacking"}
        or state.target_python != target_identity
        or state.plugin_id != plugin_id
        or not _SAFE_ID.fullmatch(state.plugin_id)
        or state.distribution != canonicalize_name(state.distribution)
        or any(
            value is not None and (not isinstance(value, str) or not _SHA256.fullmatch(value))
            for value in (state.current_promotion_id, state.pending_promotion_id)
        )
        or (state.status == "stable" and state.pending_promotion_id is not None)
        or (state.status == "installing" and state.pending_promotion_id is None)
        or (state.status == "rollbacking" and state.current_promotion_id is None)
        or (
            state.status == "stable"
            and (state.current_promotion_id is None) != (state.current_receipt is None)
        )
    ):
        raise CandidatePromotionEvidenceError(
            "promotion-state-invalid",
            "Promotion state is invalid",
        )
    if state.current_receipt is not None:
        _validate_target_receipt(state.current_receipt, plugin_id, state.distribution)
    _validate_environment_content_receipt(state.environment_content)
    return state


def _store_state(paths: _RegistryPaths, state: _PromotionState) -> None:
    paths.target.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": PROMOTION_SCHEMA_VERSION, **asdict(state)}
    _atomic_json(paths.state, payload)


def _store_record(paths: _RegistryPaths, record: _PromotionRecord) -> None:
    paths.records.mkdir(parents=True, exist_ok=True)
    path = _record_path(paths.records, record.promotion_id)
    payload = {"schema_version": PROMOTION_SCHEMA_VERSION, **asdict(record)}
    payload["environment_receipt"] = [
        asdict(item) for item in record.environment_receipt
    ]
    if path.exists():
        existing = _read_json(path, "promotion-record-invalid")
        if existing == payload:
            return
        existing_content = existing.get("environment_content")
        expected_without_content = dict(payload)
        expected_without_content["environment_content"] = existing_content
        if existing_content is None and expected_without_content == existing:
            _atomic_json(path, payload)
            return
        if existing != payload:
            raise CandidatePromotionEvidenceError(
                "promotion-record-collision", "Promotion record identity collided"
            )
    _atomic_json(path, payload)


def _load_record(paths: _RegistryPaths, promotion_id: str) -> _PromotionRecord:
    if not _SHA256.fullmatch(promotion_id):
        raise CandidatePromotionEvidenceError("promotion-record-invalid", "Promotion id is invalid")
    raw = _read_json(_record_path(paths.records, promotion_id), "promotion-record-invalid")
    if raw.pop("schema_version", None) != PROMOTION_SCHEMA_VERSION:
        raise CandidatePromotionEvidenceError(
            "promotion-record-invalid",
            "Promotion record schema is invalid",
        )
    try:
        raw["environment_receipt"] = _managed_environment_receipt(
            raw["environment_receipt"],
            "promotion-record-invalid",
        )
        content_raw = raw.get("environment_content")
        raw["environment_content"] = (
            TargetEnvironmentReceipt(**content_raw) if content_raw is not None else None
        )
        record = _PromotionRecord(**raw)
    except (KeyError, TypeError) as error:
        raise CandidatePromotionEvidenceError(
            "promotion-record-invalid", "Promotion record is invalid"
        ) from error
    if (
        record.promotion_id != promotion_id
        or not _SAFE_ID.fullmatch(record.plugin_id)
        or record.distribution != canonicalize_name(record.distribution)
        or not _SHA256.fullmatch(record.artifact_sha256)
        or PurePosixPath(record.artifact_filename).name != record.artifact_filename
        or "\\" in record.artifact_filename
        or any(
            not _SHA256.fullmatch(value)
            for value in (
                record.validation_report_sha256,
                record.comparison_report_sha256,
                record.approval_digest,
            )
        )
        or (
            record.previous_promotion_id is not None
            and not _SHA256.fullmatch(record.previous_promotion_id)
        )
    ):
        raise CandidatePromotionEvidenceError(
            "promotion-record-invalid",
            "Promotion record is invalid",
        )
    if record.environment_content is not None:
        _validate_environment_content_receipt(record.environment_content)
    return record


def _store_receipt(
    paths: _RegistryPaths,
    promotion_id: str,
    receipt: TargetDistributionReceipt,
) -> None:
    paths.receipts.mkdir(parents=True, exist_ok=True)
    _atomic_json(
        _record_path(paths.receipts, promotion_id),
        {"schema_version": PROMOTION_SCHEMA_VERSION, **asdict(receipt)},
    )


def _load_receipt(paths: _RegistryPaths, promotion_id: str) -> TargetDistributionReceipt:
    raw = _read_json(
        _record_path(paths.receipts, promotion_id),
        "promotion-receipt-invalid",
    )
    if raw.pop("schema_version", None) != PROMOTION_SCHEMA_VERSION:
        raise CandidatePromotionEvidenceError(
            "promotion-receipt-invalid",
            "Promotion receipt schema is invalid",
        )
    try:
        receipt = TargetDistributionReceipt(**raw)
    except TypeError as error:
        raise CandidatePromotionEvidenceError(
            "promotion-receipt-invalid", "Promotion receipt is invalid"
        ) from error
    _validate_target_receipt(receipt, receipt.plugin_id, receipt.distribution)
    return receipt


def _validate_target_receipt(
    receipt: TargetDistributionReceipt,
    plugin_id: str,
    distribution: str,
) -> None:
    if (
        receipt.distribution != canonicalize_name(distribution)
        or receipt.plugin_id != plugin_id
        or not receipt.version
        or len(receipt.version) > 256
        or not receipt.entry_value
        or len(receipt.entry_value) > 512
        or not _SHA256.fullmatch(receipt.content_sha256)
        or isinstance(receipt.file_count, bool)
        or receipt.file_count < 1
        or receipt.file_count > MAX_TARGET_RECEIPT_FILES
        or isinstance(receipt.size_bytes, bool)
        or receipt.size_bytes < 1
        or receipt.size_bytes > MAX_TARGET_RECEIPT_BYTES
    ):
        raise CandidatePromotionEvidenceError(
            "promotion-receipt-invalid",
            "Promotion receipt is invalid",
        )


def _validate_environment_content_receipt(receipt: TargetEnvironmentReceipt) -> None:
    if (
        not _SHA256.fullmatch(receipt.content_sha256)
        or isinstance(receipt.file_count, bool)
        or not 1 <= receipt.file_count <= MAX_TARGET_ENVIRONMENT_FILES
        or isinstance(receipt.size_bytes, bool)
        or not 1 <= receipt.size_bytes <= MAX_TARGET_ENVIRONMENT_BYTES
    ):
        raise CandidatePromotionEvidenceError(
            "promotion-environment-content-receipt-invalid",
            "Installed-package content receipt is invalid",
        )


def _store_artifact(paths: _RegistryPaths, evidence: ValidationEvidence) -> Path:
    directory = paths.artifacts / evidence.artifact.sha256
    destination = directory / evidence.artifact_source.name
    if destination.exists():
        if sha256_file(destination) != evidence.artifact.sha256:
            raise CandidatePromotionEvidenceError(
                "promotion-artifact-collision", "Stored artifact digest collided"
            )
        return destination
    directory.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".partial")
    try:
        _write_fsynced(temporary, evidence.artifact_content)
        verify_validation_artifact(temporary, evidence)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _read_json(path: Path, code: str) -> dict[str, Any]:
    try:
        content = path.read_bytes()
        raw = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CandidatePromotionEvidenceError(code, "Managed evidence is invalid") from error
    if len(content) > MAX_REPORT_BYTES or not isinstance(raw, dict):
        raise CandidatePromotionEvidenceError(code, "Managed evidence is invalid")
    return raw


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    content = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _write_fsynced(temporary, content)
    os.replace(temporary, path)


def _write_fsynced(path: Path, content: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _promotion_report(
    *,
    action: str,
    code: str,
    approval_digest: str,
    promotion_id: str | None,
    validation: ValidationEvidence,
    comparison: _ComparisonEvidence,
    target_python: Path,
    target_core_version: str,
    previous_promotion_id: str | None,
) -> PromotionReport:
    return PromotionReport(
        PROMOTION_SCHEMA_VERSION,
        action,
        True,
        datetime.now(UTC).isoformat(),
        code,
        approval_digest,
        promotion_id,
        validation.candidate.plugin_id,
        canonicalize_name(validation.candidate.distribution),
        validation.candidate.version,
        validation.artifact.sha256,
        validation.report_sha256,
        comparison.sha256,
        validation.core_commit,
        comparison.suite_id,
        comparison.suite_digest,
        str(target_python),
        target_core_version,
        comparison.classification,
        comparison.improvements,
        comparison.regressions,
        previous_promotion_id,
        "exact-wheel" if previous_promotion_id else "uninstall",
        _boundaries(),
    )


def _boundaries() -> tuple[str, ...]:
    return (
        "L4 只接受 improved 且零 regression 的宿主 L3 证据；人工批准不能覆盖已知回归。",
        "批准摘要绑定 L2/L3 字节、精确 Wheel、目标 Python 和当前托管状态；变化后必须重新审批。",
        "推广只安装已审计 SHA-256 对应的 Wheel，不重新构建候选，也不进入 AgentRuntime。",
        "首轮推广拒绝接管未由该 Registry 管理的同名 Distribution。",
        "回滚恢复上一份精确托管 Wheel；第一版回滚为卸载该 Distribution。",
        "这是同一用户权限下的 Python 环境事务，不是操作系统沙箱，也不能防御外部并发篡改目标环境。",
    )


def _commit_report_bundle(output: Path, report: PromotionReport) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    try:
        _atomic_json(staging / "report.json", report.to_dict())
        lines = [
            "# TraceHarness 插件候选 L4 审批与推广报告",
            "",
            f"动作：**{report.action}**",
            f"结果：**{report.code}**",
            f"候选：`{_markdown_code(report.plugin_id)}` / "
            f"`{_markdown_code(report.distribution)}=={_markdown_code(report.version)}`",
            f"Wheel SHA-256：`{report.artifact_sha256}`",
            f"L2 报告 SHA-256：`{report.validation_report_sha256 or '不适用'}`",
            f"L3 报告 SHA-256：`{report.comparison_report_sha256 or '不适用'}`",
            f"可信核心提交：`{report.core_commit or '回滚沿用已托管记录'}`",
            f"宿主任务集：`{report.suite_id or '不适用'}` / "
            f"`{report.suite_digest or '不适用'}`",
            f"目标 Python：`{_markdown_code(report.target_python)}`",
            f"L3 分类：**{report.classification}**",
            f"改进案例：`{len(report.improvements)}`；回归案例：`{len(report.regressions)}`",
            f"人工批准摘要：`{report.approval_digest or '不适用'}`",
            f"推广 ID：`{report.promotion_id or '尚未推广/已回到未安装状态'}`",
            f"回滚目标：`{report.rollback_kind}`",
            "",
            "## 人工检查",
            "",
            "- 确认插件名称、版本、目标解释器与预期一致。",
            "- 确认改进案例确实代表需要的能力，且回归数为 0。",
            "- 只有接受风险和边界后，才把本报告中的完整批准摘要交回 `--approve`。",
            "",
            "## 宿主证据",
            "",
            *(
                ["- 改进案例：" + "、".join(f"`{item}`" for item in report.improvements)]
                if report.improvements
                else ["- 改进案例：无"]
            ),
            *(
                ["- 回归案例：" + "、".join(f"`{item}`" for item in report.regressions)]
                if report.regressions
                else ["- 回归案例：无"]
            ),
            "",
            "## 边界",
            "",
            *(f"- {item}" for item in report.boundaries_zh),
        ]
        (staging / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(staging, output)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _markdown_code(value: str, *, limit: int = 500) -> str:
    output: list[str] = []
    for character in value:
        category = unicodedata.category(character)
        if character == "`":
            output.append("\\`")
        elif category.startswith("C"):
            output.append(f"\\u{ord(character):04x}")
        else:
            output.append(character)
        if sum(len(item) for item in output) >= limit:
            output.append("…")
            break
    return "".join(output)


def _target_environment() -> dict[str, str]:
    environment = sanitized_environment()
    environment.pop("PYTHONPATH", None)
    environment.update(
        {
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "TRACEH_CANDIDATE_PROMOTION": "1",
        }
    )
    environment.pop("PIP_FIND_LINKS", None)
    environment.pop("PIP_INDEX_URL", None)
    environment.pop("PIP_EXTRA_INDEX_URL", None)
    return environment


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _require_control_paths_outside_target(
    output: Path,
    registry: Path,
    facts: _TargetFacts,
) -> None:
    target_root = Path(facts.python_prefix).resolve()
    if _paths_overlap(output, target_root) or _paths_overlap(registry, target_root):
        raise CandidatePromotionConfigurationError(
            "Promotion output and Registry must be outside the target Python environment"
        )


_TARGET_INSPECTION_SCRIPT = r'''
import hashlib
import importlib.metadata as metadata
import json
import re
import sys
import sysconfig
from pathlib import Path

executable = Path(sys.executable).absolute()
venv_config = executable.parent.parent / "pyvenv.cfg"
if venv_config.is_file():
    target_prefix = venv_config.parent.resolve()
    target_paths = sysconfig.get_paths(
        scheme="venv",
        vars={"base": str(target_prefix), "platbase": str(target_prefix)},
    )
else:
    target_prefix = Path(sys.base_prefix).resolve()
    target_paths = sysconfig.get_paths(
        vars={"base": str(target_prefix), "platbase": str(target_prefix)}
    )
package_roots = []
for raw_root in (
    target_paths.get("purelib"),
    target_paths.get("platlib"),
):
    if raw_root:
        root = Path(raw_root).resolve()
        if venv_config.is_file() and not root.is_relative_to(target_prefix):
            raise SystemExit(15)
        if root not in package_roots:
            package_roots.append(root)
        rendered_root = str(root)
        if rendered_root not in sys.path:
            sys.path.append(rendered_root)

def canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()

(
    distribution_name,
    plugin_id,
    max_files_raw,
    max_bytes_raw,
    max_distributions_raw,
    max_environment_files_raw,
    max_environment_bytes_raw,
) = sys.argv[1:]
max_files = int(max_files_raw)
max_bytes = int(max_bytes_raw)
max_distributions = int(max_distributions_raw)
max_environment_files = int(max_environment_files_raw)
max_environment_bytes = int(max_environment_bytes_raw)
try:
    core_version = metadata.version("traceharness-py")
except metadata.PackageNotFoundError:
    raise SystemExit(10)
distributions = []
seen = set()
for installed in metadata.distributions():
    raw_installed_name = installed.metadata.get("Name")
    installed_version = installed.version
    if not isinstance(raw_installed_name, str) or not isinstance(installed_version, str):
        raise SystemExit(14)
    installed_name = canonical(raw_installed_name)
    if installed_name in seen:
        raise SystemExit(14)
    seen.add(installed_name)
    distributions.append({"name": installed_name, "version": installed_version})
    if len(distributions) > max_distributions:
        raise SystemExit(14)
distributions.sort(key=lambda item: item["name"])
environment_digest = hashlib.sha256()
environment_count = 0
environment_total = 0
for root_index, package_root in enumerate(package_roots):
    if not package_root.is_dir():
        raise SystemExit(15)
    for path in sorted(package_root.rglob("*"), key=lambda item: item.as_posix()):
        is_junction = getattr(path, "is_junction", lambda: False)
        if path.is_symlink() or is_junction():
            raise SystemExit(15)
        if "__pycache__" in path.relative_to(package_root).parts:
            continue
        if not path.is_file():
            continue
        content = path.read_bytes()
        environment_count += 1
        environment_total += len(content)
        if (
            environment_count > max_environment_files
            or environment_total > max_environment_bytes
        ):
            raise SystemExit(15)
        relative = f"{root_index}/{path.relative_to(package_root).as_posix()}".encode("utf-8")
        environment_digest.update(len(relative).to_bytes(8, "big"))
        environment_digest.update(relative)
        environment_digest.update(len(content).to_bytes(8, "big"))
        environment_digest.update(content)
if environment_count == 0:
    raise SystemExit(15)
try:
    dist = metadata.distribution(distribution_name)
except metadata.PackageNotFoundError:
    candidate = None
else:
    raw_name = dist.metadata.get("Name")
    version = dist.version
    entries = [
        entry for entry in dist.entry_points
        if entry.group == "traceh.plugins" and entry.name == plugin_id
    ]
    if (
        not isinstance(raw_name, str)
        or canonical(raw_name) != distribution_name
        or len(entries) != 1
    ):
        raise SystemExit(11)
    root = Path(dist.locate_file(".")).resolve()
    digest = hashlib.sha256()
    count = 0
    total = 0
    files = sorted((dist.files or ()), key=lambda value: str(value))
    for relative in files:
        path = Path(dist.locate_file(relative)).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            raise SystemExit(12)
        if not path.is_file():
            continue
        content = path.read_bytes()
        count += 1
        total += len(content)
        if count > max_files or total > max_bytes:
            raise SystemExit(13)
        name = str(relative).replace("\\", "/").encode("utf-8")
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    candidate = {
        "distribution": canonical(raw_name),
        "version": version,
        "plugin_id": plugin_id,
        "entry_value": entries[0].value,
        "content_sha256": digest.hexdigest(),
        "file_count": count,
        "size_bytes": total,
    }
print(json.dumps({
    "python_implementation": sys.implementation.name,
    "python_version": sys.version,
    "python_prefix": str(target_prefix),
    "core_version": core_version,
    "distributions": distributions,
    "environment_content": {
        "content_sha256": environment_digest.hexdigest(),
        "file_count": environment_count,
        "size_bytes": environment_total,
    },
    "candidate": candidate,
}, sort_keys=True))
'''


__all__ = [
    "CandidatePromoter",
    "CandidatePromotionConfig",
    "CandidatePromotionConfigurationError",
    "CandidatePromotionEvidenceError",
    "CandidatePromotionExecutionError",
    "CandidatePromotionRollbackError",
    "CandidateRollbackConfig",
    "CandidateRollbacker",
    "PROMOTION_EXIT_CODE",
    "PromotionReport",
    "TargetDistributionReceipt",
    "TargetEnvironmentReceipt",
]
