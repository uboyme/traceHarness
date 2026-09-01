"""F3 UI observation handshake and pure command parsing."""

from __future__ import annotations

import ast
import asyncio
import inspect
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from product_fixtures import ORIGIN_SESSION, build_assembly, opened

import traceh.chat.driver as chat_driver_module
import traceh.cli.chat as line_chat_module
import traceh.cli.product as line_product_module
import traceh.product.chat as product_chat_module
import traceh.product.observation as product_observation_module
from traceh.agents import AgentRegistrar
from traceh.agents.identity import AGENT_DIRECTORY_STREAM
from traceh.api.agents import AgentSpec
from traceh.api.budgets import BudgetAmounts, BudgetLimits
from traceh.api.events import PendingEvent
from traceh.api.llm import UsageQuality
from traceh.api.product import ProductTaskSummary
from traceh.api.promotion import PatchReviewReport
from traceh.artifacts.errors import ArtifactCasError
from traceh.artifacts.events import ARTIFACT_CATALOG_STREAM
from traceh.artifacts.unified_diff import UnifiedDiff, UnifiedDiffSummary
from traceh.budgets import BUDGET_LEDGER_STREAM, BudgetLedgerService
from traceh.product.chat import (
    ProductCommandOperation,
    parse_product_command,
)
from traceh.product.errors import ProductInputError, ProductStateError
from traceh.product.events import product_task_stream
from traceh.product.execution import product_task_owner_id
from traceh.product.host import build_product_chat_host
from traceh.product.inspection import ProductPatchEvidence
from traceh.product.observation import (
    ObservedStreamHead,
    ProductObservation,
    ProductObservationReader,
    ProductObservationSession,
)
from traceh.product.projection import ProductTaskStreamReader
from traceh.promotion.events import PROMOTION_LEDGER_STREAM
from traceh.session.event_feed import PublishingEventStore, SessionEventFeed
from traceh.session.event_store import InMemoryEventStore
from traceh.workflow.events import workflow_stream_id


class _RaceReader:
    """Reveal a related stream only after changing it before subscription."""

    def __init__(self, store: PublishingEventStore) -> None:
        self.store = store
        self.calls = 0

    async def load(self, task_id: str) -> ProductObservation:
        self.calls += 1
        related = "session:related-agent"
        if self.calls == 1:
            # This fact lands before ProductObservationSession can subscribe to
            # the just-discovered stream.  The required re-read, not Feed replay,
            # is what makes it visible.
            await self.store.append(
                related,
                expected_seq=0,
                events=(PendingEvent(type="probe/accepted", data={}),),
            )
        head = await self.store.head(related)
        latest = None
        if head:
            latest = (await self.store.read(related, from_seq=head))[-1]
        return ProductObservation(
            task_id=task_id,
            summary=None,
            workflow=None,
            evidence=None,
            review=None,
            approval=None,
            promotion=None,
            approval_digest=None,
            stream_heads=(
                ObservedStreamHead(
                    related,
                    head,
                    None if latest is None else latest.type,
                    None if latest is None else latest.occurred_at,
                    True,
                ),
            ),
            observed_at=datetime.now(UTC),
        )


class _FailingReader:
    async def load(self, task_id: str) -> ProductObservation:
        del task_id
        raise RuntimeError("observation read failed")


class _PatchEvidenceReader:
    def __init__(
        self,
        store: InMemoryEventStore,
        result: ProductPatchEvidence | BaseException,
    ) -> None:
        self.store = store
        self.result = result
        self.calls: list[tuple[ProductTaskSummary, PatchReviewReport]] = []

    async def load_patch(
        self,
        summary: ProductTaskSummary,
        review: PatchReviewReport,
    ) -> ProductPatchEvidence:
        self.calls.append((summary, review))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class _FixedObservationReader(ProductObservationReader):
    __slots__ = ("_fixed",)

    def __init__(
        self,
        store: InMemoryEventStore,
        evidence: _PatchEvidenceReader,
        observation: ProductObservation,
    ) -> None:
        super().__init__(
            store,
            evidence,  # type: ignore[arg-type]
            promotion_target_id="observation-target",
        )
        self._fixed = observation

    async def load(self, task_id: str) -> ProductObservation:
        assert task_id == self._fixed.task_id
        return self._fixed


async def test_subscribe_before_read_re_reads_every_discovered_exact_stream() -> None:
    feed = SessionEventFeed()
    store = PublishingEventStore(InMemoryEventStore(), feed)
    reader = _RaceReader(store)
    observer = ProductObservationSession(reader, feed, "task-race")  # type: ignore[arg-type]

    observation = await observer.start()

    assert reader.calls == 2
    assert "session:related-agent" in observer.subscribed_streams
    assert observation.stream_heads[0].stream_id == "session:related-agent"
    assert observation.stream_heads[0].seq == 1
    assert observation.stream_heads[0].event_type == "probe/accepted"

    await store.append(
        "session:related-agent",
        expected_seq=1,
        events=(PendingEvent(type="probe/finished", data={}),),
    )
    await observer.wait_dirty()
    refreshed = await observer.refresh()
    assert refreshed.stream_heads[0].seq == 2
    assert refreshed.stream_heads[0].event_type == "probe/finished"
    await observer.aclose()
    assert feed.subscriber_count("session:related-agent") == 0


async def test_failed_start_rolls_back_every_partial_subscription() -> None:
    feed = SessionEventFeed()
    observer = ProductObservationSession(  # type: ignore[arg-type]
        _FailingReader(), feed, "task-failed-start"
    )

    with pytest.raises(RuntimeError, match="observation read failed"):
        await observer.start()
    with pytest.raises(ProductStateError) as restarted:
        await observer.start()
    assert restarted.value.code == "product-observation-closed"

    assert observer.subscribed_streams == ()
    assert not observer.dirty
    assert not any(
        feed.subscriber_count(stream_id)
        for stream_id in (
            product_task_stream("task-failed-start"),
            workflow_stream_id("task-failed-start"),
            AGENT_DIRECTORY_STREAM,
            ARTIFACT_CATALOG_STREAM,
            BUDGET_LEDGER_STREAM,
            PROMOTION_LEDGER_STREAM,
        )
    )


def _fixed_patch_observation(
    summary: ProductTaskSummary | None,
    review: PatchReviewReport | None,
) -> ProductObservation:
    return ProductObservation(
        task_id="task-patch",
        summary=summary,
        workflow=None,
        evidence=None,
        review=review,
        approval=None,
        promotion=None,
        approval_digest=None,
        stream_heads=(),
        observed_at=datetime.now(UTC),
    )


async def test_load_patch_delegates_only_after_a_fresh_review_observation() -> None:
    store = InMemoryEventStore()
    summary = cast(ProductTaskSummary, object())
    review = cast(PatchReviewReport, object())
    patch = ProductPatchEvidence(
        artifact_id="patch-artifact",
        patch_sha256="a" * 64,
        patch_size_bytes=0,
        content=b"",
        diff=UnifiedDiff(UnifiedDiffSummary((), 0, 0, True), ()),
    )
    evidence = _PatchEvidenceReader(store, patch)
    reader = _FixedObservationReader(
        store,
        evidence,
        _fixed_patch_observation(summary, review),
    )

    assert await reader.load_patch("task-patch") is patch
    assert evidence.calls == [(summary, review)]

    no_review = _FixedObservationReader(
        store,
        evidence,
        _fixed_patch_observation(summary, None),
    )
    assert await no_review.load_patch("task-patch") is None
    assert evidence.calls == [(summary, review)]


async def test_load_patch_does_not_hide_fresh_artifact_failure() -> None:
    store = InMemoryEventStore()
    summary = cast(ProductTaskSummary, object())
    review = cast(PatchReviewReport, object())
    evidence = _PatchEvidenceReader(
        store,
        ArtifactCasError("artifact-cas-digest-mismatch"),
    )
    reader = _FixedObservationReader(
        store,
        evidence,
        _fixed_patch_observation(summary, review),
    )

    with pytest.raises(ArtifactCasError) as failed:
        await reader.load_patch("task-patch")
    assert failed.value.code == "artifact-cas-digest-mismatch"
    assert evidence.calls == [(summary, review)]


def _usage_limits(**overrides: int | None) -> BudgetLimits:
    values: dict[str, int | None] = {
        "max_tokens": 100_000,
        "max_steps": 100,
        "max_tool_calls": 100,
        "max_wall_milliseconds": 600_000,
        "max_children": 2,
        "max_depth": 1,
        "max_processes": 2,
    }
    values.update(overrides)
    return BudgetLimits(**values)


async def _usage_owner(
    store: InMemoryEventStore | PublishingEventStore,
    task_id: str,
    *,
    limits: BudgetLimits | None = None,
) -> tuple[BudgetLedgerService, str]:
    owner_id = product_task_owner_id(task_id)
    await AgentRegistrar(store).create_agent(
        AgentSpec(
            preset="observation-usage",
            workspace_id=f"workspace-{task_id}",
            owner_agent_id=None,
        ),
        request_id=f"create-{task_id}",
        agent_id=owner_id,
        session_id=f"session-{task_id}",
    )
    budgets = BudgetLedgerService(store)
    await budgets.grant_root(
        operation_id=f"grant-{task_id}",
        agent_id=owner_id,
        limits=limits or _usage_limits(),
    )
    return budgets, owner_id


def _usage_reader(
    store: InMemoryEventStore | PublishingEventStore,
) -> ProductObservationReader:
    return ProductObservationReader(
        store,
        SimpleNamespace(store=store),  # type: ignore[arg-type]
        promotion_target_id="observation-target",
    )


async def _settle_usage(
    budgets: BudgetLedgerService,
    owner_id: str,
    *,
    prefix: str,
    tokens: int,
    wall_milliseconds: int,
    quality: UsageQuality,
) -> None:
    await budgets.reserve_usage(
        operation_id=f"{prefix}-tokens-reserve",
        reservation_id=f"{prefix}-tokens",
        agent_id=owner_id,
        amounts=BudgetAmounts(tokens=50_000),
    )
    await budgets.start_usage(
        operation_id=f"{prefix}-tokens-start",
        reservation_id=f"{prefix}-tokens",
    )
    await budgets.settle_usage(
        operation_id=f"{prefix}-tokens-settle",
        reservation_id=f"{prefix}-tokens",
        amounts=BudgetAmounts(tokens=tokens),
        usage_quality=quality,
    )
    await budgets.reserve_usage(
        operation_id=f"{prefix}-wall-reserve",
        reservation_id=f"{prefix}-wall",
        agent_id=owner_id,
        amounts=BudgetAmounts(wall_milliseconds=300_000),
    )
    await budgets.start_usage(
        operation_id=f"{prefix}-wall-start",
        reservation_id=f"{prefix}-wall",
    )
    await budgets.settle_usage(
        operation_id=f"{prefix}-wall-settle",
        reservation_id=f"{prefix}-wall",
        amounts=BudgetAmounts(wall_milliseconds=wall_milliseconds),
        usage_quality=None,
    )


async def test_product_usage_is_fresh_durable_and_task_scoped() -> None:
    store = InMemoryEventStore()
    task_id = "task-usage-primary"
    budgets, owner_id = await _usage_owner(store, task_id)
    await _settle_usage(
        budgets,
        owner_id,
        prefix="primary",
        tokens=34_840,
        wall_milliseconds=192_000,
        quality=UsageQuality.EXACT,
    )
    await budgets.record_usage(
        operation_id="primary-steps",
        agent_id=owner_id,
        amounts=BudgetAmounts(steps=2),
    )

    other_budgets, other_owner = await _usage_owner(store, "task-usage-other")
    await other_budgets.record_usage(
        operation_id="other-usage",
        agent_id=other_owner,
        amounts=BudgetAmounts(tokens=7_000, steps=7, wall_milliseconds=7_000),
        usage_quality=UsageQuality.EXACT,
    )

    observation = await _usage_reader(store).load(task_id)
    assert observation.usage is not None
    assert observation.usage.tokens == 34_840
    assert observation.usage.token_quality is UsageQuality.EXACT
    assert observation.usage.steps == 2
    assert observation.usage.wall_milliseconds == 192_000
    budget_head = next(
        head
        for head in observation.stream_heads
        if head.stream_id == BUDGET_LEDGER_STREAM
    )
    assert budget_head.seq == await store.head(BUDGET_LEDGER_STREAM)
    assert not budget_head.task_bound


async def test_product_usage_missing_and_unknown_dimensions_stay_unavailable() -> None:
    store = InMemoryEventStore()
    missing = await _usage_reader(store).load("task-usage-missing")
    assert missing.usage is not None
    assert missing.usage.tokens is None
    assert missing.usage.steps is None
    assert missing.usage.wall_milliseconds is None

    task_id = "task-usage-unknown"
    budgets, owner_id = await _usage_owner(store, task_id)
    await _settle_usage(
        budgets,
        owner_id,
        prefix="unknown",
        tokens=50_000,
        wall_milliseconds=1_500,
        quality=UsageQuality.UNKNOWN,
    )
    await budgets.record_usage(
        operation_id="unknown-steps",
        agent_id=owner_id,
        amounts=BudgetAmounts(steps=1),
    )

    observation = await _usage_reader(store).load(task_id)
    assert observation.usage is not None
    assert observation.usage.tokens is None
    assert observation.usage.token_quality is None
    assert observation.usage.steps == 1
    assert observation.usage.wall_milliseconds == 1_500


async def test_budget_settlement_marks_product_observation_dirty() -> None:
    feed = SessionEventFeed()
    store = PublishingEventStore(InMemoryEventStore(), feed)
    task_id = "task-usage-dirty"
    budgets, owner_id = await _usage_owner(store, task_id)
    observer = ProductObservationSession(_usage_reader(store), feed, task_id)
    initial = await observer.start()
    assert initial.usage is not None
    assert initial.usage.steps == 0
    assert BUDGET_LEDGER_STREAM in observer.subscribed_streams

    await budgets.record_usage(
        operation_id="dirty-steps",
        agent_id=owner_id,
        amounts=BudgetAmounts(steps=1),
    )
    await asyncio.wait_for(observer.wait_dirty(), timeout=2)
    refreshed = await observer.refresh()
    assert refreshed.usage is not None
    assert refreshed.usage.steps == 1
    await observer.aclose()
    assert feed.subscriber_count(BUDGET_LEDGER_STREAM) == 0


async def test_current_product_task_is_rebuilt_from_durable_streams() -> None:
    assembly = await build_assembly()
    reader = ProductTaskStreamReader(assembly.store)
    try:
        assert await reader.current_for_session(ORIGIN_SESSION) is None
        await opened(assembly, task_id="task-current")
        assert await reader.current_for_session(ORIGIN_SESSION) == "task-current"

        await assembly.service.cancel_task(
            task_id="task-current",
            operation_id="task-current-cancel",
            reason_code="test-cancelled",
        )
        assert await reader.current_for_session(ORIGIN_SESSION) is None

        await opened(assembly, task_id="task-live-one")
        await opened(assembly, task_id="task-live-two")
        with pytest.raises(ProductStateError) as ambiguous:
            await reader.current_for_session(ORIGIN_SESSION)
        assert ambiguous.value.code == "product-observation-session-ambiguous"
    finally:
        await assembly.aclose()
    assert not any(
        task.get_name() == "traceh-product-observer-task-failed-start"
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
    )


def test_product_host_requires_an_explicit_event_feed() -> None:
    parameter = inspect.signature(build_product_chat_host).parameters["event_feed"]
    assert parameter.default is inspect.Parameter.empty
    sessions = inspect.signature(build_product_chat_host).parameters["sessions"]
    assert sessions.default is inspect.Parameter.empty


def test_product_command_parser_is_pure_and_exact() -> None:
    command = parse_product_command("/task cancel product-task-7")
    assert command is not None
    assert command.operation is ProductCommandOperation.CANCEL
    assert command.task_id == "product-task-7"
    assert parse_product_command("ordinary chat") is None
    with pytest.raises(ProductInputError, match="product command"):
        parse_product_command("/task cancel product-task-7 extra")


def _module_tree(module: object) -> ast.Module:
    path = Path(module.__file__)  # type: ignore[attr-defined]
    return ast.parse(path.read_text(encoding="utf-8"))


def _imports(module: object) -> set[str]:
    result: set[str] = set()
    for node in ast.walk(_module_tree(module)):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            result.add(node.module)
    return result


def test_ui_neutral_drivers_do_not_import_line_terminal_code() -> None:
    for module in (
        chat_driver_module,
        product_chat_module,
        product_observation_module,
    ):
        assert not any(name.startswith("traceh.cli") for name in _imports(module))
    assert "traceh.chat.driver" in _imports(line_chat_module)
    assert "traceh.product.observation" in _imports(line_product_module)


def test_product_observation_has_no_control_or_store_write_call() -> None:
    forbidden_control = {
        "abandon",
        "approve",
        "cancel",
        "execute_command",
        "inspect",
        "reject",
    }
    calls = [
        node.func
        for node in ast.walk(_module_tree(product_observation_module))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    control_calls = {call.attr for call in calls} & forbidden_control
    store_writes = {
        call.attr
        for call in calls
        if call.attr == "append"
        and isinstance(call.value, ast.Attribute)
        and call.value.attr == "_store"
    }
    assert not control_calls, control_calls
    assert not store_writes, store_writes
