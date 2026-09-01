"""ProductTask status frozen into one exact, replayable model message.

These tests keep the ProductTask stream as the authority.  They exercise the
public synchronizer against real ProductTask facts and use direct Session
appends only for protocol rejection and race-order counterexamples.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
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
from traceh.api.json_types import fingerprint
from traceh.api.product import ProductTaskStatus
from traceh.product.context import ProductModelContext
from traceh.product.errors import ProductContextError, ProductEvidenceError
from traceh.session.event_store import ConcurrencyConflict, InMemoryEventStore
from traceh.session.invariants import CoreInvariantChecker
from traceh.session.product_context import (
    MAX_PRODUCT_CONTEXT_CONTENT_CHARS,
    PRODUCT_CONTEXT_FORMAT_VERSION,
    PRODUCT_CONTEXT_SNAPSHOT,
    product_context_snapshot_data,
)
from traceh.session.service import SessionService
from traceh.session.surface import SurfaceProjector


def _context_events(events):
    return tuple(event for event in events if event.type == PRODUCT_CONTEXT_SNAPSHOT)


async def _complete(assembly, task_id: str) -> None:
    await opened(assembly, task_id=task_id)
    await assembly.service.start_task(
        task_id=task_id,
        operation_id=f"{task_id}-start",
        receipt=receipt(),
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


async def test_no_product_task_adds_no_model_context_event() -> None:
    store = InMemoryEventStore()
    await seed_session(store)
    sessions = SessionService(store)

    assert await ProductModelContext(sessions, store).synchronize("session-alpha") is None

    history = await sessions.read_session("session-alpha")
    assert _context_events(history) == ()
    assert all(message.role != "system" for message in SurfaceProjector().project(history))


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


async def test_product_context_read_failure_is_stable_and_writes_nothing() -> None:
    store = _ContextReadBoundaryStore()
    await seed_session(store)
    store.mode = "error"
    sessions = SessionService(store)

    with pytest.raises(ProductContextError) as captured:
        await ProductModelContext(sessions, store).synchronize("session-alpha")

    assert captured.value.code == "product-context-read-failed"
    assert "backend detail" not in str(captured.value)
    assert _context_events(await sessions.read_session("session-alpha")) == ()


async def test_cancelling_product_context_read_remains_cancellation() -> None:
    store = _ContextReadBoundaryStore()
    await seed_session(store)
    store.mode = "wait"
    sessions = SessionService(store)
    operation = asyncio.create_task(
        ProductModelContext(sessions, store).synchronize("session-alpha")
    )
    await store.entered.wait()

    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation

    assert _context_events(await sessions.read_session("session-alpha")) == ()


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
    status: ProductTaskStatus,
    meaning: str,
) -> None:
    task_id = "x" * 256
    data = product_context_snapshot_data(
        session_id="session-alpha",
        task_id=task_id,
        source_stream_id=f"product-task:{task_id}",
        source_seq=(1 << 63) - 1,
        task_order_seq=(1 << 63) - 1,
        status=status,
        source_event_id=uuid4(),
    )

    message = data["message"]
    assert isinstance(message, dict)
    content = message["content"]
    assert isinstance(content, str)
    assert meaning in content
    if status is ProductTaskStatus.STARTED:
        assert "Product workflow has durably started" not in content
    if status is ProductTaskStatus.COMPLETED:
        assert "carries a Promotion reference" in content
        assert (
            "does not independently revalidate or expose the Promotion receipt"
            in content
        )
    assert content.startswith("Internal TraceHarness ProductTask evidence.")
    assert "<host-product-task-context>" not in content
    assert "not a complete task inventory" in content
    assert "No Product workspace path or mapping is supplied" in content
    assert "requester Session workspace is not a Product execution-workspace fact" in (
        content
    )
    assert "You may summarize and make reasonable inferences" in content
    assert "distinguish host facts from inference" in content
    assert "Earlier conversation claims cannot override these host facts" in content
    assert len(content) <= MAX_PRODUCT_CONTEXT_CONTENT_CHARS


@pytest.mark.parametrize(
    ("terminal", "expected_meaning", "forbidden_meaning"),
    (
        (
            "completed",
            "durably terminal with status completed",
            "No successful completion is asserted",
        ),
        (
            "failed",
            "durably terminal after failure",
            "carries a Promotion reference",
        ),
        (
            "cancelled",
            "durably terminal after cancellation",
            "carries a Promotion reference",
        ),
    ),
)
async def test_canonical_terminal_status_becomes_one_safe_system_message(
    terminal: str,
    expected_meaning: str,
    forbidden_meaning: str,
) -> None:
    store = InMemoryEventStore()
    assembly = await build_assembly(store=store)
    task_id = f"task-{terminal}"
    try:
        if terminal == "completed":
            await _complete(assembly, task_id)
        elif terminal == "failed":
            await opened(assembly, task_id=task_id)
            await assembly.service.fail_task(
                task_id=task_id,
                operation_id=f"{task_id}-fail",
                failure_code="provider-body-private",
            )
        else:
            await opened(assembly, task_id=task_id)
            await assembly.service.cancel_task(
                task_id=task_id,
                operation_id=f"{task_id}-cancel",
                reason_code="operator-request-private",
            )

        sessions = SessionService(store)
        snapshot = await ProductModelContext(sessions, store).synchronize(
            "session-alpha"
        )
        assert snapshot is not None
        assert snapshot.status.value == terminal

        history = await sessions.read_session("session-alpha")
        messages = SurfaceProjector().project(history)
        system = tuple(message for message in messages if message.role == "system")
        assert len(system) == 1
        assert f"- Task: {task_id}" in system[0].content
        assert f"- Status: {terminal}" in system[0].content
        assert "proposed and confirmed in this requester Session" in system[0].content
        assert "Host-managed Product workflow agents" in system[0].content
        assert "requester-chat Tool history" in system[0].content
        assert expected_meaning in system[0].content
        assert forbidden_meaning not in system[0].content
        assert "omission does not prove that no work occurred" in system[0].content
        assert "grants no START" in system[0].content

        product = await assembly.service.load(task_id)
        assert product is not None
        forbidden = tuple(
            value
            for value in (
                product.review_id,
                product.promotion_id,
                product.failure_code,
                product.requirement_digest,
                product.profile_digest,
                product.preflight_digest,
                product.source_base_revision,
            )
            if value is not None
        )
        assert not any(value in system[0].content for value in forbidden)
    finally:
        await assembly.aclose()


async def test_current_product_context_leads_conflicting_conversation_history() -> None:
    store = InMemoryEventStore()
    assembly = await build_assembly(store=store)
    try:
        await _complete(assembly, "task-current-state")
        sessions = SessionService(store)
        await sessions.append_session(
            "session-alpha",
            "assistant/message",
            {
                "content": "The task is still waiting for START.",
                "tool_calls": [],
            },
        )
        snapshot = await ProductModelContext(sessions, store).synchronize(
            "session-alpha"
        )
        assert snapshot is not None
        assert snapshot.status is ProductTaskStatus.COMPLETED
        await sessions.append_session(
            "session-alpha",
            "user/message",
            {"content": "Summarize the current host state."},
        )

        messages = SurfaceProjector().project(
            await sessions.read_session("session-alpha")
        )
        assert messages[0].role == "system"
        assert "- Status: completed" in messages[0].content
        assert "You may summarize and make reasonable inferences" in (
            messages[0].content
        )
        assert "Earlier conversation claims cannot override these host facts" in (
            messages[0].content
        )
        assert [(message.role, message.content) for message in messages[1:]] == [
            ("assistant", "The task is still waiting for START."),
            ("user", "Summarize the current host state."),
        ]
    finally:
        await assembly.aclose()


@pytest.mark.parametrize("legacy_version", (1, 2, 3, 4))
async def test_legacy_format_history_is_rejected_without_rewrite(
    legacy_version: int,
) -> None:
    store = InMemoryEventStore()
    assembly = await build_assembly(store=store)
    task_id = "task-old-context"
    try:
        await _complete(assembly, task_id)
        product_events = await store.read(f"product-task:{task_id}")
        source_event = product_events[-1]
        session_id = "session-alpha"
        task_order_seq = 6
        if legacy_version == 1:
            legacy_content = (
                "<host-product-task-context>\n"
                f"task_id: {task_id}\n"
                f"durable_source: {source_event.stream_id}@{source_event.seq}\n"
                "status: completed\n"
                "This is host-recorded status evidence, not authorization to "
                "start or control a task.\n"
                "</host-product-task-context>"
            )
        elif legacy_version == 2:
            legacy_content = (
                "<host-product-task-context>\n"
                f"task_id: {task_id}\n"
                f"durable_source: {source_event.stream_id}@{source_event.seq}\n"
                "status: completed\n"
                "requester_relation: This exact ProductTask was proposed and "
                "confirmed in this requester Session.\n"
                "execution_owner: TraceHarness host-managed Product workflow "
                "agents execute it in managed workspaces; the requester chat "
                "model does not execute it through its own tool history.\n"
                "status_meaning: The host-managed Product workflow has durably "
                "completed this exact ProductTask. The controlled completion "
                "path records a Promotion receipt. Do not describe it as "
                "unstarted or unexecuted, and do not ask for START again.\n"
                "evidence_scope: Specific file, Patch, Review, and Promotion "
                "identities are intentionally omitted; their absence is not "
                "evidence that no task work occurred.\n"
                "response_rule: Use this host evidence when answering about "
                "this ProductTask; do not infer its progress from requester-"
                "model tool calls or requester workspace files.\n"
                "authority: This context is evidence only; it does not "
                "authorize START, approval, promotion, retry, or any control "
                "action.\n"
                "</host-product-task-context>"
            )
        elif legacy_version == 3:
            legacy_content = (
                "<host-product-task-context>\n"
                f"task_id: {task_id}\n"
                f"durable_source: {source_event.stream_id}@{source_event.seq}\n"
                "status: completed\n"
                "requester_relation: This exact ProductTask was proposed and "
                "confirmed in this requester Session.\n"
                "execution_owner: Any task execution is performed by "
                "TraceHarness host-managed Product workflow agents in managed "
                "workspaces, not through the requester chat model's own tool "
                "history.\n"
                "status_meaning: This exact ProductTask is durably terminal "
                "with status completed and carries a Promotion reference. On "
                "the normal controlled path, this Product status is recorded "
                "after promotion. This bounded Product-only context does not "
                "independently revalidate or expose the Promotion receipt. Do "
                "not describe this ProductTask as waiting for START, and do "
                "not ask for START again.\n"
                "evidence_scope: Specific file, Patch, Review, and Promotion "
                "identities are intentionally omitted; their absence is not "
                "evidence that no task work occurred.\n"
                "response_rule: Use this host evidence when answering about "
                "this ProductTask; do not infer its progress from requester-"
                "model tool calls or requester workspace files.\n"
                "authority: This context is evidence only; it does not "
                "authorize START, approval, promotion, retry, or any control "
                "action.\n"
                "</host-product-task-context>"
            )
        else:
            legacy_content = (
                "Internal TraceHarness ProductTask evidence. Answer naturally; "
                "do not quote this block or its labels.\n"
                "Facts\n"
                f"- Task: {task_id}\n"
                f"- Durable Product head: {source_event.stream_id}@"
                f"{source_event.seq}\n"
                "- Status: completed\n"
                "- Relation: This exact ProductTask was proposed and confirmed "
                "in this requester Session.\n"
                "- Execution: Host-managed Product workflow agents perform "
                "Product work; requester-chat Tool history neither performs nor "
                "refutes it.\n"
                "- Meaning: This exact ProductTask is durably terminal with "
                "status completed and carries a Promotion reference. On the "
                "normal controlled path, this Product status is recorded after "
                "promotion. This bounded Product-only context does not "
                "independently revalidate or expose the Promotion receipt. Do "
                "not describe this ProductTask as waiting for START, and do not "
                "ask for START again.\n"
                "Limits\n"
                "- This selected task is not a complete task inventory; missing "
                "ids do not prove that no other tasks exist.\n"
                "- No Product workspace path or mapping is supplied. A requester "
                "Session workspace is not a Product execution-workspace fact.\n"
                "- Files, commands, tests, outputs, Patch, Review, and Promotion "
                "identities are omitted; omission does not prove that no work "
                "occurred.\n"
                "Use\n"
                "- You may summarize and make reasonable inferences, but "
                "distinguish host facts from inference and do not invent omitted "
                "specifics.\n"
                "- This evidence grants no START, approval, promotion, retry, or "
                "other control authority."
            )
        old_data = {
            "context_id": fingerprint(
                {
                    "purpose": PRODUCT_CONTEXT_SNAPSHOT,
                    "format_version": legacy_version,
                    "session_id": session_id,
                    "task_id": task_id,
                    "source_stream_id": source_event.stream_id,
                    "source_seq": source_event.seq,
                    "source_event_id": str(source_event.event_id),
                    "task_order_seq": task_order_seq,
                    "status": ProductTaskStatus.COMPLETED.value,
                }
            ),
            "format_version": legacy_version,
            "task_id": task_id,
            "source_stream_id": source_event.stream_id,
            "source_seq": source_event.seq,
            "task_order_seq": task_order_seq,
            "status": ProductTaskStatus.COMPLETED.value,
            "message": {
                "role": "system",
                "content": legacy_content,
            },
        }
        current_data = product_context_snapshot_data(
            session_id=session_id,
            task_id=task_id,
            source_stream_id=source_event.stream_id,
            source_seq=source_event.seq,
            task_order_seq=task_order_seq,
            status=ProductTaskStatus.COMPLETED,
            source_event_id=source_event.event_id,
        )
        assert current_data["format_version"] == PRODUCT_CONTEXT_FORMAT_VERSION == 5
        assert current_data["context_id"] != old_data["context_id"]
        session_stream = f"session:{session_id}"
        await store.append(
            session_stream,
            expected_seq=await store.head(session_stream),
            events=(
                PendingEvent(
                    type=PRODUCT_CONTEXT_SNAPSHOT,
                    data=old_data,
                    causation_id=source_event.event_id,
                ),
            ),
        )
        before = await store.read(session_stream)

        with pytest.raises(ProductEvidenceError) as captured:
            await ProductModelContext(SessionService(store), store).synchronize(
                session_id
            )

        assert captured.value.code == "product-session-unreadable"
        assert await store.read(session_stream) == before
        assert len(_context_events(before)) == 1
        assert _context_events(before)[0].data["format_version"] == legacy_version
    finally:
        await assembly.aclose()


async def test_same_product_head_is_idempotent() -> None:
    store = InMemoryEventStore()
    assembly = await build_assembly(store=store)
    try:
        await _complete(assembly, "task-idempotent")
        sessions = SessionService(store)
        context = ProductModelContext(sessions, store)

        first = await context.synchronize("session-alpha")
        # A fresh bridge/service pair represents process restart: replayed
        # evidence, not an in-memory cache, must make the write idempotent.
        second = await ProductModelContext(
            SessionService(store), store
        ).synchronize("session-alpha")

        assert first is not None
        assert second == first
        assert len(_context_events(await sessions.read_session("session-alpha"))) == 1
    finally:
        await assembly.aclose()


async def test_new_task_and_new_head_supersede_older_context() -> None:
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
    sessions = SessionService(store)
    context = ProductModelContext(sessions, store)
    try:
        await _complete(assembly, "task-older")
        older = await context.synchronize("session-alpha")
        assert older is not None
        assert older.status is ProductTaskStatus.COMPLETED

        newer_proposal = replace(
            proposal(),
            proposal_id="proposal-2",
            origin_turn_id="turn-3",
            origin_message_id="message-3",
            proposed_turn_id="turn-3",
        )
        newer_confirmation = confirmation(
            proposal_id="proposal-2",
            turn_id="turn-4",
            message_id="message-4",
        )
        await assembly.service.open_task(
            task_id="task-newer",
            operation_id="task-newer-open",
            proposal=newer_proposal,
            confirmation=newer_confirmation,
        )
        opened_snapshot = await context.synchronize("session-alpha")
        assert opened_snapshot is not None
        assert opened_snapshot.task_id == "task-newer"
        assert opened_snapshot.status is ProductTaskStatus.OPENED

        await assembly.service.fail_task(
            task_id="task-newer",
            operation_id="task-newer-fail",
            failure_code="stable-failure-code",
        )
        failed_snapshot = await context.synchronize("session-alpha")
        assert failed_snapshot is not None
        assert failed_snapshot.task_id == "task-newer"
        assert failed_snapshot.status is ProductTaskStatus.FAILED

        history = await sessions.read_session("session-alpha")
        assert len(_context_events(history)) == 3
        system = tuple(
            message
            for message in SurfaceProjector().project(history)
            if message.role == "system"
        )
        assert len(system) == 1
        assert "- Task: task-newer" in system[0].content
        assert "- Status: failed" in system[0].content
        assert "task-older" not in system[0].content
    finally:
        await assembly.aclose()


async def test_late_append_of_an_older_head_cannot_roll_surface_back() -> None:
    store = InMemoryEventStore()
    assembly = await build_assembly(store=store)
    try:
        await _complete(assembly, "task-race")
        product_events = await store.read("product-task:task-race")
        opened_event = product_events[0]
        completed_event = product_events[-1]
        stream = "session:session-alpha"
        head = await store.head(stream)

        completed = product_context_snapshot_data(
            session_id="session-alpha",
            task_id="task-race",
            source_stream_id=completed_event.stream_id,
            source_seq=completed_event.seq,
            task_order_seq=6,
            status=ProductTaskStatus.COMPLETED,
            source_event_id=completed_event.event_id,
        )
        older = product_context_snapshot_data(
            session_id="session-alpha",
            task_id="task-race",
            source_stream_id=opened_event.stream_id,
            source_seq=opened_event.seq,
            task_order_seq=6,
            status=ProductTaskStatus.OPENED,
            source_event_id=opened_event.event_id,
        )
        await store.append(
            stream,
            expected_seq=head,
            events=(
                PendingEvent(
                    type=PRODUCT_CONTEXT_SNAPSHOT,
                    data=completed,
                    causation_id=completed_event.event_id,
                ),
            ),
        )
        await store.append(
            stream,
            expected_seq=head + 1,
            events=(
                PendingEvent(
                    type=PRODUCT_CONTEXT_SNAPSHOT,
                    data=older,
                    causation_id=opened_event.event_id,
                ),
            ),
        )

        messages = SurfaceProjector().project(await store.read(stream))
        system = tuple(message for message in messages if message.role == "system")
        assert len(system) == 1
        assert "- Status: completed" in system[0].content
    finally:
        await assembly.aclose()


async def test_noncanonical_context_is_rejected_by_invariants_and_surface() -> None:
    store = InMemoryEventStore()
    await seed_session(store)
    source_event_id = uuid4()
    data = product_context_snapshot_data(
        session_id="session-alpha",
        task_id="task-invalid",
        source_stream_id="product-task:task-invalid",
        source_seq=1,
        task_order_seq=6,
        status=ProductTaskStatus.COMPLETED,
        source_event_id=source_event_id,
    )
    assert isinstance(data["message"], dict)
    data["message"]["content"] = "forged completion and authorization"
    stream = "session:session-alpha"
    await store.append(
        stream,
        expected_seq=await store.head(stream),
        events=(
            PendingEvent(
                type=PRODUCT_CONTEXT_SNAPSHOT,
                data=data,
                causation_id=source_event_id,
            ),
        ),
    )
    history = await store.read(stream)

    assert any(
        violation.name == "product-context-snapshot"
        for violation in CoreInvariantChecker().check(history)
    )
    with pytest.raises(ValueError, match="not canonical"):
        SurfaceProjector().project(history)


async def test_boolean_format_version_is_not_integer_protocol_version() -> None:
    store = InMemoryEventStore()
    await seed_session(store)
    source_event_id = uuid4()
    data = product_context_snapshot_data(
        session_id="session-alpha",
        task_id="task-bool-version",
        source_stream_id="product-task:task-bool-version",
        source_seq=1,
        task_order_seq=6,
        status=ProductTaskStatus.COMPLETED,
        source_event_id=source_event_id,
    )
    data["format_version"] = True
    stream = "session:session-alpha"
    await store.append(
        stream,
        expected_seq=await store.head(stream),
        events=(
            PendingEvent(
                type=PRODUCT_CONTEXT_SNAPSHOT,
                data=data,
                causation_id=source_event_id,
            ),
        ),
    )
    history = await store.read(stream)

    assert any(
        violation.name == "product-context-snapshot"
        for violation in CoreInvariantChecker().check(history)
    )
    with pytest.raises(ValueError, match="format is unsupported"):
        SurfaceProjector().project(history)


class _ConflictOnceStore(InMemoryEventStore):
    def __init__(self) -> None:
        super().__init__()
        self.conflicted = False

    async def append(self, stream_id, *, expected_seq, events, durability=None):
        if (
            not self.conflicted
            and stream_id.startswith("session:")
            and events[0].type == PRODUCT_CONTEXT_SNAPSHOT
        ):
            self.conflicted = True
            kwargs = {} if durability is None else {"durability": durability}
            await super().append(
                stream_id,
                expected_seq=expected_seq,
                events=(PendingEvent(type="test/interleaving", data={}),),
                **kwargs,
            )
            raise ConcurrencyConflict("deterministic interleaving")
        kwargs = {} if durability is None else {"durability": durability}
        return await super().append(
            stream_id,
            expected_seq=expected_seq,
            events=events,
            **kwargs,
        )


async def test_session_cas_conflict_restarts_fresh_read_and_retries() -> None:
    store = _ConflictOnceStore()
    assembly = await build_assembly(store=store)
    try:
        await _complete(assembly, "task-cas")
        sessions = SessionService(store)

        snapshot = await ProductModelContext(sessions, store).synchronize(
            "session-alpha"
        )

        assert store.conflicted
        assert snapshot is not None
        assert snapshot.status is ProductTaskStatus.COMPLETED
        assert len(_context_events(await sessions.read_session("session-alpha"))) == 1
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
            stream_id,
            expected_seq=expected_seq,
            events=events,
            **kwargs,
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
            forged_data = dict(events[0].data)
            forged_data["source_seq"] = True
            await super().append(
                stream_id,
                expected_seq=expected_seq,
                events=(replace(events[0], data=forged_data),),
                **kwargs,
            )
            raise RuntimeError("append outcome requires exact reconciliation")
        return await super().append(
            stream_id,
            expected_seq=expected_seq,
            events=events,
            **kwargs,
        )


async def test_commit_reconciliation_is_json_type_sensitive() -> None:
    store = _CommitDifferentJsonTypeStore()
    assembly = await build_assembly(store=store)
    try:
        await _complete(assembly, "task-json-identity")
        sessions = SessionService(store)

        with pytest.raises(ProductContextError) as captured:
            await ProductModelContext(sessions, store).synchronize("session-alpha")

        assert captured.value.code == "product-context-write-failed"
        assert captured.value.committed is False
    finally:
        await assembly.aclose()


async def test_cancelled_may_have_committed_append_converges_without_duplicate() -> None:
    store = _CommitThenWaitStore()
    assembly = await build_assembly(store=store)
    try:
        await _complete(assembly, "task-cancel")
        sessions = SessionService(store)
        context = ProductModelContext(sessions, store)
        operation = asyncio.create_task(context.synchronize("session-alpha"))
        await store.committed.wait()

        operation.cancel()
        store.release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation

        assert len(_context_events(await sessions.read_session("session-alpha"))) == 1
        recovered = await context.synchronize("session-alpha")
        assert recovered is not None
        assert recovered.status is ProductTaskStatus.COMPLETED
        assert len(_context_events(await sessions.read_session("session-alpha"))) == 1
    finally:
        await assembly.aclose()
