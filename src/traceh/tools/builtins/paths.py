"""Workspace path safety helpers."""

from __future__ import annotations

from pathlib import Path


class WorkspaceBoundaryError(PermissionError):
    pass


def resolve_workspace_path(workspace: Path, requested: str, *, must_exist: bool = True) -> Path:
    root = workspace.resolve(strict=True)
    candidate = (root / requested).resolve(strict=must_exist)
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise WorkspaceBoundaryError(f"path escapes workspace: {requested}") from error
    return candidate
