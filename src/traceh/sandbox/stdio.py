"""Bounded control IO for an already-owned Docker execution.

The transport owns transient CLI clients only. It cannot create containers,
select another owner, or grant execution authority.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
import threading
from pathlib import Path

from traceh.api.json_types import canonical_json
from traceh.concurrency import await_worker_convergence


class SandboxStdioError(RuntimeError):
    pass


class DockerStdioChannel:
    def __init__(self, context, limits):
        self._context = context
        self._limits = limits
        self._container = None
        self._ready = threading.Event()
        self._closed = False
        self._operations: set[asyncio.Task] = set()

    def bind(self, container):
        if self._container is not None:
            raise RuntimeError("sandbox-stdio-already-bound")
        self._container = container
        self._ready.set()

    async def call(self, operation):
        if self._closed:
            raise SandboxStdioError("sandbox-stdio-closed")
        task = asyncio.create_task(asyncio.to_thread(self._call, operation))
        self._operations.add(task)
        try:
            return await asyncio.shield(task)
        finally:
            await await_worker_convergence(task)
            self._operations.discard(task)

    async def close(self):
        self._closed = True
        for task in tuple(self._operations):
            await await_worker_convergence(task)
            # IO errors belong to their call site; cleanup still waits for the
            # actual CLI client. The owning executor records container cleanup.
            if not task.cancelled():
                task.exception()

    def _call(self, operation):
        if not self._ready.is_set():
            raise SandboxStdioError("sandbox-stdio-not-ready")
        maximum = 2 * self._limits.frame_bytes + 4096
        wire = canonical_json(operation).encode()
        if len(wire) > maximum:
            raise ValueError("sandbox-stdio-frame-limit")
        helper = Path(__file__).with_name("_stdio_client.py").read_text("utf-8")
        with tempfile.TemporaryFile() as incoming, tempfile.TemporaryFile() as outgoing:
            incoming.write(wire)
            incoming.seek(0)
            try:
                process = subprocess.Popen(
                    ["docker", "--context", self._context, "exec", "--interactive", "--user", "0",
                     self._container, "python", "-I", "-c", helper, str(maximum)],
                    stdin=incoming, stdout=outgoing, stderr=subprocess.DEVNULL,
                )
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                    raise SandboxStdioError("sandbox-stdio-control-timeout") from None
                outgoing.seek(0)
                payload = outgoing.read(maximum + 1)
                if process.returncode or len(payload) > maximum:
                    raise SandboxStdioError("sandbox-stdio-control-failed")
                response = json.loads(payload)
                if type(response) is not dict or response.get("ok") is not True:
                    raise SandboxStdioError("sandbox-stdio-request-failed")
                return response
            except (OSError, ValueError):
                raise SandboxStdioError("sandbox-stdio-control-failed") from None
