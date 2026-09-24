"""Completion verification on its real scoped command capability."""

import asyncio
import shlex
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from sandbox_fixtures import real_sandbox_service, wait_for_guest

from traceh.api.llm import ModelResponse
from traceh.artifacts.cas import LocalArtifactCas
from traceh.runtime.continuation import Continue, DefaultContinuationRuntime, VerificationFeedback
from traceh.runtime.response_completeness import judge_response
from traceh.runtime.verification import SUMMARY_TAIL_CHARS, CommandVerifier, invoke_verifier
from traceh.session.event_store import InMemoryEventStore
from traceh.session.service import SessionService


async def execution(tmp_path, code, command_seconds=1):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions = SessionService(InMemoryEventStore())
    session_id = await sessions.create_session(workspace)
    cas = LocalArtifactCas(tmp_path / "cas")
    sandbox = real_sandbox_service(sessions.store, cas)
    sandbox.policy = replace(sandbox.policy, limits=replace(sandbox.policy.limits, wall_seconds=30))
    task = asyncio.create_task(
        invoke_verifier(
            CommandVerifier(shlex.join(("python", "-c", code)), command_seconds),
            workspace,
            sandbox_service=sandbox,
            sessions=sessions,
            session_id=session_id,
            turn_id="verification-turn",
            step_id="verification-step",
            data_dir=tmp_path / "data",
        )
    )
    return task, sessions, session_id, sandbox


async def test_timeout_keeps_flushed_output_and_reaches_continuation(tmp_path):
    task, _, _, _ = await execution(
        tmp_path,
        "import sys,time; print('EARLY-STDOUT',end='',flush=True); "
        "print('EARLY-STDERR',file=sys.stderr,end='',flush=True); time.sleep(60)",
    )
    result = await task
    assert not result.passed and result.sandbox_receipt["converged"]
    assert result.stdout == "EARLY-STDOUT"
    assert result.stderr == "EARLY-STDERR"
    response = ModelResponse(content="looks done")
    directive = await DefaultContinuationRuntime().decide(
        response=response,
        completeness=judge_response(response),
        step_number=1,
        max_steps=20,
        verification=VerificationFeedback(result.passed, result.summary),
        verification_failures=1,
        max_verification_retries=1,
    )
    assert isinstance(directive, Continue)
    text = "\n".join(directive.messages)
    assert all(value in text for value in ("timed out", "EARLY-STDOUT", "EARLY-STDERR"))


async def test_timeout_summary_keeps_only_tail_of_bounded_capture(tmp_path):
    expected = "A" * (SUMMARY_TAIL_CHARS * 2) + "STDOUT-TAIL"
    task, _, _, _ = await execution(
        tmp_path, f"import time; print({expected!r},end='',flush=True); time.sleep(60)"
    )
    result = await task
    assert result.stdout == expected
    tail = result.summary.split("stdout:\n", 1)[1].split("\nstderr:", 1)[0]
    assert tail == expected[-SUMMARY_TAIL_CHARS:]


async def test_repeated_verifier_cancel_converges_an_observed_guest(tmp_path):
    task, sessions, session_id, sandbox = await execution(
        tmp_path,
        "import time; time.sleep(60)",
        command_seconds=30,
    )
    try:
        await wait_for_guest(
            sessions.store, sessions.session_stream(session_id), sandbox.policy, task
        )
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        events = await sessions.read_session(session_id)
        outcome = next(e.data for e in events if e.type == "sandbox/outcome")
        assert outcome["status"] == "cancelled" and outcome["converged"] is True
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_verifier_returns_chinese_from_real_guest(tmp_path):
    task, _, _, _ = await execution(tmp_path, "print('隔离执行完成')")
    result = await task
    assert result.passed and result.stdout == "隔离执行完成\n"


def test_owned_verifier_closes_event_loop_without_transport_noise(tmp_path):
    # Explicit opt-in before starting the independent interpreter.
    from sandbox_fixtures import real_sandbox_policy

    real_sandbox_policy()
    root = Path(__file__).resolve().parent
    script = tmp_path / "probe.py"
    script.write_text(
        """import asyncio,sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from test_sandbox_verification import execution
async def main():
    task, _, _, _ = await execution(Path(sys.argv[2]),
        "import subprocess,time; subprocess.Popen(['python','-c','import time; time.sleep(60)']); "
        "print('BEFORE-TIMEOUT',flush=True); time.sleep(60)")
    result = await task
    assert result.sandbox_receipt['converged']
    print(result.stdout)
    print('tasks_after:', len(asyncio.all_tasks()))
asyncio.run(main())
print('clean-exit')
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-W", "error::ResourceWarning", str(script), str(root), str(tmp_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "BEFORE-TIMEOUT" in result.stdout and "tasks_after: 1" in result.stdout
    assert "clean-exit" in result.stdout
    assert not any(
        noise in result.stderr
        for noise in ("Event loop is closed", "unclosed transport", "Exception ignored")
    )
