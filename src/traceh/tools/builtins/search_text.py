from __future__ import annotations

import re
from dataclasses import dataclass, field

from traceh.api.json_types import JsonValue
from traceh.api.tools import EffectKind, ToolExecutionContext, ToolOutput
from traceh.tools.builtins.paths import resolve_workspace_path


@dataclass(slots=True)
class SearchTextTool:
    name: str = "search_text"
    description: str = "Search UTF-8 files for a substring or regular expression."
    effect_kind: EffectKind = EffectKind.WORKSPACE_READ
    input_schema: dict[str, JsonValue] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.input_schema = {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "path": {"type": "string"},
                "regex": {"type": "boolean"},
                "max_results": {"type": "integer"},
            },
            "required": ["query"],
            "additionalProperties": False,
        }

    async def execute(self, arguments, context: ToolExecutionContext) -> ToolOutput:
        query = str(arguments["query"])
        regex = bool(arguments.get("regex", False))
        max_results = max(1, min(int(arguments.get("max_results", 100)), 1000))
        root = resolve_workspace_path(
            context.workspace,
            str(arguments.get("path", ".")),
            must_exist=True,
        )
        pattern = re.compile(query) if regex else None
        candidates = [root] if root.is_file() else sorted(root.rglob("*"))
        matches: list[dict[str, JsonValue]] = []
        ignored = {".git", ".traceh", "__pycache__", ".venv", "node_modules"}
        for path in candidates:
            if len(matches) >= max_results:
                break
            relative = path.relative_to(context.workspace)
            if not path.is_file() or any(part in ignored for part in relative.parts):
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for line_number, line in enumerate(lines, start=1):
                found = bool(pattern.search(line)) if pattern else query in line
                if found:
                    matches.append(
                        {
                            "path": path.relative_to(context.workspace).as_posix(),
                            "line": line_number,
                            "text": line,
                        }
                    )
                    if len(matches) >= max_results:
                        break
        content = "\n".join(
            f"{item['path']}:{item['line']}: {item['text']}" for item in matches
        ) or "<no matches>"
        return ToolOutput(content, {"matches": matches, "truncated": len(matches) >= max_results})
