"""Host-owned isolated execution values; no model-granted authority or storage."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from traceh.api.json_types import JsonValue


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    memory_bytes: int
    workspace_bytes: int
    workspace_files: int
    output_bytes: int
    pids: int
    cpus: float
    wall_seconds: float

    def __post_init__(self) -> None:
        for value in (
            self.memory_bytes,
            self.workspace_bytes,
            self.workspace_files,
            self.output_bytes,
            self.pids,
        ):
            if type(value) is not int or value < 1:
                raise ValueError("sandbox-limits-must-be-positive-integers")
        if self.memory_bytes < 64 * 1024 * 1024 or self.pids < 4:
            raise ValueError("sandbox-supervisor-reserve-too-small")
        for value in (self.cpus, self.wall_seconds):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError("sandbox-limit-must-be-positive-finite")


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    """Explicit host policy. The first backend supports Linux and no network."""

    docker_context: str
    image: str
    limits: SandboxLimits
    read_paths: tuple[str, ...]
    write_paths: tuple[str, ...]
    excluded_paths: tuple[str, ...] = ()
    network: str = "none"

    def __post_init__(self) -> None:
        if not self.docker_context.strip() or "\0" in self.docker_context:
            raise ValueError("sandbox-docker-context-required")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.image):
            raise ValueError("sandbox-pinned-image-required")
        if self.network != "none":
            raise ValueError("sandbox-network-policy-unsupported")
        if type(self.limits) is not SandboxLimits:
            raise TypeError("sandbox-limits-required")
        for paths in (self.read_paths, self.write_paths, self.excluded_paths):
            if type(paths) is not tuple or len(set(paths)) != len(paths):
                raise ValueError("sandbox-paths-must-be-unique-tuple")
            for path in paths:
                validate_relative_path(path, root_allowed=True)
        if not self.read_paths:
            raise ValueError("sandbox-read-scope-required")
        for path in self.write_paths:
            if not within_scope(path, self.read_paths):
                raise ValueError("sandbox-write-scope-outside-read-scope")


@dataclass(frozen=True, slots=True)
class SandboxConfiguration:
    policy: SandboxPolicy
    cas_root: Path
    plugin_grants: tuple[SandboxPluginGrant, ...] = ()

    def __post_init__(self) -> None:
        if type(self.policy) is not SandboxPolicy or not self.cas_root.is_absolute():
            raise ValueError("sandbox-host-configuration-invalid")
        if (type(self.plugin_grants) is not tuple
                or any(type(grant) is not SandboxPluginGrant for grant in self.plugin_grants)
                or len({g.plugin_id for g in self.plugin_grants}) != len(self.plugin_grants)):
            raise ValueError("sandbox-plugin-grants-invalid")


def validate_relative_path(value: str, *, root_allowed: bool = False) -> None:
    """One portable spelling; reject Windows aliases even for a Linux guest."""

    if root_allowed and value == ".":
        return
    if not isinstance(value, str) or not value or any(c in value for c in "\\:\0"):
        raise ValueError("sandbox-path-invalid")
    for part in value.split("/"):
        if not part or part in (".", "..") or part.rstrip(" .") != part:
            raise ValueError("sandbox-path-invalid")
        stem = part.split(".", 1)[0].upper()
        if stem in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?:COM|LPT)[1-9]", stem):
            raise ValueError("sandbox-device-path-refused")


def within_scope(path: str, scopes: tuple[str, ...]) -> bool:
    return any(scope == "." or path == scope or path.startswith(scope + "/") for scope in scopes)


@dataclass(frozen=True, slots=True)
class SandboxOwner:
    """Identity supplied by the existing execution owner after its admission."""

    kind: str
    owner_id: str
    session_id: str | None = None
    turn_id: str | None = None
    step_id: str | None = None
    tool_call_id: str | None = None
    agent_id: str | None = None
    budget_reservation: str | None = None
    budget_admission: str | None = None
    plugin_id: str | None = None
    plugin_version: str | None = None
    activation_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"effect", "verification", "promotion", "plugin"}:
            raise ValueError("sandbox-owner-kind-invalid")
        if not self.owner_id.strip():
            raise ValueError("sandbox-owner-required")
        if self.kind == "effect" and not all(
            (self.session_id, self.turn_id, self.step_id, self.tool_call_id)
        ):
            raise ValueError("sandbox-effect-identity-incomplete")
        if self.kind == "plugin" and not all(
            (self.plugin_id, self.plugin_version, self.activation_id)
        ):
            raise ValueError("sandbox-plugin-identity-incomplete")


@dataclass(frozen=True, slots=True)
class SandboxStdioLimits:
    """Host-granted limits for one external program's byte transport."""

    input_bytes: int
    frame_bytes: int

    def __post_init__(self) -> None:
        if any(type(v) is not int or v < 1 for v in (self.input_bytes, self.frame_bytes)):
            raise ValueError("sandbox-stdio-limits-invalid")
        if self.frame_bytes > self.input_bytes:
            raise ValueError("sandbox-stdio-frame-exceeds-input-limit")


@dataclass(frozen=True, slots=True)
class SandboxPluginGrant:
    """Explicit host grant for one trusted plugin version's activation."""

    plugin_id: str
    version: str
    workspace: Path
    stdio: SandboxStdioLimits
    max_processes: int

    def __post_init__(self) -> None:
        if (not self.plugin_id.strip() or not self.version.strip()
                or not self.workspace.is_absolute()
                or type(self.stdio) is not SandboxStdioLimits
                or type(self.max_processes) is not int or self.max_processes < 1):
            raise ValueError("sandbox-plugin-grant-invalid")


@dataclass(frozen=True, slots=True)
class SandboxRequest:
    execution_id: str
    owner: SandboxOwner
    workspace: Path
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    policy: SandboxPolicy
    cwd: str = "."
    retain_output: bool = True
    export_workspace: bool = True
    stdio: SandboxStdioLimits | None = None

    def __post_init__(self) -> None:
        if self.stdio is not None and type(self.stdio) is not SandboxStdioLimits:
            raise ValueError("sandbox-stdio-limits-invalid")
        if type(self.retain_output) is not bool or type(self.export_workspace) is not bool:
            raise ValueError("sandbox-output-retention-invalid")
        if not re.fullmatch(r"[0-9a-f]{32}", self.execution_id):
            raise ValueError("sandbox-execution-id-invalid")
        if not self.workspace.is_absolute():
            raise ValueError("sandbox-workspace-must-be-absolute")
        if (
            type(self.argv) is not tuple
            or not self.argv
            or any(not isinstance(arg, str) or "\0" in arg for arg in self.argv)
            or not self.argv[0]
        ):
            raise ValueError("sandbox-argv-invalid")
        validate_relative_path(self.cwd, root_allowed=True)
        if not within_scope(self.cwd, self.policy.read_paths) and self.cwd != ".":
            raise ValueError("sandbox-cwd-outside-read-scope")
        keys: set[str] = set()
        if type(self.environment) is not tuple:
            raise ValueError("sandbox-environment-must-be-immutable")
        for entry in self.environment:
            if type(entry) is not tuple or len(entry) != 2:
                raise ValueError("sandbox-environment-entry-invalid")
            key, value = entry
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or "\0" in value or key in keys:
                raise ValueError("sandbox-environment-invalid")
            keys.add(key)


class SandboxRecorder(Protocol):
    """Bound writer into the existing owner's EventStore stream, never another DB."""

    async def __call__(self, event_type: str, data: dict[str, JsonValue]) -> None: ...


@dataclass(frozen=True, slots=True)
class SandboxCommandResult:
    status: str
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    receipt: dict[str, JsonValue]
    publication: dict[str, JsonValue] | None


@dataclass(frozen=True, slots=True)
class SandboxReceiptReference:
    stream_id: str
    execution_id: str
    receipt_digest: str
    policy_digest: str

    def __post_init__(self) -> None:
        if not self.stream_id or not re.fullmatch(r"[0-9a-f]{32}", self.execution_id):
            raise ValueError("sandbox-receipt-reference-invalid")
        if any(not re.fullmatch(r"[0-9a-f]{64}", value)
               for value in (self.receipt_digest, self.policy_digest)):
            raise ValueError("sandbox-receipt-reference-invalid")

    @classmethod
    def from_dict(cls, value):
        if type(value) is not dict or set(value) != {
            "stream_id", "execution_id", "receipt_digest", "policy_digest",
        }:
            raise ValueError("sandbox-receipt-reference-invalid")
        return cls(**value)


class SandboxCommandPort(Protocol):
    """A live owner capability; callers cannot select backend, environment or grants."""

    async def run(
        self, argv: tuple[str, ...], *, timeout_seconds: float, cwd: str = ".",
        max_output_bytes: int | None = None,
    ) -> SandboxCommandResult:
        ...


class SandboxProcessPort(Protocol):
    """Byte transport for one activation-owned external process."""

    async def write(self, data: bytes) -> None: ...

    async def read(self, max_bytes: int, *, stream: str = "stdout") -> bytes: ...

    async def close_input(self) -> None: ...

    async def wait(self) -> SandboxCommandResult: ...

    async def aclose(self) -> None: ...


def narrow_policy(
    policy: SandboxPolicy, *, timeout_seconds: float, max_output_bytes: int | None = None,
) -> SandboxPolicy:
    if (type(timeout_seconds) not in (float, int)
            or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
        raise ValueError("sandbox-command-timeout-invalid")
    if max_output_bytes is not None and (type(max_output_bytes) is not int or max_output_bytes < 1):
        raise ValueError("sandbox-command-output-limit-invalid")
    return replace(policy, limits=replace(
        policy.limits, wall_seconds=min(timeout_seconds, policy.limits.wall_seconds),
        output_bytes=min(max_output_bytes, policy.limits.output_bytes)
        if max_output_bytes is not None else policy.limits.output_bytes,
    ))
