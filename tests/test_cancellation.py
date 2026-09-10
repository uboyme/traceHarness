from __future__ import annotations

import asyncio
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from traceh.api.events import EventEnvelope, PendingEvent
from traceh.api.llm import ModelRequest, ModelResponse
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.process_control import converge_process
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.session.event_store import Durability, InMemoryEventStore
from traceh.session.file_lock import FileLockTimeout, exclusive_file_lock
from traceh.session.invariants import CoreInvariantChecker
from traceh.tools.builtins.shell import sanitized_environment

#: Hard self-deadline every helper process gets, so no failure path can leave a
#: test process running. The outer watchdog is always a multiple of this.
CHILD_DEADLINE_SECONDS = 60

# The child announces itself, holds an OS lock for a while, and only then writes
# `finished`. Holding the lock makes "is it still alive?" answerable without
# guessing, because the operating system releases it when the process dies. The
# lock is taken with the plain standard library on the same byte the store's
# `exclusive_file_lock` uses, so the two interoperate without the child having
# to import TraceHarness.
SLOW_CHILD = """
import os
import sys
import time

try:
    import fcntl
except ImportError:
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None

started, finished, lock, seconds = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
if fcntl is not None:
    fcntl.flock(descriptor, fcntl.LOCK_EX)
else:
    os.lseek(descriptor, 0, os.SEEK_SET)
    msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)

with open(started, "w", encoding="utf-8") as handle:
    handle.write("started")
time.sleep(seconds)
with open(finished, "w", encoding="utf-8") as handle:
    handle.write("finished")
"""


# Same idea, plus a grandchild that inherits the captured output handles and
# outlives its parent. It is what proves the child's own shutdown does not wait
# on descendants, and it stays alive across the whole convergence window.


async def cancel_again_and_assert_still_running(task: asyncio.Task, attempts: int = 2) -> None:
    for attempt in range(attempts):
        task.cancel()
        # Let the loop actually deliver and handle the cancellation.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not task.done(), f"cancel #{attempt + 2} released the caller mid-convergence"


def slow_child_command(tmp_path: Path, *, seconds: float) -> tuple[str, Path, Path, Path]:
    script = tmp_path / "slow_child.py"
    script.write_text(SLOW_CHILD, encoding="utf-8")
    started = tmp_path / "started"
    finished = tmp_path / "finished"
    lock = tmp_path / "child.lock"
    command = shlex.join(
        [sys.executable, str(script), str(started), str(finished), str(lock), str(seconds)]
    )
    return command, started, finished, lock


def signalled(path: Path) -> bool:
    return path.exists()


async def wait_for_file(path: Path, seconds: float = 30.0) -> None:
    """Wait for the child's own signal instead of guessing how long it takes."""

    deadline = time.monotonic() + seconds
    while not signalled(path):
        assert time.monotonic() < deadline, f"child never signalled through {path}"
        await asyncio.sleep(0.005)


def assert_child_has_exited(lock: Path) -> None:
    """The lock is only free once the operating system reaped the process."""

    try:
        with exclusive_file_lock(lock, timeout=5.0):
            pass
    except FileLockTimeout as error:  # pragma: no cover - only on a real leak
        raise AssertionError("the child process is still running") from error


def wait_until_lock_is_free(lock: Path, seconds: float = 15.0) -> bool:
    """Wait for the recorded child to be reaped. Never raises; used in cleanup."""

    if not lock.exists():
        return True
    try:
        with exclusive_file_lock(lock, timeout=seconds):
            return True
    except (FileLockTimeout, OSError):  # pragma: no cover - cleanup best effort
        return False


def terminate_recorded_processes(pid_file: Path) -> None:
    """Stop exactly the processes this test recorded, and nothing else.

    No matching by name or by command line: only pids the child itself wrote
    down are signalled, so a failing test can never take an unrelated process
    with it.
    """

    if not pid_file.exists():
        return
    for token in pid_file.read_text(encoding="utf-8").split():
        try:
            pid = int(token)
        except ValueError:  # pragma: no cover - defensive
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except (OSError, ValueError):
            # Already gone, or never ours to signal.
            pass


class _GatedProvider:
    name = "gated"

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        self.entered.set()
        await self.release.wait()
        return ModelResponse(content="too late")


class _TerminalGateStore:
    """Hold each cancelled Turn terminal before it becomes durable."""

    _TERMINALS = ("model/attempt-end", "step/end", "turn/end")

    def __init__(self) -> None:
        self.inner = InMemoryEventStore()
        self.entered = {name: asyncio.Event() for name in self._TERMINALS}
        self.release = {name: asyncio.Event() for name in self._TERMINALS}

    async def append(
        self,
        stream_id: str,
        *,
        expected_seq: int,
        events: tuple[PendingEvent, ...],
        durability: Durability = Durability.SYNC,
    ) -> tuple[EventEnvelope, ...]:
        for event in events:
            if event.type in self.entered:
                self.entered[event.type].set()
                await self.release[event.type].wait()
        return await self.inner.append(
            stream_id,
            expected_seq=expected_seq,
            events=events,
            durability=durability,
        )

    async def read(self, stream_id: str, *, from_seq: int = 1) -> tuple[EventEnvelope, ...]:
        return await self.inner.read(stream_id, from_seq=from_seq)

    async def head(self, stream_id: str) -> int:
        return await self.inner.head(stream_id)

    async def list_streams(self, *, prefix: str | None = None) -> tuple[str, ...]:
        return await self.inner.list_streams(prefix=prefix)


class _FailingAttemptEndStore(_TerminalGateStore):
    def __init__(self) -> None:
        super().__init__()
        for release in self.release.values():
            release.set()

    async def append(
        self,
        stream_id: str,
        *,
        expected_seq: int,
        events: tuple[PendingEvent, ...],
        durability: Durability = Durability.SYNC,
    ) -> tuple[EventEnvelope, ...]:
        if any(event.type == "model/attempt-end" for event in events):
            raise OSError("deterministic terminal append failure")
        return await super().append(
            stream_id,
            expected_seq=expected_seq,
            events=events,
            durability=durability,
        )


@pytest.mark.asyncio
async def test_cancel_closes_turn_and_reaches_quiescence(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = ScriptedLlmProvider(
        (ModelResponse(content="late"),),
        delay_seconds=10,
    )
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data", provider="scripted", model="slow"),
        provider=provider,
        event_store=InMemoryEventStore(),
    )
    session_id = await runtime.create_session(workspace)
    waiter = asyncio.create_task(runtime.run_existing(session_id, "wait"))

    for _ in range(100):
        events = await runtime.sessions.read_session(session_id)
        if any(event.type == "model/attempt-start" for event in events):
            break
        await asyncio.sleep(0.01)
    assert await runtime.cancel(session_id, reason="test cancellation")
    with pytest.raises(asyncio.CancelledError):
        await waiter

    events = await runtime.sessions.read_session(session_id)
    assert events[-1].type == "turn/end"
    assert events[-1].data["reason"] == "cancelled"
    assert not await runtime.check_invariants(session_id)
    await runtime.dispose()


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_attempt_step_and_turn_facts(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = _GatedProvider()
    store = _TerminalGateStore()
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data", provider="gated", model="slow"),
        provider=provider,
        event_store=store,
    )
    session_id = await runtime.create_session(workspace)
    running = asyncio.create_task(runtime.run_existing(session_id, "wait"))
    try:
        await asyncio.wait_for(provider.entered.wait(), timeout=10)
        running.cancel()

        for event_type in _TerminalGateStore._TERMINALS:
            await asyncio.wait_for(store.entered[event_type].wait(), timeout=10)
            await cancel_again_and_assert_still_running(running)
            assert not any(
                event.type == event_type
                for event in await runtime.sessions.read_session(session_id)
            )
            store.release[event_type].set()

        with pytest.raises(asyncio.CancelledError):
            await running

        events = await runtime.sessions.read_session(session_id)
        assert tuple(event.type for event in events[-3:]) == (
            "model/attempt-end",
            "step/end",
            "turn/end",
        )
        assert events[-3].data["status"] == "cancelled"
        assert events[-2].data["reason"] == "cancelled"
        assert events[-1].data["reason"] == "cancelled"
        assert CoreInvariantChecker().check(events) == ()
        durable_ids = tuple(event.event_id for event in events)
        await asyncio.sleep(0)
        assert (
            tuple(event.event_id for event in await runtime.sessions.read_session(session_id))
            == durable_ids
        )
    finally:
        for release in store.release.values():
            release.set()
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        await runtime.dispose()


@pytest.mark.asyncio
async def test_cancelled_turn_keeps_a_finalizer_failure_visible(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    provider = _GatedProvider()
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data", provider="gated", model="slow"),
        provider=provider,
        event_store=_FailingAttemptEndStore(),
    )
    session_id = await runtime.create_session(workspace)
    running = asyncio.create_task(runtime.run_existing(session_id, "wait"))
    await asyncio.wait_for(provider.entered.wait(), timeout=10)

    running.cancel()
    with pytest.raises(BaseExceptionGroup) as caught:
        await running

    assert any(isinstance(error, asyncio.CancelledError) for error in caught.value.exceptions)
    assert any(isinstance(error, OSError) for error in caught.value.exceptions)
    await runtime.dispose()


@pytest.mark.asyncio
async def test_sanitized_environment_can_still_start_a_python_child() -> None:
    # Without the platform essentials a Windows child dies importing asyncio,
    # which would silently break every verifier command.
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import asyncio; print('ok')",
        env=sanitized_environment(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()

    assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
    assert stdout.decode("utf-8", errors="replace").strip() == "ok"
    assert not any("KEY" in name or "TOKEN" in name for name in sanitized_environment())


def test_sanitized_environment_preserves_only_a_local_offline_pip_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheelhouse = tmp_path / "wheel house"
    wheelhouse.mkdir()
    wheelhouse_uri = wheelhouse.resolve().as_uri()
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_FIND_LINKS", wheelhouse_uri)
    monkeypatch.setenv("PIP_INDEX_URL", "https://credential.invalid/simple")

    environment = sanitized_environment()

    assert environment["PIP_NO_INDEX"] == "1"
    assert environment["PIP_FIND_LINKS"] == wheelhouse_uri
    assert "%20" in environment["PIP_FIND_LINKS"]
    assert len(environment["PIP_FIND_LINKS"].split()) == 1
    assert "PIP_INDEX_URL" not in environment

    rejected_values = (
        str(wheelhouse.resolve()),
        f"{wheelhouse_uri} https://credential.invalid/wheels",
        "https://credential.invalid/wheels",
        "file://credential.invalid/wheels",
        (tmp_path / "missing-wheelhouse").resolve().as_uri(),
        f"{wheelhouse_uri}?source=unexpected",
        f"{wheelhouse_uri}#unexpected",
    )
    for rejected in rejected_values:
        monkeypatch.setenv("PIP_FIND_LINKS", rejected)
        environment = sanitized_environment()
        assert "PIP_NO_INDEX" not in environment
        assert "PIP_FIND_LINKS" not in environment


@pytest.mark.asyncio
async def test_converge_process_does_not_return_until_the_child_is_gone(tmp_path) -> None:
    command, started, finished, lock = slow_child_command(tmp_path, seconds=30)
    process = await asyncio.create_subprocess_exec(*shlex.split(command))
    await wait_for_file(started)

    await converge_process(process)

    assert process.returncode is not None
    assert not finished.exists()
    assert_child_has_exited(lock)


# Writes to both streams, flushes them, and only then drops the marker: if the
# marker exists, the output really was produced before the process was stopped.


# Runs one verifier timeout in its own interpreter so the event loop really
# closes afterwards. That is when a leaked transport turns into
# "Event loop is closed" / "unclosed transport" on stderr.

# The child holds a lock (so the test can prove it exited), spawns a grandchild
# that inherits the captured output handles and outlives the whole shutdown, and
# writes output before hanging.


def test_terminate_recorded_processes_stops_the_pids_it_was_given(tmp_path) -> None:
    # The cleanup path every failing test relies on: it must actually work, and
    # it must only touch pids that were written down for it.
    script = tmp_path / "recorded_child.py"
    script.write_text(
        "import os, sys, time\n"
        "with open(sys.argv[1], 'w', encoding='utf-8') as handle:\n"
        "    handle.write(str(os.getpid()))\n"
        f"time.sleep({CHILD_DEADLINE_SECONDS})\n",
        encoding="utf-8",
    )
    pids = tmp_path / "pids"
    process = subprocess.Popen([sys.executable, str(script), str(pids)])
    try:
        deadline = time.monotonic() + 30
        while not pids.exists():
            assert time.monotonic() < deadline, "child never recorded its pid"
            time.sleep(0.01)

        terminate_recorded_processes(pids)

        assert process.wait(timeout=30) is not None
    finally:
        if process.poll() is None:  # pragma: no cover - only if cleanup failed
            process.kill()
            process.wait(timeout=30)
    # A Windows venv may launch the base interpreter through a small
    # ``Scripts/python.exe`` redirector.  ``Popen.pid`` then identifies the
    # redirector while ``os.getpid()`` in the script identifies the process the
    # cleanup helper must actually stop.  The child-authored pid file is the
    # ownership fact; requiring it to equal the launcher pid makes this test
    # fail in the exact clean-venv environment L2 is meant to exercise.
    recorded_pid = int(pids.read_text(encoding="utf-8").strip())
    assert recorded_pid > 0
    assert process.returncode is not None


@pytest.mark.asyncio
async def test_python_child_returns_utf8_chinese_bytes() -> None:
    text = "验证命令的中文输出必须原样返回"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import sys; sys.stdout.write(sys.argv[1])",
        text,
        env=sanitized_environment(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()

    assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
    # Strict decoding on purpose: errors="replace" would hide a CP936 child.
    assert stdout.decode("utf-8") == text
    assert "�" not in stdout.decode("utf-8")


# Sandbox command cancellation, timeout output and interpreter shutdown now
# run through public owner scopes in test_sandbox_verification.py,
# test_sandbox_promotion.py and test_tool_runtime_failures.py. Native helpers
# above remain for trusted host control processes, not user code execution.
