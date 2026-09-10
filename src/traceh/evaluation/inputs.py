"""Host-owned, immutable evaluation inputs; no domain state or secret loading."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from traceh.evaluation.errors import BenchmarkManifestError

MAX_DOCUMENT_BYTES = 4 * 1024 * 1024


def object_fields(value, keys, field):
    if type(value) is not dict or set(value) != set(keys):
        raise BenchmarkManifestError("evaluation-manifest-invalid", field)
    return value


def text_field(value, field):
    if type(value) is not str or not value or value != value.strip():
        raise BenchmarkManifestError("evaluation-manifest-invalid", field)
    return value


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def decode_document(data: bytes):
    def invalid_constant(value):
        raise ValueError("nonfinite JSON")

    try:
        return json.loads(
            data.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=invalid_constant
        )
    except (ValueError, UnicodeError):
        raise BenchmarkManifestError("evaluation-manifest-invalid", "json") from None


def confined_path(root: Path, value: object, *, directory=False) -> Path:
    name = text_field(value, "file")
    relative = Path(name)
    if relative.is_absolute() or relative.drive or ".." in relative.parts or ":" in name:
        raise BenchmarkManifestError("evaluation-manifest-invalid", "file")
    try:
        root = root.resolve(strict=True)
        candidate = root
        for part in relative.parts:
            candidate /= part
            if candidate.is_symlink() or candidate.is_junction():
                raise ValueError("linked input")
        resolved = candidate.resolve(strict=True)
        if (
            resolved == root
            or root not in resolved.parents
            or not (resolved.is_dir() if directory else resolved.is_file())
        ):
            raise ValueError("outside input root")
        if not directory and resolved.stat().st_nlink != 1:
            raise ValueError("linked file")
    except (OSError, ValueError):
        raise BenchmarkManifestError("evaluation-manifest-invalid", "file") from None
    return resolved


@dataclass(frozen=True, slots=True)
class FrozenFile:
    root: Path
    relative: str
    content: bytes

    @property
    def sha256(self):
        return digest_bytes(self.content)

    @property
    def data(self):
        return decode_document(self.content)

    def reference(self):
        return {"file": self.relative, "sha256": self.sha256}

    def verify(self):
        try:
            current = read_input(self.root, self.relative)
            if current.content != self.content:
                raise ValueError("changed")
        except (ValueError, OSError):
            raise BenchmarkManifestError("evaluation-frozen-input-drift", "file") from None


def read_input(root: Path, relative: str) -> FrozenFile:
    path = confined_path(root, relative)
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_DOCUMENT_BYTES + 1)
        if len(data) > MAX_DOCUMENT_BYTES:
            raise ValueError("too large")
    except (OSError, ValueError):
        raise BenchmarkManifestError("evaluation-manifest-invalid", "file") from None
    return FrozenFile(root.resolve(), path.relative_to(root.resolve()).as_posix(), data)


def referenced_input(root: Path, value: object) -> FrozenFile:
    ref = object_fields(value, {"file", "sha256"}, "reference")
    item = read_input(root, ref["file"])
    if ref["sha256"] != item.sha256:
        raise BenchmarkManifestError("evaluation-frozen-input-drift", "sha256")
    return item
