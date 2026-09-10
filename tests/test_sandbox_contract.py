from dataclasses import replace

import pytest

from traceh.api.json_types import fingerprint
from traceh.api.sandbox import SandboxLimits, SandboxOwner, SandboxPolicy, validate_relative_path
from traceh.sandbox.ledger import SandboxEventRecorder
from traceh.sandbox.workspace import snapshot
from traceh.session.event_store import InMemoryEventStore


def policy(**overrides):
    return SandboxPolicy(
        docker_context="explicit-test-context",
        image="sha256:" + "a" * 64,
        limits=SandboxLimits(128 * 1024 * 1024, 4096, 20, 1024, 16, 0.5, 3),
        read_paths=(".",),
        write_paths=(".",),
        **overrides,
    )


@pytest.mark.parametrize(
    "path",
    ["../outside", "/outside", "a\\b", "NUL.txt", "a/../b", "a//b", "a.", "a ", "c:foo", "", "."],
)
def test_portable_paths_reject_aliases(path):
    with pytest.raises(ValueError):
        validate_relative_path(path)


def test_policy_cannot_grant_unsupported_network_or_unpinned_image():
    with pytest.raises(ValueError, match="network-policy-unsupported"):
        replace(policy(), network="host")
    with pytest.raises(ValueError, match="pinned-image"):
        replace(policy(), image="python:latest")
    with pytest.raises(ValueError, match="outside-read-scope"):
        replace(policy(), read_paths=("src",), write_paths=("tests",))


def test_snapshot_authorized_files_and_protected_subtrees(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "hello.txt").write_text("不同项目的内容", "utf-8")
    (tmp_path / ".env").write_text("SYNTHETIC_SECRET=fixture")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("private fixture")
    (tmp_path / "private-store").mkdir()
    (tmp_path / "private-store" / "record").write_text("excluded")
    files = snapshot(tmp_path, policy(excluded_paths=("private-store",))).files
    assert [(f.path, f.content.decode()) for f in files] == [("src/hello.txt", "不同项目的内容")]


def test_snapshot_refuses_hardlink_to_outside_without_reading(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"private fixture")
    (workspace / "linked").hardlink_to(outside)
    with pytest.raises(ValueError, match="hardlink-refused"):
        snapshot(workspace, policy())


def test_snapshot_refuses_windows_junction(tmp_path):
    import os
    import subprocess

    if os.name != "nt":
        pytest.skip("Windows junction boundary")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").write_text("must stay outside")
    junction = workspace / "linked"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)], check=True, capture_output=True
    )
    try:
        with pytest.raises(ValueError, match="reparse-path-refused"):
            snapshot(workspace, policy())
        assert (outside / "sentinel").read_text() == "must stay outside"
    finally:
        # Remove only this directory link, never traverse its target.
        os.rmdir(junction)


def test_snapshot_stops_at_byte_budget_and_missing_explicit_scope(tmp_path):
    (tmp_path / "large").write_bytes(b"x" * 4097)
    with pytest.raises(ValueError, match="byte-limit"):
        snapshot(tmp_path, policy())
    with pytest.raises(ValueError, match="scope-unavailable"):
        snapshot(tmp_path, replace(policy(), read_paths=("missing",), write_paths=()))


async def test_ledger_rejects_duplicate_execution_and_wrong_owner():
    import asyncio
    from dataclasses import asdict

    store = InMemoryEventStore()
    owner = SandboxOwner("verification", "fixed-command")
    recorder = SandboxEventRecorder(store, "session:test", owner)
    request = {"execution_id": "a" * 32, "owner": asdict(owner)}
    request["digest"] = fingerprint(request)
    results = await asyncio.gather(
        recorder("sandbox/request", request),
        recorder("sandbox/request", request),
        return_exceptions=True,
    )
    assert sum(isinstance(value, ValueError) for value in results) == 1
    assert len(await store.read("session:test")) == 1
    invalid = {**request, "owner": asdict(SandboxOwner("verification", "other"))}
    invalid["digest"] = fingerprint({k: v for k, v in invalid.items() if k != "digest"})
    with pytest.raises(ValueError, match="owner-mismatch"):
        await recorder("sandbox/request", invalid)


async def test_ledger_outcome_is_bound_to_exact_request_and_written_once():
    from dataclasses import asdict

    store = InMemoryEventStore()
    owner = SandboxOwner("verification", "fixed-command")
    recorder = SandboxEventRecorder(store, "session:test", owner)
    request = {"execution_id": "b" * 32, "owner": asdict(owner)}
    request["digest"] = fingerprint(request)
    await recorder("sandbox/request", request)
    outcome = {"execution_id": "b" * 32, "owner": asdict(owner), "request_digest": "wrong"}
    outcome["digest"] = fingerprint(outcome)
    with pytest.raises(ValueError, match="request-mismatch"):
        await recorder("sandbox/outcome", outcome)
    outcome["request_digest"] = request["digest"]
    outcome["digest"] = fingerprint({k: v for k, v in outcome.items() if k != "digest"})
    await recorder("sandbox/outcome", outcome)
    with pytest.raises(ValueError, match="without-open-request"):
        await recorder("sandbox/outcome", outcome)
