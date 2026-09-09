"""F5 frozen inputs enter the single real Product benchmark attempt owner."""

import asyncio
import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import pytest
from memory_fixtures import memory_policy
from retrieval_fixtures import context_policy, retrieval_policy
from test_product_benchmark_e2e import (
    PRODUCT_MODEL_ID,
    _ProductProvider,
    build_benchmark,
)

from traceh.api.json_types import fingerprint
from traceh.evaluation.errors import BenchmarkManifestError
from traceh.evaluation.manifest import load_benchmark_manifest
from traceh.evaluation.retrieval import score_blocks
from traceh.evaluation.runner import ProductBenchmarkRunner

REPOSITORY = Path(__file__).resolve().parents[1]


def benchmark(root, *, query="goals.code", relevant=True, category="exact"):
    build_benchmark(root, arms=(("single", 1),))
    manifest = json.loads((root / "benchmark.json").read_text(encoding="utf-8"))
    manifest["tasks"][0]["requirement"] = query
    memory = asdict(memory_policy())
    memory["denied_patterns"] = list(memory["denied_patterns"])
    identity = {"kind": "memory", "id": "goals.code", "tiers": ["summary"]}
    inactive = ["other.goals", "revoked.goals", "old.goals"]
    spec = {
        "format": 1,
        "context": context_policy(skills=None, memory=retrieval_policy()).to_dict(),
        "project_limits": {"max_catalog_events": 200, "max_label_bytes": 200},
        "memory_policy": memory,
        "memories": [
            {
                "id": "goals.code",
                "scope": "current",
                "slot": "project-goals",
                "body": "项目目标保持边界清楚。Create added.txt with added followed by a newline.",
                "status": "active",
                "successor": None,
            },
            {
                "id": "other.goals",
                "scope": "foreign",
                "slot": "project-goals",
                "body": "Foreign project scope evidence.",
                "status": "active",
                "successor": None,
            },
            {
                "id": "revoked.goals",
                "scope": "current",
                "slot": "former-goals",
                "body": "Revoked planning evidence.",
                "status": "revoked",
                "successor": None,
            },
            {
                "id": "old.goals",
                "scope": "current",
                "slot": "phase",
                "body": "Old phase evidence.",
                "status": "superseded",
                "successor": {"id": "new.phase", "body": "Current delivery phase."},
            },
        ],
        "plugins": {
            "enabled": [],
            "retired": [],
            "catalog_digest": fingerprint([]),
            "selected": [],
            "limits": None,
        },
        "judgments": [
            {
                "task_id": manifest["tasks"][0]["task_id"],
                "role": "requester",
                "query": query,
                "category": category,
                "relevant": [identity] if relevant else [],
                "forbidden": [{"kind": "memory", "id": i, "tiers": ["summary"]} for i in inactive],
            }
        ],
        "evaluator": {
            "k": 3,
            "thresholds": {category: {"recall": 1, "mrr": 1, "precision": 1, "zero_hit": 1}},
            "max_context_bytes": 30000,
            "max_context_prepare_ms": 120000,
        },
    }
    write_spec(root, manifest, spec)
    return manifest, spec


def write_spec(root, manifest, spec):
    data = json.dumps(spec, ensure_ascii=False, indent=2).encode("utf-8")
    (root / "corpus.json").write_bytes(data)
    manifest["retrieval"] = {"file": "corpus.json", "sha256": hashlib.sha256(data).hexdigest()}
    (root / "benchmark.json").write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.parametrize(
    ("query", "relevant", "category"),
    [
        ("goals.code", True, "exact"),
        ("项目目标保持边界清楚", True, "lexical"),
        ("unmatched-stellar-question", False, "zero-hit"),
    ],
)
async def test_frozen_corpus_is_seeded_before_host_and_measured_from_real_steps(
    tmp_path,
    query,
    relevant,
    category,
):
    root = tmp_path / "benchmark"
    benchmark(root, query=query, relevant=relevant, category=category)
    provider = _ProductProvider(requests=[])
    runner = ProductBenchmarkRunner(
        root, tmp_path / "out", provider=provider, model_id=PRODUCT_MODEL_ID
    )
    report = await runner.run()
    result = report.attempts[0].retrieval
    assert result is not None, report.attempts[0].error_code
    assert result["quality_passed"] is True, result
    assert result["expected_judgments"] == 1 and not result["unproven"]
    measured = [r for r in result["observations"] if r["status"] == "measured"]
    assert len(measured) == 1 and measured[0]["metrics"]["scope_violations"] == 0
    assert result["seed"]["before_product_host"] is True
    assert result["seed"]["bindings"]["current"] != result["seed"]["bindings"]["foreign"]
    assert not list((tmp_path / "out" / "attempts" / "001" / "source").rglob("corpus.json"))
    assert all("judgments" not in str(request.to_dict()) for request in provider.requests)
    assert report.attempts[0].evidence is not None
    assert report.attempts[0].success
    assert any(row["role"] == "coder" for row in result["observations"])
    assert result["scope_violations"] == 0
    assert result["seed"]["indexes"][0]["manifest"]["item_count"] > 0
    assert result["seed"]["indexes"][0]["manifest"]["item_bytes"] > 0


@pytest.mark.parametrize("mutation", ["digest", "escape", "extra", "old", "threshold", "inside"])
def test_invalid_frozen_inputs_fail_before_creating_attempt(tmp_path, mutation):
    root = tmp_path / "benchmark"
    manifest, spec = benchmark(root)
    if mutation == "digest":
        manifest["retrieval"]["sha256"] = "0" * 64
    elif mutation == "escape":
        manifest["retrieval"]["file"] = "../corpus.json"
    elif mutation == "old":
        manifest["protocol_version"] = 1
    elif mutation == "inside":
        target = root / manifest["tasks"][0]["initial_dir"] / "corpus.json"
        target.write_bytes((root / "corpus.json").read_bytes())
        manifest["retrieval"]["file"] = target.relative_to(root).as_posix()
    else:
        if mutation == "extra":
            spec["implicit_model"] = "disabled"
        else:
            spec["evaluator"]["thresholds"]["exact"]["recall"] = 2
        write_spec(root, manifest, spec)
    (root / "benchmark.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BenchmarkManifestError):
        load_benchmark_manifest(root, provider_id="scripted", model_id="explicit-test")


async def test_frozen_file_drift_after_load_is_not_seeded(tmp_path):
    root = tmp_path / "benchmark"
    benchmark(root)
    runner = ProductBenchmarkRunner(
        root, tmp_path / "out", provider=_ProductProvider(), model_id=PRODUCT_MODEL_ID
    )
    (root / "corpus.json").write_text("{}", encoding="utf-8")
    result = await runner.run()
    assert result.attempts[0].error_code == "retrieval-frozen-input-changed"
    assert result.attempts[0].evidence is None


def test_metrics_count_unique_injected_identities_and_forbidden_beyond_k():
    item = {"kind": "memory", "id": "fact", "tier": "summary"}
    judge = {
        "relevant": [{"kind": "memory", "id": "fact", "tiers": ["summary"]}],
        "forbidden": [{"kind": "memory", "id": "foreign", "tiers": ["summary"]}],
    }
    result = score_blocks([item, item, {**item, "id": "foreign"}], judge, 1)
    assert result == {
        "recall": 1,
        "mrr": 1,
        "precision": 1,
        "context_precision": 0.5,
        "zero_hit": None,
        "scope_violations": 1,
    }


def test_relevance_uses_any_injected_tier_without_duplicate_rank():
    judge = {"relevant": [{"kind": "skill", "id": "guide", "tiers": ["section"]}], "forbidden": []}
    assert (
        score_blocks(
            [
                {"kind": "skill", "id": "guide", "tier": "directory"},
                {"kind": "memory", "id": "noise", "tier": "summary"},
                {"kind": "skill", "id": "guide", "tier": "section"},
            ],
            judge,
            1,
        )["mrr"]
        == 1
    )


@pytest.mark.parametrize("cancel", [False, True])
async def test_seed_failure_and_cancel_converge_after_real_approval(tmp_path, monkeypatch, cancel):
    import traceh.evaluation.attempt as attempt
    from traceh.runtime.memory_control import MemoryControl
    from traceh.session.sqlite import SqliteEventStore

    root = tmp_path / "benchmark"
    benchmark(root)
    reached = asyncio.Event()
    runtimes, approved, hosts, disposed = [], [], [], []
    build, approve, host = (
        attempt.build_default_runtime_async,
        MemoryControl.approve,
        attempt.build_product_chat_host,
    )

    async def capture(*args, **kwargs):
        runtime = await build(*args, **kwargs)
        runtimes.append(runtime)
        original_dispose = runtime.dispose

        async def dispose():
            await original_dispose()
            disposed.append(True)

        monkeypatch.setattr(runtime, "dispose", dispose)
        return runtime

    async def fail_after_approve(self, *args, **kwargs):
        approved.append(await approve(self, *args, **kwargs))
        reached.set()
        if cancel:
            await asyncio.Event().wait()
        raise ValueError("seed-approval-followup-failed")

    async def observe_host(**kwargs):
        hosts.append(True)
        return await host(**kwargs)

    monkeypatch.setattr(attempt, "build_default_runtime_async", capture)
    monkeypatch.setattr(attempt, "build_product_chat_host", observe_host)
    monkeypatch.setattr(MemoryControl, "approve", fail_after_approve)
    runner = ProductBenchmarkRunner(
        root, tmp_path / "out", provider=_ProductProvider(), model_id=PRODUCT_MODEL_ID
    )
    task = asyncio.create_task(runner.run())
    await asyncio.wait_for(reached.wait(), 30)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        report = await task
        assert report.attempts[0].error_code == "benchmark-retrieval-preparation-failed"
        assert report.attempts[0].retrieval["expected_judgments"] == 1
        assert len(report.attempts[0].retrieval["unproven"]) == 1
    assert approved and not hosts and disposed == [True]
    with pytest.raises(RuntimeError, match="event-store-closed"):
        await runtimes[0].create_session(tmp_path)
    store = SqliteEventStore(tmp_path / "out" / "attempts" / "001" / "ev")
    try:
        assert approved[0] in await store.read(approved[0].stream_id)
    finally:
        await store.aclose()


async def test_shipped_frozen_baseline_uses_real_plugin_lifecycle(tmp_path, monkeypatch):
    """One frozen grid, real owners; source import substitutes only installed metadata."""
    from skill_fixtures import discovery

    import traceh.evaluation.attempt as attempt

    repository = REPOSITORY
    monkeypatch.syspath_prepend(str(repository / "examples/plugins/traceh-reference-skills/src"))
    from traceh_reference_skills import CurrentPlugin, RetiredPlugin

    current, retired = CurrentPlugin(), RetiredPlugin()
    calls = []
    for plugin in (current, retired):
        original = plugin.setup

        async def tracked(context, config, original=original, plugin=plugin):
            await original(context, config)
            calls.append(plugin.manifest.plugin_id)

        monkeypatch.setattr(plugin, "setup", tracked)
    build = attempt.build_default_runtime_async

    async def assemble(*args, **kwargs):
        return await build(*args, **kwargs, plugin_discovery=discovery(current, retired))

    monkeypatch.setattr(attempt, "build_default_runtime_async", assemble)
    runner = ProductBenchmarkRunner(
        repository / "benchmarks/retrieval_v1",
        tmp_path / "out",
        provider=_ProductProvider(),
        model_id=PRODUCT_MODEL_ID,
    )
    report = await runner.run()
    assert len(report.attempts) == 11
    assert len(calls) >= 22  # Both contributions actually registered before retirement.
    for attempt_report in report.attempts:
        result = attempt_report.retrieval
        assert attempt_report.success, attempt_report.error_code
        rows = [r for r in result["observations"] if r["status"] == "measured"]
        assert len(rows) == 1 and not result["unproven"]
        row = rows[0]
        bound = runner.manifest.retrieval.data["evaluator"]["thresholds"][row["category"]]
        expected = all(
            row["metrics"][key] is None or row["metrics"][key] >= value
            for key, value in bound.items()
        )
        assert result["quality_passed"] is expected
        assert result["quality_passed"] is True, (row["category"], row["metrics"])
        assert result["seed"]["retired_skill_ids"] == ["reference.retired"]
        assert result["scope_violations"] == 0
    assert {
        r["category"]
        for a in report.attempts
        for r in a.retrieval["observations"]
        if r["status"] == "measured"
    } == set(runner.manifest.retrieval.data["evaluator"]["thresholds"])
