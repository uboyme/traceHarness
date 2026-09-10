"""Real bounded stdio connections through the shared execution scope."""

import asyncio
import subprocess
import tempfile
from dataclasses import replace

import pytest

from tests.sandbox_fixtures import real_sandbox_service, wait_for_guest
from traceh.api.sandbox import SandboxOwner, SandboxStdioLimits
from traceh.artifacts.cas import LocalArtifactCas
from traceh.sandbox.reader import read_execution
from traceh.sandbox.stdio import SandboxStdioError
from traceh.session.event_store import InMemoryEventStore


def setup_scope(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = InMemoryEventStore()
    cas = LocalArtifactCas(tmp_path / "cas")
    service = real_sandbox_service(store, cas)
    service.policy = replace(
        service.policy, limits=replace(service.policy.limits, wall_seconds=30)
    )
    scope = service.scope(
        SandboxOwner("plugin", "test-activation", plugin_id="stdio.fixture", plugin_version="1",
                     activation_id="test-activation"), stream_id="test-activation",
        workspace=workspace, data_dir=None, publish_changes=False,
    )
    return scope, store, cas, service.policy


async def assert_converged(store, cas, policy):
    events = await store.read("test-activation")
    assert [e.type for e in events] == ["sandbox/request", "sandbox/outcome"]
    view = await read_execution(
        store, cas, stream_id="test-activation", execution_id=events[0].data["execution_id"]
    )
    assert view.outcome["converged"] is True
    name = "traceh-exec-" + events[0].data["execution_id"]

    def inspect():
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(
                ["docker", "--context", policy.docker_context, "ps", "-a", "--filter",
                 f"name=^/{name}$", "--format", "{{.ID}}"],
                stdout=output, stderr=subprocess.DEVNULL, timeout=10,
            )
            assert result.returncode == 0
            output.seek(0)
            assert output.read() == b""
    await asyncio.to_thread(inspect)
    return view


async def test_real_bidirectional_stdio_and_final_evidence(tmp_path):
    scope, store, cas, policy = setup_scope(tmp_path)
    script = (
        "import os,socket,sys\n"
        "assert os.getuid()==65532\n"
        "try:\n"
        " s=socket.socket(socket.AF_UNIX);s.connect('/tmp/.traceh-control/stdio')\n"
        "except PermissionError:\n"
        " print('control-denied',flush=True)\n"
        "else: raise AssertionError('workload accessed controller')\n"
        "for line in sys.stdin.buffer:\n"
        " sys.stdout.buffer.write(line.upper());sys.stdout.buffer.flush()\n"
        "print('finished',file=sys.stderr,flush=True)\n"
    )
    async with scope:
        process = await scope.open_process(
            ("python", "-u", "-c", script), timeout_seconds=30,
            stdio=SandboxStdioLimits(4096, 1024),
        )
        assert await process.read(1024) == b"control-denied\n"
        await process.write("另一组交互输入\n".encode())
        assert await process.read(1024) == "另一组交互输入\n".encode()
        await process.write(b"second request\n")
        assert await process.read(1024) == b"SECOND REQUEST\n"
        await process.close_input()
        result = await process.wait()
        assert result.status == "finished" and result.exit_code == 0
        assert await process.read(1024) == b""
        assert await process.read(1024, stream="stderr") == b"finished\n"
    view = await assert_converged(store, cas, policy)
    assert view.request["stdio"] == {"input_bytes": 4096, "frame_bytes": 1024}
    assert view.result["payload"]["stdout"]
    with pytest.raises(SandboxStdioError, match="closed"):
        await process.write(b"late")


async def test_real_frame_and_input_limits_do_not_dispatch_rejected_bytes(tmp_path):
    scope, store, cas, policy = setup_scope(tmp_path)
    async with scope:
        process = await scope.open_process(
            ("python", "-u", "-c", "import sys;print(sys.stdin.buffer.read().hex())"),
            timeout_seconds=30, stdio=SandboxStdioLimits(6, 4),
        )
        with pytest.raises(ValueError, match="frame-limit"):
            await process.write(b"12345")
        await process.write(b"1234")
        with pytest.raises(ValueError, match="input-limit"):
            await process.write(b"789")
        await process.write(b"56")
        await process.close_input()
        result = await process.wait()
        assert result.stdout == b"313233343536\n"
    await assert_converged(store, cas, policy)


async def test_real_cancel_read_converges_started_server_and_descendant(tmp_path):
    scope, store, cas, policy = setup_scope(tmp_path)
    async with scope:
        process = await scope.open_process(
            ("python", "-u", "-c",
             "import subprocess,sys;subprocess.Popen(['python','-c','while True: pass'],"
             "start_new_session=True);print('ready',flush=True);sys.stdin.read()"),
            timeout_seconds=30, stdio=SandboxStdioLimits(4096, 1024),
        )
        assert await process.read(1024) == b"ready\n"
        reading = asyncio.create_task(process.read(1024))
        # The process has acknowledged startup and its detached descendant is
        # already running. Cancellation must end that real execution as well.
        await asyncio.sleep(0)
        reading.cancel()
        reading.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reading
    view = await assert_converged(store, cas, policy)
    assert view.outcome["status"] == "cancelled"


async def test_real_start_failure_has_terminal_receipt_and_no_connection(tmp_path):
    scope, store, cas, policy = setup_scope(tmp_path)
    async with scope:
        with pytest.raises(SandboxStdioError, match="ended-before-connection"):
            await scope.open_process(
                ("/missing-explicit-test-server",), timeout_seconds=30,
                stdio=SandboxStdioLimits(4096, 1024),
            )
    view = await assert_converged(store, cas, policy)
    assert view.outcome["status"] == "start-failed"


async def test_real_interactive_guest_deadline_stops_server_and_descendant(tmp_path):
    scope, store, cas, policy = setup_scope(tmp_path)
    async with scope:
        process = await scope.open_process(
            ("python", "-u", "-c", "import subprocess,sys;"
             "subprocess.Popen(['python','-c','while True: pass'],start_new_session=True);"
             "print('deadline-ready',flush=True);sys.stdin.read()"),
            timeout_seconds=3, stdio=SandboxStdioLimits(4096, 1024),
        )
        assert await process.read(1024) == b"deadline-ready\n"
        result = await process.wait()
        assert result.status == "timed-out"
    await assert_converged(store, cas, policy)


async def test_real_cancel_partial_input_write_closes_without_retry(tmp_path):
    scope, store, cas, policy = setup_scope(tmp_path)
    async with scope:
        process = await scope.open_process(
            ("python", "-u", "-c", "import sys;from pathlib import Path;"
             "print('ready',flush=True);sys.stdin.buffer.read(1);"
             "Path('consumed').write_text('flushed');exec('while True: pass')"),
            timeout_seconds=30, stdio=SandboxStdioLimits(262144, 262144),
        )
        assert await process.read(1024) == b"ready\n"
        writing = asyncio.create_task(process.write(b"x" * 262144))
        await wait_for_guest(store, "test-activation", policy, writing, relative="consumed")
        # The guest has consumed part of this actual write but cannot drain the
        # full frame. Cancellation cannot safely retry the uncertain remainder.
        writing.cancel()
        writing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await writing
        with pytest.raises(SandboxStdioError, match="closed"):
            await process.write(b"retry")
    view = await assert_converged(store, cas, policy)
    assert view.outcome["status"] == "cancelled"


@pytest.mark.parametrize("values", [(0, 1), (1, 0), (1, 2), (True, 1), (1, 1.0)])
def test_stdio_limits_require_bounded_explicit_integers(values):
    with pytest.raises(ValueError):
        SandboxStdioLimits(*values)
