from __future__ import annotations

from pathlib import Path

import pytest

from traceh.api.tools import ToolExecutionContext
from traceh.tools.builtins.apply_patch import ApplyPatchTool
from traceh.tools.builtins.paths import WorkspaceBoundaryError, resolve_workspace_path
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


@pytest.mark.parametrize("requested", (".git/config", ".GIT/config", ".traceh/log"))
def test_workspace_control_paths_are_rejected_without_echo(
    tmp_path: Path, requested: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(WorkspaceBoundaryError) as raised:
        resolve_workspace_path(workspace, requested, must_exist=False)
    assert requested not in str(raised.value)


def test_workspace_root_remains_a_valid_search_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert resolve_workspace_path(workspace, ".") == workspace


def test_workspace_symlink_component_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    link = workspace / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")
    with pytest.raises(WorkspaceBoundaryError):
        resolve_workspace_path(workspace, "linked/secret.txt")


def test_sanitized_environment_removes_secret_names(monkeypatch) -> None:
    monkeypatch.setenv("MY_API_KEY", "secret")
    monkeypatch.setenv("PATH", "/bin")
    env = sanitized_environment()
    assert "MY_API_KEY" not in env
    assert env["PATH"] == "/bin"
