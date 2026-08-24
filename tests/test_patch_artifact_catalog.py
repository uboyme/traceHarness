"""Immutable Patch Manifest protocol and local CAS contracts."""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from traceh.agents.commit_reconciliation import committed_after_failure
from traceh.api.artifacts import PatchBlob
from traceh.api.events import EventEnvelope, PendingEvent
from traceh.artifacts import (
    ArtifactCasError,
    ArtifactInputError,
    ArtifactProtocolError,
    LocalArtifactCas,
    PatchArtifactCatalog,
    PatchArtifactReader,
)
from traceh.artifacts.events import (
    ARTIFACT_CATALOG_STREAM,
    PATCH_MANIFEST_RECORDED,
    is_patch_manifest_event,
    patch_manifest_data,
)
from traceh.artifacts.manifest import (
    PATCH_BLOB_PROTOCOL_VERSION,
    freeze_changed_paths,
    patch_artifact_id,
    patch_capture_key,
)
from traceh.session.event_store import InMemoryEventStore


def _blob(content: bytes = b"patch\n") -> PatchBlob:
    digest = hashlib.sha256(content).hexdigest()
    return PatchBlob(
        sha256=digest,
        size_bytes=len(content),
        address=f"sha256/{digest}",
        protocol_version=PATCH_BLOB_PROTOCOL_VERSION,
    )


_CAPTURE_KEY = patch_capture_key(
    agent_id="agent-one",
    message_id="message-one",
    workspace_id="workspace-one",
    workspace_generation=2,
)


def _data(*, capture_key: str = _CAPTURE_KEY, artifact_id: str | None = None):
    return patch_manifest_data(
        artifact_id=artifact_id or patch_artifact_id(capture_key),
        capture_key=capture_key,
        blob=_blob(),
        agent_id="agent-one",
        session_id="session-one",
        message_id="message-one",
        turn_id="turn-one",
        workspace_id="workspace-one",
        workspace_generation=2,
        repository_fingerprint="2" * 64,
        base_revision="3" * 40,
        workspace_head_revision="4" * 40,
        candidate_tree="5" * 40,
        changed_paths=("src/module.py", "tests/test_module.py"),
    )


def _event(seq: int, data=None, **overrides) -> EventEnvelope:
    fields = {
        "event_id": uuid4(),
        "stream_id": ARTIFACT_CATALOG_STREAM,
        "seq": seq,
        "type": PATCH_MANIFEST_RECORDED,
        "schema_version": 1,
        "data": _data() if data is None else data,
        "occurred_at": datetime(2026, 1, seq, tzinfo=UTC),
    }
    fields.update(overrides)
    return EventEnvelope(**fields)


def test_manifest_round_trips_with_stable_reference_and_source_binding() -> None:
    catalog = PatchArtifactCatalog.rebuild((_event(1),))
    manifest = catalog.manifests[0]

    assert manifest.artifact_id == patch_artifact_id(_CAPTURE_KEY)
    assert manifest.changed_paths == ("src/module.py", "tests/test_module.py")
    assert manifest.workspace_generation == 2
    assert manifest.reference == f"patch:{manifest.artifact_id}@{manifest.manifest_digest}"
    assert catalog.for_capture(_CAPTURE_KEY) is manifest
    assert catalog.for_message("agent-one", "message-one") == (manifest,)


@pytest.mark.parametrize(
    ("events", "code", "seq"),
    (
        ((_event(2),), "artifact-sequence-invalid", 2),
        ((_event(1), _event(2)), "artifact-id-duplicate", 2),
    ),
)
def test_catalog_rejects_contradictory_or_noncontiguous_history(
    events, code: str, seq: int
) -> None:
    with pytest.raises(ArtifactProtocolError) as raised:
        PatchArtifactCatalog.rebuild(events)
    assert raised.value.code == code
    assert raised.value.seq == seq


def test_catalog_recomputes_derived_capture_and_artifact_identity() -> None:
    valid = _data()
    cases = (
        (
            {
                **valid,
                "capture_key": "1" * 64,
                "artifact_id": patch_artifact_id("1" * 64),
            },
            "artifact-capture-key-invalid",
        ),
        ({**valid, "artifact_id": "patch-arbitrary"}, "artifact-id-invalid"),
    )
    for data, code in cases:
        with pytest.raises(ArtifactProtocolError) as raised:
            PatchArtifactCatalog.rebuild((_event(1, data),))
        assert raised.value.code == code
        assert raised.value.seq == 1


@pytest.mark.parametrize(
    "path",
    (
        ".gitmodules",
        ".git/config",
        ".traceh/state.json",
        "../escape.txt",
        "folder\\file.txt",
        "name\nwith-control.txt",
        "NUL.txt",
    ),
)
def test_manifest_paths_reject_control_escape_and_nonportable_names(path: str) -> None:
    with pytest.raises(ArtifactInputError):
        freeze_changed_paths((path,))


def test_manifest_paths_reject_casefold_and_unicode_normalization_collisions() -> None:
    with pytest.raises(ArtifactInputError) as casefolded:
        freeze_changed_paths(("pkg/Name.py", "pkg/name.py"))
    assert casefolded.value.code == "artifact-path-collision"

    with pytest.raises(ArtifactInputError) as decomposed:
        freeze_changed_paths(("e\N{COMBINING ACUTE ACCENT}.txt",))
    assert decomposed.value.code == "artifact-path-normalization-invalid"


class _HostileType(str):
    def __ne__(self, other):
        raise RuntimeError("fixture hostile comparison")


def test_hostile_envelope_fields_become_one_stable_protocol_failure() -> None:
    event = replace(_event(1), type=_HostileType(PATCH_MANIFEST_RECORDED))
    with pytest.raises(ArtifactProtocolError) as raised:
        PatchArtifactCatalog.rebuild((event,))
    assert raised.value.code == "artifact-payload-invalid"
    assert raised.value.seq == 1


class _HostileItems(dict):
    def items(self):
        raise RuntimeError("fixture comparison unavailable")


async def test_unreadable_commit_comparison_is_unknown_not_false() -> None:
    expected = _data()
    event = _event(1, _HostileItems(expected))

    async def read_events() -> tuple[EventEnvelope, ...]:
        return (event,)

    committed = await committed_after_failure(
        read_events,
        lambda candidate: is_patch_manifest_event(candidate, expected),
    )
    assert committed is None


async def test_local_cas_is_idempotent_and_fresh_reader_verifies_bytes(
    tmp_path: Path,
) -> None:
    cas = LocalArtifactCas(tmp_path / "cas")
    content = b"diff --git a/a b/a\n"
    first = await cas.put(content)
    second = await cas.put(content)
    assert first == second
    assert await cas.read(first) == content

    store = InMemoryEventStore()
    data = _data()
    data["blob_sha256"] = first.sha256
    data["blob_size_bytes"] = first.size_bytes
    data["blob_address"] = first.address
    await store.append(
        ARTIFACT_CATALOG_STREAM,
        expected_seq=0,
        events=(
            PendingEvent(type=PATCH_MANIFEST_RECORDED, data=data),
        ),
    )
    loaded = await PatchArtifactReader(store, cas).load(patch_artifact_id(_CAPTURE_KEY))
    assert loaded.content == content
    assert loaded.manifest.blob == first


async def test_local_cas_detects_changed_or_missing_content(tmp_path: Path) -> None:
    cas = LocalArtifactCas(tmp_path / "cas")
    blob = await cas.put(b"original")
    destination = cas.local_root / "sha256" / blob.sha256[:2] / blob.sha256
    destination.write_bytes(b"changed")
    with pytest.raises(ArtifactCasError) as changed:
        await cas.read(blob)
    assert changed.value.code == "artifact-cas-collision"

    destination.unlink()
    with pytest.raises(ArtifactCasError) as missing:
        await cas.read(blob)
    assert missing.value.code == "artifact-cas-missing"


async def _make_directory_link(link: Path, target: Path) -> None:
    if os.name != "nt":
        try:
            await asyncio.to_thread(
                os.symlink, target, link, target_is_directory=True
            )
        except OSError:
            pytest.skip("directory symlink creation is unavailable")
        return
    process = await asyncio.create_subprocess_exec(
        "cmd",
        "/c",
        "mklink",
        "/J",
        str(link),
        str(target),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    if await process.wait() != 0:
        pytest.skip("junction creation is unavailable")


async def test_local_cas_rejects_a_post_init_reparse_parent_without_outside_effects(
    tmp_path: Path,
) -> None:
    cas = LocalArtifactCas(tmp_path / "cas")
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = cas.local_root / "sha256"
    await _make_directory_link(linked_parent, outside)
    content = b"outside-content"
    blob = _blob(content)
    outside_prefix = outside / blob.sha256[:2]

    try:
        with pytest.raises(ArtifactCasError) as writing:
            await cas.put(content)
        assert writing.value.code == "artifact-cas-path-unsafe"
        assert not outside_prefix.exists()

        outside_prefix.mkdir()
        (outside_prefix / blob.sha256).write_bytes(content)
        with pytest.raises(ArtifactCasError) as reading:
            await cas.read(blob)
        assert reading.value.code == "artifact-cas-path-unsafe"
    finally:
        if os.name == "nt":
            linked_parent.rmdir()
        else:
            linked_parent.unlink()


class _GatedLocalArtifactCas(LocalArtifactCas):
    __slots__ = ("entered", "release")

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.entered = threading.Event()
        self.release = threading.Event()

    def _put_sync(self, content: bytes) -> PatchBlob:
        blob = super()._put_sync(content)
        self.entered.set()
        self.release.wait()
        return blob


async def test_local_cas_put_converges_before_rethrowing_repeated_cancellation(
    tmp_path: Path,
) -> None:
    cas = _GatedLocalArtifactCas(tmp_path / "cas")
    content = b"durable before the caller can leave"
    putting = asyncio.create_task(cas.put(content))
    assert await asyncio.to_thread(cas.entered.wait, 5)
    putting.cancel()
    putting.cancel()
    putting.cancel()
    assert not putting.done()

    cas.release.set()
    with pytest.raises(asyncio.CancelledError):
        await putting
    assert await cas.read(_blob(content)) == content
