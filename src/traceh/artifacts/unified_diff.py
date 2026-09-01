"""Pure, fail-closed projection of an immutable Git unified diff.

The Patch Artifact remains the source of truth.  This module only derives a
read model for human inspection: a bounded file summary and line-numbered text
hunks.  It deliberately returns unknown statistics for binary or malformed
input instead of presenting partial counts as exact evidence.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from enum import StrEnum


class PatchLineKind(StrEnum):
    """One displayable unified-diff line kind."""

    CONTEXT = "context"
    ADDITION = "addition"
    DELETION = "deletion"
    METADATA = "metadata"


@dataclass(frozen=True, slots=True)
class UnifiedDiffLine:
    """One text hunk line with its old/new source positions."""

    kind: PatchLineKind
    old_line: int | None
    new_line: int | None
    text: str


@dataclass(frozen=True, slots=True)
class UnifiedDiffFileSummary:
    """Bounded file-level facts safe to retain in Product observation."""

    path: str
    status: str
    additions: int | None
    deletions: int | None
    binary: bool


@dataclass(frozen=True, slots=True)
class UnifiedDiffSummary:
    """File summary and exact totals, when the complete patch proves them."""

    files: tuple[UnifiedDiffFileSummary, ...]
    additions: int | None
    deletions: int | None
    complete: bool


@dataclass(frozen=True, slots=True)
class UnifiedDiffFile:
    """One parsed file section used by the on-demand full Patch view."""

    summary: UnifiedDiffFileSummary
    old_path: str | None
    new_path: str | None
    lines: tuple[UnifiedDiffLine, ...]


@dataclass(frozen=True, slots=True)
class UnifiedDiff:
    """A pure projection of exact Patch Artifact bytes."""

    summary: UnifiedDiffSummary
    files: tuple[UnifiedDiffFile, ...]


@dataclass(slots=True)
class _FileBuilder:
    header_old_path: str
    header_new_path: str
    old_path: str | None = None
    new_path: str | None = None
    status: str = "modified"
    saw_old_marker: bool = False
    saw_new_marker: bool = False
    binary: bool = False
    malformed: bool = False
    additions: int = 0
    deletions: int = 0
    lines: list[UnifiedDiffLine] | None = None

    def __post_init__(self) -> None:
        self.lines = []


_HUNK_HEADER = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$"
)
_SECTION_PREFIX = "diff --git "
_NO_NEWLINE_MARKER = "\\ No newline at end of file"
_FILE_STATUSES = frozenset({"added", "modified", "deleted", "renamed"})


def parse_unified_diff(
    content: bytes,
    *,
    expected_paths: tuple[str, ...],
) -> UnifiedDiff:
    """Parse one complete Git patch without ever guessing incomplete counts.

    ``expected_paths`` comes from the already validated immutable Manifest and
    bounds the number and identity of file sections accepted here.  Any
    structure, path, or hunk-count mismatch makes the whole projection
    incomplete.  Non-UTF-8 body bytes survive as surrogate escapes for the
    later safe-display boundary.  Binary sections retain their proven
    path/status but expose no synthetic line counts.
    """

    if type(content) is not bytes or type(expected_paths) is not tuple:
        raise TypeError("unified diff inputs must be bytes and tuple")
    if any(type(path) is not str for path in expected_paths):
        raise TypeError("expected paths must be strings")
    if len(set(expected_paths)) != len(expected_paths):
        raise ValueError("expected paths must be unique")
    text = content.decode("utf-8", errors="surrogateescape")

    # Git patches are LF records.  ``str.splitlines()`` is deliberately not
    # used: U+2028/U+2029 are untrusted file content, not Patch structure, and
    # must remain in the line for the UI's safe-display boundary to escape.
    raw_lines = text.split("\n")
    if raw_lines and raw_lines[-1] == "":
        raw_lines.pop()
    if not raw_lines:
        if expected_paths:
            return _invalid_diff(expected_paths)
        return UnifiedDiff(UnifiedDiffSummary((), 0, 0, True), ())

    sections: list[list[str]] = []
    for line in raw_lines:
        if line.startswith(_SECTION_PREFIX):
            sections.append([line])
        elif not sections:
            return _invalid_diff(expected_paths)
        else:
            sections[-1].append(line)
    if len(sections) != len(expected_paths):
        return _invalid_diff(expected_paths)

    parsed: list[UnifiedDiffFile] = []
    for section in sections:
        item = _parse_file(section)
        if item is None:
            return _invalid_diff(expected_paths)
        parsed.append(item)

    paths = tuple(file.summary.path for file in parsed)
    if len(set(paths)) != len(paths) or set(paths) != set(expected_paths):
        return _invalid_diff(expected_paths)

    ordered = tuple(sorted(parsed, key=lambda item: item.summary.path.encode("utf-8")))
    has_unknown = any(
        item.summary.additions is None or item.summary.deletions is None
        for item in ordered
    )
    summaries = tuple(item.summary for item in ordered)
    summary = UnifiedDiffSummary(
        files=summaries,
        additions=(
            None
            if has_unknown
            else sum(item.additions or 0 for item in summaries)
        ),
        deletions=(
            None
            if has_unknown
            else sum(item.deletions or 0 for item in summaries)
        ),
        complete=not has_unknown,
    )
    return UnifiedDiff(summary, ordered)


def _parse_file(lines: list[str]) -> UnifiedDiffFile | None:
    paths = _header_paths(lines[0])
    if paths is None:
        return None
    builder = _FileBuilder(*paths)
    index = 1
    while index < len(lines):
        line = lines[index]
        if line.startswith("new file mode "):
            if builder.status != "modified":
                return None
            builder.status = "added"
        elif line.startswith("deleted file mode "):
            if builder.status != "modified":
                return None
            builder.status = "deleted"
        elif line.startswith("rename from "):
            builder.status = "renamed"
            builder.old_path = _metadata_path(line.removeprefix("rename from "))
            if builder.old_path is None:
                return None
        elif line.startswith("rename to "):
            builder.status = "renamed"
            builder.new_path = _metadata_path(line.removeprefix("rename to "))
            if builder.new_path is None:
                return None
        elif line.startswith("--- "):
            if builder.saw_old_marker:
                return None
            builder.saw_old_marker = True
            builder.old_path = _marker_path(line.removeprefix("--- "), "a/")
            if builder.old_path is None and line != "--- /dev/null":
                return None
        elif line.startswith("+++ "):
            if builder.saw_new_marker:
                return None
            builder.saw_new_marker = True
            builder.new_path = _marker_path(line.removeprefix("+++ "), "b/")
            if builder.new_path is None and line != "+++ /dev/null":
                return None
        elif line.startswith("@@ "):
            consumed = _parse_hunk(lines, index, builder)
            if consumed is None:
                return None
            index = consumed
            continue
        elif line == "GIT binary patch" or (
            line.startswith("Binary files ") and line.endswith(" differ")
        ):
            builder.binary = True
            assert builder.lines is not None
            builder.lines.extend(
                UnifiedDiffLine(PatchLineKind.METADATA, None, None, item)
                for item in lines[index:]
            )
            break
        elif line.startswith("index "):
            pass
        elif line.startswith(
            ("old mode ", "new mode ", "similarity index ", "dissimilarity index ")
        ):
            assert builder.lines is not None
            builder.lines.append(
                UnifiedDiffLine(PatchLineKind.METADATA, None, None, line)
            )
        else:
            return None
        index += 1

    return _finish_file(builder)


def _parse_hunk(
    lines: list[str],
    index: int,
    builder: _FileBuilder,
) -> int | None:
    match = _HUNK_HEADER.fullmatch(lines[index])
    if match is None or builder.binary:
        return None
    old_line = int(match.group(1))
    old_count = 1 if match.group(2) is None else int(match.group(2))
    new_line = int(match.group(3))
    new_count = 1 if match.group(4) is None else int(match.group(4))
    used_old = 0
    used_new = 0
    index += 1
    assert builder.lines is not None
    while index < len(lines):
        line = lines[index]
        if line.startswith("@@ "):
            break
        if line.startswith(_SECTION_PREFIX):
            return None
        if line == _NO_NEWLINE_MARKER:
            builder.lines.append(
                UnifiedDiffLine(PatchLineKind.METADATA, None, None, line)
            )
            index += 1
            continue
        if not line:
            return None
        marker = line[0]
        body = line[1:]
        if marker == " ":
            builder.lines.append(
                UnifiedDiffLine(PatchLineKind.CONTEXT, old_line, new_line, body)
            )
            old_line += 1
            new_line += 1
            used_old += 1
            used_new += 1
        elif marker == "+":
            builder.lines.append(
                UnifiedDiffLine(PatchLineKind.ADDITION, None, new_line, body)
            )
            new_line += 1
            used_new += 1
            builder.additions += 1
        elif marker == "-":
            builder.lines.append(
                UnifiedDiffLine(PatchLineKind.DELETION, old_line, None, body)
            )
            old_line += 1
            used_old += 1
            builder.deletions += 1
        else:
            return None
        index += 1
    if used_old != old_count or used_new != new_count:
        return None
    return index


def _finish_file(builder: _FileBuilder) -> UnifiedDiffFile | None:
    if builder.status not in _FILE_STATUSES:
        return None
    if builder.status == "added":
        if builder.old_path is not None:
            return None
        path = builder.new_path or builder.header_new_path
    elif builder.status == "deleted":
        if builder.new_path is not None:
            return None
        path = builder.old_path or builder.header_old_path
    elif builder.status == "renamed":
        if builder.old_path is None or builder.new_path is None:
            return None
        path = builder.new_path
    else:
        path = builder.new_path or builder.old_path or builder.header_new_path

    if not builder.binary:
        markers_match = builder.saw_old_marker == builder.saw_new_marker
        if not markers_match:
            return None
        if builder.status == "added" and builder.new_path is None:
            return None
        if builder.status == "deleted" and builder.old_path is None:
            return None
        if builder.status == "modified" and builder.saw_old_marker:
            if builder.old_path != builder.new_path:
                return None
    assert builder.lines is not None
    summary = UnifiedDiffFileSummary(
        path=path,
        status=builder.status,
        additions=None if builder.binary else builder.additions,
        deletions=None if builder.binary else builder.deletions,
        binary=builder.binary,
    )
    return UnifiedDiffFile(
        summary=summary,
        old_path=builder.old_path,
        new_path=builder.new_path,
        lines=tuple(builder.lines),
    )


def _header_paths(line: str) -> tuple[str, str] | None:
    try:
        parts = shlex.split(line.removeprefix(_SECTION_PREFIX), posix=True)
    except ValueError:
        return None
    if len(parts) != 2:
        return None
    old_token = _decode_git_token(parts[0])
    new_token = _decode_git_token(parts[1])
    if old_token is None or new_token is None:
        return None
    old_path = _prefixed_path(old_token, "a/")
    new_path = _prefixed_path(new_token, "b/")
    if old_path is None or new_path is None:
        return None
    return old_path, new_path


def _marker_path(value: str, prefix: str) -> str | None:
    if value == "/dev/null":
        return None
    parsed = _metadata_path(value)
    return None if parsed is None else _prefixed_path(parsed, prefix)


def _metadata_path(value: str) -> str | None:
    try:
        parts = shlex.split(value, posix=True)
    except ValueError:
        return None
    if len(parts) != 1:
        return None
    return _decode_git_token(parts[0])


def _decode_git_token(value: str) -> str | None:
    """Decode Git's C-style octal path bytes after shell-like tokenization."""

    encoded = bytearray()
    index = 0
    escapes = {
        "a": 7,
        "b": 8,
        "t": 9,
        "n": 10,
        "v": 11,
        "f": 12,
        "r": 13,
        "\\": 92,
        '"': 34,
    }
    while index < len(value):
        character = value[index]
        if character != "\\":
            encoded.extend(character.encode("utf-8", errors="surrogateescape"))
            index += 1
            continue
        index += 1
        if index >= len(value):
            return None
        escaped = value[index]
        if escaped in escapes:
            encoded.append(escapes[escaped])
            index += 1
            continue
        if escaped in "01234567":
            end = index
            while end < len(value) and end - index < 3 and value[end] in "01234567":
                end += 1
            encoded.append(int(value[index:end], 8))
            index = end
            continue
        return None
    return bytes(encoded).decode("utf-8", errors="surrogateescape")


def _prefixed_path(value: str, prefix: str) -> str | None:
    if not value.startswith(prefix) or value == prefix:
        return None
    return value.removeprefix(prefix)


def _invalid_diff(expected_paths: tuple[str, ...]) -> UnifiedDiff:
    files = tuple(
        UnifiedDiffFileSummary(path, "unknown", None, None, False)
        for path in expected_paths
    )
    return UnifiedDiff(UnifiedDiffSummary(files, None, None, False), ())


__all__ = [
    "PatchLineKind",
    "UnifiedDiff",
    "UnifiedDiffFile",
    "UnifiedDiffFileSummary",
    "UnifiedDiffLine",
    "UnifiedDiffSummary",
    "parse_unified_diff",
]
