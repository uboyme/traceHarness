"""Fresh reconstruction and CAS verification of Patch Artifacts."""

from __future__ import annotations

from traceh.api.artifacts import ArtifactCas, PatchArtifact, PatchManifest
from traceh.artifacts.catalog import PatchArtifactCatalogReader
from traceh.artifacts.errors import (
    ArtifactInputError,
    ArtifactNotFoundError,
    ArtifactProtocolError,
)
from traceh.artifacts.manifest import require_artifact_identifier
from traceh.session.event_store import EventStore


class PatchArtifactReader:
    """Resolve Manifest facts and verify their exact content-addressed bytes."""

    __slots__ = ("_cas", "_catalogs", "_store")

    def __init__(self, store: EventStore, cas: ArtifactCas) -> None:
        self._store = store
        self._cas = cas
        self._catalogs = PatchArtifactCatalogReader(store)

    @property
    def store(self) -> EventStore:
        return self._store

    @property
    def cas(self) -> ArtifactCas:
        return self._cas

    async def load(self, artifact_id: str) -> PatchArtifact:
        artifact_id = require_artifact_identifier(
            artifact_id, field="artifact_id"
        )
        manifest = (await self._catalogs.load()).get(artifact_id)
        if manifest is None:
            raise ArtifactNotFoundError
        return PatchArtifact(manifest=manifest, content=await self._cas.read(manifest.blob))

    async def for_capture(self, capture_key: str) -> PatchArtifact | None:
        if (
            type(capture_key) is not str
            or len(capture_key) != 64
            or capture_key != capture_key.lower()
            or any(character not in "0123456789abcdef" for character in capture_key)
        ):
            raise ArtifactInputError("artifact-capture-key-invalid", "capture_key")
        manifest = (await self._catalogs.load()).for_capture(capture_key)
        if manifest is None:
            return None
        return PatchArtifact(manifest=manifest, content=await self._cas.read(manifest.blob))

    async def refs_for(self, agent_id: str, message_id: str) -> tuple[str, ...]:
        agent_id = require_artifact_identifier(agent_id, field="agent_id")
        message_id = require_artifact_identifier(message_id, field="message_id")
        manifests = (await self._catalogs.load()).for_message(agent_id, message_id)
        references: list[str] = []
        for manifest in manifests:
            await self._cas.read(manifest.blob)
            references.append(manifest.reference)
        return tuple(references)

    async def resolve_reference(self, reference: str) -> PatchArtifact:
        if type(reference) is not str or not reference.startswith("patch:"):
            raise ArtifactInputError("artifact-reference-invalid", "reference")
        body = reference.removeprefix("patch:")
        artifact_id, separator, digest = body.partition("@")
        if (
            not separator
            or len(digest) != 64
            or digest != digest.lower()
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ArtifactInputError("artifact-reference-invalid", "reference")
        artifact = await self.load(artifact_id)
        if artifact.manifest.manifest_digest != digest:
            raise ArtifactProtocolError(
                "artifact-reference-digest-mismatch",
                artifact.manifest.recorded_seq,
            )
        return artifact


def require_same_manifest(
    existing: PatchManifest, expected_data: dict[str, object]
) -> None:
    """Reject capture-key reuse with any different immutable source binding."""

    from traceh.api.json_types import canonical_json
    from traceh.artifacts.catalog import manifest_event_data

    try:
        same = canonical_json(manifest_event_data(existing)) == canonical_json(
            expected_data
        )
    except Exception:
        same = False
    if not same:
        from traceh.artifacts.errors import ArtifactOperationConflictError

        raise ArtifactOperationConflictError


__all__ = ["PatchArtifactReader", "require_same_manifest"]
