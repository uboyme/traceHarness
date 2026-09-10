"""One bounded Docker execution, owned until its container has converged.

The backend owns transient OS objects only. The caller supplies its existing
ledger writer; input/output bytes use the existing content-addressed store.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import subprocess
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from traceh.api.artifacts import ArtifactCas
from traceh.api.json_types import JsonValue, canonical_json, fingerprint, to_json_value
from traceh.api.sandbox import SandboxRecorder, SandboxRequest, validate_relative_path
from traceh.concurrency import await_worker_convergence
from traceh.sandbox.workspace import SandboxFile, WorkspaceSnapshot, protected, snapshot


class SandboxBackendError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SandboxResult:
    status: str
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    files: tuple[SandboxFile, ...]
    receipt: dict[str, JsonValue]
    directories: tuple[str, ...]
    input_snapshot: WorkspaceSnapshot


class DockerSandboxExecutor:
    """No host subprocess fallback, image pulling, network grant or host mounts."""

    def __init__(self, cas: ArtifactCas) -> None:
        self._cas = cas

    async def execute(self, request: SandboxRequest, record: SandboxRecorder, *, channel=None
                      ) -> SandboxResult:
        if (request.stdio is None) != (channel is None):
            raise ValueError("sandbox-stdio-channel-required")
        cas_root = self._cas.local_root
        if cas_root is not None and cas_root.is_relative_to(request.workspace):
            from traceh.api.sandbox import within_scope

            relative = cas_root.relative_to(request.workspace).as_posix()
            if not protected(relative) and not within_scope(
                relative, request.policy.excluded_paths
            ):
                raise ValueError("sandbox-cas-must-be-outside-readable-workspace")
        worker = asyncio.create_task(asyncio.to_thread(snapshot, request.workspace, request.policy))
        try:
            initial = await asyncio.shield(worker)
        except BaseException:
            await await_worker_convergence(worker)
            raise
        files = initial.files
        manifest = [{"path": f.path, "digest": f.digest, "executable": f.executable} for f in files]
        # Persist bytes in CAS before the ledger reference; a crash can leave an
        # unreferenced blob, never an event pointing at missing committed bytes.
        input_blob = await self._cas.put(
            canonical_json(
                {
                    "files": {f.path: base64.b64encode(f.content).decode() for f in files},
                    "manifest": manifest,
                    "directories": list(initial.directories),
                }
            ).encode()
        )
        identity = {
            "format": 1,
            "execution_id": request.execution_id,
            "owner": asdict(request.owner),
            "workspace": str(request.workspace),
            "policy": asdict(request.policy),
            "argv_digest": fingerprint(request.argv),
            "cwd": request.cwd,
            "environment_digest": fingerprint(request.environment),
            "input_blob": asdict(input_blob),
            "retain_output": request.retain_output,
            "export_workspace": request.export_workspace,
            "stdio": asdict(request.stdio) if request.stdio is not None else None,
        }
        request_digest = fingerprint(identity)
        cancellation: asyncio.CancelledError | None = None
        admission = asyncio.create_task(
            record("sandbox/request", to_json_value({**identity, "digest": request_digest}))
        )
        try:
            await asyncio.shield(admission)
        except asyncio.CancelledError as error:
            cancellation = error
            await await_worker_convergence(admission)
            admission.result()
        if cancellation is not None:
            raw = {"status": "cancelled", "converged": True}
        else:
            cancel = threading.Event()
            task = asyncio.create_task(asyncio.to_thread(
                _execute, request, initial, cancel, channel
            ))
            try:
                raw = await asyncio.shield(task)
            except asyncio.CancelledError as error:
                cancellation = error
                cancel.set()
                await await_worker_convergence(task)
                raw = task.result()

        async def finish() -> SandboxResult:
            durable = to_json_value(raw)
            if not request.retain_output and durable.get("payload"):
                payload = durable["payload"]
                for part in ("stdout", "stderr"):
                    content = _decode(payload[part])
                    payload[part + "_sha256"] = hashlib.sha256(content).hexdigest()
                    payload[part + "_bytes"] = len(content)
                    payload[part] = ""
                payload.update(files={}, directories=[], executables=[])
            blob = await self._cas.put(canonical_json(durable).encode())
            receipt = to_json_value(
                {
                    "format": 1,
                    "execution_id": request.execution_id,
                    "owner": asdict(request.owner),
                    "request_digest": request_digest,
                    "policy_digest": fingerprint(request.policy),
                    "status": raw["status"],
                    "converged": raw["converged"],
                    "container_id": raw.get("container_id"),
                    "backend": raw.get("backend"),
                    "started_at": raw.get("started_at"),
                    "finished_at": raw.get("finished_at"),
                    "failure_code": raw.get("failure_code"),
                    "cleanup_failures": raw.get("cleanup_failures", []),
                    "output_blob": asdict(blob),
                }
            )
            receipt["digest"] = fingerprint(receipt)
            await record("sandbox/outcome", receipt)
            payload = raw.get("payload") or {}
            return SandboxResult(
                raw["status"],
                payload.get("exit_code"),
                _decode(payload.get("stdout", "")),
                _decode(payload.get("stderr", "")),
                tuple(
                    SandboxFile(name, _decode(value), name in payload.get("executables", []))
                    for name, value in sorted(payload.get("files", {}).items())
                ),
                receipt,
                tuple(payload.get("directories", [])),
                initial,
            )

        finalizer = asyncio.create_task(finish())
        try:
            result = await asyncio.shield(finalizer)
        except asyncio.CancelledError as error:
            cancellation = cancellation or error
            await await_worker_convergence(finalizer)
            result = finalizer.result()
        failure = None
        if not result.receipt["converged"]:
            failure = SandboxBackendError("sandbox-unknown-convergence")
        elif result.receipt["cleanup_failures"]:
            failure = SandboxBackendError("sandbox-cleanup-failed")
        if failure is not None:
            causes = [SandboxBackendError(code) for code in result.receipt["cleanup_failures"]]
            if result.receipt["failure_code"]:
                causes.insert(0, SandboxBackendError(result.receipt["failure_code"]))
            if causes:
                failure.__cause__ = (
                    causes[0]
                    if len(causes) == 1
                    else ExceptionGroup("sandbox execution and cleanup failed", causes)
                )
        if cancellation is not None:
            if failure is not None:
                raise cancellation from failure
            raise cancellation
        if failure is not None:
            raise failure
        return result


def _decode(value: str) -> bytes:
    return base64.b64decode(value, validate=True)


def _execute(request: SandboxRequest, initial: WorkspaceSnapshot, cancel: threading.Event,
             channel=None):
    files = initial.files
    policy = request.policy
    limits = policy.limits
    name = "traceh-exec-" + request.execution_id
    attempted = False
    identity = None
    result = {"status": "backend-unavailable", "converged": True}
    # The controller is trusted; child output cannot write to this channel.
    wire_limit = 4 * (limits.workspace_bytes + 2 * limits.output_bytes) + 1_048_576

    def cli(*argv: str, check: bool = True):
        with tempfile.TemporaryFile() as stream:
            process = subprocess.Popen(
                ["docker", "--context", policy.docker_context, *argv],
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=stream,
            )
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                raise SandboxBackendError("sandbox-backend-timeout") from None
            stream.seek(0)
            output = stream.read(wire_limit + 1)
            if len(output) > wire_limit:
                raise SandboxBackendError("sandbox-backend-output-limit")
            if check and process.returncode:
                # Docker diagnostics can contain local paths or daemon config.
                raise SandboxBackendError(f"sandbox-docker-{argv[0]}-failed")
            return process.returncode, output.decode("utf-8", "strict")

    scratch = tempfile.TemporaryDirectory(prefix="traceh-sandbox-control-")
    try:
        _, version = cli("version", "--format", "{{json .Server}}")
        backend = json.loads(version)
        if backend["Os"] != "linux":
            raise SandboxBackendError("sandbox-platform-unsupported")
        result["backend"] = {key: backend[key] for key in ("Version", "ApiVersion", "Os", "Arch")}
        _, info_raw = cli("info", "--format", "{{json .}}")
        info = json.loads(info_raw)
        if info["CgroupVersion"] != "2" or not all(
            info.get(key)
            for key in (
                "MemoryLimit",
                "SwapLimit",
                "CpuCfsPeriod",
                "CpuCfsQuota",
                "PidsLimit",
            )
        ):
            raise SandboxBackendError("sandbox-resource-controls-unsupported")
        _, image_raw = cli("image", "inspect", policy.image)
        image = json.loads(image_raw)[0]
        if image["Id"] != policy.image or image["Os"] != "linux" or image["Config"].get("Volumes"):
            raise SandboxBackendError("sandbox-image-contract-unsupported")
        if cancel.is_set():
            result["status"] = "cancelled"
            return result
        control = Path(scratch.name, "request.json")
        control.write_text(
            canonical_json(
                {
                    "argv": list(request.argv),
                    "environment": dict(request.environment),
                    "cwd": request.cwd,
                    "files": {f.path: base64.b64encode(f.content).decode() for f in files},
                    "executables": [f.path for f in files if f.executable],
                    "directories": list(initial.directories),
                    "export_workspace": request.export_workspace,
                    "stdio": asdict(request.stdio) if request.stdio is not None else None,
                    "wall_seconds": limits.wall_seconds,
                    "output_bytes": limits.output_bytes,
                    "workspace_bytes": limits.workspace_bytes,
                    "workspace_files": limits.workspace_files,
                }
            ),
            "utf-8",
        )
        guest = Path(__file__).with_name("_guest.py").read_text("utf-8")
        if channel is not None:
            guest = Path(__file__).with_name("_guest_stdio.py").read_text("utf-8") + "\n" + guest
        # Mark uncertain creation before dispatch, so even a timed-out create
        # must reconcile its exact name before returning.
        attempted = True
        _, created = cli(
            "create",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "SETUID",
            "--cap-add",
            "SETGID",
            "--cap-add",
            "KILL",
            "--cap-add",
            "CHOWN",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "0:0",
            "--pids-limit",
            str(limits.pids),
            "--memory",
            str(limits.memory_bytes),
            "--memory-swap",
            str(limits.memory_bytes),
            "--cpus",
            str(limits.cpus),
            "--restart",
            "no",
            "--tmpfs",
            f"/workspace:rw,nosuid,nodev,size={limits.workspace_bytes}",
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,noexec,size={limits.workspace_bytes}",
            "--mount",
            f"type=bind,source={control},target=/request.json,readonly",
            "--label",
            f"traceh.execution={request.execution_id}",
            "--label",
            f"traceh.owner={fingerprint(request.owner)}",
            "--entrypoint",
            "python",
            policy.image,
            "-I",
            "-c",
            guest,
        )
        identity = created.strip()
        result["container_id"] = identity
        _, inspection = cli("inspect", identity)
        current = json.loads(inspection)[0]
        if (
            current["Image"] != policy.image
            or current["HostConfig"]["NetworkMode"] != "none"
            or not current["HostConfig"]["ReadonlyRootfs"]
            or current["HostConfig"]["Privileged"]
            or current["HostConfig"]["Memory"] != limits.memory_bytes
            or current["HostConfig"]["PidsLimit"] != limits.pids
        ):
            raise SandboxBackendError("sandbox-backend-contract-mismatch")
        cli("start", identity)
        if channel is not None:
            channel.bind(identity)
        while True:
            _, inspection = cli("inspect", identity)
            current = json.loads(inspection)[0]
            result["started_at"] = current["State"]["StartedAt"]
            if not current["State"]["Running"]:
                result["finished_at"] = current["State"]["FinishedAt"]
                break
            if cancel.wait(0.05):
                cli("kill", identity, check=False)
                result["status"] = "cancelled"
                break
        if result["status"] != "cancelled":
            _, output = cli("logs", identity)
            payload = json.loads(output)
            _validate_payload(payload, request)
            result["payload"] = payload
            result["status"] = payload["status"]
    except (OSError, ValueError, KeyError, SandboxBackendError) as error:
        result["status"] = "execution-failed" if attempted else "backend-unavailable"
        result["failure_code"] = (
            str(error)
            if isinstance(error, SandboxBackendError)
            else "sandbox-backend-result-invalid"
        )
    finally:
        if attempted:
            try:
                # Query the daemon successfully even if create never returned an
                # ID. Do not interpret a failed inspect as proof of absence.
                _, inventory = cli(
                    "ps", "-a", "--no-trunc", "--filter", f"name=^/{name}$", "--format", "{{.ID}}"
                )
                ids = inventory.split()
                if len(ids) > 1:
                    raise SandboxBackendError("sandbox-identity-ambiguous")
                if not ids and identity is None:
                    # A killed create client may still have an in-flight daemon
                    # request. One empty inventory cannot prove it will not commit.
                    raise SandboxBackendError("sandbox-create-commit-unknown")
                if ids:
                    _, inspection = cli("inspect", ids[0])
                    current = json.loads(inspection)[0]
                    if current["Config"]["Labels"].get("traceh.execution") != request.execution_id:
                        raise SandboxBackendError("sandbox-owner-mismatch")
                    if current["Config"]["Labels"].get("traceh.owner") != fingerprint(
                        request.owner
                    ):
                        raise SandboxBackendError("sandbox-owner-mismatch")
                    if identity is not None and current["Id"] != identity:
                        raise SandboxBackendError("sandbox-container-mismatch")
                    if current["State"].get("StartedAt", "").startswith("0001-") is False:
                        result["started_at"] = current["State"].get("StartedAt")
                    if (not current["State"]["Running"]
                            and not current["State"].get("FinishedAt", "").startswith("0001-")):
                        result["finished_at"] = current["State"].get("FinishedAt")
                    cli("rm", "--force", ids[0])
                _, remaining = cli("ps", "-a", "--filter", f"name=^/{name}$", "--format", "{{.ID}}")
                if remaining.strip():
                    raise SandboxBackendError("sandbox-cleanup-incomplete")
            except (OSError, ValueError, KeyError, SandboxBackendError):
                result["status"] = "unknown-convergence"
                result["converged"] = False
                result.setdefault("cleanup_failures", []).append("sandbox-container-cleanup-failed")
        # No host workspace was mounted. The read-only request can be released
        # even after daemon loss; any running namespace still has its deadline.
        try:
            scratch.cleanup()
        except OSError:
            result.setdefault("cleanup_failures", []).append("sandbox-control-cleanup-failed")
            if result["converged"]:
                result["status"] = "cleanup-failed"
    return result


def _validate_payload(payload, request: SandboxRequest) -> None:
    limits = request.policy.limits
    if payload["status"] not in {
        "finished",
        "start-failed",
        "timed-out",
        "output-exceeded",
        "unsafe-workspace",
        "workspace-limit",
        "workspace-unreadable",
    }:
        raise ValueError("sandbox-result-status-invalid")
    if any(len(_decode(payload[part])) > limits.output_bytes for part in ("stdout", "stderr")):
        raise ValueError("sandbox-result-output-limit")
    names: set[str] = set()
    total = 0
    for name in payload["directories"]:
        validate_relative_path(name)
        if protected(name) or name.casefold() in names:
            raise ValueError("sandbox-result-path-refused")
        names.add(name.casefold())
    for name, encoded in payload["files"].items():
        validate_relative_path(name)
        if protected(name) or name.casefold() in names:
            raise ValueError("sandbox-result-path-refused")
        names.add(name.casefold())
        total += len(_decode(encoded))
    if total > limits.workspace_bytes or len(names) > limits.workspace_files:
        raise ValueError("sandbox-result-workspace-limit")
