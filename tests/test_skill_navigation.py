"""Directory contracts through actual frozen requests; model behavior is synthetic here.

Real model evidence is produced separately by tests/live_skill_navigation/run.py.
"""

import asyncio
import json
from dataclasses import replace

import pytest
from plugin_fixtures import ScriptedPlugin, manifest
from retrieval_fixtures import build_case, context_policy, retrieval_policy, select
from skill_fixtures import builder, contribution, digest, policy, with_resource

from traceh.api.json_types import canonical_json
from traceh.api.llm import ModelRequest, ModelResponse, ToolCall
from traceh.api.skills import SkillDescriptor, SkillResourceRoot, SkillSection, SkillSectionContent
from traceh.kernel.composition import CompositionSnapshot, RuntimeComposition
from traceh.plugins.errors import PluginActivationError
from traceh.runtime.request_builder import reconstruct_request, verify_request_snapshots


def navigation_skill():
    value = contribution("reference.author", "maintenance.manual", "placeholder")
    sections = (
        ("a192", "替换登记", "仅在更换部件后登记。", "REPLACEMENT ONLY 829"),
        ("f863", "离线标定", "停机后的校准步骤；不用于在线校准。", "OFFLINE BODY 731"),
    )
    descriptor = replace(
        value.descriptor,
        title="维护校准",
        summary="maintenance calibration procedure",
        tags=(),
        sections=tuple(
            SkillSection(
                sid,
                "section",
                digest(body),
                len(body.encode("utf-8")),
                title=title,
                summary=summary,
            )
            for sid, title, summary, body in sections
        ),
    )
    value = replace(
        value,
        descriptor=descriptor,
        sections=tuple(SkillSectionContent(sid, body) for sid, _, _, body in sections),
    )
    value = with_resource(value, "appendix.txt", "TABLE BODY 682")
    resource = value.descriptor.resources[0]
    resource = replace(
        resource,
        title="参数附表",
        summary="按校准场景分块。",
        chunks=(
            replace(
                resource.chunks[0], chunk_id="c517", title="离线参数", summary="停机校准的参数表。"
            ),
        ),
    )
    return replace(value, descriptor=replace(value.descriptor, resources=(resource,)))


class DirectoryClient:
    """Deterministic protocol probe. IDs are obtained only from Provider-bound navigation."""

    name = "scripted"

    def __init__(self, tier, *, gate=None):
        self.tier = tier
        self.requests = []
        self.gate = gate

    async def complete(self, request):
        self.requests.append(request)
        blocks = json.loads(request.messages[-1].content.split("\n")[1])
        if any(b["tier"] == self.tier for b in blocks):
            assert all(
                b["body_status"] == "source-content" for b in blocks if b["tier"] == self.tier
            )
            if self.gate is not None:
                self.gate.set()
                await asyncio.Event().wait()
            return ModelResponse(content="read requested body")
        directory = next((b for b in blocks if b["tier"] == "directory"), None)
        chosen = directory or next((b for b in blocks if b["kind"] == "skill"), None)
        if chosen is None:
            return ModelResponse(content="no available Skill")
        assert chosen.get("body_status") == "navigation-only"
        if chosen["tier"] == "summary":
            action = chosen["read_action"]
            return ModelResponse(
                tool_calls=(
                    ToolCall(
                        f"read-{len(self.requests)}", action["tool_name"], action["arguments"]
                    ),
                )
            )
        args = {
            "skill_id": chosen["id"],
            "version": chosen["version"],
            "catalog_digest": chosen["catalog_digest"],
            "requested_tier": "directory",
            "section_id": None,
            "resource_id": None,
            "chunk_id": None,
        }
        if directory:
            navigation = json.loads(directory["body"])
            if self.tier == "section":
                target = next(
                    (
                        s
                        for s in navigation.get("sections", [])
                        if isinstance(s, dict) and s.get("title") == "离线标定"
                    ),
                    None,
                )
                if target is None:
                    return ModelResponse(content="directory cannot identify the chapter")
                args.update(requested_tier="section", section_id=target["section_id"])
            else:
                target = next(
                    (
                        (r, c)
                        for r in navigation.get("resources", [])
                        for c in r["chunks"]
                        if isinstance(c, dict) and c.get("title") == "离线参数"
                    ),
                    None,
                )
                if target is None:
                    return ModelResponse(content="directory cannot identify the chunk")
                args.update(
                    requested_tier="chunk",
                    resource_id=target[0]["resource_id"],
                    chunk_id=target[1]["chunk_id"],
                )
        return ModelResponse(
            tool_calls=(
                ToolCall(
                    f"read-{len(self.requests)}",
                    "request_skill_reference",
                    args,
                ),
            )
        )


async def test_skill_summary_discloses_usable_directory_action_without_body(tmp_path):
    from test_history_runtime import SelectingProvider, items

    def read_directory(request):
        block = next(b for b in items(request) if b["kind"] == "skill")
        action = block.get("read_action")
        if action is None:
            return ModelResponse(content="directory action unavailable")
        return ModelResponse(
            tool_calls=(ToolCall("directory", action["tool_name"], action["arguments"]),)
        )

    provider = SelectingProvider([read_directory, ModelResponse(content="directory read")])
    runtime, store, _, session, values = await build_case(tmp_path, provider=provider)
    try:
        await select(runtime, session, values[0])
        result = await runtime.run_existing(session, "architecture")
        block = next(b for b in items(provider.requests[0]) if b["kind"] == "skill")
        assert block.get("body_status") == "navigation-only"
        assert result.final_text == "directory read"
        assert any(b["tier"] == "directory" for b in items(provider.requests[-1]))
        assert "SECTION ORIGINAL" not in canonical_json([r.to_dict() for r in provider.requests])
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, session)
        assert not await runtime.check_invariants(session)
    finally:
        await runtime.dispose()
        await store.aclose()


@pytest.mark.parametrize("tier", ["section", "chunk"])
@pytest.mark.parametrize("unused_value", ["null", "unused-placeholder"])
async def test_unused_id_strings_fail_without_disclosure_then_corrected_request_succeeds(
    tmp_path, tier, unused_value
):
    class CorrectingClient(DirectoryClient):
        async def complete(self, request):
            response = await super().complete(request)
            if len(self.requests) == 1:
                call = response.tool_calls[0]
                arguments = {
                    key: unused_value if value is None else value
                    for key, value in call.arguments.items()
                }
                return ModelResponse(tool_calls=(replace(call, arguments=arguments),))
            return response

    value = navigation_skill()
    (tmp_path / "appendix.txt").write_bytes(b"TABLE BODY 682")
    provider = CorrectingClient(tier)
    runtime, store, _, session, _ = await build_case(
        tmp_path,
        values=(value,),
        provider=provider,
        context=context_policy(skills=retrieval_policy(default_tier="directory")),
        activation_policy=policy(SkillResourceRoot(value.descriptor.plugin, tmp_path)),
    )
    try:
        await select(runtime, session, value)
        await runtime.skill_context.rebuild_index(session)
        result = await runtime.run_existing(session, "maintenance calibration")
        assert result.steps == 3 and result.final_text == "read requested body"
        events = await runtime.sessions.read_session(session)
        results = [e.data for e in events if e.type == "tool/result"]
        assert results[0]["status"] == "failed"
        assert "JSON null (unquoted), not a string" in results[0]["content"]
        assert "skill_receipt" not in results[0]["data"]
        assert results[1]["status"] == "succeeded"
        second = json.loads(provider.requests[1].messages[-1].content.split("\n")[1])
        assert all(block["tier"] == "directory" for block in second)
        assert runtime.invariants.check(events) == ()
        assert await verify_request_snapshots(runtime.sessions, runtime.surface, session) == ()
    finally:
        await runtime.dispose()
        await store.aclose()


@pytest.mark.parametrize("initial", ["directory", "summary"])
@pytest.mark.parametrize("temperature", [None, 0, 0.0, 0.25])
@pytest.mark.parametrize(
    "tier,body", [("section", "OFFLINE BODY 731"), ("chunk", "TABLE BODY 682")]
)
async def test_model_reads_navigation_from_dispatched_request_and_selects_exact_body(
    tmp_path,
    initial,
    tier,
    body,
    temperature,
):
    value = navigation_skill()
    (tmp_path / "appendix.txt").write_bytes(b"TABLE BODY 682")
    provider = DirectoryClient(tier)
    runtime, store, _, session, _ = await build_case(
        tmp_path,
        values=(value,),
        provider=provider,
        context=context_policy(skills=retrieval_policy(default_tier=initial)),
        config_changes={"temperature": temperature},
        activation_policy=policy(SkillResourceRoot(value.descriptor.plugin, tmp_path)),
    )
    try:
        await select(runtime, session, value)
        await runtime.skill_context.rebuild_index(session)
        result = await runtime.run_existing(session, "maintenance calibration")
        assert result.final_text == "read requested body"
        assert body not in canonical_json(provider.requests[0].to_dict())
        assert body in provider.requests[-1].messages[-1].content
        assert "Skill documentation, separate from workspace files" in (
            provider.requests[0].messages[-1].content
        )
        assert "traceh.runtime.references" in provider.requests[0].system_prompt
        assert "request_skill_reference" in provider.requests[0].system_prompt
        events = await runtime.sessions.read_session(session)
        contexts = [e for e in events if e.type == "context/input"]
        directory = next(b for e in contexts for b in e.data["blocks"] if b["tier"] == "directory")
        navigation = json.loads(directory["body"])
        assert navigation["sections"][1] == {
            "section_id": "f863",
            "title": "离线标定",
            "summary": "停机后的校准步骤；不用于在线校准。",
            "content_bytes": len(b"OFFLINE BODY 731"),
        }
        for event in events:
            if event.type == "tool/result":
                assert event.data["data"]["available"] == value.descriptor.navigation()
                assert body not in canonical_json(event.data)
        assert "REPLACEMENT ONLY 829" not in canonical_json(
            [r.to_dict() for r in provider.requests]
        )
        assert runtime.invariants.check(events) == ()
        assert await verify_request_snapshots(runtime.sessions, runtime.surface, session) == ()
        snapshot = next(e for e in events if e.type == "request/snapshot")
        await select(runtime, session, operation="clear", head=1)
        (tmp_path / "appendix.txt").unlink()
        rebuilt = await reconstruct_request(runtime.sessions, runtime.surface, session, snapshot)
        assert rebuilt.request == provider.requests[0]
        await runtime.run_existing(session, "unrelated")
        assert body not in provider.requests[-1].messages[-1].content
    finally:
        await runtime.dispose()
        await store.aclose()


@pytest.mark.parametrize("kind", ["section", "resource", "chunk"])
@pytest.mark.parametrize("field", ["title", "summary"])
def test_missing_navigation_metadata_is_rejected_in_frozen_descriptors(kind, field):
    raw = navigation_skill().descriptor.to_dict()
    target = (
        raw["sections"][0]
        if kind == "section"
        else raw["resources"][0]
        if kind == "resource"
        else raw["resources"][0]["chunks"][0]
    )
    del target[field]
    with pytest.raises(ValueError, match="keys-invalid"):
        SkillDescriptor.from_dict(raw)


@pytest.mark.parametrize("value", ["", " \n", None])
@pytest.mark.parametrize("field", ["title", "summary"])
def test_empty_navigation_is_not_replaced_by_id_or_body(value, field):
    with pytest.raises(ValueError, match="text-invalid"):
        replace(navigation_skill().descriptor.sections[0], **{field: value})


@pytest.mark.parametrize("temperature", [True, "0", float("nan"), float("inf"), float("-inf")])
def test_request_reader_does_not_coerce_invalid_temperature(temperature):
    raw = ModelRequest(provider="scripted", model="fixture", messages=()).to_dict()
    raw["temperature"] = temperature
    with pytest.raises(ValueError, match="finite JSON number"):
        ModelRequest.from_dict(raw)


@pytest.mark.parametrize("kind", ["section", "resource", "chunk"])
@pytest.mark.parametrize("field", ["title", "summary"])
def test_navigation_change_cannot_reuse_old_composition_identity(kind, field):
    descriptor = navigation_skill().descriptor
    snapshot = RuntimeComposition(
        "scripted",
        "fixture",
        "",
        (),
        plugins=(descriptor.plugin,),
        skill_catalog=(descriptor,),
    ).snapshot()
    raw = snapshot.to_dict()
    target = (
        raw["skill_catalog"][0]["sections"][0]
        if kind == "section"
        else raw["skill_catalog"][0]["resources"][0]
        if kind == "resource"
        else raw["skill_catalog"][0]["resources"][0]["chunks"][0]
    )
    target[field] = "changed navigation description"
    with pytest.raises(ValueError, match="catalog-digest-mismatch"):
        CompositionSnapshot.from_dict(raw)


@pytest.mark.parametrize("kind", ["section", "resource", "chunk"])
async def test_author_navigation_summary_obeys_activation_limit(kind):
    value = navigation_skill()
    raw = value.descriptor.to_dict()
    target = (
        raw["sections"][0]
        if kind == "section"
        else raw["resources"][0]
        if kind == "resource"
        else raw["resources"][0]["chunks"][0]
    )
    target["summary"] = "界" * 101  # 303 bytes; the explicit test limit is 300.
    value = replace(value, descriptor=SkillDescriptor.from_dict(raw))
    plugin = ScriptedPlugin(manifest("reference.author"), skills=(value,))
    with pytest.raises(PluginActivationError):
        await builder(plugin, skill_policy=policy()).prepare(("reference.author",))
    assert all(reg.disposed for reg in plugin.skill_registrations)


async def test_directory_budget_excludes_whole_navigation_without_truncating(tmp_path):
    value = navigation_skill()
    (tmp_path / "appendix.txt").write_bytes(b"TABLE BODY 682")
    provider = DirectoryClient("section")
    runtime, store, _, session, _ = await build_case(
        tmp_path,
        values=(value,),
        provider=provider,
        context=context_policy(item_bytes=600, skills=retrieval_policy(default_tier="directory")),
        activation_policy=policy(SkillResourceRoot(value.descriptor.plugin, tmp_path)),
    )
    try:
        await select(runtime, session, value)
        await runtime.skill_context.rebuild_index(session)
        await runtime.run_existing(session, "maintenance calibration")
        events = await runtime.sessions.read_session(session)
        context = next(e for e in events if e.type == "context/input")
        assert context.data["blocks"] == []
        assert any(e["reason"] == "budget-excluded" for e in context.data["exclusions"])
        assert "离线标定" not in provider.requests[0].messages[-1].content
        assert runtime.invariants.check(events) == ()
    finally:
        await runtime.dispose()
        await store.aclose()


async def test_cancellation_after_disclosure_converges_without_carrying_body_to_new_turn(tmp_path):
    value = navigation_skill()
    (tmp_path / "appendix.txt").write_bytes(b"TABLE BODY 682")
    gate = asyncio.Event()
    provider = DirectoryClient("section", gate=gate)
    runtime, store, _, session, _ = await build_case(
        tmp_path,
        values=(value,),
        provider=provider,
        context=context_policy(skills=retrieval_policy(default_tier="directory")),
        activation_policy=policy(SkillResourceRoot(value.descriptor.plugin, tmp_path)),
    )
    try:
        await select(runtime, session, value)
        await runtime.skill_context.rebuild_index(session)
        task = asyncio.create_task(runtime.run_existing(session, "maintenance calibration"))
        await asyncio.wait_for(gate.wait(), timeout=10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert "OFFLINE BODY 731" in provider.requests[-1].messages[-1].content
        events = await runtime.sessions.read_session(session)
        assert runtime.invariants.check(events) == ()
        await select(runtime, session, operation="clear", head=1)
        await runtime.run_existing(session, "unrelated")
        assert "OFFLINE BODY 731" not in provider.requests[-1].messages[-1].content
        assert await verify_request_snapshots(runtime.sessions, runtime.surface, session) == ()
    finally:
        await runtime.dispose()
        await store.aclose()
