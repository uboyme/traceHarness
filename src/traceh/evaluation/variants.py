"""Frozen local source variants. Only host-declared explanatory strings may change."""

import ast
import importlib.metadata
import platform
import sqlite3
import sys
from pathlib import Path

from traceh.api.json_types import fingerprint
from traceh.evaluation.errors import BenchmarkManifestError
from traceh.evaluation.inputs import digest_bytes, object_fields, text_field

EDITABLE_TEXT = {
    "supervision/structured_collaboration.py": ("ALLOCATION_GUIDANCE",),
    "tools/reference_search.py": (
        "HistorySearchTool.description",
        "SkillSearchTool.description",
        "MemorySearchTool.description",
    ),
    "tools/output.py": (
        "ReadToolOutput.description",
        "ListToolOutputs.description",
        "SearchToolOutput.description",
    ),
    "runtime/prompt.py": ("_REFERENCE_GUIDANCE",),
}


def source_files():
    root = Path(__file__).resolve().parents[1]
    return root, tuple(
        (p.relative_to(root).as_posix(), p.read_bytes()) for p in sorted(root.rglob("*.py"))
    )


def source_digest(files):
    return fingerprint([{"file": p, "sha256": digest_bytes(b)} for p, b in sorted(files)])


def environment_identity():
    """Same installed environment, not a claim to a reproducible dependency lock."""
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "sqlite": sqlite3.sqlite_version,
        "executable_sha256": digest_bytes(Path(sys.executable).read_bytes()),
        "dependencies": sorted(
            {
                (d.metadata["Name"].lower().replace("_", "-"), d.version)
                for d in importlib.metadata.distributions()
                if d.metadata["Name"]
            }
        ),
    }


def text_node(tree, selector):
    parts = selector.split(".")
    body = tree.body
    if len(parts) == 2:
        classes = [n for n in body if isinstance(n, ast.ClassDef) and n.name == parts[0]]
        if len(classes) != 1:
            raise BenchmarkManifestError("evaluation-candidate-scope-invalid", "selector")
        body = classes[0].body
    found = [
        n.value
        for n in body
        if isinstance(n, ast.Assign)
        and len(n.targets) == 1
        and isinstance(n.targets[0], ast.Name)
        and n.targets[0].id == parts[-1]
    ]
    if len(found) != 1 or not isinstance(found[0], ast.Constant) or type(found[0].value) is not str:
        raise BenchmarkManifestError("evaluation-candidate-scope-invalid", "selector")
    return found[0]


def apply_candidate(files, patch):
    """Use AST ownership and byte ranges; no executable patch commands or whole-file edits."""
    object_fields(patch, {"format", "base_source_digest", "edits"}, "candidate")
    if type(patch["format"]) is not int or patch["format"] != 1:
        raise BenchmarkManifestError("evaluation-version-unsupported", "candidate")
    if patch["base_source_digest"] != source_digest(files):
        raise BenchmarkManifestError("evaluation-frozen-input-drift", "candidate-base")
    edits = patch["edits"]
    if type(edits) is not list or not edits or len(edits) > sum(map(len, EDITABLE_TEXT.values())):
        raise BenchmarkManifestError("evaluation-candidate-scope-invalid", "edits")
    changed, seen = dict(files), set()
    for edit in edits:
        object_fields(edit, {"file", "selector", "old_sha256", "new_text"}, "edit")
        name, selector = text_field(edit["file"], "file"), text_field(edit["selector"], "selector")
        if selector not in EDITABLE_TEXT.get(name, ()) or (name, selector) in seen:
            raise BenchmarkManifestError("evaluation-candidate-scope-invalid", "selector")
        seen.add((name, selector))
        replacement = text_field(edit["new_text"], "new_text")
        if len(replacement.encode("utf-8")) > 65536:
            raise BenchmarkManifestError("evaluation-candidate-scope-invalid", "new_text")
        raw = changed[name]
        tree = ast.parse(raw)
        node = text_node(tree, selector)
        if digest_bytes(node.value.encode("utf-8")) != edit["old_sha256"]:
            raise BenchmarkManifestError("evaluation-frozen-input-drift", "candidate-text")
        lines = raw.splitlines(keepends=True)
        start = sum(map(len, lines[: node.lineno - 1])) + node.col_offset
        end = sum(map(len, lines[: node.end_lineno - 1])) + node.end_col_offset
        candidate = raw[:start] + repr(replacement).encode("utf-8") + raw[end:]
        node.value = replacement
        if ast.dump(tree) != ast.dump(ast.parse(candidate)):
            raise BenchmarkManifestError("evaluation-candidate-scope-invalid", "ast")
        changed[name] = candidate
    return tuple(sorted(changed.items()))
