"""Standalone worker process used by ``tests/test_event_store_cross_process.py``.

This file is deliberately *not* named ``test_*`` so pytest never collects it.
Each command below runs in its own independent Python interpreter, which is the
only way to prove that ``JsonlEventStore`` serializes real OS processes rather
than just tasks inside one event loop.

Processes coordinate through explicit handshake files instead of fixed sleeps:
a worker signals readiness, then waits for the parent's start gate.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from traceh.api.events import PendingEvent
from traceh.session.event_store import ConcurrencyConflict
from traceh.session.file_lock import exclusive_file_lock
from traceh.session.jsonl import JsonlEventStore

HANDSHAKE_TIMEOUT_SECONDS = 60.0
HANDSHAKE_POLL_SECONDS = 0.005


def signal(path: str) -> None:
    Path(path).write_text("1", encoding="utf-8")


def wait_for(path: str, timeout: float = HANDSHAKE_TIMEOUT_SECONDS) -> None:
    target = Path(path)
    deadline = time.monotonic() + timeout
    while not target.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"handshake file never appeared: {target}")
        time.sleep(HANDSHAKE_POLL_SECONDS)


class WideWindowStore(JsonlEventStore):
    """Store that stretches the read-head -> write window inside the lock.

    The real race window between reading the stream head and writing the next
    event is only microseconds wide, so two processes would almost never
    collide by chance. Sleeping *inside* the critical section makes the overlap
    deterministic: with a genuine cross-process lock the other process simply
    waits its turn, without one it computes the same head and duplicates a seq.
    """

    def __init__(self, root: Path, hold_seconds: float) -> None:
        super().__init__(root)
        self.hold_seconds = hold_seconds

    def _head_unlocked(self, stream_id: str) -> int:
        head = super()._head_unlocked(stream_id)
        time.sleep(self.hold_seconds)
        return head


async def append_loop(
    root: str,
    stream: str,
    actor: str,
    count: int,
    hold_seconds: float,
    ready: str,
    start: str,
) -> dict:
    """Read head then append, ``count`` times, retrying on expected-seq conflicts."""

    store = WideWindowStore(Path(root), hold_seconds)
    signal(ready)
    wait_for(start)

    appended = 0
    conflicts = 0
    for index in range(count):
        while True:
            head = await store.head(stream)
            try:
                await store.append(
                    stream,
                    expected_seq=head,
                    events=(PendingEvent("turn/start", {"actor": actor, "index": index}),),
                )
            except ConcurrencyConflict:
                conflicts += 1
                continue
            appended += 1
            break
    return {"actor": actor, "appended": appended, "conflicts": conflicts}


async def race_once(
    root: str, stream: str, actor: str, hold_seconds: float, ready: str, start: str
) -> dict:
    """Read head, wait at the gate, then append with the now possibly stale head."""

    store = WideWindowStore(Path(root), hold_seconds)
    head = await store.head(stream)
    signal(ready)
    wait_for(start)
    try:
        await store.append(
            stream,
            expected_seq=head,
            events=(PendingEvent("turn/start", {"actor": actor}),),
        )
    except ConcurrencyConflict as conflict:
        return {
            "actor": actor,
            "expected_seq": head,
            "outcome": "conflict",
            "detail": str(conflict),
        }
    return {"actor": actor, "expected_seq": head, "outcome": "appended"}


async def append_once(root: str, stream: str, actor: str) -> dict:
    """Read head (repairing any partial tail) and append a single event."""

    store = JsonlEventStore(Path(root))
    head = await store.head(stream)
    materialized = await store.append(
        stream,
        expected_seq=head,
        events=(PendingEvent("turn/start", {"actor": actor}),),
    )
    return {"actor": actor, "head_before": head, "seq": materialized[0].seq}


def hold_lock(lock_path: str, held: str, release: str) -> dict:
    """Hold the OS lock on ``lock_path`` until the parent creates ``release``."""

    with exclusive_file_lock(Path(lock_path)):
        signal(held)
        wait_for(release)
    return {"held": True}


def hold_lock_and_die(lock_path: str, held: str) -> dict:
    """Acquire the lock and terminate without unlocking, simulating a crash."""

    descriptor_holder = exclusive_file_lock(Path(lock_path))
    descriptor_holder.__enter__()
    signal(held)
    sys.stdout.flush()
    os._exit(9)


def main(argv: list[str]) -> int:
    command, args = argv[1], argv[2:]
    if command == "append-loop":
        result = asyncio.run(
            append_loop(args[0], args[1], args[2], int(args[3]), float(args[4]), args[5], args[6])
        )
    elif command == "race-once":
        result = asyncio.run(
            race_once(args[0], args[1], args[2], float(args[3]), args[4], args[5])
        )
    elif command == "append-once":
        result = asyncio.run(append_once(args[0], args[1], args[2]))
    elif command == "hold-lock":
        result = hold_lock(args[0], args[1], args[2])
    elif command == "hold-lock-and-die":
        result = hold_lock_and_die(args[0], args[1])
    else:
        raise SystemExit(f"unknown worker command: {command!r}")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
