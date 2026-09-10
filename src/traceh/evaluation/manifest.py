"""One versioned evaluation envelope, independent of task-specific configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from traceh.evaluation.contracts import TaskType
from traceh.evaluation.errors import BenchmarkManifestError
from traceh.evaluation.inputs import (
    FrozenFile,
    object_fields,
    read_input,
    referenced_input,
    text_field,
)

BENCHMARK_PROTOCOL_VERSION = 3
MANIFEST_FILENAME = "benchmark.json"
LEGACY_CASE_FILENAME = "case.json"
TOP_KEYS = frozenset(
    {"protocol_version", "benchmark_id", "task_type", "dataset", "task_settings", "assessment"}
)


@dataclass(frozen=True, slots=True)
class BenchmarkManifest:
    document: FrozenFile
    dataset: FrozenFile
    benchmark_id: str
    task_type: TaskType

    @property
    def directory(self):
        return self.document.root

    @property
    def task_settings(self):
        return self.document.data["task_settings"]

    @property
    def assessment(self):
        return self.document.data["assessment"]

    def verify(self):
        self.document.verify()
        self.dataset.verify()


def load_benchmark_manifest(directory: Path) -> BenchmarkManifest:
    directory = Path(directory)
    if not (directory / MANIFEST_FILENAME).is_file():
        code = (
            "benchmark-legacy-manifest-rejected"
            if any(directory.glob("*/case.json"))
            else "benchmark-manifest-missing"
        )
        raise BenchmarkManifestError(code, "manifest")
    document = read_input(directory, MANIFEST_FILENAME)
    raw = document.data
    if type(raw) is not dict:
        raise BenchmarkManifestError("evaluation-manifest-invalid", "root")
    if (
        type(raw.get("protocol_version")) is not int
        or raw["protocol_version"] != BENCHMARK_PROTOCOL_VERSION
    ):
        raise BenchmarkManifestError("evaluation-version-unsupported", "protocol_version")
    root = object_fields(raw, TOP_KEYS, "root")
    try:
        task_type = TaskType(root["task_type"])
    except (ValueError, TypeError):
        raise BenchmarkManifestError("evaluation-task-type-unsupported", "task_type") from None
    if type(root["task_settings"]) is not dict or type(root["assessment"]) is not dict:
        raise BenchmarkManifestError("evaluation-manifest-invalid", "settings")
    return BenchmarkManifest(
        document,
        referenced_input(directory, root["dataset"]),
        text_field(root["benchmark_id"], "benchmark_id"),
        task_type,
    )
