"""Explicit backend selection for tests which execute real guest programs."""

import asyncio
import os
import subprocess
import tempfile

import pytest

from traceh.api.sandbox import SandboxLimits, SandboxPolicy
from traceh.sandbox.service import SandboxExecutionService


def real_sandbox_policy():
    image = os.environ.get("TRACEH_SANDBOX_TEST_IMAGE")
    context = os.environ.get("TRACEH_SANDBOX_TEST_CONTEXT")
    if not image or not context:
        pytest.skip("explicit real Docker test image and context required")
    return SandboxPolicy(
        context,
        image,
        SandboxLimits(128 * 1024 * 1024, 8 * 1024 * 1024, 256, 1024 * 1024, 32, 0.5, 10),
        (".",),
        (".",),
    )


def real_sandbox_service(store, cas):
    return SandboxExecutionService(store=store, cas=cas, policy=real_sandbox_policy())


async def wait_for_guest(store, stream_id, policy, running, *, relative=None):
    """Observe this execution's real non-root child, optionally its flushed marker."""
    from traceh.concurrency import await_worker_convergence

    def probe(name):
        argv = ["docker", "--context", policy.docker_context]
        if relative is None:
            argv += ["top", name, "-eo", "pid,uid"]
        else:
            argv += [
                "exec",
                name,
                "python",
                "-c",
                "from pathlib import Path; import sys; "
                "assert Path('/workspace',sys.argv[1]).read_text() == 'flushed'",
                relative,
            ]
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(argv, stdout=output, stderr=subprocess.DEVNULL, timeout=5)
            output.seek(0)
            return result.returncode == 0 and (
                relative is not None or b"65532" in output.read(65536)
            )

    async with asyncio.timeout(30):
        while True:
            if running.done():
                raise AssertionError("execution ended before the workload was observed")
            events = await store.read(stream_id)
            request = next((e for e in events if e.type == "sandbox/request"), None)
            if request is None:
                await asyncio.sleep(0)
                continue
            check = asyncio.create_task(
                asyncio.to_thread(probe, "traceh-exec-" + request.data["execution_id"])
            )
            try:
                if await asyncio.shield(check):
                    return request.data["execution_id"]
            finally:
                await await_worker_convergence(check)
