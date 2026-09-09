"""C3 diagnostic controls; no local models, network or new production retrieval lane."""

from dataclasses import replace

import pytest
from local_retrieval_screen.screen import digest, observation_for_query, select, verify
from test_memory_context import memory_case

from traceh.session.context_input import ContextInputPolicy


async def test_screen_uses_real_eligible_block_and_budget_without_mutating_session(tmp_path):
    async with memory_case(tmp_path) as (runtime, _, _, session, _, _):
        await runtime.memory.rebuild_index(session)
        await runtime.run_existing(session, "context-fact")
        events = await runtime.sessions.read_session(session)
        snapshot = next(e.data for e in events if e.type == "context/input")
        block = snapshot["blocks"][0]
        policy = ContextInputPolicy.from_dict(snapshot["policy"]["config"])
        candidate = {"cosine_min": 0.6, "reranker_min": None}

        def choose(query, score=0.9, *, base=None, limits=policy, cross=0.0, config=candidate):
            return select(base or [], [block], query, [score], [cross], config, 3, limits)

        assert choose("reproducibility") == [block]  # Zero literal coverage is permitted here.
        assert choose("reproducibility", score=0.2) == []
        assert choose("absent/source.py") == []  # Literal anchors remain binding.
        assert choose("reproducibility", limits=replace(policy, item_bytes=1)) == []
        assert choose("reproducibility", base=[block], score=0.2) == [block]
        assert (
            choose("reproducibility", config={**candidate, "reranker_min": 0.0}, cross=-1.0) == []
        )
        assert await runtime.sessions.read_session(session) == events
        assert runtime.invariants.check(events) == ()


@pytest.mark.parametrize("changed", ["source", "asset"])
def test_frozen_screen_rejects_source_or_asset_drift(tmp_path, changed):
    source = tmp_path / "source.py"
    source.write_text("value = 1\n", encoding="utf-8")
    asset_root = tmp_path / "model"
    asset_root.mkdir()
    asset = asset_root / "model.safetensors"
    asset.write_bytes(b"explicit fake asset for digest validation only")
    frozen = {
        "sources": {source.name: digest(source)},
        "assets": {"test": {"path": str(asset_root), "files": {asset.name: digest(asset)}}},
    }
    verify(tmp_path, frozen)
    (source if changed == "source" else asset).write_bytes(b"changed")
    with pytest.raises(ValueError, match="c3-frozen-.*-changed"):
        verify(tmp_path, frozen)


def test_capture_lookup_uses_original_normalized_query_and_rejects_ambiguity():
    observation = {"context": {"query": {"text": "src/alpha.py alphafault"}}}
    assert observation_for_query([observation], "src/alpha.py AlphaFault") is observation
    with pytest.raises(ValueError, match="observation-not-unique"):
        observation_for_query([], "src/alpha.py AlphaFault")
    with pytest.raises(ValueError, match="observation-not-unique"):
        observation_for_query([observation, observation], "src/alpha.py AlphaFault")
