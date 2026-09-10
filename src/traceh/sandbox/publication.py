"""Apply validated guest changes under the existing owner's workspace authority.

This is a sequence of atomic file operations, not an atomic directory transaction.
Failures report the exact completed operations; input bytes remain in the original
CAS. No recursive removal, automatic rollback or overwrite of a changed snapshot.
"""

from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from traceh.api.sandbox import SandboxPolicy, validate_relative_path, within_scope
from traceh.sandbox.workspace import WorkspaceSnapshot, _ordinary, protected, snapshot


@dataclass(frozen=True, slots=True)
class SandboxPublication:
    status: str
    applied: tuple[str, ...]
    failure_code: str | None = None


def publish(
    root: Path,
    policy: SandboxPolicy,
    before: WorkspaceSnapshot,
    after: WorkspaceSnapshot,
) -> SandboxPublication:
    applied: list[str] = []
    try:
        if snapshot(root, policy) != before:
            raise ValueError("sandbox-workspace-changed-before-publication")
        old_files = {f.path: f for f in before.files}
        new_files = {f.path: f for f in after.files}
        old_dirs, new_dirs = set(before.directories), set(after.directories)
        if len(new_files) != len(after.files) or len(new_dirs) != len(after.directories):
            raise ValueError("sandbox-publication-duplicate-path")
        removed_files = set(old_files) - set(new_files)
        removed_dirs = old_dirs - new_dirs
        added_dirs = new_dirs - old_dirs
        written = {name for name, item in new_files.items() if old_files.get(name) != item}
        all_names = list(new_files) + list(new_dirs)
        if len({name.casefold() for name in all_names}) != len(all_names):
            raise ValueError("sandbox-publication-path-alias")
        if (
            len(all_names) > policy.limits.workspace_files
            or sum(len(f.content) for f in after.files) > policy.limits.workspace_bytes
        ):
            raise ValueError("sandbox-publication-limit")
        changed = removed_files | removed_dirs | added_dirs | written
        for name in changed:
            validate_relative_path(name)
            if (
                protected(name)
                or within_scope(name, policy.excluded_paths)
                or not within_scope(name, policy.write_paths)
            ):
                raise ValueError("sandbox-publication-outside-write-scope")
            _target(root, name)
        for name in removed_dirs:
            # A protected/unselected child was never copied into the guest.
            # Its absence there does not authorize deletion on the host.
            for child in (root / name).iterdir():
                relative = child.relative_to(root).as_posix()
                if relative not in removed_dirs | removed_files:
                    raise ValueError("sandbox-publication-directory-not-empty")
        for name in sorted(removed_files):
            target = _target(root, name)
            target.unlink()
            applied.append("delete-file:" + name)
        for name in sorted(removed_dirs, key=lambda item: (-item.count("/"), item)):
            _target(root, name).rmdir()
            applied.append("delete-directory:" + name)
        for name in sorted(added_dirs, key=lambda item: (item.count("/"), item)):
            _target(root, name).mkdir()
            applied.append("create-directory:" + name)
        for name in sorted(written):
            target = _target(root, name)
            item = new_files[name]
            fd, temporary = tempfile.mkstemp(prefix=".traceh-write-", dir=target.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(item.content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temporary, 0o755 if item.executable else 0o644)
                os.replace(temporary, target)
                applied.append("write-file:" + name)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return SandboxPublication("completed", tuple(applied))
    except (OSError, ValueError) as error:
        code = str(error) if isinstance(error, ValueError) else "sandbox-publication-io-failed"
        return SandboxPublication("partial" if applied else "rejected", tuple(applied), code)


def _target(root: Path, name: str) -> Path:
    current = root
    for part in (*reversed(root.parents), root):
        if not stat.S_ISDIR(_ordinary(part).st_mode):
            raise ValueError("sandbox-publication-root-changed")
    for part in name.split("/"):
        current /= part
        if os.path.lexists(current):
            _ordinary(current)
    return current
