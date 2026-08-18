"""OwnedTaskSet owns task *outcomes*, not just task lifetimes.

A task that raises before shutdown completes on its own. Its done callback used
to only drop it from the set, so ``cancel_and_wait()`` never saw it and never
retrieved its exception - and asyncio then reported "Task exception was never
retrieved" from the garbage collector.

Every test here installs a real event-loop exception handler rather than reading
stderr, so the assertion is on what asyncio actually reported, and forces a
collection so a deferred report cannot slip past the end of the test.
"""

from __future__ import annotations

import asyncio
import gc

import pytest

from traceh.kernel.tasks import OwnedTaskSet


@pytest.fixture
async def loop_reports():
    """Collect everything the running loop reports during the test."""

    loop = asyncio.get_running_loop()
    reports: list[dict] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reports.append(context))
    yield reports
    loop.set_exception_handler(previous)


async def settle(times: int = 5) -> None:
    """Let the loop actually run, then force a collection."""

    for _ in range(times):
        await asyncio.sleep(0)
    gc.collect()
    for _ in range(times):
        await asyncio.sleep(0)


def never_retrieved(reports: list[dict]) -> list[dict]:
    return [item for item in reports if "never retrieved" in str(item.get("message", ""))]


async def failing() -> None:
    raise RuntimeError("owned background work failed")


async def succeeding() -> int:
    return 42


async def forever() -> None:
    await asyncio.Event().wait()


# --------------------------------------------------------------------------
# The defect
# --------------------------------------------------------------------------


async def test_task_failing_before_dispose_reports_nothing(loop_reports) -> None:
    owner = OwnedTaskSet()
    owner.spawn(failing(), name="t")

    await settle()

    assert never_retrieved(loop_reports) == [], "exception was left for the garbage collector"


async def test_task_failing_before_dispose_reports_nothing_after_dispose(
    loop_reports,
) -> None:
    """The failure must stay owned across shutdown and collection, too."""

    owner = OwnedTaskSet()
    owner.spawn(failing(), name="t")
    await settle()
    assert never_retrieved(loop_reports) == []

    await owner.cancel_and_wait()
    del owner
    await settle()

    assert never_retrieved(loop_reports) == []


async def test_the_exception_is_retrieved_but_not_retained() -> None:
    """Retrieval is the whole job; keeping the exception object is not.

    An earlier revision stored every failure in an unbounded list nothing read.
    Each entry held a traceback, and through it every frame's locals - untrusted
    plugin state kept alive for no consumer.
    """

    owner = OwnedTaskSet()
    owner.spawn(failing(), name="t")

    await settle()

    assert not hasattr(owner, "failures")
    assert not any(
        isinstance(value, (list, tuple)) and value
        for value in vars(owner).values()
        if not isinstance(value, (set, dict))
    ), "the owner is retaining something after the task finished"


async def test_task_still_running_at_dispose_is_also_retrieved(loop_reports) -> None:
    """The gather in shutdown covers tasks that had not finished yet."""

    gate = asyncio.Event()

    async def blocked_then_failing() -> None:
        await gate.wait()
        raise RuntimeError("late failure")

    owner = OwnedTaskSet()
    task = owner.spawn(blocked_then_failing(), name="t")
    await settle()
    assert not task.done()

    await owner.cancel_and_wait()
    del owner
    await settle()

    assert task.cancelled()
    assert never_retrieved(loop_reports) == []


# --------------------------------------------------------------------------
# Nothing else may be misreported
# --------------------------------------------------------------------------


async def test_successful_task_is_not_recorded_as_a_failure(loop_reports) -> None:
    owner = OwnedTaskSet()
    task = owner.spawn(succeeding(), name="t")

    await settle()

    assert task.result() == 42
    assert never_retrieved(loop_reports) == []


async def test_cancelled_task_is_not_recorded_as_a_failure(loop_reports) -> None:
    """Cancellation is the expected shutdown outcome, not a failure."""

    owner = OwnedTaskSet()
    task = owner.spawn(forever(), name="t")
    await settle()

    await owner.cancel_and_wait()
    await settle()

    assert task.cancelled()
    assert never_retrieved(loop_reports) == []


async def test_mixed_outcomes_record_only_the_failure(loop_reports) -> None:
    owner = OwnedTaskSet()
    owner.spawn(succeeding(), name="ok")
    owner.spawn(failing(), name="bad")
    owner.spawn(forever(), name="blocked")
    await settle()

    await owner.cancel_and_wait()
    del owner
    await settle()

    assert never_retrieved(loop_reports) == []


async def test_repeated_failures_stay_silent_and_accumulate_nothing(loop_reports) -> None:
    owner = OwnedTaskSet()

    async def raising(label: str) -> None:
        raise ValueError(label)

    for index in range(5):
        owner.spawn(raising(f"failure-{index}"), name=f"t{index}")
        await settle()

    assert never_retrieved(loop_reports) == []
    assert owner.active_count == 0


# --------------------------------------------------------------------------
# Scope: ownership, not supervision
# --------------------------------------------------------------------------


async def test_a_failing_task_does_not_make_shutdown_fail() -> None:
    """v0.4 takes ownership of the exception; it does not escalate it."""

    owner = OwnedTaskSet()
    owner.spawn(failing(), name="t")
    await settle()

    await owner.cancel_and_wait()  # must not raise

    assert owner.active_count == 0


async def test_a_failing_task_does_not_stop_later_spawns() -> None:
    owner = OwnedTaskSet()
    owner.spawn(failing(), name="a")
    await settle()

    later = owner.spawn(succeeding(), name="b")
    await settle()

    assert later.result() == 42


async def test_owner_state_does_not_grow_with_failures() -> None:
    """No unbounded store: a hundred failures leave the owner the same size."""

    owner = OwnedTaskSet()
    baseline = len(owner._tasks)

    for index in range(100):
        owner.spawn(failing(), name=f"t{index}")
    await settle()

    assert len(owner._tasks) == baseline == 0


async def test_active_count_ignores_finished_tasks() -> None:
    owner = OwnedTaskSet()
    owner.spawn(failing(), name="a")
    owner.spawn(forever(), name="b")
    await settle()

    assert owner.active_count == 1

    await owner.cancel_and_wait()
    assert owner.active_count == 0


async def test_spawn_after_close_is_rejected_without_a_pending_coroutine_warning(
    loop_reports,
) -> None:
    owner = OwnedTaskSet()
    await owner.cancel_and_wait()

    with pytest.raises(RuntimeError, match="task owner is closed"):
        owner.spawn(succeeding(), name="t")

    await settle()
    assert never_retrieved(loop_reports) == []
