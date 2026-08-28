from __future__ import annotations

import asyncio
import io
import os
import sys
from pathlib import Path

import pytest

from traceh.api.llm import ModelRequest, ModelResponse
from traceh.cli.chat import (
    INTERRUPTED_EXIT_CODE,
    INTERRUPTED_TURN_NOTICE,
    chat_target,
    run_chat,
)
from traceh.cli.console import (
    Console,
    configure_stdio,
    contains_undecodable_input,
    normalize_input,
)
from traceh.cli.main import (
    CliConfigurationError,
    _configure_from_environment,
    _provider_and_model,
    build_parser,
    main,
)
from traceh.llm.scripted import ScriptedLlmProvider
from traceh.runtime.agent_runtime import RuntimeConfig, build_default_runtime
from traceh.session.event_store import InMemoryEventStore
from traceh.session.invariants import CoreInvariantChecker
from traceh.session.projections import StateProjector

_TRACEH_VARIABLES = (
    "TRACEH_PROVIDER",
    "TRACEH_BASE_URL",
    "TRACEH_MODEL",
    "TRACEH_API_KEY_ENV",
    "TRACEH_DATA_DIR",
    "TRACEH_MAX_STEPS",
    "TRACEH_VERIFY_COMMAND",
)


@pytest.fixture(autouse=True)
def isolated_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "environ", os.environ.copy())
    for name in _TRACEH_VARIABLES:
        monkeypatch.delenv(name, raising=False)


class FakeConsole:
    """Scripted terminal. Items may be strings or exceptions to raise."""

    def __init__(self, inputs: tuple[object, ...] = ()) -> None:
        self._inputs = list(inputs)
        self.prompts: list[str] = []
        self.lines: list[str] = []

    def read_line(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self._inputs:
            raise EOFError
        item = self._inputs.pop(0)
        if isinstance(item, BaseException):
            raise item
        return str(item)

    def write(self, text: str) -> None:
        self.lines.append(text)

    @property
    def console(self) -> Console:
        return Console(read_line=self.read_line, write=self.write)

    @property
    def output(self) -> str:
        return "\n".join(self.lines)


class FlakyProvider:
    """Fails the first `failures` calls, then answers. Never touches the network."""

    name = "scripted"

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("provider exploded")
        return ModelResponse(content="answer after the failure")


class GatedProvider:
    """Blocks inside the model call so a turn can be interrupted mid-flight."""

    name = "scripted"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.started.set()
        await self.release.wait()
        return ModelResponse(content="never delivered")


def build_runtime(tmp_path: Path, store: InMemoryEventStore, *, provider=None):
    actual = provider or ScriptedLlmProvider(
        (ModelResponse(content="scripted answer"),), repeat_last=True
    )
    runtime = build_default_runtime(
        RuntimeConfig(
            data_dir=tmp_path / "data",
            provider="scripted",
            model="scripted-model",
        ),
        provider=actual,
        event_store=store,
    )
    return runtime, actual


def event_types(events) -> list[str]:
    return [event.type for event in events]


def user_messages(events) -> list[str]:
    return [str(event.data.get("content")) for event in events if event.type == "user/message"]


# --- parser and configuration -------------------------------------------------


def test_chat_parser_accepts_a_new_workspace(tmp_path: Path) -> None:
    args = build_parser().parse_args(["chat", str(tmp_path)])
    assert chat_target(args.workspace, args.session_id) == (tmp_path, None)


def test_chat_parser_accepts_an_existing_session() -> None:
    args = build_parser().parse_args(["chat", "--session-id", "abc"])
    assert chat_target(args.workspace, args.session_id) == (None, "abc")


def test_chat_rejects_workspace_and_session_together(tmp_path: Path) -> None:
    args = build_parser().parse_args(["chat", str(tmp_path), "--session-id", "abc"])
    with pytest.raises(CliConfigurationError, match="not both"):
        chat_target(args.workspace, args.session_id)


def test_chat_rejects_missing_workspace_and_session() -> None:
    args = build_parser().parse_args(["chat"])
    with pytest.raises(CliConfigurationError, match="--session-id"):
        chat_target(args.workspace, args.session_id)


def test_chat_inherits_env_file_configuration(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "TRACEH_PROVIDER=openai-compatible",
                "TRACEH_BASE_URL=https://compatible.example/v1",
                "TRACEH_MODEL=configured-model",
                "TRACEH_MAX_STEPS=7",
            )
        ),
        encoding="utf-8",
    )
    args = build_parser().parse_args(["chat", str(tmp_path), "--env-file", str(env_file)])

    report = _configure_from_environment(args)

    assert report.loaded
    assert args.provider == "openai-compatible"
    assert args.model == "configured-model"
    assert args.base_url == "https://compatible.example/v1"
    assert args.max_steps == 7


def test_explicit_cli_model_still_beats_the_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TRACEH_MODEL=file-model\n", encoding="utf-8")
    args = build_parser().parse_args(
        ["chat", str(tmp_path), "--env-file", str(env_file), "--model", "cli-model"]
    )

    _configure_from_environment(args)

    assert args.model == "cli-model"


# --- multi-turn behaviour -----------------------------------------------------


async def test_chat_runs_two_turns_in_one_session(tmp_path: Path) -> None:
    store = InMemoryEventStore()
    runtime, provider = build_runtime(tmp_path, store)
    console = FakeConsole(("第一个问题", "the second question", "/exit"))

    code = await run_chat(runtime, console.console, workspace=tmp_path)
    assert code == 0

    sessions = await runtime.sessions.list_sessions()
    assert len(sessions) == 1
    events = await runtime.sessions.read_session(sessions[0])
    assert event_types(events).count("turn/start") == 2
    assert user_messages(events) == ["第一个问题", "the second question"]

    # The second request is built from the event log, so it can see turn one.
    assert len(provider.requests) == 2
    replayed = [message.content for message in provider.requests[1].messages]
    assert "第一个问题" in replayed
    assert console.output.count("assistant> ") == 2
    assert "reason=completed" in console.output
    effects = await runtime.sessions.read_effects(sessions[0])
    assert not CoreInvariantChecker().check(events, effects)


async def test_chat_continues_an_existing_session_after_recovery(tmp_path: Path) -> None:
    store = InMemoryEventStore()
    setup, _ = build_runtime(tmp_path, store)
    session_id = await setup.create_session(tmp_path)
    await setup.sessions.append_session(session_id, "turn/start", {"turn_id": "t"})
    await setup.sessions.append_session(
        session_id, "step/start", {"turn_id": "t", "step_id": "s"}
    )
    await setup.sessions.append_session(
        session_id,
        "model/attempt-start",
        {"turn_id": "t", "step_id": "s", "attempt_id": "a"},
    )
    await setup.dispose()

    runtime, _ = build_runtime(tmp_path, store)
    console = FakeConsole(("/exit",))
    code = await run_chat(runtime, console.console, session_id=session_id)

    assert code == 0
    assert "recovered:" in console.output
    assert f"session_id={session_id}" in console.output
    events = await runtime.sessions.read_session(session_id)
    # Recovery converged the crash, but no new turn was started for the user.
    assert event_types(events).count("turn/start") == 1
    assert "runtime/recovered" in event_types(events)
    assert user_messages(events) == []


async def test_existing_session_only_starts_a_turn_when_the_user_types(tmp_path: Path) -> None:
    store = InMemoryEventStore()
    setup, _ = build_runtime(tmp_path, store)
    session_id = await setup.create_session(tmp_path)
    await setup.dispose()

    runtime, _ = build_runtime(tmp_path, store)
    console = FakeConsole(("继续这个任务", "/exit"))
    assert await run_chat(runtime, console.console, session_id=session_id) == 0

    events = await runtime.sessions.read_session(session_id)
    # A clean session needs no repair, so no recovery marker is appended.
    assert "runtime/recovered" not in event_types(events)
    assert "recovered:" not in console.output
    assert event_types(events).count("turn/start") == 1
    assert user_messages(events) == ["继续这个任务"]


async def test_unknown_session_is_a_configuration_error(tmp_path: Path) -> None:
    runtime, _ = build_runtime(tmp_path, InMemoryEventStore())
    console = FakeConsole(())
    with pytest.raises(CliConfigurationError, match="session not found"):
        await run_chat(runtime, console.console, session_id="does-not-exist")


async def test_the_built_in_provider_answers_a_second_turn(tmp_path: Path) -> None:
    # Without a script `traceh chat` must still survive more than one turn.
    args = build_parser().parse_args(
        ["chat", str(tmp_path), "--env-file", str(tmp_path / "missing.env")]
    )
    _configure_from_environment(args)
    provider, model = _provider_and_model(args)

    store = InMemoryEventStore()
    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data", provider="scripted", model=model),
        provider=provider,
        event_store=store,
    )
    console = FakeConsole(("first", "second", "/exit"))

    assert await run_chat(runtime, console.console, workspace=tmp_path) == 0

    assert "error:" not in console.output
    assert console.output.count("assistant> ") == 2
    session_id = (await runtime.sessions.list_sessions())[0]
    events = await runtime.sessions.read_session(session_id)
    assert event_types(events).count("turn/start") == 2
    assert event_types(events).count("turn/end") == 2


async def test_an_explicit_script_still_reports_exhaustion(tmp_path: Path) -> None:
    # An explicit script is a fixture: running past its end stays an error.
    script = tmp_path / "one-response.json"
    script.write_text('[{"content": "only answer"}]', encoding="utf-8")
    args = build_parser().parse_args(
        [
            "chat",
            str(tmp_path),
            "--script",
            str(script),
            "--env-file",
            str(tmp_path / "missing.env"),
        ]
    )
    _configure_from_environment(args)
    provider, model = _provider_and_model(args)

    runtime = build_default_runtime(
        RuntimeConfig(data_dir=tmp_path / "data", provider="scripted", model=model),
        provider=provider,
        event_store=InMemoryEventStore(),
    )
    console = FakeConsole(("first", "second", "/exit"))

    assert await run_chat(runtime, console.console, workspace=tmp_path) == 0

    assert "assistant> only answer" in console.output
    assert "error: ScriptExhaustedError" in console.output


# --- internal commands --------------------------------------------------------


async def test_internal_commands_and_blank_input_never_create_turns(tmp_path: Path) -> None:
    store = InMemoryEventStore()
    runtime, provider = build_runtime(tmp_path, store)
    console = FakeConsole(("", "   ", "/help", "/session", "/bogus", "/exit"))

    assert await run_chat(runtime, console.console, workspace=tmp_path) == 0

    session_id = (await runtime.sessions.list_sessions())[0]
    events = await runtime.sessions.read_session(session_id)
    assert event_types(events) == ["session/created"]
    assert user_messages(events) == []
    assert provider.requests == []
    assert "/help" in console.output
    assert "unknown command (try /help)" in console.output
    assert console.output.count(f"session_id={session_id}") == 2  # banner plus /session


async def test_quit_leaves_the_chat(tmp_path: Path) -> None:
    runtime, provider = build_runtime(tmp_path, InMemoryEventStore())
    console = FakeConsole(("/quit", "never reached"))

    assert await run_chat(runtime, console.console, workspace=tmp_path) == 0
    assert provider.requests == []


async def test_a_sentence_that_contains_a_slash_is_not_a_command(tmp_path: Path) -> None:
    store = InMemoryEventStore()
    runtime, _ = build_runtime(tmp_path, store)
    text = "please run /help inside the repository and tell me what it prints"
    console = FakeConsole((text, "/exit"))

    assert await run_chat(runtime, console.console, workspace=tmp_path) == 0

    session_id = (await runtime.sessions.list_sessions())[0]
    assert user_messages(await runtime.sessions.read_session(session_id)) == [text]


# --- failures, EOF and interruption -------------------------------------------


async def test_turn_failure_is_reported_and_the_chat_continues(tmp_path: Path) -> None:
    store = InMemoryEventStore()
    runtime, _ = build_runtime(tmp_path, store, provider=FlakyProvider(failures=1))
    console = FakeConsole(("first question", "second question", "/exit"))

    assert await run_chat(runtime, console.console, workspace=tmp_path) == 0

    assert "error: RuntimeError: provider exploded" in console.output
    assert "assistant> answer after the failure" in console.output
    session_id = (await runtime.sessions.list_sessions())[0]
    events = await runtime.sessions.read_session(session_id)
    types = event_types(events)
    assert "runtime/error" in types
    assert types.count("turn/start") == 2
    assert types.count("turn/end") == 2
    assert not CoreInvariantChecker().check(events, await runtime.sessions.read_effects(session_id))


async def test_end_of_input_exits_and_disposes_the_runtime(tmp_path: Path) -> None:
    runtime, _ = build_runtime(tmp_path, InMemoryEventStore())
    console = FakeConsole(("a question",))

    assert await run_chat(runtime, console.console, workspace=tmp_path) == 0

    session_id = (await runtime.sessions.list_sessions())[0]
    with pytest.raises(RuntimeError, match="disposed"):
        await runtime.run_existing(session_id, "after dispose")


async def test_interrupt_converges_the_session_and_prints_how_to_resume(tmp_path: Path) -> None:
    store = InMemoryEventStore()
    runtime, _ = build_runtime(tmp_path, store)
    console = FakeConsole(("a question", KeyboardInterrupt()))

    code = await run_chat(runtime, console.console, workspace=tmp_path)

    assert code == INTERRUPTED_EXIT_CODE
    session_id = (await runtime.sessions.list_sessions())[0]
    # Shell-agnostic: the exact quoting depends on the rendered shell.
    assert "resume later" in console.output
    assert session_id in console.output

    events = await runtime.sessions.read_session(session_id)
    projection = StateProjector().project(events)
    assert projection.open_turn_id is None
    assert projection.open_step_id is None
    assert not CoreInvariantChecker().check(events, await runtime.sessions.read_effects(session_id))
    with pytest.raises(RuntimeError, match="disposed"):
        await runtime.run_existing(session_id, "after dispose")


async def test_interrupting_a_running_turn_converges_it(tmp_path: Path) -> None:
    store = InMemoryEventStore()
    provider = GatedProvider()
    runtime, _ = build_runtime(tmp_path, store, provider=provider)
    console = FakeConsole(("a slow question", "/exit"))

    chat = asyncio.create_task(run_chat(runtime, console.console, workspace=tmp_path))
    await asyncio.wait_for(provider.started.wait(), timeout=10)
    session_id = (await runtime.sessions.list_sessions())[0]

    # Exactly the cancellation path Ctrl+C triggers while a turn is in flight.
    assert await runtime.cancel(session_id) is True
    # An interrupted turn no longer ends the chat: the session stays open and the
    # user leaves on their own terms, so this exits 0 via the following /exit.
    assert await chat == 0
    assert INTERRUPTED_TURN_NOTICE in console.output

    events = await runtime.sessions.read_session(session_id)
    types = event_types(events)
    assert "runtime/cancel-requested" in types
    assert types.count("model/attempt-end") == 1
    projection = StateProjector().project(events)
    assert projection.open_turn_id is None
    assert projection.open_step_id is None
    assert not CoreInvariantChecker().check(events, await runtime.sessions.read_effects(session_id))
    # Shell-agnostic: the exact quoting depends on the rendered shell.
    assert "resume later" in console.output
    assert session_id in console.output
    with pytest.raises(RuntimeError, match="disposed"):
        await runtime.run_existing(session_id, "after dispose")


# --- encoding -----------------------------------------------------------------


async def test_chinese_input_and_output_survive_the_console(tmp_path: Path) -> None:
    question = "请把 README 里的中文说明整理成一段摘要，并保留原有术语。"
    answer = "已经整理完毕：摘要保留了全部术语，未改动代码。"
    store = InMemoryEventStore()
    runtime, _ = build_runtime(
        tmp_path, store, provider=ScriptedLlmProvider((ModelResponse(content=answer),))
    )
    console = FakeConsole((question, "/exit"))

    assert await run_chat(runtime, console.console, workspace=tmp_path) == 0

    session_id = (await runtime.sessions.list_sessions())[0]
    assert user_messages(await runtime.sessions.read_session(session_id)) == [question]
    assert f"assistant> {answer}" in console.output


async def test_undecodable_input_is_refused_before_reaching_the_runtime(tmp_path: Path) -> None:
    store = InMemoryEventStore()
    runtime, provider = build_runtime(tmp_path, store)
    console = FakeConsole(("���这是乱码", "/exit"))

    assert await run_chat(runtime, console.console, workspace=tmp_path) == 0

    session_id = (await runtime.sessions.list_sessions())[0]
    events = await runtime.sessions.read_session(session_id)
    assert user_messages(events) == []
    assert event_types(events) == ["session/created"]
    assert provider.requests == []
    assert "could not be decoded" in console.output


async def test_a_byte_order_mark_never_reaches_the_event_log(tmp_path: Path) -> None:
    # Windows PowerShell 5.1's `Out-File -Encoding utf8` prefixes U+FEFF; it is
    # part of the stream, not of what the user typed.
    question = "请总结这个仓库"
    store = InMemoryEventStore()
    runtime, _ = build_runtime(tmp_path, store)
    console = FakeConsole((f"﻿{question}", "/exit"))

    assert await run_chat(runtime, console.console, workspace=tmp_path) == 0

    session_id = (await runtime.sessions.list_sessions())[0]
    assert user_messages(await runtime.sessions.read_session(session_id)) == [question]


def test_normalize_input_only_removes_stream_artefacts() -> None:
    assert normalize_input("﻿  中文 输入  ") == "中文 输入"
    assert normalize_input("keep  inner   spacing") == "keep  inner   spacing"
    assert normalize_input("﻿/exit") == "/exit"


def test_contains_undecodable_input_only_flags_the_replacement_character() -> None:
    assert contains_undecodable_input("abc�")
    assert not contains_undecodable_input("完全正常的中文和 ASCII")


def test_configure_stdio_degrades_safely_on_streams_without_reconfigure() -> None:
    plain = io.StringIO()
    report = configure_stdio(stdin=plain, stdout=plain, stderr=plain)
    assert report.stdin is False
    assert report.stdout is False
    assert report.stderr is False


def test_configure_stdio_degrades_on_an_incompatible_reconfigure_signature() -> None:
    class IncompatibleStream:
        def reconfigure(self) -> None:
            return None

    stream = IncompatibleStream()
    report = configure_stdio(stdin=stream, stdout=stream, stderr=stream)
    assert report == type(report)(False, False, False)


def test_configure_stdio_applies_utf8_to_real_text_streams() -> None:
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    report = configure_stdio(stdin=stream, stdout=stream, stderr=stream)
    assert report.stdout is True
    assert stream.encoding.lower().replace("-", "") == "utf8"
    assert stream.errors == "replace"


class _LegacyWindowsTextStream:
    """A strict GBK-like stream until the CLI applies its UTF-8 policy."""

    def __init__(self) -> None:
        self.encoding = "gbk"
        self.errors = "strict"
        self.parts: list[str] = []

    def reconfigure(self, *, encoding: str, errors: str) -> None:
        self.encoding = encoding
        self.errors = errors

    def write(self, value: str) -> int:
        value.encode(self.encoding, errors=self.errors)
        self.parts.append(value)
        return len(value)

    def flush(self) -> None:
        return None


def test_public_replay_configures_unicode_output_before_rendering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    async def seed() -> str:
        runtime = build_default_runtime(
            RuntimeConfig(
                data_dir=data_dir,
                provider="scripted",
                model="scripted-model",
            ),
            provider=ScriptedLlmProvider(
                (ModelResponse(content="verified ✅"),), repeat_last=True
            ),
        )
        try:
            session_id = await runtime.create_session(workspace)
            await runtime.run_existing(session_id, "respond")
            return session_id
        finally:
            await runtime.dispose()

    session_id = asyncio.run(seed())
    stdout = _LegacyWindowsTextStream()
    stderr = _LegacyWindowsTextStream()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    with pytest.raises(SystemExit) as caught:
        main(["replay", session_id, "--data-dir", str(data_dir)])

    assert caught.value.code == 0
    assert stdout.encoding == "utf-8"
    assert "verified ✅" in "".join(stdout.parts)
