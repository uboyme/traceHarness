from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass, field

from traceh.api.json_types import JsonValue
from traceh.api.tools import EffectKind, ToolExecutionContext, ToolOutput
from traceh.tools.builtins.paths import resolve_workspace_path


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class ApplyPatchTool:
    name: str = "apply_patch"
    description: str = (
        "Replace exact text in one workspace file. For a new file, use old_text='' and create=true."
    )
    effect_kind: EffectKind = EffectKind.WORKSPACE_WRITE
    input_schema: dict[str, JsonValue] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.input_schema = {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
                "expected_replacements": {"type": "integer"},
                "create": {"type": "boolean"},
            },
            "required": ["path", "old_text", "new_text"],
            "additionalProperties": False,
        }

    async def execute(self, arguments, context: ToolExecutionContext) -> ToolOutput:
        relative = str(arguments["path"])
        create = bool(arguments.get("create", False))
        path = resolve_workspace_path(context.workspace, relative, must_exist=not create)
        if create and path.exists():
            raise FileExistsError(f"refusing to create existing file: {relative}")
        if create:
            old_content = ""
        else:
            if not path.is_file():
                raise IsADirectoryError(path)
            old_content = path.read_text(encoding="utf-8")

        old_text = str(arguments["old_text"])
        new_text = str(arguments["new_text"])
        expected = int(arguments.get("expected_replacements", 1))
        if create:
            if old_text:
                raise ValueError("old_text must be empty when create=true")
            new_content = new_text
            replacements = 1
        else:
            replacements = old_content.count(old_text)
            if replacements != expected:
                raise ValueError(
                    f"expected {expected} replacement(s), found {replacements}; file was not changed"
                )
            new_content = old_content.replace(old_text, new_text, expected)

        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(new_content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

        before_hash = _sha256(old_content)
        after_hash = _sha256(new_content)
        relative_path = path.relative_to(context.workspace).as_posix()
        return ToolOutput(
            f"Updated {relative_path}: {replacements} replacement(s).",
            {
                "path": relative_path,
                "replacements": replacements,
                "before_sha256": before_hash,
                "after_sha256": after_hash,
            },
            evidence=(f"file-sha256:{relative_path}:{after_hash}",),
        )
