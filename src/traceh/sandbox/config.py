"""Explicit host sandbox policy file; no secrets or guessed backend selection."""

import json
from dataclasses import dataclass, fields
from pathlib import Path

from traceh.api.sandbox import SandboxLimits, SandboxPluginGrant, SandboxPolicy, SandboxStdioLimits

MAX_CONFIG_BYTES = 65536
SANDBOX_CONFIG_FORMAT = 2


@dataclass(frozen=True, slots=True)
class SandboxHostSettings:
    policy: SandboxPolicy
    plugin_grants: tuple[SandboxPluginGrant, ...]


def parse_sandbox_config(value: object) -> SandboxHostSettings:
    try:
        if type(value) is not dict or set(value) != {"format", "policy", "plugin_grants"}:
            raise ValueError
        if type(value["format"]) is not int or value["format"] != SANDBOX_CONFIG_FORMAT:
            raise ValueError
        data = value["policy"]
        if type(data) is not dict or set(data) != {field.name for field in fields(SandboxPolicy)}:
            raise ValueError
        limits = data["limits"]
        limit_fields = {field.name for field in fields(SandboxLimits)}
        if type(limits) is not dict or set(limits) != limit_fields:
            raise ValueError
        if any(type(data[key]) is not str for key in ("docker_context", "image", "network")):
            raise ValueError
        data = dict(data)
        for name in ("read_paths", "write_paths", "excluded_paths"):
            if type(data[name]) is not list or any(type(path) is not str for path in data[name]):
                raise ValueError
            data[name] = tuple(data[name])
        data["limits"] = SandboxLimits(**limits)
        policy = SandboxPolicy(**data)
        raw_grants = value["plugin_grants"]
        if type(raw_grants) is not list:
            raise ValueError
        grants = []
        for grant in raw_grants:
            if type(grant) is not dict or set(grant) != {
                field.name for field in fields(SandboxPluginGrant)
            }:
                raise ValueError
            if any(type(grant[key]) is not str for key in ("plugin_id", "version", "workspace")):
                raise ValueError
            stdio = grant["stdio"]
            if type(stdio) is not dict or set(stdio) != {"input_bytes", "frame_bytes"}:
                raise ValueError
            grants.append(SandboxPluginGrant(
                grant["plugin_id"], grant["version"], Path(grant["workspace"]),
                SandboxStdioLimits(**stdio), grant["max_processes"],
            ))
        if len({grant.plugin_id for grant in grants}) != len(grants):
            raise ValueError
        return SandboxHostSettings(policy, tuple(grants))
    except (TypeError, ValueError):
        raise ValueError("sandbox-host-config-invalid") from None


def load_sandbox_file(path: Path) -> SandboxHostSettings:
    try:
        with Path(path).open("rb") as source:
            content = source.read(MAX_CONFIG_BYTES + 1)
        if len(content) > MAX_CONFIG_BYTES:
            raise ValueError
        return parse_sandbox_config(json.loads(content))
    except (OSError, ValueError):
        raise ValueError("sandbox-host-config-invalid") from None
