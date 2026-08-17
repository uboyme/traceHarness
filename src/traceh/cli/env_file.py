"""Small, dependency-free ``.env`` file loader for CLI configuration."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from traceh.cli.errors import CliConfigurationError


class EnvFileError(ValueError):
    pass


_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def is_env_var_name(value: str) -> bool:
    """Whether `value` is a usable environment variable name."""

    return bool(_ENV_NAME.match(value))


def validate_env_var_name(value: str, *, setting: str) -> str:
    """Return `value` if it names an environment variable, else fail loudly.

    Shared by the `.env` parser and by CLI/environment configuration so that one
    rule decides what a variable name is. Validation happens before a runtime or
    session is built: a name that cannot be looked up will not find a key, and
    the previous behaviour - accept it, then quietly drop it from the resume
    command - meant the next run silently fell back to a different variable.

    **The rejected value is never reported.** Escaping it was not enough:
    escaping defends against control characters, not against a printable secret.
    The usual way this setting is got wrong is by pasting the key itself instead
    of the name of the variable holding it - so the invalid value is exactly the
    thing that must not be echoed. Nothing derived from it is shown either: no
    length, no prefix or suffix, no hash, since each of those narrows a guess.
    The message names the setting and the required format, which is what the user
    needs in order to fix it.
    """

    if is_env_var_name(value):
        return value
    raise CliConfigurationError(
        f"{setting} must be the NAME of an environment variable "
        "(letters, digits and underscore, not starting with a digit) - not the "
        "key itself. The supplied value is invalid and is not shown, because a "
        "wrong value here is often a secret."
    )


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
            # The name is not echoed either: a malformed left-hand side can carry
            # a pasted secret or terminal control characters just as easily as
            # the value can. The line number is enough to find it.
            raise EnvFileError(
                f"line {line_number}: invalid environment variable name "
                "(letters, digits and underscore, not starting with a digit); "
                "the offending text is not shown"
            )
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
