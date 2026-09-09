"""F1 resource integrity, bounded reads and existing Lease/drain ownership."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace

import pytest
from plugin_fixtures import ScriptedPlugin, manifest
from skill_fixtures import builder, contribution, digest, discovery, lease, policy, with_resource

from traceh.api.plugins import PluginIdentity
from traceh.api.skills import SkillChunk, SkillResource, SkillResourceRoot
from traceh.plugins.errors import PluginActivationError
from traceh.plugins.skills import FrozenSkill
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime_async
from traceh.session.event_store import InMemoryEventStore


@pytest.mark.parametrize("body", ["首段：资源说明\n第二段", "gravity reference\nsecond part"])
async def test_reload_keeps_exact_old_resource_until_last_lease_and_cleans_once(
    tmp_path, monkeypatch, body
):
    root = tmp_path / "resources"
    root.mkdir()
    path = root / "guide.md"
    path.write_bytes(body.encode("utf-8"))
    identity = PluginIdentity("resources.author", "1.0.0")
    value = with_resource(
        contribution(identity.plugin_id, "resources.skill", "section body"), "guide.md", body
    )
    old = ScriptedPlugin(manifest(identity.plugin_id), skills=(value,))
    closed = []
    original = FrozenSkill.close

    async def close(self):
        closed.append(self.descriptor)
        await original(self)

    monkeypatch.setattr(FrozenSkill, "close", close)
    runtime = await build_default_runtime_async(
        RuntimeConfig(
            data_dir=tmp_path / "data", skill_policy=policy(SkillResourceRoot(identity, root))
        ),
        enabled_plugins=(identity.plugin_id,),
        plugin_discovery=discovery(old),
        event_store=InMemoryEventStore(),
    )
    first, second = lease(runtime, tmp_path), lease(runtime, tmp_path)
    old_active = await first.__aenter__()
    other_old = await second.__aenter__()
    try:
        assert old_active.skills.read_resource("resources.skill", "reference") == body.encode()
        next_body = "new frozen revision"
        path.write_text(next_body, encoding="utf-8")
        next_value = with_resource(value, "guide.md", next_body)
        new = ScriptedPlugin(manifest(identity.plugin_id), skills=(next_value,))
        await runtime.replace_plugin_composition(
            (identity.plugin_id,), plugin_discovery=discovery(new)
        )
        async with lease(runtime, tmp_path) as current:
            assert current.generation_id != old_active.generation_id
            assert current.snapshot.revision != old_active.snapshot.revision
            assert current.skills.catalog == (next_value.descriptor,)
            assert (
                current.skills.read_resource("resources.skill", "reference") == next_body.encode()
            )
            # Disk removal cannot affect either validated, bounded Generation.
            path.unlink()
            assert (
                current.skills.read_chunk("resources.skill", "reference", "whole")
                == next_body.encode()
            )
            assert (
                old_active.skills.read_chunk("resources.skill", "reference", "whole")
                == body.encode()
            )
            assert old_active.snapshot.skill_catalog == (value.descriptor,)
            assert closed == []
            assert old.cleanup_calls == 0
        await first.__aexit__(None, None, None)
        with pytest.raises(RuntimeError, match="lease-inactive"):
            old_active.skills.read_resource("resources.skill", "reference")
        assert other_old.skills.read_resource("resources.skill", "reference") == body.encode()
        assert closed == []
        await second.__aexit__(None, None, None)
        await runtime.loop.compositions.drain()
        assert closed == [value.descriptor]
        assert old.cleanup_calls == 1
        async with lease(runtime, tmp_path) as current:
            assert (
                current.skills.read_resource("resources.skill", "reference") == next_body.encode()
            )
            with pytest.raises(ValueError, match="not-declared"):
                current.skills.read_resource("resources.skill", "guide.md")
            with pytest.raises(ValueError, match="not-declared"):
                current.skills.read_chunk("resources.skill", "reference", "missing")
            with pytest.raises(ValueError, match="not-in-leased-catalog"):
                current.skills.read_section("missing.skill", "guide")
    finally:
        await first.__aexit__(None, None, None)
        await second.__aexit__(None, None, None)
        await runtime.dispose()
    await runtime.dispose()
    assert closed == [value.descriptor, next_value.descriptor]
    assert old.cleanup_calls == new.cleanup_calls == 1


async def test_cancelled_drain_waits_for_owned_resource_cleanup_before_return(
    tmp_path, monkeypatch
):
    plugin = ScriptedPlugin(
        manifest("drain.author"),
        skills=(contribution("drain.author", "drain.skill", "owned content"),),
    )
    runtime = await build_default_runtime_async(
        RuntimeConfig(data_dir=tmp_path / "data", skill_policy=policy()),
        enabled_plugins=(plugin.manifest.plugin_id,),
        plugin_discovery=discovery(plugin),
        event_store=InMemoryEventStore(),
    )
    entered, finish = asyncio.Event(), asyncio.Event()
    closed = []
    original = FrozenSkill.close

    async def close(self):
        entered.set()
        await finish.wait()
        await original(self)
        closed.append(self.descriptor.skill_id)

    monkeypatch.setattr(FrozenSkill, "close", close)
    old_lease = lease(runtime, tmp_path)
    active = await old_lease.__aenter__()
    await runtime.replace_plugin_composition(())
    assert active.skills.read_section("drain.skill", "guide") == b"owned content"
    await old_lease.__aexit__(None, None, None)
    task = asyncio.create_task(runtime.loop.compositions.drain())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        task.cancel()
        assert not task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed == ["drain.skill"]
        with pytest.raises(RuntimeError, match="lease-inactive"):
            active.skills.read_section("drain.skill", "guide")
    finally:
        finish.set()
        await runtime.dispose()
    assert plugin.cleanup_calls == 1


@pytest.mark.parametrize(
    "change",
    [
        "digest",
        "size",
        "chunk-digest",
        "chunk-utf8",
        "missing",
        "root-version",
        "root-absent",
        "per-resource-limit",
        "disk-limit",
        "invalid-utf8",
        "private-path",
    ],
)
async def test_activation_rejects_resource_mismatch_without_publishing(tmp_path, change):
    identity = PluginIdentity("validation.author", "1.0.0")
    body = "中文 content"
    path = tmp_path / "reference.md"
    path.write_bytes(body.encode("utf-8"))
    value = with_resource(
        contribution(identity.plugin_id, "validation.skill", "body"), path.name, body
    )
    resource = value.descriptor.resources[0]
    limits = policy(SkillResourceRoot(identity, tmp_path))
    if change == "digest":
        resource = replace(resource, content_digest=digest("wrong"))
    elif change == "size":
        resource = replace(resource, content_bytes=resource.content_bytes + 1)
    elif change == "chunk-digest":
        resource = replace(
            resource, chunks=(replace(resource.chunks[0], content_digest=digest("wrong")),)
        )
    elif change == "chunk-utf8":
        fragment = body.encode()[:1]
        resource = replace(
            resource,
            chunks=(
                SkillChunk(
                    "broken",
                    0,
                    1,
                    digest(fragment),
                    1,
                    title="Fixture navigation",
                    summary="Explicit fixture content description",
                ),
            ),
        )
    elif change == "missing":
        path.unlink()
    elif change == "root-version":
        limits = policy(SkillResourceRoot(PluginIdentity(identity.plugin_id, "2.0.0"), tmp_path))
    elif change == "root-absent":
        limits = policy()
    elif change == "per-resource-limit":
        limits = policy(SkillResourceRoot(identity, tmp_path), max_resource_bytes=1)
    elif change == "disk-limit":
        path.write_bytes(b"x" * (limits.limits.max_resource_bytes + 1))
    elif change == "invalid-utf8":
        path.write_bytes(b"\xff")
        resource = replace(resource, content_digest=digest(b"\xff"), content_bytes=1, chunks=())
    elif change == "private-path":
        # A rejected name only; no actual environment file is opened or made.
        resource = replace(resource, relative_path=".env.fixture")
    value = replace(value, descriptor=replace(value.descriptor, resources=(resource,)))
    plugin = ScriptedPlugin(manifest(identity.plugin_id), skills=(value,), has_health_check=True)
    host = builder(plugin, skill_policy=limits)
    with pytest.raises(PluginActivationError):
        await host.prepare((identity.plugin_id,))
    assert not plugin.skill_registrations
    assert plugin.health_calls == 0
    assert host.tools.names() == ()
    assert host.prompt.sections() == ()


@pytest.mark.parametrize(
    "path",
    [
        "../outside.md",
        "/absolute.md",
        "C:/outside.md",
        "./guide.md",
        "a//b.md",
        "a/../b.md",
        "a\\b.md",
        "a:stream",
        "guide.md ",
        "NUL",
        "folder/CON.txt",
        ".",
        "a/",
    ],
)
def test_descriptor_refuses_nonportable_or_escaping_paths(path):
    with pytest.raises(ValueError, match="path-invalid"):
        SkillResource(
            "resource",
            path,
            digest("body"),
            4,
            title="Fixture navigation",
            summary="Explicit fixture content description",
        )


async def test_resource_symlink_is_rejected_before_activation(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside resource", encoding="utf-8")
    try:
        os.symlink(outside, root / "link.md")
    except OSError as error:
        if os.name == "nt" and error.winerror == 1314:
            pytest.skip("Windows account lacks symlink privilege")
        raise
    identity = PluginIdentity("link.author", "1.0.0")
    value = with_resource(
        contribution(identity.plugin_id, "link.skill", "body"), "link.md", "outside resource"
    )
    plugin = ScriptedPlugin(manifest(identity.plugin_id), skills=(value,))
    with pytest.raises(PluginActivationError):
        await builder(plugin, skill_policy=policy(SkillResourceRoot(identity, root))).prepare(
            (identity.plugin_id,)
        )
    assert not plugin.skill_registrations


@pytest.mark.parametrize("offset", [-1, True])
def test_chunks_refuse_invalid_offsets(offset):
    with pytest.raises(ValueError):
        SkillChunk(
            "chunk",
            offset,
            4,
            digest("body"),
            4,
            title="Fixture navigation",
            summary="Explicit fixture content description",
        )


def test_chunk_identity_overlap_and_order_are_explicit():
    chunk = SkillChunk(
        "first",
        0,
        3,
        digest("abc"),
        3,
        title="Fixture navigation",
        summary="Explicit fixture content description",
    )
    for other in (
        chunk,
        SkillChunk(
            "next",
            2,
            4,
            digest("cd"),
            2,
            title="Fixture navigation",
            summary="Explicit fixture content description",
        ),
    ):
        with pytest.raises(ValueError):
            SkillResource(
                "resource",
                "guide.md",
                digest("abcd"),
                4,
                (chunk, other),
                title="Fixture navigation",
                summary="Explicit fixture content description",
            )
    later = SkillChunk(
        "later",
        3,
        4,
        digest("d"),
        1,
        title="Fixture navigation",
        summary="Explicit fixture content description",
    )
    with pytest.raises(ValueError):
        SkillResource(
            "resource",
            "guide.md",
            digest("abcd"),
            4,
            (later, chunk),
            title="Fixture navigation",
            summary="Explicit fixture content description",
        )
