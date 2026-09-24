"""Every complete tool result is addressable; only large ones are withheld.

Deciding addressability from one result's size meant a long task could produce
hundreds of small results, none individually over the ceiling, none ever
referenceable - and therefore none foldable. These tests pin the split: size
still decides first disclosure, and no longer decides whether the original can
be found again.
"""

from __future__ import annotations

import json

import pytest

from traceh.api.llm import CompletionCategory, ModelResponse, ToolCall
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.session.event_store import InMemoryEventStore
from traceh.session.tool_output import (
    INLINE,
    OUTPUT_TOOL_IDS,
    RETAINED,
    output_reference,
    resolve_tool_output,
)


def read_file_call(call_id: str) -> ModelResponse:
    return ModelResponse(
        content="",
        tool_calls=(ToolCall(call_id, "read_file", {"path": "target.txt"}),),
        completion=CompletionCategory.TOOL_HANDOFF,
    )


async def run(tmp_path, body: str, *responses: ModelResponse):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "target.txt").write_text(body, encoding="utf-8")
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data", max_tool_output_chars=600),
        provider=ScriptedLlmProvider(responses, repeat_last=True),
        event_store=InMemoryEventStore(),
    )
    session_id = await runtime.create_session(workspace)
    try:
        await runtime.run_existing(session_id, "read it")
        events = await runtime.sessions.read_session(session_id)
        effects = await runtime.sessions.read_effects(session_id)
        violations = await runtime.check_invariants(session_id)
    finally:
        await runtime.dispose()
    return events, effects, session_id, violations


@pytest.mark.asyncio
async def test_a_small_result_is_shown_in_full_and_still_addressable(tmp_path) -> None:
    events, effects, session_id, violations = await run(
        tmp_path,
        "small body\n",
        read_file_call("c1"),
        ModelResponse(content="done", completion=CompletionCategory.NORMAL),
    )

    result = next(e for e in events if e.type == "tool/result")
    reference = result.data["output_ref"]
    assert reference["disclosure"] == INLINE
    # Unchanged first view: the body itself, not a retention notice.
    assert "small body" in result.data["content"]
    assert "output retained" not in result.data["content"]
    # And the original is reachable through the ordinary resolver.
    payload = resolve_tool_output(
        events,
        effects,
        session_id=session_id,
        effect_id=reference["effect_id"],
        digest=reference["digest"],
    )
    assert "small body" in payload["content"]
    assert not violations


@pytest.mark.asyncio
async def test_a_large_result_is_still_withheld_behind_its_reference(tmp_path) -> None:
    events, effects, session_id, violations = await run(
        tmp_path,
        "x" * 5000,
        read_file_call("c1"),
        ModelResponse(content="done", completion=CompletionCategory.NORMAL),
    )

    result = next(e for e in events if e.type == "tool/result")
    reference = result.data["output_ref"]
    assert reference["disclosure"] == RETAINED
    shown = json.loads(result.data["content"])
    assert "notice" in shown and "x" * 5000 not in result.data["content"]
    payload = resolve_tool_output(
        events,
        effects,
        session_id=session_id,
        effect_id=reference["effect_id"],
        digest=reference["digest"],
    )
    assert "x" * 5000 in payload["content"]
    assert not violations


@pytest.mark.asyncio
async def test_many_small_results_are_each_addressable(tmp_path) -> None:
    """The accumulation case: none is individually large, all are foldable."""

    events, _, _, violations = await run(
        tmp_path,
        "small body\n",
        *(read_file_call(f"c{i}") for i in range(5)),
        ModelResponse(content="done", completion=CompletionCategory.NORMAL),
    )

    results = [e for e in events if e.type == "tool/result"]
    assert len(results) == 5
    assert all(e.data["output_ref"]["disclosure"] == INLINE for e in results)
    # Distinct effects, so folding one cannot be confused with folding another.
    assert len({e.data["output_ref"]["effect_id"] for e in results}) == 5
    assert not violations


@pytest.mark.asyncio
async def test_an_inline_reference_whose_payload_disagrees_is_refused(tmp_path) -> None:
    """The inline mode must prove it stored what the model was actually shown."""

    _, effects, _, _ = await run(
        tmp_path,
        "small body\n",
        read_file_call("c1"),
        ModelResponse(content="done", completion=CompletionCategory.NORMAL),
    )
    outcome = next(e for e in effects if e.type == "effect/outcome")
    assert output_reference(outcome) is not None

    tampered = type(outcome)(
        **{
            **{f: getattr(outcome, f) for f in outcome.__dataclass_fields__},
            "data": {**outcome.data, "content": "a body the model never saw"},
        }
    )
    with pytest.raises(ValueError, match="tool-output-inline-payload-mismatch"):
        output_reference(tampered)


def test_the_read_back_capability_is_named_once() -> None:
    assert OUTPUT_TOOL_IDS == ("read_tool_output", "list_tool_outputs", "search_tool_output")


@pytest.mark.asyncio
async def test_a_host_can_grant_read_back_without_granting_shell(tmp_path) -> None:
    """A readonly role must not pick up write tools to gain read-back."""

    provider = ScriptedLlmProvider((ModelResponse(content="done"),), repeat_last=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data"),
        provider=provider,
        event_store=InMemoryEventStore(),
        include_default_tools=False,
        output_tool_ids=OUTPUT_TOOL_IDS,
    )
    try:
        session_id = await runtime.create_session(workspace)
        await runtime.run_existing(session_id, "do the task")
    finally:
        await runtime.dispose()

    # What the model was actually offered, from the request the host froze.
    names = {tool.name for tool in provider.requests[0].tools}
    assert set(OUTPUT_TOOL_IDS) <= names
    assert not names & {"shell", "apply_patch", "read_file", "search_text", "list_files"}


@pytest.mark.asyncio
async def test_an_unknown_read_back_tool_is_refused(tmp_path) -> None:
    with pytest.raises(ValueError, match="unknown output tool"):
        build_default_runtime(
            RuntimeConfig(data_dir=tmp_path / "data"),
            provider=ScriptedLlmProvider((ModelResponse(content="done"),)),
            event_store=InMemoryEventStore(),
            include_default_tools=False,
            output_tool_ids=("read_tool_output", "not_a_tool"),
        )


def test_a_role_without_a_context_policy_keeps_the_historical_behaviour() -> None:
    """Absent means no metering and no compaction, exactly as before."""

    from traceh.api.agents import AgentSpec
    from traceh.api.workspaces import WorkspaceAccess
    from traceh.product.registry import ResolvedAgentAssembly
    from traceh.product.runtime import _context_governance

    assembly = ResolvedAgentAssembly(
        spec=AgentSpec(preset="p", workspace_id="w", capability_grants=()),
        provider_id="scripted",
        model_id="m",
        tool_ids=(),
        prompt_ids=(),
        policy_ids=(),
        workspace_access=WorkspaceAccess.READ_ONLY,
    )
    assert _context_governance(assembly) == (None, None)


def test_a_stated_context_policy_reaches_the_runtime_as_both_objects() -> None:
    """Metering and a compaction owner are produced together or not at all.

    Metering alone would measure the pressure with nothing able to relieve it,
    and a compaction owner alone is the Turn-front byte mode that a single long
    Turn already defeats.
    """

    from traceh.api.agents import AgentSpec
    from traceh.api.product import ProductContextPolicy
    from traceh.api.workspaces import WorkspaceAccess
    from traceh.product.registry import ResolvedAgentAssembly, agent_assembly_digest
    from traceh.product.runtime import _context_governance

    policy = ProductContextPolicy(
        encoding="cl100k_base",
        window_tokens=32000,
        output_reserve_tokens=4096,
        safety_margin_tokens=512,
        trigger_percent=60,
        fold_relief_percent=40,
        fold_protect_recent_groups=2,
        fold_protect_readback_utf8_bytes=40_000,
        compaction_trigger_utf8_bytes=200_000,
        compaction_max_summary_utf8_bytes=4096,
        compaction_keep_recent_turns=1,
    )
    base = {
        "spec": AgentSpec(preset="p", workspace_id="w", capability_grants=()),
        "provider_id": "scripted",
        "model_id": "m",
        "tool_ids": (),
        "prompt_ids": (),
        "policy_ids": (),
        "workspace_access": WorkspaceAccess.READ_ONLY,
    }
    governed = ResolvedAgentAssembly(**base, context_policy=policy)
    ungoverned = ResolvedAgentAssembly(**base)

    token_budget, compaction = _context_governance(governed)
    assert token_budget is not None and compaction is not None
    assert token_budget.step_fold_enabled
    assert token_budget.fold_protect_recent_groups == 2
    assert token_budget.fold_protect_readback_utf8_bytes == 40_000
    assert token_budget.relief_tokens < token_budget.input_limit
    assert compaction.enabled and compaction.keep_recent_turns == 1

    # Same preset name, different governance, must not share an assembly digest:
    # a metered and an unmetered run are not the same run.
    assert agent_assembly_digest(governed) != agent_assembly_digest(ungoverned)


def test_rebuilding_a_role_tuple_skips_the_runtime_built_read_back_tools() -> None:
    """Granting read-back to a Product role must not drop a tool on the floor.

    ``_tools`` deliberately skips the read-back ids because the Runtime builds
    those from its own SessionService. The role-specific branches then reorder
    the tuple by the profile's grant list, and that lookup has to skip them too.
    It did not, so the first profile to actually grant them failed at agent
    creation with a bare KeyError, after its workspace was already provisioned.
    """

    from traceh.product.runtime import _role_tools
    from traceh.session.tool_output import OUTPUT_TOOL_IDS

    class _Tool:
        def __init__(self, name: str) -> None:
            self.name = name

    class _Assembly:
        tool_ids = ("read_file", "read_tool_output", "shell", "search_tool_output")

    ordered = _role_tools(_Assembly(), (_Tool("shell"),), (_Tool("read_file"),))

    # Ordered by the profile's own grant list, with the runtime-built ids absent.
    assert [tool.name for tool in ordered] == ["read_file", "shell"]
    assert not {tool.name for tool in ordered} & set(OUTPUT_TOOL_IDS)


def test_a_grant_nothing_can_build_still_fails_with_its_stable_code() -> None:
    """Skipping the read-back ids must not turn a real misbinding into silence."""

    from traceh.product.errors import ProductProfileError
    from traceh.product.runtime import _role_tools

    class _Assembly:
        tool_ids = ("read_tool_output", "a_tool_no_host_builds")

    with pytest.raises(ProductProfileError) as caught:
        _role_tools(_Assembly(), (), ())
    assert caught.value.code == "product-tool-binding-missing"
