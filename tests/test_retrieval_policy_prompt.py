"""Policy delivery/replay checks; semantic compliance requires real Provider evidence."""

import pytest

from traceh.api.llm import ModelResponse
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.event_store import InMemoryEventStore


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message,answer", [("Hello", "Hello"), ("Can you resolve this?", "Insufficient evidence")]
)
async def test_policy_is_frozen_and_does_not_force_a_tool_call(
    tmp_path, monkeypatch, message, answer
):
    import traceh.runtime.prompt as prompts

    provider = ScriptedLlmProvider((ModelResponse(content=answer),))
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data", provider="scripted", model="policy-contract"),
        provider=provider,
        event_store=InMemoryEventStore(),
        include_default_tools=False,
    )
    try:
        session = await runtime.create_session(tmp_path)
        result = await runtime.run_existing(session, message)
        assert (result.final_text, result.steps, result.reason) == (answer, 1, "completed")
        assert "## traceh.runtime.references" in provider.requests[0].system_prompt
        assert provider.requests[0].tools == ()
        before = await runtime.sessions.read_session(session)
        # Simulate a newer installed prompt. Replay must use the persisted Composition text.
        monkeypatch.setattr(prompts, "_REFERENCE_GUIDANCE", "Different future prompt policy")
        assert not await verify_request_snapshots(runtime.sessions, runtime.surface, session)
        assert await runtime.sessions.read_session(session) == before
        assert not await runtime.check_invariants(session)
    finally:
        await runtime.dispose()
