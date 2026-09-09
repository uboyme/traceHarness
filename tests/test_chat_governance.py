"""F5 shared governance through real Runtime/Memory/Skill/History owners."""

import asyncio
import json
from dataclasses import replace

import pytest
from retrieval_fixtures import build_case
from test_history_runtime import policy, seed
from test_memory_context import BODY, memory_case

from traceh.api.llm import ModelResponse
from traceh.chat.config import load_context_host_file
from traceh.chat.governance import ChatGovernance, display
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.plugins.manager import PluginManager
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.runtime.prompt import PromptAssembler
from traceh.session.event_store import ConcurrencyConflict, InMemoryEventStore
from traceh.tools.registry import ToolRegistry


async def yes(_):
    return True


async def no(_):
    return False


async def test_memory_governance_fresh_read_revoke_and_frozen_context(tmp_path):
    async with memory_case(tmp_path) as (runtime, _, provider, session, _, _):
        await runtime.run_existing(session, "context-fact")
        control = ChatGovernance(runtime, session)
        original = await control.context()
        assert original["snapshot"]["blocks"][0]["body"] == BODY
        seen = []

        async def confirm(review):
            seen.append(review)
            return True

        result = await control.run("/memory revoke context-fact reviewer", confirm=confirm)
        assert result["status"] == "revoke"
        assert seen[0]["body"] == BODY and seen[0]["project_id"] == "context-project"
        assert (await control.memory())["active"] == []
        assert await control.context() == original
        assert len(provider.requests) == 1


async def test_memory_declared_proposed_approved_superseded(tmp_path):
    async with memory_case(tmp_path) as (runtime, _, _, session, _, _):
        control = ChatGovernance(runtime, session)
        for action in ('/memory declare reviewer "A newer approved boundary"',):
            await control.run(action, confirm=yes)
        view = await control.memory()
        proposal = view["proposals"][-1]["proposal_id"]
        assert len(view["active"]) == 1
        with pytest.raises(ValueError, match="slot-occupied"):
            await control.run(
                f"/memory approve {proposal} project-boundaries fresh reviewer", confirm=yes
            )
        await control.run(
            f"/memory supersede {proposal} project-boundaries fresh reviewer", confirm=yes
        )
        assert (await control.memory())["active"][0]["memory_id"] == "fresh"
        await control.run('/memory declare reviewer "A separate fact"', confirm=yes)
        proposal = (await control.memory())["proposals"][-1]["proposal_id"]
        await control.run(f"/memory approve {proposal} separate second reviewer", confirm=yes)
        assert len((await control.memory())["active"]) == 2


async def test_memory_cancel_and_stale_confirmation_do_not_overwrite(tmp_path):
    async with memory_case(tmp_path) as (runtime, store, _, session, _, _):
        control = ChatGovernance(runtime, session)
        command = "/memory revoke context-fact reviewer"
        assert (await control.run(command, confirm=no))["status"] == "cancelled"
        assert await store.head("memory:context-project") == 2
        reached, release = asyncio.Event(), asyncio.Event()

        async def blocked(_):
            reached.set()
            await release.wait()
            return True

        task = asyncio.create_task(control.run(command, confirm=blocked))
        await reached.wait()
        await control.run('/memory declare reviewer "Concurrent proposal"', confirm=yes)
        release.set()
        with pytest.raises(ConcurrencyConflict):
            await task
        assert len((await control.memory())["active"]) == 1
        reached.clear()
        release.clear()
        task = asyncio.create_task(control.run(command, confirm=blocked))
        await reached.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await store.head("memory:context-project") == 3


async def test_skill_selection_is_separate_from_last_retrieval(tmp_path):
    runtime, store, provider, session, values = await build_case(tmp_path)
    try:
        control = ChatGovernance(runtime, session)
        await control.run("/skills select reviewer boundary.notes", confirm=yes)
        await runtime.run_existing(session, "boundary.notes")
        view = await control.skills()
        assert view["eligible"] == ["boundary.notes"]
        assert view["last_retrieved"][0]["id"] == "boundary.notes"
        await control.run("/skills clear reviewer", confirm=yes)
        view = await control.skills()
        assert view["eligible"] == [] and view["last_retrieved"]
        assert len(provider.requests) == 1
        with pytest.raises(ValueError, match="identity-invalid"):
            await control.run("/skills select reviewer unknown", confirm=yes)

        from plugin_fixtures import ScriptedPlugin, manifest
        from skill_fixtures import contribution, discovery

        replacement = ScriptedPlugin(
            manifest("reference.author"),
            skills=(
                contribution(
                    "reference.author", "boundary.notes", "Different content under the same version"
                ),
                values[1],
            ),
        )
        reviewed_digest = view["catalog_digest"]

        async def change_catalog(_):
            await runtime.migrate_session_plugin_composition(
                session, ("reference.author",), plugin_discovery=discovery(replacement)
            )
            assert (await runtime.skill_context.read(session))["catalog_digest"] != reviewed_digest
            return True

        with pytest.raises(ValueError, match="review-stale"):
            await control.run("/skills select reviewer boundary.notes", confirm=change_catalog)
        assert (await runtime.skill_context.read(session))["selection_head"] == 2
    finally:
        await runtime.dispose()
        await store.aclose()


async def test_history_page_and_context_read_do_not_write_or_call_model(tmp_path):
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data", context_input=policy()),
        provider=ScriptedLlmProvider((ModelResponse(content="old answer"),)),
        event_store=InMemoryEventStore(),
    )
    try:
        session = await seed(runtime, tmp_path, "原始观察 exact original")
        control = ChatGovernance(runtime, session)
        before = await runtime.sessions.read_session(session)
        directory = await control.history()
        block = directory["history"][0]
        assert block["freshness"] == "unknown" and block["original_bytes"] > 0
        page = await control.run(f"/history page {block['block_id']} 0", confirm=yes)
        assert "原始观察" in page["history"][0]["page"]["body"]
        assert await runtime.sessions.read_session(session) == before
        with pytest.raises(ValueError):
            await control.history("f" * 64, 0)
    finally:
        await runtime.dispose()


def test_plugin_review_does_not_setup_and_reuses_exact_manifest():
    from plugin_fixtures import ScriptedPlugin, manifest
    from skill_fixtures import discovery

    plugin = ScriptedPlugin(manifest("review.plugin"))
    manager = PluginManager(
        tools=ToolRegistry(), prompt=PromptAssembler(), discovery=discovery(plugin)
    )
    rows, reviewed = manager.review_enable(("review.plugin",))
    assert rows[0]["state"] == "loaded"
    assert rows[0]["manifest"]["trust_mode"] == "trusted"
    assert plugin.setup_calls == 0
    assert reviewed.discover()[0].entry_point.load() is plugin
    plugin.manifest = replace(plugin.manifest, version="9.1.0")
    with pytest.raises(ValueError, match="review-stale"):
        reviewed.discover()[0].entry_point.load()


def test_host_config_uses_same_policy_reader_and_rejects_extras(tmp_path):
    raw = {"format": 1, "context": policy().to_dict(), "skill_policy": None, "project": None}
    path = tmp_path / "context.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert load_context_host_file(path).context == policy()
    raw["context"]["local_lanes"]["semantic"] = "not-selected"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="lane-unsupported"):
        load_context_host_file(path)


def test_display_neutralizes_terminal_and_bidi_controls():
    rendered = display({"body": "中文\x1b[2J\u202edanger"})
    assert "中文" in rendered and "\x1b" not in rendered and "\u202e" not in rendered


async def test_plugin_confirmation_activates_only_after_review_and_rolls_back_failure(tmp_path):
    from plugin_fixtures import ScriptedPlugin, manifest
    from skill_fixtures import discovery

    from traceh.runtime.agent_runtime import build_default_runtime_async

    first = ScriptedPlugin(manifest("review.first"))
    failing = ScriptedPlugin(manifest("review.failed"), setup_error=ValueError("setup failed"))
    provider = ScriptedLlmProvider((ModelResponse(content="unused"),))
    runtime = await build_default_runtime_async(
        RuntimeConfig(data_dir=tmp_path),
        provider=provider,
        event_store=InMemoryEventStore(),
        plugin_discovery=discovery(first, failing),
    )
    try:
        session = await runtime.create_session(tmp_path)
        control = ChatGovernance(runtime, session, discovery=discovery(first, failing))

        async def review(value):
            assert first.setup_calls == 0
            assert value["plugins"][0]["manifest"]["plugin_id"] == "review.first"
            return True

        await control.run("/plugins use review.first", confirm=review)
        assert first.setup_calls == 1 and runtime.enabled_plugin_ids == ("review.first",)
        with pytest.raises(RuntimeError):
            await control.run("/plugins use review.failed", confirm=yes)
        assert failing.setup_calls == 1
        assert runtime.enabled_plugin_ids == ("review.first",)
        assert provider.requests == []
    finally:
        await runtime.dispose()


async def test_project_commands_use_explicit_scope_and_original_catalog_head(tmp_path):
    async with memory_case(tmp_path) as (runtime, _, _, session, _, _):
        control = ChatGovernance(runtime, session)
        before = (await runtime.project_scope.catalog()).head
        assert (await control.run('/project create fresh reviewer "新项目"', confirm=no))[
            "status"
        ] == "cancelled"
        assert (await runtime.project_scope.catalog()).head == before
        await control.run('/project create fresh reviewer "新项目"', confirm=yes)
        with pytest.raises(ValueError):
            await control.run("/project source fresh context-source reviewer", confirm=yes)
        with pytest.raises(ValueError):
            await control.run("/project bind missing-project reviewer", confirm=yes)
        catalog = await control.run("/project", confirm=yes)
        assert "fresh" in str(catalog) and "新项目" in str(catalog)


def test_context_host_file_resolves_explicit_sources_and_skill_roots(tmp_path):
    from dataclasses import asdict

    from memory_fixtures import memory_policy
    from retrieval_fixtures import context_policy, retrieval_policy
    from skill_fixtures import policy as skill_policy

    mp = asdict(memory_policy())
    mp["denied_patterns"] = list(mp["denied_patterns"])
    raw = {
        "format": 1,
        "context": context_policy(memory=retrieval_policy()).to_dict(),
        "skill_policy": {
            "limits": asdict(skill_policy().limits),
            "resource_roots": [
                {
                    "plugin": {"plugin_id": "explicit.plugin", "version": "1.0.0"},
                    "path": "resources",
                }
            ],
        },
        "project": {
            "sources": {"explicit-source": "source"},
            "managed_root": "work",
            "limits": {"max_catalog_events": 200, "max_label_bytes": 200},
            "memory_policy": mp,
        },
    }
    path = tmp_path / "context.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    settings = load_context_host_file(path)
    assert settings.sources == (("explicit-source", tmp_path / "source"),)
    assert settings.skill_policy.resource_roots[0].path == tmp_path / "resources"
    assert settings.memory.memory_policy == memory_policy()
    raw["project"] = None
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="memory-authority-required"):
        load_context_host_file(path)
