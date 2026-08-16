from __future__ import annotations

from pathlib import Path

import pytest

from traceh.api.llm import ModelResponse, ToolCall
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.runtime.verification import CommandVerifier


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
                finish_reason="tool_calls",
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
                finish_reason="tool_calls",
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
                finish_reason="tool_calls",
            ),
            ModelResponse(content="done"),
        )
    )
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "data",
            provider="scripted",
            model="test-model",
        ),
        provider=provider,
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
