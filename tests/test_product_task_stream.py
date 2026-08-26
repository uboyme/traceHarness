"""v0.7-F1: the ProductTask parser and projector.

Everything here goes through the public replay path. A stream is either built by
the real service or appended directly to the real store; nothing constructs a
projection out of hand-made internal state, because a rule that only holds for
values the tests themselves shaped is not a rule about the protocol.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from product_fixtures import (
    build_assembly,
    confirmation,
    opened,
    preflight,
    proposal,
    receipt,
)

from traceh.api.events import EventEnvelope, PendingEvent
from traceh.api.product import (
    PRODUCT_TASK_ABANDONED,
    PRODUCT_TASK_AWAITING,
    PRODUCT_TASK_CANCELLED,
    PRODUCT_TASK_COMPLETED,
    PRODUCT_TASK_EVENT_TYPES,
    PRODUCT_TASK_FAILED,
    PRODUCT_TASK_OPENED,
    PRODUCT_TASK_REJECTED,
    PRODUCT_TASK_ROUTED,
    PRODUCT_TASK_SCHEMA_VERSION,
    PRODUCT_TASK_STARTED,
    ProductTaskStatus,
    RequestedTaskMode,
    ResolvedTaskMode,
    TaskModeSource,
    TaskRouting,
)
from traceh.product import (
    ProductOperationConflictError,
    ProductProtocolError,
    ProductTaskStreamReader,
    product_task_stream,
    rebuild_product_task,
    task_opened_data,
    task_started_data,
    validate_product_task,
)
from traceh.product.events import parse_product_event
from traceh.session.event_store import InMemoryEventStore

STREAM = product_task_stream("task-1")


def envelope(seq: int, event_type: str, data: dict, **overrides: object):
    pending = PendingEvent(
        type=event_type,
        data=data,
        schema_version=overrides.pop("schema_version", PRODUCT_TASK_SCHEMA_VERSION),  # type: ignore[arg-type]
    )
    built = EventEnvelope.materialize(
        overrides.pop("stream_id", STREAM), seq, pending  # type: ignore[arg-type]
    )
    return built if not overrides else _replace_envelope(built, overrides)


def _replace_envelope(event: EventEnvelope, overrides: dict) -> EventEnvelope:
    from dataclasses import replace

    return replace(event, **overrides)


async def _drive_to_completed(assembly, *, task_id: str = "task-1"):
    service = assembly.service
    await opened(assembly, task_id=task_id)
    await service.start_task(
        task_id=task_id, operation_id=f"{task_id}-start", receipt=receipt()
    )
    await service.record_awaiting(
        task_id=task_id, operation_id=f"{task_id}-await", review_id="review-1"
    )
    return await service.complete_task(
        task_id=task_id, operation_id=f"{task_id}-done", promotion_id="promotion-1"
    )


# ------------------------------------------------------------- forward replay


async def test_every_event_type_replays_into_the_summary_it_establishes() -> None:
    """All nine facts, driven through the real service, then replayed."""

    seen: set[str] = set()

    # completed path: opened -> started -> awaiting -> completed
    assembly = await build_assembly()
    summary = await _drive_to_completed(assembly)
    assert summary.status is ProductTaskStatus.COMPLETED
    assert summary.promotion_id == "promotion-1"
    assert summary.review_id == "review-1"
    assert summary.head_seq == 4
    seen |= {
        PRODUCT_TASK_OPENED,
        PRODUCT_TASK_STARTED,
        PRODUCT_TASK_AWAITING,
        PRODUCT_TASK_COMPLETED,
    }
    await assembly.aclose()

    # routed path: opened(auto) -> routed -> started
    assembly = await build_assembly()
    await opened(assembly, task_id="task-2", requested_mode=RequestedTaskMode.AUTO)
    routed = await assembly.service.record_routing(
        task_id="task-2",
        operation_id="task-2-route",
        routing=TaskRouting(ResolvedTaskMode.MULTI, "cross-module change"),
        router_agent_id="router-agent",
        routing_session_id="router-session",
    )
    assert routed.status is ProductTaskStatus.ROUTED
    assert routed.resolved_mode is ResolvedTaskMode.MULTI
    assert routed.reason_display == "cross-module change"
    assert routed.router_agent_id == "router-agent"
    started = await assembly.service.start_task(
        task_id="task-2",
        operation_id="task-2-start",
        receipt=receipt(mode=ResolvedTaskMode.MULTI),
    )
    assert started.status is ProductTaskStatus.STARTED
    seen |= {PRODUCT_TASK_ROUTED}
    await assembly.aclose()

    # rejected
    assembly = await build_assembly()
    await opened(assembly, task_id="task-3")
    await assembly.service.start_task(
        task_id="task-3", operation_id="task-3-start", receipt=receipt()
    )
    await assembly.service.record_awaiting(
        task_id="task-3", operation_id="task-3-await", review_id="review-3"
    )
    rejected = await assembly.service.reject_task(
        task_id="task-3", operation_id="task-3-reject", review_id="review-3"
    )
    assert rejected.status is ProductTaskStatus.REJECTED
    seen |= {PRODUCT_TASK_REJECTED}

    # cancelled and failed, each from a live task
    for task_id, method, kwargs, status, code in (
        ("task-4", "cancel_task", {"reason_code": "user-requested"},
         ProductTaskStatus.CANCELLED, "reason_code"),
        ("task-5", "fail_task", {"failure_code": "workflow-failed"},
         ProductTaskStatus.FAILED, "failure_code"),
    ):
        await opened(assembly, task_id=task_id)
        settled = await getattr(assembly.service, method)(
            task_id=task_id, operation_id=f"{task_id}-end", **kwargs
        )
        assert settled.status is status
        assert getattr(settled, code) == next(iter(kwargs.values()))
    seen |= {PRODUCT_TASK_CANCELLED, PRODUCT_TASK_FAILED}

    # abandoned: only reachable through a derived interrupted view
    assembly.workflow.status_value = None
    await opened(assembly, task_id="task-6")
    await assembly.service.start_task(
        task_id="task-6", operation_id="task-6-start", receipt=receipt()
    )
    assembly.workflow.status_value = __import__(
        "traceh.api.workflow", fromlist=["WorkflowStatus"]
    ).WorkflowStatus.RUNNING
    assembly.ownership.owned = False
    abandoned = await assembly.service.abandon_task(
        task_id="task-6", operation_id="task-6-abandon", reason_code="host-exit"
    )
    assert abandoned.status is ProductTaskStatus.ABANDONED
    seen |= {PRODUCT_TASK_ABANDONED}
    await assembly.aclose()

    assert seen == set(PRODUCT_TASK_EVENT_TYPES)


async def test_an_unopened_task_replays_as_nothing() -> None:
    assembly = await build_assembly()
    assert await assembly.service.load("never-opened") is None
    assert rebuild_product_task("never-opened", ()) is None
    await assembly.aclose()


# ------------------------------------------------------------ shape rejection


async def test_a_stream_from_another_task_is_refused() -> None:
    data = task_opened_data(
        task_id="task-1",
        operation_id="op-1",
        proposal=proposal(),
        confirmation=confirmation(),
    )
    with pytest.raises(ProductProtocolError) as raised:
        rebuild_product_task(
            "task-1", (envelope(1, PRODUCT_TASK_OPENED, data, stream_id="product-task:other"),)
        )
    assert raised.value.code == "product-stream-unexpected"


async def test_a_payload_naming_another_task_is_refused() -> None:
    """The stream name and the payload must agree; neither is trusted alone."""

    data = task_opened_data(
        task_id="task-9",
        operation_id="op-1",
        proposal=proposal(),
        confirmation=confirmation(),
    )
    with pytest.raises(ProductProtocolError) as raised:
        rebuild_product_task("task-1", (envelope(1, PRODUCT_TASK_OPENED, data),))
    assert raised.value.code == "product-task-identity-invalid"


async def test_a_query_identity_is_normalized_once_for_the_whole_replay() -> None:
    """Stream selection, payload comparison and Summary share one plain id."""

    class EqualToEverything(str):
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

    query = EqualToEverything("task-stream")
    store = InMemoryEventStore()
    forged = task_opened_data(
        task_id="task-payload",
        operation_id="op-forged",
        proposal=proposal(),
        confirmation=confirmation(),
    )
    await store.append(
        "product-task:task-stream",
        expected_seq=0,
        events=(PendingEvent(PRODUCT_TASK_OPENED, forged),),
    )

    with pytest.raises(ProductProtocolError) as raised:
        await ProductTaskStreamReader(store).load(query)

    assert raised.value.code == "product-task-identity-invalid"
    assert EqualToEverything.comparisons == 0

    valid_store = InMemoryEventStore()
    valid = task_opened_data(
        task_id="task-stream",
        operation_id="op-valid",
        proposal=proposal(),
        confirmation=confirmation(),
    )
    await valid_store.append(
        "product-task:task-stream",
        expected_seq=0,
        events=(PendingEvent(PRODUCT_TASK_OPENED, valid),),
    )
    summary = await ProductTaskStreamReader(valid_store).load(query)
    assert summary is not None
    assert summary.task_id == "task-stream"
    assert type(summary.task_id) is str
    assert EqualToEverything.comparisons == 0


async def test_an_unknown_schema_version_is_refused_not_upcast() -> None:
    data = task_opened_data(
        task_id="task-1",
        operation_id="op-1",
        proposal=proposal(),
        confirmation=confirmation(),
    )
    for version in (0, 2, 99):
        with pytest.raises(ProductProtocolError) as raised:
            rebuild_product_task(
                "task-1",
                (envelope(1, PRODUCT_TASK_OPENED, data, schema_version=version),),
            )
        assert raised.value.code == "product-schema-version-unsupported", version


async def test_an_unknown_event_type_is_refused() -> None:
    with pytest.raises(ProductProtocolError) as raised:
        rebuild_product_task(
            "task-1", (envelope(1, "product/task-settled", {"task_id": "task-1"}),)
        )
    assert raised.value.code == "product-event-type-unknown"


async def test_a_missing_or_extra_key_is_refused() -> None:
    base = task_opened_data(
        task_id="task-1",
        operation_id="op-1",
        proposal=proposal(),
        confirmation=confirmation(),
    )
    extra = dict(base, unexpected="value")
    missing = {key: value for key, value in base.items() if key != "mode_source"}
    for payload in (extra, missing):
        with pytest.raises(ProductProtocolError) as raised:
            rebuild_product_task("task-1", (envelope(1, PRODUCT_TASK_OPENED, payload),))
        assert raised.value.code == "product-payload-keys-unexpected"


async def test_a_sequence_gap_is_refused() -> None:
    data = task_opened_data(
        task_id="task-1",
        operation_id="op-1",
        proposal=proposal(),
        confirmation=confirmation(),
    )
    with pytest.raises(ProductProtocolError) as raised:
        rebuild_product_task("task-1", (envelope(2, PRODUCT_TASK_OPENED, data),))
    assert raised.value.code == "product-sequence-invalid"


# --------------------------------------------------------- hostile envelopes


async def test_a_hostile_envelope_becomes_a_stable_protocol_error() -> None:
    """Reading an object the store handed back may itself fail."""

    class Exploding:
        @property
        def stream_id(self):
            raise RuntimeError("payload is hostile")

        seq = 1

    with pytest.raises(ProductProtocolError) as raised:
        rebuild_product_task("task-1", (Exploding(),))  # type: ignore[arg-type]
    assert raised.value.code == "product-payload-invalid"


async def test_a_non_dict_payload_is_refused() -> None:
    from dataclasses import replace

    built = envelope(1, PRODUCT_TASK_OPENED, {})
    forged = replace(built, data=["not", "a", "dict"])  # type: ignore[arg-type]
    with pytest.raises(ProductProtocolError) as raised:
        rebuild_product_task("task-1", (forged,))
    assert raised.value.code == "product-payload-invalid"


async def test_a_naive_timestamp_is_refused() -> None:
    data = task_opened_data(
        task_id="task-1",
        operation_id="op-1",
        proposal=proposal(),
        confirmation=confirmation(),
    )
    naive = envelope(1, PRODUCT_TASK_OPENED, data, occurred_at=datetime(2026, 1, 1))
    with pytest.raises(ProductProtocolError) as raised:
        rebuild_product_task("task-1", (naive,))
    assert raised.value.code == "product-recorded-at-invalid"

    # A tz-aware one is fine, whatever offset it carries.
    aware = envelope(
        1,
        PRODUCT_TASK_OPENED,
        data,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=3),
    )
    assert rebuild_product_task("task-1", (aware,)) is not None


async def test_a_hostile_string_subclass_cannot_smuggle_an_identity() -> None:
    """A value that compares equal now may behave differently once stored."""

    class Sneaky(str):
        def __eq__(self, other: object) -> bool:  # noqa: D105
            return True

        def __hash__(self) -> int:  # noqa: D105
            return hash("task-1")

    data = task_opened_data(
        task_id="task-1",
        operation_id="op-1",
        proposal=proposal(),
        confirmation=confirmation(),
    )
    forged = dict(data, operation_id=Sneaky("op-1"))
    event = envelope(1, PRODUCT_TASK_OPENED, forged)
    parsed = parse_product_event(event, STREAM)
    assert type(parsed.data["operation_id"]) is str
    with pytest.raises(TypeError):
        parsed.data["operation_id"] = "changed"  # type: ignore[index]

    summary = rebuild_product_task("task-1", (event,))
    # It is normalised to a plain ``str`` rather than kept as the subclass.
    assert summary is not None
    assert type(summary.task_id) is str


# ------------------------------------------------------- order and terminals


async def test_no_fact_may_follow_a_terminal() -> None:
    store = InMemoryEventStore()
    assembly = await build_assembly(store=store)
    await _drive_to_completed(assembly)
    await assembly.aclose()

    # Forge an append past the terminal directly on the store, then replay.
    forged = {
        "task_id": "task-1",
        "operation_id": "op-after-end",
        "reason_code": "user-requested",
    }
    await store.append(
        STREAM,
        expected_seq=4,
        events=(
            PendingEvent(
                type=PRODUCT_TASK_CANCELLED,
                data=forged,
                schema_version=PRODUCT_TASK_SCHEMA_VERSION,
            ),
        ),
    )
    issues = await validate_product_task(store, "task-1")
    assert [issue.code for issue in issues] == ["product-transition-invalid"]


async def test_a_second_opening_is_refused() -> None:
    store = InMemoryEventStore()
    assembly = await build_assembly(store=store)
    await opened(assembly)
    await assembly.aclose()
    data = task_opened_data(
        task_id="task-1",
        operation_id="op-second",
        proposal=proposal(),
        confirmation=confirmation(),
    )
    await store.append(
        STREAM,
        expected_seq=1,
        events=(
            PendingEvent(
                type=PRODUCT_TASK_OPENED,
                data=data,
                schema_version=PRODUCT_TASK_SCHEMA_VERSION,
            ),
        ),
    )
    issues = await validate_product_task(store, "task-1")
    assert [issue.code for issue in issues] == ["product-transition-invalid"]


async def test_a_duplicate_operation_id_is_refused_during_replay() -> None:
    store = InMemoryEventStore()
    assembly = await build_assembly(store=store)
    await opened(assembly)
    await assembly.aclose()
    await store.append(
        STREAM,
        expected_seq=1,
        events=(
            PendingEvent(
                type=PRODUCT_TASK_CANCELLED,
                data={
                    "task_id": "task-1",
                    "operation_id": "task-1-open",
                    "reason_code": "user-requested",
                },
                schema_version=PRODUCT_TASK_SCHEMA_VERSION,
            ),
        ),
    )
    issues = await validate_product_task(store, "task-1")
    assert [issue.code for issue in issues] == ["product-operation-duplicate"]


async def test_a_first_fact_other_than_opening_is_refused() -> None:
    store = InMemoryEventStore()
    await store.append(
        STREAM,
        expected_seq=0,
        events=(
            PendingEvent(
                type=PRODUCT_TASK_STARTED,
                data={
                    "task_id": "task-1",
                    "operation_id": "op-1",
                    "mode": "single",
                    "workflow_run_id": "task-1",
                    "definition_hash": "a" * 64,
                    "assembly_digest": "b" * 64,
                    "preflight_digest": "c" * 64,
                    "source_base_revision": "d" * 40,
                },
                schema_version=PRODUCT_TASK_SCHEMA_VERSION,
            ),
        ),
    )
    issues = await validate_product_task(store, "task-1")
    assert [issue.code for issue in issues] == ["product-transition-invalid"]


# ------------------------------------------------- cross-event decided values


async def test_a_started_fact_naming_another_mode_is_refused() -> None:
    """``single`` cannot start as ``multi``, and replay recomputes the answer."""

    store = InMemoryEventStore()
    assembly = await build_assembly(store=store)
    await opened(assembly, requested_mode=RequestedTaskMode.SINGLE)
    await assembly.aclose()
    await store.append(
        STREAM,
        expected_seq=1,
        events=(
            PendingEvent(
                type=PRODUCT_TASK_STARTED,
                data={
                    "task_id": "task-1",
                    "operation_id": "op-start",
                    "mode": "multi",
                    "workflow_run_id": "task-1",
                    "definition_hash": "a" * 64,
                    "assembly_digest": "b" * 64,
                    "preflight_digest": preflight().digest,
                    "source_base_revision": "4" * 40,
                },
                schema_version=PRODUCT_TASK_SCHEMA_VERSION,
            ),
        ),
    )
    issues = await validate_product_task(store, "task-1")
    assert [issue.code for issue in issues] == ["product-decided-value-invalid"]


async def test_a_started_fact_naming_another_run_or_preflight_is_refused() -> None:
    """The run id and the confirmed preflight are both replay-checkable."""

    for key, value in (
        ("workflow_run_id", "some-other-task"),
        ("preflight_digest", "0" * 64),
    ):
        store = InMemoryEventStore()
        assembly = await build_assembly(store=store)
        await opened(assembly)
        await assembly.aclose()
        payload = {
            "task_id": "task-1",
            "operation_id": "op-start",
            "mode": "single",
            "workflow_run_id": "task-1",
            "definition_hash": "a" * 64,
            "assembly_digest": "b" * 64,
            "preflight_digest": preflight().digest,
            "source_base_revision": "4" * 40,
        }
        payload[key] = value
        await store.append(
            STREAM,
            expected_seq=1,
            events=(
                PendingEvent(
                    type=PRODUCT_TASK_STARTED,
                    data=payload,
                    schema_version=PRODUCT_TASK_SCHEMA_VERSION,
                ),
            ),
        )
        issues = await validate_product_task(store, "task-1")
        assert [issue.code for issue in issues] == ["product-decided-value-invalid"], key


async def test_decided_values_ignore_hostile_string_comparison_methods() -> None:
    """A forged value cannot compare equal to the value the opening fixed."""

    class EqualToEverything(str):
        def __eq__(self, other: object) -> bool:  # noqa: D105
            del other
            return True

        def __ne__(self, other: object) -> bool:  # noqa: D105
            del other
            return False

        __hash__ = str.__hash__

    store = InMemoryEventStore()
    assembly = await build_assembly(store=store)
    await opened(assembly)
    await assembly.aclose()
    forged = task_started_data(
        task_id="task-1", operation_id="op-start", receipt=receipt()
    )
    forged["workflow_run_id"] = EqualToEverything("task-other")
    await store.append(
        STREAM,
        expected_seq=1,
        events=(
            PendingEvent(
                type=PRODUCT_TASK_STARTED,
                data=forged,
                schema_version=PRODUCT_TASK_SCHEMA_VERSION,
            ),
        ),
    )

    issues = await validate_product_task(store, "task-1")
    assert [issue.code for issue in issues] == ["product-decided-value-invalid"]


async def test_idempotency_compares_a_detached_normalized_event_payload() -> None:
    """A prior operation cannot hide from conflict detection then poison replay."""

    class DifferentFromEverything(str):
        def __eq__(self, other: object) -> bool:  # noqa: D105
            del other
            return False

        def __ne__(self, other: object) -> bool:  # noqa: D105
            del other
            return True

        __hash__ = str.__hash__

    store = InMemoryEventStore()
    assembly = await build_assembly(store=store)
    await opened(assembly)
    started = task_started_data(
        task_id="task-1", operation_id="op-shared", receipt=receipt()
    )
    started["operation_id"] = DifferentFromEverything("op-shared")
    await store.append(
        STREAM,
        expected_seq=1,
        events=(
            PendingEvent(
                type=PRODUCT_TASK_STARTED,
                data=started,
                schema_version=PRODUCT_TASK_SCHEMA_VERSION,
            ),
        ),
    )

    with pytest.raises(ProductOperationConflictError):
        await assembly.service.record_awaiting(
            task_id="task-1", operation_id="op-shared", review_id="review-1"
        )

    assert await store.head(STREAM) == 2
    assert await validate_product_task(store, "task-1") == ()
    await assembly.aclose()


async def test_stateful_string_conversion_cannot_change_an_operation_between_replays() -> None:
    """Normalization must not execute a caller's stateful ``__str__``."""

    class StatefulString(str):
        calls = 0

        def __str__(self) -> str:  # noqa: D105
            type(self).calls += 1
            return "op-not-shared" if type(self).calls == 1 else "op-shared"

    store = InMemoryEventStore()
    assembly = await build_assembly(store=store)
    await opened(assembly)
    started = task_started_data(
        task_id="task-1", operation_id="op-shared", receipt=receipt()
    )
    started["operation_id"] = StatefulString("op-shared")
    await store.append(
        STREAM,
        expected_seq=1,
        events=(
            PendingEvent(
                type=PRODUCT_TASK_STARTED,
                data=started,
                schema_version=PRODUCT_TASK_SCHEMA_VERSION,
            ),
        ),
    )

    with pytest.raises(ProductOperationConflictError):
        await assembly.service.record_awaiting(
            task_id="task-1", operation_id="op-shared", review_id="review-1"
        )

    assert StatefulString.calls == 0
    assert await store.head(STREAM) == 2
    assert await validate_product_task(store, "task-1") == ()
    await assembly.aclose()


async def test_a_rejection_naming_another_review_is_refused() -> None:
    store = InMemoryEventStore()
    assembly = await build_assembly(store=store)
    await opened(assembly)
    await assembly.service.start_task(
        task_id="task-1", operation_id="op-start", receipt=receipt()
    )
    await assembly.service.record_awaiting(
        task_id="task-1", operation_id="op-await", review_id="review-1"
    )
    await assembly.aclose()
    await store.append(
        STREAM,
        expected_seq=3,
        events=(
            PendingEvent(
                type=PRODUCT_TASK_REJECTED,
                data={
                    "task_id": "task-1",
                    "operation_id": "op-reject",
                    "review_id": "review-somewhere-else",
                },
                schema_version=PRODUCT_TASK_SCHEMA_VERSION,
            ),
        ),
    )
    issues = await validate_product_task(store, "task-1")
    assert [issue.code for issue in issues] == ["product-decided-value-invalid"]


async def test_auto_cannot_start_before_routing_produced_a_mode() -> None:
    store = InMemoryEventStore()
    assembly = await build_assembly(store=store)
    await opened(assembly, requested_mode=RequestedTaskMode.AUTO)
    await assembly.aclose()
    await store.append(
        STREAM,
        expected_seq=1,
        events=(
            PendingEvent(
                type=PRODUCT_TASK_STARTED,
                data={
                    "task_id": "task-1",
                    "operation_id": "op-start",
                    "mode": "single",
                    "workflow_run_id": "task-1",
                    "definition_hash": "a" * 64,
                    "assembly_digest": "b" * 64,
                    "preflight_digest": preflight().digest,
                    "source_base_revision": "4" * 40,
                },
                schema_version=PRODUCT_TASK_SCHEMA_VERSION,
            ),
        ),
    )
    issues = await validate_product_task(store, "task-1")
    assert [issue.code for issue in issues] == ["product-transition-invalid"]


async def test_an_explicit_request_is_never_routed() -> None:
    store = InMemoryEventStore()
    assembly = await build_assembly(store=store)
    await opened(assembly, requested_mode=RequestedTaskMode.MULTI)
    await assembly.aclose()
    await store.append(
        STREAM,
        expected_seq=1,
        events=(
            PendingEvent(
                type=PRODUCT_TASK_ROUTED,
                data={
                    "task_id": "task-1",
                    "operation_id": "op-route",
                    "router_agent_id": "router-agent",
                    "routing_session_id": "router-session",
                    "resolved_mode": "multi",
                    "reason_display": None,
                },
                schema_version=PRODUCT_TASK_SCHEMA_VERSION,
            ),
        ),
    )
    issues = await validate_product_task(store, "task-1")
    assert [issue.code for issue in issues] == ["product-transition-invalid"]


async def test_a_routed_auto_task_must_start_with_the_routed_mode() -> None:
    store = InMemoryEventStore()
    assembly = await build_assembly(store=store)
    await opened(assembly, requested_mode=RequestedTaskMode.AUTO)
    await assembly.service.record_routing(
        task_id="task-1",
        operation_id="op-route",
        routing=TaskRouting(ResolvedTaskMode.MULTI, None),
        router_agent_id="router-agent",
        routing_session_id="router-session",
    )
    await assembly.aclose()
    await store.append(
        STREAM,
        expected_seq=2,
        events=(
            PendingEvent(
                type=PRODUCT_TASK_STARTED,
                data={
                    "task_id": "task-1",
                    "operation_id": "op-start",
                    "mode": "single",
                    "workflow_run_id": "task-1",
                    "definition_hash": "a" * 64,
                    "assembly_digest": "b" * 64,
                    "preflight_digest": preflight().digest,
                    "source_base_revision": "4" * 40,
                },
                schema_version=PRODUCT_TASK_SCHEMA_VERSION,
            ),
        ),
    )
    issues = await validate_product_task(store, "task-1")
    assert [issue.code for issue in issues] == ["product-decided-value-invalid"]


async def test_a_reason_display_that_could_forge_output_is_refused() -> None:
    store = InMemoryEventStore()
    assembly = await build_assembly(store=store)
    await opened(assembly, requested_mode=RequestedTaskMode.AUTO)
    await assembly.aclose()
    await store.append(
        STREAM,
        expected_seq=1,
        events=(
            PendingEvent(
                type=PRODUCT_TASK_ROUTED,
                data={
                    "task_id": "task-1",
                    "operation_id": "op-route",
                    "router_agent_id": "router-agent",
                    "routing_session_id": "router-session",
                    "resolved_mode": "multi",
                    "reason_display": "line one\nline two",
                },
                schema_version=PRODUCT_TASK_SCHEMA_VERSION,
            ),
        ),
    )
    issues = await validate_product_task(store, "task-1")
    assert [issue.code for issue in issues] == ["product-reason-display-invalid"]


async def test_a_mode_value_outside_the_enum_is_refused() -> None:
    data = task_opened_data(
        task_id="task-1",
        operation_id="op-1",
        proposal=proposal(),
        confirmation=confirmation(),
    )
    for key, value in (
        ("requested_mode", "sideways"),
        ("mode_source", "guessed"),
        ("requested_mode", 1),
    ):
        forged = dict(data)
        forged[key] = value
        with pytest.raises(ProductProtocolError) as raised:
            rebuild_product_task("task-1", (envelope(1, PRODUCT_TASK_OPENED, forged),))
        assert raised.value.code in (
            "product-enum-invalid",
            "product-transition-invalid",
        ), (key, value)


async def test_the_opening_fact_pins_its_protocol_version() -> None:
    data = task_opened_data(
        task_id="task-1",
        operation_id="op-1",
        proposal=proposal(),
        confirmation=confirmation(),
    )
    forged = dict(data, product_protocol_version=2)
    with pytest.raises(ProductProtocolError) as raised:
        rebuild_product_task("task-1", (envelope(1, PRODUCT_TASK_OPENED, forged),))
    assert raised.value.code == "product-protocol-version-unsupported"


async def test_mode_source_survives_replay() -> None:
    assembly = await build_assembly()
    summary = await opened(assembly, mode_source=TaskModeSource.PROFILE)
    assert summary.mode_source is TaskModeSource.PROFILE
    reloaded = await assembly.service.load("task-1")
    assert reloaded is not None and reloaded.mode_source is TaskModeSource.PROFILE
    await assembly.aclose()
