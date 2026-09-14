"""Bounded views of one actual UTF-8 file read; no cache or historical file store."""

from __future__ import annotations

import hashlib
import re

from traceh.api.json_types import canonical_json

MAX_FILE_PAGE_LINES = 120
MAX_FILE_PAGE_CHARS = 8000


MAX_OUTLINE_ENTRIES = 400
MAX_OUTLINE_TEXT_CHARS = 200
#: Definition and heading lines. This is a line predicate, not a parser: it never
#: fails on unexpected text, and it claims nothing about semantics.
_OUTLINE_LINE = re.compile(r"^(?:\s*(?:async\s+def|def|class)\s+\w|#{1,6}\s+\S)")


def render_file_outline(path, raw, *, start_line, source_sha256):
    """Where a file's definitions are, without returning their bodies.

    Reading a large tree page by page puts every line into the model request and
    keeps it there. An outline answers "what is in this file and at which line"
    for a small fraction of that, so a reader can navigate first and spend real
    pages only where they are needed.
    """

    digest = hashlib.sha256(raw).hexdigest()
    if source_sha256 is not None and source_sha256 != digest:
        raise ValueError("file-read-source-changed")
    lines = raw.decode("utf-8").splitlines()
    found = [
        {"line": number, "text": lines[number - 1].strip()[:MAX_OUTLINE_TEXT_CHARS]}
        for number in range(start_line, len(lines) + 1)
        if _OUTLINE_LINE.match(lines[number - 1])
    ]
    entries = found[:MAX_OUTLINE_ENTRIES]

    def build(items):
        page = {
            "format": 1,
            "mode": "outline",
            "path": path,
            "source_sha256": digest,
            "total_lines": len(lines),
            "definitions": items,
            "eof": len(items) == len(found),
        }
        if not page["eof"]:
            page["next_read"] = {
                "path": path,
                "mode": "outline",
                "start_line": items[-1]["line"] + 1,
                "source_sha256": digest,
            }
        return page

    while entries and len(canonical_json(build(entries))) > MAX_FILE_PAGE_CHARS:
        entries.pop()
    return build(entries)


def read_options(arguments):
    values = {}
    for name, default in (("start_line", 1), ("end_line", None), ("start_column", 1)):
        value = arguments.get(name, default)
        if value is None and name == "end_line" and name not in arguments:
            values[name] = None
            continue
        if type(value) is not int or value < 1:
            raise ValueError("file-read-range-invalid")
        values[name] = value
    if values["end_line"] is not None and values["end_line"] < values["start_line"]:
        raise ValueError("file-read-range-invalid")
    digest = arguments.get("source_sha256")
    if "source_sha256" in arguments and (
        type(digest) is not str or re.fullmatch(r"[0-9a-f]{64}", digest) is None
    ):
        raise ValueError("file-read-source-digest-invalid")
    return {**values, "source_sha256": digest}


def render_file_page(path, raw, *, start_line, end_line, start_column, source_sha256):
    digest = hashlib.sha256(raw).hexdigest()
    if source_sha256 is not None and source_sha256 != digest:
        raise ValueError("file-read-source-changed")
    # Match SearchTextTool's splitlines semantics, including CRLF and Unicode
    # separators. The digest refers to raw bytes, never the normalized view.
    lines = raw.decode("utf-8").splitlines()
    total = len(lines)
    if start_line > max(1, total) or (
        start_column > (max(1, len(lines[start_line - 1])) if total else 1)
    ):
        raise ValueError("file-read-range-outside-source")
    stop = min(total, end_line if end_line is not None else total)
    parts = []
    line, column = start_line, start_column
    last_line = last_column = 0

    def render(parts, line, column, last_line, last_column):
        eof = line > total
        next_read = None
        if not eof:
            next_read = {
                "path": path,
                "start_line": line,
                "start_column": column,
                "source_sha256": digest,
            }
            if end_line is not None and line <= stop:
                next_read["end_line"] = end_line
        return {
            "format": 1,
            "path": path,
            "source_sha256": digest,
            "total_lines": total,
            "start_line": start_line,
            "start_column": start_column,
            "end_line": last_line,
            "end_column": last_column,
            "text": "\n".join(parts),
            "truncated": line <= stop,
            "eof": eof,
            "next_read": next_read,
        }

    while line <= stop and len(parts) < MAX_FILE_PAGE_LINES:
        body = lines[line - 1]
        remaining = len(body) - (column - 1)

        def candidate(count, *, line=line, column=column, body=body):
            end_column = column - 1 + count
            complete = end_column == len(body)
            position = (line + 1, 1) if complete else (line, end_column + 1)
            text = f"{line}: {body[column - 1 : end_column]}"
            page = render([*parts, text], *position, line, end_column)
            return text, position, page

        low, high, best = 0, min(remaining, MAX_FILE_PAGE_CHARS), -1
        # Completing a line can remove continuation metadata, so serialized
        # length is monotonic only within a partial line. Check completion first.
        if remaining <= MAX_FILE_PAGE_CHARS:
            _, _, complete_page = candidate(remaining)
            if len(canonical_json(complete_page)) <= MAX_FILE_PAGE_CHARS:
                best, high = remaining, -1
            else:
                high = remaining - 1
        while low <= high:
            count = (low + high) // 2
            _, _, page = candidate(count)
            if len(canonical_json(page)) <= MAX_FILE_PAGE_CHARS:
                best = count
                low = count + 1
            else:
                high = count - 1
        if best < 0 or (best == 0 and remaining):
            if not parts:
                raise ValueError("file-read-page-budget-too-small")
            break
        text, position, _ = candidate(best)
        parts.append(text)
        last_line, last_column = line, column - 1 + best
        line, column = position
        if best < remaining:
            break
    page = render(parts, line, column, last_line, last_column)
    if len(canonical_json(page)) > MAX_FILE_PAGE_CHARS:
        raise ValueError("file-read-page-budget-too-small")
    return page
