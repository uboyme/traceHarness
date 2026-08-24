"""Read-only reconstruction of immutable Patch Manifest history."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from traceh.api.artifacts import PatchManifest
from traceh.api.events import EventEnvelope
from traceh.api.json_types import JsonValue
from traceh.artifacts.errors import ArtifactProtocolError
from traceh.artifacts.events import (
    ARTIFACT_CATALOG_STREAM,
    parse_patch_manifest,
)
from traceh.session.event_store import EventStore


@dataclass(frozen=True, slots=True)
class ArtifactCatalogIssue:
    code: str
    seq: int


class PatchArtifactCatalog:
    """Immutable projection of the one global Patch Manifest stream."""

    __slots__ = (
        "_by_artifact",
        "_by_capture",
        "_by_message",
        "_head_seq",
        "_manifests",
    )

    def __init__(self, manifests: tuple[PatchManifest, ...], head_seq: int) -> None:
        self._manifests = manifests
        self._head_seq = head_seq
        self._by_artifact = {item.artifact_id: item for item in manifests}
        self._by_capture = {item.capture_key: item for item in manifests}
        grouped: dict[tuple[str, str], list[PatchManifest]] = {}
        for item in manifests:
            grouped.setdefault((item.agent_id, item.message_id), []).append(item)
        self._by_message = {
            key: tuple(values) for key, values in grouped.items()
        }

    @classmethod
    def empty(cls) -> PatchArtifactCatalog:
        return cls((), 0)

    @classmethod
    def rebuild(
        cls, events: tuple[EventEnvelope, ...]
    ) -> PatchArtifactCatalog:
        manifests: list[PatchManifest] = []
        by_artifact: set[str] = set()
        by_capture: set[str] = set()
        expected_seq = 1
        for event in events:
            manifest = parse_patch_manifest(event)
            if manifest.recorded_seq != expected_seq:
                raise ArtifactProtocolError("artifact-sequence-invalid", manifest.recorded_seq)
            expected_seq += 1
            if manifest.artifact_id in by_artifact:
                raise ArtifactProtocolError(
                    "artifact-id-duplicate", manifest.recorded_seq
                )
            if manifest.capture_key in by_capture:
                raise ArtifactProtocolError(
                    "artifact-capture-duplicate", manifest.recorded_seq
                )
            by_artifact.add(manifest.artifact_id)
            by_capture.add(manifest.capture_key)
            manifests.append(manifest)
        return cls(tuple(manifests), expected_seq - 1)

    @property
    def head_seq(self) -> int:
        return self._head_seq

    @property
    def manifests(self) -> tuple[PatchManifest, ...]:
        return self._manifests

    def get(self, artifact_id: str) -> PatchManifest | None:
        return self._by_artifact.get(artifact_id)

    def for_capture(self, capture_key: str) -> PatchManifest | None:
        return self._by_capture.get(capture_key)

    def for_message(
        self, agent_id: str, message_id: str
    ) -> tuple[PatchManifest, ...]:
        return self._by_message.get((agent_id, message_id), ())

    def __len__(self) -> int:
        return len(self._manifests)

    def __iter__(self) -> Iterator[PatchManifest]:
        return iter(self._manifests)


def manifest_event_data(manifest: PatchManifest) -> dict[str, JsonValue]:
    """Return the exact event payload represented by ``manifest``."""

    return {
        "artifact_id": manifest.artifact_id,
        "capture_key": manifest.capture_key,
        "blob_sha256": manifest.blob.sha256,
        "blob_size_bytes": manifest.blob.size_bytes,
        "blob_address": manifest.blob.address,
        "blob_protocol_version": manifest.blob.protocol_version,
        "agent_id": manifest.agent_id,
        "session_id": manifest.session_id,
        "message_id": manifest.message_id,
        "turn_id": manifest.turn_id,
        "workspace_id": manifest.workspace_id,
        "workspace_generation": manifest.workspace_generation,
        "repository_fingerprint": manifest.repository_fingerprint,
        "base_revision": manifest.base_revision,
        "workspace_head_revision": manifest.workspace_head_revision,
        "candidate_tree": manifest.candidate_tree,
        "changed_paths": list(manifest.changed_paths),
        "capture_protocol_version": manifest.capture_protocol_version,
    }


class PatchArtifactCatalogReader:
    __slots__ = ("_store",)

    def __init__(self, store: EventStore) -> None:
        self._store = store

    @property
    def store(self) -> EventStore:
        return self._store

    async def read_events(self) -> tuple[EventEnvelope, ...]:
        return await self._store.read(ARTIFACT_CATALOG_STREAM)

    async def load(self) -> PatchArtifactCatalog:
        return PatchArtifactCatalog.rebuild(await self.read_events())


async def validate_patch_artifact_events(
    store: EventStore,
) -> tuple[ArtifactCatalogIssue, ...]:
    try:
        await PatchArtifactCatalogReader(store).load()
    except ArtifactProtocolError as error:
        return (ArtifactCatalogIssue(error.code, error.seq),)
    return ()


__all__ = [
    "ArtifactCatalogIssue",
    "PatchArtifactCatalog",
    "PatchArtifactCatalogReader",
    "manifest_event_data",
    "validate_patch_artifact_events",
]
