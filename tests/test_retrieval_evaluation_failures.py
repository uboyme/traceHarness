"""F5 negative evidence through production Context and Product owners."""

import asyncio
import json

import pytest
from test_memory_context import memory_case
from test_product_benchmark_e2e import PRODUCT_MODEL_ID, _ProductProvider
from test_retrieval_evaluation import benchmark, write_spec

from traceh.evaluation.evaluators.product_manifest import load_product_suite
from traceh.evaluation.manifest import load_benchmark_manifest
from traceh.evaluation.retrieval import collect_retrieval
from traceh.evaluation.runner import EvaluationRunner
from traceh.session.service import SessionService
from traceh.session.sqlite import SqliteEventStore
from traceh.workspaces.catalog import WorkspaceCatalogReader


async def test_unjudged_dispatched_step_still_checks_isolation_and_missing_denominator(tmp_path):
    root = tmp_path / "benchmark"
    manifest, spec = benchmark(root)
    spec["evaluator"]["thresholds"] = None
    write_spec(root, manifest, spec)
    frozen = load_product_suite(
        load_benchmark_manifest(root), provider_id="scripted", model_id="explicit"
    ).retrieval
    async with memory_case(tmp_path) as (runtime, _, _, session, _, _):
        await runtime.run_existing(session, "context-fact")
        result = await collect_retrieval(
            runtime.sessions,
            frozen,
            session_roles={session: "coder"},
            task_id=json.loads((root / "dataset.json").read_text(encoding="utf-8"))["cases"][0][
                "case_id"
            ],
            seed_receipt={
                "forbidden": [{"kind": "memory", "id": "context-fact"}],
                "bindings": {"current": {"project_id": "context-project"}},
            },
        )
        assert result["observations"][0]["status"] == "unjudged"
        assert result["scope_violations"] == 1
        assert result["quality_passed"] is None
        assert result["expected_judgments"] == 1 and len(result["unproven"]) == 1


@pytest.mark.parametrize("cancel", [False, True])
async def test_product_index_preparation_failure_converges_attached_worktree(
    tmp_path,
    monkeypatch,
    cancel,
):
    import traceh.evaluation.attempt as attempt

    root = tmp_path / "benchmark"
    benchmark(root)
    provider = _ProductProvider(requests=[])
    host_built = False
    reached = asyncio.Event()
    release = asyncio.Event()
    original_host, original_rebuild = (
        attempt.build_product_chat_host,
        SessionService.rebuild_context_index,
    )

    async def host(**kwargs):
        nonlocal host_built
        result = await original_host(**kwargs)
        host_built = True
        return result

    async def rebuild(self, corpus):
        await original_rebuild(self, corpus)
        if host_built:
            reached.set()
            if cancel:
                await release.wait()
                return
            raise ValueError("injected-product-index-preparation-failure")

    monkeypatch.setattr(attempt, "build_product_chat_host", host)
    monkeypatch.setattr(SessionService, "rebuild_context_index", rebuild)
    runner = EvaluationRunner(root, tmp_path / "out", provider=provider, model_id=PRODUCT_MODEL_ID)
    task = asyncio.create_task(runner.run())
    await asyncio.wait_for(reached.wait(), 60)
    if cancel:
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        report = (await task).task_report
        assert not report.attempts[0].success
    assert provider.requests == []
    store = SqliteEventStore(tmp_path / "out" / "attempts" / "001" / "ev")
    try:
        catalog = await WorkspaceCatalogReader(store).load()
        assert catalog.workspaces
        events = await store.read("workspaces:catalog")
        assert any(e.type == "workspace/attached" for e in events)
        assert any(e.type == "workspace/released" for e in events)
    finally:
        await store.aclose()
