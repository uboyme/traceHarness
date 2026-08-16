from __future__ import annotations

import asyncio

import pytest

from traceh.api.services import ServiceKey
from traceh.kernel.activation import Activation
from traceh.kernel.hooks import HookDispatcher, HookKey, HookMode
from traceh.kernel.registry import ServiceRegistry
from traceh.kernel.scope import Scope


@pytest.mark.asyncio
async def test_activation_reverses_registrations_and_tasks() -> None:
    registry = ServiceRegistry()
    key = ServiceKey[str]("demo", 1)
    activation = Activation("demo.plugin")
    registration = await registry.provide(key, "value")
    activation.own(registration)

    started = asyncio.Event()

    async def worker() -> None:
        started.set()
        await asyncio.Event().wait()

    activation.tasks.spawn(worker(), name="owned-worker")
    await started.wait()
    activation.publish()
    assert registry.require(key) == "value"

    await activation.dispose()
    assert registry.get(key) is None
    assert activation.tasks.active_count == 0


@pytest.mark.asyncio
async def test_notify_hooks_isolate_failures_and_transform_is_ordered() -> None:
    hooks = HookDispatcher()
    notify = HookKey[int]("notify", HookMode.NOTIFY)
    transform = HookKey[int]("transform", HookMode.TRANSFORM)
    seen: list[int] = []

    async def good(value: int) -> None:
        seen.append(value)

    async def bad(value: int) -> None:
        del value
        raise RuntimeError("subscriber failed")

    async def plus_one(value: int) -> int:
        return value + 1

    async def times_two(value: int) -> int:
        return value * 2

    await hooks.register(notify, good)
    await hooks.register(notify, bad)
    await hooks.register(transform, plus_one)
    await hooks.register(transform, times_two)

    errors = await hooks.notify(notify, 7)
    assert seen == [7]
    assert len(errors) == 1
    assert await hooks.transform(transform, 3) == 8


def test_scope_resolution_prefers_nearest_scope() -> None:
    key = ServiceKey[str]("service", 1)
    root = Scope(name="root")
    child = Scope(name="child", parent=root)
    root.provide(key, "root")
    assert child.require(key) == "root"
    child.provide(key, "child")
    assert child.require(key) == "child"
