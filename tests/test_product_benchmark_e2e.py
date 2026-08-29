"""F4's real local boundary: `traceh eval` measured from durable facts only.

Every run here uses a deterministic in-process provider, real local Git, a real
frozen verifier subprocess and a one-shot bare target. Nothing reads `.env` and
nothing calls an external model or a real remote.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import traceh.evaluation.attempt as attempt_module
from traceh.api.events import PendingEvent
from traceh.api.llm import ModelRequest, ModelResponse, ToolCall, Usage, UsageQuality
from traceh.api.product import (
    ProductTaskStatus,
    ResolvedTaskMode,
)
from traceh.api.promotion import VerifierOutcome
from traceh.api.workflow import WorkflowStatus
from traceh.evaluation.attempt import REQUESTER_PROVIDER_ID
from traceh.evaluation.errors import BenchmarkEvidenceError, BenchmarkExecutionError
from traceh.evaluation.manifest import BENCHMARK_TARGET_ID
from traceh.evaluation.metrics import collect_attempt_evidence
from traceh.evaluation.report import APPROVAL_POLICY
from traceh.evaluation.repositories import read_target_revision
from traceh.evaluation.runner import ProductBenchmarkRunner
from traceh.promotion.models import (
    expected_approval_digest,
    promotion_identity,
    verification_evidence_digest,
)
from traceh.promotion.projection import PromotionLedgerReader
from traceh.session.sqlite import DATABASE_FILENAME, SqliteEventStore

PRODUCT_PROVIDER_ID = "benchmark-test-provider"
PRODUCT_MODEL_ID = "benchmark-test-model"

_VERIFIER_ARGV = (
    sys.executable,
    "-c",
    "import pathlib,sys;sys.exit(0 if "
    "pathlib.Path('added.txt').read_text() == 'added\\n' else 1)",
)


def _read_stream_records(root: Path, stream_id: str) -> list[dict]:
    connection = sqlite3.connect(root / DATABASE_FILENAME)
    try:
        rows = connection.execute(
            "SELECT envelope_json FROM events WHERE stream_id = ? ORDER BY seq",
            (stream_id,),
        ).fetchall()
        return [json.loads(row[0]) for row in rows]
    finally:
        connection.close()


def _write_stream_records(root: Path, stream_id: str, records: list[dict]) -> None:
    connection = sqlite3.connect(root / DATABASE_FILENAME, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for record in records:
            assert record["stream_id"] == stream_id
            connection.execute(
                "UPDATE events SET envelope_json = ? WHERE stream_id = ? AND seq = ?",
                (
                    json.dumps(
                        record,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    stream_id,
                    record["seq"],
                ),
            )
        connection.execute("COMMIT")
    finally:
        connection.close()


def _replace_stream_value(
    root: Path,
    stream_id: str,
    old: str,
    new: str,
    *,
    required: bool = True,
) -> None:
    records = _read_stream_records(root, stream_id)
    original = json.dumps(records, ensure_ascii=False, sort_keys=True)
    if old not in original:
        assert not required
        return
    rewritten = json.loads(original.replace(old, new))
    _write_stream_records(root, stream_id, rewritten)


class _ProductProvider:
    """One deterministic candidate: analyse, then write the one required file."""

    name = PRODUCT_PROVIDER_ID

    def __init__(
        self,
        requests: list[ModelRequest] | None = None,
        *,
        route_to: ResolvedTaskMode = ResolvedTaskMode.MULTI,
    ) -> None:
        self.requests = requests
        self.route_to = route_to

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if self.requests is not None:
            self.requests.append(request)
        if request.system_prompt and "routing classifier" in request.system_prompt:
            return _response(
                json.dumps({"mode": self.route_to.value, "reason": "fixed test route"})
            )
        tool_names = {tool.name for tool in request.tools}
        if "apply_patch" not in tool_names:
            return _response("bounded analysis")
        if not any(message.role == "tool" for message in request.messages):
            return _response(
                "",
                ToolCall(
                    id="create-file",
                    name="apply_patch",
                    arguments={
                        "path": "added.txt",
                        "old_text": "",
                        "new_text": "added\n",
                        "create": True,
                    },
                ),
            )
        return _response("implemented and checked")


class _UnparsableRouterProvider(_ProductProvider):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.system_prompt and "routing classifier" in request.system_prompt:
            return _response("this is not one JSON object")
        return await super().complete(request)


class _UntrustworthyRouterUsageProvider(_ProductProvider):
    """Routes correctly, but will not stand behind the routing token count."""

    async def complete(self, request: ModelRequest) -> ModelResponse:
        response = await super().complete(request)
        if request.system_prompt and "routing classifier" in request.system_prompt:
            return ModelResponse(
                content=response.content,
                tool_calls=response.tool_calls,
                usage=Usage(0, 0, UsageQuality.UNKNOWN),
            )
        return response


class _FailAfterWritingProvider(_ProductProvider):
    """Writes the file for real, then crashes on the next model call."""

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if "apply_patch" in {tool.name for tool in request.tools} and any(
            message.role == "tool" for message in request.messages
        ):
            raise RuntimeError("candidate stopped responding")
        return await super().complete(request)


class _WrongContentProvider(_ProductProvider):
    """Leaves a dirty worktree the frozen verifier will reject."""

    async def complete(self, request: ModelRequest) -> ModelResponse:
        response = await super().complete(request)
        return ModelResponse(
            content=response.content,
            tool_calls=tuple(
                ToolCall(
                    id=call.id,
                    name=call.name,
                    arguments={**call.arguments, "new_text": "not what was asked\n"},
                )
                if call.name == "apply_patch"
                else call
                for call in response.tool_calls
            ),
            usage=response.usage,
        )


class _FailingParentProvider(_ProductProvider):
    """Fails the read-only parent role, which only ``multi`` runs."""

    async def complete(self, request: ModelRequest) -> ModelResponse:
        tool_names = {tool.name for tool in request.tools}
        routing = request.system_prompt and "routing classifier" in request.system_prompt
        if not routing and "apply_patch" not in tool_names and tool_names:
            raise RuntimeError("read-only role stopped responding")
        return await super().complete(request)


class _StalledCoderProvider(_ProductProvider):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if "apply_patch" in {tool.name for tool in request.tools}:
            self.entered.set()
            await asyncio.Event().wait()
        return await super().complete(request)


def _response(content: str, *calls: ToolCall) -> ModelResponse:
    return ModelResponse(
        content=content,
        tool_calls=tuple(calls),
        usage=Usage(11, 7, UsageQuality.EXACT),
    )


def _budget(**changes: int | None) -> dict[str, int | None]:
    values: dict[str, int | None] = {
        "max_tokens": 40_000,
        "max_steps": 8,
        "max_tool_calls": 20,
        "max_wall_milliseconds": 300_000,
        "max_children": 0,
        "max_depth": 0,
        "max_processes": 4,
    }
    values.update(changes)
    return values


def _role(preset: str, grants: tuple[str, ...]) -> dict[str, object]:
    return {
        "preset": preset,
        "capability_grants": list(grants),
        "max_output_tokens": 4_096,
        "budget": _budget(),
    }


def build_benchmark(
    root: Path,
    *,
    arms: tuple[tuple[str, int], ...],
    tasks: tuple[str, ...] = ("write_expected_file",),
) -> Path:
    """Write a schema-1 manifest whose verifier is a real local subprocess."""

    root.mkdir(parents=True, exist_ok=True)
    for task_id in tasks:
        initial = root / task_id / "initial"
        initial.mkdir(parents=True, exist_ok=True)
        (initial / "kept.txt").write_text("kept\n", encoding="utf-8")
    manifest = {
        "protocol_version": 1,
        "benchmark_id": "benchmark-under-test",
        "profile_id": "benchmark-profile",
        "approver_id": "benchmark-host",
        "default_mode": "single",
        "roles": {
            "parent": _role(
                "bench-parent", ("list_files", "read_file", "search_text")
            ),
            "reviewer": _role(
                "bench-reviewer", ("list_files", "read_file", "search_text")
            ),
            "coder": _role(
                "bench-coder",
                ("list_files", "read_file", "search_text", "apply_patch", "shell"),
            ),
        },
        "router": {
            "preset": "bench-router",
            "max_output_tokens": 256,
            "budget": _budget(max_steps=2, max_tool_calls=0),
            "timeout_milliseconds": 60_000,
            "max_response_bytes": 2_048,
        },
        "task_budget": _budget(
            max_tokens=400_000,
            max_steps=120,
            max_tool_calls=200,
            max_wall_milliseconds=1_800_000,
            max_children=8,
            max_depth=2,
            max_processes=8,
        ),
        "verification": {
            "plan_id": "benchmark-plan",
            "plan_version": 1,
            "commands": [
                {
                    "command_id": "expected-file",
                    "argv": list(_VERIFIER_ARGV),
                    "timeout_ms": 120_000,
                }
            ],
            "environment": {
                "policy_id": "benchmark-env",
                "passthrough": [
                    "PATH",
                    "PATHEXT",
                    "SYSTEMROOT",
                    "SYSTEMDRIVE",
                    "WINDIR",
                    "COMSPEC",
                    "TEMP",
                    "TMP",
                    "TMPDIR",
                    "HOME",
                    "LANG",
                    "LC_ALL",
                ],
                "overrides": {"PYTHONIOENCODING": "utf-8"},
            },
            "max_output_bytes": 1_048_576,
            "protocol_version": 1,
        },
        "capture_limits": {
            "max_changed_paths": 100,
            "max_path_bytes": 1_024,
            "max_file_bytes": 1_048_576,
            "max_total_file_bytes": 4_194_304,
            "max_patch_bytes": 4_194_304,
        },
        "max_report_chars": 4_096,
        "arms": [
            {"requested_mode": mode, "repetitions": repetitions}
            for mode, repetitions in arms
        ],
        "tasks": [
            {
                "task_id": task_id,
                "requirement": "Create the file the frozen check requires.",
                "initial_dir": f"{task_id}/initial",
            }
            for task_id in tasks
        ],
    }
    (root / "benchmark.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return root


def steady_clock(step: float = 0.25) -> Iterator[float]:
    """A monotonic clock that advances a fixed amount on every observation."""

    value = 0.0
    while True:
        value += step
        yield value


def _runner(
    tmp_path: Path,
    *,
    arms: tuple[tuple[str, int], ...],
    provider: _ProductProvider | None = None,
    tasks: tuple[str, ...] = ("write_expected_file",),
    output: str = "evidence",
) -> ProductBenchmarkRunner:
    benchmark = build_benchmark(tmp_path / "benchmark", arms=arms, tasks=tasks)
    clock = steady_clock()
    return ProductBenchmarkRunner(
        benchmark,
        tmp_path / output,
        provider=provider or _ProductProvider(),
        model_id=PRODUCT_MODEL_ID,
        monotonic=lambda: next(clock),
    )


# ------------------------------------------------------------------ the grid


@pytest.mark.parametrize(
    ("requested", "resolved"),
    (
        ("single", ResolvedTaskMode.SINGLE),
        ("multi", ResolvedTaskMode.MULTI),
    ),
)
async def test_an_explicit_arm_runs_the_whole_product_mainline(
    tmp_path: Path, requested: str, resolved: ResolvedTaskMode
) -> None:
    runner = _runner(tmp_path, arms=((requested, 1),))
    report = await runner.run()

    assert report.complete
    (attempt,) = report.attempts
    assert attempt.success
    evidence = attempt.evidence
    assert evidence is not None
    assert evidence.resolved_mode is resolved
    assert evidence.review_passed is True
    assert evidence.promotion_id is not None
    assert evidence.new_revision != evidence.previous_revision
    assert evidence.target_revision == evidence.new_revision
    assert evidence.converged
    assert evidence.unavailable == ()
    # An explicit arm never pays for routing, and says so with an absence.
    assert evidence.routing is None
    assert evidence.routing_parsed is False
    expected_roles = {ResolvedTaskMode.SINGLE: 1, ResolvedTaskMode.MULTI: 3}[resolved]
    assert len(evidence.execution.sessions) == expected_roles
    assert evidence.execution.tokens is not None
    assert evidence.execution.tokens.total_tokens > 0
    assert evidence.execution.tokens.quality == "exact"


async def test_the_promoted_revision_is_what_the_target_ref_points_at(
    tmp_path: Path,
) -> None:
    runner = _runner(tmp_path, arms=(("single", 1),))
    report = await runner.run()

    (attempt,) = report.attempts
    assert attempt.evidence is not None
    target = tmp_path / "evidence" / attempt.directory / "tgt.git"
    assert await read_target_revision(target, "refs/heads/main") == (
        attempt.evidence.new_revision
    )


async def test_single_and_multi_share_one_experiment_condition(
    tmp_path: Path,
) -> None:
    runner = _runner(tmp_path, arms=(("single", 1), ("multi", 1)))
    report = await runner.run()

    (conditions,) = report.tasks
    assert conditions.coherent
    assert conditions.divergent_fields == ()
    assert conditions.profile_digest is not None
    assert conditions.requirement_digest is not None
    assert conditions.source_base_revision is not None
    assert conditions.verifier_definition_digest is not None
    modes = {attempt.resolved_mode for attempt in report.attempts}
    assert modes == {ResolvedTaskMode.SINGLE, ResolvedTaskMode.MULTI}
    # The one experimental variable is the confirmed mode: every other durable
    # digest is identical across the two arms.
    digests = {
        (
            attempt.evidence.profile_digest,
            attempt.evidence.requirement_digest,
            attempt.evidence.source_base_revision,
            attempt.evidence.verifier_definition_digest,
        )
        for attempt in report.attempts
        if attempt.evidence is not None
    }
    assert len(digests) == 1


async def test_auto_is_counted_in_the_arm_its_router_resolved(
    tmp_path: Path,
) -> None:
    runner = _runner(
        tmp_path,
        arms=(("multi", 1), ("auto", 1)),
        provider=_ProductProvider(route_to=ResolvedTaskMode.MULTI),
    )
    report = await runner.run()

    (arm,) = report.quality_arms
    assert arm.resolved_mode is ResolvedTaskMode.MULTI
    assert arm.observations == 2
    assert arm.to_dict()["requested_modes"] == {"auto": 1, "multi": 1}
    routing = report.routing_arm
    assert routing is not None
    assert routing.observations == 1
    assert routing.parsed == 1
    assert routing.to_dict()["resolved_modes"] == {"multi": 1}
    # auto is never its own quality arm.
    assert [item["resolved_mode"] for item in report.to_dict()["quality_arms"]] == [
        "multi"
    ]


async def test_routing_and_execution_tokens_are_separate_measurements(
    tmp_path: Path,
) -> None:
    runner = _runner(tmp_path, arms=(("auto", 1),))
    report = await runner.run()

    (attempt,) = report.attempts
    evidence = attempt.evidence
    assert evidence is not None
    assert evidence.routing is not None
    assert evidence.routing.tokens is not None
    assert evidence.routing.tokens.total_tokens > 0
    assert evidence.execution.tokens is not None
    routing_sessions = {evidence.routing.session_id}
    execution_sessions = {item.session_id for item in evidence.execution.sessions}
    assert routing_sessions.isdisjoint(execution_sessions)
    assert evidence.routing.agent_id not in {
        item.agent_id for item in evidence.execution.sessions
    }
    # The ledger's conservative settlement is reported beside, never instead of,
    # what the provider said it used.
    assert evidence.budget.settled_tokens >= 0
    assert evidence.execution.steps > 0
    assert evidence.execution.tool_calls > 0


async def test_active_elapsed_excludes_the_approval_wait(tmp_path: Path) -> None:
    runner = _runner(tmp_path, arms=(("single", 1),))
    report = await runner.run()

    (attempt,) = report.attempts
    timing = attempt.timing
    assert timing is not None
    assert timing.approval_wait_ms > 0
    assert timing.wall_ms > timing.approval_wait_ms
    assert timing.active_ms == timing.wall_ms - timing.approval_wait_ms
    assert attempt.to_dict()["timing"]["approval_policy"] == APPROVAL_POLICY


async def test_repeating_one_arm_aggregates_and_drops_the_n1_label(
    tmp_path: Path,
) -> None:
    runner = _runner(tmp_path, arms=(("single", 2),))
    report = await runner.run()

    (arm,) = report.quality_arms
    data = arm.to_dict()
    assert data["observations"] == 2
    assert data["single_observation"] is False
    assert len(data["execution_tokens"]["values"]) == 2
    assert data["execution_tokens"]["total"] == sum(
        data["execution_tokens"]["values"]
    )
    assert "single observation" not in _markdown(tmp_path / "evidence")


async def test_one_observation_is_labelled_in_both_outputs(tmp_path: Path) -> None:
    runner = _runner(tmp_path, arms=(("single", 1),))
    report = await runner.run()

    (arm,) = report.quality_arms
    assert arm.single_observation is True
    assert "single observation" in _markdown(tmp_path / "evidence")
    assert "No statistical significance is claimed." in _markdown(
        tmp_path / "evidence"
    )


async def test_the_two_reports_carry_the_same_values(tmp_path: Path) -> None:
    runner = _runner(tmp_path, arms=(("single", 1), ("auto", 1)))
    report = await runner.run()

    data = json.loads(
        (tmp_path / "evidence" / "report.json").read_text(encoding="utf-8")
    )
    markdown = _markdown(tmp_path / "evidence")
    assert data == report.to_dict()
    for attempt in data["attempts"]:
        assert attempt["attempt_id"] in markdown
        assert str(attempt["timing"]["active_ms"]) in markdown
        assert str(attempt["timing"]["wall_ms"]) in markdown
        if attempt["evidence"]["promotion_id"] is not None:
            assert attempt["evidence"]["promotion_id"] in markdown
    for arm in data["quality_arms"]:
        assert arm["resolved_mode"] in markdown
        assert f"successes: {arm['successes']}" in markdown
    assert data["approval_policy"] in markdown
    assert str(data["complete"]).lower() in markdown


# --------------------------------------------------------- failure and cleanup


async def test_attempt_retains_execution_and_store_close_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    execution_error = RuntimeError("attempt failed")
    close_error = RuntimeError("store close failed")

    class FailingCloseStore:
        def __init__(self, root: Path) -> None:
            self.root = root

        async def aclose(self) -> None:
            raise close_error

    async def build_repositories(**_kwargs: object) -> object:
        return object()

    async def fail_attempt(*_args: object, **_kwargs: object) -> object:
        raise execution_error

    monkeypatch.setattr(attempt_module, "SqliteEventStore", FailingCloseStore)
    monkeypatch.setattr(
        attempt_module, "build_attempt_repositories", build_repositories
    )
    monkeypatch.setattr(attempt_module, "_run_attempt_with_store", fail_attempt)
    request = SimpleNamespace(
        directory=tmp_path / "attempt",
        task=SimpleNamespace(initial_dir=tmp_path / "initial"),
    )

    with pytest.raises(BaseExceptionGroup) as caught:
        await attempt_module.run_attempt(
            request,
            manifest=object(),
            providers={},
        )

    assert caught.value.exceptions == (execution_error, close_error)


async def test_a_router_failure_is_measured_and_its_owners_converge(
    tmp_path: Path,
) -> None:
    runner = _runner(
        tmp_path, arms=(("auto", 1),), provider=_UnparsableRouterProvider()
    )
    report = await runner.run()

    (attempt,) = report.attempts
    assert not attempt.success
    assert attempt.measured
    evidence = attempt.evidence
    assert evidence is not None
    assert evidence.product_status.value == "failed"
    assert evidence.failure_code == "product-router-response-unparsable"
    assert evidence.converged
    assert evidence.promotion_id is None
    # The rejected Router answer still cost tokens. No durable Product fact names
    # that Agent, so its cost is reported as unattributed rather than relabelled
    # as routing the task never recorded, or silently dropped.
    assert evidence.routing is None
    assert evidence.routing_parsed is False
    assert len(evidence.unattributed.sessions) == 1
    assert evidence.unattributed.tokens is not None
    assert evidence.unattributed.tokens.total_tokens > 0
    assert evidence.execution.sessions == ()
    # A failed attempt is still a measurement, so the run is still complete.
    assert report.complete
    assert report.quality_arms == ()


async def test_interrupting_a_run_converges_before_it_propagates(
    tmp_path: Path,
) -> None:
    provider = _StalledCoderProvider()
    runner = _runner(tmp_path, arms=(("single", 1),), provider=provider)
    running = asyncio.create_task(runner.run())
    await asyncio.wait_for(provider.entered.wait(), timeout=30)

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, timeout=60)

    attempt_dir = tmp_path / "evidence" / "attempts" / "001"
    target = attempt_dir / "tgt.git"
    base = attempt_dir / "source"
    assert target.exists()
    # Nothing was promoted, and the evidence directory was not deleted to make
    # the interrupted attempt look clean.
    assert base.exists()

    store = SqliteEventStore(attempt_dir / "ev")
    (stream,) = await store.list_streams(prefix="product-task:")
    task_id = stream.removeprefix("product-task:")
    evidence = await collect_attempt_evidence(
        store,
        task_id=task_id,
        promotion_target_id=BENCHMARK_TARGET_ID,
        target_ref="refs/heads/main",
        target_revision=await read_target_revision(target, "refs/heads/main"),
        verification_plan=runner.manifest.settings.host_profile.verification_plan,
    )
    assert not evidence.success
    assert evidence.product_status is ProductTaskStatus.CANCELLED
    # An interrupted attempt is clean because its owners converged, not because
    # its evidence was removed.
    assert evidence.converged
    assert evidence.promotion_id is None


async def test_untrustworthy_usage_is_unavailable_rather_than_zero(
    tmp_path: Path,
) -> None:
    runner = _runner(
        tmp_path,
        arms=(("auto", 1),),
        provider=_UntrustworthyRouterUsageProvider(),
    )
    report = await runner.run()

    (attempt,) = report.attempts
    evidence = attempt.evidence
    assert evidence is not None and attempt.success
    # A total of 0 in a token column reads as "no tokens were used". The provider
    # said it did not know, so that one column is empty and the rest is intact.
    assert evidence.routing is not None
    assert evidence.routing.tokens is None
    assert "routing.tokens" in evidence.unavailable
    assert evidence.execution.tokens is not None
    assert evidence.execution.tokens.quality == "exact"
    data = report.to_dict()
    assert data["attempts_with_unavailable_metrics"] == 1
    assert data["routing_arm"]["routing_tokens"]["unavailable"] == 1
    assert data["routing_arm"]["routing_tokens"]["values"] == []
    assert data["quality_arms"][0]["execution_tokens"]["observations"] == 1
    assert report.complete
    assert "attempts with unavailable metrics: 1" in _markdown(tmp_path / "evidence")


async def test_a_role_that_worked_before_failing_still_reports_its_cost(
    tmp_path: Path,
) -> None:
    """A failed node records no ``agent_id``; the work it did is still real.

    The coder really calls ``apply_patch`` and only then stops responding. Read
    from the terminal payload alone this Agent disappears, and the arm aggregates
    a confident zero for a role that spent tokens and changed a worktree.
    """

    runner = _runner(
        tmp_path, arms=(("single", 1),), provider=_FailAfterWritingProvider()
    )
    report = await runner.run()

    (attempt,) = report.attempts
    evidence = attempt.evidence
    assert evidence is not None
    assert not attempt.success
    assert evidence.workflow_status is WorkflowStatus.FAILED
    assert len(evidence.execution.sessions) == 1
    coder = evidence.execution.sessions[0]
    assert coder.tool_calls == 1
    assert coder.tokens is not None and coder.tokens.total_tokens > 0
    # The cost belongs to the coder, not to an unattributed bucket.
    assert evidence.unattributed.sessions == ()
    arm = report.to_dict()["quality_arms"][0]
    assert arm["execution_tokens"]["total"] == coder.tokens.total_tokens
    assert arm["tool_calls"]["total"] == 1


async def test_a_dirty_worktree_quarantined_on_failure_is_converged(
    tmp_path: Path,
) -> None:
    """Quarantine is the Product contract's terminal for failure evidence."""

    runner = _runner(
        tmp_path, arms=(("single", 1),), provider=_WrongContentProvider()
    )
    report = await runner.run()

    (attempt,) = report.attempts
    evidence = attempt.evidence
    assert evidence is not None and not attempt.success
    assert evidence.workspaces.quarantined >= 1
    assert evidence.workspaces.live == 0
    # Reporting this as unconverged would call the failure lifecycle broken at
    # the exact moment it preserved the evidence it was designed to preserve.
    assert evidence.workspaces.converged
    assert evidence.converged
    assert report.to_dict()["attempts"][0]["evidence"]["workspaces"]["live"] == 0


async def test_an_arm_failing_before_review_leaves_the_verifier_unproven(
    tmp_path: Path,
) -> None:
    runner = _runner(
        tmp_path,
        arms=(("single", 1), ("multi", 1)),
        provider=_FailingParentProvider(),
    )
    report = await runner.run()

    outcomes = {
        attempt.requested_mode.value: attempt for attempt in report.attempts
    }
    assert outcomes["single"].success
    assert not outcomes["multi"].success
    assert outcomes["multi"].evidence is not None
    assert outcomes["multi"].evidence.verifier_definition_digest is None

    (conditions,) = report.tasks
    assert conditions.verifier_definition_digest == (
        runner.manifest.verifier_definition_digest
    )
    # Proved from the frozen manifest, and the arm that never demonstrated it is
    # named instead of being filtered into apparent agreement.
    assert "verifier_definition_digest" in conditions.unproven_fields
    assert conditions.coherent
    assert "unproven" in _markdown(tmp_path / "evidence")


# ----------------------------------------------------------- evidence refusals


async def test_evidence_is_refused_when_the_durable_facts_do_not_support_it(
    tmp_path: Path,
) -> None:
    """Each refusal is triggered through the public collector, on a real store."""

    runner = _runner(tmp_path, arms=(("single", 1),))
    report = await runner.run()
    (attempt,) = report.attempts
    assert attempt.evidence is not None
    store = SqliteEventStore(tmp_path / "evidence" / attempt.directory / "ev")

    with pytest.raises(BenchmarkEvidenceError) as unknown:
        await collect_attempt_evidence(
            store,
            task_id="product-task-0000000000000000",
            promotion_target_id=BENCHMARK_TARGET_ID,
            target_ref="refs/heads/main",
            target_revision=attempt.evidence.new_revision,
            verification_plan=runner.manifest.settings.host_profile.verification_plan,
        )
    assert unknown.value.code == "benchmark-product-task-missing"

    with pytest.raises(BenchmarkEvidenceError) as plan:
        await collect_attempt_evidence(
            store,
            task_id=attempt.evidence.task_id,
            promotion_target_id="a-different-target",
            target_ref="refs/heads/main",
            target_revision=attempt.evidence.new_revision,
            verification_plan=runner.manifest.settings.host_profile.verification_plan,
        )
    # A run interpreted through a definition it never agreed to would report node
    # kinds and results that never happened.
    assert plan.value.code == "benchmark-definition-hash-mismatch"

    with pytest.raises(BenchmarkEvidenceError) as ref:
        await collect_attempt_evidence(
            store,
            task_id=attempt.evidence.task_id,
            promotion_target_id=BENCHMARK_TARGET_ID,
            target_ref="refs/heads/somewhere-else",
            target_revision=attempt.evidence.new_revision,
            verification_plan=runner.manifest.settings.host_profile.verification_plan,
        )
    assert ref.value.code == "benchmark-promotion-target-mismatch"


async def test_a_session_that_breaks_the_core_invariants_is_refused(
    tmp_path: Path,
) -> None:
    """Counting events without checking the lifecycle inflates any number.

    One extra ``model/attempt-end`` with no matching start is enough: the stream
    still parses, the Turn/Step nesting still looks plausible, and the token
    column silently gains whatever the forged usage claims.
    """

    runner = _runner(tmp_path, arms=(("single", 1),))
    report = await runner.run()
    (attempt,) = report.attempts
    assert attempt.evidence is not None and attempt.success
    honest = attempt.evidence.execution.tokens
    assert honest is not None
    store = SqliteEventStore(tmp_path / "evidence" / attempt.directory / "ev")

    coder = attempt.evidence.execution.sessions[0]
    stream = f"session:{coder.session_id}"
    await store.append(
        stream,
        expected_seq=await store.head(stream),
        events=(
            PendingEvent(
                type="model/attempt-end",
                data={
                    "turn_id": "forged-turn",
                    "step_id": "forged-step",
                    "attempt_id": "forged-attempt",
                    "status": "succeeded",
                    "finish_reason": "stop",
                    "usage": {
                        "input_tokens": 1_000_000,
                        "output_tokens": 1_000_000,
                        "total_tokens": 2_000_000,
                        "quality": "exact",
                    },
                },
                schema_version=1,
            ),
        ),
    )

    with pytest.raises(BenchmarkEvidenceError) as caught:
        await collect_attempt_evidence(
            store,
            task_id=attempt.evidence.task_id,
            promotion_target_id=BENCHMARK_TARGET_ID,
            target_ref="refs/heads/main",
            target_revision=attempt.evidence.new_revision,
            verification_plan=runner.manifest.settings.host_profile.verification_plan,
        )
    assert caught.value.code == "benchmark-session-invariants-violated"


async def test_a_review_the_workflow_never_produced_breaks_the_chain(
    tmp_path: Path,
) -> None:
    """Verification, Review and Promotion must be one chain, not three facts.

    Each is well-formed on its own, so reading them independently lets a report
    say "verified, approved and promoted" from three unrelated records.
    """

    runner = _runner(tmp_path, arms=(("single", 1),))
    report = await runner.run()
    (attempt,) = report.attempts
    assert attempt.evidence is not None and attempt.success
    events = tmp_path / "evidence" / attempt.directory / "ev"
    assert attempt.evidence.review_id is not None
    _replace_stream_value(
        events,
        f"workflow:{attempt.evidence.task_id}",
        attempt.evidence.review_id,
        "review-somebody-elses-report",
    )
    with pytest.raises(BenchmarkEvidenceError) as caught:
        await collect_attempt_evidence(
            SqliteEventStore(events),
            task_id=attempt.evidence.task_id,
            promotion_target_id=BENCHMARK_TARGET_ID,
            target_ref="refs/heads/main",
            target_revision=attempt.evidence.new_revision,
            verification_plan=runner.manifest.settings.host_profile.verification_plan,
        )
    assert caught.value.code == "benchmark-review-chain-broken"


async def test_a_review_result_outside_the_frozen_plan_is_refused(
    tmp_path: Path,
) -> None:
    """Internal Review consistency cannot replace binding to the host plan.

    Recomputing the Review's evidence digest leaves a shape-valid durable event.
    The collector must still refuse it because the result no longer names the
    exact verifier command frozen in the benchmark manifest.
    """

    runner = _runner(tmp_path, arms=(("single", 1),))
    report = await runner.run()
    (attempt,) = report.attempts
    assert attempt.evidence is not None and attempt.success

    events = tmp_path / "evidence" / attempt.directory / "ev"
    event_store = SqliteEventStore(events)
    ledger = await PromotionLedgerReader(event_store).load()
    await event_store.aclose()
    assert attempt.evidence.review_id is not None
    review = ledger.review(attempt.evidence.review_id)
    assert review is not None
    assert attempt.evidence.promotion_id is not None
    promotion = ledger.promotion(attempt.evidence.promotion_id)
    assert promotion is not None
    records = _read_stream_records(events, "patch-promotions:ledger")
    recorded = next(
        record for record in records if record["type"] == "patch/review-recorded"
    )
    result = dict(recorded["data"]["results"][0])
    replacement = "9" * 64
    assert result["argv_digest"] != replacement
    result["argv_digest"] = replacement
    recorded["data"]["results"] = [result]
    results = (VerifierOutcome(**result),)
    evidence_digest = verification_evidence_digest(
        recorded["data"]["verifier_definition_digest"], results
    )
    recorded["data"]["verification_evidence_digest"] = evidence_digest
    approval_digest = expected_approval_digest(
        replace(
            review,
            results=results,
            verification_evidence_digest=evidence_digest,
        )
    )
    replacement_promotion_id = promotion_identity(approval_digest)
    for record in records:
        if "approval_digest" in record["data"]:
            record["data"]["approval_digest"] = approval_digest
        if record["type"] == "patch/promotion-committed":
            record["data"]["promotion_id"] = replacement_promotion_id
    _write_stream_records(events, "patch-promotions:ledger", records)
    # Keep the other durable domains internally consistent with the rewritten
    # ledger. Without the frozen-plan check the old collector therefore accepts
    # the attempt as a successful, fully chained measurement.
    for fact_stream in (
        f"product-task:{attempt.evidence.task_id}",
        f"workflow:{attempt.evidence.task_id}",
    ):
        _replace_stream_value(
            events,
            fact_stream,
            promotion.approval_digest,
            approval_digest,
        )
        _replace_stream_value(
            events,
            fact_stream,
            promotion.promotion_id,
            replacement_promotion_id,
            required=False,
        )

    with pytest.raises(BenchmarkEvidenceError) as caught:
        await collect_attempt_evidence(
            SqliteEventStore(events),
            task_id=attempt.evidence.task_id,
            promotion_target_id=BENCHMARK_TARGET_ID,
            target_ref="refs/heads/main",
            target_revision=attempt.evidence.new_revision,
            verification_plan=runner.manifest.settings.host_profile.verification_plan,
        )
    assert caught.value.code == "benchmark-verifier-evidence-mismatch"


async def test_a_routing_session_the_router_agent_does_not_own_is_refused(
    tmp_path: Path,
) -> None:
    """The Directory decides which Session an Agent owns, not the payload.

    ``product/task-routed`` names both, so taking the pair on trust lets the
    routing identity point at a role Session of the same task. That Session
    parses cleanly and passes the invariant check, so the same tokens would be
    counted once as routing and once as execution - which is exactly the
    separation the two metrics exist to keep.
    """

    runner = _runner(tmp_path, arms=(("auto", 1),))
    report = await runner.run()
    (attempt,) = report.attempts
    evidence = attempt.evidence
    assert evidence is not None and attempt.success
    assert evidence.routing is not None
    coder = evidence.execution.sessions[-1]
    assert coder.session_id != evidence.routing.session_id

    events = tmp_path / "evidence" / attempt.directory / "ev"
    _replace_stream_value(
        events,
        f"product-task:{evidence.task_id}",
        evidence.routing.session_id,
        coder.session_id,
    )

    with pytest.raises(BenchmarkEvidenceError) as caught:
        await collect_attempt_evidence(
            SqliteEventStore(events),
            task_id=evidence.task_id,
            promotion_target_id=BENCHMARK_TARGET_ID,
            target_ref="refs/heads/main",
            target_revision=evidence.new_revision,
            verification_plan=runner.manifest.settings.host_profile.verification_plan,
        )
    assert caught.value.code == "benchmark-routing-session-mismatch"


async def test_success_requires_the_ref_to_hold_the_promoted_revision(
    tmp_path: Path,
) -> None:
    """A promotion receipt alone is not proof that the repository moved."""

    runner = _runner(tmp_path, arms=(("single", 1),))
    report = await runner.run()
    (attempt,) = report.attempts
    assert attempt.evidence is not None and attempt.success
    store = SqliteEventStore(tmp_path / "evidence" / attempt.directory / "ev")

    for revision in (None, attempt.evidence.previous_revision):
        evidence = await collect_attempt_evidence(
            store,
            task_id=attempt.evidence.task_id,
            promotion_target_id=BENCHMARK_TARGET_ID,
            target_ref="refs/heads/main",
            target_revision=revision,
            verification_plan=runner.manifest.settings.host_profile.verification_plan,
        )
        assert evidence.product_status.value == "completed"
        assert evidence.review_passed is True
        assert evidence.promotion_id is not None
        # Everything else agrees; only the ref disagrees, and that is enough.
        assert evidence.success is False


# ------------------------------------------------------------------- authority


async def test_no_evaluator_approval_or_promotion_value_reaches_the_model(
    tmp_path: Path,
) -> None:
    requests: list[ModelRequest] = []
    runner = _runner(
        tmp_path, arms=(("multi", 1),), provider=_ProductProvider(requests)
    )
    report = await runner.run()

    (attempt,) = report.attempts
    evidence = attempt.evidence
    assert evidence is not None and attempt.success
    surface = repr(requests)
    assert requests
    # The model must receive its writable Workspace path, which deliberately
    # lives below the attempt directory.  That parent directory is a location,
    # not evaluator authority or a frozen evaluation value.  The values below
    # are the actual Review, Promotion, verifier and target facts that must stay
    # out of every ModelRequest.
    secrets = [
        evidence.review_id,
        evidence.promotion_id,
        evidence.new_revision,
        evidence.previous_revision,
        evidence.target_ref,
        evidence.verifier_definition_digest,
        _VERIFIER_ARGV[2],
    ]
    for value in secrets:
        assert value is not None
        assert value not in surface


async def test_the_runner_refuses_a_provider_the_profile_cannot_name(
    tmp_path: Path,
) -> None:
    class _Anonymous:
        name = ""

        async def complete(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError("must not be called")

    benchmark = build_benchmark(tmp_path / "benchmark", arms=(("single", 1),))
    with pytest.raises(BenchmarkExecutionError) as caught:
        ProductBenchmarkRunner(
            benchmark,
            tmp_path / "evidence",
            provider=_Anonymous(),
            model_id=PRODUCT_MODEL_ID,
        )
    assert caught.value.code == "benchmark-provider-binding-missing"


async def test_the_requester_relay_is_not_the_measured_provider(
    tmp_path: Path,
) -> None:
    requests: list[ModelRequest] = []
    runner = _runner(
        tmp_path, arms=(("single", 1),), provider=_ProductProvider(requests)
    )
    report = await runner.run()

    (attempt,) = report.attempts
    assert attempt.evidence is not None
    assert all(request.provider == PRODUCT_PROVIDER_ID for request in requests)
    assert all(
        request.provider != REQUESTER_PROVIDER_ID for request in requests
    )
    # The requirement the model saw is the manifest's, not one a model authored.
    assert any(
        "Create the file the frozen check requires." in message.content
        for request in requests
        for message in request.messages
    )


def _markdown(output: Path) -> str:
    return (output / "report.md").read_text(encoding="utf-8")
