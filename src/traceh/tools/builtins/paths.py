"""Workspace path safety helpers."""

from __future__ import annotations

import os
import stat
from pathlib import Path

_CONTROL_PATH_NAMES = frozenset({".git", ".traceh"})


class WorkspaceBoundaryError(PermissionError):
    code = "workspace-path-rejected"

    def __init__(self) -> None:
        super().__init__("the requested path is outside the managed workspace policy")


def _is_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(os.path, "isjunction", None)
        if is_junction is not None and is_junction(path):
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        return bool(attributes & marker)
    except OSError:
        return True


def resolve_workspace_path(workspace: Path, requested: str, *, must_exist: bool = True) -> Path:
    if type(requested) is not str or not requested:
        raise WorkspaceBoundaryError
    try:
        relative = Path(requested)
        parts = relative.parts
    except Exception:
        raise WorkspaceBoundaryError from None
    if (
        relative.is_absolute()
        or (not parts and requested != ".")
        or any(
            part in {"", ".", ".."} or part.casefold() in _CONTROL_PATH_NAMES
            for part in parts
        )
        or _is_reparse(workspace)
    ):
        raise WorkspaceBoundaryError
    try:
        root = workspace.resolve(strict=True)
    except (OSError, RuntimeError):
        raise WorkspaceBoundaryError from None
    unresolved = root / relative
    current = root
    for part in parts:
        current /= part
        if os.path.lexists(current) and _is_reparse(current):
            raise WorkspaceBoundaryError
    try:
        candidate = unresolved.resolve(strict=must_exist)
    except (OSError, RuntimeError):
        raise WorkspaceBoundaryError from None
    try:
        candidate.relative_to(root)
    except ValueError:
        raise WorkspaceBoundaryError from None
    return candidate
