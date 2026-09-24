from dataclasses import replace
from pathlib import Path

import pytest
from test_sandbox_contract import policy

from traceh.sandbox.workspace import snapshot


def observe_read(monkeypatch, target, read):
    original_open = Path.open
    calls = []

    class ObservedFile:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def fileno(self):
            return self.stream.fileno()

        def read(self, count):
            calls.append(count)
            return read(self.stream, count, original_open)

    def open_file(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        return ObservedFile(stream) if path == target and args == ("rb",) else stream

    monkeypatch.setattr(Path, "open", open_file)
    return calls


@pytest.mark.parametrize(
    "content", [b"", b"independent fixture", b"x" * 4096], ids=["empty", "small", "4096-bytes"]
)
def test_snapshot_read_allocation_is_bounded_by_observed_file_size(
    tmp_path, monkeypatch, content
):
    target = tmp_path / "value"
    target.write_bytes(content)
    rules = policy()
    rules = replace(rules, limits=replace(rules.limits, workspace_bytes=128 * 1024 * 1024))

    def bounded_read(stream, count, original_open):
        # Model an allocator that can hold this file and one growth sentinel,
        # but not a workspace-sized transient allocation for every small file.
        if count > len(content) + 1:
            raise MemoryError("read allocation exceeds observed file size plus sentinel")
        return stream.read(count)

    calls = observe_read(monkeypatch, target, bounded_read)
    result = snapshot(tmp_path, rules)
    assert [(item.path, item.content) for item in result.files] == [("value", content)]
    assert len(calls) == 1


@pytest.mark.parametrize("replacement", [b"grown content", b""])
def test_snapshot_still_rejects_file_changes_during_bounded_read(
    tmp_path, monkeypatch, replacement
):
    target = tmp_path / "value"
    target.write_bytes(b"before")

    def changing_read(stream, count, original_open):
        with original_open(target, "wb") as writer:
            writer.write(replacement)
        return stream.read(count)

    calls = observe_read(monkeypatch, target, changing_read)
    with pytest.raises(ValueError, match="sandbox-workspace-changed"):
        snapshot(tmp_path, policy())
    assert len(calls) == 1
    assert target.read_bytes() == replacement


def test_snapshot_propagates_real_read_failure(tmp_path, monkeypatch):
    target = tmp_path / "value"
    target.write_bytes(b"before")

    def failed_read(stream, count, original_open):
        raise OSError("injected read failure")

    calls = observe_read(monkeypatch, target, failed_read)
    with pytest.raises(OSError, match="injected read failure"):
        snapshot(tmp_path, policy())
    assert len(calls) == 1


def test_snapshot_accepts_exact_total_budget_and_rejects_excess(tmp_path):
    (tmp_path / "first").write_bytes(b"a" * 2048)
    (tmp_path / "second").write_bytes(b"b" * 2048)
    result = snapshot(tmp_path, policy())
    assert sum(len(item.content) for item in result.files) == 4096
    (tmp_path / "second").write_bytes(b"b" * 2049)
    with pytest.raises(ValueError, match="sandbox-workspace-byte-limit"):
        snapshot(tmp_path, policy())
