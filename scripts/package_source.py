"""Build the distributable source ZIP.

Written as a script rather than a shell one-liner for two reasons that both bit a
previous attempt:

* **Filenames.** This repository contains Chinese Markdown and PNG filenames. Some
  archivers re-encode them into ``#Uxxxx`` escapes, producing an archive that no
  longer round-trips and whose relative links break. ``zipfile`` with explicit
  UTF-8 gives a byte-exact round trip, and this script *verifies* it before
  finishing rather than assuming it.
* **Exclusions.** A real ``.env``, ``.git``, virtualenvs, caches, build output and
  recorded session data must never leave the machine. Denying by pattern in one
  reviewable place beats remembering flags at the call site.

Usage:

    python scripts/package_source.py [output.zip]
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    ".traceh",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "build",
    "dist",
    "node_modules",
    ".idea",
    ".vscode",
}

# Built artifacts, not source. A stray wheel left in the working tree is exactly
# the kind of thing that silently ships a stale version of the package.
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".pyd", ".so", ".zip", ".whl", ".tar.gz"}


def is_excluded(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    parts = relative.parts

    if any(part in EXCLUDED_DIRS for part in parts):
        return True
    if any(part.endswith(".egg-info") for part in parts):
        return True
    if path.suffix in EXCLUDED_SUFFIXES:
        return True
    # The real secret file, but never the safe template beside it.
    if path.name == ".env" or path.name.startswith(".env."):
        if path.name != ".env.example":
            return True
    return False


def collect() -> list[Path]:
    return sorted(
        path
        for path in ROOT.rglob("*")
        if path.is_file() and not is_excluded(path)
    )


def verify(archive: Path, expected: list[Path]) -> None:
    """Prove the archive round-trips before anyone relies on it."""

    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        mangled = [name for name in names if "#U" in name]
        if mangled:
            raise SystemExit(f"filename escaping detected in {len(mangled)} entries: {mangled[:3]}")
        stored = set(names)
        missing = [
            str(path.relative_to(ROOT)).replace("\\", "/")
            for path in expected
            if str(path.relative_to(ROOT)).replace("\\", "/") not in stored
        ]
        if missing:
            raise SystemExit(f"missing from archive: {missing[:5]}")
        for path in expected:
            name = str(path.relative_to(ROOT)).replace("\\", "/")
            if bundle.read(name) != path.read_bytes():
                raise SystemExit(f"content mismatch for {name}")


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT.parent / "traceharness-py-v0.4-source.zip"
    target = target.resolve()
    files = collect()

    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in files:
            name = str(path.relative_to(ROOT)).replace("\\", "/")
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            # Force the UTF-8 flag rather than relying on the writer's heuristic.
            info.flag_bits |= 0x800
            bundle.writestr(info, path.read_bytes())

    verify(target, files)

    non_ascii = [
        str(path.relative_to(ROOT)) for path in files if not str(path).isascii()
    ]
    print(f"archive: {target}")
    print(f"files:   {len(files)}")
    print(f"size:    {target.stat().st_size:,} bytes")
    print(f"non-ASCII filenames preserved: {len(non_ascii)}")
    for name in non_ascii:
        print(f"  {name}")
    print("verified: no #Uxxxx escaping, all entries byte-identical")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
