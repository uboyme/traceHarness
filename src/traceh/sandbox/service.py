"""Owned command scopes shared by Tool, verification and trusted adapters."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

from traceh.api.artifacts import ArtifactCas
from traceh.api.json_types import fingerprint, to_json_value
from traceh.api.sandbox import (
    SandboxCommandResult,
    SandboxConfiguration,
    SandboxOwner,
    SandboxPolicy,
    SandboxRequest,
    SandboxStdioLimits,
    narrow_policy,
)
from traceh.concurrency import await_worker_convergence, combine_failures
from traceh.sandbox.docker import DockerSandboxExecutor
from traceh.sandbox.ledger import SandboxEventRecorder
from traceh.sandbox.publication import publish
from traceh.sandbox.stdio import DockerStdioChannel, SandboxStdioError
from traceh.sandbox.workspace import WorkspaceSnapshot
from traceh.session.event_store import EventStore

# Host policy defaults for the supported Linux guest, never inherited host env.
GUEST_ENVIRONMENT = (
    ("PATH", "/usr/local/bin:/usr/bin:/bin"),
    ("LANG", "C.UTF-8"),
    ("HOME", "/tmp"),
    ("TMPDIR", "/tmp"),
    ("PYTHONIOENCODING", "utf-8"),
)


def build_sandbox_service(store: EventStore, configuration: SandboxConfiguration):
    from traceh.artifacts.cas import LocalArtifactCas

    return SandboxExecutionService(
        store=store,
        cas=LocalArtifactCas(configuration.cas_root),
        policy=configuration.policy,
    )


class SandboxExecutionService:
    def __init__(self, *, store: EventStore, cas: ArtifactCas, policy: SandboxPolicy) -> None:
        self.store = store
        self.cas = cas
        self.policy = policy
        self.executor = DockerSandboxExecutor(cas)

    def scope(
        self,
        owner: SandboxOwner,
        *,
        stream_id: str,
        workspace: Path,
        data_dir: Path | None,
        publish_changes: bool,
        environment: tuple[tuple[str, str], ...] = GUEST_ENVIRONMENT,
        retain_output: bool = True,
    ) -> SandboxCommandScope:
        policy = self.policy
        if data_dir == workspace:
            raise ValueError("sandbox-workspace-is-data-directory")
        if data_dir is not None and data_dir.is_relative_to(workspace):
            excluded = data_dir.relative_to(workspace).as_posix()
            policy = replace(
                policy, excluded_paths=tuple(sorted(set(policy.excluded_paths) | {excluded}))
            )
        return SandboxCommandScope(
            self.executor,
            SandboxEventRecorder(self.store, stream_id, owner),
            policy,
            workspace,
            environment,
            publish_changes,
            retain_output,
        )


class SandboxCommandScope:
    """One owner's lifetime; at most one command at a time, no detached work."""

    def __init__(
        self, executor, recorder, policy, workspace, environment, publish_changes, retain_output
    ):
        self._executor = executor
        self._recorder = recorder
        self._policy = policy
        self._workspace = workspace
        self._environment = environment
        self._publish_changes = publish_changes
        self._retain_output = retain_output
        self._open = False
        self._closed = False
        self._task: asyncio.Task | None = None
        self._process: SandboxProcess | None = None

    async def __aenter__(self):
        if self._open or self._closed:
            raise RuntimeError("sandbox-scope-already-used")
        self._open = True
        return self

    async def __aexit__(self, exc_type, error, traceback):
        caller = asyncio.current_task()
        cancellations = caller.cancelling()
        self._open = False
        self._closed = True
        if self._process is not None:
            await self._process._channel.close()
        task = self._task
        if task is not None:
            task.cancel()
            await await_worker_convergence(task)
            failure = _task_failure(task)
            if failure is not None and failure is not error:
                if isinstance(error, asyncio.CancelledError):
                    raise error from failure
                if error is None and caller.cancelling() > cancellations:
                    raise asyncio.CancelledError from failure
                raise combine_failures(error, failure, "sandbox scope cleanup failed")
            if error is None and caller.cancelling() > cancellations:
                raise asyncio.CancelledError

    async def run(
        self, argv, *, timeout_seconds: float, cwd: str = ".", max_output_bytes: int | None = None
    ) -> SandboxCommandResult:
        if not self._open:
            raise RuntimeError("sandbox-scope-closed")
        if self._task is not None:
            raise RuntimeError("sandbox-owner-command-already-running")
        policy = narrow_policy(
            self._policy, timeout_seconds=timeout_seconds, max_output_bytes=max_output_bytes
        )
        request = SandboxRequest(
            uuid4().hex,
            self._recorder.owner,
            self._workspace,
            argv,
            self._environment,
            policy,
            cwd,
            self._retain_output,
            self._publish_changes,
        )
        task = asyncio.create_task(self._run(request))
        self._task = task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError as cancellation:
            task.cancel()
            await await_worker_convergence(task)
            failure = _task_failure(task)
            if failure is not None:
                raise cancellation from failure
            raise
        finally:
            self._task = None

    async def open_process(
        self, argv, *, timeout_seconds: float, stdio: SandboxStdioLimits, cwd: str = "."
    ) -> SandboxProcess:
        if not self._open:
            raise RuntimeError("sandbox-scope-closed")
        if self._task is not None:
            raise RuntimeError("sandbox-owner-command-already-running")
        if self._publish_changes:
            raise ValueError("sandbox-process-workspace-publication-refused")
        policy = narrow_policy(self._policy, timeout_seconds=timeout_seconds)
        request = SandboxRequest(
            uuid4().hex, self._recorder.owner, self._workspace, argv, self._environment,
            policy, cwd, self._retain_output, False, stdio,
        )
        channel = DockerStdioChannel(policy.docker_context, stdio)
        task = asyncio.create_task(self._run(request, channel=channel))
        self._task = task
        process = SandboxProcess(self, channel, task, stdio)
        self._process = process
        try:
            while not task.done():
                try:
                    await channel.call({"operation": "hello"})
                    return process
                except SandboxStdioError:
                    # Only this side-effect-free startup handshake is retried.
                    # All writes either return an acknowledgement or close.
                    await asyncio.sleep(0.05)
            task.result()
            raise SandboxStdioError("sandbox-process-ended-before-connection")
        except BaseException as error:
            await self.__aexit__(type(error), error, None)
            raise

    async def _run(self, request: SandboxRequest, *, channel=None) -> SandboxCommandResult:
        result = await self._executor.execute(request, self._recorder, channel=channel)
        publication = None
        status = result.status
        if self._publish_changes and status == "finished":

            async def complete_publication():
                change = await asyncio.to_thread(
                    publish,
                    self._workspace,
                    request.policy,
                    result.input_snapshot,
                    WorkspaceSnapshot(result.files, result.directories),
                )
                data = to_json_value(
                    {
                        "format": 1,
                        "execution_id": request.execution_id,
                        "owner": asdict(request.owner),
                        "outcome_digest": result.receipt["digest"],
                        **asdict(change),
                    }
                )
                data["digest"] = fingerprint(data)
                await self._recorder("sandbox/publication", data)
                return data

            finalizer = asyncio.create_task(complete_publication())
            try:
                publication = await asyncio.shield(finalizer)
            except asyncio.CancelledError:
                await await_worker_convergence(finalizer)
                finalizer.result()
                raise
            if publication["status"] != "completed":
                status = "publication-failed"
        return SandboxCommandResult(
            status,
            result.exit_code,
            result.stdout,
            result.stderr,
            result.receipt,
            publication,
        )


class SandboxProcess:
    """One live stdio connection, closed by the same execution scope as commands."""

    def __init__(self, scope, channel, task, limits):
        self._scope = scope
        self._channel = channel
        self._task = task
        self._limits = limits
        self._offsets = [0, 0]
        self._reads = [asyncio.Lock(), asyncio.Lock()]
        self._writes = asyncio.Lock()
        self._input_bytes = 0
        self._input_closed = False

    def _require_open(self):
        if not self._scope._open:
            raise SandboxStdioError("sandbox-stdio-closed")

    async def _call(self, operation):
        self._require_open()
        try:
            return await self._channel.call(operation)
        except BaseException as error:
            await self._scope.__aexit__(type(error), error, None)
            raise

    async def write(self, data: bytes) -> None:
        if type(data) is not bytes or len(data) > self._limits.frame_bytes:
            raise ValueError("sandbox-stdio-frame-limit")
        async with self._writes:
            self._require_open()
            if self._input_closed:
                raise SandboxStdioError("sandbox-stdio-input-closed")
            if self._input_bytes + len(data) > self._limits.input_bytes:
                raise ValueError("sandbox-stdio-input-limit")
            self._input_bytes += len(data)
            response = await self._call(
                {"operation": "write", "data": base64.b64encode(data).decode()}
            )
            if response.get("written") != len(data):
                await self.aclose()
                raise SandboxStdioError("sandbox-stdio-write-unknown")

    async def close_input(self):
        async with self._writes:
            self._require_open()
            if not self._input_closed:
                try:
                    await self._channel.call({"operation": "close-input"})
                except SandboxStdioError:
                    # EOF may make the program finish before its control helper
                    # returns. A converged terminal execution has no open stdin;
                    # consult its original result, never resend the operation.
                    try:
                        await asyncio.shield(self._task)
                    except BaseException as error:
                        await self._scope.__aexit__(type(error), error, None)
                        raise
                except BaseException as error:
                    await self._scope.__aexit__(type(error), error, None)
                    raise
                self._input_closed = True

    async def read(self, max_bytes: int, *, stream: str = "stdout") -> bytes:
        if type(max_bytes) is not int or not 1 <= max_bytes <= self._limits.frame_bytes:
            raise ValueError("sandbox-stdio-frame-limit")
        if stream not in ("stdout", "stderr"):
            raise ValueError("sandbox-stdio-stream-invalid")
        index = 0 if stream == "stdout" else 1
        async with self._reads[index]:
            try:
                while True:
                    self._require_open()
                    offset = self._offsets[index]
                    if self._task.done():
                        result = self._task.result()
                        content = getattr(result, stream)[offset:offset + max_bytes]
                        self._offsets[index] += len(content)
                        return content
                    try:
                        response = await self._channel.call(
                            {"operation": "read", "stream": index, "offset": offset,
                             "size": max_bytes}
                        )
                    except SandboxStdioError:
                        # The guest can exit between inspect and a read. Wait
                        # for the existing execution's durable terminal result;
                        # it carries the bounded tail without rerunning anything.
                        await asyncio.shield(self._task)
                        continue
                    content = base64.b64decode(response["data"], validate=True)
                    if content or response["eof"]:
                        self._offsets[index] += len(content)
                        return content
                    await asyncio.sleep(0.05)
            except BaseException as error:
                await self._scope.__aexit__(type(error), error, None)
                raise

    async def wait(self) -> SandboxCommandResult:
        self._require_open()
        try:
            return await asyncio.shield(self._task)
        except BaseException as error:
            await self._scope.__aexit__(type(error), error, None)
            raise

    async def aclose(self):
        await self._scope.__aexit__(None, None, None)


def _task_failure(task: asyncio.Task) -> BaseException | None:
    try:
        task.result()
    except asyncio.CancelledError as cancelled:
        return cancelled.__cause__
    except BaseException as error:
        return error
    return None
