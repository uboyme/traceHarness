"""F1 contribution contracts through the real activation and request owners."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, replace

import pytest
from plugin_fixtures import LoadCounter, ScriptedPlugin, entry_point_for, manifest, provider_for
from skill_fixtures import builder, contribution, digest, discovery, lease, policy

from traceh.api.json_types import canonical_json, fingerprint
from traceh.api.llm import ModelResponse
from traceh.api.plugins import PluginIdentity
from traceh.api.skills import SkillDescriptor, SkillSection
from traceh.kernel.composition import CompositionSnapshot, RuntimeComposition
from traceh.llm.registry import LlmRegistry
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.plugins.discovery import PluginDiscovery
from traceh.plugins.errors import PluginActivationError, PluginValidationError
from traceh.plugins.manager import PluginActivationSet
from traceh.plugins.skills import FrozenSkill
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime_async
from traceh.runtime.composition_runtime import CompositionGeneration
from traceh.runtime.request_builder import reconstruct_request, verify_request_snapshots
from traceh.session.event_store import InMemoryEventStore
from traceh.session.service import SessionService
from traceh.session.sqlite import SqliteEventStore
from traceh.tools.runtime import ToolRuntime


@pytest.mark.parametrize("subject", ["组件边界：正文只在选择后披露", "stellar scheduling body"])
@pytest.mark.parametrize("sqlite", [False, True])
async def test_enabled_catalog_never_injects_skill_body_or_grants_tools(tmp_path, subject, sqlite):
    value = contribution("catalog.author", "author.guide", subject)
    plugin = ScriptedPlugin(manifest("catalog.author"), skills=(value,))
    provider = ScriptedLlmProvider((ModelResponse(content="answer"),))
    store = SqliteEventStore(tmp_path / "events") if sqlite else InMemoryEventStore()
    runtime = await build_default_runtime_async(
        RuntimeConfig(data_dir=tmp_path / "data", skill_policy=policy()),
        provider=provider,
        event_store=store,
        include_default_tools=False,
        enabled_plugins=(plugin.manifest.plugin_id,),
        plugin_discovery=discovery(plugin),
    )
    try:
        async with lease(runtime, tmp_path) as active:
            assert active.skills.catalog == (value.descriptor,)
            assert (
                active.skills.read_section(value.descriptor.skill_id, "guide") == subject.encode()
            )
            assert active.snapshot.skill_catalog == (value.descriptor,)
            assert active.snapshot.skill_catalog_digest == fingerprint([value.descriptor.to_dict()])
            assert active.snapshot.tools == ()
            assert active.tools.registry.names() == ()
            assert subject not in active.snapshot.system_prompt
            with pytest.raises(FrozenInstanceError):
                active.skills.catalog[0].summary = "changed"
        with pytest.raises(RuntimeError, match="skill-lease-inactive"):
            active.skills.read_section(value.descriptor.skill_id, "guide")
        result = await runtime.run(tmp_path, "ordinary request")
        events = await runtime.sessions.read_session(result.session_id)
        request = next(e for e in events if e.type == "request/snapshot")
        composition = next(e for e in events if e.type == "composition/snapshot")
        context = next(e for e in events if e.type == "context/input")
        assert composition.data["skill_catalog"] == [value.descriptor.to_dict()]
        assert context.data["skill_catalog_digest"] == composition.data["skill_catalog_digest"]
        assert context.data["blocks"] == []
        assert {"kind": "skill", "id": None, "digest": None, "reason": "not-selected"} in (
            context.data["exclusions"]
        )
        assert subject not in canonical_json(provider.requests[0].to_dict())
        assert value.descriptor.summary not in canonical_json(provider.requests[0].to_dict())
        assert all(
            subject not in (message.content or "") for message in runtime.surface.project(events)
        )
        rebuilt = await reconstruct_request(
            runtime.sessions, runtime.surface, result.session_id, request
        )
        assert rebuilt.request == provider.requests[0]
        assert (
            await verify_request_snapshots(runtime.sessions, runtime.surface, result.session_id)
            == ()
        )
        assert runtime.invariants.check(events) == ()
    finally:
        await runtime.dispose()
        if sqlite:
            await store.aclose()
    assert plugin.cleanup_calls == 1


async def test_discovery_and_disabled_plugins_are_metadata_only():
    plugin = ScriptedPlugin(
        manifest("disabled.author"),
        skills=(contribution("disabled.author", "disabled.guide", "must remain absent"),),
    )
    counter = LoadCounter()
    source = PluginDiscovery(
        entry_points_provider=provider_for(entry_point_for(plugin, counter=counter))
    )
    assert source.discover()
    assert counter.loaded == []
    host = builder(skill_policy=policy())
    candidate = await host.prepare((), discovery=source)
    try:
        assert candidate.skill_catalog == ()
        assert counter.loaded == []
        assert plugin.setup_calls == 0
    finally:
        await candidate.dispose()


@pytest.mark.parametrize("phase", ["setup", "health", "conflict"])
async def test_failed_activation_rolls_back_skill_resources_and_all_other_contributions(
    phase, monkeypatch
):
    first = ScriptedPlugin(
        manifest("alpha.author"),
        skills=(contribution("alpha.author", "same.skill", "first body"),),
        has_health_check=True,
    )
    second = ScriptedPlugin(
        manifest("beta.author"),
        skills=(
            contribution(
                "beta.author", "same.skill" if phase == "conflict" else "other.skill", "next"
            ),
        ),
        setup_error=RuntimeError("setup failure") if phase == "setup" else None,
        health_result=False if phase == "health" else None,
        has_health_check=True,
    )
    closed = []
    original = FrozenSkill.close

    async def close(self):
        closed.append(self.descriptor.skill_id)
        await original(self)

    monkeypatch.setattr(FrozenSkill, "close", close)
    host = builder(first, second, skill_policy=policy())
    with pytest.raises((PluginActivationError, PluginValidationError)):
        await host.prepare(("alpha.author", "beta.author"))
    assert first.cleanup_calls == 1
    assert sorted(closed) == (
        ["same.skill"] if phase == "conflict" else ["other.skill", "same.skill"]
    )
    if phase != "health":
        assert first.health_calls == second.health_calls == 0
    assert host.tools.names() == ()
    assert host.prompt.sections() == ()
    candidate = await host.prepare(())
    assert candidate.skill_catalog == ()
    await candidate.dispose()


async def test_repeated_cancellation_converges_resource_rollback(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    closing, finish = asyncio.Event(), asyncio.Event()
    plugin = ScriptedPlugin(
        manifest("cancel.author"),
        skills=(contribution("cancel.author", "cancel.skill", "captured before cancellation"),),
        has_health_check=True,
        health_entered=entered,
        health_gate=release,
        spawn_forever=True,
    )
    closed = []
    original = FrozenSkill.close

    async def close(self):
        closing.set()
        await finish.wait()
        await original(self)
        closed.append(self.descriptor.skill_id)

    monkeypatch.setattr(FrozenSkill, "close", close)
    host = builder(plugin, skill_policy=policy())
    task = asyncio.create_task(host.prepare((plugin.manifest.plugin_id,)))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    await asyncio.wait_for(closing.wait(), 5)
    task.cancel()
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == ["cancel.skill"]
    assert plugin.cleanup_calls == 1
    assert plugin.owned_task.done() and plugin.owned_task_cancelled


@pytest.mark.parametrize("change", ["plugin", "digest", "sections", "compatibility", "policy"])
async def test_activation_rejects_invalid_identity_content_compatibility_and_absent_policy(change):
    value = contribution("valid.author", "valid.skill", "body")
    descriptor = value.descriptor
    if change == "plugin":
        descriptor = replace(descriptor, plugin=PluginIdentity("another.author", "1.0.0"))
    elif change == "digest":
        descriptor = replace(
            descriptor, sections=(SkillSection("guide", "section", digest("nope"), 4),)
        )
    elif change == "sections":
        value = replace(value, sections=())
    elif change == "compatibility":
        descriptor = replace(descriptor, requires_traceh="<0")
    value = replace(value, descriptor=descriptor)
    plugin = ScriptedPlugin(manifest("valid.author"), skills=(value,))
    host = builder(plugin, skill_policy=None if change == "policy" else policy())
    with pytest.raises(PluginActivationError):
        await host.prepare((plugin.manifest.plugin_id,))
    assert not plugin.skill_registrations
    assert host.prompt.sections() == ()


@pytest.mark.parametrize(
    "limit", ["max_skills", "max_catalog_bytes", "max_summary_bytes", "max_content_bytes"]
)
async def test_explicit_limits_cover_whole_candidate_not_only_individual_skills(limit):
    a = contribution("limits.author", "a.skill", "abcd")
    b = contribution("limits.author", "b.skill", "abcd")
    bounds = {
        "max_skills": 1,
        "max_catalog_bytes": len(canonical_json([a.descriptor.to_dict()]).encode()),
        "max_summary_bytes": 1,
        "max_content_bytes": 7,
    }
    plugin = ScriptedPlugin(manifest("limits.author"), skills=(a, b))
    with pytest.raises(PluginActivationError):
        await builder(plugin, skill_policy=policy(**{limit: bounds[limit]})).prepare(
            ("limits.author",)
        )
    assert all(reg.disposed for reg in plugin.skill_registrations)


async def test_registration_withdrawal_only_during_setup_and_read_only_after_publish(tmp_path):
    value = contribution("withdraw.author", "withdraw.skill", "body")

    class WithdrawPlugin(ScriptedPlugin):
        async def setup(self, context, config):
            registration = context.register_skill(value)
            await registration.dispose()
            await registration.dispose()
            await super().setup(context, config)

    plugin = WithdrawPlugin(manifest("withdraw.author"), skills=(value,))
    runtime = await build_default_runtime_async(
        RuntimeConfig(data_dir=tmp_path, skill_policy=policy()),
        event_store=InMemoryEventStore(),
        enabled_plugins=("withdraw.author",),
        plugin_discovery=discovery(plugin),
    )
    try:
        async with lease(runtime, tmp_path) as active:
            with pytest.raises(RuntimeError, match="closed after setup"):
                await plugin.skill_registrations[0].dispose()
            assert active.skills.read_section("withdraw.skill", "guide") == b"body"
    finally:
        await runtime.dispose()
    assert plugin.skill_registrations[0].disposed


def test_catalog_exact_shape_no_tool_grant_and_revision_binding():
    value = contribution("shape.author", "shape.skill", "body")
    descriptor = value.descriptor
    raw = descriptor.to_dict()
    assert SkillDescriptor.from_dict(raw) == descriptor
    for extra in ("tools", "allowed_tools", "root", "execute"):
        with pytest.raises(ValueError, match="keys-invalid"):
            SkillDescriptor.from_dict({**raw, extra: []})
    composition = RuntimeComposition(
        "scripted",
        "fixture",
        "system",
        (),
        plugins=(descriptor.plugin,),
        skill_catalog=(descriptor,),
    ).snapshot()
    assert CompositionSnapshot.from_dict(composition.to_dict()) == composition
    changed = replace(descriptor, summary="different summary")
    other = RuntimeComposition(
        "scripted", "fixture", "system", (), plugins=(descriptor.plugin,), skill_catalog=(changed,)
    ).snapshot()
    assert other.skill_catalog_digest != composition.skill_catalog_digest
    assert other.revision != composition.revision
    forged = composition.to_dict()
    forged["skill_catalog"] = [changed.to_dict()]
    with pytest.raises(ValueError, match="catalog-digest"):
        CompositionSnapshot.from_dict(forged)
    forged["skill_catalog_digest"] = other.skill_catalog_digest
    with pytest.raises(ValueError, match="revision-mismatch"):
        CompositionSnapshot.from_dict(forged)
    forged["revision"] = fingerprint({k: v for k, v in forged.items() if k != "revision"})
    forged["skill_catalog"][0]["tools"] = []
    with pytest.raises(ValueError, match="keys-invalid"):
        CompositionSnapshot.from_dict(forged)
    with pytest.raises(ValueError, match="plugin-mismatch"):
        RuntimeComposition("scripted", "fixture", "system", (), skill_catalog=(descriptor,))


async def test_candidate_skill_receipt_cannot_be_replaced_or_borrowed_from_other_activation(
    tmp_path,
):
    plugin = ScriptedPlugin(
        manifest("receipt.author"),
        skills=(contribution("receipt.author", "receipt.skill", "body"),),
    )
    runtime = await build_default_runtime_async(
        RuntimeConfig(data_dir=tmp_path),
        event_store=InMemoryEventStore(),
    )
    host = builder(plugin, skill_policy=policy())
    host.llms = LlmRegistry()
    host.llms.register(ScriptedLlmProvider((ModelResponse(content="unused"),)))
    candidate = await host.prepare(("receipt.author",))
    try:
        with pytest.raises(ValueError, match="owner-mismatch"):
            PluginActivationSet(
                tools=candidate.tools,
                prompt=candidate.prompt,
                services=host.services,
                identities=candidate.identities,
                activation_order=(),
                activations=(),
                skills=candidate._skills,
            )
        # Public candidate hand-off must verify its stored receipt. A replaced
        # resource tuple may not produce a successfully usable Generation.
        candidate._skills = ()
        with pytest.raises(ValueError, match="skill-receipt-invalid"):
            CompositionGeneration(
                llms=candidate.llms,
                tools=ToolRuntime(
                    candidate.tools, SessionService(InMemoryEventStore()), policies=()
                ),
                prompt=candidate.prompt,
                provider="scripted",
                model="scripted-model",
                plugins=candidate.identities,
                activation_set=candidate,
            )
    finally:
        await candidate.dispose()
        await runtime.dispose()
    assert plugin.cleanup_calls == 1
