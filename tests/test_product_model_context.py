"""ProductTask memory: one deterministic, replayable format-7 snapshot."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from product_fixtures import (
    build_assembly,
    confirmation,
    opened,
    proposal,
    receipt,
    seed_session,
)

from traceh.api.events import PendingEvent
from traceh.api.json_types import canonical_json, fingerprint
from traceh.api.product import (
    ProductTaskStatus,
    RequestedTaskMode,
    ResolvedTaskMode,
)
from traceh.product.activity import ProductTaskActivity
from traceh.product.context import ProductModelContext
from traceh.product.errors import ProductContextError, ProductStateError
from traceh.product.memory import ProductTaskMemory, ProductTaskMemoryReader
from traceh.product.observation import ProductObservation
from traceh.session.event_store import ConcurrencyConflict, InMemoryEventStore
from traceh.session.invariants import CoreInvariantChecker
from traceh.session.product_context import (
    MAX_PRODUCT_CONTEXT_CONTENT_CHARS,
    MAX_PRODUCT_CONTEXT_EXCERPT_JSON_CHARS,
    MAX_PRODUCT_CONTEXT_TASKS,
    PRODUCT_CONTEXT_FORMAT_VERSION,
    PRODUCT_CONTEXT_SNAPSHOT,
    ProductContextExecutionSummary,
    ProductContextTask,
    bounded_product_context_excerpt,
    latest_product_context,
    parse_product_context_snapshot,
    product_context_snapshot_data,
)
from traceh.session.service import SessionService
from traceh.session.surface import SurfaceProjector


def _context_events(events):
    return tuple(event for event in events if event.type == PRODUCT_CONTEXT_SNAPSHOT)


def _task(
    task_id: str,
    *,
    order: int,
    status: ProductTaskStatus = ProductTaskStatus.COMPLETED,
    source_seq: int = 4,
    excerpt: str = "check the inventory reservation implementation",
    excerpt_truncated: bool = False,
) -> ProductContextTask:
    execution_summary = None
    if status in {
        ProductTaskStatus.AWAITING_APPROVAL,
        ProductTaskStatus.COMPLETED,
        ProductTaskStatus.REJECTED,
        ProductTaskStatus.CANCELLED,
        ProductTaskStatus.FAILED,
    }:
        execution_summary = ProductContextExecutionSummary(
            workflow_status=None,
            managed_tool_call_count=0,
            changed_path_count=None,
            verification_passed=None,
            verifier_count=None,
            promotion_recorded=status is ProductTaskStatus.COMPLETED,
        )
    return ProductContextTask(
        task_id=task_id,
        source_stream_id=f"product-task:{task_id}",
        source_seq=source_seq,
        source_event_id=uuid4(),
        task_order_seq=order,
        status=status,
        requested_mode=RequestedTaskMode.SINGLE,
        resolved_mode=ResolvedTaskMode.SINGLE,
        requirement_digest="a" * 64,
        origin_message_id=f"origin-{task_id}",
        source_excerpt=excerpt,
        source_excerpt_truncated=excerpt_truncated,
        execution_summary=execution_summary,
    )


def _snapshot_data(session_id: str, focus: ProductContextTask, tasks, total_tasks: int):
    tasks = tuple(tasks)
    tasks = (focus,) + tuple(
        replace(task, execution_summary=None) for task in tasks[1:]
    )
    return product_context_snapshot_data(
        session_id=session_id,
        focus=focus,
        tasks=tuple(tasks),
        total_tasks=total_tasks,
    )


class _ContextObservation:
    def __init__(self, store) -> None:
        self.store = store


class _ContextMemoryReader(ProductTaskMemoryReader):
    """Keep catalog tests focused while real E2E proves the evidence join."""

    def __init__(self, store) -> None:
        super().__init__(store, _ContextObservation(store))

    async def load(self, session_id: str, task_id: str) -> ProductTaskMemory:
        head = await self.load_head(session_id, task_id)
        return await self.load_for_head(session_id, head)

    async def load_for_head(self, session_id, head) -> ProductTaskMemory:
        observation = ProductObservation(
            task_id=head.summary.task_id,
            summary=head.summary,
            workflow=None,
            evidence=None,
            review=None,
            approval=None,
            promotion=(object() if head.summary.status is ProductTaskStatus.COMPLETED else None),
            approval_digest=None,
            stream_heads=(),
            observed_at=datetime.now(UTC),
        )
        return ProductTaskMemory(
            head=head,
            observation=observation,
            activity=ProductTaskActivity(head.summary.task_id, ()),
        )


def _model_context(store, sessions: SessionService | None = None) -> ProductModelContext:
    return ProductModelContext(
        SessionService(store) if sessions is None else sessions,
        store,
        _ContextMemoryReader(store),
    )


class _RejectingOnceMemoryReader(_ContextMemoryReader):
    def __init__(self, store, assembly) -> None:
        super().__init__(store)
        self._assembly = assembly
        self.detail_reads = 0

    async def load_for_head(self, session_id, head) -> ProductTaskMemory:
        self.detail_reads += 1
        if self.detail_reads == 1:
            await self._assembly.service.reject_task(
                task_id=head.summary.task_id,
                operation_id=f"{head.summary.task_id}-race-reject",
                review_id=head.summary.review_id,
            )
            raise ProductStateError(
                "product-memory-product-head-changed", head.summary.task_id
            )
        return await super().load_for_head(session_id, head)


async def _complete(assembly, task_id: str = "task-completed") -> None:
    await opened(assembly, task_id=task_id)
    await assembly.service.start_task(
        task_id=task_id, operation_id=f"{task_id}-start", receipt=receipt()
    )
    await assembly.service.record_awaiting(
        task_id=task_id,
        operation_id=f"{task_id}-await",
        review_id=f"{task_id}-review-private",
    )
    await assembly.service.complete_task(
        task_id=task_id,
        operation_id=f"{task_id}-complete",
        promotion_id=f"{task_id}-promotion-private",
    )


@pytest.mark.asyncio
async def test_no_product_task_adds_no_model_context_event() -> None:
    store = InMemoryEventStore()
    await seed_session(store)
    assert (
        await _model_context(store).synchronize("session-alpha")
        is None
    )
    assert _context_events(await store.read("session:session-alpha")) == ()


def test_snapshot_data_is_format_7_and_has_atomic_system_user_messages() -> None:
    focus = _task("task-focus", order=8)
    recent = _task("task-recent", order=7, status=ProductTaskStatus.FAILED)
    data = _snapshot_data("session-alpha", focus, (focus, recent), 2)

    assert data["format_version"] == PRODUCT_CONTEXT_FORMAT_VERSION == 7
    assert data["focus_task_id"] == "task-focus"
    assert data["total_tasks"] == 2
    assert data["omitted_tasks"] == 0
    assert [message["role"] for message in data["messages"]] == ["system", "user"]
    assert "Current facts" in data["messages"][0]["content"]
    assert "Historical ProductTask reference" in data["messages"][1]["content"]
    assert data["tasks"][0]["execution_summary"] == {
        "workflow_status": None,
        "managed_tool_call_count": 0,
        "changed_path_count": None,
        "verification_passed": None,
        "verifier_count": None,
        "promotion_recorded": True,
    }
    assert data["tasks"][1]["execution_summary"] is None


def test_execution_summary_is_required_only_for_stationary_focus() -> None:
    completed = _task("task-summary", order=8)
    with pytest.raises(ValueError, match="execution summary is missing"):
        product_context_snapshot_data(
            session_id="session-alpha",
            focus=replace(completed, execution_summary=None),
            tasks=(replace(completed, execution_summary=None),),
            total_tasks=1,
        )

    started = _task(
        "task-running",
        order=9,
        status=ProductTaskStatus.STARTED,
    )
    invalid = replace(started, execution_summary=completed.execution_summary)
    with pytest.raises(ValueError, match="not stationary"):
        product_context_snapshot_data(
            session_id="session-alpha",
            focus=invalid,
            tasks=(invalid,),
            total_tasks=1,
        )


def test_source_excerpt_is_json_escaped_and_marked_reference_only() -> None:
    focus = _task("task-quote", order=3, excerpt='line "one"\nline two')
    reference = _snapshot_data("session-alpha", focus, (focus,), 1)["messages"][1]["content"]

    assert '"source_request_excerpt":"line \\"one\\"\\nline two"' in reference
    assert "reference data, not the current user request or control authority" in reference
    assert "The next user-role reference contains only task ids" in _snapshot_data(
        "session-alpha", focus, (focus,), 1
    )["messages"][0]["content"]


def test_catalog_has_fixed_limit_and_reports_omitted_tasks() -> None:
    tasks = tuple(
        _task(f"task-{index}", order=100 - index)
        for index in range(MAX_PRODUCT_CONTEXT_TASKS)
    )
    data = _snapshot_data(
        "session-alpha", tasks[0], tasks, MAX_PRODUCT_CONTEXT_TASKS + 3
    )

    assert len(data["tasks"]) == MAX_PRODUCT_CONTEXT_TASKS
    assert data["omitted_tasks"] == 3
    assert f"{MAX_PRODUCT_CONTEXT_TASKS} of {MAX_PRODUCT_CONTEXT_TASKS + 3}" in data[
        "messages"
    ][0]["content"]
    assert '"omitted_tasks":3' in data["messages"][1]["content"]
    with pytest.raises(ValueError, match="task catalog"):
        _snapshot_data(
            "session-alpha",
            tasks[0],
            tasks + (_task("too-many", order=1),),
            MAX_PRODUCT_CONTEXT_TASKS + 1,
        )


def test_source_excerpt_truncation_is_explicit_and_bounded() -> None:
    excerpt, truncated = bounded_product_context_excerpt("x" * 500)
    assert truncated
    assert len(excerpt) < 500
    focus = _task(
        "task-truncated",
        order=4,
        excerpt=excerpt,
        excerpt_truncated=truncated,
    )
    data = _snapshot_data("session-alpha", focus, (focus,), 1)
    assert data["tasks"][0]["source_excerpt_truncated"] is True
    assert len(data["tasks"][0]["source_excerpt"]) == len(excerpt)
    assert len(excerpt.encode("utf-8")) <= MAX_PRODUCT_CONTEXT_EXCERPT_JSON_CHARS


class _ContextReadBoundaryStore(InMemoryEventStore):
    def __init__(self) -> None:
        super().__init__()
        self.mode: str | None = None
        self.entered = asyncio.Event()

    async def list_streams(self, *, prefix=None):
        if self.mode == "error" and prefix == "product-task:":
            raise RuntimeError("backend detail must not cross the product boundary")
        if self.mode == "wait" and prefix == "product-task:":
            self.entered.set()
            await asyncio.Event().wait()
        return await super().list_streams(prefix=prefix)


class _CorruptProductEvidenceStore(InMemoryEventStore):
    def __init__(self, field: str, value: str) -> None:
        super().__init__()
        self.field = field
        self.value = value

    async def read(self, stream_id: str):
        events = await super().read(stream_id)
        if stream_id.startswith("product-task:") and events:
            data = dict(events[0].data)
            data[self.field] = self.value
            return (replace(events[0], data=data), *events[1:])
        return events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "error_code"),
    (
        ("origin_message_id", "missing-origin", "product-context-origin-missing"),
        ("origin_turn_id", "turn-2", "product-context-origin-mismatch"),
        (
            "confirmation_message_id",
            "missing-confirmation",
            "product-context-confirmation-missing",
        ),
        (
            "confirmation_turn_id",
            "turn-1",
            "product-context-confirmation-mismatch",
        ),
        (
            "confirmation_session_id",
            "session-other",
            "product-context-session-mismatch",
        ),
    ),
)
async def test_missing_or_mismatched_evidence_fails_closed_without_context_write(
    field: str, value: str, error_code: str
) -> None:
    store = _CorruptProductEvidenceStore(field, value)
    await seed_session(store)
    assembly = await build_assembly(store=store, seed=False)
    try:
        await _complete(assembly, "task-evidence-boundary")
        with pytest.raises(ProductContextError) as captured:
            await _model_context(store).synchronize(
                "session-alpha"
            )
        assert captured.value.code == error_code
        assert _context_events(await store.read("session:session-alpha")) == ()
    finally:
        await assembly.aclose()


@pytest.mark.asyncio
async def test_product_context_read_failure_is_stable_and_writes_nothing() -> None:
    store = _ContextReadBoundaryStore()
    await seed_session(store)
    store.mode = "error"
    with pytest.raises(ProductContextError) as captured:
        await _model_context(store).synchronize(
            "session-alpha"
        )
    assert captured.value.code == "product-context-read-failed"
    assert "backend detail" not in str(captured.value)
    assert _context_events(await store.read("session:session-alpha")) == ()


@pytest.mark.asyncio
async def test_cancelling_product_context_read_remains_cancellation() -> None:
    store = _ContextReadBoundaryStore()
    await seed_session(store)
    store.mode = "wait"
    operation = asyncio.create_task(
        _model_context(store).synchronize("session-alpha")
    )
    await store.entered.wait()
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert _context_events(await store.read("session:session-alpha")) == ()


@pytest.mark.parametrize(
    ("status", "meaning"),
    (
        (ProductTaskStatus.OPENED, "No host-managed execution-start fact"),
        (ProductTaskStatus.ROUTED, "No host-managed execution-start fact"),
        (
            ProductTaskStatus.STARTED,
            "does not assert that a Workflow run-start fact is already durable",
        ),
        (ProductTaskStatus.AWAITING_APPROVAL, "human approval barrier"),
        (ProductTaskStatus.COMPLETED, "durably terminal with status completed"),
        (ProductTaskStatus.REJECTED, "durably terminal after rejection"),
        (ProductTaskStatus.CANCELLED, "durably terminal after cancellation"),
        (ProductTaskStatus.FAILED, "durably terminal after failure"),
        (ProductTaskStatus.ABANDONED, "durably terminal after abandonment"),
    ),
)
def test_every_product_status_has_bounded_specific_meaning(
    status: ProductTaskStatus, meaning: str
) -> None:
    focus = _task("task-meaning", order=1, status=status)
    data = _snapshot_data("session-alpha", focus, (focus,), 1)
    content = data["messages"][0]["content"]
    assert meaning in content
    assert content.startswith("Internal TraceHarness ProductTask evidence.")
    assert "not a complete task inventory" not in content
    assert len(content) <= MAX_PRODUCT_CONTEXT_CONTENT_CHARS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal", "expected_meaning"),
    (
        ("completed", "durably terminal with status completed"),
        ("failed", "durably terminal after failure"),
        ("cancelled", "durably terminal after cancellation"),
    ),
)
async def test_canonical_terminal_status_becomes_safe_context(
    terminal: str, expected_meaning: str
) -> None:
    store = InMemoryEventStore()
    assembly = await build_assembly(store=store)
    task_id = f"task-{terminal}"
    try:
        if terminal == "completed":
            await _complete(assembly, task_id)
        else:
            await opened(assembly, task_id=task_id)
            if terminal == "failed":
                await assembly.service.fail_task(
                    task_id=task_id,
                    operation_id=f"{task_id}-fail",
                    failure_code="private-failure",
                )
            else:
                await assembly.service.cancel_task(
                    task_id=task_id,
                    operation_id=f"{task_id}-cancel",
                    reason_code="private-cancel",
                )
        snapshot = await _model_context(store).synchronize(
            "session-alpha"
        )
        assert snapshot is not None and snapshot.status.value == terminal
        assert expected_meaning in snapshot.messages[0].content
        assert tuple(message.role for message in snapshot.messages) == ("system", "user")
        for secret in ("private-failure", "private-cancel"):
            assert secret not in snapshot.messages[0].content
    finally:
        await assembly.aclose()


@pytest.mark.asyncio
async def test_current_product_context_precedes_conflicting_conversation_history() -> None:
    store = InMemoryEventStore()
    assembly = await build_assembly(store=store)
    try:
        await _complete(assembly, "task-current-state")
        sessions = SessionService(store)
        await sessions.append_session(
            "session-alpha",
            "assistant/message",
            {"content": "The task is still waiting for START.", "tool_calls": []},
        )
        await _model_context(store, sessions).synchronize("session-alpha")
        await sessions.append_session(
            "session-alpha", "user/message", {"content": "Summarize current state."}
        )
        messages = SurfaceProjector().project(await sessions.read_session("session-alpha"))
        assert tuple(message.role for message in messages[:2]) == ("system", "user")
        assert "- Status: completed" in messages[0].content
        assert messages[-2].content == "The task is still waiting for START."
    finally:
        await assembly.aclose()


@pytest.mark.asyncio
async def test_multiple_terminal_tasks_keep_focus_and_recent_history() -> None:
    store = InMemoryEventStore()
    await seed_session(
        store,
        messages=(
            ("message-1", "turn-1"),
            ("message-2", "turn-2"),
            ("message-3", "turn-3"),
            ("message-4", "turn-4"),
        ),
    )
    assembly = await build_assembly(store=store, seed=False)
    try:
        await _complete(assembly, "task-first")
        second_proposal = replace(
            proposal(),
            proposal_id="proposal-2",
            origin_turn_id="turn-3",
            origin_message_id="message-3",
            proposed_turn_id="turn-3",
        )
        await assembly.service.open_task(
            task_id="task-second",
            operation_id="task-second-open",
            proposal=second_proposal,
            confirmation=confirmation(
                proposal_id="proposal-2", turn_id="turn-4", message_id="message-4"
            ),
        )
        await assembly.service.start_task(
            task_id="task-second", operation_id="task-second-start", receipt=receipt()
        )
        await assembly.service.record_awaiting(
            task_id="task-second", operation_id="task-second-await", review_id="review-2"
        )
        await assembly.service.complete_task(
            task_id="task-second", operation_id="task-second-complete", promotion_id="promotion-2"
        )

        snapshot = await _model_context(store).synchronize(
            "session-alpha"
        )
        assert snapshot is not None
        assert snapshot.focus.task_id == "task-second"
        assert snapshot.total_tasks == 2
        assert [task.task_id for task in snapshot.tasks] == ["task-second", "task-first"]
        assert snapshot.omitted_tasks == 0
        assert tuple(message.role for message in snapshot.messages) == ("system", "user")
    finally:
        await assembly.aclose()


@pytest.mark.asyncio
async def test_live_task_focus_is_kept_and_unrelated_session_is_excluded() -> None:
    store = InMemoryEventStore()
    await seed_session(
        store,
        session_id="session-alpha",
        messages=(
            ("message-1", "turn-1"),
            ("message-2", "turn-2"),
            ("message-3", "turn-3"),
            ("message-4", "turn-4"),
        ),
    )
    await seed_session(store, session_id="session-other")
    assembly = await build_assembly(store=store, seed=False)
    try:
        await _complete(assembly, "task-alpha")
        other_proposal = replace(
            proposal(session_id="session-other"),
            proposal_id="proposal-other",
            origin_turn_id="turn-1",
            origin_message_id="message-1",
            proposed_turn_id="turn-1",
        )
        await assembly.service.open_task(
            task_id="task-other",
            operation_id="task-other-open",
            proposal=other_proposal,
            confirmation=confirmation(
                session_id="session-other", proposal_id="proposal-other"
            ),
        )
        live = replace(
            proposal(),
            proposal_id="proposal-live",
            origin_turn_id="turn-3",
            origin_message_id="message-3",
            proposed_turn_id="turn-3",
        )
        await assembly.service.open_task(
            task_id="task-live",
            operation_id="task-live-open",
            proposal=live,
            confirmation=confirmation(
                proposal_id="proposal-live", turn_id="turn-4", message_id="message-4"
            ),
        )
        snapshot = await _model_context(store).synchronize(
            "session-alpha"
        )
        assert snapshot is not None
        assert snapshot.focus.task_id == "task-live"
        assert snapshot.focus.status is ProductTaskStatus.OPENED
        assert [task.task_id for task in snapshot.tasks] == ["task-live", "task-alpha"]
        assert "task-other" not in snapshot.messages[1].content
    finally:
        await assembly.aclose()


@pytest.mark.asyncio
async def test_multiple_live_tasks_fail_closed_without_context_write() -> None:
    store = InMemoryEventStore()
    await seed_session(
        store,
        messages=(
            ("message-1", "turn-1"),
            ("message-2", "turn-2"),
            ("message-3", "turn-3"),
            ("message-4", "turn-4"),
        ),
    )
    assembly = await build_assembly(store=store, seed=False)
    try:
        await opened(assembly, task_id="task-live-one")
        second = replace(
            proposal(),
            proposal_id="proposal-live-two",
            origin_turn_id="turn-3",
            origin_message_id="message-3",
            proposed_turn_id="turn-3",
        )
        await assembly.service.open_task(
            task_id="task-live-two",
            operation_id="task-live-two-open",
            proposal=second,
            confirmation=confirmation(
                proposal_id="proposal-live-two", turn_id="turn-4", message_id="message-4"
            ),
        )
        with pytest.raises(ProductStateError) as captured:
            await _model_context(store).synchronize(
                "session-alpha"
            )
        assert captured.value.code == "product-observation-session-ambiguous"
        assert _context_events(await store.read("session:session-alpha")) == ()
    finally:
        await assembly.aclose()


@pytest.mark.asyncio
async def test_duplicate_task_order_sequence_fails_closed_without_context_write() -> None:
    store = InMemoryEventStore()
    await seed_session(store)
    assembly = await build_assembly(store=store, seed=False)
    try:
        await _complete(assembly, "task-order-one")
        duplicate = replace(proposal(), proposal_id="proposal-order-two")
        await assembly.service.open_task(
            task_id="task-order-two",
            operation_id="task-order-two-open",
            proposal=duplicate,
            confirmation=confirmation(proposal_id="proposal-order-two"),
        )
        with pytest.raises(ProductStateError) as captured:
            await _model_context(store).synchronize(
                "session-alpha"
            )
        assert captured.value.code == "product-context-session-ambiguous"
        assert _context_events(await store.read("session:session-alpha")) == ()
    finally:
        await assembly.aclose()


@pytest.mark.asyncio
async def test_same_catalog_is_idempotent_and_old_snapshot_does_not_win() -> None:
    store = InMemoryEventStore()
    assembly = await build_assembly(store=store)
    try:
        await _complete(assembly, "task-stable")
        context = _model_context(store)
        first = await context.synchronize("session-alpha")
        second = await context.synchronize("session-alpha")
        assert first is not None and second == first
        stream = "session:session-alpha"
        assert len(_context_events(await store.read(stream))) == 1

        older = _task(
            "task-stable", order=1, source_seq=1, status=ProductTaskStatus.OPENED
        )
        stale = _snapshot_data("session-alpha", older, (older,), 1)
        await store.append(
            stream,
            expected_seq=await store.head(stream),
            events=(
                PendingEvent(
                    type=PRODUCT_CONTEXT_SNAPSHOT,
                    data=stale,
                    causation_id=older.source_event_id,
                ),
            ),
        )
        selected = latest_product_context(await store.read(stream))
        assert selected is not None
        assert selected[1].focus.task_id == "task-stable"
        assert selected[1].focus.status is ProductTaskStatus.COMPLETED
    finally:
        await assembly.aclose()


class _ConflictOnceStore(InMemoryEventStore):
    def __init__(self) -> None:
        super().__init__()
        self.conflicted = False

    async def append(self, stream_id, *, expected_seq, events, durability=None):
        kwargs = {} if durability is None else {"durability": durability}
        if (
            not self.conflicted
            and stream_id.startswith("session:")
            and events[0].type == PRODUCT_CONTEXT_SNAPSHOT
        ):
            self.conflicted = True
            await super().append(
                stream_id,
                expected_seq=expected_seq,
                events=(PendingEvent(type="test/interleaving", data={}),),
                **kwargs,
            )
            raise ConcurrencyConflict("deterministic interleaving")
        return await super().append(
            stream_id, expected_seq=expected_seq, events=events, **kwargs
        )


@pytest.mark.asyncio
async def test_session_cas_conflict_restarts_fresh_read_and_retries() -> None:
    store = _ConflictOnceStore()
    assembly = await build_assembly(store=store)
    try:
        await _complete(assembly, "task-cas")
        snapshot = await _model_context(store).synchronize(
            "session-alpha"
        )
        assert store.conflicted
        assert snapshot is not None and snapshot.status is ProductTaskStatus.COMPLETED
        assert len(_context_events(await store.read("session:session-alpha"))) == 1
    finally:
        await assembly.aclose()


@pytest.mark.asyncio
async def test_product_head_change_restarts_the_whole_catalog_join() -> None:
    store = InMemoryEventStore()
    assembly = await build_assembly(store=store)
    task_id = "task-head-retry"
    try:
        await opened(assembly, task_id=task_id)
        await assembly.service.start_task(
            task_id=task_id,
            operation_id=f"{task_id}-start",
            receipt=receipt(),
        )
        await assembly.service.record_awaiting(
            task_id=task_id,
            operation_id=f"{task_id}-await",
            review_id=f"{task_id}-review",
        )
        memory = _RejectingOnceMemoryReader(store, assembly)
        context = ProductModelContext(SessionService(store), store, memory)

        snapshot = await context.synchronize("session-alpha")

        assert memory.detail_reads == 2
        assert snapshot is not None
        assert snapshot.focus.status is ProductTaskStatus.REJECTED
        assert len(_context_events(await store.read("session:session-alpha"))) == 1
    finally:
        await assembly.aclose()


class _CommitThenWaitStore(InMemoryEventStore):
    def __init__(self) -> None:
        super().__init__()
        self.committed = asyncio.Event()
        self.release = asyncio.Event()
        self.waited = False

    async def append(self, stream_id, *, expected_seq, events, durability=None):
        kwargs = {} if durability is None else {"durability": durability}
        result = await super().append(
            stream_id, expected_seq=expected_seq, events=events, **kwargs
        )
        if events[0].type == PRODUCT_CONTEXT_SNAPSHOT and not self.waited:
            self.waited = True
            self.committed.set()
            await self.release.wait()
        return result


class _CommitDifferentJsonTypeStore(InMemoryEventStore):
    def __init__(self) -> None:
        super().__init__()
        self.injected = False

    async def append(self, stream_id, *, expected_seq, events, durability=None):
        kwargs = {} if durability is None else {"durability": durability}
        if (
            not self.injected
            and stream_id.startswith("session:")
            and events[0].type == PRODUCT_CONTEXT_SNAPSHOT
        ):
            self.injected = True
            forged = dict(events[0].data)
            forged["focus_task_id"] = True
            await super().append(
                stream_id,
                expected_seq=expected_seq,
                events=(replace(events[0], data=forged),),
                **kwargs,
            )
            raise RuntimeError("append outcome requires exact reconciliation")
        return await super().append(
            stream_id, expected_seq=expected_seq, events=events, **kwargs
        )


@pytest.mark.asyncio
async def test_commit_reconciliation_is_json_type_sensitive() -> None:
    store = _CommitDifferentJsonTypeStore()
    assembly = await build_assembly(store=store)
    try:
        await _complete(assembly, "task-json-identity")
        with pytest.raises(ProductContextError) as captured:
            await _model_context(store).synchronize(
                "session-alpha"
            )
        assert captured.value.code == "product-context-write-failed"
        assert captured.value.committed is False
    finally:
        await assembly.aclose()


@pytest.mark.asyncio
async def test_cancelled_may_have_committed_append_converges_without_duplicate() -> None:
    store = _CommitThenWaitStore()
    assembly = await build_assembly(store=store)
    try:
        await _complete(assembly, "task-cancel")
        context = _model_context(store)
        operation = asyncio.create_task(context.synchronize("session-alpha"))
        await store.committed.wait()
        operation.cancel()
        store.release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert len(_context_events(await store.read("session:session-alpha"))) == 1
        recovered = await context.synchronize("session-alpha")
        assert recovered is not None and recovered.status is ProductTaskStatus.COMPLETED
        assert len(_context_events(await store.read("session:session-alpha"))) == 1
    finally:
        await assembly.aclose()


@pytest.mark.asyncio
async def test_legacy_formats_one_through_six_are_rejected_without_rewrite() -> None:
    store = InMemoryEventStore()
    await seed_session(store)
    focus = _task("task-legacy", order=6)
    current = _snapshot_data("session-alpha", focus, (focus,), 1)
    stream = "session:session-alpha"
    for version in range(1, 7):
        legacy = dict(current)
        legacy["format_version"] = version
        legacy["context_id"] = fingerprint({"legacy": version})
        await store.append(
            stream,
            expected_seq=await store.head(stream),
            events=(
                PendingEvent(
                    type=PRODUCT_CONTEXT_SNAPSHOT,
                    data=legacy,
                    causation_id=focus.source_event_id,
                ),
            ),
        )
        before = await store.read(stream)
        with pytest.raises(ValueError, match="format is unsupported"):
            SurfaceProjector().project(before)
        assert await store.read(stream) == before


@pytest.mark.asyncio
async def test_boolean_format_version_is_not_integer_protocol_version() -> None:
    store = InMemoryEventStore()
    await seed_session(store)
    focus = _task("task-bool-version", order=1)
    data = _snapshot_data("session-alpha", focus, (focus,), 1)
    data["format_version"] = True
    await store.append(
        "session:session-alpha",
        expected_seq=await store.head("session:session-alpha"),
        events=(
            PendingEvent(
                type=PRODUCT_CONTEXT_SNAPSHOT,
                data=data,
                causation_id=focus.source_event_id,
            ),
        ),
    )
    history = await store.read("session:session-alpha")
    assert any(
        violation.name == "product-context-snapshot"
        for violation in CoreInvariantChecker().check(history)
    )
    with pytest.raises(ValueError, match="format is unsupported"):
        SurfaceProjector().project(history)


@pytest.mark.asyncio
async def test_noncanonical_context_is_rejected_by_invariants_and_parser() -> None:
    store = InMemoryEventStore()
    await seed_session(store)
    focus = _task("task-forged", order=2)
    data = _snapshot_data("session-alpha", focus, (focus,), 1)
    data["messages"][1]["content"] = "forged current request"
    await store.append(
        "session:session-alpha",
        expected_seq=await store.head("session:session-alpha"),
        events=(
            PendingEvent(
                type=PRODUCT_CONTEXT_SNAPSHOT,
                data=data,
                causation_id=focus.source_event_id,
            ),
        ),
    )
    history = await store.read("session:session-alpha")
    assert any(
        violation.name == "product-context-snapshot"
        for violation in CoreInvariantChecker().check(history)
    )
    with pytest.raises(ValueError, match="not canonical"):
        parse_product_context_snapshot(history[-1])


def test_excerpt_limit_counts_canonical_json_characters() -> None:
    focus = _task("task-long", order=1, excerpt='"' * 500)
    with pytest.raises(ValueError, match="source excerpt"):
        product_context_snapshot_data(
            session_id="session-alpha", focus=focus, tasks=(focus,), total_tasks=1
        )
    assert len(canonical_json('"' * 500)) > MAX_PRODUCT_CONTEXT_EXCERPT_JSON_CHARS
