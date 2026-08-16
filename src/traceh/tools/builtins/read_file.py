from __future__ import annotations

from dataclasses import dataclass, field

from traceh.api.json_types import JsonValue
from traceh.api.tools import EffectKind, ToolExecutionContext, ToolOutput
from traceh.tools.builtins.paths import resolve_workspace_path


@dataclass(slots=True)
class ReadFileTool:
    name: str = "read_file"
    description: str = "Read a UTF-8 text file from the workspace."
    effect_kind: EffectKind = EffectKind.WORKSPACE_READ
    input_schema: dict[str, JsonValue] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.input_schema = {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        }

    async def execute(self, arguments, context: ToolExecutionContext) -> ToolOutput:
        path = resolve_workspace_path(context.workspace, str(arguments["path"]))
        if not path.is_file():
            raise IsADirectoryError(path)
        content = path.read_text(encoding="utf-8")
        return ToolOutput(content, {"path": path.relative_to(context.workspace).as_posix()})
