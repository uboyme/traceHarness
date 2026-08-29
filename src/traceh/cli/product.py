"""Line-terminal adapter for the UI-neutral Product Chat coordination."""

from __future__ import annotations

import asyncio
from pathlib import Path

from traceh.api.product import (
    ProductTaskStatus,
    ProductTaskView,
    RequestedTaskMode,
)
from traceh.chat.activity import Clock, default_clock
from traceh.cli.command_line import (
    Literal,
    UnsafeCommandValue,
    escape_for_display,
    render_command,
)
from traceh.cli.console import Console, contains_undecodable_input, normalize_input
from traceh.concurrency import await_worker_convergence
from traceh.product.chat import (
    ProductChatTurn,
    ProductCommandResult,
    ProductInspectionResult,
    ProductStartRequest,
    parse_product_command,
)
from traceh.product.control import PendingProductProposal, ProductAdvanceResult, ProductInspection
from traceh.product.errors import ProductInputError
from traceh.product.host import ProductChatHost
from traceh.product.inspection import ProductTaskEvidence
from traceh.product.observation import ProductObservation, ProductObservationSession


class LineProductAdapter:
    """Render Product typed values and collect the exact terminal START gesture."""

    __slots__ = ("_data_dir", "_host")

    def __init__(self, host: ProductChatHost, *, data_dir: Path) -> None:
        self._host = host
        self._data_dir = Path(data_dir).absolute()

    @property
    def control(self):
        return self._host.control

    @property
    def observation(self):
        return self._host.observation

    async def prepare_turn(self, session_id: str, text: str) -> ProductChatTurn:
        return await self._host.prepare_turn(session_id, text)

    async def finish_turn(
        self,
        session_id: str,
        prepared: ProductChatTurn,
        *,
        turn_id: str,
        console: Console,
        heartbeat_seconds: float = 0.0,
        clock: Clock | None = None,
    ) -> None:
        try:
            resolution = await self._host.resolve_turn(
                session_id, prepared, turn_id=turn_id
            )
            if resolution.proposal is not None:
                _render_proposal(console, resolution.proposal)
                return
            if resolution.notice_code is not None:
                console.write("task confirmation ignored: no proposal was pending")
                return
            request = resolution.start_request
            if request is None or not _explicit_start_authorized(console, request):
                return
            await self._start(
                request,
                console,
                heartbeat_seconds=heartbeat_seconds,
                clock=clock or default_clock(),
            )
        except Exception as error:
            _render_operation_failure(console, error)

    async def _start(
        self,
        request: ProductStartRequest,
        console: Console,
        *,
        heartbeat_seconds: float,
        clock: Clock,
    ) -> None:
        pending = request.pending
        _render_execution_started(console, pending)
        observer = self._host.observe(pending.task_id)
        observation_started = False
        heartbeat: asyncio.Task[None] | None = None
        result: ProductAdvanceResult | None = None
        primary: Exception | None = None
        try:
            await observer.start()
            observation_started = True
            if heartbeat_seconds > 0:
                heartbeat = asyncio.create_task(
                    _emit_product_observation(
                        console,
                        observer,
                        interval_seconds=heartbeat_seconds,
                        clock=clock,
                    ),
                    name=f"traceh-product-heartbeat-{pending.task_id}",
                )
            result = await self._host.start(request)
        except Exception as error:
            primary = error
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                await await_worker_convergence(heartbeat)
            try:
                if observation_started:
                    try:
                        _render_product_observation(console, await observer.refresh())
                    except Exception as error:
                        _render_observation_failure(console, pending.task_id, error)
            finally:
                await observer.aclose()
        if primary is not None:
            _render_operation_failure(console, primary)
            return
        assert result is not None
        if result.summary.status is ProductTaskStatus.AWAITING_APPROVAL:
            _render_inspection_result(
                console,
                await self._host.inspect(result.summary.task_id),
                data_dir=self._data_dir,
            )
        else:
            _render_advance(console, result)

    async def discard_turn(self, *args, **kwargs) -> None:
        await self._host.discard_turn(*args, **kwargs)

    async def handle_command(self, text: str, console: Console) -> bool:
        try:
            command = parse_product_command(text)
        except ProductInputError:
            console.write(
                "usage: /task inspect|approve|reject|cancel|abandon TASK_ID"
            )
            return True
        if command is None:
            return False
        task_id = command.task_id
        await self._render_observation(console, task_id)
        try:
            result = await self._host.execute_command(command)
        except Exception as error:
            _render_operation_failure(console, error)
            return True
        _render_command_result(console, result, data_dir=self._data_dir)
        await self._render_observation(console, task_id)
        return True

    async def _render_observation(self, console: Console, task_id: str) -> None:
        try:
            observation = await self._host.observation.load(task_id)
        except Exception as error:
            _render_observation_failure(console, task_id, error)
            return
        _render_product_observation(console, observation)

    async def aclose(self) -> None:
        await self._host.aclose()


def _render_proposal(console: Console, pending: PendingProductProposal) -> None:
    proposal = pending.proposal
    binding = proposal.preflight
    console.write("task proposal (not started):")
    console.write(f"  proposal: {proposal.proposal_id}")
    console.write(f"  task if confirmed: {pending.task_id}")
    console.write(f"  requirement: {escape_for_display(pending.requirement)}")
    console.write(f"  profile:  {pending.profile_id}")
    console.write(f"  mode:     {proposal.requested_mode.value}")
    console.write(f"  mode source: {proposal.mode_source.value}")
    console.write(f"  source:   {binding.base_revision}")
    console.write(
        f"  target:   {binding.promotion_target_ref} "
        f"at {binding.promotion_expected_revision}"
    )
    console.write("  safety: fixed Workflow, host verification, human approval")
    console.write(
        "Reply naturally in a later message. If acceptance is detected, the host "
        "will require exact START authorization before any task begins."
    )


def _explicit_start_authorized(
    console: Console, request: ProductStartRequest
) -> bool:
    task_id = request.pending.task_id
    try:
        answer = console.read_line(
            f"Start exact ProductTask {task_id}? Type START to authorize: "
        )
    except EOFError:
        console.write(f"task {task_id}: start not authorized (input ended)")
        return False
    if contains_undecodable_input(answer) or normalize_input(answer) != "START":
        console.write(f"task {task_id}: start not authorized")
        return False
    console.write(f"task {task_id}: explicit START authorized by host user")
    return True


def _render_command_result(
    console: Console, result: ProductCommandResult, *, data_dir: Path
) -> None:
    if result.inspection is not None:
        _render_inspection_result(console, result.inspection, data_dir=data_dir)
        return
    assert result.advance is not None
    _render_advance(console, result.advance)


def _render_inspection_result(
    console: Console, result: ProductInspectionResult, *, data_dir: Path
) -> None:
    _render_inspection(
        console,
        result.inspection,
        evidence=result.evidence,
        evidence_error=result.evidence_error,
        data_dir=data_dir,
    )


def _render_inspection(
    console: Console,
    inspection: ProductInspection,
    *,
    evidence: ProductTaskEvidence | None,
    evidence_error: str | None,
    data_dir: Path,
) -> None:
    summary = inspection.view.summary
    console.write(f"task {summary.task_id}: {inspection.view.status.value}")
    console.write(f"  requested mode: {summary.requested_mode.value}")
    console.write(f"  mode source: {summary.mode_source.value}")
    if summary.resolved_mode is not None:
        console.write(f"  resolved mode: {summary.resolved_mode.value}")
    if inspection.view.workflow_status is not None:
        console.write(f"  workflow: {inspection.view.workflow_status.value}")
    if evidence_error is not None:
        console.write(f"  evidence: unavailable ({evidence_error})")
        console.write("  do not approve until the durable evidence can be read")
    elif evidence is not None:
        _render_evidence(console, evidence, data_dir=data_dir)
    if inspection.review is not None:
        _render_review(console, inspection)
        if evidence_error is None and evidence is not None:
            console.write(
                "  decision: /task approve TASK_ID or /task reject TASK_ID "
                "after reviewing the evidence above"
            )
        else:
            console.write(
                "  decision: do not approve; retry inspection or reject the task"
            )


def _render_review(console: Console, inspection: ProductInspection) -> None:
    review = inspection.review
    assert review is not None
    console.write(f"  review: {review.review_id}")
    console.write(f"  patch_sha256: {review.patch_sha256}")
    console.write(f"  target: {review.target_ref} at {review.expected_revision}")
    console.write(f"  integration_commit: {review.integration_commit}")
    console.write(f"  approval_digest: {inspection.approval_digest}")


def _render_evidence(
    console: Console, evidence: ProductTaskEvidence, *, data_dir: Path
) -> None:
    if evidence.nodes:
        console.write("  workflow nodes:")
    for node in evidence.nodes:
        line = f"    {node.node_id}: {node.status} ({node.kind})"
        if node.failure_code is not None:
            line += f" failure={node.failure_code}"
        console.write(line)
        if node.agent_id is not None:
            console.write(f"      agent: {node.agent_id}")
        if node.session_id is not None:
            console.write(f"      session: {node.session_id}")
            try:
                replay = render_command(
                    (
                        Literal("traceh"),
                        Literal("replay"),
                        node.session_id,
                        Literal("--data-dir"),
                        str(data_dir),
                    )
                )
            except UnsafeCommandValue:
                replay = "unavailable: a value cannot be rendered as one safe command"
            console.write(f"      replay: {replay}")
    review = evidence.review
    if review is None:
        return
    console.write(f"  changed paths ({len(review.changed_paths)})")
    for path in review.changed_paths:
        console.write(f"    {escape_for_display(path, limit=500)}")
    console.write("  verification:")
    for verifier in review.verifiers:
        exit_code = "unavailable" if verifier.exit_code is None else str(verifier.exit_code)
        console.write(f"    {verifier.command_id}: {verifier.status} exit={exit_code}")
        executable = escape_for_display(verifier.executable, limit=500)
        console.write(
            f"      command: {executable} ({verifier.argument_count} arguments; "
            f"argv_sha256={verifier.argv_digest})"
        )
    suffix = "truncated" if review.patch_preview_truncated else "complete"
    console.write(f"  patch preview ({review.patch_size_bytes} bytes, {suffix})")
    for line in review.patch_preview.split("\n"):
        console.write(f"    {line}")
    if review.patch_utf8_replaced:
        console.write(
            "    note: non-UTF-8 Patch bytes are shown with replacement characters"
        )


def _render_execution_started(
    console: Console, pending: PendingProductProposal
) -> None:
    console.write(f"task {pending.task_id}: confirmation accepted; starting execution")
    console.write(f"  requested mode: {pending.proposal.requested_mode.value}")
    if pending.proposal.requested_mode is RequestedTaskMode.AUTO:
        console.write("  resolved mode: pending Router decision")


async def _emit_product_observation(
    console: Console,
    observer: ProductObservationSession,
    *,
    interval_seconds: float,
    clock: Clock,
) -> None:
    started = clock.monotonic()
    while True:
        dirty = asyncio.create_task(observer.wait_dirty())
        periodic = asyncio.create_task(clock.sleep(interval_seconds))
        try:
            done, _pending = await asyncio.wait(
                (dirty, periodic), return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in (dirty, periodic):
                if not task.done():
                    task.cancel()
                    await await_worker_convergence(task)
        for task in done:
            await task
        try:
            observation = await observer.refresh()
        except Exception:
            continue
        _render_product_observation(
            console,
            observation,
            waiting_seconds=max(0.0, clock.monotonic() - started),
        )


def _render_product_observation(
    console: Console,
    observation: ProductObservation,
    *,
    waiting_seconds: float | None = None,
) -> None:
    summary = observation.summary
    if summary is None:
        return
    mode = "pending" if summary.resolved_mode is None else summary.resolved_mode.value
    workflow = (
        "not-started"
        if observation.workflow_status is None
        else observation.workflow_status.value
    )
    suffix = "; streams=unreconciled" if observation.streams_diverged else ""
    prefix = (
        "" if waiting_seconds is None else f"[waiting {_format_seconds(waiting_seconds)}] "
    )
    console.write(
        f"{prefix}task {summary.task_id}: {summary.status.value}; "
        f"workflow={workflow}; mode={mode}{suffix}"
    )


def _render_advance(console: Console, result: ProductAdvanceResult) -> None:
    console.write(f"task {result.summary.task_id}: {result.summary.status.value}")
    if result.summary.failure_code is not None:
        console.write(f"  failure: {result.summary.failure_code}")
    if result.review is not None:
        _render_review(
            console,
            ProductInspection(
                view=_view_for_result(result),
                review=result.review,
                approval_digest=result.approval_digest,
            ),
        )
    if result.summary.status is ProductTaskStatus.COMPLETED:
        console.write(f"  promotion: {result.summary.promotion_id}")


def _render_observation_failure(
    console: Console, task_id: str, error: Exception
) -> None:
    code = getattr(error, "code", None)
    if type(code) is not str or not code:
        code = "product-observation-unavailable"
    console.write(f"task {task_id}: observation unavailable ({code})")


def _render_operation_failure(console: Console, error: Exception) -> None:
    code = getattr(error, "code", None)
    if type(code) is not str or not code:
        code = "product-execution-failed"
    task_id = getattr(error, "task_id", None)
    if type(task_id) is str and task_id:
        console.write(f"task {escape_for_display(task_id)} operation failed: {code}")
    else:
        console.write(f"task operation failed: {code}")


def _view_for_result(result: ProductAdvanceResult) -> ProductTaskView:
    status = None if result.workflow is None else result.workflow.status
    return ProductTaskView(
        summary=result.summary,
        workflow_status=status,
        owned_by_this_host=True,
    )


def _format_seconds(value: float) -> str:
    return f"{int(value)}s" if value.is_integer() else f"{value:.1f}s"


__all__ = ["LineProductAdapter"]
