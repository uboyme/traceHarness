"""Explicit schema-3 test material writers; not production configuration defaults."""

import json
from dataclasses import asdict

from traceh.evaluation.inputs import digest_bytes
from traceh.evaluation.repositories import (
    InitialTreeLimits,
    capture_initial_tree,
    initial_tree_digest,
)

# Explicit policy of these small fixtures, not a production fallback.
FIXTURE_TREE_LIMITS = InitialTreeLimits(256, 1_048_576, 8_388_608)


def capture_fixture_tree(path):
    return capture_initial_tree(path, limits=FIXTURE_TREE_LIMITS)


def fixture_tree_limits():
    return asdict(FIXTURE_TREE_LIMITS)


def write_dataset(root, manifest, cases, *, format_version=1):
    payload = json.dumps(
        {"format": format_version, "cases": cases}, ensure_ascii=False, indent=2
    ).encode("utf-8")
    (root / "dataset.json").write_bytes(payload)
    manifest["dataset"] = {"file": "dataset.json", "sha256": digest_bytes(payload)}
    (root / "benchmark.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def material_case(root, case_id, requirement, initial_tree):
    return {
        "case_id": case_id,
        "group_id": case_id,
        "requirement": requirement,
        "initial_tree": initial_tree,
        "initial_tree_limits": asdict(FIXTURE_TREE_LIMITS),
        "sha256": initial_tree_digest(capture_fixture_tree(root / initial_tree)),
    }


def product_manifest(benchmark_id, settings):
    return {
        "protocol_version": 3,
        "benchmark_id": benchmark_id,
        "task_type": "product_task",
        "dataset": None,
        "task_settings": settings,
        "assessment": {
            "scorer_id": "product-durable-v1",
            "version": 1,
            "rubric": None,
            "requires_review": False,
        },
    }
