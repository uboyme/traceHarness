from dataclasses import replace

from test_sandbox_contract import policy

from traceh.sandbox.publication import publish
from traceh.sandbox.workspace import SandboxFile, WorkspaceSnapshot, snapshot


def test_publish_edits_and_empty_directories(tmp_path):
    (tmp_path / "old").write_bytes(b"before")
    before = snapshot(tmp_path, policy())
    after = WorkspaceSnapshot((SandboxFile("new/value", b"after", False),), ("empty", "new"))
    result = publish(tmp_path, policy(), before, after)
    assert result.status == "completed"
    assert not (tmp_path / "old").exists()
    assert (tmp_path / "new/value").read_bytes() == b"after"
    assert (tmp_path / "empty").is_dir()


def test_publish_rejects_conflicting_host_edit_before_any_write(tmp_path):
    (tmp_path / "value").write_bytes(b"before")
    before = snapshot(tmp_path, policy())
    (tmp_path / "value").write_bytes(b"other writer")
    after = WorkspaceSnapshot((SandboxFile("value", b"guest", False),), ())
    result = publish(tmp_path, policy(), before, after)
    assert result.status == "rejected" and not result.applied
    assert (tmp_path / "value").read_bytes() == b"other writer"


def test_publish_does_not_widen_write_scope(tmp_path):
    (tmp_path / "allowed").mkdir()
    rules = replace(policy(), write_paths=("allowed",))
    before = snapshot(tmp_path, rules)
    after = WorkspaceSnapshot((SandboxFile("outside", b"no", False),), ("allowed",))
    result = publish(tmp_path, rules, before, after)
    assert result.status == "rejected" and not (tmp_path / "outside").exists()


def test_publish_io_failure_reports_completed_operations(tmp_path, monkeypatch):
    import os

    (tmp_path / "old").write_bytes(b"before")
    before = snapshot(tmp_path, policy())
    after = WorkspaceSnapshot((SandboxFile("new", b"after", False),), ())

    def fail_replace(*args):
        raise OSError("injected disk error")

    monkeypatch.setattr(os, "replace", fail_replace)
    result = publish(tmp_path, policy(), before, after)
    assert result.status == "partial" and result.applied == ("delete-file:old",)
    assert not (tmp_path / "old").exists() and not (tmp_path / "new").exists()
    assert not list(tmp_path.iterdir())
