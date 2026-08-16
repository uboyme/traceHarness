from __future__ import annotations

from dataclasses import dataclass, field

from traceh.api.json_types import JsonValue
from traceh.api.tools import EffectKind, ToolExecutionContext, ToolOutput


@dataclass(slots=True)
class ListFilesTool:
    name: str = "list_files"
    description: str = "List files under the workspace using relative paths."
    effect_kind: EffectKind = EffectKind.WORKSPACE_READ
    input_schema: dict[str, JsonValue] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.input_schema = {
            "type": "object",
            "properties": {
                "max_files": {"type": "integer"},
            },
            "additionalProperties": False,
        }

    async def execute(self, arguments, context: ToolExecutionContext) -> ToolOutput:
        max_files = int(arguments.get("max_files", 500))
        max_files = max(1, min(max_files, 5000))
        ignored = {".git", ".traceh", "__pycache__", ".pytest_cache", ".venv", "node_modules"}
        files: list[str] = []
        for path in sorted(context.workspace.rglob("*")):
            relative = path.relative_to(context.workspace)
            if any(part in ignored for part in relative.parts):
                continue
            if path.is_file():
                files.append(relative.as_posix())
                if len(files) >= max_files:
                    break
        content = "\n".join(files) if files else "<workspace contains no files>"
        return ToolOutput(content, {"files": files, "truncated": len(files) >= max_files})
