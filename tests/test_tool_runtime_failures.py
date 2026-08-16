from __future__ import annotations

import asyncio
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from traceh.api.llm import ToolCall
from traceh.api.tools import EffectKind, ToolExecutionContext, ToolOutput
from traceh.session.event_store import InMemoryEventStore
from traceh.session.file_lock import FileLockTimeout, exclusive_file_lock
from traceh.session.service import SessionService
from traceh.tools.builtins.shell import ShellTool
from traceh.tools.policy import AllowByDefaultPolicy, DangerousShellPolicy
from traceh.tools.registry import ToolRegistry
from traceh.tools.runtime import ToolRuntime

#: Hard self-deadline for helper processes, so no failure path leaves one behind.
CHILD_DEADLINE_SECONDS = 60

# Prints on both streams, flushes them, drops a marker to prove the output really
# happened, and only then hangs. It also holds an OS lock, which makes "did it
# actually exit?" answerable without guessing: the lock is released by the
# operating system when the process dies.
FLUSHING_CHILD = """
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

marker, lock = sys.argv[1], sys.argv[2]
descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
if fcntl is not None:
    fcntl.flock(descriptor, fcntl.LOCK_EX)
else:
    os.lseek(descriptor, 0, os.SEEK_SET)
    msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)

sys.stdout.write("STDOUT-MARKER")
sys.stdout.flush()
sys.stderr.write("STDERR-MARKER")
sys.stderr.flush()
with open(marker, "w", encoding="utf-8") as handle:
    handle.write("flushed")
time.sleep(SLEEP_SECONDS)
""".replace("SLEEP_SECONDS", str(CHILD_DEADLINE_SECONDS))


def flushing_child_command(tmp_path: Path) -> tuple[str, Path, Path]:
    script = tmp_path / "flushing_child.py"
    script.write_text(FLUSHING_CHILD, encoding="utf-8")
    marker = tmp_path / "flushed"
    lock = tmp_path / "child.lock"
    return shlex.join([sys.executable, str(script), str(marker), str(lock)]), marker, lock


def assert_child_has_exited(lock: Path) -> None:
    """The lock is only free once the operating system reaped the process."""

    try:
        with exclusive_file_lock(lock, timeout=10.0):
            pass
    except FileLockTimeout as error:  # pragma: no cover - only on a real leak
        raise AssertionError("the shell child is still running") from error


async def shell_batch(
    tmp_path: Path,
    *,
    command: str,
    tool_timeout: float,
    runtime_timeout: float,
):
    """Run one shell call through the real ToolRuntime, not the tool directly."""

    sessions = SessionService(InMemoryEventStore())
    session_id = await sessions.create_session(tmp_path)
    await sessions.append_session(session_id, "turn/start", {"turn_id": "t"})
    await sessions.append_session(session_id, "step/start", {"turn_id": "t", "step_id": "s"})
    registry = ToolRegistry()
    registry.register(ShellTool())
    runtime = ToolRuntime(
        registry,
        sessions,
        policies=(AllowByDefaultPolicy(),),
        timeout_seconds=runtime_timeout,
    )
    context = ToolExecutionContext(session_id, "t", "s", "batch", tmp_path, tmp_path)
    results = await runtime.execute_batch(
        (ToolCall("c1", "shell", {"command": command, "timeout": tool_timeout}),),
        context=context,
        composition_revision="r",
    )
    return results[0], await sessions.read_effects(session_id), await sessions.read_session(
        session_id
    )


@dataclass(slots=True)
class SlowReadTool:
    name: str = "slow_read"
    description: str = "slow"
    input_schema: dict = field(init=False, repr=False)
    effect_kind: EffectKind = EffectKind.PURE_READ

    def __post_init__(self) -> None:
        self.input_schema = {"type": "object", "properties": {}, "additionalProperties": False}

    async def execute(self, arguments, context) -> ToolOutput:
        del arguments, context
        await asyncio.sleep(1)
        return ToolOutput("late")


@pytest.mark.asyncio
async def test_unknown_and_denied_tools_receive_results_without_effects(tmp_path) -> None:
    sessions = SessionService(InMemoryEventStore())
    session_id = await sessions.create_session(tmp_path)
    await sessions.append_session(session_id, "turn/start", {"turn_id": "t"})
    await sessions.append_session(
        session_id,
        "step/start",
        {"turn_id": "t", "step_id": "s"},
    )
    registry = ToolRegistry()

    from traceh.tools.builtins.shell import ShellTool

    registry.register(ShellTool())
    runtime = ToolRuntime(
        registry,
        sessions,
        policies=(DangerousShellPolicy(), AllowByDefaultPolicy()),
    )
    context = ToolExecutionContext(session_id, "t", "s", "batch", tmp_path, tmp_path)
    results = await runtime.execute_batch(
        (
            ToolCall("unknown", "missing", {}),
            ToolCall("denied", "shell", {"command": "rm -rf ."}),
        ),
        context=context,
        composition_revision="r",
    )
    assert [result.status for result in results] == ["invalid", "denied"]
    assert await sessions.read_effects(session_id) == ()


@pytest.mark.asyncio
async def test_tool_timeout_is_durable(tmp_path) -> None:
    sessions = SessionService(InMemoryEventStore())
    session_id = await sessions.create_session(tmp_path)
    await sessions.append_session(session_id, "turn/start", {"turn_id": "t"})
    await sessions.append_session(
        session_id,
        "step/start",
        {"turn_id": "t", "step_id": "s"},
    )
    registry = ToolRegistry()
    registry.register(SlowReadTool())
    runtime = ToolRuntime(
        registry,
        sessions,
        policies=(AllowByDefaultPolicy(),),
        timeout_seconds=0.01,
    )
    context = ToolExecutionContext(session_id, "t", "s", "batch", tmp_path, tmp_path)
    results = await runtime.execute_batch(
        (ToolCall("slow", "slow_read", {}),),
        context=context,
        composition_revision="r",
    )
    assert results[0].status == "failed"
    assert results[0].error_type == "TimeoutError"
    effects = await sessions.read_effects(session_id)
    assert [event.type for event in effects] == [
        "effect/intent",
        "effect/dispatched",
        "effect/outcome",
    ]
    assert effects[-1].data["status"] == "failed"
    assert effects[-1].data["reported_by"] == "runtime"


@pytest.mark.asyncio
async def test_shell_timeout_keeps_its_output_through_the_tool_runtime(tmp_path) -> None:
    # The real agent path: the tool times itself out well inside the runtime's
    # budget, so the runtime must report the tool's account, not its own.
    command, marker, lock = flushing_child_command(tmp_path)

    result, effects, session_events = await shell_batch(
        tmp_path, command=command, tool_timeout=1.0, runtime_timeout=30.0
    )

    assert marker.exists(), "the child never got far enough to flush its output"
    assert result.status == "failed"
    assert result.error_type == "TimeoutError"

    outcome = effects[-1]
    assert outcome.type == "effect/outcome"
    assert outcome.data["reported_by"] == "tool"
    tool_result = next(
        event for event in session_events if event.type == "tool/result"
    )

    for marker_text in ("STDOUT-MARKER", "STDERR-MARKER"):
        assert marker_text in result.content, result.content
        assert marker_text in str(outcome.data["message"])
        assert marker_text in str(tool_result.data["content"])

    # The runtime's own budget was never reached and must not be reported.
    for carrier in (result.content, str(outcome.data["message"])):
        assert "timed out after 30.0s" not in carrier
        assert "Tool timed out after" not in carrier
    assert "timed_out=true" in result.content
    assert_child_has_exited(lock)


@pytest.mark.asyncio
async def test_runtime_budget_still_wins_when_it_expires_first(tmp_path) -> None:
    # The reverse direction: the runtime's budget expires long before the tool's
    # own timeout, so the generic runtime timeout semantics stay in place.
    command, marker, lock = flushing_child_command(tmp_path)

    result, effects, _ = await shell_batch(
        tmp_path, command=command, tool_timeout=30.0, runtime_timeout=1.0
    )

    # Without this the test could pass on a child that never ran at all: the
    # marker proves it took the lock, flushed both streams and reached its hang,
    # which is what makes the convergence assertion below meaningful.
    assert marker.exists(), "the child never got far enough to flush its output"

    assert result.status == "failed"
    assert result.error_type == "TimeoutError"
    assert result.content == "Tool timed out after 1.0s"
    outcome = effects[-1]
    assert outcome.data["reported_by"] == "runtime"
    assert outcome.data["status"] == "failed"

    # The child was converged on the way out rather than left running.
    assert_child_has_exited(lock)
