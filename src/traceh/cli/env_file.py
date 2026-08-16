"""Small, dependency-free ``.env`` file loader for CLI configuration."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path


class EnvFileError(ValueError):
    pass


_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


@dataclass(frozen=True, slots=True)
class EnvLoadReport:
    path: Path | None
    loaded: bool
    applied_keys: tuple[str, ...] = ()


def _parse_quoted_value(raw: str, *, line_number: int) -> str:
    quote = raw[0]
    if quote == '"':
        escaped = False
        end = None
        for index, character in enumerate(raw[1:], start=1):
            if character == '"' and not escaped:
                end = index
                break
            escaped = character == "\\" and not escaped
            if character != "\\":
                escaped = False
        if end is None:
            raise EnvFileError(f"line {line_number}: unterminated double-quoted value")
        encoded = raw[: end + 1]
        try:
            value = json.loads(encoded)
        except json.JSONDecodeError as error:
            raise EnvFileError(f"line {line_number}: invalid quoted value: {error.msg}") from error
    else:
        end = raw.find("'", 1)
        if end < 0:
            raise EnvFileError(f"line {line_number}: unterminated single-quoted value")
        value = raw[1:end]

    trailing = raw[end + 1 :].strip()
    if trailing and not trailing.startswith("#"):
        raise EnvFileError(f"line {line_number}: unexpected text after quoted value")
    return value


def _parse_unquoted_value(raw: str) -> str:
    for index, character in enumerate(raw):
        if character == "#" and (index == 0 or raw[index - 1].isspace()):
            return raw[:index].rstrip()
    return raw.strip()


def parse_env_file(text: str) -> dict[str, str]:
    """Parse the conservative subset of dotenv syntax needed by TraceHarness."""

    values: dict[str, str] = {}
    for line_number, source_line in enumerate(text.lstrip("\ufeff").splitlines(), start=1):
        line = source_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        if "=" not in line:
            raise EnvFileError(f"line {line_number}: expected NAME=VALUE")
        raw_name, raw_value = line.split("=", 1)
        name = raw_name.strip()
        if not _ENV_NAME.fullmatch(name):
            raise EnvFileError(f"line {line_number}: invalid environment variable name {name!r}")
        value_source = raw_value.strip()
        value = (
            _parse_quoted_value(value_source, line_number=line_number)
            if value_source.startswith(("'", '"'))
            else _parse_unquoted_value(value_source)
        )
        values[name] = value
    return values


def load_env_file(path: Path | None, *, override: bool = False) -> EnvLoadReport:
    """Load a dotenv file without replacing existing process variables by default."""

    if path is None:
        return EnvLoadReport(None, False)
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return EnvLoadReport(resolved, False)
    if not resolved.is_file():
        raise EnvFileError(f"environment file is not a regular file: {resolved}")
    values = parse_env_file(resolved.read_text(encoding="utf-8"))
    applied: list[str] = []
    for name, value in values.items():
        if override or name not in os.environ:
            os.environ[name] = value
            applied.append(name)
    return EnvLoadReport(resolved, True, tuple(applied))
