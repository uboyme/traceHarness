from __future__ import annotations

import asyncio
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from traceh.api.json_types import JsonValue
from traceh.api.tools import EffectKind, ToolExecutionContext, ToolOutput
from traceh.process_control import converge_process
from traceh.tools.process_control import capture_output

_SENSITIVE_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "AUTH")

# Windows loads Winsock, and therefore anything importing `asyncio`, relative to
# SystemRoot. Without these a child dies with WinError 10106 before running a
# single line, so a verifier command as ordinary as `python -m pytest` cannot
# start. They describe the machine, not the user, and the sensitive-marker
# filter below still applies.
_WINDOWS_ESSENTIALS = frozenset(
    {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "SYSTEMDRIVE", "PROCESSOR_ARCHITECTURE"}
)


def _canonical_local_directory_uri(value: str) -> str | None:
    """Accept exactly one canonical local ``file://`` directory URI."""

    if any(character.isspace() for character in value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if (
        parsed.scheme != "file"
        or parsed.netloc
        or parsed.query
        or parsed.fragment
    ):
        return None
    try:
        path = Path(url2pathname(parsed.path))
    except (TypeError, ValueError):
        return None
    if not path.is_absolute() or not path.is_dir():
        return None
    canonical = path.resolve().as_uri()
    if value != canonical:
        return None
    return canonical


def sanitized_environment() -> dict[str, str]:
    safe: dict[str, str] = {}
    allowed_exact = {
        "PATH",
        "HOME",
        "USER",
        "TMPDIR",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
        "PYTHONPATH",
    } | _WINDOWS_ESSENTIALS
    for key, value in os.environ.items():
        upper = key.upper()
        if any(marker in upper for marker in _SENSITIVE_MARKERS):
            continue
        if key in allowed_exact or key.startswith("LC_"):
            safe[key] = value
    # Candidate validation/comparison may deliberately confine every nested
    # pip invocation to one local Wheel directory. pip splits PIP_FIND_LINKS on
    # whitespace, so only the canonical, percent-encoded file URI produced by
    # the host is safe to preserve as a single source. Raw paths, remote hosts,
    # multiple values and arbitrary pip configuration remain excluded.
    wheelhouse = os.environ.get("PIP_FIND_LINKS")
    canonical_wheelhouse = (
        _canonical_local_directory_uri(wheelhouse) if wheelhouse else None
    )
    if os.environ.get("PIP_NO_INDEX") == "1" and canonical_wheelhouse:
        safe["PIP_NO_INDEX"] = "1"
        safe["PIP_FIND_LINKS"] = canonical_wheelhouse
    safe["TRACEH_CHILD_PROCESS"] = "1"
    # Output is captured as bytes and decoded as UTF-8. A Windows Python child
    # would otherwise encode its stdout with the system code page (CP936 on a
    # Chinese install), and every non-ASCII character would arrive as U+FFFD.
    # This settles the encoding for Python children only; a native tool still
    # follows whatever the console code page says.
    safe["PYTHONUTF8"] = "1"
    safe["PYTHONIOENCODING"] = "utf-8"
    return safe


@dataclass(slots=True)
class ShellTool:
    name: str = "shell"
    description: str = "Run a non-shell command in the workspace and capture stdout/stderr."
    effect_kind: EffectKind = EffectKind.PROCESS
    input_schema: dict[str, JsonValue] = field(init=False, repr=False)
    default_timeout: float = 30.0

    def __post_init__(self) -> None:
        self.input_schema = {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "number"},
            },
            "required": ["command"],
            "additionalProperties": False,
        }

    async def execute(self, arguments, context: ToolExecutionContext) -> ToolOutput:
        command = str(arguments["command"])
        argv = shlex.split(command)
        if not argv:
            raise ValueError("empty command")
        timeout = float(arguments.get("timeout", self.default_timeout))
        timeout = max(0.1, min(timeout, 300.0))
        # The captured files are the one owner of this command's output, so
        # whatever it flushed survives a timeout or a cancellation.
        with capture_output() as capture:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=context.workspace,
                env=sanitized_environment(),
                stdout=capture.stdout,
                stderr=capture.stderr,
            )
            timed_out = False
            try:
                async with asyncio.timeout(timeout):
                    await process.wait()
            except TimeoutError:
                timed_out = True
                # Converge first, then read: a cancellation arriving during the
                # shutdown is absorbed and only re-raised once the child is gone.
                if await converge_process(process):
                    raise asyncio.CancelledError from None
            except asyncio.CancelledError:
                await converge_process(process)
                raise
            stdout_bytes, stderr_bytes = capture.read()

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        result = {
            "command": command,
            "argv": argv,
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "stdout": stdout,
            "stderr": stderr,
        }
        content = (
            f"exit_code={process.returncode} timed_out={str(timed_out).lower()}\n"
            f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
        )
        if timed_out:
            raise TimeoutError(content)
        return ToolOutput(content, result, evidence=(f"process-exit:{process.returncode}",))
