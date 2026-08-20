"""Clean source-copy and Wheel-audit primitives shared by L1 acceptance and L2."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath

BUILD_INPUT_FILES = ("pyproject.toml", "README.md")
TRANSIENT_PARTS = frozenset(
    {
        ".git",
        ".hg",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".traceh",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "venv",
    }
)
MAX_CANDIDATE_FILES = 10_000
MAX_CANDIDATE_BYTES = 100 * 1024 * 1024
MAX_WHEEL_FILES = 10_000
MAX_WHEEL_BYTES = 100 * 1024 * 1024

# A candidate Wheel is imported into the same validation interpreter as the
# trusted core and its host-owned evaluator.  Owning one of those import roots
# would let the candidate shadow the code that is supposed to evaluate it.
_VALIDATION_CONTROL_IMPORT_ROOTS = frozenset(
    {
        "_pytest",
        "packaging",
        "pip",
        "pluggy",
        "pytest",
        "pytest_asyncio",
        "setuptools",
        "traceh",
        "wheel",
    }
)
_RESERVED_IMPORT_ROOTS = frozenset(
    name.casefold()
    for name in (*sys.stdlib_module_names, *_VALIDATION_CONTROL_IMPORT_ROOTS)
)


class ArtifactContractError(ValueError):
    """A fixed-code source or archive contract failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def copy_declared_build_input(project: Path, destination: Path) -> Path:
    """Copy the standard build inputs without stale local build products."""

    reject_source_link(project)
    project = project.resolve()
    destination.mkdir(parents=True)
    for name in BUILD_INPUT_FILES:
        source = project / name
        if not source.is_file():
            raise ArtifactContractError(
                "required-build-input-missing",
                "A required package build input is missing",
            )
        shutil.copy2(source, destination / name)

    source_tree = project / "src"
    if not source_tree.is_dir():
        raise ArtifactContractError(
            "required-source-tree-missing",
            "The package source tree is missing",
        )
    shutil.copytree(
        source_tree,
        destination / "src",
        ignore=shutil.ignore_patterns(
            "__pycache__",
            ".pytest_cache",
            ".ruff_cache",
            "build",
            "dist",
            "*.egg-info",
            "*.pyc",
            "*.pyo",
        ),
    )
    return destination


def copy_candidate_source(candidate: Path, destination: Path) -> Path:
    """Copy one candidate without following links or carrying local state.

    L2 accepts uncommitted L1 source, so Git cannot define this file set.  The
    copy instead walks the explicit candidate root, rejects links and secrets,
    excludes transient directories, and applies generic file/byte budgets.
    """

    reject_source_link(candidate)
    candidate = candidate.resolve()
    destination.mkdir(parents=True)
    file_count = 0
    byte_count = 0
    for root_text, directories, files in os.walk(candidate, followlinks=False):
        root = Path(root_text)
        kept_directories: list[str] = []
        for name in directories:
            source = root / name
            reject_source_link(source)
            if _is_transient_part(name):
                continue
            kept_directories.append(name)
        directories[:] = kept_directories

        relative_root = root.relative_to(candidate)
        target_root = destination / relative_root
        target_root.mkdir(parents=True, exist_ok=True)
        for name in files:
            source = root / name
            reject_source_link(source)
            if name.casefold() == ".env":
                raise ArtifactContractError(
                    "candidate-secret-file-rejected",
                    "Candidate source contains a forbidden environment file",
                )
            if _is_transient_file(name):
                continue
            size = source.stat().st_size
            file_count += 1
            byte_count += size
            if file_count > MAX_CANDIDATE_FILES or byte_count > MAX_CANDIDATE_BYTES:
                raise ArtifactContractError(
                    "candidate-source-budget-exceeded",
                    "Candidate source exceeds the validation copy budget",
                )
            shutil.copy2(source, target_root / name)
    return destination


def transient_wheel_members(wheel: Path) -> tuple[str, ...]:
    """Return cache/build members that must never ship in a Wheel."""

    try:
        with zipfile.ZipFile(wheel) as archive:
            members = []
            for info in archive.infolist():
                path = PurePosixPath(info.filename)
                if (
                    info.filename.casefold().endswith((".pyc", ".pyo"))
                    or any(part.casefold() in TRANSIENT_PARTS for part in path.parts)
                    or any(part.casefold().endswith(".egg-info") for part in path.parts)
                ):
                    members.append(info.filename)
    except (OSError, zipfile.BadZipFile):
        return ("<unreadable-wheel>",)
    return tuple(sorted(members))


def audit_candidate_wheel(wheel: Path, *, entry_module: str) -> tuple[str, ...]:
    """Return stable issue codes for an L2 candidate Wheel.

    A source-only candidate is one plugin package.  Rejecting extra top-level
    modules, ``.pth`` files and Python startup hooks prevents a candidate from
    silently joining every interpreter startup instead of the explicit
    ``traceh.plugins`` Entry Point path.
    """

    issues: set[str] = set()
    expected_root = entry_module.split(".", 1)[0]
    expected_module_file = f"{entry_module.replace('.', '/')}.py"
    expected_package_file = f"{entry_module.replace('.', '/')}/__init__.py"
    if expected_root.casefold() in _RESERVED_IMPORT_ROOTS:
        issues.add("wheel-host-namespace-collision")
    try:
        with zipfile.ZipFile(wheel) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_WHEEL_FILES:
                issues.add("wheel-file-budget-exceeded")
            if sum(info.file_size for info in infos) > MAX_WHEEL_BYTES:
                issues.add("wheel-byte-budget-exceeded")
            names = {info.filename for info in infos}
            if expected_module_file not in names and expected_package_file not in names:
                issues.add("entry-module-missing-from-wheel")
            dist_info_roots = {
                PurePosixPath(info.filename).parts[0]
                for info in infos
                if PurePosixPath(info.filename).parts
                and PurePosixPath(info.filename).parts[0].endswith(".dist-info")
            }
            if len(dist_info_roots) != 1:
                issues.add("wheel-dist-info-count-invalid")
            for info in infos:
                path = PurePosixPath(info.filename)
                parts = path.parts
                if (
                    path.is_absolute()
                    or ".." in parts
                    or "\\" in info.filename
                    or info.flag_bits & 0x1
                    or stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF)
                ):
                    issues.add("wheel-member-unsafe")
                    continue
                if not parts:
                    continue
                first = parts[0]
                import_root = first[:-3] if first.casefold().endswith(".py") else first
                if (
                    first not in dist_info_roots
                    and import_root.casefold() in _RESERVED_IMPORT_ROOTS
                ):
                    issues.add("wheel-host-namespace-collision")
                allowed_module_file = len(parts) == 1 and first == f"{expected_root}.py"
                if (
                    first != expected_root
                    and first not in dist_info_roots
                    and not allowed_module_file
                ):
                    issues.add("wheel-unexpected-top-level-member")
                if first.lower() in {"sitecustomize.py", "usercustomize.py"}:
                    issues.add("wheel-python-startup-hook-rejected")
                if info.filename.casefold().endswith(".pth"):
                    issues.add("wheel-path-hook-rejected")
    except (OSError, zipfile.BadZipFile):
        return ("wheel-unreadable",)

    if transient_wheel_members(wheel):
        issues.add("wheel-transient-member-rejected")
    return tuple(sorted(issues))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def apply_wheelhouse_environment(
    environment: dict[str, str],
    wheelhouse: Path,
) -> None:
    """Force this process and nested pip calls onto one local Wheel source.

    pip splits ``PIP_FIND_LINKS`` on whitespace.  A raw filesystem path would
    therefore become multiple sources when any parent directory contains a
    space.  ``Path.as_uri()`` produces one percent-encoded token and also makes
    the local-only trust boundary explicit to the environment sanitizer.
    """

    environment["PIP_NO_INDEX"] = "1"
    environment["PIP_FIND_LINKS"] = wheelhouse.resolve().as_uri()


def reject_source_link(path: Path) -> None:
    """Reject links and Windows reparse points without following them.

    ``Path.is_symlink()`` deliberately does not classify a Windows directory
    Junction as a symlink.  Both can redirect a source walk outside the
    explicitly selected candidate root, so the contract rejects either form.
    """

    try:
        if path.is_symlink():
            raise ArtifactContractError(
                "candidate-symlink-rejected",
                "Candidate source contains a symbolic link",
            )
        is_junction = getattr(path, "is_junction", None)
        if is_junction is not None and is_junction():
            raise ArtifactContractError(
                "candidate-reparse-point-rejected",
                "Candidate source contains a Windows Junction",
            )
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if reparse_flag and attributes & reparse_flag:
            raise ArtifactContractError(
                "candidate-reparse-point-rejected",
                "Candidate source contains a filesystem reparse point",
            )
    except FileNotFoundError:
        return


def _is_transient_part(name: str) -> bool:
    folded = name.casefold()
    return folded in TRANSIENT_PARTS or folded.endswith(".egg-info")


def _is_transient_file(name: str) -> bool:
    return name.casefold().endswith((".pyc", ".pyo", ".whl"))


__all__ = [
    "ArtifactContractError",
    "apply_wheelhouse_environment",
    "audit_candidate_wheel",
    "copy_candidate_source",
    "copy_declared_build_input",
    "reject_source_link",
    "sha256_file",
    "transient_wheel_members",
]
