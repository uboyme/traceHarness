"""Read-only observation of original retrieval owners for the explicit C3 screen."""

import contextvars
import json
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from skill_fixtures import discovery
from test_product_benchmark_e2e import PRODUCT_MODEL_ID, _ProductProvider

from traceh.evaluation.runner import ProductBenchmarkRunner
from traceh.session.retrieval import block_identity


async def capture(repository, output):
    import traceh.evaluation.attempt as attempt
    import traceh.memory.context as memory
    import traceh.session.context_input as context
    import traceh.session.retrieval as retrieval
    import traceh.session.skill_retrieval as skill

    sys.path.insert(0, str(repository / "examples/plugins/traceh-reference-skills/src"))
    from traceh_reference_skills import CurrentPlugin, RetiredPlugin

    plugins = discovery(CurrentPlugin(), RetiredPlugin())
    build = attempt.build_default_runtime_async
    freeze, memory_prepare, skill_prepare, rank = (
        context.ContextInputService.freeze,
        memory.prepare_corpus,
        skill.prepare_corpus,
        retrieval.rank,
    )
    active = contextvars.ContextVar("c3-observation", default=None)
    observations = []

    async def assemble(*args, **kwargs):
        return await build(*args, **kwargs, plugin_discovery=plugins)

    def observe_memory(source, policy):
        result = memory_prepare(source, policy)
        current = active.get()
        if current is not None:
            facts = {fact.memory_id: fact for fact in source.view.active}
            for block in result[1]:
                fact = facts[block["id"]]
                current["texts"][block_identity(block)] = " ".join(
                    (fact.memory_id, fact.fact_slot, fact.body)
                )
            current["corpora"].append(json.loads(result[0].manifest_json))
        return result

    def observe_skill(*args, **kwargs):
        result = skill_prepare(*args, **kwargs)
        current = active.get()
        if current is not None:
            for block, descriptor in zip(result[1], result[3], strict=True):
                current["texts"][block_identity(block)] = " ".join(
                    (descriptor.skill_id, descriptor.title, descriptor.summary, *descriptor.tags)
                )
            current["corpora"].append(json.loads(result[0].manifest_json))
        return result

    def observe_rank(blocks, rows, values, query, hits, policy):
        result = rank(blocks, rows, values, query, hits, policy)
        current = active.get()
        if current is not None:
            current["lanes"].append(
                {
                    "blocks": blocks,
                    "rows": rows,
                    "values": values,
                    "query": query,
                    "hits": hits,
                    "policy": policy.to_dict(),
                    "result": result,
                }
            )
        return result

    async def observe_freeze(self, **kwargs):
        current = {"texts": {}, "corpora": [], "lanes": []}
        token = active.set(current)
        try:
            snapshot = await freeze(self, **kwargs)
            current["context"] = snapshot.to_dict()
            observations.append(current)
            return snapshot
        finally:
            active.reset(token)

    with ExitStack() as patches:
        for owner, name, replacement in (
            (attempt, "build_default_runtime_async", assemble),
            (memory, "prepare_corpus", observe_memory),
            (skill, "prepare_corpus", observe_skill),
            (retrieval, "rank", observe_rank),
            (context.ContextInputService, "freeze", observe_freeze),
        ):
            patches.enter_context(patch.object(owner, name, replacement))
        runner = ProductBenchmarkRunner(
            repository / "benchmarks/retrieval_v1",
            output / "baseline",
            provider=_ProductProvider(),
            model_id=PRODUCT_MODEL_ID,
        )
        report = await runner.run()
    if len(report.attempts) != 11 or not all(
        item.success
        and item.retrieval
        and item.retrieval["quality_passed"]
        and item.retrieval["scope_violations"] == 0
        for item in report.attempts
    ):
        raise RuntimeError("c3-original-baseline-failed")
    (output / "capture.json").write_text(
        json.dumps(observations, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return observations


if __name__ == "__main__":
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    arguments.output.mkdir(parents=True, exist_ok=False)
    asyncio.run(capture(Path(__file__).resolve().parents[2], arguments.output))
