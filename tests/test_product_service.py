"""v0.7-F1: the host-owned ProductTask writer.

Every case drives the public service. Concurrency is expressed with `Event`
gates rather than sleeps, so a passing run means the window was actually
reached rather than merely likely to have been.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from product_fixtures import (
    CONFIRM_MESSAGE,
    CONFIRM_TURN,
    ORIGIN_MESSAGE,
    ORIGIN_TURN,
    Gate,
    build_assembly,
    confirmation,
    opened,
    preflight,
    proposal,
    receipt,
    seed_session,
)

from traceh.api.events import PendingEvent
from traceh.api.product import (
    PRODUCT_TASK_OPENED,
    ProductTaskStatus,
    ProductTaskViewStatus,
    RequestedTaskMode,
    ResolvedTaskMode,
    TaskRouting,
)
from traceh.api.workflow import WorkflowStatus
from traceh.product import (
    ProductEvidenceError,
    ProductInputError,
    ProductOperationConflictError,
    ProductServiceClosedError,
    ProductStateError,
    ProductStreamConflictError,
    ProductTaskService,
    ProductWriteError,
    SessionEvidenceReader,
    product_task_stream,
    validate_product_task,
)
from traceh.session.event_store import (
    ConcurrencyConflict,
    Durability,
    InMemoryEventStore,
)


class _Wrapping:
    """A store that forwards everything and lets one append misbehave."""

    def __init__(self) -> None:
        self.inner = InMemoryEventStore()
        self.fail_on: str | None = None
        self.mode = "raise"
        self.fired = False
        self.read_fails = False

    async def append(self, stream_id, *, expected_seq, events, durability=None):
        kwargs = {} if durability is None else {"durability": durability}
        target = self.fail_on is not None and events[0].type == self.fail_on
        if target and not self.fired and self.mode == "before":
            self.fired = True
            raise OSError("append refused before committing")
        result = await self.inner.append(
            stream_id, expected_seq=expected_seq, events=events, **kwargs
        )
        if target and not self.fired:
            self.fired = True
            if self.mode == "raise":
                raise OSError("append failed after committing")
            if self.mode == "cancel":
                raise asyncio.CancelledError
        return result

    async def read(self, stream_id, *, from_seq=1):
        # Only after the append misbehaved: the point is that reconciliation
        # cannot answer, not that the task could never be read at all.
        if self.read_fails and self.fired and stream_id.startswith("product-task:"):
            raise OSError("stream unreadable")
        return await self.inner.read(stream_id, from_seq=from_seq)

    async def head(self, stream_id):
        return await self.inner.head(stream_id)

    async def list_streams(self, *, prefix=None):
        return await self.inner.list_streams(prefix=prefix)


class _SessionReadFailureStore(InMemoryEventStore):
    """A real Store whose Session read can fail after fixture setup."""

    def __init__(self) -> None:
        super().__init__()
        self.session_error: BaseException | None = None

    async def read(self, stream_id, *, from_seq=1):
        if self.session_error is not None and stream_id.startswith("session:"):
            raise self.session_error
        return await super().read(stream_id, from_seq=from_seq)


# ------------------------------------------------------- confirmation evidence


async def test_opening_requires_the_session_to_show_both_messages() -> None:
    """A DTO can carry ids; only the Session can show they happened."""

    assembly = await build_assembly(seed=False)
    with pytest.raises(ProductEvidenceError) as raised:
        await opened(assembly)
    assert raised.value.code == "product-origin-message-unknown"
    assert await assembly.service.load("task-1") is None
    await assembly.aclose()


async def test_a_confirmation_naming_the_wrong_turn_is_refused() -> None:
    store = InMemoryEventStore()
    await seed_session(
        store, messages=((ORIGIN_MESSAGE, ORIGIN_TURN), (CONFIRM_MESSAGE, "turn-99"))
    )
    assembly = await build_assembly(store=store, seed=False)
    with pytest.raises(ProductEvidenceError) as raised:
        await opened(assembly)
    assert raised.value.code == "product-confirmation-turn-mismatch"
    await assembly.aclose()


async def test_an_unclaimed_message_is_not_evidence() -> None:
    """Acceptance alone does not tie a message to a Turn."""

    store = InMemoryEventStore()
    await seed_session(store, claim=False)
    assembly = await build_assembly(store=store, seed=False)
    with pytest.raises(ProductEvidenceError) as raised:
        await opened(assembly)
    assert raised.value.code == "product-origin-message-unknown"
    await assembly.aclose()


async def test_a_session_that_was_never_created_is_not_evidence() -> None:
    store = InMemoryEventStore()
    await seed_session(store, created=False)
    assembly = await build_assembly(store=store, seed=False)
    with pytest.raises(ProductEvidenceError):
        await opened(assembly)
    await assembly.aclose()


async def test_only_plain_user_messages_can_confirm_a_product_task() -> None:
    """An Agent-authored Turn is durable, but it is not a human decision."""

    store = InMemoryEventStore()
    await seed_session(store, source="agent-child")
    assembly = await build_assembly(store=store, seed=False)
    with pytest.raises(ProductEvidenceError) as raised:
        await opened(assembly)
    assert raised.value.code == "product-origin-message-unknown"
    assert await assembly.service.load("task-1") is None
    await assembly.aclose()


async def test_malformed_session_history_cannot_prove_confirmation() -> None:
    """Presence of familiar type names is not a valid Session protocol."""

    store = InMemoryEventStore()
    stream = "session:session-alpha"
    malformed = (
        ("inbox/claimed", {"message_id": ORIGIN_MESSAGE, "turn_id": ORIGIN_TURN}),
        (
            "inbox/accepted",
            {
                "message_id": ORIGIN_MESSAGE,
                "source": "user",
                "content": "requirement",
                "target": "new_turn",
            },
        ),
        ("inbox/claimed", {"message_id": CONFIRM_MESSAGE, "turn_id": CONFIRM_TURN}),
        (
            "inbox/accepted",
            {
                "message_id": CONFIRM_MESSAGE,
                "source": "user",
                "content": "confirmation",
                "target": "new_turn",
            },
        ),
        (
            "session/created",
            {
                "session_id": "session-alpha",
                "workspace": "workspace-fixture",
                "metadata": {},
            },
        ),
    )
    for seq, (event_type, data) in enumerate(malformed):
        await store.append(
            stream,
            expected_seq=seq,
            events=(PendingEvent(event_type, data, schema_version=999),),
        )
    assembly = await build_assembly(store=store, seed=False)
    with pytest.raises(ProductEvidenceError) as raised:
        await opened(assembly)
    assert raised.value.code == "product-session-unreadable"
    assert await assembly.service.load("task-1") is None
    await assembly.aclose()


@pytest.mark.parametrize("hostile", [False, True])
async def test_only_an_exact_plain_new_turn_acceptance_can_confirm(
    hostile: bool,
) -> None:
    """Payload comparison cannot execute a caller-controlled ``str`` method."""

    class PretendsToBeNewTurn(str):
        comparisons = 0

        def __eq__(self, other: object) -> bool:  # noqa: D105
            type(self).comparisons += 1
            del other
            return True

        def __ne__(self, other: object) -> bool:  # noqa: D105
            type(self).comparisons += 1
            del other
            return False

        __hash__ = str.__hash__

    target: object = PretendsToBeNewTurn("next_step") if hostile else "next_step"
    store = InMemoryEventStore()
    events = (
        PendingEvent(
            "session/created",
            {
                "session_id": "session-alpha",
                "workspace": "workspace-fixture",
                "metadata": {},
            },
        ),
        PendingEvent(
            "inbox/accepted",
            {
                "message_id": ORIGIN_MESSAGE,
                "source": "user",
                "content": "example requirement",
                "target": "new_turn",
            },
        ),
        PendingEvent(
            "inbox/claimed",
            {"message_id": ORIGIN_MESSAGE, "turn_id": ORIGIN_TURN},
        ),
        PendingEvent(
            "turn/start",
            {"turn_id": ORIGIN_TURN, "message_id": ORIGIN_MESSAGE},
        ),
        PendingEvent("turn/end", {"turn_id": ORIGIN_TURN, "reason": "completed"}),
        PendingEvent(
            "inbox/accepted",
            {
                "message_id": CONFIRM_MESSAGE,
                "source": "user",
                "content": "example confirmation",
                "target": target,
            },
        ),
        PendingEvent(
            "inbox/claimed",
            {"message_id": CONFIRM_MESSAGE, "turn_id": CONFIRM_TURN},
        ),
        PendingEvent(
            "turn/start",
            {"turn_id": CONFIRM_TURN, "message_id": CONFIRM_MESSAGE},
        ),
    )
    await store.append("session:session-alpha", expected_seq=0, events=events)
    assembly = await build_assembly(store=store, seed=False)

    with pytest.raises(ProductEvidenceError) as raised:
        await opened(assembly)

    assert raised.value.code == "product-session-unreadable"
    assert PretendsToBeNewTurn.comparisons == 0
    assert await store.head(product_task_stream("task-1")) == 0
    await assembly.aclose()


async def test_session_store_read_failure_has_one_stable_product_outcome() -> None:
    store = _SessionReadFailureStore()
    await seed_session(store)
    assembly = await build_assembly(store=store, seed=False)
    store.session_error = OSError("backend-specific detail")

    with pytest.raises(ProductEvidenceError) as raised:
        await opened(assembly)

    assert raised.value.code == "product-session-unreadable"
    assert "backend-specific detail" not in str(raised.value)
    assert await store.head(product_task_stream("task-1")) == 0
    await assembly.aclose()


async def test_session_store_base_exception_remains_caller_control() -> None:
    store = _SessionReadFailureStore()
    await seed_session(store)
    stop = SystemExit("stop")
    store.session_error = stop

    with pytest.raises(SystemExit) as raised:
        await SessionEvidenceReader(store).message("session-alpha", ORIGIN_MESSAGE)

    assert raised.value is stop
    assert await store.head(product_task_stream("task-1")) == 0


async def test_confirmation_evidence_must_share_the_product_event_store() -> None:
    """A Session in another fact universe cannot authorize a local task."""

    product_store = InMemoryEventStore()
    session_store = InMemoryEventStore()
    await seed_session(session_store)
    helper = await build_assembly(store=product_store, seed=False)
    with pytest.raises(ProductInputError) as raised:
        ProductTaskService(
            product_store,
            sessions=SessionEvidenceReader(session_store),
            workflow=helper.workflow,
            ownership=helper.ownership,
        )
    assert raised.value.code == "product-session-store-mismatch"
    await helper.aclose()


async def test_workflow_evidence_must_share_the_product_event_store() -> None:
    """Another Store's same-named run cannot decide this task's view."""

    product_store = InMemoryEventStore()
    workflow_store = InMemoryEventStore()
    helper = await build_assembly(store=product_store, seed=False)
    helper.workflow.store = workflow_store
    with pytest.raises(ProductInputError) as raised:
        ProductTaskService(
            product_store,
            sessions=SessionEvidenceReader(product_store),
            workflow=helper.workflow,
            ownership=helper.ownership,
        )
    assert raised.value.code == "product-workflow-store-mismatch"
    await helper.aclose()


async def test_a_workflow_source_rebound_after_construction_is_refused() -> None:
    """The constructor check is not a stale identity receipt."""

    assembly = await build_assembly()
    await opened(assembly)
    assembly.workflow.store = InMemoryEventStore()
    with pytest.raises(ProductInputError) as raised:
        await assembly.service.view("task-1")
    assert raised.value.code == "product-workflow-store-mismatch"
    await assembly.aclose()


async def test_a_self_confirmed_proposal_is_refused_before_any_read() -> None:
    """The rule and the facts are both required; this is the rule."""

    assembly = await build_assembly()
    with pytest.raises(ProductStateError) as raised:
        await assembly.service.open_task(
            task_id="task-1",
            operation_id="op-open",
            proposal=proposal(proposed_turn_id=CONFIRM_TURN),
            confirmation=confirmation(turn_id=CONFIRM_TURN),
        )
    assert raised.value.code == "product-confirmation-invalid"
    assert await assembly.service.load("task-1") is None
    await assembly.aclose()


async def test_confirmation_must_be_a_message_accepted_after_the_proposal_turn() -> None:
    """An older durable user message cannot be relabelled as approval."""

    store = InMemoryEventStore()
    await seed_session(
        store,
        messages=(("message-older", "turn-older"), (ORIGIN_MESSAGE, ORIGIN_TURN)),
    )
    assembly = await build_assembly(store=store, seed=False)

    with pytest.raises(ProductEvidenceError) as raised:
        await assembly.service.open_task(
            task_id="task-1",
            operation_id="op-open",
            proposal=proposal(),
            confirmation=confirmation(
                message_id="message-older", turn_id="turn-older"
            ),
        )

    assert raised.value.code == "product-confirmation-not-after-proposal"
    assert await assembly.service.load("task-1") is None
    await assembly.aclose()


@pytest.mark.parametrize(
    ("missing_turn", "expected_code"),
    (
        (ORIGIN_TURN, "product-origin-turn-unknown"),
        (CONFIRM_TURN, "product-confirmation-turn-unknown"),
    ),
)
async def test_a_claim_cannot_authorize_a_turn_that_never_started(
    missing_turn: str, expected_code: str
) -> None:
    """A claim names a Turn; only ``turn/start`` proves that Turn exists."""

    store = InMemoryEventStore()
    proposed_turn_id = ORIGIN_TURN
    message_turns = [
        (ORIGIN_MESSAGE, ORIGIN_TURN),
        (CONFIRM_MESSAGE, CONFIRM_TURN),
    ]
    if missing_turn == ORIGIN_TURN:
        # Keep the Proposal-producing Turn independently real and closed. If
        # the origin start check disappears, this exact history can otherwise
        # authorize instead of merely failing later for an incomplete Proposal.
        proposed_turn_id = "turn-proposal"
        message_turns.insert(1, ("message-proposal", proposed_turn_id))
    events = [
        PendingEvent(
            "session/created",
            {
                "session_id": "session-alpha",
                "workspace": "workspace-fixture",
                "metadata": {},
            },
        )
    ]
    for message_id, turn_id in message_turns:
        events.extend(
            (
                PendingEvent(
                    "inbox/accepted",
                    {
                        "message_id": message_id,
                        "source": "user",
                        "content": "example user message",
                        "target": "new_turn",
                    },
                ),
                PendingEvent(
                    "inbox/claimed",
                    {"message_id": message_id, "turn_id": turn_id},
                ),
            )
        )
        if turn_id != missing_turn:
            events.extend(
                (
                    PendingEvent(
                        "turn/start",
                        {"turn_id": turn_id, "message_id": message_id},
                    ),
                    PendingEvent(
                        "turn/end", {"turn_id": turn_id, "reason": "completed"}
                    ),
                )
            )
    await store.append("session:session-alpha", expected_seq=0, events=tuple(events))
    assembly = await build_assembly(store=store, seed=False)

    with pytest.raises(ProductEvidenceError) as raised:
        await opened(assembly, proposed_turn_id=proposed_turn_id)

    assert raised.value.code == expected_code
    assert await assembly.service.load("task-1") is None
    await assembly.aclose()


async def test_a_claim_cannot_borrow_another_messages_started_turn() -> None:
    """A real Turn is evidence only for the message that started it."""

    proposal_message = "message-proposal"
    proposal_turn = "turn-proposal"
    store = InMemoryEventStore()
    events = (
        PendingEvent(
            "session/created",
            {
                "session_id": "session-alpha",
                "workspace": "workspace-fixture",
                "metadata": {},
            },
        ),
        PendingEvent(
            "inbox/accepted",
            {
                "message_id": ORIGIN_MESSAGE,
                "source": "user",
                "content": "example requirement",
                "target": "new_turn",
            },
        ),
        PendingEvent(
            "inbox/claimed",
            {"message_id": ORIGIN_MESSAGE, "turn_id": ORIGIN_TURN},
        ),
        PendingEvent(
            "turn/start",
            {"turn_id": ORIGIN_TURN, "message_id": ORIGIN_MESSAGE},
        ),
        PendingEvent("turn/end", {"turn_id": ORIGIN_TURN, "reason": "completed"}),
        PendingEvent(
            "inbox/accepted",
            {
                "message_id": proposal_message,
                "source": "user",
                "content": "example proposal request",
                "target": "new_turn",
            },
        ),
        PendingEvent(
            "inbox/claimed",
            {"message_id": proposal_message, "turn_id": proposal_turn},
        ),
        PendingEvent(
            "turn/start",
            {"turn_id": proposal_turn, "message_id": proposal_message},
        ),
        PendingEvent("turn/end", {"turn_id": proposal_turn, "reason": "completed"}),
        PendingEvent(
            "inbox/accepted",
            {
                "message_id": CONFIRM_MESSAGE,
                "source": "user",
                "content": "example confirmation",
                "target": "new_turn",
            },
        ),
        PendingEvent(
            "inbox/claimed",
            {"message_id": CONFIRM_MESSAGE, "turn_id": ORIGIN_TURN},
        ),
    )
    await store.append("session:session-alpha", expected_seq=0, events=events)
    assembly = await build_assembly(store=store, seed=False)

    with pytest.raises(ProductEvidenceError) as raised:
        await assembly.service.open_task(
            task_id="task-1",
            operation_id="op-open",
            proposal=proposal(proposed_turn_id=proposal_turn),
            confirmation=confirmation(turn_id=ORIGIN_TURN),
        )

    assert raised.value.code == "product-session-unreadable"
    assert await assembly.service.load("task-1") is None
    await assembly.aclose()


async def test_the_confirmation_turn_may_be_open_after_its_durable_start() -> None:
    """Authorization needs a real Turn, not a completed confirmation Turn."""

    store = InMemoryEventStore()
    events = (
        PendingEvent(
            "session/created",
            {
                "session_id": "session-alpha",
                "workspace": "workspace-fixture",
                "metadata": {},
            },
        ),
        PendingEvent(
            "inbox/accepted",
            {
                "message_id": ORIGIN_MESSAGE,
                "source": "user",
                "content": "example requirement",
                "target": "new_turn",
            },
        ),
        PendingEvent(
            "inbox/claimed",
            {"message_id": ORIGIN_MESSAGE, "turn_id": ORIGIN_TURN},
        ),
        PendingEvent(
            "turn/start",
            {"turn_id": ORIGIN_TURN, "message_id": ORIGIN_MESSAGE},
        ),
        PendingEvent("turn/end", {"turn_id": ORIGIN_TURN, "reason": "completed"}),
        PendingEvent(
            "inbox/accepted",
            {
                "message_id": CONFIRM_MESSAGE,
                "source": "user",
                "content": "example confirmation",
                "target": "new_turn",
            },
        ),
        PendingEvent(
            "inbox/claimed",
            {"message_id": CONFIRM_MESSAGE, "turn_id": CONFIRM_TURN},
        ),
        PendingEvent(
            "turn/start",
            {"turn_id": CONFIRM_TURN, "message_id": CONFIRM_MESSAGE},
        ),
    )
    await store.append("session:session-alpha", expected_seq=0, events=events)
    assembly = await build_assembly(store=store, seed=False)

    summary = await opened(assembly)

    assert summary.status is ProductTaskStatus.OPENED
    await assembly.aclose()


async def test_a_turn_that_closes_over_an_open_step_cannot_authorize() -> None:
    """Product evidence reuses the core Session lifecycle invariants."""

    store = InMemoryEventStore()
    events = (
        PendingEvent(
            "session/created",
            {
                "session_id": "session-alpha",
                "workspace": "workspace-fixture",
                "metadata": {},
            },
        ),
        PendingEvent(
            "inbox/accepted",
            {
                "message_id": ORIGIN_MESSAGE,
                "source": "user",
                "content": "example requirement",
                "target": "new_turn",
            },
        ),
        PendingEvent(
            "inbox/claimed",
            {"message_id": ORIGIN_MESSAGE, "turn_id": ORIGIN_TURN},
        ),
        PendingEvent(
            "turn/start",
            {"turn_id": ORIGIN_TURN, "message_id": ORIGIN_MESSAGE},
        ),
        PendingEvent(
            "step/start", {"turn_id": ORIGIN_TURN, "step_id": "step-open"}
        ),
        PendingEvent("turn/end", {"turn_id": ORIGIN_TURN, "reason": "completed"}),
        PendingEvent(
            "inbox/accepted",
            {
                "message_id": CONFIRM_MESSAGE,
                "source": "user",
                "content": "example confirmation",
                "target": "new_turn",
            },
        ),
        PendingEvent(
            "inbox/claimed",
            {"message_id": CONFIRM_MESSAGE, "turn_id": CONFIRM_TURN},
        ),
        PendingEvent(
            "turn/start",
            {"turn_id": CONFIRM_TURN, "message_id": CONFIRM_MESSAGE},
        ),
    )
    await store.append("session:session-alpha", expected_seq=0, events=events)
    assembly = await build_assembly(store=store, seed=False)

    with pytest.raises(ProductEvidenceError) as raised:
        await opened(assembly)

    assert raised.value.code == "product-session-unreadable"
    assert await assembly.service.load("task-1") is None
    await assembly.aclose()


async def test_hostile_confirmation_identity_cannot_authorize_another_session() -> None:
    """The values authorized are exactly the plain values later persisted."""

    class EqualToEverything(str):
        def __eq__(self, other: object) -> bool:  # noqa: D105
            del other
            return True

        def __ne__(self, other: object) -> bool:  # noqa: D105
            del other
            return False

        __hash__ = str.__hash__

    store = InMemoryEventStore()
    await seed_session(
        store,
        session_id="session-alpha",
        messages=((ORIGIN_MESSAGE, ORIGIN_TURN),),
    )
    await seed_session(
        store,
        session_id="session-beta",
        messages=((CONFIRM_MESSAGE, CONFIRM_TURN),),
    )
    assembly = await build_assembly(store=store, seed=False)

    with pytest.raises(ProductStateError) as raised:
        await assembly.service.open_task(
            task_id="task-1",
            operation_id="op-open",
            proposal=proposal(session_id="session-alpha"),
            confirmation=confirmation(
                session_id=EqualToEverything("session-beta")
            ),
        )

    assert raised.value.code == "product-confirmation-invalid"
    assert await assembly.service.load("task-1") is None
    await assembly.aclose()


async def test_hostile_origin_turn_cannot_claim_an_unrelated_turn() -> None:
    """Session evidence compares detached built-in identities, never DTO methods."""

    class EqualToEverything(str):
        def __eq__(self, other: object) -> bool:  # noqa: D105
            del other
            return True

        def __ne__(self, other: object) -> bool:  # noqa: D105
            del other
            return False

        __hash__ = str.__hash__

    assembly = await build_assembly()
    with pytest.raises(ProductEvidenceError) as raised:
        await assembly.service.open_task(
            task_id="task-1",
            operation_id="op-open",
            proposal=proposal(origin_turn_id=EqualToEverything("turn-unclaimed")),
            confirmation=confirmation(),
        )

    assert raised.value.code == "product-origin-turn-mismatch"
    assert await assembly.service.load("task-1") is None
    await assembly.aclose()


# ------------------------------------------------------------ admission rules


async def test_the_service_refuses_what_the_projector_would_refuse() -> None:
    """The writer is never more permissive than the reader."""

    assembly = await build_assembly()
    with pytest.raises(ProductStateError) as raised:
        await assembly.service.record_awaiting(
            task_id="task-1", operation_id="op-await", review_id="review-1"
        )
    assert raised.value.code == "product-task-unknown"

    await opened(assembly, requested_mode=RequestedTaskMode.SINGLE)
    with pytest.raises(ProductStateError) as raised:
        await assembly.service.record_routing(
            task_id="task-1",
            operation_id="op-route",
            routing=TaskRouting(ResolvedTaskMode.SINGLE, None),
            router_agent_id="router-agent",
            routing_session_id="router-session",
        )
    assert raised.value.code == "product-transition-invalid"

    with pytest.raises(ProductStateError) as raised:
        await assembly.service.complete_task(
            task_id="task-1", operation_id="op-done", promotion_id="promotion-1"
        )
    assert raised.value.code == "product-transition-invalid"

    # Nothing was written by any refusal.
    summary = await assembly.service.load("task-1")
    assert summary is not None and summary.head_seq == 1
    await assembly.aclose()


async def test_a_receipt_on_another_preflight_cannot_start_the_task() -> None:
    """``binds()`` runs before anything is written."""

    assembly = await build_assembly()
    await opened(assembly)
    drifted = receipt(binding=preflight(base_revision="9" * 40))
    with pytest.raises(ProductStateError) as raised:
        await assembly.service.start_task(
            task_id="task-1", operation_id="op-start", receipt=drifted
        )
    assert raised.value.code == "product-preflight-drifted"
    summary = await assembly.service.load("task-1")
    assert summary is not None and summary.status is ProductTaskStatus.OPENED
    await assembly.aclose()


@pytest.mark.parametrize("field", ["workflow_definition_hash", "base_revision"])
async def test_a_malformed_receipt_is_refused_before_it_can_damage_the_stream(
    field: str,
) -> None:
    assembly = await build_assembly()
    await opened(assembly)
    valid = receipt()
    if field == "workflow_definition_hash":
        malformed = replace(valid, workflow_definition_hash=1)  # type: ignore[arg-type]
    else:
        malformed = replace(
            valid,
            preflight=replace(valid.preflight, base_revision=1),  # type: ignore[arg-type]
        )
    with pytest.raises(ProductInputError):
        await assembly.service.start_task(
            task_id="task-1", operation_id=f"op-invalid-{field}", receipt=malformed
        )
    events = await assembly.store.read(product_task_stream("task-1"))
    assert [event.type for event in events] == [PRODUCT_TASK_OPENED]
    assert await validate_product_task(assembly.store, "task-1") == ()
    await assembly.aclose()


async def test_a_rejection_must_name_the_awaited_review() -> None:
    assembly = await build_assembly()
    await opened(assembly)
    await assembly.service.start_task(
        task_id="task-1", operation_id="op-start", receipt=receipt()
    )
    await assembly.service.record_awaiting(
        task_id="task-1", operation_id="op-await", review_id="review-1"
    )
    with pytest.raises(ProductStateError) as raised:
        await assembly.service.reject_task(
            task_id="task-1", operation_id="op-reject", review_id="review-other"
        )
    assert raised.value.code == "product-decided-value-invalid"
    await assembly.aclose()


async def test_a_settled_task_admits_nothing_further() -> None:
    assembly = await build_assembly()
    await opened(assembly)
    await assembly.service.cancel_task(
        task_id="task-1", operation_id="op-cancel", reason_code="user-requested"
    )
    with pytest.raises(ProductStateError) as raised:
        await assembly.service.fail_task(
            task_id="task-1", operation_id="op-fail", failure_code="workflow-failed"
        )
    assert raised.value.code == "product-transition-invalid"
    await assembly.aclose()


# --------------------------------------------------------------- idempotency


async def test_an_identical_repeat_is_the_same_write() -> None:
    assembly = await build_assembly()
    first = await opened(assembly)
    again = await opened(assembly)
    assert again.head_seq == first.head_seq == 1
    events = await assembly.store.read(product_task_stream("task-1"))
    assert len(events) == 1
    await assembly.aclose()


async def test_the_same_operation_id_with_other_content_is_a_conflict() -> None:
    """Idempotency binds the id to its content, never to the id alone."""

    assembly = await build_assembly()
    await opened(assembly)
    await assembly.service.start_task(
        task_id="task-1", operation_id="op-start", receipt=receipt()
    )
    await assembly.service.record_awaiting(
        task_id="task-1", operation_id="op-await", review_id="review-1"
    )
    with pytest.raises(ProductOperationConflictError):
        await assembly.service.complete_task(
            task_id="task-1", operation_id="op-await", promotion_id="promotion-1"
        )
    summary = await assembly.service.load("task-1")
    assert summary is not None and summary.status is ProductTaskStatus.AWAITING_APPROVAL
    await assembly.aclose()


async def test_two_different_writes_cannot_share_an_in_flight_task() -> None:
    """A second caller must not receive a receipt for work it never described."""

    gate = Gate()

    class _Gated(SessionEvidenceReader):
        async def message(self, session_id, message_id):
            await gate.wait()
            return await super().message(session_id, message_id)

    store = InMemoryEventStore()
    await seed_session(store)
    service = ProductTaskService(
        store,
        sessions=_Gated(store),
        workflow=(await build_assembly(store=store, seed=False)).workflow,
        ownership=(await build_assembly(store=store, seed=False)).ownership,
    )
    first = asyncio.create_task(
        service.open_task(
            task_id="task-1",
            operation_id="op-a",
            proposal=proposal(),
            confirmation=confirmation(),
        )
    )
    await gate.entered.wait()
    with pytest.raises(ProductOperationConflictError):
        await service.open_task(
            task_id="task-1",
            operation_id="op-b",
            proposal=proposal(),
            confirmation=confirmation(),
        )
    gate.release.set()
    await first
    await service.aclose()


# ---------------------------------------------------- CAS and reconciliation


async def test_a_lost_compare_and_swap_retries_against_the_new_history() -> None:
    """Two writers, one deterministic interleaving, one surviving fact."""

    gate = Gate()
    store = InMemoryEventStore()
    await seed_session(store)
    assembly = await build_assembly(store=store, seed=False)
    await opened(assembly)

    original_append = store.append
    released = False

    async def gated(stream_id, *, expected_seq, events, durability=Durability.BATCHED):
        nonlocal released
        if stream_id.startswith("product-task:") and not released:
            released = True
            await gate.wait()
        return await original_append(
            stream_id, expected_seq=expected_seq, events=events, durability=durability
        )

    store.append = gated  # type: ignore[method-assign]
    slow = asyncio.create_task(
        assembly.service.start_task(
            task_id="task-1", operation_id="op-start", receipt=receipt()
        )
    )
    await gate.entered.wait()
    # A second writer wins the race while the first is parked inside append.
    store.append = original_append  # type: ignore[method-assign]
    await store.append(
        product_task_stream("task-1"),
        expected_seq=1,
        events=(
            __import__("traceh.api.events", fromlist=["PendingEvent"]).PendingEvent(
                type="product/task-cancelled",
                data={
                    "task_id": "task-1",
                    "operation_id": "op-race",
                    "reason_code": "host-shutdown",
                },
                schema_version=1,
            ),
        ),
    )
    gate.release.set()
    # The parked writer loses the CAS, re-reads, and finds a settled task.
    with pytest.raises(ProductStateError) as raised:
        await slow
    assert raised.value.code == "product-transition-invalid"
    summary = await assembly.service.load("task-1")
    assert summary is not None and summary.status is ProductTaskStatus.CANCELLED
    await assembly.aclose()


async def test_the_cas_expectation_comes_from_the_replayed_history() -> None:
    """Validating one history and writing against another is not the same thing.

    The window is between "this stream permits my fact" and "this is the head I
    write against". If the expectation is re-read separately, a fact that landed
    in between is silently accepted as the basis for a decision that was made
    without it - here, a ``started`` appended on top of an already-cancelled
    task, which the very next replay refuses.
    """

    gate = Gate()

    class _GatedRead:
        def __init__(self) -> None:
            self.inner = InMemoryEventStore()
            self.armed = False

        async def read(self, stream_id, *, from_seq=1):
            events = await self.inner.read(stream_id, from_seq=from_seq)
            if self.armed and stream_id.startswith("product-task:"):
                self.armed = False
                await gate.wait()
            return events

        async def append(self, stream_id, *, expected_seq, events, durability=None):
            kwargs = {} if durability is None else {"durability": durability}
            return await self.inner.append(
                stream_id, expected_seq=expected_seq, events=events, **kwargs
            )

        async def head(self, stream_id):
            return await self.inner.head(stream_id)

        async def list_streams(self, *, prefix=None):
            return await self.inner.list_streams(prefix=prefix)

    store = _GatedRead()
    await seed_session(store)
    assembly = await build_assembly(store=store, seed=False)
    await opened(assembly)

    store.armed = True
    slow = asyncio.create_task(
        assembly.service.start_task(
            task_id="task-1", operation_id="op-start", receipt=receipt()
        )
    )
    await gate.entered.wait()

    # A terminal lands after the slow writer replayed, before it appends.
    await store.append(
        product_task_stream("task-1"),
        expected_seq=1,
        events=(
            PendingEvent(
                type="product/task-cancelled",
                data={
                    "task_id": "task-1",
                    "operation_id": "op-race",
                    "reason_code": "host-shutdown",
                },
                schema_version=1,
            ),
        ),
    )
    gate.release.set()

    with pytest.raises(ProductStateError) as raised:
        await slow
    assert raised.value.code == "product-transition-invalid"

    # The decisive property: the stream is still replayable. A write that used a
    # separately re-read head would have landed after the terminal.
    assert await validate_product_task(store, "task-1") == ()
    summary = await assembly.service.load("task-1")
    assert summary is not None and summary.status is ProductTaskStatus.CANCELLED
    await assembly.aclose()


async def test_an_append_that_committed_before_failing_is_not_written_twice() -> None:
    store = _Wrapping()
    await seed_session(store)
    assembly = await build_assembly(store=store, seed=False)
    store.fail_on = PRODUCT_TASK_OPENED
    store.mode = "raise"
    summary = await opened(assembly)
    assert store.fired is True
    assert summary.status is ProductTaskStatus.OPENED
    events = await store.read(product_task_stream("task-1"))
    assert len(events) == 1
    await assembly.aclose()


async def test_an_append_that_did_not_commit_is_reported_as_such() -> None:
    store = _Wrapping()
    await seed_session(store)
    assembly = await build_assembly(store=store, seed=False)
    store.fail_on = PRODUCT_TASK_OPENED
    store.mode = "before"
    with pytest.raises(ProductWriteError) as raised:
        await opened(assembly)
    assert raised.value.committed is False
    assert await assembly.service.load("task-1") is None
    await assembly.aclose()


async def test_an_unreadable_stream_makes_the_outcome_unknown_not_absent() -> None:
    """Collapsing unknown into "absent" would write a second copy."""

    store = _Wrapping()
    await seed_session(store)
    assembly = await build_assembly(store=store, seed=False)
    store.fail_on = PRODUCT_TASK_OPENED
    store.mode = "raise"
    store.read_fails = True
    with pytest.raises(ProductWriteError) as raised:
        await opened(assembly)
    assert raised.value.committed is None
    await assembly.aclose()


async def test_a_failed_result_read_preserves_the_known_commit() -> None:
    """A normal append return proves commit even when the receipt read fails."""

    store = _Wrapping()
    await seed_session(store)
    assembly = await build_assembly(store=store, seed=False)
    store.fail_on = PRODUCT_TASK_OPENED
    store.mode = "success"
    store.read_fails = True

    with pytest.raises(ProductWriteError) as raised:
        await opened(assembly)

    assert raised.value.committed is True
    assert await store.inner.head(product_task_stream("task-1")) == 1
    await assembly.aclose()


async def test_a_persistent_conflict_is_bounded() -> None:
    class _AlwaysConflicts(_Wrapping):
        async def append(self, stream_id, *, expected_seq, events, durability=None):
            if stream_id.startswith("product-task:") and events[0].type != PRODUCT_TASK_OPENED:
                raise ConcurrencyConflict("head moved")
            kwargs = {} if durability is None else {"durability": durability}
            return await self.inner.append(
                stream_id, expected_seq=expected_seq, events=events, **kwargs
            )

    store = _AlwaysConflicts()
    await seed_session(store)
    assembly = await build_assembly(store=store, seed=False)
    await opened(assembly)
    with pytest.raises(ProductStreamConflictError):
        await assembly.service.start_task(
            task_id="task-1", operation_id="op-start", receipt=receipt()
        )
    await assembly.aclose()


# --------------------------------------------------------------- cancellation


async def test_a_cancelled_append_reconciles_before_the_caller_is_released() -> None:
    store = _Wrapping()
    await seed_session(store)
    assembly = await build_assembly(store=store, seed=False)
    store.fail_on = PRODUCT_TASK_OPENED
    store.mode = "cancel"
    with pytest.raises(asyncio.CancelledError):
        await opened(assembly)
    # The fact is durable, and a later identical write recognises it.
    events = await store.read(product_task_stream("task-1"))
    assert len(events) == 1
    again = await opened(assembly)
    assert again.head_seq == 1
    assert len(await store.read(product_task_stream("task-1"))) == 1
    await assembly.aclose()


async def test_repeated_cancellation_cannot_release_the_caller_early() -> None:
    gate = Gate()

    class _Gated(SessionEvidenceReader):
        async def message(self, session_id, message_id):
            await gate.wait()
            return await super().message(session_id, message_id)

    store = InMemoryEventStore()
    await seed_session(store)
    helper = await build_assembly(store=store, seed=False)
    service = ProductTaskService(
        store,
        sessions=_Gated(store),
        workflow=helper.workflow,
        ownership=helper.ownership,
    )
    running = asyncio.create_task(
        service.open_task(
            task_id="task-1",
            operation_id="op-open",
            proposal=proposal(),
            confirmation=confirmation(),
        )
    )
    await gate.entered.wait()
    running.cancel()
    running.cancel()
    running.cancel()
    await asyncio.sleep(0)
    assert not running.done()

    gate.release.set()
    with pytest.raises(asyncio.CancelledError):
        await running
    # The owned work converged: the fact is durable either way.
    summary = await service.load("task-1")
    assert summary is not None and summary.status is ProductTaskStatus.OPENED
    await service.aclose()


async def test_close_converges_in_flight_writes_and_admits_no_more() -> None:
    gate = Gate()

    class _Gated(SessionEvidenceReader):
        async def message(self, session_id, message_id):
            await gate.wait()
            return await super().message(session_id, message_id)

    store = InMemoryEventStore()
    await seed_session(store)
    helper = await build_assembly(store=store, seed=False)
    service = ProductTaskService(
        store,
        sessions=_Gated(store),
        workflow=helper.workflow,
        ownership=helper.ownership,
    )
    running = asyncio.create_task(
        service.open_task(
            task_id="task-1",
            operation_id="op-open",
            proposal=proposal(),
            confirmation=confirmation(),
        )
    )
    await gate.entered.wait()
    closing = asyncio.create_task(service.aclose())
    await asyncio.sleep(0)
    assert not closing.done()

    gate.release.set()
    await running
    await closing
    with pytest.raises(ProductServiceClosedError):
        await service.open_task(
            task_id="task-2",
            operation_id="op-open-2",
            proposal=proposal(),
            confirmation=confirmation(),
        )
    await helper.aclose()


# ---------------------------------------------------------------- views


async def test_the_three_view_only_answers_are_all_reachable() -> None:
    assembly = await build_assembly()
    await opened(assembly)
    await assembly.service.start_task(
        task_id="task-1", operation_id="op-start", receipt=receipt()
    )

    assembly.workflow.status_value = WorkflowStatus.RUNNING
    assembly.ownership.owned = False
    view = await assembly.service.view("task-1")
    assert view is not None and view.status is ProductTaskViewStatus.INTERRUPTED

    assembly.workflow.status_value = WorkflowStatus.COMPLETED
    view = await assembly.service.view("task-1")
    assert view is not None and view.status is ProductTaskViewStatus.UNRECONCILED

    await assembly.service.record_awaiting(
        task_id="task-1", operation_id="op-await", review_id="review-1"
    )
    assembly.workflow.status_value = WorkflowStatus.AWAITING_APPROVAL
    view = await assembly.service.view("task-1")
    assert view is not None and view.status is ProductTaskViewStatus.RESUMABLE
    await assembly.aclose()


async def test_neither_workflow_state_nor_ownership_is_cached() -> None:
    """Both are asked again on every view, because both change underneath it."""

    assembly = await build_assembly()
    await opened(assembly)
    await assembly.service.start_task(
        task_id="task-1", operation_id="op-start", receipt=receipt()
    )
    assembly.workflow.status_value = WorkflowStatus.RUNNING
    assembly.ownership.owned = True

    first = await assembly.service.view("task-1")
    assert first is not None and first.status is ProductTaskViewStatus.STARTED
    reads = (assembly.workflow.reads, assembly.ownership.reads)

    assembly.ownership.owned = False
    second = await assembly.service.view("task-1")
    assert second is not None and second.status is ProductTaskViewStatus.INTERRUPTED

    assembly.workflow.status_value = WorkflowStatus.AWAITING_APPROVAL
    third = await assembly.service.view("task-1")
    assert third is not None and third.status is ProductTaskViewStatus.UNRECONCILED

    assert assembly.workflow.reads > reads[0]
    assert assembly.ownership.reads > reads[1]
    await assembly.aclose()


async def test_a_view_of_an_unopened_task_is_nothing() -> None:
    assembly = await build_assembly()
    assert await assembly.service.view("never-opened") is None
    await assembly.aclose()


async def test_abandoning_requires_a_genuinely_derived_interruption() -> None:
    """The honest terminal is honest only because of this condition."""

    assembly = await build_assembly()
    await opened(assembly)
    await assembly.service.start_task(
        task_id="task-1", operation_id="op-start", receipt=receipt()
    )

    assembly.workflow.status_value = WorkflowStatus.RUNNING
    assembly.ownership.owned = True
    with pytest.raises(ProductStateError) as raised:
        await assembly.service.abandon_task(
            task_id="task-1", operation_id="op-abandon", reason_code="host-exit"
        )
    assert raised.value.code == "product-task-not-interrupted"

    # Unowned but disagreeing streams are a reconciliation, not an abandonment.
    assembly.ownership.owned = False
    assembly.workflow.status_value = WorkflowStatus.COMPLETED
    with pytest.raises(ProductStateError) as raised:
        await assembly.service.abandon_task(
            task_id="task-1", operation_id="op-abandon", reason_code="host-exit"
        )
    assert raised.value.code == "product-task-not-interrupted"

    assembly.workflow.status_value = WorkflowStatus.RUNNING
    settled = await assembly.service.abandon_task(
        task_id="task-1", operation_id="op-abandon", reason_code="host-exit"
    )
    assert settled.status is ProductTaskStatus.ABANDONED
    await assembly.aclose()


async def test_abandoning_a_resumable_task_is_refused() -> None:
    """A clean Approval barrier can be picked up, so it is not abandoned."""

    assembly = await build_assembly()
    await opened(assembly)
    await assembly.service.start_task(
        task_id="task-1", operation_id="op-start", receipt=receipt()
    )
    await assembly.service.record_awaiting(
        task_id="task-1", operation_id="op-await", review_id="review-1"
    )
    assembly.ownership.owned = False
    assembly.workflow.status_value = WorkflowStatus.AWAITING_APPROVAL
    with pytest.raises(ProductStateError) as raised:
        await assembly.service.abandon_task(
            task_id="task-1", operation_id="op-abandon", reason_code="host-exit"
        )
    assert raised.value.code == "product-task-not-interrupted"
    await assembly.aclose()
