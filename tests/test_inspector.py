from __future__ import annotations

import pytest

from traceh.api.llm import ModelResponse
from traceh.inspector import SessionInspector
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.session.event_store import InMemoryEventStore


@pytest.mark.asyncio
async def test_inspector_writes_static_html(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data", provider="scripted", model="demo"),
        provider=ScriptedLlmProvider((ModelResponse(content="done"),)),
        event_store=InMemoryEventStore(),
    )
    result = await runtime.run(workspace, "hello")
    inspector = SessionInspector(runtime.sessions, runtime.surface)
    output = await inspector.render_html(result.session_id, tmp_path / "trace.html")
    text = output.read_text(encoding="utf-8")
    assert "TraceHarness Session" in text
    assert result.session_id in text
    assert "request/snapshot" in text
    await runtime.dispose()
