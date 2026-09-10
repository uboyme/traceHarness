"""Real owner paths, explicitly opting into the configured Docker backend."""

import shlex

from test_sandbox_docker import settings as settings

from traceh.api.llm import ModelResponse, ToolCall
from traceh.api.sandbox import SandboxConfiguration
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.session.event_store import InMemoryEventStore


async def test_real_shell_and_completion_verifier_use_original_owner_streams(tmp_path, settings):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    command = shlex.join(
        (
            "python",
            "-c",
            "from pathlib import Path; Path('empty').mkdir(); "
            "Path('answer.txt').write_text('isolated edit'); print('隔离执行完成')",
        )
    )
    verification = shlex.join(
        (
            "python",
            "-c",
            "from pathlib import Path; "
            "assert Path('answer.txt').read_text()=='isolated edit'; "
            "assert Path('empty').is_dir(); Path('verifier-only').touch(); print('verified')",
        )
    )
    provider = ScriptedLlmProvider(
        (
            ModelResponse(
                tool_calls=(ToolCall("execute", "shell", {"command": command}),),
                finish_reason="tool_calls",
            ),
            ModelResponse(content="finished"),
        )
    )
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "data",
            model="test-model",
            sandbox=SandboxConfiguration(settings, tmp_path / "cas"),
            verification_command=verification,
        ),
        event_store=InMemoryEventStore(),
        provider=provider,
    )
    try:
        session_id = await runtime.create_session(workspace)
        result = await runtime.run_existing(session_id, "perform the authorized edit")
        assert result.reason == "completed"
        assert result.verification_passed is True
        assert (workspace / "answer.txt").read_text() == "isolated edit"
        assert (workspace / "empty").is_dir()
        assert not (workspace / "verifier-only").exists()
        effects = await runtime.sessions.read_effects(session_id)
        intent = next(e for e in effects if e.type == "effect/intent")
        request = next(e for e in effects if e.type == "sandbox/request")
        assert request.data["owner"]["owner_id"] == intent.data["effect_id"]
        assert request.data["owner"]["tool_call_id"] == "execute"
        publication = next(e for e in effects if e.type == "sandbox/publication")
        assert publication.data["status"] == "completed"
        events = await runtime.sessions.read_session(session_id)
        assert any(e.type == "tool/result" and "隔离执行完成" in e.data["content"] for e in events)
        assert any(
            e.type == "sandbox/request" and e.data["owner"]["kind"] == "verification"
            for e in events
        )
        from traceh.chat.sandbox_inspection import sandbox_report

        report = await sandbox_report(
            runtime.sessions.store, session_id=session_id, policy=settings,
        )
        execution_ids = [
            e.data["execution_id"] for e in (*events, *effects) if e.type == "sandbox/request"
        ]
        assert len(execution_ids) == 2
        assert all(identity in report for identity in execution_ids)
        assert not await runtime.check_invariants(session_id)
    finally:
        await runtime.dispose()


async def test_sandbox_receipt_uses_existing_agent_admission_and_wall_reservation(
    tmp_path, settings
):
    from test_budget_enforcement import limits

    from traceh.agents import AgentRegistrar
    from traceh.api.agents import AgentSpec
    from traceh.api.turns import TurnInput
    from traceh.budgets import BudgetEnforcement, BudgetLedgerService
    from traceh.runtime.continuation import DefaultContinuationRuntime
    from traceh.supervision import AgentRuntimeExecution

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = InMemoryEventStore()
    budgets = BudgetLedgerService(store)
    enforcement = BudgetEnforcement(
        budgets,
        agent_id="owned-agent",
        session_id="owned-session",
        continuation=DefaultContinuationRuntime(),
    )
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "data",
            model="test-model",
            sandbox=SandboxConfiguration(settings, tmp_path / "cas"),
        ),
        event_store=store,
        provider=ScriptedLlmProvider(
            (
                ModelResponse(
                    tool_calls=(ToolCall("check", "shell", {"command": "python -c 'print(123)'"}),),
                    finish_reason="tool_calls",
                ),
                ModelResponse(content="completed"),
            )
        ),
        continuation=enforcement.continuation,
        llm_runtime=enforcement.llm_runtime,
        tool_admission_gate=enforcement.tool_admission_gate,
    )
    try:
        await runtime.create_session(workspace, session_id="owned-session")
        await AgentRegistrar(store).create_agent(
            AgentSpec(preset="managed", workspace_id="workspace"),
            request_id="create",
            agent_id="owned-agent",
            session_id="owned-session",
        )
        await budgets.grant_root(
            operation_id="grant",
            agent_id="owned-agent",
            limits=limits(max_tokens=None, max_wall_milliseconds=60000),
        )
        execution = enforcement.wrap(AgentRuntimeExecution(runtime, "owned-session"))
        result = await execution.run_turn(TurnInput("run the check", "message"))
        assert result.reason == "completed"
        effects = await runtime.sessions.read_effects("owned-session")
        intent = next(e.data for e in effects if e.type == "effect/intent")
        request = next(e.data for e in effects if e.type == "sandbox/request")
        owner = request["owner"]
        assert owner["agent_id"] == "owned-agent"
        assert owner["budget_admission"] == intent["budget_admission"]
        assert owner["budget_reservation"] == intent["budget_reservation"]
        assert owner["budget_admission"] and owner["budget_reservation"]
        ledger = await budgets.ledger()
        assert ledger.usage_reservation(owner["budget_reservation"]).agent_id == "owned-agent"
    finally:
        await runtime.dispose()


async def test_unconfigured_shell_fails_closed_in_real_runtime(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    command = shlex.join(("python", "-c", "from pathlib import Path; Path('escaped').touch()"))
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data", model="test-model"),
        event_store=InMemoryEventStore(),
        provider=ScriptedLlmProvider(
            (
                ModelResponse(
                    tool_calls=(ToolCall("execute", "shell", {"command": command}),),
                    finish_reason="tool_calls",
                ),
                ModelResponse(content="stopped"),
            )
        ),
    )
    try:
        session_id = await runtime.create_session(workspace)
        await runtime.run_existing(session_id, "try execution")
        effects = await runtime.sessions.read_effects(session_id)
        outcome = next(e for e in effects if e.type == "effect/outcome")
        assert outcome.data["status"] == "failed"
        assert "sandbox-not-configured" in outcome.data["message"]
        assert not (workspace / "escaped").exists()
    finally:
        await runtime.dispose()
