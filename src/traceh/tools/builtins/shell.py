from __future__ import annotations

import asyncio
import os
import shlex
from dataclasses import dataclass, field

from traceh.api.json_types import JsonValue
from traceh.api.tools import EffectKind, ToolExecutionContext, ToolOutput


_SENSITIVE_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "AUTH")


def sanitized_environment() -> dict[str, str]:
    safe: dict[str, str] = {}
    allowed_exact = {"PATH", "HOME", "USER", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "PYTHONPATH"}
    for key, value in os.environ.items():
        upper = key.upper()
        if any(marker in upper for marker in _SENSITIVE_MARKERS):
            continue
        if key in allowed_exact or key.startswith("LC_"):
            safe[key] = value
    safe["TRACEH_CHILD_PROCESS"] = "1"
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
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=context.workspace,
            env=sanitized_environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        timed_out = False
        try:
            async with asyncio.timeout(timeout):
                stdout_bytes, stderr_bytes = await process.communicate()
        except TimeoutError:
            timed_out = True
            process.kill()
            stdout_bytes, stderr_bytes = await process.communicate()
        except asyncio.CancelledError:
            process.terminate()
            try:
                async with asyncio.timeout(2.0):
                    await process.wait()
            except TimeoutError:
                process.kill()
                await process.wait()
            raise

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
