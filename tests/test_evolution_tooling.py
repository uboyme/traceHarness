"""Experiment C tooling: combined materials stay byte-identical; plans are paired and direct."""

import json
import os
import shutil
import urllib.request
from pathlib import Path

import pytest
from real_repository_evaluation.combine import combine
from real_repository_evaluation.evolution import _direct_only, plans_dev, plans_validate, settings

from traceh.chat.background import load_background_settings
from traceh.evaluation.plan import load_run_options

PRODUCT = Path(__file__).parents[1] / "benchmarks" / "product_v1"


def _sandbox(tmp_path):
    path = tmp_path / "sandbox.json"
    path.write_text(json.dumps({"format": 2, "policy": None, "plugin_grants": []}))
    return path


def test_combined_cases_are_copied_verbatim_and_revalidated(tmp_path):
    output = combine(
        [(PRODUCT, "fix_failing_check"), (PRODUCT, "guard_invalid_input")],
        PRODUCT / "benchmark.json",
        tmp_path / "combined",
        "explicit-combined-fixture",
    )
    source = {c["case_id"]: c for c in json.loads((PRODUCT / "dataset.json").read_bytes())["cases"]}
    combined = json.loads((output / "dataset.json").read_bytes())["cases"]
    assert [c["case_id"] for c in combined] == ["fix_failing_check", "guard_invalid_input"]
    for case in combined:
        original = source[case["case_id"]]
        assert case["verification"] == original["verification"]
        assert case["sha256"] == original["sha256"]
        assert case["requirement"] == original["requirement"]


def test_combining_refuses_duplicates_and_tree_drift(tmp_path):
    with pytest.raises(ValueError, match="duplicated"):
        combine(
            [(PRODUCT, "fix_failing_check"), (PRODUCT, "fix_failing_check")],
            PRODUCT / "benchmark.json",
            tmp_path / "dup",
            "explicit-combined-fixture",
        )
    drifted = tmp_path / "drifted"
    shutil.copytree(PRODUCT, drifted)
    initial = drifted / "fix_failing_check" / "initial"
    victim = next(p for p in sorted(initial.rglob("*")) if p.is_file())
    victim.write_bytes(victim.read_bytes() + b"\n# drift\n")
    with pytest.raises(ValueError, match="drifted"):
        combine(
            [(drifted, "fix_failing_check")],
            PRODUCT / "benchmark.json",
            tmp_path / "out",
            "explicit-combined-fixture",
        )


def test_every_generated_plan_is_paired_direct_and_loadable(tmp_path):
    sandbox = _sandbox(tmp_path)
    dev = plans_dev(
        PRODUCT, [("fix_failing_check", sandbox), ("add_missing_helper", sandbox)], tmp_path / "dev"
    )
    candidate = tmp_path / "candidate.json"
    candidate.write_text('{"format": 1}', encoding="utf-8")
    validation = plans_validate(
        candidate, [(PRODUCT, "guard_invalid_input", sandbox)], tmp_path / "v"
    )
    paths = [e["plan"] for e in dev["plans"]] + [dev["template"]["plan"]]
    paths += [e["plan"] for e in validation]
    for path in paths:
        raw = json.loads(Path(path).read_bytes())
        # Paired plans run each arm in a private worker with proxies disabled.
        assert raw["execution"]["network_mode"] == "direct"
        assert [v["role"] for v in raw["variants"]] == ["baseline", "candidate"]
        assert load_run_options(Path(path)).case_ids
    assert [e["repetition"] for e in validation] == [1, 2]
    firsts = [
        json.loads(Path(e["plan"]).read_bytes())["execution"]["first_arm"] for e in validation
    ]
    assert firsts == ["baseline", "candidate"]


def test_background_settings_are_the_current_format(tmp_path):
    raw = settings(PRODUCT, tmp_path / "plan.json", tmp_path / "ws", tmp_path / "out", 1)
    path = tmp_path / "background.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    loaded = load_background_settings(path)
    assert loaded.document.data["format"] == 2
    assert loaded.document.data["selectors"] == [["product/execution.py", "CODER_GUIDANCE"]]


@pytest.mark.frozen_unicode
def test_the_batch_runner_records_each_plan_and_refuses_edited_plans(tmp_path, monkeypatch):
    import hashlib

    from task_type_evaluation.run_batch import run
    from test_evaluation_comparison import pair_plan
    from test_retrieval_episode_evaluator import BENCHMARK

    monkeypatch.setenv("UNUSED_EVALUATION_KEY", "local-fixture-not-a-real-key")
    plan = pair_plan(tmp_path / "p1", case_id="m-direct")
    digest = hashlib.sha256(plan.read_bytes()).hexdigest()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"plans": [{"plan": str(plan), "sha256": digest}]}))
    ledger = tmp_path / "ledger.jsonl"
    empty = tmp_path / "p1" / "empty.env"
    assert run(manifest, BENCHMARK, tmp_path / "out", ledger, env_file=empty) == 0
    record = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
    assert record["exit_code"] == 0 and record["pairs"][0]["case_id"] == "m-direct"
    assert record["invariant_violated"] is False and "provider_or_unknown" in record
    plan.write_text(plan.read_text() + " ")
    with pytest.raises(SystemExit, match="plan changed"):
        run(manifest, BENCHMARK, tmp_path / "out2", ledger, env_file=empty)


def test_the_suggest_step_loads_its_key_from_env_and_still_connects_directly(tmp_path, monkeypatch):
    for key in [k for k in os.environ if k.upper().endswith("_PROXY")] + ["DASHSCOPE_API_KEY"]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    (tmp_path / ".env").write_text(
        "DASHSCOPE_API_KEY=fixture-not-a-key\nHTTPS_PROXY=http://127.0.0.1:9\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(urllib.request, "_opener", None)

    _direct_only()

    assert os.environ["DASHSCOPE_API_KEY"] == "fixture-not-a-key"
    assert not [key for key in os.environ if key.upper().endswith("_PROXY")]
