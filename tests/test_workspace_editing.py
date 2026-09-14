"""Managed editing identity, preimage, rollback and convergence contracts."""

import asyncio
import threading
from dataclasses import replace

import pytest
from test_workspace_policy import _attached

from traceh.api.workspace_edits import (
    FileEdit,
    FileImage,
    WorkspaceEditCancelled,
    WorkspaceEditPlan,
)
from traceh.api.workspaces import WorkspaceAccess
from traceh.workspaces import editing

pytestmark = pytest.mark.asyncio


async def world(tmp_path, access=WorkspaceAccess.WRITABLE):
    service, session_id, root = await _attached(tmp_path, access)
    record = (await service.catalog()).for_session(session_id)
    before = FileImage(b"before\n", "100644", "a" * 40)
    after = FileImage(b"after\n", "100644", "b" * 40)
    files = tuple(FileEdit(name, before, after) for name in ("one.txt", "two.txt"))
    for edit in files:
        (root / edit.path).write_bytes(edit.before.content)
    (root / "mine.txt").write_bytes(b"main existing changes")
    plan = WorkspaceEditPlan(
        "artifact",
        "c" * 64,
        "d" * 64,
        record.workspace_id,
        record.updated_seq,
        record.agent_id,
        session_id,
        record.source_id,
        record.base_revision,
        record.repository_fingerprint,
        files,
    )
    return service, session_id, root, plan


async def test_edit_and_repeated_attempt_preserve_unrelated_changes(tmp_path):
    service, session_id, root, plan = await world(tmp_path)
    first = await service.edit(plan, operation_id="same", session_id=session_id, workspace=root)
    assert first["outcome"] == "applied"
    second = await service.edit(plan, operation_id="same", session_id=session_id, workspace=root)
    assert second["outcome"] == "conflict"
    assert (root / "one.txt").read_bytes() == b"after\n"
    assert (root / "mine.txt").read_bytes() == b"main existing changes"


async def test_create_and_delete_are_explicit_images(tmp_path):
    service, session_id, root, plan = await world(tmp_path)
    absent = FileImage(None, "000000", None)
    files = (
        FileEdit("one.txt", plan.files[0].before, absent),
        FileEdit("nested/new.txt", absent, plan.files[0].after),
    )
    receipt = await service.edit(
        replace(plan, files=files), operation_id="op", session_id=session_id, workspace=root
    )
    assert receipt["outcome"] == "applied"
    assert not (root / "one.txt").exists()
    assert (root / "nested/new.txt").read_bytes() == b"after\n"


async def test_readonly_workspace_cannot_edit(tmp_path):
    service, session_id, root, plan = await world(tmp_path, WorkspaceAccess.READ_ONLY)
    from traceh.workspaces.errors import WorkspaceStateError

    with pytest.raises(WorkspaceStateError):
        await service.edit(plan, operation_id="op", session_id=session_id, workspace=root)
    assert (root / "one.txt").read_bytes() == b"before\n"


@pytest.mark.parametrize(
    "field,value",
    [
        ("agent_id", "other"),
        ("session_id", "other"),
        ("workspace_id", "other"),
        ("workspace_generation", 999),
        ("source_id", "other"),
        ("repository_fingerprint", "f" * 64),
        ("base_revision", "f" * 40),
    ],
)
async def test_edit_binding_rejected_before_any_file_changes(tmp_path, field, value):
    service, session_id, root, plan = await world(tmp_path)
    from traceh.workspaces.errors import WorkspaceStateError

    with pytest.raises(WorkspaceStateError):
        await service.edit(
            replace(plan, **{field: value}),
            operation_id="op",
            session_id=session_id,
            workspace=root,
        )
    assert (root / "one.txt").read_bytes() == b"before\n"


async def test_all_preimages_are_checked_before_first_publish(tmp_path):
    service, session_id, root, plan = await world(tmp_path)
    (root / "two.txt").write_bytes(b"main edits")
    receipt = await service.edit(plan, operation_id="op", session_id=session_id, workspace=root)
    assert receipt["outcome"] == "conflict"
    assert (root / "one.txt").read_bytes() == b"before\n"
    assert (root / "two.txt").read_bytes() == b"main edits"


async def test_line_ending_drift_is_not_silently_normalized(tmp_path):
    service, session_id, root, plan = await world(tmp_path)
    (root / "one.txt").write_bytes(b"before\r\n")
    receipt = await service.edit(plan, operation_id="op", session_id=session_id, workspace=root)
    assert receipt["outcome"] == "conflict"
    assert (root / "one.txt").read_bytes() == b"before\r\n"


@pytest.mark.parametrize("rollback", ["normal", "conflict", "failure"])
async def test_partial_failure_reconciles_actual_images(tmp_path, monkeypatch, rollback):
    service, session_id, root, plan = await world(tmp_path)
    original = editing.publish
    published = []

    def publish(root, edit, expected, replacement):
        if edit.path == "two.txt":
            if rollback == "conflict":
                (root / "one.txt").write_bytes(b"concurrent main edit")
            raise OSError("second publication failed")
        if replacement == edit.before and rollback == "failure":
            raise OSError("rollback failed")
        original(root, edit, expected, replacement)
        published.append(replacement.content)

    monkeypatch.setattr(editing, "publish", publish)
    receipt = await service.edit(plan, operation_id="op", session_id=session_id, workspace=root)
    assert published[0] == b"after\n"
    assert receipt["outcome"] == "failed"
    expected = {
        "normal": ("rolled_back", b"before\n"),
        "conflict": ("conflict", b"concurrent main edit"),
        "failure": ("unknown", b"after\n"),
    }[rollback]
    assert receipt["files"][0]["status"] == expected[0]
    assert (root / "one.txt").read_bytes() == expected[1]
    assert (root / "two.txt").read_bytes() == b"before\n"


async def test_repeated_cancel_waits_for_publication_and_rollback(tmp_path, monkeypatch):
    service, session_id, root, plan = await world(tmp_path)
    entered, converging = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = editing.publish
    import traceh.workspaces.service as service_module

    original_converge = service_module.await_worker_convergence

    def publish(root, edit, expected, replacement):
        original(root, edit, expected, replacement)
        if edit.path == "one.txt" and replacement == edit.after:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(15)

    async def converge(task):
        converging.set()
        await original_converge(task)

    monkeypatch.setattr(editing, "publish", publish)
    monkeypatch.setattr(service_module, "await_worker_convergence", converge)
    task = asyncio.create_task(
        service.edit(plan, operation_id="cancel", session_id=session_id, workspace=root)
    )
    try:
        await asyncio.wait_for(entered.wait(), 10)
        task.cancel()
        await asyncio.wait_for(converging.wait(), 10)
        task.cancel()
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(WorkspaceEditCancelled) as caught:
            await task
    assert caught.value.receipt["files"][0]["status"] == "rolled_back"
    assert (root / "one.txt").read_bytes() == b"before\n"
    assert (root / "two.txt").read_bytes() == b"before\n"
