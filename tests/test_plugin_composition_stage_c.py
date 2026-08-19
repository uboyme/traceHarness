"""Stage C contracts for user-facing composition switching and migration facts."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from plugin_fixtures import RecordingTool, ScriptedPlugin, entry_point_for, manifest, provider_for

from traceh.api.llm import ModelResponse
from traceh.api.prompts import PromptSection
from traceh.cli.activity import default_clock
from traceh.cli.chat import ChatSession, _chat_loop, run_chat
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_runtime import (
    AgentAlreadyRunningError,
    RuntimeConfig,
    SessionPluginMigrationError,
    SessionPluginMismatchError,
    build_default_runtime_async,
)
from traceh.runtime.request_builder import verify_request_snapshots
from traceh.session.event_store import InMemoryEventStore
from traceh.session.invariants import CoreInvariantChecker
from traceh.session.plugin_identity import MIGRATION_EVENT_TYPE
from traceh.session.service import SessionService


class _Console:
    def __init__(self, *inputs: str) -> None:
        self.inputs = list(inputs)
        self.lines: list[str] = []

    def read_line(self, prompt: str) -> str:
        del prompt
        if not self.inputs:
            raise EOFError
        return self.inputs.pop(0)

    def write(self, text: str) -> None:
        self.lines.append(text)


class _GatedProvider:
    name = "scripted"

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request):
        del request
        self.entered.set()
        await self.release.wait()
        return ModelResponse(content="gated answer")


class _ObservedGate:
    def __init__(self, lock: asyncio.Lock, attempted: asyncio.Event) -> None:
        self._lock = lock
        self.attempted = attempted

    async def __aenter__(self):
        self.attempted.set()
        await self._lock.acquire()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self._lock.release()


def _discovery(*plugins: object):
    from traceh.plugins.discovery import PluginDiscovery

    return PluginDiscovery(
        entry_points_provider=provider_for(*(entry_point_for(plugin) for plugin in plugins))
    )


async def _runtime(
    tmp_path: Path,
    *plugins: object,
    enabled: tuple[str, ...],
    provider: object | None = None,
):
    return await build_default_runtime_async(
        RuntimeConfig(data_dir=tmp_path / "data"),
        provider=provider,
        enabled_plugins=enabled,
        plugin_discovery=_discovery(*plugins),
    )


async def test_plugins_help_and_idle_commands_do_not_create_turn_or_model_call(
    tmp_path: Path,
) -> None:
    plugin = ScriptedPlugin(manifest("a.example"))
    provider = ScriptedLlmProvider((ModelResponse(content="unused"),), repeat_last=True)
    runtime = await _runtime(tmp_path, plugin, enabled=("a.example",), provider=provider)
    console = _Console("/help", "/plugins", "/plugins reload", "/exit")
    try:
        assert await run_chat(runtime, console, workspace=tmp_path) == 0
        events = await runtime.sessions.read_session((await runtime.sessions.list_sessions())[0])
        assert all(event.type not in {"turn/start", "user/message"} for event in events)
        assert provider.requests == []
        assert "/plugins reload" in "\n".join(console.lines)
        assert "active plugins: a.example==1.0.0" in console.lines
    finally:
        # run_chat owns and disposes this runtime; the second call verifies
        # idempotence without creating another cleanup.
        await runtime.dispose()


async def test_dispose_rechecks_turn_admission_after_identity_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = ScriptedLlmProvider((ModelResponse(content="must not run"),))
    runtime = await _runtime(tmp_path, enabled=(), provider=provider)
    session_id = await runtime.create_session(tmp_path)
    verification_entered = asyncio.Event()
    release_verification = asyncio.Event()
    original_verify = runtime.verify_session_plugins

    async def gated_verify(target_session_id: str) -> None:
        verification_entered.set()
        await release_verification.wait()
        await original_verify(target_session_id)

    monkeypatch.setattr(runtime, "verify_session_plugins", gated_verify)
    turn = asyncio.create_task(runtime.run_existing(session_id, "must not start"))
    await verification_entered.wait()

    disposal = asyncio.create_task(runtime.dispose())
    await runtime._dispose_started.wait()
    release_verification.set()

    with pytest.raises(RuntimeError, match="runtime is disposed"):
        await turn
    await disposal

    events = await runtime.sessions.read_session(session_id)
    assert all(event.type != "turn/start" for event in events)
    assert provider.requests == []


@pytest.mark.parametrize(
    "open_events",
    [
        (("turn/start", {"turn_id": "interrupted-turn"}),),
        (
            ("turn/start", {"turn_id": "interrupted-turn"}),
            ("step/start", {"step_id": "interrupted-step"}),
        ),
    ],
)
async def test_migration_rejects_persisted_open_turn_or_step_before_setup(
    tmp_path: Path,
    open_events: tuple[tuple[str, dict[str, str]], ...],
) -> None:
    first = ScriptedPlugin(manifest("a.example"))
    setup_entered = asyncio.Event()
    second = ScriptedPlugin(
        manifest("b.example"),
        setup_entered=setup_entered,
    )
    runtime = await _runtime(tmp_path, first, second, enabled=("a.example",))
    session_id = await runtime.create_session(tmp_path)
    for event_type, data in open_events:
        await runtime.sessions.append_session(session_id, event_type, data)

    try:
        with pytest.raises(AgentAlreadyRunningError, match="open durable"):
            await runtime.migrate_session_plugin_composition(
                session_id,
                ("b.example",),
            )
        assert not setup_entered.is_set()
        assert runtime.enabled_plugin_ids == ("a.example",)
        events = await runtime.sessions.read_session(session_id)
        assert all(event.type != MIGRATION_EVENT_TYPE for event in events)
    finally:
        await runtime.dispose()


async def test_resume_hint_uses_durable_target_after_fail_closed_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = ScriptedPlugin(manifest("a.example"))
    second = ScriptedPlugin(manifest("b.example"))
    runtime = await _runtime(tmp_path, first, second, enabled=("a.example",))
    session_id = await runtime.create_session(tmp_path)

    async def fail_publish(generation):
        del generation
        raise RuntimeError("internal publication detail")

    monkeypatch.setattr(runtime.loop.compositions, "publish", fail_publish)
    try:
        with pytest.raises(SessionPluginMigrationError):
            await runtime.migrate_session_plugin_composition(
                session_id,
                ("b.example",),
            )

        console = _Console("/session", "/exit")
        session = ChatSession(session_id, tmp_path)
        assert await _chat_loop(
            runtime,
            console,
            session,
            timeline=False,
            heartbeat_seconds=0,
            clock=default_clock(),
            resume_environment=None,
        ) == 0
        resume_lines = [line for line in console.lines if "traceh chat" in line]
        assert resume_lines
        assert any("b.example" in line for line in resume_lines)
        assert all("a.example" not in line for line in resume_lines)
    finally:
        await runtime.dispose()


async def test_unknown_chat_command_does_not_echo_untrusted_input(
    tmp_path: Path,
) -> None:
    runtime = await _runtime(tmp_path, enabled=())
    console = _Console("/paste SECRET\x1b[2J\u202e", "/exit")
    try:
        assert await run_chat(runtime, console, workspace=tmp_path) == 0
        assert "unknown command (try /help)" in console.lines
        assert not any("SECRET" in line for line in console.lines)
        assert all("\x1b" not in line and "\u202e" not in line for line in console.lines)
    finally:
        await runtime.dispose()


async def test_use_switches_generation_and_next_turn_persists_target_composition(
    tmp_path: Path,
) -> None:
    first = ScriptedPlugin(
        manifest("a.example", "1.0.0"),
        tools=(RecordingTool("old_tool"),),
        prompts=(PromptSection("old-plugin", "old instructions", priority=10),),
    )
    second = ScriptedPlugin(
        manifest("b.example", "2.0.0"),
        tools=(RecordingTool("new_tool"),),
        prompts=(PromptSection("new-plugin", "new instructions", priority=10),),
    )
    provider = ScriptedLlmProvider((ModelResponse(content="answer"),), repeat_last=True)
    runtime = await _runtime(
        tmp_path,
        first,
        second,
        enabled=("a.example",),
        provider=provider,
    )
    session_id = await runtime.create_session(tmp_path)
    try:
        replacement = await runtime.migrate_session_plugin_composition(
            session_id, ("b.example",)
        )
        assert replacement.migration_id
        await runtime.run_existing(session_id, "after switch")
        events = await runtime.sessions.read_session(session_id)
        authorization = next(event for event in events if event.type == MIGRATION_EVENT_TYPE)
        assert authorization.data["from_plugins"] == [
            {"plugin_id": "a.example", "version": "1.0.0"}
        ]
        assert authorization.data["to_plugins"] == [
            {"plugin_id": "b.example", "version": "2.0.0"}
        ]
        snapshot = [event for event in events if event.type == "composition/snapshot"][-1]
        assert snapshot.data["plugins"][-1] == {
            "plugin_id": "b.example",
            "version": "2.0.0",
        }
        assert "new_tool" in [item["name"] for item in snapshot.data["tools"]]
        assert "new instructions" in snapshot.data["system_prompt"]
        assert await verify_request_snapshots(runtime.sessions, runtime.surface, session_id) == ()
        assert not any("generation_id" in event.data for event in events)
    finally:
        await runtime.dispose()


async def test_use_none_removes_old_plugin_tools_from_next_snapshot(
    tmp_path: Path,
) -> None:
    plugin = ScriptedPlugin(manifest("a.example"), tools=(RecordingTool("old_tool"),))
    runtime = await _runtime(tmp_path, plugin, enabled=("a.example",))
    session_id = await runtime.create_session(tmp_path)
    try:
        await runtime.run_existing(session_id, "first")
        await runtime.migrate_session_plugin_composition(session_id, ())
        await runtime.run_existing(session_id, "second")
        snapshots = [
            event
            for event in await runtime.sessions.read_session(session_id)
            if event.type == "composition/snapshot"
        ]
        assert "old_tool" in [item["name"] for item in snapshots[0].data["tools"]]
        assert "old_tool" not in [item["name"] for item in snapshots[-1].data["tools"]]
        assert runtime.enabled_plugin_ids == ()
    finally:
        await runtime.dispose()


async def test_chat_use_command_changes_real_generation_without_a_turn_for_the_command(
    tmp_path: Path,
) -> None:
    first = ScriptedPlugin(manifest("a.example", "1.0.0"))
    second = ScriptedPlugin(manifest("b.example", "2.0.0"))
    provider = ScriptedLlmProvider((ModelResponse(content="turn answer"),), repeat_last=True)
    runtime = await _runtime(
        tmp_path,
        first,
        second,
        enabled=("a.example",),
        provider=provider,
    )
    console = _Console("/plugins use b.example", "question", "/exit")
    try:
        assert await run_chat(runtime, console, workspace=tmp_path) == 0
        session_id = (await runtime.sessions.list_sessions())[0]
        events = await runtime.sessions.read_session(session_id)
        assert sum(event.type == "turn/start" for event in events) == 1
        assert sum(event.type == MIGRATION_EVENT_TYPE for event in events) == 1
        assert "plugin composition switched" in console.lines
        assert "active plugins: b.example==2.0.0" in console.lines
        assert len(provider.requests) == 1
    finally:
        await runtime.dispose()


async def test_same_identity_reload_does_not_append_migration_event(tmp_path: Path) -> None:
    first = ScriptedPlugin(manifest("a.example", "1.0.0"))
    runtime = await _runtime(tmp_path, first, enabled=("a.example",))
    session_id = await runtime.create_session(tmp_path)
    try:
        before = runtime.loop.compositions.current_generation_id
        result = await runtime.reload_plugin_composition(session_id)
        events = await runtime.sessions.read_session(session_id)
        assert result.migration_id is None
        assert runtime.loop.compositions.current_generation_id != before
        assert not any(event.type == MIGRATION_EVENT_TYPE for event in events)
        assert first.cleanup_calls == 1
    finally:
        await runtime.dispose()


async def test_append_failure_rolls_back_candidate_without_authorizing_session(
    tmp_path: Path,
) -> None:
    first = ScriptedPlugin(manifest("a.example"))
    second = ScriptedPlugin(manifest("b.example"))
    runtime = await _runtime(tmp_path, first, second, enabled=("a.example",))
    session_id = await runtime.create_session(tmp_path)
    original_append = runtime.sessions.append_session

    async def fail_append(*args, **kwargs):
        if args[1] == MIGRATION_EVENT_TYPE:
            raise RuntimeError("append backend detail must not escape")
        return await original_append(*args, **kwargs)

    runtime.sessions.append_session = fail_append  # type: ignore[method-assign]
    try:
        with pytest.raises(SessionPluginMigrationError) as caught:
            await runtime.migrate_session_plugin_composition(session_id, ("b.example",))
        assert "append backend detail" not in str(caught.value)
        assert second.cleanup_calls == 1
        assert runtime.enabled_plugin_ids == ("a.example",)
        events = await runtime.sessions.read_session(session_id)
        assert not any(event.type == MIGRATION_EVENT_TYPE for event in events)
        await runtime.run_existing(session_id, "still old")
    finally:
        await runtime.dispose()


async def test_session_head_cas_failure_rolls_back_candidate(tmp_path: Path) -> None:
    first = ScriptedPlugin(manifest("a.example"))
    second = ScriptedPlugin(manifest("b.example"))
    runtime = await _runtime(tmp_path, first, second, enabled=("a.example",))
    session_id = await runtime.create_session(tmp_path)
    other_writer = SessionService(runtime.sessions.store)
    original_append = runtime.sessions.append_session

    async def append_after_external_write(*args, **kwargs):
        if args[1] == MIGRATION_EVENT_TYPE:
            await other_writer.append_session(session_id, "session/metadata-note", {"ok": True})
        return await original_append(*args, **kwargs)

    runtime.sessions.append_session = append_after_external_write  # type: ignore[method-assign]
    try:
        with pytest.raises(Exception) as caught:
            await runtime.migrate_session_plugin_composition(session_id, ("b.example",))
        assert "session changed before migration authorization" in str(caught.value)
        assert second.cleanup_calls == 1
        assert runtime.enabled_plugin_ids == ("a.example",)
    finally:
        await runtime.dispose()


async def test_append_cancellation_reconciles_durable_authorization_before_escaping(
    tmp_path: Path,
) -> None:
    first = ScriptedPlugin(manifest("a.example"))
    second = ScriptedPlugin(manifest("b.example"))
    runtime = await _runtime(tmp_path, first, second, enabled=("a.example",))
    session_id = await runtime.create_session(tmp_path)
    original_append = runtime.sessions.append_session

    async def append_then_cancel(*args, **kwargs):
        event = await original_append(*args, **kwargs)
        if args[1] == MIGRATION_EVENT_TYPE:
            raise asyncio.CancelledError
        return event

    runtime.sessions.append_session = append_then_cancel  # type: ignore[method-assign]
    try:
        with pytest.raises(asyncio.CancelledError):
            await runtime.migrate_session_plugin_composition(session_id, ("b.example",))
        assert runtime.enabled_plugin_ids == ("b.example",)
        events = await runtime.sessions.read_session(session_id)
        assert sum(event.type == MIGRATION_EVENT_TYPE for event in events) == 1
        await runtime.run_existing(session_id, "authorized target")
    finally:
        await runtime.dispose()


async def test_authorized_but_unpublished_session_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = ScriptedPlugin(manifest("a.example"))
    second = ScriptedPlugin(manifest("b.example"))
    runtime = await _runtime(tmp_path, first, second, enabled=("a.example",))
    session_id = await runtime.create_session(tmp_path)

    async def fail_publish(generation):
        del generation
        raise RuntimeError("internal publication detail")

    monkeypatch.setattr(runtime.loop.compositions, "publish", fail_publish)
    try:
        with pytest.raises(SessionPluginMigrationError) as caught:
            await runtime.migrate_session_plugin_composition(session_id, ("b.example",))
        assert "internal publication detail" not in str(caught.value)
        events = await runtime.sessions.read_session(session_id)
        assert any(event.type == MIGRATION_EVENT_TYPE for event in events)
        with pytest.raises(SessionPluginMismatchError):
            await runtime.run_existing(session_id, "must fail closed")
    finally:
        await runtime.dispose()


async def test_dispose_converges_inflight_stage_c_migration(tmp_path: Path) -> None:
    setup_entered = asyncio.Event()
    setup_gate = asyncio.Event()
    plugin = ScriptedPlugin(
        manifest("b.example"),
        setup_entered=setup_entered,
        setup_gate=setup_gate,
        spawn_forever=True,
    )
    first = ScriptedPlugin(manifest("a.example"))
    runtime = await _runtime(tmp_path, first, plugin, enabled=("a.example",))
    session_id = await runtime.create_session(tmp_path)
    migration = asyncio.create_task(
        runtime.migrate_session_plugin_composition(session_id, ("b.example",))
    )
    await setup_entered.wait()
    await plugin.owned_task_started.wait()
    try:
        await runtime.dispose()
        with pytest.raises(asyncio.CancelledError):
            await migration
        assert plugin.cleanup_calls == 1
        assert plugin.owned_task is not None and plugin.owned_task.done()
    finally:
        setup_gate.set()
        if not runtime._dispose_task or not runtime._dispose_task.done():
            await runtime.dispose()


async def test_migration_survives_restart_and_old_composition_is_rejected(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    first = ScriptedPlugin(manifest("a.example", "1.0.0"))
    second = ScriptedPlugin(manifest("b.example", "2.0.0"))
    runtime = await build_default_runtime_async(
        RuntimeConfig(data_dir=data_dir),
        enabled_plugins=("a.example",),
        plugin_discovery=_discovery(first, second),
    )
    session_id = await runtime.create_session(tmp_path)
    await runtime.migrate_session_plugin_composition(session_id, ("b.example",))
    await runtime.run_existing(session_id, "persist target")
    await runtime.dispose()

    recovered = await build_default_runtime_async(
        RuntimeConfig(data_dir=data_dir),
        enabled_plugins=("b.example",),
        plugin_discovery=_discovery(ScriptedPlugin(manifest("b.example", "2.0.0"))),
    )
    old = await build_default_runtime_async(
        RuntimeConfig(data_dir=data_dir),
        enabled_plugins=("a.example",),
        plugin_discovery=_discovery(ScriptedPlugin(manifest("a.example", "1.0.0"))),
    )
    try:
        await recovered.verify_session_plugins(session_id)
        with pytest.raises(SessionPluginMismatchError):
            await old.verify_session_plugins(session_id)
        assert await verify_request_snapshots(
            recovered.sessions, recovered.surface, session_id
        ) == ()
        assert await recovered.check_invariants(session_id) == ()
    finally:
        await recovered.dispose()
        await old.dispose()


async def test_migration_rejects_active_turn_before_candidate_setup(tmp_path: Path) -> None:
    gate = asyncio.Event()
    entered = asyncio.Event()
    first = ScriptedPlugin(manifest("a.example"))
    second = ScriptedPlugin(
        manifest("b.example"),
        setup_gate=gate,
        setup_entered=entered,
    )
    provider = _GatedProvider()
    runtime = await _runtime(
        tmp_path,
        first,
        second,
        enabled=("a.example",),
        provider=provider,
    )
    session_id = await runtime.create_session(tmp_path)
    turn = asyncio.create_task(runtime.run_existing(session_id, "active"))
    try:
        await provider.entered.wait()
        with pytest.raises(AgentAlreadyRunningError):
            await runtime.migrate_session_plugin_composition(session_id, ("b.example",))
        assert not entered.is_set()
    finally:
        provider.release.set()
        await turn
        gate.set()
        await runtime.dispose()


async def test_migration_gate_blocks_turn_admission_until_candidate_publishes(
    tmp_path: Path,
) -> None:
    setup_gate = asyncio.Event()
    setup_entered = asyncio.Event()
    first = ScriptedPlugin(manifest("a.example"))
    second = ScriptedPlugin(
        manifest("b.example"),
        setup_gate=setup_gate,
        setup_entered=setup_entered,
    )
    runtime = await _runtime(tmp_path, first, second, enabled=("a.example",))
    first_session = await runtime.create_session(tmp_path)
    second_session = await runtime.create_session(tmp_path)
    turn_gate_attempted = asyncio.Event()
    runtime._plugin_compositions._gate = _ObservedGate(  # type: ignore[assignment]
        runtime._plugin_compositions._gate,
        turn_gate_attempted,
    )
    migration = asyncio.create_task(
        runtime.migrate_session_plugin_composition(first_session, ("b.example",))
    )
    waiting_turn = None
    try:
        await setup_entered.wait()
        turn_gate_attempted.clear()
        waiting_turn = asyncio.create_task(runtime.run_existing(second_session, "race"))
        await turn_gate_attempted.wait()
        setup_gate.set()
        await migration
        with pytest.raises(SessionPluginMismatchError):
            await waiting_turn
    finally:
        setup_gate.set()
        if waiting_turn is not None and not waiting_turn.done():
            await waiting_turn
        if not migration.done():
            await migration
        await runtime.dispose()


async def test_invalid_migration_events_have_stable_invariant_names(tmp_path: Path) -> None:
    sessions = SessionService(InMemoryEventStore())
    session_id = await sessions.create_session(tmp_path, metadata={"traceh_plugins": []})
    await sessions.append_session(session_id, "turn/start", {"turn_id": "t"})
    await sessions.append_session(
        session_id,
        MIGRATION_EVENT_TYPE,
        {
            "migration_id": "",
            "source_seq": 999,
            "from_plugins": [
                {"plugin_id": "a.example", "version": "bad version"},
                {"plugin_id": "a.example", "version": "1.0.0"},
            ],
            "to_plugins": [],
        },
    )
    violations = CoreInvariantChecker().check(await sessions.read_session(session_id))
    names = {violation.name for violation in violations}
    assert {
        "migration-id-present",
        "migration-plugins-valid",
        "migration-source-seq-matches",
        "migration-outside-turn",
    } <= names
