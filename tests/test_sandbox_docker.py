"""Opt-in real backend checks; never pull images or start Docker implicitly."""

import asyncio
import os
import subprocess
import sys
import tempfile
from dataclasses import replace
from uuid import uuid4

import pytest

from traceh.api.sandbox import SandboxLimits, SandboxOwner, SandboxPolicy, SandboxRequest
from traceh.artifacts.cas import LocalArtifactCas
from traceh.concurrency import await_worker_convergence
from traceh.sandbox.docker import DockerSandboxExecutor, SandboxBackendError
from traceh.sandbox.ledger import SandboxEventRecorder
from traceh.sandbox.reader import read_execution
from traceh.session.event_store import InMemoryEventStore


@pytest.fixture
def settings():
    image = os.environ.get("TRACEH_SANDBOX_TEST_IMAGE")
    context = os.environ.get("TRACEH_SANDBOX_TEST_CONTEXT")
    if not image or not context:
        pytest.skip("explicit real Docker test image and context required")
    return SandboxPolicy(
        context,
        image,
        SandboxLimits(128 * 1024 * 1024, 1024 * 1024, 64, 4096, 16, 0.5, 2),
        (".",),
        (".",),
    )


async def test_real_executor_preserves_evidence_and_never_mounts_workspace(tmp_path, settings):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "input.txt").write_text("a different fixture")
    (workspace / ".env").write_text("SYNTHETIC_SECRET=must-not-read")
    cas = LocalArtifactCas(tmp_path / "cas")
    executor = DockerSandboxExecutor(cas)
    request = SandboxRequest(
        uuid4().hex,
        SandboxOwner("verification", "real-test"),
        workspace,
        (
            "python",
            "-c",
            "from pathlib import Path; import os; "
            "assert os.getuid()==65532; assert not Path('.env').exists(); "
            "assert not Path('/var/run/docker.sock').exists(); "
            "assert Path('input.txt').read_text()=='a different fixture'; "
            "Path('new.txt').write_text('guest only'); print('verified')",
        ),
        (("PATH", "/usr/local/bin:/usr/bin:/bin"),),
        settings,
    )
    store = InMemoryEventStore()
    record = SandboxEventRecorder(store, "session:real-test", request.owner)
    result = await executor.execute(request, record)
    events = [(e.type, e.data) for e in await store.read("session:real-test")]
    assert result.status == "finished" and result.exit_code == 0
    assert result.stdout == b"verified\n"
    assert not (workspace / "new.txt").exists()
    assert next(f.content for f in result.files if f.path == "new.txt") == b"guest only"
    assert [kind for kind, _ in events] == ["sandbox/request", "sandbox/outcome"]
    assert result.receipt["converged"] is True
    assert result.receipt["started_at"] and result.receipt["finished_at"]
    assert result.receipt["request_digest"] == events[0][1]["digest"]
    view = await read_execution(
        store, cas, stream_id="session:real-test", execution_id=request.execution_id
    )
    assert view.outcome == result.receipt
    assert view.result["payload"]["status"] == "finished"
    with pytest.raises(ValueError, match="already-admitted"):
        await executor.execute(request, record)


async def test_real_guest_deadline_kills_detached_descendants(tmp_path, settings):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    events = []

    async def record(kind, data):
        events.append((kind, data))

    request = SandboxRequest(
        uuid4().hex,
        SandboxOwner("verification", "deadline-test"),
        workspace,
        (
            "python",
            "-c",
            "import subprocess; "
            "subprocess.Popen(['python','-c','while True: pass'],start_new_session=True); "
            "exec('while True: pass')",
        ),
        (("PATH", "/usr/local/bin:/usr/bin:/bin"),),
        settings,
    )
    result = await DockerSandboxExecutor(LocalArtifactCas(tmp_path / "cas")).execute(
        request, record
    )
    assert result.status == "timed-out" and result.receipt["converged"]
    assert events[-1][1]["status"] == "timed-out"


async def test_cancel_during_ledger_write_never_starts_execution(tmp_path, settings):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    entered = asyncio.Event()
    hold = asyncio.Event()
    recorded = []

    async def record(kind, data):
        entered.set()
        await hold.wait()
        recorded.append(kind)

    request = SandboxRequest(
        uuid4().hex,
        SandboxOwner("verification", "cancel-test"),
        workspace,
        ("python", "-c", "raise AssertionError('must not run')"),
        (),
        settings,
    )
    task = asyncio.create_task(
        DockerSandboxExecutor(LocalArtifactCas(tmp_path / "cas")).execute(request, record)
    )
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    hold.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert recorded == ["sandbox/request", "sandbox/outcome"]


async def test_real_missing_backend_fails_closed(tmp_path, settings):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = SandboxRequest(
        uuid4().hex,
        SandboxOwner("verification", "missing-backend"),
        workspace,
        ("python", "-c", "from pathlib import Path; Path('escaped').touch()"),
        (),
        replace(settings, docker_context="traceh-intentionally-absent-" + uuid4().hex),
    )
    store = InMemoryEventStore()
    recorder = SandboxEventRecorder(store, "session:missing-backend", request.owner)
    result = await DockerSandboxExecutor(LocalArtifactCas(tmp_path / "cas")).execute(
        request, recorder
    )
    assert result.status == "backend-unavailable" and result.receipt["converged"]
    assert not (workspace / "escaped").exists()
    assert len(await store.read("session:missing-backend")) == 2


async def test_real_repeated_cancel_after_child_started_converges(tmp_path, settings):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = SandboxRequest(
        uuid4().hex,
        SandboxOwner("verification", "running-cancel"),
        workspace,
        ("python", "-c", "print('started', flush=True); exec('while True: pass')"),
        (("PATH", "/usr/local/bin:/usr/bin:/bin"),),
        replace(settings, limits=replace(settings.limits, wall_seconds=20)),
    )
    store = InMemoryEventStore()
    recorder = SandboxEventRecorder(store, "session:running-cancel", request.owner)
    task = asyncio.create_task(
        DockerSandboxExecutor(LocalArtifactCas(tmp_path / "cas")).execute(request, recorder)
    )

    async def cli(*argv):
        def invoke():
            # Docker Desktop helpers may inherit a pipe after the CLI exits.
            # A file capture observes direct completion without waiting for EOF.
            with tempfile.TemporaryFile() as output:
                subprocess.run(
                    ["docker", "--context", settings.docker_context, *argv],
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
                output.seek(0)
                return output.read(65536).decode()

        probe = asyncio.create_task(asyncio.to_thread(invoke))
        try:
            return await asyncio.shield(probe)
        except BaseException:
            await await_worker_convergence(probe)
            raise

    try:
        async with asyncio.timeout(12):
            while True:
                if task.done():
                    pytest.fail(f"workload did not remain running: {(await task).status}")
                running = await cli(
                    "ps",
                    "--filter",
                    f"label=traceh.execution={request.execution_id}",
                    "--format",
                    "{{.ID}}",
                )
                if running.strip():
                    top = await cli("top", running.strip(), "-eo", "pid,uid")
                    if "65532" in top:
                        break
        task.cancel()
        # A second cancellation lands during the worker's actual cleanup.
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not (
            await cli(
                "ps",
                "-a",
                "--filter",
                f"label=traceh.execution={request.execution_id}",
                "--format",
                "{{.ID}}",
            )
        ).strip()
        events = await store.read("session:running-cancel")
        assert events[-1].type == "sandbox/outcome"
        assert events[-1].data["status"] == "cancelled"
        assert events[-1].data["converged"] is True
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_real_cleanup_failure_is_durable_unknown_convergence(tmp_path, settings, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = SandboxRequest(
        uuid4().hex,
        SandboxOwner("verification", "cleanup-failure"),
        workspace,
        ("python", "-c", "print('work-really-ran')"),
        (("PATH", "/usr/local/bin:/usr/bin:/bin"),),
        settings,
    )
    store = InMemoryEventStore()
    cas = LocalArtifactCas(tmp_path / "cas")
    recorder = SandboxEventRecorder(store, "session:cleanup-failure", request.owner)
    original = subprocess.Popen

    def refuse_remove(argv, **kwargs):
        if argv[:3] == ["docker", "--context", settings.docker_context] and argv[3] == "rm":
            return original([sys.executable, "-c", "raise SystemExit(1)"], **kwargs)
        return original(argv, **kwargs)

    container_id = None
    try:
        with monkeypatch.context() as patch:
            patch.setattr(subprocess, "Popen", refuse_remove)
            with pytest.raises(SandboxBackendError, match="unknown-convergence"):
                await DockerSandboxExecutor(cas).execute(request, recorder)
        view = await read_execution(
            store, cas, stream_id="session:cleanup-failure", execution_id=request.execution_id
        )
        container_id = view.outcome["container_id"]
        assert view.outcome["converged"] is False
        assert view.result["payload"]["exit_code"] == 0
        assert view.result["payload"]["stdout"] == "d29yay1yZWFsbHktcmFuCg=="
    finally:
        if container_id is None:
            events = await store.read("session:cleanup-failure")
            container_id = next(
                (e.data.get("container_id") for e in events if e.type == "sandbox/outcome"), None
            )
        if container_id:
            cleanup = asyncio.create_task(
                asyncio.to_thread(
                    subprocess.run,
                    ["docker", "--context", settings.docker_context, "rm", "--force", container_id],
                    check=True, stdout=subprocess.DEVNULL, timeout=15,
                )
            )
            await await_worker_convergence(cleanup)
            cleanup.result()
