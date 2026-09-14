"""Product-owned suite parsing; the generic manifest owns the envelope."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from traceh.api.product import RequestedTaskMode
from traceh.evaluation.errors import BenchmarkManifestError
from traceh.evaluation.inputs import (
    FrozenFile,
    confined_path,
    object_fields,
    referenced_input,
    text_field,
)
from traceh.evaluation.repositories import BENCHMARK_SOURCE_REVISION
from traceh.evaluation.retrieval import FrozenRetrieval, load_retrieval
from traceh.product.config import (
    PRODUCT_HOST_SETTINGS_KEYS,
    ProductHostSettings,
    parse_product_host_settings,
)
from traceh.product.errors import ProductError
from traceh.product.events import MAX_PRODUCT_REQUIREMENT_CHARS
from traceh.promotion.models import verifier_definition_digest

#: Identities the runner owns because it creates the repositories they name.
BENCHMARK_SOURCE_ID = "benchmark-source"
BENCHMARK_TARGET_ID = "benchmark-target"

MAX_TASKS = 16
MAX_ARMS = len(RequestedTaskMode)

PRODUCT_SUITE_SETTINGS_KEYS = (PRODUCT_HOST_SETTINGS_KEYS - {"verification"}) | {
    "modes",
    "retrieval",
}
_TASK_KEYS = frozenset(
    {"case_id", "group_id", "requirement", "initial_tree", "sha256", "verification"}
)


@dataclass(frozen=True, slots=True)
class BenchmarkTask:
    """One coding requirement plus the tree an attempt starts from.

    ``initial_dir`` is resolved against the benchmark directory at load time so
    a manifest cannot reach outside the benchmark it belongs to.
    """

    task_id: str
    group_id: str
    material_digest: str
    requirement: str
    initial_dir: Path
    settings: ProductHostSettings

    @property
    def verifier_definition_digest(self) -> str:
        return verifier_definition_digest(self.settings.host_profile.verification_plan)


@dataclass(frozen=True, slots=True)
class ProductSuite:
    """A complete, validated benchmark definition."""

    benchmark_id: str
    profile_id: str
    provider_id: str
    model_id: str
    modes: tuple[RequestedTaskMode, ...]
    tasks: tuple[BenchmarkTask, ...]
    directory: Path
    retrieval: FrozenRetrieval | None = None
    rubric: FrozenFile | None = None


def load_product_suite(manifest, *, provider_id: str, model_id: str) -> ProductSuite:
    root = _object(manifest.task_settings, PRODUCT_SUITE_SETTINGS_KEYS, "task_settings")
    assessment = manifest.assessment
    semantic = assessment["requires_review"] is True
    if not semantic and assessment != {
        "scorer_id": "product-durable-v1",
        "version": 1,
        "rubric": None,
        "requires_review": False,
    }:
        raise BenchmarkManifestError("evaluation-manifest-invalid", "assessment")
    if semantic and (
        assessment["scorer_id"] != "product-durable-semantic-v1"
        or assessment["version"] != 1
        or assessment["rubric"] is None
    ):
        raise BenchmarkManifestError("evaluation-manifest-invalid", "assessment")
    if (
        type(manifest.assessment["version"]) is not int
        or type(manifest.assessment["requires_review"]) is not bool
    ):
        raise BenchmarkManifestError("evaluation-manifest-invalid", "assessment")
    dataset = _object(manifest.dataset.data, {"format", "cases"}, "dataset")
    if type(dataset["format"]) is not int or dataset["format"] != 2:
        raise BenchmarkManifestError("evaluation-version-unsupported", "dataset")
    tasks = _tasks(
        dataset["cases"],
        manifest.directory,
        shared=root,
        provider_id=provider_id,
        model_id=model_id,
    )
    rubric = referenced_input(manifest.directory, assessment["rubric"]) if semantic else None
    if rubric is not None:
        raw = object_fields(rubric.data, {"format", "criteria"}, "product-rubric")
        if (
            type(raw["format"]) is not int
            or raw["format"] != 1
            or type(raw["criteria"]) is not dict
        ):
            raise BenchmarkManifestError("evaluation-manifest-invalid", "product-rubric")
        if set(raw["criteria"]) != {task.task_id for task in tasks}:
            raise BenchmarkManifestError("evaluation-manifest-invalid", "product-rubric-cases")
        for criteria in raw["criteria"].values():
            if type(criteria) is not list or not criteria:
                raise BenchmarkManifestError(
                    "evaluation-manifest-invalid", "product-rubric-criteria"
                )
            for criterion in criteria:
                text_field(criterion, "product-rubric-criterion")
    return ProductSuite(
        benchmark_id=manifest.benchmark_id,
        profile_id=root["profile_id"],
        provider_id=provider_id,
        model_id=model_id,
        modes=_modes(root["modes"]),
        tasks=tasks,
        directory=manifest.directory,
        retrieval=load_retrieval(root["retrieval"], manifest.directory, tasks),
        rubric=rubric,
    )


def _modes(value):
    if type(value) is not list or not value or len(value) > MAX_ARMS:
        raise BenchmarkManifestError("evaluation-manifest-invalid", "modes")
    modes = []
    for item in value:
        try:
            mode = RequestedTaskMode(item)
        except (ValueError, TypeError):
            raise BenchmarkManifestError("evaluation-manifest-invalid", "modes") from None
        if mode in modes:
            raise BenchmarkManifestError("benchmark-manifest-arm-duplicate", "modes")
        modes.append(mode)
    return tuple(modes)


def _tasks(
    value: object, directory: Path, *, shared, provider_id, model_id
) -> tuple[BenchmarkTask, ...]:
    if type(value) is not list or not value or len(value) > MAX_TASKS:
        raise BenchmarkManifestError("benchmark-manifest-tasks-invalid", "tasks")
    tasks: list[BenchmarkTask] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        field = f"tasks.{index}"
        entry = _object(item, _TASK_KEYS, field)
        task_id = _identifier(entry["case_id"], f"{field}.task_id")
        if task_id in seen:
            raise BenchmarkManifestError("benchmark-manifest-task-duplicate", f"{field}.task_id")
        seen.add(task_id)
        requirement = _text(entry["requirement"], f"{field}.requirement")
        if len(requirement) > MAX_PRODUCT_REQUIREMENT_CHARS:
            raise BenchmarkManifestError(
                "benchmark-manifest-requirement-invalid", f"{field}.requirement"
            )
        try:
            settings = parse_product_host_settings(
                {**shared, "verification": entry["verification"]},
                provider_id=provider_id,
                model_id=model_id,
                source_id=BENCHMARK_SOURCE_ID,
                source_revision=BENCHMARK_SOURCE_REVISION,
                promotion_target_id=BENCHMARK_TARGET_ID,
            )
        except ProductError as error:
            raise BenchmarkManifestError(error.code, f"{field}.profile") from None
        tasks.append(
            BenchmarkTask(
                task_id=task_id,
                group_id=_identifier(entry["group_id"], "group_id"),
                material_digest=_text(entry["sha256"], "sha256"),
                requirement=requirement,
                initial_dir=_initial_dir(entry["initial_tree"], directory, f"{field}.initial_dir"),
                settings=settings,
            )
        )
    return tuple(tasks)


def _initial_dir(value: object, directory: Path, field: str) -> Path:
    return confined_path(directory, value, directory=True)


def _object(value: object, keys: frozenset[str], field: str) -> Mapping[str, object]:
    if type(value) is not dict or set(value) != keys:
        raise BenchmarkManifestError("benchmark-manifest-shape-invalid", field)
    return value


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BenchmarkManifestError("benchmark-manifest-value-invalid", field)
    return value


def _identifier(value: object, field: str) -> str:
    text = _text(value, field)
    if len(text) > 64 or not all(character.isalnum() or character in "-_" for character in text):
        raise BenchmarkManifestError("benchmark-manifest-identifier-invalid", field)
    return text


def _integer(value: object, field: str) -> int:
    # ``type(...) is int`` already rejects ``bool``: exactness is the point, so a
    # JSON ``true`` cannot become a repetition count.
    if type(value) is not int:
        raise BenchmarkManifestError("benchmark-manifest-value-invalid", field)
    return value
