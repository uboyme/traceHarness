"""Local content-addressed storage for immutable Patch bytes."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import tempfile
from pathlib import Path

from traceh.api.artifacts import PatchBlob
from traceh.artifacts.errors import ArtifactCasError, ArtifactInputError
from traceh.artifacts.manifest import PATCH_BLOB_PROTOCOL_VERSION
from traceh.concurrency import await_worker_convergence


class LocalArtifactCas:
    """Explicit local SHA-256 CAS; Event Log entries never contain this path."""

    __slots__ = ("_root",)

    def __init__(self, root: Path) -> None:
        try:
            normalized = Path(root)
        except Exception:
            raise ArtifactInputError("artifact-cas-root-invalid", "cas_root") from None
        if not normalized.is_absolute():
            raise ArtifactInputError("artifact-cas-root-invalid", "cas_root")
        self._root = normalized.absolute()
        try:
            _initialize_owned_root(self._root)
        except (ArtifactCasError, OSError):
            raise ArtifactInputError("artifact-cas-root-invalid", "cas_root") from None

    @property
    def local_root(self) -> Path:
        return self._root

    async def put(self, content: bytes) -> PatchBlob:
        if type(content) is not bytes:
            raise ArtifactInputError("artifact-blob-invalid", "content")
        task = asyncio.create_task(
            asyncio.to_thread(self._put_sync, content),
            name="traceh-artifact-cas-put",
        )
        return await _await_owned(task)

    async def read(self, blob: PatchBlob) -> bytes:
        if type(blob) is not PatchBlob:
            raise ArtifactCasError("artifact-cas-reference-invalid")
        task = asyncio.create_task(
            asyncio.to_thread(self._read_sync, blob),
            name="traceh-artifact-cas-read",
        )
        return await _await_owned(task)

    def _put_sync(self, content: bytes) -> PatchBlob:
        self._require_root()
        digest = hashlib.sha256(content).hexdigest()
        blob = PatchBlob(
            sha256=digest,
            size_bytes=len(content),
            address=f"sha256/{digest}",
            protocol_version=PATCH_BLOB_PROTOCOL_VERSION,
        )
        destination = self._destination(digest)
        self._require_owned_directory(destination.parent, create=True)
        if os.path.lexists(destination):
            self._verify_file(destination, blob, content)
            return blob

        temporary: Path | None = None
        try:
            self._require_owned_directory(destination.parent, create=False)
            with tempfile.NamedTemporaryFile(
                mode="w+b",
                dir=destination.parent,
                prefix=".patch-",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            self._require_owned_directory(destination.parent, create=False)
            try:
                os.link(temporary, destination)
            except FileExistsError:
                pass
            except OSError:
                if not os.path.lexists(destination):
                    raise ArtifactCasError("artifact-cas-write-failed") from None
            self._verify_file(destination, blob, content)
            return blob
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _read_sync(self, blob: PatchBlob) -> bytes:
        self._require_root()
        if (
            type(blob.sha256) is not str
            or len(blob.sha256) != 64
            or blob.sha256 != blob.sha256.lower()
            or any(character not in "0123456789abcdef" for character in blob.sha256)
            or type(blob.size_bytes) is not int
            or blob.size_bytes < 0
            or type(blob.address) is not str
            or blob.address != f"sha256/{blob.sha256}"
            or type(blob.protocol_version) is not int
            or blob.protocol_version != PATCH_BLOB_PROTOCOL_VERSION
        ):
            raise ArtifactCasError("artifact-cas-reference-invalid")
        destination = self._destination(blob.sha256)
        if not self._require_owned_directory(destination.parent, create=False):
            raise ArtifactCasError("artifact-cas-missing")
        if not os.path.lexists(destination):
            raise ArtifactCasError("artifact-cas-missing")
        return self._verify_file(destination, blob)

    def _verify_file(
        self,
        path: Path,
        blob: PatchBlob,
        expected_content: bytes | None = None,
    ) -> bytes:
        try:
            if not self._require_owned_directory(path.parent, create=False):
                raise ArtifactCasError("artifact-cas-missing")
            if not path.is_file() or _is_reparse(path):
                raise ArtifactCasError("artifact-cas-path-unsafe")
            content = path.read_bytes()
        except ArtifactCasError:
            raise
        except OSError:
            raise ArtifactCasError("artifact-cas-read-failed") from None
        if (
            len(content) != blob.size_bytes
            or hashlib.sha256(content).hexdigest() != blob.sha256
            or (expected_content is not None and content != expected_content)
        ):
            raise ArtifactCasError("artifact-cas-collision")
        return content

    def _destination(self, digest: str) -> Path:
        return self._root / "sha256" / digest[:2] / digest

    def _require_root(self) -> None:
        if not self._root.is_dir() or _has_reparse_component(self._root):
            raise ArtifactCasError("artifact-cas-path-unsafe")

    def _require_owned_directory(self, directory: Path, *, create: bool) -> bool:
        """Walk one lexical CAS-relative directory chain without following links."""

        self._require_root()
        try:
            components = directory.relative_to(self._root).parts
        except ValueError:
            raise ArtifactCasError("artifact-cas-path-unsafe") from None
        current = self._root
        for component in components:
            if component in ("", ".", ".."):
                raise ArtifactCasError("artifact-cas-path-unsafe")
            current /= component
            if not os.path.lexists(current):
                if not create:
                    return False
                try:
                    current.mkdir()
                except FileExistsError:
                    pass
                except OSError:
                    raise ArtifactCasError("artifact-cas-write-failed") from None
            if not current.is_dir() or _is_reparse(current):
                raise ArtifactCasError("artifact-cas-path-unsafe")
        if _has_reparse_component(directory):
            raise ArtifactCasError("artifact-cas-path-unsafe")
        return True


async def _await_owned[T](task: asyncio.Task[T]) -> T:
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancellation:
        await await_worker_convergence(task)
        if task.cancelled():
            raise cancellation
        failure = task.exception()
        if failure is not None:
            raise cancellation from failure
        raise cancellation


def _is_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(os.path, "isjunction", None)
        if is_junction is not None and is_junction(path):
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        return bool(attributes & marker)
    except OSError:
        return True


def _has_reparse_component(path: Path) -> bool:
    if not path.is_absolute():
        return True
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if os.path.lexists(current) and _is_reparse(current):
            return True
    return False


def _initialize_owned_root(path: Path) -> None:
    """Create an absolute root one component at a time without crossing links."""

    if not path.is_absolute():
        raise ArtifactCasError("artifact-cas-path-unsafe")
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        if not os.path.lexists(current):
            try:
                current.mkdir()
            except FileExistsError:
                pass
        if not current.is_dir() or _is_reparse(current):
            raise ArtifactCasError("artifact-cas-path-unsafe")


__all__ = ["LocalArtifactCas"]
