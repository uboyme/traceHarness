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

import traceh
from traceh import process_control
from traceh.api.llm import ModelResponse
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.process_control import converge_process
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.runtime.continuation import (
    Continue,
    DefaultContinuationRuntime,
    VerificationFeedback,
)
from traceh.runtime.verification import SUMMARY_TAIL_CHARS, CommandVerifier
from traceh.session.file_lock import FileLockTimeout, exclusive_file_lock
from traceh.tools.builtins.shell import sanitized_environment

SRC_ROOT = Path(traceh.__file__).resolve().parents[1]

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
HANDLE_HOLDING_CHILD = """
import os
import subprocess
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

started, finished, lock, release = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
CHILD_DEADLINE_SECONDS = 60

descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
if fcntl is not None:
    fcntl.flock(descriptor, fcntl.LOCK_EX)
else:
    os.lseek(descriptor, 0, os.SEEK_SET)
    msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)

holder = (
    "import sys, time\\n"
    "from pathlib import Path\\n"
    "release = Path(sys.argv[1])\\n"
    "deadline = time.monotonic() + 60\\n"
    "while not release.exists() and time.monotonic() < deadline:\\n"
    "    time.sleep(0.01)\\n"
)
subprocess.Popen([sys.executable, "-c", holder, release])

with open(started, "w", encoding="utf-8") as handle:
    handle.write("started")
time.sleep(CHILD_DEADLINE_SECONDS)
with open(finished, "w", encoding="utf-8") as handle:
    handle.write("finished")
"""


class GatedChild:
    """A verifier/shell command whose cleanup can be held open on purpose."""

    def __init__(self, tmp_path: Path) -> None:
        script = tmp_path / "handle_holding_child.py"
        script.write_text(HANDLE_HOLDING_CHILD, encoding="utf-8")
        self.started = tmp_path / "started"
        self.finished = tmp_path / "finished"
        self.lock = tmp_path / "child.lock"
        self.release_path = tmp_path / "release"
        self.command = shlex.join(
            [
                sys.executable,
                str(script),
                str(self.started),
                str(self.finished),
                str(self.lock),
                str(self.release_path),
            ]
        )

    def release(self) -> None:
        self.release_path.write_text("go", encoding="utf-8")


def observe_converging(monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
    """Signal the moment convergence starts waiting for the child to exit.

    The wrapper only lights the signal and delegates, so the behaviour under
    test - absorbing further cancellation - is still the production one.
    """

    converging = asyncio.Event()
    original = process_control._await_exit

    async def observed(process, limit_seconds):
        converging.set()
        return await original(process, limit_seconds)

    monkeypatch.setattr(process_control, "_await_exit", observed)
    return converging


def deaf_to_terminate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the child ignore `terminate()` so the grace period really elapses.

    Windows terminates unconditionally, so this is the portable way to hold the
    coroutine inside the cancellation-absorbing wait long enough to cancel it
    again. The child is still killed at the end of the grace period.
    """

    monkeypatch.setattr(asyncio.subprocess.Process, "terminate", lambda self: None)


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


@pytest.mark.asyncio
async def test_cancelled_verifier_leaves_no_running_child(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    command, started, finished, lock = slow_child_command(tmp_path, seconds=30)
    task = asyncio.create_task(CommandVerifier(command, timeout_seconds=60).verify(workspace))
    await wait_for_file(started)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The caller has resumed, so the verifier command must already be dead.
    assert_child_has_exited(lock)
    assert not finished.exists()


@pytest.mark.asyncio
async def test_repeated_cancellation_during_verifier_convergence(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    child = GatedChild(tmp_path)
    deaf_to_terminate(monkeypatch)
    converging = observe_converging(monkeypatch)
    task = asyncio.create_task(
        CommandVerifier(child.command, timeout_seconds=60).verify(workspace)
    )
    await wait_for_file(child.started)

    task.cancel()
    # Deterministic proof that cleanup is under way rather than a guessed delay.
    await asyncio.wait_for(converging.wait(), timeout=30)
    await cancel_again_and_assert_still_running(task)

    child.release()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert_child_has_exited(child.lock)
    assert not child.finished.exists()


@pytest.mark.asyncio
async def test_cancelling_during_verifier_timeout_cleanup(tmp_path, monkeypatch) -> None:
    # The timeout branch used to kill and then `communicate()` unguarded, so a
    # cancellation landing there escaped without confirming the child was gone.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    child = GatedChild(tmp_path)
    deaf_to_terminate(monkeypatch)
    converging = observe_converging(monkeypatch)
    task = asyncio.create_task(
        CommandVerifier(child.command, timeout_seconds=0.3).verify(workspace)
    )

    await asyncio.wait_for(converging.wait(), timeout=30)
    await cancel_again_and_assert_still_running(task)

    child.release()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert_child_has_exited(child.lock)
    assert not child.finished.exists()


@pytest.mark.asyncio
async def test_cancelling_during_shell_timeout_cleanup(tmp_path, monkeypatch) -> None:
    from traceh.api.tools import ToolExecutionContext
    from traceh.tools.builtins.shell import ShellTool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    child = GatedChild(tmp_path)
    deaf_to_terminate(monkeypatch)
    converging = observe_converging(monkeypatch)
    context = ToolExecutionContext(
        session_id="s",
        turn_id="t",
        step_id="p",
        tool_call_id="c",
        workspace=workspace,
        data_dir=tmp_path / "data",
    )
    task = asyncio.create_task(
        ShellTool().execute({"command": child.command, "timeout": 0.3}, context)
    )

    await asyncio.wait_for(converging.wait(), timeout=30)
    await cancel_again_and_assert_still_running(task)

    child.release()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert_child_has_exited(child.lock)
    assert not child.finished.exists()


# Writes to both streams, flushes them, and only then drops the marker: if the
# marker exists, the output really was produced before the process was stopped.
FLUSHING_CHILD = """
import sys
import time

marker = sys.argv[1]
CHILD_DEADLINE_SECONDS = 60
sys.stdout.write("EARLY-STDOUT")
sys.stdout.flush()
sys.stderr.write("EARLY-STDERR")
sys.stderr.flush()
with open(marker, "w", encoding="utf-8") as handle:
    handle.write("flushed")
time.sleep(CHILD_DEADLINE_SECONDS)
"""


def flushing_child_command(tmp_path: Path) -> tuple[str, Path]:
    script = tmp_path / "flushing_child.py"
    script.write_text(FLUSHING_CHILD, encoding="utf-8")
    marker = tmp_path / "flushed"
    return shlex.join([sys.executable, str(script), str(marker)]), marker


@pytest.mark.asyncio
async def test_verifier_timeout_keeps_output_produced_before_the_timeout(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    command, marker = flushing_child_command(tmp_path)

    result = await CommandVerifier(command, timeout_seconds=2.0).verify(workspace)

    # The marker proves the premise: the child did flush before it was stopped.
    assert marker.exists(), "the child never got far enough to flush its output"
    assert result.passed is False
    assert "timed out" in result.summary
    assert result.stdout == "EARLY-STDOUT"
    assert result.stderr == "EARLY-STDERR"


@pytest.mark.asyncio
async def test_verifier_timeout_output_reaches_the_next_step(tmp_path) -> None:
    # Keeping the bytes is only half the job: the continuation policy feeds the
    # *summary* back to the model, so the summary has to carry them too.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    command, marker = flushing_child_command(tmp_path)

    result = await CommandVerifier(command, timeout_seconds=2.0).verify(workspace)
    assert marker.exists(), "the child never got far enough to flush its output"

    # The full fields keep their existing meaning.
    assert result.stdout == "EARLY-STDOUT"
    assert result.stderr == "EARLY-STDERR"

    directive = await DefaultContinuationRuntime().decide(
        response=ModelResponse(content="looks finished to me"),
        step_number=1,
        max_steps=20,
        verification=VerificationFeedback(result.passed, result.summary),
        verification_failures=1,
        max_verification_retries=1,
    )

    assert isinstance(directive, Continue)
    injected = "\n".join(directive.messages)
    assert "timed out" in injected
    assert "EARLY-STDOUT" in injected
    assert "EARLY-STDERR" in injected


@pytest.mark.asyncio
async def test_timeout_summary_is_bounded_like_a_normal_result(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = tmp_path / "loud_child.py"
    script.write_text(
        "import sys, time\n"
        f"sys.stdout.write('A' * {SUMMARY_TAIL_CHARS * 2} + 'STDOUT-TAIL')\n"
        "sys.stdout.flush()\n"
        "with open(sys.argv[1], 'w', encoding='utf-8') as handle:\n"
        "    handle.write('flushed')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    marker = tmp_path / "flushed"
    command = shlex.join([sys.executable, str(script), str(marker)])

    result = await CommandVerifier(command, timeout_seconds=2.0).verify(workspace)

    assert marker.exists()
    # The field keeps everything; the summary keeps a bounded tail of it.
    assert len(result.stdout) == SUMMARY_TAIL_CHARS * 2 + len("STDOUT-TAIL")
    tail = result.summary.split("stdout:\n", 1)[1].split("\nstderr:", 1)[0]
    assert len(tail) == SUMMARY_TAIL_CHARS
    assert tail == result.stdout[-SUMMARY_TAIL_CHARS:]
    assert tail.endswith("STDOUT-TAIL")


@pytest.mark.asyncio
async def test_shell_timeout_keeps_output_produced_before_the_timeout(tmp_path) -> None:
    from traceh.api.tools import ToolExecutionContext
    from traceh.tools.builtins.shell import ShellTool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    command, marker = flushing_child_command(tmp_path)
    context = ToolExecutionContext(
        session_id="s",
        turn_id="t",
        step_id="p",
        tool_call_id="c",
        workspace=workspace,
        data_dir=tmp_path / "data",
    )

    with pytest.raises(TimeoutError) as error:
        await ShellTool().execute({"command": command, "timeout": 2.0}, context)

    assert marker.exists(), "the child never got far enough to flush its output"
    assert "EARLY-STDOUT" in str(error.value)
    assert "EARLY-STDERR" in str(error.value)


# Runs one verifier timeout in its own interpreter so the event loop really
# closes afterwards. That is when a leaked transport turns into
# "Event loop is closed" / "unclosed transport" on stderr.
LOOP_SHUTDOWN_PROBE = """
import asyncio
import shlex
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from traceh.runtime.verification import CommandVerifier

script, workspace = sys.argv[2], sys.argv[3]
release, lock, pids = sys.argv[4], sys.argv[5], sys.argv[6]


async def main() -> None:
    command = shlex.join([sys.executable, script, release, lock, pids])
    result = await CommandVerifier(command, timeout_seconds=1.0).verify(Path(workspace))
    print("stdout:", result.stdout)
    print("child-stderr:", result.stderr)
    print("tasks_after:", len(asyncio.all_tasks()))


asyncio.run(main())
print("clean-exit")
"""

# The child holds a lock (so the test can prove it exited), spawns a grandchild
# that inherits the captured output handles and outlives the whole shutdown, and
# writes output before hanging.
LOOP_SHUTDOWN_CHILD = """
import os
import subprocess
import sys
import time

release, lock, pids = sys.argv[1], sys.argv[2], sys.argv[3]
CHILD_DEADLINE_SECONDS = 60

try:
    import fcntl
except ImportError:
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None

descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
if fcntl is not None:
    fcntl.flock(descriptor, fcntl.LOCK_EX)
else:
    os.lseek(descriptor, 0, os.SEEK_SET)
    msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)

holder = (
    "import sys, time\\n"
    "from pathlib import Path\\n"
    "release = Path(sys.argv[1])\\n"
    "deadline = time.monotonic() + 60\\n"
    "while not release.exists() and time.monotonic() < deadline:\\n"
    "    time.sleep(0.01)\\n"
)
grandchild = subprocess.Popen([sys.executable, "-c", holder, release])
# Record both pids so the test can clean up deterministically on any path.
with open(pids, "w", encoding="utf-8") as handle:
    handle.write("{}\\n{}\\n".format(os.getpid(), grandchild.pid))

sys.stdout.write("BEFORE-TIMEOUT")
sys.stdout.flush()
time.sleep(CHILD_DEADLINE_SECONDS)
"""


def test_event_loop_shutdown_leaves_no_transport_noise(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    probe = tmp_path / "loop_shutdown_probe.py"
    probe.write_text(LOOP_SHUTDOWN_PROBE, encoding="utf-8")
    child = tmp_path / "loop_shutdown_child.py"
    child.write_text(LOOP_SHUTDOWN_CHILD, encoding="utf-8")
    release = tmp_path / "release"
    lock = tmp_path / "child.lock"
    pids = tmp_path / "pids"

    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-W",
                "error::ResourceWarning",
                str(probe),
                str(SRC_ROOT),
                str(child),
                str(workspace),
                str(release),
                str(lock),
                str(pids),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            # Comfortably above every child's own deadline, so a hang here is a
            # real hang rather than a child still counting down.
            timeout=CHILD_DEADLINE_SECONDS * 3,
        )
    finally:
        # Whatever happened above - assertion, TimeoutExpired, anything - the
        # processes this test created are shut down and waited for. Only the
        # pids this test recorded are touched; nothing is swept by name.
        release.write_text("go", encoding="utf-8")
        terminate_recorded_processes(pids)
        wait_until_lock_is_free(lock)

    assert completed.returncode == 0, completed.stderr
    assert "clean-exit" in completed.stdout
    assert "BEFORE-TIMEOUT" in completed.stdout
    # No collector task of ours survives the call.
    assert "tasks_after: 1" in completed.stdout
    for noise in ("Event loop is closed", "unclosed transport", "Exception ignored"):
        assert noise not in completed.stderr, completed.stderr
    # The direct child is gone even though the grandchild is still holding on.
    assert_child_has_exited(lock)


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


@pytest.mark.asyncio
async def test_shell_tool_reports_chinese_output_unchanged(tmp_path) -> None:
    from traceh.api.tools import ToolExecutionContext
    from traceh.tools.builtins.shell import ShellTool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    text = "工具输出的中文不应变成问号"
    command = shlex.join([sys.executable, "-c", "import sys; sys.stdout.write(sys.argv[1])", text])
    context = ToolExecutionContext(
        session_id="s",
        turn_id="t",
        step_id="p",
        tool_call_id="c",
        workspace=workspace,
        data_dir=tmp_path / "data",
    )

    output = await ShellTool().execute({"command": command, "timeout": 30}, context)

    assert output.data["stdout"] == text


@pytest.mark.asyncio
async def test_cancelled_shell_tool_leaves_no_running_child(tmp_path) -> None:
    from traceh.api.tools import ToolExecutionContext
    from traceh.tools.builtins.shell import ShellTool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    command, started, finished, lock = slow_child_command(tmp_path, seconds=30)
    context = ToolExecutionContext(
        session_id="s",
        turn_id="t",
        step_id="p",
        tool_call_id="c",
        workspace=workspace,
        data_dir=tmp_path / "data",
    )
    task = asyncio.create_task(
        ShellTool().execute({"command": command, "timeout": 60}, context)
    )
    await wait_for_file(started)

    task.cancel()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert_child_has_exited(lock)
    assert not finished.exists()
