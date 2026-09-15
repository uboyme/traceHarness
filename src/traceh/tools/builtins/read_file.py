from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from traceh.api.json_types import JsonValue, canonical_json
from traceh.api.tools import EffectKind, ToolExecutionContext, ToolOutput
from traceh.concurrency import await_worker_convergence
from traceh.tools.builtins.file_page import (
    MAX_FILE_PAGE_CHARS,
    MAX_FILE_PAGE_LINES,
    read_options,
    render_file_outline,
    render_file_page,
)
from traceh.tools.builtins.paths import resolve_workspace_path


@dataclass(slots=True)
class ReadFileTool:
    name: str = "read_file"
    description: str = (
        "Read a UTF-8 workspace file with real 1-based line numbers. "
        'mode="outline" returns only this file\'s definition and heading lines with their '
        "line numbers and no bodies, which is the cheap way to learn what a file contains "
        "and where; follow it with a normal read of the exact range you need. Surveying "
        "many files body-first is what exhausts a context. "
        'mode="page" (the default) returns real content. '
        "Use start_line/end_line (inclusive) to inspect nearby code after a search hit. "
        f"Each page has at most {MAX_FILE_PAGE_LINES} lines "
        f"and {MAX_FILE_PAGE_CHARS} JSON characters; "
        "path alone reads the first page, not necessarily the whole file. "
        "If needed, copy next_read for the next page. Long lines continue at start_column "
        "(1-based Unicode characters); source_sha256 detects changes between reads. "
        "truncated means the requested range was not fully returned; eof means file end. "
        "When a requested range ends before EOF, next_read starts after that range. "
        "A source-changed error requires a fresh read; do not combine different versions."
    )
    effect_kind: EffectKind = EffectKind.WORKSPACE_READ
    input_schema: dict[str, JsonValue] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.input_schema = {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "mode": {"type": "string", "enum": ["page", "outline"]},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
                "start_column": {"type": "integer", "minimum": 1},
                "source_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            },
            "required": ["path"],
            "additionalProperties": False,
        }

    async def execute(self, arguments, context: ToolExecutionContext) -> ToolOutput:
        mode = arguments.get("mode", "page")
        options = read_options(arguments)
        if mode == "outline":
            if "end_line" in arguments or "start_column" in arguments:
                raise ValueError("file-read-outline-range-invalid")
            options = {
                "start_line": options["start_line"],
                "source_sha256": options["source_sha256"],
            }
        worker = asyncio.create_task(
            asyncio.to_thread(self._read, context.workspace, arguments["path"], options, mode)
        )
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError as cancelled:
            await await_worker_convergence(worker)
            raise cancelled from (None if worker.cancelled() else worker.exception())

    @staticmethod
    def _read(workspace, requested, options, mode="page"):
        path = resolve_workspace_path(workspace, requested)
        if not path.is_file():
            raise IsADirectoryError(path)
        relative = path.relative_to(workspace.resolve()).as_posix()
        render = render_file_outline if mode == "outline" else render_file_page
        page = render(relative, path.read_bytes(), **options)
        return ToolOutput(
            canonical_json(page), {key: value for key, value in page.items() if key != "text"}
        )
