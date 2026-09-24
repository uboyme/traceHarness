"""Combine already-admitted single-case materials into one benchmark (plan experiment C).

The background host scopes suggestions to one benchmark and its development
cases, while each admitted real-repository case was prepared as its own material.
This copies the named cases verbatim - requirement, initial tree, frozen verifier
and limits - re-proves every copied tree against its recorded digest, and applies
one explicit ``task_settings`` document to all of them. It never re-derives a
verifier, never edits a case and refuses duplicates or digest drift.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from real_repository_evaluation.materials import sha, write_json
from traceh.evaluation.repositories import (
    InitialTreeLimits,
    capture_initial_tree,
    initial_tree_digest,
)


def combine(sources, settings_benchmark, output, benchmark_id):
    output.mkdir(parents=True, exist_ok=False)
    cases, provenance = [], []
    for material, case_id in sources:
        material = Path(material).resolve()
        manifest = json.loads((material / "benchmark.json").read_bytes())
        dataset_bytes = (material / manifest["dataset"]["file"]).read_bytes()
        if sha(dataset_bytes) != manifest["dataset"]["sha256"]:
            raise ValueError("source dataset digest mismatch: " + case_id)
        rows = [c for c in json.loads(dataset_bytes)["cases"] if c["case_id"] == case_id]
        if len(rows) != 1 or any(c["case_id"] == case_id for c in cases):
            raise ValueError("case missing or duplicated: " + case_id)
        row = dict(rows[0])
        limits = InitialTreeLimits(**row["initial_tree_limits"])
        target = output / case_id / "initial"
        shutil.copytree(material / row["initial_tree"], target)
        if initial_tree_digest(capture_initial_tree(target, limits=limits)) != row["sha256"]:
            raise ValueError("copied initial tree drifted: " + case_id)
        row["initial_tree"] = f"{case_id}/initial"
        cases.append(row)
        provenance.append(
            {
                "case_id": case_id,
                "source_material": str(material),
                "source_dataset": sha(dataset_bytes),
            }
        )
    write_json(output / "dataset.json", dict(format=3, cases=cases))
    manifest = json.loads(Path(settings_benchmark).read_bytes())
    manifest["benchmark_id"] = benchmark_id
    manifest["dataset"] = dict(
        file="dataset.json", sha256=sha((output / "dataset.json").read_bytes())
    )
    write_json(output / "benchmark.json", manifest)
    write_json(output.parent / (output.name + "-provenance.json"), {"cases": provenance})
    from traceh.evaluation.evaluators.product_manifest import load_product_suite
    from traceh.evaluation.manifest import load_benchmark_manifest

    load_product_suite(load_benchmark_manifest(output), provider_id="offline", model_id="offline")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", nargs=2, action="append", metavar=("MATERIAL", "CASE_ID"))
    parser.add_argument("--settings-benchmark", type=Path, required=True)
    parser.add_argument("--benchmark-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    print(
        combine(
            options.case, options.settings_benchmark, options.output.resolve(), options.benchmark_id
        )
    )
