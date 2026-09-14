"""Policy delivery/replay checks; semantic compliance requires real Provider evidence."""

import pytest

from traceh.api.llm import ModelResponse
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.event_store import InMemoryEventStore
from traceh.session.service import SessionService
from traceh.tools.output import ReadToolOutput


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message,answer", [("Hello", "Hello"), ("Can you resolve this?", "Insufficient evidence")]
)
async def test_policy_is_frozen_and_does_not_force_a_tool_call(
    tmp_path, monkeypatch, message, answer
):
    import traceh.runtime.prompt as prompts

    provider = ScriptedLlmProvider((ModelResponse(content=answer),))
    store = InMemoryEventStore()
    sessions = SessionService(store)
    # The reference policy is only delivered to a composition that actually has a
    # reference source. This one does, which is what makes the frozen-replay check
    # below meaningful; the tool-less case is pinned separately.
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data", provider="scripted", model="policy-contract"),
        provider=provider,
        event_store=store,
        include_default_tools=False,
        additional_tools=(ReadToolOutput(sessions, max_chars=4096),),
    )
    try:
        session = await runtime.create_session(tmp_path)
        result = await runtime.run_existing(session, message)
        assert (result.final_text, result.steps, result.reason) == (answer, 1, "completed")
        assert "## traceh.runtime.references" in provider.requests[0].system_prompt
        assert [tool.name for tool in provider.requests[0].tools] == ["read_tool_output"]
        before = await runtime.sessions.read_session(session)
        # Simulate a newer installed prompt. Replay must use the persisted Composition text.
        monkeypatch.setattr(prompts, "_REFERENCE_GUIDANCE", "Different future prompt policy")
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, session)
        assert await runtime.sessions.read_session(session) == before
        assert not await runtime.check_invariants(session)
    finally:
        await runtime.dispose()


@pytest.mark.asyncio
async def test_a_composition_without_reference_sources_is_not_given_their_policy(tmp_path):
    """Telling an agent how to use tools it does not have is noise, not guidance.

    The section that names those tools is already conditional on them; this pins
    the instructions for using them to the same condition.
    """

    provider = ScriptedLlmProvider((ModelResponse(content="done"),))
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data", provider="scripted", model="policy-contract"),
        provider=provider,
        event_store=InMemoryEventStore(),
        include_default_tools=False,
    )
    try:
        session = await runtime.create_session(tmp_path)
        await runtime.run_existing(session, "Hello")
        prompt = provider.requests[0].system_prompt
        assert provider.requests[0].tools == ()
        assert "## traceh.runtime.references" not in prompt
        assert "## traceh.runtime.source_navigation" not in prompt
        for absent in ("search_history", "read_tool_output", "request_skill_reference"):
            assert absent not in prompt
        # The sections that describe this composition are still delivered.
        assert "## traceh.identity" in prompt and "## traceh.runtime.workspace" in prompt
    finally:
        await runtime.dispose()
