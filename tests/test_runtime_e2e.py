from __future__ import annotations

import pytest
from sandbox_fixtures import real_sandbox_policy

from traceh.api.llm import CompletionCategory, ModelResponse, ToolCall
from traceh.api.sandbox import SandboxConfiguration
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.runtime.verification import CommandVerifier
from traceh.session.event_store import InMemoryEventStore


@pytest.mark.asyncio
async def test_scripted_coding_agent_modifies_and_verifies_workspace(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "calculator.py").write_text(
        "def add(a, b):\n    return a - b\n",
        encoding="utf-8",
    )
    (workspace / "test_calculator.py").write_text(
        "import unittest\nfrom calculator import add\n"
        "class T(unittest.TestCase):\n"
        "    def test_add(self): self.assertEqual(add(2, 3), 5)\n",
        encoding="utf-8",
    )
    provider = ScriptedLlmProvider(
        (
            ModelResponse(
                content="inspect",
                tool_calls=(ToolCall("read", "read_file", {"path": "calculator.py"}),),
                completion=CompletionCategory.TOOL_HANDOFF,
            ),
            ModelResponse(
                content="fix",
                tool_calls=(
                    ToolCall(
                        "patch",
                        "apply_patch",
                        {
                            "path": "calculator.py",
                            "old_text": "    return a - b\n",
                            "new_text": "    return a + b\n",
                            "expected_replacements": 1,
                        },
                    ),
                ),
                completion=CompletionCategory.TOOL_HANDOFF,
            ),
            ModelResponse(
                content="test",
                tool_calls=(
                    ToolCall(
                        "tests",
                        "shell",
                        {"command": "python -m unittest -v", "timeout": 20},
                    ),
                ),
                completion=CompletionCategory.TOOL_HANDOFF,
            ),
            ModelResponse(content="done"),
        )
    )
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "data",
            provider="scripted",
            model="test-model",
            sandbox=SandboxConfiguration(real_sandbox_policy(), tmp_path / "cas"),
        ),
        provider=provider,
        event_store=InMemoryEventStore(),
        verifier=CommandVerifier("python -m unittest -v"),
    )
    session_id = await runtime.create_session(workspace)
    result = await runtime.run_existing(session_id, "fix addition")

    assert result.reason == "completed"
    assert result.verification_passed is True
    assert "a + b" in (workspace / "calculator.py").read_text(encoding="utf-8")
    assert not await runtime.check_invariants(session_id)
    assert not await verify_request_snapshots(runtime.sessions, runtime.surface, session_id)
    effects = await runtime.sessions.read_effects(session_id)
    assert sum(event.type == "effect/intent" for event in effects) == 3
    await runtime.dispose()
