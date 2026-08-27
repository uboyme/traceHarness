"""One benchmark attempt: confirm, run, approve, measure, converge.

An attempt uses the *same* host control plane a person drives from
``traceh chat``: the ProductTask is opened by a real confirmation proven from a
real Session, the fixed Workflow runs, the frozen verifier produces a Review and
the host approves and promotes.  Nothing here is a benchmark-only shortcut past
a gate.

Two things are deliberately taken away from the model.

The **requirement and the mode come from the manifest**, not from a model's
Proposal.  The chat surface exists so a person can turn a conversation into a
task; a benchmark already knows the task, and letting a model author it would let
the candidate rewrite the question it is being scored on.

The **requester Session runs a host-frozen provider with no tools**.  It exists
only to produce the durable ``inbox/accepted`` -> ``inbox/claimed`` ->
``turn/start`` -> ``turn/end`` evidence that ``product/task-opened`` requires.  It
never sees the managed Workspace and its Session is not part of the measured
Agent subtree.

Every attempt owns a fresh event store, a fresh source repository, a fresh
one-shot bare target and its own directory.  Nothing is deleted on failure:
converging the owners is what makes an attempt clean, not removing the evidence
that it happened.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from traceh.api.llm import LlmProvider, ModelRequest, ModelResponse, Usage, UsageQuality
from traceh.api.product import ProductTaskStatus, RequestedTaskMode
from traceh.api.promotion import PromotionTargetBinding
from traceh.api.prompts import PromptSection
from traceh.api.turns import TurnInput
from traceh.artifacts.cas import LocalArtifactCas
from traceh.concurrency import combine_failures
from traceh.evaluation.errors import BenchmarkEvidenceError, BenchmarkExecutionError
from traceh.evaluation.manifest import (
    BENCHMARK_SOURCE_ID,
    BENCHMARK_TARGET_ID,
    BenchmarkManifest,
    BenchmarkTask,
)
from traceh.evaluation.metrics import collect_attempt_evidence
from traceh.evaluation.report import AttemptReport, PhaseTiming
from traceh.evaluation.repositories import (
    build_attempt_repositories,
    read_target_revision,
)
from traceh.product.control import ProductTaskControlPlane
from traceh.product.errors import ProductError, ProductStateError
from traceh.product.host import ProductChatHost, build_product_chat_host
from traceh.promotion.local_git import LocalBareGitPromotionTargets
from traceh.runtime.agent_runtime import (
    AgentRuntime,
    RuntimeConfig,
    build_default_runtime,
)
from traceh.runtime.prompt import PromptAssembler
from traceh.session.jsonl import JsonlEventStore
from traceh.workspaces.local_git import LocalGitWorkspaceProvider

REQUESTER_PROVIDER_ID = "traceh-benchmark-requester"
REQUESTER_MODEL_ID = "traceh-benchmark-requester-v1"
CONFIRMATION_TEXT = "Yes, start that task."

_REQUESTER_REPLY = (
    "Acknowledged. The benchmark host owns the requirement and the mode for this "
    "run."
)
_REQUESTER_PROMPT = PromptSection(
    "traceh.benchmark.requester",
    "You relay a benchmark requirement. You have no tools and make no decisions.",
    10,
)


class BenchmarkRequesterProvider(LlmProvider):
    """The host-frozen, tool-free relay that carries one requester Session.

    It reports zero exact tokens because it performs no model work.  Its Session
    is not part of the measured Agent subtree, so this number never enters the
    routing or execution totals; it is stated exactly rather than left unknown so
    the Budget contract has nothing to guess at.
    """

    name = REQUESTER_PROVIDER_ID

    async def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        return ModelResponse(
            content=_REQUESTER_REPLY,
            usage=Usage(0, 0, UsageQuality.EXACT),
        )


@dataclass(frozen=True, slots=True)
class AttemptRequest:
    """One (task, requested mode, repetition) cell of the benchmark grid.

    ``directory`` is short and numbered rather than descriptive.  A managed Git
    worktree is named by a 77-character derived identity and lives inside it, and
    a descriptive path plus that identity exceeds the Windows path limit from any
    ordinary output directory.  ``relative_directory`` is recorded in the report,
    so the readable ``attempt_id`` and the directory on disk stay connected.
    """

    attempt_id: str
    task: BenchmarkTask
    requested_mode: RequestedTaskMode
    repetition: int
    directory: Path
    relative_directory: str


async def run_attempt(
    request: AttemptRequest,
    *,
    manifest: BenchmarkManifest,
    providers: Mapping[str, LlmProvider],
    monotonic: Callable[[], float] = time.monotonic,
) -> AttemptReport:
    """Execute one attempt and return what its durable facts support."""

    settings = manifest.settings
    request.directory.mkdir(parents=True, exist_ok=False)
    repositories = await build_attempt_repositories(
        initial_dir=request.task.initial_dir,
        source=request.directory / "source",
        target=request.directory / "tgt.git",
    )
    store = JsonlEventStore(request.directory / "ev")
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=request.directory / "rt",
            provider=REQUESTER_PROVIDER_ID,
            model=REQUESTER_MODEL_ID,
            max_steps=1,
        ),
        provider=BenchmarkRequesterProvider(),
        event_store=store,
        include_default_tools=False,
        prompt=PromptAssembler((_REQUESTER_PROMPT,)),
        policies=(),
    )
    try:
        host = await build_product_chat_host(
            store=runtime.sessions.store,
            data_dir=request.directory / "pd",
            host_profile=settings.host_profile,
            providers=providers,
            workspace_provider=LocalGitWorkspaceProvider(
                managed_root=request.directory / "work",
                sources={BENCHMARK_SOURCE_ID: repositories.source},
            ),
            artifact_cas=LocalArtifactCas(request.directory / "cas"),
            promotion_targets=LocalBareGitPromotionTargets(
                targets={
                    BENCHMARK_TARGET_ID: PromotionTargetBinding(
                        repository_path=repositories.target,
                        target_ref=repositories.target_ref,
                    )
                }
            ),
            capture_limits=settings.capture_limits,
            approver_id=settings.approver_id,
            max_report_chars=settings.max_report_chars,
        )
    except BaseException:
        # The Runtime exists from here on and owns a shutdown Task. Host assembly
        # can still fail - an unresolvable Profile, an unusable CAS root - and
        # leaving it undisposed would leak that owner for the rest of the grid.
        # This mirrors what ``cli/main._chat`` does at the same seam.
        await runtime.dispose()
        raise
    control = host.control
    task_id: str | None = None
    error_code: str | None = None
    timing: PhaseTiming | None = None
    interrupted: BaseException | None = None
    try:
        prepared = await _prepare(control, runtime=runtime, request=request)
        task_id = prepared.task_id
        timing = await _advance(
            control,
            prepared=prepared,
            approver_id=settings.approver_id,
            monotonic=monotonic,
        )
    except Exception as error:
        # An ordinary failure is an attempt outcome, not a reason to abandon the
        # run. Its owners are still converged below and whatever the ProductTask
        # stream does contain is still measured.
        error_code = _error_code(error)
    except BaseException as error:
        # Cancellation converges the same owners before it is re-raised, so a
        # caller that interrupts the benchmark does not leave a live Activation,
        # a held process slot or an unreleased worktree behind.
        interrupted = error
    finally:
        settle_code = (
            None if task_id is None else await _settle(control, task_id)
        )
        error_code = error_code or settle_code
        try:
            await _close(host, runtime)
        except BaseException as close_error:
            combined = combine_failures(
                interrupted, close_error, "benchmark attempt shutdown failed"
            )
            assert combined is not None
            raise combined from None
    if interrupted is not None:
        raise interrupted

    target_revision = await read_target_revision(
        repositories.target, repositories.target_ref
    )
    if task_id is None:
        return AttemptReport(
            attempt_id=request.attempt_id,
            benchmark_task_id=request.task.task_id,
            requested_mode=request.requested_mode,
            repetition=request.repetition,
            directory=request.relative_directory,
            error_code=error_code or "benchmark-attempt-not-opened",
            evidence=None,
            timing=timing,
        )
    evidence = await collect_attempt_evidence(
        store,
        task_id=task_id,
        promotion_target_id=BENCHMARK_TARGET_ID,
        target_ref=repositories.target_ref,
        target_revision=target_revision,
    )
    if (
        evidence.source_base_revision is not None
        and evidence.source_base_revision != repositories.base_revision
    ):
        # The task recorded a base revision other than the one this attempt
        # created. Reporting it would attribute the run to a source that was
        # never used.
        raise BenchmarkEvidenceError(
            "benchmark-source-revision-mismatch", request.attempt_id
        )
    return AttemptReport(
        attempt_id=request.attempt_id,
        benchmark_task_id=request.task.task_id,
        requested_mode=request.requested_mode,
        repetition=request.repetition,
        directory=request.relative_directory,
        error_code=error_code,
        evidence=evidence,
        timing=timing,
    )


@dataclass(frozen=True, slots=True)
class _PreparedConfirmation:
    """The durable Session evidence one confirmation will be proven against."""

    task_id: str
    session_id: str
    confirming_turn_id: str
    confirming_message_id: str


async def _prepare(
    control: ProductTaskControlPlane,
    *,
    runtime: AgentRuntime,
    request: AttemptRequest,
) -> _PreparedConfirmation:
    """Run the two real user Turns a ``product/task-opened`` requires.

    The task identity is known once the Proposal exists, before anything
    fallible starts, which is what lets a failed confirmation still be measured
    against the stream it opened.
    """

    workspace = request.directory / "rw"
    workspace.mkdir(parents=True, exist_ok=False)
    session_id = await runtime.create_session(
        workspace, metadata={"benchmark_attempt": request.attempt_id}
    )
    origin = TurnInput(
        content=request.task.requirement, message_id=str(uuid4()), source="user"
    )
    origin_result = await runtime.run_existing(session_id, origin)
    pending = await control.offer(
        session_id=session_id,
        origin_turn_id=origin_result.turn_id,
        origin_message_id=origin.message_id,
        proposed_turn_id=origin_result.turn_id,
        requirement=request.task.requirement,
        requested_mode=request.requested_mode,
    )
    confirmation = TurnInput(
        content=CONFIRMATION_TEXT, message_id=str(uuid4()), source="user"
    )
    confirmation_result = await runtime.run_existing(session_id, confirmation)
    return _PreparedConfirmation(
        task_id=pending.task_id,
        session_id=session_id,
        confirming_turn_id=confirmation_result.turn_id,
        confirming_message_id=confirmation.message_id,
    )


async def _advance(
    control: ProductTaskControlPlane,
    *,
    prepared: _PreparedConfirmation,
    approver_id: str,
    monotonic: Callable[[], float],
) -> PhaseTiming:
    """Confirm, run to the barrier, approve immediately and time the phases."""

    started_at = monotonic()
    advance = await control.confirm(
        session_id=prepared.session_id,
        confirming_turn_id=prepared.confirming_turn_id,
        confirming_message_id=prepared.confirming_message_id,
    )
    barrier_at = monotonic()
    decided_at = barrier_at
    if advance.summary.status is ProductTaskStatus.AWAITING_APPROVAL:
        # The host decides here, and this is the only interval allowed to look
        # like waiting. Everything after it is work again.
        decided_at = monotonic()
        await control.approve(prepared.task_id, approver_id=approver_id)
    terminal_at = monotonic()
    return PhaseTiming(
        wall_ms=_milliseconds(started_at, terminal_at),
        approval_wait_ms=_milliseconds(barrier_at, decided_at),
    )


async def _settle(control: ProductTaskControlPlane, task_id: str) -> str | None:
    """Converge a task the attempt did not carry to a durable end.

    A settled task is left alone; the durable stream is already the answer. A
    non-terminal one is cancelled through the existing owner so the Budget
    accounts and the managed worktree converge before the report claims the
    attempt is over.
    """

    try:
        await control.cancel(task_id)
    except ProductStateError as error:
        if error.code == "product-task-unknown":
            # ``offer()`` produced the identity but ``open_task`` never ran, so
            # no owner, account or worktree was ever allocated for it.
            return None
        return error.code
    except ProductError as error:
        return _error_code(error)
    except Exception:
        return "benchmark-attempt-settle-failed"
    return None


async def _close(host: ProductChatHost, runtime: AgentRuntime) -> None:
    primary: BaseException | None = None
    try:
        await host.aclose()
    except BaseException as error:
        primary = error
    try:
        await runtime.dispose()
    except BaseException as error:
        combined = combine_failures(
            primary, error, "benchmark attempt shutdown failed"
        )
        assert combined is not None
        raise combined from None
    if primary is not None:
        raise primary


def _error_code(error: BaseException) -> str:
    code = getattr(error, "code", None)
    if type(code) is str and code:
        return code
    if isinstance(error, BenchmarkExecutionError | BenchmarkEvidenceError):
        return error.code
    return "benchmark-attempt-failed"


def _milliseconds(start: float, end: float) -> int:
    elapsed = end - start
    if elapsed < 0:
        # A monotonic clock cannot go backwards; a supplied one that does is a
        # broken instrument, not a negative duration.
        raise BenchmarkExecutionError("benchmark-clock-not-monotonic")
    return int(elapsed * 1000)


__all__ = [
    "CONFIRMATION_TEXT",
    "REQUESTER_MODEL_ID",
    "REQUESTER_PROVIDER_ID",
    "AttemptRequest",
    "BenchmarkRequesterProvider",
    "run_attempt",
]
