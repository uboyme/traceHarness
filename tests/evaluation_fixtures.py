"""Explicit schema-3 test material writers; not production configuration defaults."""

import json

from traceh.evaluation.inputs import digest_bytes
from traceh.evaluation.repositories import capture_initial_tree, initial_tree_digest


def write_dataset(root, manifest, cases):
    payload = json.dumps({"format": 1, "cases": cases}, ensure_ascii=False, indent=2).encode(
        "utf-8"
    )
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
        "sha256": initial_tree_digest(capture_initial_tree(root / initial_tree)),
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
