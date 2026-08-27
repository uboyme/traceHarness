"""The one benchmark manifest this build reads, and the one it refuses.

A benchmark is host-frozen.  The manifest therefore names *what to ask for* and
*under which bounds*, and it cannot name a repository, a Workflow node, an Agent
count, an approval digest or a promotion target path.  The runner creates a
throwaway source repository and a one-shot local bare target for every attempt,
which is the structural reason a manifest can never point this command at a real
remote.

The shared half of the schema is parsed by
:func:`traceh.product.config.parse_product_host_settings`, the same function the
optional ``traceh chat --product-config`` host uses.  There is one definition of
"what a Product Profile is"; this file adds only the benchmark's own keys.

The v0.6 ``*/case.json`` layout is **refused**, not upcast.  It named a single
scripted Agent, a shell verify command and a copied directory - none of which
exists in this pipeline - so reading it would mean inventing the confirmation,
the Budget, the Workspace and the promotion target it never had.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from traceh.api.product import RequestedTaskMode
from traceh.evaluation.errors import BenchmarkManifestError
from traceh.product.config import (
    PRODUCT_HOST_SETTINGS_KEYS,
    ProductHostSettings,
    parse_product_host_settings,
)
from traceh.product.errors import ProductError
from traceh.product.router import MAX_ROUTER_SUMMARY_CHARS
from traceh.promotion.models import verifier_definition_digest

MANIFEST_FILENAME = "benchmark.json"
LEGACY_CASE_FILENAME = "case.json"

BENCHMARK_PROTOCOL_VERSION = 1

#: Identities the runner owns because it creates the repositories they name.
BENCHMARK_SOURCE_ID = "benchmark-source"
BENCHMARK_SOURCE_REVISION = "main"
BENCHMARK_TARGET_ID = "benchmark-target"
BENCHMARK_TARGET_REF = "refs/heads/main"

MAX_TASKS = 16
MAX_ARMS = len(RequestedTaskMode)
MAX_REPETITIONS = 25

_TOP_KEYS = PRODUCT_HOST_SETTINGS_KEYS | frozenset(
    {"protocol_version", "benchmark_id", "arms", "tasks"}
)
_ARM_KEYS = frozenset({"requested_mode", "repetitions"})
_TASK_KEYS = frozenset({"task_id", "requirement", "initial_dir"})


@dataclass(frozen=True, slots=True)
class BenchmarkTask:
    """One coding requirement plus the tree an attempt starts from.

    ``initial_dir`` is resolved against the benchmark directory at load time so
    a manifest cannot reach outside the benchmark it belongs to.
    """

    task_id: str
    requirement: str
    initial_dir: Path


@dataclass(frozen=True, slots=True)
class BenchmarkArm:
    """One requested mode and how many times it is repeated per task."""

    requested_mode: RequestedTaskMode
    repetitions: int


@dataclass(frozen=True, slots=True)
class BenchmarkManifest:
    """A complete, validated benchmark definition."""

    benchmark_id: str
    settings: ProductHostSettings
    arms: tuple[BenchmarkArm, ...]
    tasks: tuple[BenchmarkTask, ...]
    directory: Path

    @property
    def attempt_count(self) -> int:
        return len(self.tasks) * sum(arm.repetitions for arm in self.arms)

    @property
    def verifier_definition_digest(self) -> str:
        """The frozen plan's own digest, computed before any attempt runs.

        This is what makes "every arm used the same verifier" provable from the
        host's input rather than inferred from whichever attempts survived long
        enough to produce a Review. It reuses the Promotion domain's single
        definition of that digest instead of computing a second one.
        """

        return verifier_definition_digest(
            self.settings.host_profile.verification_plan
        )


def load_benchmark_manifest(
    directory: Path, *, provider_id: str, model_id: str
) -> BenchmarkManifest:
    """Read ``<directory>/benchmark.json`` exactly, or refuse with one code.

    ``provider_id`` and ``model_id`` are arguments, not keys.  A checked-in
    manifest that named a model would either pin one vendor into this repository
    or ship a placeholder somebody has to remember to edit.  One run resolves one
    provider and one model and uses it for every arm, which is also what makes
    "single and multi used the same model family" true by construction rather
    than by a comparison after the fact.
    """

    root_directory = Path(directory)
    manifest_path = root_directory / MANIFEST_FILENAME
    if not manifest_path.is_file():
        if _has_legacy_cases(root_directory):
            # Deliberately reads nothing out of it. Saying "this directory is
            # the old format" is a refusal; parsing any of it would be the
            # compatibility layer this cutover exists to avoid.
            raise BenchmarkManifestError(
                "benchmark-legacy-manifest-rejected", MANIFEST_FILENAME
            )
        raise BenchmarkManifestError("benchmark-manifest-missing", MANIFEST_FILENAME)
    try:
        with manifest_path.open("r", encoding="utf-8") as stream:
            raw = json.load(stream)
    except BenchmarkManifestError:
        raise
    except Exception:
        raise BenchmarkManifestError("benchmark-manifest-unreadable", MANIFEST_FILENAME) from None
    root = _object(raw, _TOP_KEYS, "root")
    if _integer(root["protocol_version"], "protocol_version") != BENCHMARK_PROTOCOL_VERSION:
        raise BenchmarkManifestError(
            "benchmark-manifest-version-unsupported", "protocol_version"
        )
    try:
        settings = parse_product_host_settings(
            root,
            provider_id=provider_id,
            model_id=model_id,
            source_id=BENCHMARK_SOURCE_ID,
            source_revision=BENCHMARK_SOURCE_REVISION,
            promotion_target_id=BENCHMARK_TARGET_ID,
        )
    except ProductError as error:
        code = getattr(error, "code", "benchmark-manifest-profile-invalid")
        raise BenchmarkManifestError(str(code), "profile") from None
    return BenchmarkManifest(
        benchmark_id=_text(root["benchmark_id"], "benchmark_id"),
        settings=settings,
        arms=_arms(root["arms"]),
        tasks=_tasks(root["tasks"], root_directory),
        directory=root_directory,
    )


def _has_legacy_cases(directory: Path) -> bool:
    try:
        return any(directory.glob(f"*/{LEGACY_CASE_FILENAME}"))
    except OSError:
        return False


def _arms(value: object) -> tuple[BenchmarkArm, ...]:
    if type(value) is not list or not value or len(value) > MAX_ARMS:
        raise BenchmarkManifestError("benchmark-manifest-arms-invalid", "arms")
    arms: list[BenchmarkArm] = []
    seen: set[RequestedTaskMode] = set()
    for index, item in enumerate(value):
        field = f"arms.{index}"
        entry = _object(item, _ARM_KEYS, field)
        try:
            mode = RequestedTaskMode(_text(entry["requested_mode"], f"{field}.requested_mode"))
        except ValueError:
            raise BenchmarkManifestError(
                "benchmark-manifest-mode-invalid", f"{field}.requested_mode"
            ) from None
        if mode in seen:
            # Two entries for one mode would silently become one arm with a
            # repetition count nobody wrote down.
            raise BenchmarkManifestError(
                "benchmark-manifest-arm-duplicate", f"{field}.requested_mode"
            )
        seen.add(mode)
        repetitions = _integer(entry["repetitions"], f"{field}.repetitions")
        if repetitions < 1 or repetitions > MAX_REPETITIONS:
            raise BenchmarkManifestError(
                "benchmark-manifest-repetitions-invalid", f"{field}.repetitions"
            )
        arms.append(BenchmarkArm(requested_mode=mode, repetitions=repetitions))
    return tuple(arms)


def _tasks(value: object, directory: Path) -> tuple[BenchmarkTask, ...]:
    if type(value) is not list or not value or len(value) > MAX_TASKS:
        raise BenchmarkManifestError("benchmark-manifest-tasks-invalid", "tasks")
    tasks: list[BenchmarkTask] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        field = f"tasks.{index}"
        entry = _object(item, _TASK_KEYS, field)
        task_id = _identifier(entry["task_id"], f"{field}.task_id")
        if task_id in seen:
            raise BenchmarkManifestError(
                "benchmark-manifest-task-duplicate", f"{field}.task_id"
            )
        seen.add(task_id)
        requirement = _text(entry["requirement"], f"{field}.requirement")
        if len(requirement) > MAX_ROUTER_SUMMARY_CHARS:
            raise BenchmarkManifestError(
                "benchmark-manifest-requirement-invalid", f"{field}.requirement"
            )
        tasks.append(
            BenchmarkTask(
                task_id=task_id,
                requirement=requirement,
                initial_dir=_initial_dir(
                    entry["initial_dir"], directory, f"{field}.initial_dir"
                ),
            )
        )
    return tuple(tasks)


def _initial_dir(value: object, directory: Path, field: str) -> Path:
    text = _text(value, field)
    candidate = Path(text)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise BenchmarkManifestError("benchmark-manifest-path-invalid", field)
    try:
        root = directory.resolve(strict=True)
        resolved = (directory / candidate).resolve(strict=True)
    except OSError:
        raise BenchmarkManifestError("benchmark-manifest-path-missing", field) from None
    if resolved == root or root not in resolved.parents or not resolved.is_dir():
        raise BenchmarkManifestError("benchmark-manifest-path-invalid", field)
    return resolved


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
    if len(text) > 64 or not all(
        character.isalnum() or character in "-_" for character in text
    ):
        raise BenchmarkManifestError("benchmark-manifest-identifier-invalid", field)
    return text


def _integer(value: object, field: str) -> int:
    # ``type(...) is int`` already rejects ``bool``: exactness is the point, so a
    # JSON ``true`` cannot become a repetition count.
    if type(value) is not int:
        raise BenchmarkManifestError("benchmark-manifest-value-invalid", field)
    return value


__all__ = [
    "BENCHMARK_PROTOCOL_VERSION",
    "BENCHMARK_SOURCE_ID",
    "BENCHMARK_SOURCE_REVISION",
    "BENCHMARK_TARGET_ID",
    "BENCHMARK_TARGET_REF",
    "LEGACY_CASE_FILENAME",
    "MANIFEST_FILENAME",
    "MAX_ARMS",
    "MAX_REPETITIONS",
    "MAX_TASKS",
    "BenchmarkArm",
    "BenchmarkManifest",
    "BenchmarkTask",
    "load_benchmark_manifest",
]
