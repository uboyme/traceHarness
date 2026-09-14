"""Exact file publication and compensating rollback under WorkspaceService ownership."""

import asyncio
import os
import stat
import tempfile

from traceh.tools.builtins.paths import resolve_workspace_path


def matches(root, edit, image):
    path = resolve_workspace_path(root, edit.path, must_exist=False)
    if image.content is None:
        return not os.path.lexists(path)
    if not path.is_file() or path.read_bytes() != image.content:
        return False
    if os.name != "nt":
        mode = "100755" if path.stat().st_mode & stat.S_IXUSR else "100644"
        return mode == image.mode
    return True


def publish(root, edit, expected, replacement):
    if not matches(root, edit, expected):
        raise ValueError("workspace-edit-preimage-conflict")
    path = resolve_workspace_path(root, edit.path, must_exist=False)
    if replacement.content is None:
        path.unlink()
        return
    permissions = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    if replacement.mode == "100755":
        permissions |= stat.S_IXUSR
    else:
        permissions &= ~(stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the existing path/reparse check after parent creation as well.
    path = resolve_workspace_path(root, edit.path, must_exist=False)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(replacement.content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, permissions)
        if not matches(root, edit, expected):
            raise ValueError("workspace-edit-preimage-conflict")
        os.replace(name, path)
    finally:
        if os.path.lexists(name):
            os.unlink(name)


async def apply_files(plan, root, operation_id, stop):
    receipt = {
        "format": 1,
        "operation_id": operation_id,
        "request_digest": plan.digest,
        "request": plan.data(),
        "outcome": "unchanged",
        "files": [{"path": edit.path, "status": "unchanged"} for edit in plan.files],
    }
    attempted = []
    try:
        for edit in plan.files:
            if (
                os.name == "nt"
                and edit.before.mode != edit.after.mode
                and "100755" in {edit.before.mode, edit.after.mode}
            ):
                raise ValueError("workspace-edit-mode-unsupported")
            if not matches(root, edit, edit.before):
                receipt["outcome"] = "conflict"
                next(r for r in receipt["files"] if r["path"] == edit.path)["status"] = "conflict"
                return receipt
        for index, edit in enumerate(plan.files):
            if stop.is_set():
                raise asyncio.CancelledError
            # Register before invoking: a failed publisher may already have
            # replaced the file. Reconciliation must inspect that exact path.
            attempted.append((index, edit))
            await asyncio.to_thread(publish, root, edit, edit.before, edit.after)
            receipt["files"][index]["status"] = "applied"
        if stop.is_set():
            raise asyncio.CancelledError
        receipt["outcome"] = "applied"
        return receipt
    except (Exception, asyncio.CancelledError) as error:
        receipt["outcome"] = "cancelled" if stop.is_set() else "failed"
        receipt["failure_type"] = type(error).__name__
        for index, edit in reversed(attempted):
            row = receipt["files"][index]
            try:
                if matches(root, edit, edit.before):
                    row["status"] = "unchanged"
                elif matches(root, edit, edit.after):
                    await asyncio.to_thread(publish, root, edit, edit.after, edit.before)
                    row["status"] = "rolled_back"
                else:
                    row["status"] = "conflict"
            except Exception:
                row["status"] = "unknown"
        return receipt
