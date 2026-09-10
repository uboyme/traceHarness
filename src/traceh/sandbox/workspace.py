"""Bounded regular-file snapshots; the guest never mounts a host workspace."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from traceh.api.sandbox import SandboxPolicy, validate_relative_path, within_scope


@dataclass(frozen=True, slots=True)
class SandboxFile:
    path: str
    content: bytes
    executable: bool

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    files: tuple[SandboxFile, ...]
    directories: tuple[str, ...]


def protected(path: str) -> bool:
    return any(
        part.casefold() in {".git", ".traceh", ".ssh", ".aws"} or part.casefold().startswith(".env")
        for part in path.split("/")
    )


def _ordinary(path: Path) -> os.stat_result:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("sandbox-reparse-path-refused")
    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        raise ValueError("sandbox-special-file-refused")
    if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
        raise ValueError("sandbox-hardlink-refused")
    return info


def snapshot(root: Path, policy: SandboxPolicy) -> WorkspaceSnapshot:
    """Read only host-authorized paths, refusing aliases before reading bytes.

    Host peers and trusted plugins are not adversaries of this boundary. A
    changing ordinary file still fails the before/after identity check; this
    function does not advertise a cross-process filesystem transaction.
    """

    if not root.is_absolute():
        raise ValueError("sandbox-workspace-must-be-absolute")
    for parent in (*reversed(root.parents), root):
        if not stat.S_ISDIR(_ordinary(parent).st_mode):
            raise ValueError("sandbox-workspace-not-directory")
    files: list[SandboxFile] = []
    directories: list[str] = []
    total = 0
    nodes = 0
    names: set[str] = set()

    def visit(directory: Path) -> None:
        nonlocal total, nodes
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                name = path.relative_to(root).as_posix()
                # An omitted subtree is never opened, even if it is a link.
                if protected(name) or within_scope(name, policy.excluded_paths):
                    continue
                selected = within_scope(name, policy.read_paths)
                ancestor = any(scope.startswith(name + "/") for scope in policy.read_paths)
                if not selected and not ancestor:
                    continue
                validate_relative_path(name)
                nodes += 1
                if nodes > policy.limits.workspace_files:
                    raise ValueError("sandbox-workspace-file-limit")
                if name.casefold() in names:
                    raise ValueError("sandbox-path-alias-refused")
                names.add(name.casefold())
                before = _ordinary(path)
                if stat.S_ISDIR(before.st_mode):
                    directories.append(name)
                    visit(path)
                    continue
                if not selected:
                    raise ValueError("sandbox-read-scope-not-directory")
                remaining = policy.limits.workspace_bytes - total
                if before.st_size > remaining:
                    raise ValueError("sandbox-workspace-byte-limit")
                with path.open("rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                        raise ValueError("sandbox-workspace-changed")
                    content = stream.read(remaining + 1)
                after = _ordinary(path)
                if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                ) or len(content) != before.st_size:
                    raise ValueError("sandbox-workspace-changed")
                total += len(content)
                files.append(SandboxFile(name, content, bool(before.st_mode & stat.S_IXUSR)))

    visit(root)
    for selected in policy.read_paths:
        if selected != "." and (protected(selected) or not (root / selected).exists()):
            raise ValueError("sandbox-read-scope-unavailable")
    return WorkspaceSnapshot(
        tuple(sorted(files, key=lambda item: item.path)), tuple(sorted(directories))
    )
