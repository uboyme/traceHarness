"""F3 UI observation handshake and pure command parsing."""

from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path

import pytest

import traceh.chat.driver as chat_driver_module
import traceh.cli.chat as line_chat_module
import traceh.cli.product as line_product_module
import traceh.product.chat as product_chat_module
import traceh.product.observation as product_observation_module
from traceh.agents.identity import AGENT_DIRECTORY_STREAM
from traceh.api.events import PendingEvent
from traceh.artifacts.events import ARTIFACT_CATALOG_STREAM
from traceh.product.chat import (
    ProductCommandOperation,
    parse_product_command,
)
from traceh.product.errors import ProductInputError, ProductStateError
from traceh.product.events import product_task_stream
from traceh.product.host import build_product_chat_host
from traceh.product.observation import (
    ObservedStreamHead,
    ProductObservation,
    ProductObservationSession,
)
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
        return ProductObservation(
            task_id=task_id,
            summary=None,
            workflow=None,
            evidence=None,
            review=None,
            approval=None,
            promotion=None,
            approval_digest=None,
            stream_heads=(ObservedStreamHead(related, head),),
        )


class _FailingReader:
    async def load(self, task_id: str) -> ProductObservation:
        del task_id
        raise RuntimeError("observation read failed")


async def test_subscribe_before_read_re_reads_every_discovered_exact_stream() -> None:
    feed = SessionEventFeed()
    store = PublishingEventStore(InMemoryEventStore(), feed)
    reader = _RaceReader(store)
    observer = ProductObservationSession(reader, feed, "task-race")  # type: ignore[arg-type]

    observation = await observer.start()

    assert reader.calls == 2
    assert "session:related-agent" in observer.subscribed_streams
    assert observation.stream_heads == (
        ObservedStreamHead("session:related-agent", 1),
    )

    await store.append(
        "session:related-agent",
        expected_seq=1,
        events=(PendingEvent(type="probe/finished", data={}),),
    )
    await observer.wait_dirty()
    refreshed = await observer.refresh()
    assert refreshed.stream_heads == (
        ObservedStreamHead("session:related-agent", 2),
    )
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
            PROMOTION_LEDGER_STREAM,
        )
    )
    assert not any(
        task.get_name() == "traceh-product-observer-task-failed-start"
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
    )


def test_product_host_requires_an_explicit_event_feed() -> None:
    parameter = inspect.signature(build_product_chat_host).parameters["event_feed"]
    assert parameter.default is inspect.Parameter.empty


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
