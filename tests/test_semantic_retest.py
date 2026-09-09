"""Pre-registered screen gates, not fake model quality measurements."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from local_retrieval_screen.retest import freeze, summarize


def inputs():
    criteria = json.loads(
        Path("tests/local_retrieval_screen/retest.json").read_text(encoding="utf-8")
    )
    criteria["candidates"] = criteria["candidates"][:1]
    candidate = criteria["candidates"][0]["id"]
    metric = {"recall": 1.0, "precision": 1.0, "mrr": 1.0, "scope_violations": 0, "zero_hit": 1.0}
    results = [
        {
            "category": category,
            "language": language,
            "lexical": {"recall": 0},
            "candidates": [{"id": candidate, "metrics": deepcopy(metric)}],
        }
        for category, language in [
            ("exact", None),
            ("semantic", None),
            *(("added-semantic", "zh") for _ in range(4)),
            *(("added-semantic", "en") for _ in range(4)),
        ]
    ]
    corpus = {"evaluator": {"thresholds": {"exact": {"recall": 1, "precision": 1, "mrr": 1}}}}
    cost = {
        "cold_load_both_ms": 1,
        "rebuild_both_ms": 1,
        "asset_bytes": 200000000,
        "vector_bytes_per_item": 3584,
        "warm_samples_ms": {candidate: [10] * 95},
    }
    return criteria, results, corpus, cost


@pytest.mark.parametrize("failure", [None, "language", "scope", "resource", "original"])
def test_screen_requires_every_frozen_gate(failure):
    criteria, results, corpus, cost = inputs()
    if failure == "language":
        for row in results:
            if row["language"] == "zh":
                row["candidates"][0]["metrics"]["precision"] = 0.749
    elif failure == "scope":
        results[-1]["candidates"][0]["metrics"]["scope_violations"] = 1
    elif failure == "resource":
        cost["asset_bytes"] = criteria["acceptance"]["model_assets_bytes"] + 1
    elif failure == "original":
        results[0]["candidates"][0]["metrics"]["recall"] = 0.99
    summaries, accepted = summarize(results, criteria, corpus, cost)
    assert bool(accepted) is (failure is None)
    assert all(summaries[0]["checks"].values()) is (failure is None)


@pytest.mark.parametrize("field", ["acceptance", "additional_queries", "encoder"])
def test_freeze_rejects_changed_gate_query_or_unsupported_encoder_before_loading_assets(
    tmp_path, field
):
    criteria, _, _, _ = inputs()
    if field == "encoder":
        criteria["encoders"][0]["pooling"] = "mean"
    elif field == "acceptance":
        criteria[field]["added_semantic_precision_per_language"] = 0.5
    else:
        criteria[field][0]["query"] = "easier replacement query"
    path = tmp_path / "criteria.json"
    path.write_text(json.dumps(criteria), encoding="utf-8")
    with pytest.raises(
        ValueError, match="retest-(original-criteria-changed|encoder-contract-unsupported)"
    ):
        freeze(
            Path.cwd(),
            tmp_path / "missing-assets",
            tmp_path / "missing-capture",
            path,
            tmp_path / "frozen.json",
        )
    assert not (tmp_path / "frozen.json").exists()
