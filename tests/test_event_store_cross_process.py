"""Cross-process locking guarantees of ``JsonlEventStore``.

Every test here drives *independent Python processes*. Tasks, threads or two
store instances inside one interpreter cannot prove that the store survives two
concurrent ``traceh`` runs, because an ``asyncio.Lock`` is invisible to another
process and a ``.lock`` file merely existing locks nothing.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import traceh
from traceh.api.events import PendingEvent
from traceh.session.event_store import ConcurrencyConflict
from traceh.session.file_lock import (
    FileLockTimeout,
    exclusive_file_lock,
    locking_backend,
)
from traceh.session.jsonl import JsonlEventStore, _StreamLockSignals

WORKER = Path(__file__).with_name("cross_process_worker.py")
SRC_ROOT = Path(traceh.__file__).resolve().parents[1]
HANDSHAKE_TIMEOUT_SECONDS = 60.0
HANDSHAKE_POLL_SECONDS = 0.005
WORKER_TIMEOUT_SECONDS = 120.0


def spawn_worker(*args: object) -> subprocess.Popen[str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(SRC_ROOT) + (os.pathsep + existing if existing else "")
    return subprocess.Popen(
        [sys.executable, str(WORKER), *[str(arg) for arg in args]],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def finish_worker(process: subprocess.Popen[str]) -> dict:
    stdout, stderr = process.communicate(timeout=WORKER_TIMEOUT_SECONDS)
    assert process.returncode == 0, f"worker failed ({process.returncode}):\n{stderr}"
    return json.loads(stdout.strip().splitlines()[-1])


def wait_for_file(path: Path, timeout: float = HANDSHAKE_TIMEOUT_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, f"handshake file never appeared: {path}"
        time.sleep(HANDSHAKE_POLL_SECONDS)


class ObservableJsonlEventStore(JsonlEventStore):
    """Keeps every operation's lock signals so tests can observe the worker.

    Without this, a test could only guess when the background thread reached
    the lock; with it, ``signals.waiting`` is set by that thread itself.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.signals: list[_StreamLockSignals] = []

    def _new_lock_signals(self) -> _StreamLockSignals:
        signals = super()._new_lock_signals()
        self.signals.append(signals)
        return signals


class GatedJsonlEventStore(ObservableJsonlEventStore):
    """Holds the critical section open until the test opens the gate."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.entered_critical_section = threading.Event()
        self.gate = threading.Event()

    def _append_unlocked(self, *args: object) -> tuple:  # type: ignore[override]
        self.entered_critical_section.set()
        assert self.gate.wait(WORKER_TIMEOUT_SECONDS), "test never opened the gate"
        return super()._append_unlocked(*args)  # type: ignore[arg-type]


async def wait_for_lock_attempt(
    store: ObservableJsonlEventStore,
    index: int,
    wait_seconds: float = HANDSHAKE_TIMEOUT_SECONDS,
) -> _StreamLockSignals:
    """Block until operation ``index``'s worker thread is waiting for the lock."""

    deadline = time.monotonic() + wait_seconds
    while len(store.signals) <= index:
        assert time.monotonic() < deadline, "the store operation never started"
        await asyncio.sleep(HANDSHAKE_POLL_SECONDS)
    signals = store.signals[index]
    remaining = max(deadline - time.monotonic(), 0.0)
    assert await asyncio.to_thread(signals.waiting.wait, remaining), (
        "the worker thread never started waiting for the stream lock"
    )
    return signals


def stream_path(root: Path) -> Path | None:
    return next(root.glob("*.jsonl"), None)


def read_stream_lines(root: Path) -> list[dict]:
    path = stream_path(root)
    assert path is not None, f"no stream file was written under {root}"
    lines = path.read_text(encoding="utf-8").splitlines()
    # Every persisted line must be parseable JSON, including after concurrent
    # writers interleaved.
    return [json.loads(line) for line in lines]


async def append_with_retry(store: JsonlEventStore, stream: str, actor: str) -> int:
    while True:
        head = await store.head(stream)
        try:
            materialized = await store.append(
                stream,
                expected_seq=head,
                events=(PendingEvent("turn/start", {"actor": actor}),),
            )
        except ConcurrencyConflict:
            continue
        return materialized[0].seq


def test_locking_backend_matches_platform() -> None:
    backend = locking_backend()
    assert backend != "none", "every supported platform must expose a real OS lock"
    if sys.platform == "win32":
        assert backend == "msvcrt"
    else:
        assert backend == "fcntl"


def test_empty_lock_file_can_be_locked(tmp_path: Path) -> None:
    # Windows byte-range locks must work on a zero-byte lock file, which is the
    # state of every freshly created stream lock.
    lock_path = tmp_path / "empty.lock"
    lock_path.write_bytes(b"")
    with exclusive_file_lock(lock_path, timeout=5):
        assert lock_path.stat().st_size == 0


async def test_two_processes_append_concurrently_without_losing_events(tmp_path: Path) -> None:
    stream = "session:cross-process"
    per_worker = 20
    # Each worker holds the critical section open for this long, so the two
    # processes provably overlap instead of racing by chance.
    hold_seconds = 0.02
    ready_files = [tmp_path / "ready-a", tmp_path / "ready-b"]
    start_file = tmp_path / "start"

    processes = [
        spawn_worker(
            "append-loop",
            tmp_path,
            stream,
            actor,
            per_worker,
            hold_seconds,
            ready_file,
            start_file,
        )
        for actor, ready_file in zip(("alpha", "beta"), ready_files, strict=True)
    ]
    for ready_file in ready_files:
        wait_for_file(ready_file)
    start_file.write_text("go", encoding="utf-8")

    results = [finish_worker(process) for process in processes]
    assert [result["appended"] for result in results] == [per_worker, per_worker]

    records = read_stream_lines(tmp_path)
    assert [record["seq"] for record in records] == list(range(1, 2 * per_worker + 1))
    assert len({record["event_id"] for record in records}) == len(records)

    actors = [record["data"]["actor"] for record in records]
    assert actors.count("alpha") == per_worker
    assert actors.count("beta") == per_worker

    store = JsonlEventStore(tmp_path)
    events = await store.read(stream)
    assert [event.seq for event in events] == list(range(1, 2 * per_worker + 1))


async def test_expected_seq_race_across_processes_produces_one_conflict(tmp_path: Path) -> None:
    stream = "session:expected-seq-race"
    ready_files = [tmp_path / "ready-a", tmp_path / "ready-b"]
    start_file = tmp_path / "start"

    # Both processes read head 0 before the gate opens, so both then try to
    # append seq 1 with expected_seq 0. The in-lock hold guarantees the second
    # process is inside its own append attempt while the first still holds the
    # lock, which is exactly the window that used to corrupt Windows streams.
    processes = [
        spawn_worker("race-once", tmp_path, stream, actor, 0.3, ready_file, start_file)
        for actor, ready_file in zip(("alpha", "beta"), ready_files, strict=True)
    ]
    for ready_file in ready_files:
        wait_for_file(ready_file)
    start_file.write_text("go", encoding="utf-8")

    results = [finish_worker(process) for process in processes]
    assert {result["expected_seq"] for result in results} == {0}
    outcomes = sorted(result["outcome"] for result in results)
    assert outcomes == ["appended", "conflict"], results

    conflict = next(result for result in results if result["outcome"] == "conflict")
    assert "expected seq 0" in conflict["detail"]
    assert "current seq is 1" in conflict["detail"]

    records = read_stream_lines(tmp_path)
    assert [record["seq"] for record in records] == [1]


async def test_partial_tail_repair_still_works_across_processes(tmp_path: Path) -> None:
    stream = "session:partial-tail"
    store = JsonlEventStore(tmp_path)
    await store.append(
        stream,
        expected_seq=0,
        events=(PendingEvent("session/created", {"actor": "parent"}),),
    )
    torn_path = stream_path(tmp_path)
    assert torn_path is not None
    with torn_path.open("ab") as handle:
        handle.write(b'{"event_id":"half-written')

    # A separate process must repair the torn tail under the same OS lock.
    result = finish_worker(spawn_worker("append-once", tmp_path, stream, "child"))
    assert result == {"actor": "child", "head_before": 1, "seq": 2}

    records = read_stream_lines(tmp_path)
    assert [record["seq"] for record in records] == [1, 2]
    events = await store.read(stream)
    assert [event.data["actor"] for event in events] == ["parent", "child"]


async def test_store_operations_block_while_another_process_holds_the_lock(
    tmp_path: Path,
) -> None:
    stream = "session:held"
    store = JsonlEventStore(tmp_path, lock_timeout=0.4)
    lock_path = store._lock_path(stream)  # the stream's OS-level lock file
    held_file = tmp_path / "held"
    release_file = tmp_path / "release"

    holder = spawn_worker("hold-lock", lock_path, held_file, release_file)
    try:
        wait_for_file(held_file)
        # No fcntl/msvcrt lock means these would silently succeed and race.
        with pytest.raises(FileLockTimeout):
            await store.head(stream)
        with pytest.raises(FileLockTimeout):
            await store.append(
                stream,
                expected_seq=0,
                events=(PendingEvent("turn/start", {"actor": "blocked"}),),
            )
        with pytest.raises(FileLockTimeout):
            await store.read(stream)
        assert stream_path(tmp_path) is None, "a blocked append must not write anything"
    finally:
        release_file.write_text("go", encoding="utf-8")
        assert finish_worker(holder) == {"held": True}

    # Once the other process releases, the very same store proceeds normally.
    assert await store.head(stream) == 0
    assert await append_with_retry(store, stream, "after-release") == 1


async def test_lock_is_reacquirable_after_holder_crashes(tmp_path: Path) -> None:
    stream = "session:crashed-holder"
    store = JsonlEventStore(tmp_path, lock_timeout=10)
    lock_path = store._lock_path(stream)
    held_file = tmp_path / "held"

    crasher = spawn_worker("hold-lock-and-die", lock_path, held_file)
    wait_for_file(held_file)
    assert crasher.wait(timeout=WORKER_TIMEOUT_SECONDS) == 9
    crasher.stdout.close()
    crasher.stderr.close()

    # The OS drops the lock when the crashed process' descriptors close, so no
    # stale lock file can wedge the stream forever.
    assert await append_with_retry(store, stream, "after-crash") == 1
    assert [record["seq"] for record in read_stream_lines(tmp_path)] == [1]


async def test_lock_is_released_after_errors_inside_the_critical_section(
    tmp_path: Path,
) -> None:
    stream = "session:error-path"
    store = JsonlEventStore(tmp_path, lock_timeout=5)
    await store.append(
        stream,
        expected_seq=0,
        events=(PendingEvent("session/created", {"actor": "parent"}),),
    )

    with pytest.raises(ConcurrencyConflict):
        await store.append(
            stream,
            expected_seq=0,
            events=(PendingEvent("turn/start", {"actor": "stale"}),),
        )
    # A conflict must not leave the stream lock held by this process.
    assert await store.head(stream) == 1

    lock_path = store._lock_path(stream)
    with pytest.raises(RuntimeError, match="boom"):
        with exclusive_file_lock(lock_path, timeout=5):
            raise RuntimeError("boom")
    with exclusive_file_lock(lock_path, timeout=5):
        pass

    # And another process can still take the lock afterwards.
    result = finish_worker(spawn_worker("append-once", tmp_path, stream, "child"))
    assert result["seq"] == 2


async def test_cancelling_append_while_waiting_for_the_lock_never_writes(
    tmp_path: Path,
) -> None:
    stream = "session:cancel-while-waiting"
    store = ObservableJsonlEventStore(tmp_path)
    lock_path = store._lock_path(stream)
    held_file = tmp_path / "held"
    release_file = tmp_path / "release"

    holder = spawn_worker("hold-lock", lock_path, held_file, release_file)
    try:
        wait_for_file(held_file)
        pending = asyncio.create_task(
            store.append(
                stream,
                expected_seq=0,
                events=(PendingEvent("turn/start", {"actor": "cancelled"}),),
            )
        )
        # Confirmed by the worker thread itself, not guessed from timing.
        signals = await wait_for_lock_attempt(store, 0)

        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

        # The holder still owns the lock, so the only way the worker can have
        # converged is by abandoning the wait. And convergence must already be
        # visible here: nothing may run in the background past this point.
        assert signals.cancel.is_set()
        assert signals.finished.is_set(), "worker thread outlived the CancelledError"
        assert stream_path(tmp_path) is None, "a cancelled append must not write"
    finally:
        release_file.write_text("go", encoding="utf-8")
        assert finish_worker(holder) == {"held": True}

    # Releasing the external lock must not resurrect the cancelled append.
    assert await append_with_retry(store, stream, "after-cancel") == 1
    events = await store.read(stream)
    assert [event.data["actor"] for event in events] == ["after-cancel"]


async def test_cancelling_head_and_read_while_waiting_for_the_lock_converges(
    tmp_path: Path,
) -> None:
    stream = "session:cancel-readers"
    store = ObservableJsonlEventStore(tmp_path)
    await store.append(
        stream,
        expected_seq=0,
        events=(PendingEvent("session/created", {"actor": "seed"}),),
    )
    lock_path = store._lock_path(stream)
    held_file = tmp_path / "held"
    release_file = tmp_path / "release"

    holder = spawn_worker("hold-lock", lock_path, held_file, release_file)
    try:
        wait_for_file(held_file)
        # signals[0] belongs to the seed append above.
        for index, operation in enumerate((store.head(stream), store.read(stream)), start=1):
            pending = asyncio.create_task(operation)
            signals = await wait_for_lock_attempt(store, index)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert signals.finished.is_set(), "reader thread outlived the CancelledError"
    finally:
        release_file.write_text("go", encoding="utf-8")
        assert finish_worker(holder) == {"held": True}

    assert await store.head(stream) == 1
    assert [event.data["actor"] for event in await store.read(stream)] == ["seed"]


async def test_cancel_inside_the_critical_section_completes_atomically(
    tmp_path: Path,
) -> None:
    stream = "session:atomic-completion"
    store = GatedJsonlEventStore(tmp_path)

    pending = asyncio.create_task(
        store.append(
            stream,
            expected_seq=0,
            events=(PendingEvent("turn/start", {"actor": "in-critical-section"}),),
        )
    )
    assert await asyncio.to_thread(
        store.entered_critical_section.wait, WORKER_TIMEOUT_SECONDS
    ), "the worker never reached the critical section"

    pending.cancel()
    # Once the uninterruptible critical section is running, the caller must not
    # be released while the file can still change underneath it.
    _, still_running = await asyncio.wait({pending}, timeout=0.5)
    assert still_running == {pending}, "cancellation abandoned a running critical section"
    assert stream_path(tmp_path) is None

    store.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert store.signals[0].finished.is_set()

    # Atomic completion: the write is whole, and the file is final by the time
    # the caller resumes.
    records = read_stream_lines(tmp_path)
    assert [record["seq"] for record in records] == [1]
    settled = stream_path(tmp_path)
    assert settled is not None
    snapshot = settled.read_bytes()
    assert await store.head(stream) == 1
    assert settled.read_bytes() == snapshot


async def test_repeated_cancellation_cannot_cut_convergence_short(tmp_path: Path) -> None:
    stream = "session:double-cancel"
    store = GatedJsonlEventStore(tmp_path)

    pending = asyncio.create_task(
        store.append(
            stream,
            expected_seq=0,
            events=(PendingEvent("turn/start", {"actor": "double-cancelled"}),),
        )
    )
    assert await asyncio.to_thread(
        store.entered_critical_section.wait, WORKER_TIMEOUT_SECONDS
    ), "the worker never reached the critical section"
    signals = store.signals[0]

    pending.cancel()
    _, still_running = await asyncio.wait({pending}, timeout=0.2)
    assert still_running == {pending}
    # The cancel token is only set on the convergence path, so this proves the
    # coroutine is now waiting for the worker rather than guessing from timing.
    assert signals.cancel.is_set(), "the first cancellation never reached convergence"
    assert not signals.finished.is_set()

    # Further cancellations must be absorbed, not used as an early exit.
    for attempt in range(5):
        pending.cancel()
        _, still_running = await asyncio.wait({pending}, timeout=0.1)
        assert still_running == {pending}, f"cancel #{attempt + 2} released the caller early"
        assert not signals.finished.is_set(), "the caller outran its own worker"

    store.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    # Original semantics kept: the caller is still cancelled, but only after the
    # worker converged and released the lock.
    assert signals.finished.is_set()

    records = read_stream_lines(tmp_path)
    assert [record["seq"] for record in records] == [1]
    assert [record["data"]["actor"] for record in records] == ["double-cancelled"]
    settled = stream_path(tmp_path)
    assert settled is not None
    snapshot = settled.read_bytes()
    assert await store.head(stream) == 1
    assert settled.read_bytes() == snapshot
