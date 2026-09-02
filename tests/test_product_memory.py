"""M2 requester memory Tool boundaries."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from product_fixtures import build_assembly, opened

from traceh.api.tools import ToolExecutionContext
from traceh.product.chat import ReadProductTaskEvidenceTool
from traceh.product.errors import ProductInputError, ProductStateError
from traceh.product.memory import ProductTaskMemoryReader
from traceh.product.observation import ProductObservationReader
from traceh.product.projection import ProductTaskStreamReader
from traceh.session.event_store import InMemoryEventStore


def _context(*, session_id: str = "session-memory") -> ToolExecutionContext:
    return ToolExecutionContext(
        session_id=session_id,
        turn_id="turn-memory",
        step_id="step-memory",
        tool_call_id="call-memory",
        workspace=Path("C:/memory-test/workspace"),
        data_dir=Path("C:/memory-test/data"),
    )


def _memory(store: InMemoryEventStore) -> ProductTaskMemoryReader:
    observation = cast(ProductObservationReader, SimpleNamespace(store=store))
    return ProductTaskMemoryReader(store, observation)


async def test_evidence_tool_rejects_non_exact_arguments_without_writing() -> None:
    store = InMemoryEventStore()
    tool = ReadProductTaskEvidenceTool(_memory(store))

    with pytest.raises(ProductInputError) as missing:
        await tool.execute({}, _context())
    assert missing.value.code == "product-evidence-arguments-invalid"
    with pytest.raises(ProductInputError):
        await tool.execute({"task_id": " task-memory "}, _context())
    assert await store.list_streams() == ()


class _BlockingReadStore(InMemoryEventStore):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()

    async def read(self, stream_id: str, *args, **kwargs):
        if stream_id == "product-task:task-blocked":
            self.entered.set()
            await asyncio.Event().wait()
        return await super().read(stream_id, *args, **kwargs)


async def test_evidence_tool_does_not_turn_cancellation_into_unavailable() -> None:
    store = _BlockingReadStore()
    tool = ReadProductTaskEvidenceTool(_memory(store))
    operation = asyncio.create_task(
        tool.execute({"task_id": "task-blocked"}, _context())
    )
    await asyncio.wait_for(store.entered.wait(), timeout=1)

    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert await store.list_streams() == ()


class _BlockingObservation:
    def __init__(self, store: InMemoryEventStore) -> None:
        self.store = store
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def load(self, task_id: str):
        self.entered.set()
        await self.release.wait()
        summary = await ProductTaskStreamReader(self.store).load(task_id)
        return SimpleNamespace(summary=summary)


async def test_memory_rejects_a_product_head_change_during_the_join() -> None:
    store = InMemoryEventStore()
    assembly = await build_assembly(store=store)
    observation = _BlockingObservation(store)
    memory = ProductTaskMemoryReader(
        store,
        cast(ProductObservationReader, observation),
    )
    try:
        await opened(assembly, task_id="task-head-race")
        captured = await memory.load_head("session-alpha", "task-head-race")
        operation = asyncio.create_task(
            memory.load_for_head("session-alpha", captured)
        )
        await asyncio.wait_for(observation.entered.wait(), timeout=1)
        await assembly.service.cancel_task(
            task_id="task-head-race",
            operation_id="task-head-race-cancel",
            reason_code="test-cancelled",
        )
        observation.release.set()

        with pytest.raises(ProductStateError) as changed:
            await operation
        assert changed.value.code == "product-memory-product-head-changed"
    finally:
        observation.release.set()
        await assembly.aclose()
