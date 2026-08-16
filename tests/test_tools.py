from __future__ import annotations

from pathlib import Path

import pytest

from traceh.api.tools import ToolExecutionContext
from traceh.tools.builtins.apply_patch import ApplyPatchTool
from traceh.tools.builtins.paths import WorkspaceBoundaryError
from traceh.tools.builtins.read_file import ReadFileTool
from traceh.tools.builtins.shell import sanitized_environment


def context(workspace: Path, tmp_path: Path) -> ToolExecutionContext:
    return ToolExecutionContext("s", "t", "p", "c", workspace, tmp_path)


@pytest.mark.asyncio
async def test_apply_patch_is_exact_and_atomic(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    file = workspace / "a.py"
    file.write_text("value = 1\n", encoding="utf-8")
    output = await ApplyPatchTool().execute(
        {
            "path": "a.py",
            "old_text": "value = 1\n",
            "new_text": "value = 2\n",
            "expected_replacements": 1,
        },
        context(workspace, tmp_path),
    )
    assert file.read_text(encoding="utf-8") == "value = 2\n"
    assert output.data["replacements"] == 1
    assert output.evidence


@pytest.mark.asyncio
async def test_workspace_escape_is_rejected(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    with pytest.raises(WorkspaceBoundaryError):
        await ReadFileTool().execute(
            {"path": "../outside.txt"},
            context(workspace, tmp_path),
        )


def test_sanitized_environment_removes_secret_names(monkeypatch) -> None:
    monkeypatch.setenv("MY_API_KEY", "secret")
    monkeypatch.setenv("PATH", "/bin")
    env = sanitized_environment()
    assert "MY_API_KEY" not in env
    assert env["PATH"] == "/bin"
